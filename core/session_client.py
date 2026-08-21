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
    """Raised when the Session Manager could not be reached or refused a call."""


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

    def create_session(self, request_text: str) -> UUID:
        """Open a session and return its identifier.

        Args:
            request_text: The user request the workflow was built from.

        Returns:
            The new session identifier.

        Raises:
            SessionClientError: If the session could not be opened.
        """
        payload = self._call("POST", "/sessions", {"request_text": request_text})
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

    def _call(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Send one request and decode the reply.

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
                f"{method} {path}: {_read_error_body(error)}"
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

        try:
            decoded: Any = json.loads(raw)
        except json.JSONDecodeError as error:
            raise SessionClientError(
                f"Session Manager returned a non-JSON reply: {error}"
            ) from error

        if not isinstance(decoded, dict):
            raise SessionClientError(
                f"Session Manager returned {type(decoded).__name__}, "
                "expected a JSON object"
            )

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

    def create_session(self, request_text: str) -> UUID:
        """Return a locally generated identifier without contacting a service."""
        return uuid4()

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
