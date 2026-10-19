import json
import logging
import sys
import urllib.request
from formatter import format_json
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


def main() -> None:
    """Run the orchestrator terminal application."""
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

    print("KIO1 Orchestrator (type 'exit' to quit)")
    print("-" * 40)

    turn = 0
    session_success = False

    with measure_duration() as session_elapsed:
        try:
            while True:
                try:
                    query = input("\n> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nGoodbye.")
                    break

                if not query:
                    continue

                if query.lower() == "exit":
                    print("Goodbye.")
                    break

                turn += 1
                logger.info("Turn started: turn=%d", turn)

                try:
                    content = ""

                    with (
                        measure_duration() as turn_elapsed,
                        trace_turn(
                            session_id=session_id,
                            turn_number=turn,
                            provider=PROVIDER_NAME,
                            model=config.model,
                        ),
                    ):
                        with measure_duration() as history_elapsed:
                            messages = load_messages(chat_file)
                            messages.append({"role": "user", "content": query})
                        logger.debug(
                            "History loaded: turn=%d duration_ms=%d message_count=%d",
                            turn,
                            history_elapsed(),
                            len(messages),
                        )

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

                        with measure_duration() as format_elapsed:
                            formatted = format_json(content)
                        logger.debug(
                            "Response formatted: turn=%d duration_ms=%d",
                            turn,
                            format_elapsed(),
                        )

                        with measure_duration() as persist_elapsed:
                            append_user_message(chat_file, query)
                            append_assistant_message(chat_file, content)
                        logger.debug(
                            "History persisted: turn=%d duration_ms=%d",
                            turn,
                            persist_elapsed(),
                        )

                        print(f"\n{formatted}")

                    logger.info(
                        "Turn completed: turn=%d duration_ms=%d",
                        turn,
                        turn_elapsed(),
                    )

                except Exception as e:
                    logger.exception(
                        "Turn failed: turn=%d duration_ms=%d",
                        turn,
                        turn_elapsed(),
                    )
                    print(f"\nError: {e}", file=sys.stderr)
                    if content:
                        print(f"Raw response: {content!r}", file=sys.stderr)
            session_success = True
        finally:
            record_session_completed(
                provider=PROVIDER_NAME,
                model=config.model,
                success=session_success,
            )
            logger.info(
                "Session ended: turns=%d duration_ms=%d",
                turn,
                session_elapsed(),
            )
            shutdown_telemetry()


if __name__ == "__main__":
    main()
