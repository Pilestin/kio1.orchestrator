# Shared image for both services. Which one runs is chosen by the compose
# command, so there is one build to keep in sync rather than two.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py config.json ./
COPY core/ ./core/
COPY providers/ ./providers/
COPY dispatch/ ./dispatch/
COPY services/ ./services/
COPY tools/ ./tools/
COPY prompts/ ./prompts/

# Logs go to stdout so the platform's Collector can scrape them into Loki.
CMD ["uvicorn", "services.workflow_api.app:app", "--host", "0.0.0.0", "--port", "8000"]
