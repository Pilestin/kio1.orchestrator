import ast
import json
import logging
from typing import Any

from core.telemetry import (
    record_format_fallback,
    record_workflow_plan,
    trace_operation,
)

logger = logging.getLogger(__name__)


def format_json(raw_json: str) -> str:
    """Parse and pretty-print a JSON string.

    Falls back to `ast.literal_eval` if standard JSON parsing fails
    (e.g., when the model returns single-quoted Python dicts).

    Args:
        raw_json: A raw JSON string to format.

    Returns:
        A formatted JSON string with 2-space indentation.

    Raises:
        ValueError: If the content parses as neither JSON nor a Python literal.
    """
    with trace_operation("kio1.response.format"):
        parsed = parse_response(raw_json)
        _record_workflow_metadata(parsed)

        return json.dumps(parsed, indent=2, ensure_ascii=False)


def parse_response(raw_json: str) -> Any:
    """Decode a model response into a Python value.

    Falls back to `ast.literal_eval` if standard JSON parsing fails
    (e.g., when the model returns single-quoted Python dicts).

    Args:
        raw_json: A raw JSON string from the model.

    Returns:
        The decoded value.

    Raises:
        ValueError: If the content parses as neither JSON nor a Python literal.
    """
    normalized_content = _strip_markdown_fence(raw_json)

    try:
        return json.loads(normalized_content)
    except json.JSONDecodeError:
        logger.warning("Standard JSON parsing failed, falling back to ast.literal_eval")

    try:
        parsed: Any = ast.literal_eval(normalized_content)
    except (ValueError, SyntaxError) as error:
        raise ValueError(
            f"Content is neither valid JSON nor a Python literal: {error}"
        ) from error

    record_format_fallback()

    return parsed


def _record_workflow_metadata(parsed: Any) -> None:
    """Record non-content workflow structure from a parsed response.

    Args:
        parsed: Parsed response value.

    Returns:
        None.
    """
    if not isinstance(parsed, dict):
        return

    workflow_id = parsed.get("workflow_id")
    execution_mode = parsed.get("execution_mode")
    steps = parsed.get("steps")

    if (
        not isinstance(workflow_id, str)
        or not workflow_id
        or not isinstance(execution_mode, str)
        or not isinstance(steps, list)
    ):
        return

    record_workflow_plan(
        workflow_id=workflow_id,
        execution_mode=execution_mode,
        step_count=len(steps),
    )


def _strip_markdown_fence(content: str) -> str:
    """Remove an optional Markdown code fence.

    Args:
        content: Raw text that may be wrapped in a Markdown code fence.

    Returns:
        The content with the surrounding code fence removed, if present;
        otherwise the original stripped content unchanged.
    """
    stripped_content = content.strip()

    if not stripped_content.startswith("```"):
        return stripped_content

    lines = stripped_content.splitlines()

    if len(lines) < 3 or lines[-1].strip() != "```":
        return stripped_content

    return "\n".join(lines[1:-1]).strip()
