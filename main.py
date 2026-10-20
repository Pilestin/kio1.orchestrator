import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

from chat_history import (
    append_assistant_message,
    append_user_message,
    create_chat_file,
    load_messages,
)
from config_loader import Config, load_config
from prompt_loader import load_prompt
from session_logger import generate_session_id, init_logger, measure_duration
from telemetry import (
    init_telemetry,
    record_gen_ai_response,
    record_session_completed,
    record_session_started,
    shutdown_telemetry,
    trace_gen_ai_request,
    trace_operation,
    trace_provider_preload,
    trace_turn,
)

logger = logging.getLogger("main")

# KIO1 only talks to a local Ollama install, so the provider name is a fixed
# label for telemetry rather than a configurable value.
PROVIDER_NAME = "ollama"


def preload_model(config: Config) -> None:
    """Preload a model into Ollama's memory.

    The context window is sent here as well as on requests, because Ollama
    allocates its cache when the model loads. Loading with a smaller window
    than requests use would force a reload on the first request.

    Args:
        config: Application configuration.

    Returns:
        None.
    """
    url = f"{config.endpoint}/api/chat"
    payload = {
        "model": config.model,
        "messages": [],
        "keep_alive": config.keep_alive,
        "options": {
            "num_ctx": config.context_window_size,
        },
    }

    request_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=request_data,
        headers={"Content-Type": "application/json"},
    )

    with measure_duration() as elapsed:
        with urllib.request.urlopen(req, timeout=config.request_timeout) as response:
            response.read()

    logger.info("Model preloaded: model=%s duration_ms=%d", config.model, elapsed())


def send_request(
    config: Config,
    system_prompt: str,
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    """Send a chat completion request to the Ollama API.

    Args:
        config: Application configuration.
        system_prompt: The system prompt text.
        messages: List of message dicts with `role` and `content` keys.

    Returns:
        The parsed JSON response from the model as a dictionary.

    Raises:
        ValueError: If the response was truncated by the context window or output limit.
    """
    url = f"{config.endpoint}/api/chat"

    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            *messages,
        ],
        "stream": False,
        "format": "json",
        "keep_alive": config.keep_alive,
        "options": {
            "temperature": config.temperature,
            "num_ctx": config.context_window_size,
            "num_predict": config.max_output_tokens,
        },
    }

    logger.debug("Request payload: %s", payload)

    request_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=request_data,
        headers={"Content-Type": "application/json"},
    )

    with measure_duration() as elapsed:
        with urllib.request.urlopen(req, timeout=config.request_timeout) as response:
            response_data = json.loads(response.read().decode("utf-8"))

    duration_ms = elapsed()
    finish_reason = response_data.get("done_reason")
    input_tokens = response_data.get("prompt_eval_count")
    output_tokens = response_data.get("eval_count")
    tokens_per_second = (
        output_tokens / (duration_ms / 1000)
        if isinstance(output_tokens, int) and duration_ms > 0
        else None
    )

    record_gen_ai_response(
        provider=PROVIDER_NAME,
        request_model=config.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        response_model=response_data.get("model"),
        finish_reason=finish_reason,
        truncated=finish_reason == "length",
        truncation_reason="length",
    )

    logger.info(
        "Response received: model=%s duration_ms=%d input_tokens=%s "
        "output_tokens=%s tokens_per_second=%s",
        config.model,
        duration_ms,
        input_tokens,
        output_tokens,
        f"{tokens_per_second:.1f}" if tokens_per_second is not None else "n/a",
    )
    logger.debug("Response: %s", response_data)

    if finish_reason == "length":
        raise ValueError(
            f"Response truncated before completion: "
            f"prompt={response_data.get('prompt_eval_count')} tokens, "
            f"output={response_data.get('eval_count')} tokens, "
            f"context_window_size={config.context_window_size}, "
            f"max_output_tokens={config.max_output_tokens}. "
            "Increase max_output_tokens if the output limit was reached; "
            "otherwise increase context_window_size or start a new session."
        )

    return response_data


