# KIO1 orchestrator — interactive terminal app (LLM query/response over Ollama).
# Build:  docker build -t kio1 .
#
# config.json is NOT baked into the image (it carries credentials); mount it
# at runtime. The orchestrator reaches the host's Ollama install via
# host.docker.internal, so override the endpoint with an env var — one
# config.json serves both host and container. --rm avoids piling up stopped
# containers on every launch; mount chats/ and logs/ so that is safe:
#   docker run --rm -it --add-host=host.docker.internal:host-gateway \
#     -v "$PWD/config.json:/app/config.json" \
#     -v "$PWD/chats:/app/chats" \
#     -v "$PWD/logs:/app/logs" \
#     -e KIO1_OLLAMA_ENDPOINT=http://host.docker.internal:11434 \
#     kio1
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user. It owns /app so the app can write its logs/ and
# chats/ directories, and a mounted config.json (host perms permitting).
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/logs /app/chats \
    && chown -R appuser:appuser /app
USER appuser

# The app is an interactive REPL with no service port, so health = "can the
# app load its (mounted) config?" — this catches a container started without
# config.json mounted or with an invalid config.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "from config_loader import load_config; load_config()" || exit 1

# main.py is an interactive REPL; run the container with -it.
CMD ["python", "main.py"]
