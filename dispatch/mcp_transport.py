"""The transport that runs a plan step on a Model Context Protocol server.

Every other transport adapts one shape to another and is done. This one has a
genuine gap to close, and it is worth naming rather than hiding: **a plan step
carries a free-text task, and an MCP tool takes structured arguments.**
`{"task": "read the pricing module"}` is not `{"path": "app/pricing.py"}`, and
no amount of string handling turns one into the other.

Three answers, tried in order, and the order is the design:

1. **The planner said.** A step may carry an `arguments` object, which is
   passed through untouched. The planner can do this because the catalog it
   reads lists each tool's arguments — discovered from the server, not
   guessed — so it is filling in a form it has actually seen. This is the
   path that should be taken, and the prompt asks for it.
2. **There is only one thing it could mean.** A tool whose schema needs
   exactly one required string gets the task as that string. A great many MCP
   tools are shaped this way — a query, a path, a command — and demanding
   ceremony for them would cost more than it bought.
3. **Nothing is needed.** A tool with no required arguments is called with
   none.

Anything else fails the step with a message naming the arguments the server
wants. That is deliberate: inventing values for a tool that writes files or
opens issues is worse than a step that says what it needed.
"""

import logging
from typing import Any

from dispatch.agent_registry import AgentEndpoint, McpToolSpec
from dispatch.agent_transport import AgentTransportError
from dispatch.mcp_client import (
    HttpMcpSession,
    McpError,
    McpSession,
    McpSessionPool,
    McpTool,
    flatten_content,
)
from dispatch.tool_transport import expand_environment, protocol_reply, render
from dispatch.workflow_planner import ExecutionMessage

logger = logging.getLogger(__name__)

# Where a plan step puts the arguments it worked out for a tool. It travels
# inside `data`, the protocol's own extension point, so nothing about the
# KIO1-KIO10 message shape has to change to carry it.
ARGUMENTS_KEY = "arguments"


def session_factory(agent: AgentEndpoint, spec: McpToolSpec, timeout: float) -> Any:
    """Return a callable that opens a session for one MCP target.

    Args:
        agent: The registry entry, for its identifier.
        spec: The connection details.
        timeout: Seconds a handshake or listing may take.

    Returns:
        A zero-argument callable the pool invokes when it needs a session.

    Raises:
        AgentTransportError: If a referenced environment variable is unset.
            Raised now rather than inside the pool, so a missing token is
            reported against the target that needs it.
    """
    # Imported lazily so a test that stubs the stdio session does not have to
    # have a real one importable.
    from dispatch.mcp_client import StdioMcpSession

    if spec.transport == "stdio":
        environment = {
            name: expand_environment(
                value, agent_id=agent.agent_id, field=f"env {name}"
            )
            for name, value in spec.env.items()
        }

        def open_stdio() -> McpSession:
            return StdioMcpSession(
                agent.agent_id,
                spec.command,
                cwd=spec.cwd,
                env=environment,
                timeout=timeout,
            )

        return open_stdio

    headers = {
        name: expand_environment(value, agent_id=agent.agent_id, field=f"header {name}")
        for name, value in spec.headers.items()
    }

    def open_http() -> McpSession:
        return HttpMcpSession(
            agent.agent_id, spec.url, headers=headers, timeout=timeout
        )

    return open_http


