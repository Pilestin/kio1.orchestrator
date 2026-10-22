import json
import logging
import urllib.error
import urllib.request
from typing import Any
from uuid import UUID, uuid4

from opentelemetry.propagate import inject

logger = logging.getLogger(__name__)

_MAX_ERROR_BODY_CHARS = 512


class SessionClientError(Exception):
    """Raised when the Session Manager could not be reached or refused a call.

    Attributes:
        status: The HTTP status the Session Manager answered with, or None
            when it was not reached. A 404 means the conversation or session
            does not exist, which a caller reports differently from an outage.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SessionClient:
    """Talks to the Session Manager service over HTTP.

    Opening a session is on the critical path, so `create_session` raises on
    failure. Everything after it is best effort: losing a step record is worse
    than nothing, but it is not a reason to abandon a workflow that is already
    running against real agents.
    """

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        """Store the service location.

        Args:
            base_url: Session Manager base URL, with or without a trailing slash.
            timeout: Seconds to wait for each call.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def create_session(
        self, request_text: str, conversation_id: UUID | None = None
    ) -> UUID:
        """Open a session and return its identifier.

        Args:
            request_text: The user request the workflow was built from.
            conversation_id: The conversation it continues; None opens a
                conversation for it.

        Returns:
            The new session identifier.

        Raises:
            SessionClientError: If the session could not be opened, with
                status 404 when the conversation does not exist.
        """
        body: dict[str, Any] = {"request_text": request_text}

        if conversation_id is not None:
            body["conversation_id"] = str(conversation_id)

        payload = self._call("POST", "/sessions", body)
        session_id = payload.get("session_id")

        if not isinstance(session_id, str):
            raise SessionClientError("Session Manager did not return a session_id")

        try:
            return UUID(session_id)
        except ValueError as error:
            raise SessionClientError(
                f"Session Manager returned a malformed session_id: {session_id!r}"
            ) from error

    def update_session(self, session_id: UUID, **fields: Any) -> None:
        """Record progress on a session, ignoring failures.

        Args:
            session_id: The session to update.
            **fields: Any subset of `status`, `workflow_id`, `execution_mode`,
                and `error`. Fields set to None are dropped.

        Returns:
            None.
        """
        body = {key: value for key, value in fields.items() if value is not None}

        if not body:
            return

        self._call_quietly("PATCH", f"/sessions/{session_id}", body)

    def record_step(self, session_id: UUID, step: dict[str, Any]) -> None:
        """Record one step outcome, ignoring failures.

        Args:
            session_id: The session the step belongs to.
            step: A `RecordStepRequest`-shaped object.

        Returns:
            None.
        """
        self._call_quietly("POST", f"/sessions/{session_id}/steps", step)

    def get_session(self, session_id: UUID) -> dict[str, Any]:
        """Return a session with its steps and lineage.

        Args:
            session_id: The session to read.

        Returns:
            The decoded `SessionDetail` object.

        Raises:
            SessionClientError: If the session could not be read.
        """
        return self._call("GET", f"/sessions/{session_id}", None)

    def list_sessions(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return the most recent sessions, newest first.

        Args:
            limit: How many to return.

        Returns:
            Decoded `Session` objects without their steps.

        Raises:
            SessionClientError: If the list could not be read.
        """
        decoded = self._request("GET", f"/sessions?limit={int(limit)}", None)

        if not isinstance(decoded, list):
            raise SessionClientError(
                f"Session Manager returned {type(decoded).__name__}, "
                "expected a JSON array of sessions"
            )

        return [item for item in decoded if isinstance(item, dict)]

    def create_conversation(self) -> dict[str, Any]:
        """Open an empty conversation.

        Returns:
            The decoded `Conversation`.

        Raises:
            SessionClientError: If it could not be opened.
        """
        return self._call("POST", "/conversations", {})

    def list_conversations(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return the conversations most recently added to, newest first.

        Raises:
            SessionClientError: If the list could not be read.
        """
        decoded = self._request("GET", f"/conversations?limit={int(limit)}", None)

        if not isinstance(decoded, list):
            raise SessionClientError(
                f"Session Manager returned {type(decoded).__name__}, "
                "expected a JSON array of conversations"
            )

        return [item for item in decoded if isinstance(item, dict)]

    def get_conversation(self, conversation_id: UUID) -> dict[str, Any]:
        """Return a conversation with each of its runs.

        Returns:
            The decoded `ConversationDetail`.

        Raises:
            SessionClientError: If it could not be read, with status 404 when
                it does not exist.
        """
        return self._call("GET", f"/conversations/{conversation_id}", None)

    def delete_conversation(self, conversation_id: UUID) -> bool:
        """Delete a conversation and every run in it.

        Returns:
            True when it was deleted, False when it did not exist.

        Raises:
            SessionClientError: If the Session Manager could not do it.
        """
        try:
            self._request("DELETE", f"/conversations/{conversation_id}", None)
        except SessionClientError as error:
            if error.status == 404:
                return False

            raise

        return True

    def _call(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Send one request and decode the reply as an object.

        Args:
            method: HTTP method.
            path: Path below the base URL, starting with a slash.
            body: JSON request body, or None for a bodyless request.

        Returns:
            The decoded response object.

        Raises:
            SessionClientError: If the request failed or the reply was not a
                JSON object.
        """
        decoded = self._request(method, path, body)

        if not isinstance(decoded, dict):
            raise SessionClientError(
                f"Session Manager returned {type(decoded).__name__}, "
                "expected a JSON object"
            )

        return decoded

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        """Send one request and decode whatever JSON came back.

        Args:
            method: HTTP method.
            path: Path below the base URL, starting with a slash.
            body: JSON request body, or None for a bodyless request.

        Returns:
            The decoded reply, an object or an array.

        Raises:
            SessionClientError: If the request failed or the reply was
                not JSON.
        """
        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {}

        if body is not None:
            headers["Content-Type"] = "application/json"

        # Carries the active span so the Session Manager's own spans can join
        # this trace once that service is instrumented.
        inject(headers)

        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise SessionClientError(
                f"Session Manager returned HTTP {error.code} for "
                f"{method} {path}: {_read_error_body(error)}",
                status=error.code,
            ) from error
        except urllib.error.URLError as error:
            raise SessionClientError(
                f"Session Manager is unreachable at {url}: {error.reason}"
            ) from error
        except TimeoutError as error:
            raise SessionClientError(
                f"Session Manager did not answer {method} {path} within "
                f"{self._timeout:g}s"
            ) from error

        if not raw:
            return None  # 204: a deletion has nothing to say

        try:
            decoded: Any = json.loads(raw)
        except json.JSONDecodeError as error:
            raise SessionClientError(
                f"Session Manager returned a non-JSON reply: {error}"
            ) from error

        return decoded

    def _call_quietly(self, method: str, path: str, body: dict[str, Any]) -> None:
        """Send one request and log, rather than raise, on failure.

        Args:
            method: HTTP method.
            path: Path below the base URL, starting with a slash.
            body: JSON request body.

        Returns:
            None.
        """
        try:
            self._call(method, path, body)
        except SessionClientError:
            logger.warning(
                "Session Manager call failed and was skipped: %s %s",
                method,
                path,
                exc_info=True,
            )


class NullSessionClient:
    """Records nothing, so callers need no branch when recording is disabled."""

    def create_session(
        self, request_text: str, conversation_id: UUID | None = None
    ) -> UUID:
        """Return a locally generated identifier without contacting a service."""
        return uuid4()

    def create_conversation(self) -> dict[str, Any]:
        """Return a conversation that exists only in the reply.

        A chat still needs an identifier to group its requests under, even
        when nothing is kept; recording being off is not a failure.
        """
        return {"conversation_id": str(uuid4()), "title": ""}

    def list_conversations(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return an empty list: nothing is recorded, as `list_sessions`."""
        return []

    def get_conversation(self, conversation_id: UUID) -> dict[str, Any]:
        """Report that nothing was recorded.

        Raises:
            SessionClientError: Always, as a 404.
        """
        raise SessionClientError(
            "Session recording is disabled; no conversation is available",
            status=404,
        )

    def delete_conversation(self, conversation_id: UUID) -> bool:
        """Report that there was nothing to delete."""
        return False

    def update_session(self, session_id: UUID, **fields: Any) -> None:
        """Do nothing."""

    def record_step(self, session_id: UUID, step: dict[str, Any]) -> None:
        """Do nothing."""

    def get_session(self, session_id: UUID) -> dict[str, Any]:
        """Report that nothing was recorded.

        Raises:
            SessionClientError: Always, because no session was stored.
        """
        raise SessionClientError(
            "Session recording is disabled; no session detail is available"
        )

    def list_sessions(self, limit: int = 25) -> list[dict[str, Any]]:
        """Report that there is nothing to list.

        Returns:
            An empty list. A console asking what has run recently wants
            an empty answer, not an error: nothing has been recorded
            because recording is off, which is not a failure.
        """
        return []


def _read_error_body(error: urllib.error.HTTPError) -> str:
    """Return a bounded, single-line description of an HTTP error body.

    Args:
        error: The raised HTTP error.

    Returns:
        The truncated response body, or a placeholder when it is unreadable.
    """
    try:
        body = error.read().decode("utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return "<unreadable body>"

    if not body:
        return "<empty body>"

    collapsed = " ".join(body.split())

    if len(collapsed) > _MAX_ERROR_BODY_CHARS:
        return f"{collapsed[:_MAX_ERROR_BODY_CHARS]}..."

    return collapsed
