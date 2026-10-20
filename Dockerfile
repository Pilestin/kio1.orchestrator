# KIO1 orchestrator — browser-based chat UI (LLM query/response over Ollama).
# Build:  docker build -t kio1 .
#
# config.json is NOT baked into the image (it carries credentials); mount it
# at runtime. The orchestrator reaches the host's Ollama install via
# host.docker.internal, so override the endpoint with an env var — one
# config.json serves both host and container. --rm avoids piling up stopped
# containers on every launch; mount chats/, logs/, and assets/ so that is
# safe:
#   docker run --rm -p 5000:5000 --add-host=host.docker.internal:host-gateway \
#     -v "$PWD/config.json:/app/config.json" \
#     -v "$PWD/chats:/app/chats" \
#     -v "$PWD/logs:/app/logs" \
#     -v "$PWD/assets:/app/assets" \
#     -e KIO1_OLLAMA_ENDPOINT=http://host.docker.internal:11434 \
#     kio1
#
# Create chats/, logs/, and assets/ on the host before the first run (e.g.
# `mkdir -p chats logs assets`). Docker auto-creates missing bind-mount
# sources as root, which the container's non-root appuser then cannot write
# to — the chown below only covers the image's own copies of these
# directories, not ones substituted in by a bind mount.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user. It owns /app so the app can write its logs/,
# chats/, and assets/ directories, and a mounted config.json (host perms
# permitting).
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/logs /app/chats /app/assets \
    && chown -R appuser:appuser /app
USER appuser

# Bind to all interfaces inside the container so the host can reach the UI
# through the published port; KIO1_WEB_HOST/KIO1_WEB_PORT override this.
ENV KIO1_WEB_HOST=0.0.0.0
EXPOSE 5000

# The web UI's own HTTP server is the health signal: this also catches a
# container started without config.json mounted or with an invalid config,
# since startup fails before the server binds.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=3)" || exit 1

CMD ["python", "main.py"]
