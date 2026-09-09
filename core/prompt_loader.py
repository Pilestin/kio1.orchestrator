from pathlib import Path

from dispatch.agent_registry import (
    KIND_CLI,
    KIND_HTTP,
    AgentEndpoint,
    AgentRegistry,
)

# Where the generated catalog is substituted into a prompt file. A prompt
# without it is used unchanged, so an existing prompt keeps working and a
# hand-written catalog stays possible.
CATALOG_PLACEHOLDER = "{{AGENT_CATALOG}}"

_KIND_LABELS = {
    KIND_HTTP: "HTTP API",
    KIND_CLI: "local command",
}


def _describe(entry: AgentEndpoint) -> str:
    """Return the catalog line body for one registered target.

    Args:
        entry: The registry entry to describe.

    Returns:
        The entry's description, or a readable fallback built from its
        capabilities when the configuration gives none.
    """
    if entry.description:
        return entry.description

    if entry.capabilities:
        return f"Handles {', '.join(sorted(entry.capabilities))}."

    return "No description configured."


def render_catalog(registry: AgentRegistry) -> str:
    """Render the registered targets as the catalog the planner reads.

    The catalog is generated rather than written by hand because the same
    entries also decide routing. Two hand-maintained copies drift, and the
    drift is invisible until a plan is dispatched: the model names a
    capability nothing accepts, or never names one that exists.

    Args:
        registry: The configured targets.

    Returns:
        A Markdown fragment listing every target, its kind when it is not a
        KIO agent, what it does, and the capability values a plan may ask it
        for. Empty configuration yields an explicit note rather than nothing,
        so a misconfigured registry is visible in the prompt.
    """
    entries = registry.entries()

    if not entries:
        return (
            "_No targets are registered. Answer that there is nothing to "
            "plan against._"
        )

    lines: list[str] = []

    for entry in entries:
        label = _KIND_LABELS.get(entry.kind)
        suffix = f" _({label})_" if label else ""
        lines.append(f"- **{entry.agent_id}**{suffix} — {_describe(entry)}")

        if entry.capabilities:
            capabilities = ", ".join(f"`{name}`" for name in sorted(entry.capabilities))
            lines.append(f"  - capabilities: {capabilities}")

    return "\n".join(lines)


def fill_catalog(prompt: str, registry: AgentRegistry) -> str:
    """Substitute the catalog into an already-loaded prompt.

    Separate from `load_prompt` because the registry changes while KIO1 runs:
    the file is read once, and this is applied per request against whatever is
    reachable at that moment.

    Args:
        prompt: The prompt text as written on disk.
        registry: The targets to describe.

    Returns:
        The prompt, unchanged when it does not ask for a catalog.
    """
    if CATALOG_PLACEHOLDER not in prompt:
        return prompt

    return prompt.replace(CATALOG_PLACEHOLDER, render_catalog(registry))


def load_prompt(prompt_path: str, registry: AgentRegistry | None = None) -> str:
    """Load a system prompt from a text file.

    Args:
        prompt_path: Path to the prompt file.
        registry: Configured targets used to fill the catalog placeholder.
            Omitted leaves the prompt exactly as written on disk.

    Returns:
        The prompt text, with the catalog substituted when the file asks for
        it and a registry was supplied.
    """
    path = Path(prompt_path)
    prompt = path.read_text(encoding="utf-8").strip()

    if registry is None:
        return prompt

    return fill_catalog(prompt, registry)
