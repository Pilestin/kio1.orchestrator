import logging
import sys

from core.chat_history import (
    append_assistant_message,
    append_user_message,
    create_chat_file,
    load_messages,
)
from core.config_loader import load_config
from core.formatter import format_json, parse_response
from core.prompt_loader import load_prompt
from core.session_logger import (
    configure_console_encoding,
    generate_session_id,
    init_logger,
)
from core.telemetry import (
    init_telemetry,
    record_session_completed,
    record_session_started,
    shutdown_telemetry,
    trace_gen_ai_request,
    trace_operation,
    trace_provider_preload,
    trace_turn,
)
from dispatch.transport_factory import build_transport
from dispatch.workflow_executor import dispatch_plan, render_summary
from providers.provider_client import load_provider

logger = logging.getLogger("main")


def main() -> None:
    """Run the orchestrator terminal application."""
    configure_console_encoding()

    try:
        session_id = generate_session_id()
        init_logger("logs", session_id)
        logger.info("Session started: session_id=%s", session_id)

        config = load_config()
        init_telemetry(config.telemetry, llm=config.model)

        if config.telemetry.enabled:
            # Export failures are kept out of the console so they cannot
            # corrupt the prompt, so name the destination once at startup;
            # otherwise a wrong endpoint is invisible for the whole session.
            print(f"Telemetry: exporting to {config.telemetry.otlp_http_endpoint}")

        startup_attributes = {
            "gen_ai.provider.name": config.provider,
            "gen_ai.request.model": config.model,
            "gen_ai.conversation.id": session_id,
        }

        with trace_operation("kio1.startup", startup_attributes):
            # The registry is passed in so the prompt's catalog is generated
            # from the same entries that decide routing. Editing config.json
            # is then the only step needed to teach KIO1 about a new target.
            system_prompt = load_prompt(config.prompt_path, config.dispatch.registry)

            provider = load_provider(config.provider, config.allowed_providers)
            client = provider.create_client(config)

            print(f"Loading {config.provider} model {config.model}...")
            with trace_provider_preload(
                provider=config.provider,
                model=config.model,
            ):
                provider.preload(config, client)

            chat_file = create_chat_file(
                config.chat_directory, system_prompt, session_id
            )
            record_session_started(
                provider=config.provider,
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
    # One transport for the whole session: a NATS transport owns a connection
    # and a thread, so building one per turn would leak both.
    transport = build_transport(config.dispatch) if config.dispatch.enabled else None

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

                with trace_turn(
                    session_id=session_id,
                    turn_number=turn,
                    provider=config.provider,
                    model=config.model,
                ):
                    messages = load_messages(chat_file)
                    messages.append({"role": "user", "content": query})

                    with trace_gen_ai_request(
                        provider=config.provider,
                        model=config.model,
                        session_id=session_id,
                        turn_number=turn,
                        message_count=len(messages) + 1,
                        max_output_tokens=config.max_output_tokens,
                        temperature=config.temperature,
                    ):
                        response = provider.send_request(
                            config=config,
                            client=client,
                            system_prompt=system_prompt,
                            messages=messages,
                        )
                    content = provider.extract_content(response)
                    formatted = format_json(content)

                    append_user_message(chat_file, query)
                    append_assistant_message(chat_file, content)
                    print(f"\n{formatted}")

                    if transport is not None:
                        workflow_result = dispatch_plan(
                            parse_response(content),
                            config.dispatch,
                            transport,
                        )

                        if workflow_result is not None:
                            print(f"\n{render_summary(workflow_result)}")

            except Exception as e:
                logger.exception("Request failed: turn=%d", turn)
                print(f"\nError: {e}", file=sys.stderr)
                if content:
                    print(f"Raw response: {content!r}", file=sys.stderr)
        session_success = True
    finally:
        record_session_completed(
            provider=config.provider,
            model=config.model,
            success=session_success,
        )
        if transport is not None:
            transport.close()

        logger.info("Session ended: turns=%d", turn)
        shutdown_telemetry()


if __name__ == "__main__":
    main()