def extract_content(response: dict[str, Any]) -> str:
    """Extract the assistant message content from an Ollama response.

    Args:
        response: The full Ollama API response dictionary.

    Returns:
        The assistant's message content as a string.
    """
    return response["message"]["content"].strip()


def run_turn(
    config: Config,
    system_prompt: str,
    chat_file: Path,
    session_id: str,
    turn: int,
    query: str,
) -> str:
    """Run one chat turn: send `query` to Ollama and persist the exchange.

    Args:
        config: Application configuration.
        system_prompt: The system prompt text.
        chat_file: Path to the session's chat history file.
        session_id: Shared session id used for tracing.
        turn: 1-based turn number, used for tracing.
        query: The user's query text.

    Returns:
        The raw assistant message content (not yet formatted).
    """
    with trace_turn(
        session_id=session_id,
        turn_number=turn,
        provider=PROVIDER_NAME,
        model=config.model,
    ):
        messages = load_messages(chat_file)
        messages.append({"role": "user", "content": query})

        with trace_gen_ai_request(
            provider=PROVIDER_NAME,
            model=config.model,
            session_id=session_id,
            turn_number=turn,
            message_count=len(messages) + 1,
            max_output_tokens=config.max_output_tokens,
            temperature=config.temperature,
        ):
            response = send_request(
                config=config,
                system_prompt=system_prompt,
                messages=messages,
            )
        content = extract_content(response)

        append_user_message(chat_file, query)
        append_assistant_message(chat_file, content)

    return content


def main() -> None:
    """Load configuration, preload the model, and serve the web UI."""
    try:
        session_id = generate_session_id()
        init_logger("logs", session_id)
        logger.info("Session started: session_id=%s", session_id)

        config = load_config()
        init_telemetry(config.telemetry, llm=config.model)

        startup_attributes = {
            "gen_ai.provider.name": PROVIDER_NAME,
            "gen_ai.request.model": config.model,
            "gen_ai.conversation.id": session_id,
        }

        with trace_operation("kio1.startup", startup_attributes):
            system_prompt = load_prompt(config.prompt_path)

            print(f"Loading Ollama model {config.model}...")
            with trace_provider_preload(
                provider=PROVIDER_NAME,
                model=config.model,
            ):
                preload_model(config)

            chat_file = create_chat_file(
                config.chat_directory, system_prompt, session_id
            )
            record_session_started(
                provider=PROVIDER_NAME,
                model=config.model,
            )

    except Exception:
        logger.exception("Startup failed")
        print(
            "Error: failed to start orchestrator. Check logs for details.",
            file=sys.stderr,
        )
        shutdown_telemetry()
        sys.exit(1)

    # Imported lazily: webapp.py imports run_turn/PROVIDER_NAME back from this
    # module, so importing it at module load time would be circular.
    from webapp import create_app

    app = create_app(
        config=config,
        system_prompt=system_prompt,
        chat_file=chat_file,
        session_id=session_id,
    )

    host = os.environ.get("KIO1_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("KIO1_WEB_PORT", "5000"))

    print(f"KIO1 Orchestrator web UI: http://{host}:{port}")

    session_success = False
    with measure_duration() as session_elapsed:
        try:
            # threaded=True: Werkzeug's dev server otherwise handles one
            # request at a time, so a file upload would stall behind an
            # in-flight (potentially slow) Ollama chat request.
            app.run(
                host=host, port=port, debug=False, use_reloader=False, threaded=True
            )
            session_success = True
        finally:
            record_session_completed(
                provider=PROVIDER_NAME,
                model=config.model,
                success=session_success,
            )
            logger.info(
                "Session ended: duration_ms=%d",
                session_elapsed(),
            )
            shutdown_telemetry()


if __name__ == "__main__":
    main()