def arguments_for(message: ExecutionMessage, tool: McpTool | None) -> dict[str, Any]:
    """Work out what to send a tool, or say why it cannot be worked out.

    Args:
        message: The step being dispatched.
        tool: The tool as the server described it, or None when the server
            has not been asked yet.

    Returns:
        The arguments object to send.

    Raises:
        AgentTransportError: If the tool needs arguments that nothing in the
            step supplies.
    """
    declared = message.data.get(ARGUMENTS_KEY)

    if isinstance(declared, dict):
        # Placeholders are substituted here for the same reason they are in
        # every other configured value: a plan may reference the workspace
        # path rather than repeating it.
        rendered = render(declared, message)

        return rendered if isinstance(rendered, dict) else {}

    if tool is None:
        # Not asked yet, so there is no schema to reason about. Sending the
        # task under a conventional name is a guess, and a wrong guess on a
        # tool that writes things is worse than a clear refusal.
        raise AgentTransportError(
            f"{message.agent_id} has not been asked what tools it has, so "
            f"{message.capability!r} cannot be called with a free-text task. "
            "Probe the target first, or give the step an 'arguments' object."
        )

    sole = tool.sole_string_argument()

    if sole is not None:
        return {sole: message.task}

    if not tool.required:
        return {}

    summary = tool.summarize_arguments()

    raise AgentTransportError(
        f"{message.agent_id}.{tool.name} needs arguments ({summary}), and the "
        "step carried only a free-text task. The plan must include an "
        "'arguments' object for this tool."
    )


class McpToolTransport:
    """Runs a plan step as a `tools/call` against an MCP server."""

    def __init__(self, pool: McpSessionPool) -> None:
        """Store the pool sessions are borrowed from.

        Args:
            pool: The shared pool. Sessions outlive single calls because
                starting an MCP server is the slow part.
        """
        self._pool = pool

    def close(self) -> None:
        """Close every open session."""
        self._pool.close()

    @staticmethod
    def _tool_named(spec: McpToolSpec, name: str) -> McpTool | None:
        """Return the discovered tool with a name, or None.

        Args:
            spec: The target's connection details and last listing.
            name: The tool being asked for.

        Returns:
            The matching tool, or None when the server never reported one.
        """
        for tool in spec.discovered:
            if tool.name == name:
                return tool

        return None

    def send(
        self,
        message: ExecutionMessage,
        agent: AgentEndpoint,
        timeout: float,
    ) -> dict[str, Any]:
        """Call one tool and turn its answer into a reply.

        Args:
            message: The execution message to deliver. Its `capability` names
                the tool, which is what makes an MCP server's own tool names
                usable as capabilities without translation.
            agent: The resolved registry entry, carrying an `McpToolSpec`.
            timeout: Seconds to allow the call.

        Returns:
            The reply in the KIO1-KIO10 shape. A tool that ran and reported a
            problem is a failed step rather than a raised error, the same way
            a command exiting non-zero is.

        Raises:
            AgentTransportError: If the entry is not an MCP target, the server
                could not be reached, or the call could not be assembled.
        """
        spec = agent.tool

        if not isinstance(spec, McpToolSpec):
            raise AgentTransportError(
                f"{agent.agent_id} is not configured as an MCP server."
            )

        tool = self._tool_named(spec, message.capability)

        if tool is None and spec.discovered:
            offered = ", ".join(sorted(item.name for item in spec.discovered))
            raise AgentTransportError(
                f"{agent.agent_id} has no tool called {message.capability!r}. "
                f"It offers: {offered}."
            )

        arguments = arguments_for(message, tool)

        logger.debug(
            "Calling MCP tool: step_id=%s agent_id=%s tool=%s arguments=%s",
            message.step_id,
            agent.agent_id,
            message.capability,
            sorted(arguments),
        )

        try:
            session = self._pool.session_for(
                agent.agent_id, session_factory(agent, spec, timeout)
            )
            result = session.call_tool(message.capability, arguments, timeout)
        except McpError as error:
            # A server that failed mid-conversation is dropped rather than
            # kept: the next step should get a fresh one instead of inheriting
            # whatever state broke this one.
            self._pool.discard(agent.agent_id)
            raise AgentTransportError(str(error)) from error

        if result.get("isError"):
            reason = flatten_content(result).get("result") or "no reason given"
            logger.info(
                "MCP tool reported failure: step_id=%s agent_id=%s tool=%s",
                message.step_id,
                agent.agent_id,
                message.capability,
            )

            return protocol_reply(
                message,
                status="error",
                error=f"{agent.agent_id}.{message.capability} failed: {reason}",
            )

        return protocol_reply(message, status="ok", output=flatten_content(result))
