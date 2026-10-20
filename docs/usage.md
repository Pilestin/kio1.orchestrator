# Usage Guide

## Starting the Orchestrator

```bash
source .venv/bin/activate
python main.py
```

On startup, the orchestrator:
1. Loads configuration from `config.json`
2. Reads the system prompt
3. Preloads the configured model into Ollama using the configured `keep_alive`
4. Creates a new chat session file
5. Starts the web UI

## Using the Web UI

```
Loading Ollama model ministral-3:8b...
KIO1 Orchestrator web UI: http://127.0.0.1:5000
```

Open that address in a browser. Type a request in the message box at the bottom and press Enter (or click Send); the orchestrator sends it to Ollama and renders the model's reply as a message bubble — that is the end of the pipeline; the input box is immediately ready for another query.

Workflow-plan responses (the `workflow_id`/`execution_mode`/`steps`/`explanation` shape the system prompt produces) are rendered as numbered step cards with the agent and capability for each step, rather than raw JSON; a "View raw JSON" toggle underneath the cards still shows the syntax-highlighted original. Any other JSON shape the model returns falls back to the syntax-highlighted JSON view directly.

Override the bind address with the `KIO1_WEB_HOST` and `KIO1_WEB_PORT` environment variables, for example to expose the UI on all interfaces:

```bash
KIO1_WEB_HOST=0.0.0.0 KIO1_WEB_PORT=8080 python main.py
```

## Uploading Files

Use the Model, Dataset, or File buttons next to the message box to upload a model checkpoint, dataset, or other file. Each button saves into its own subfolder (`assets/models/`, `assets/datasets/`, `assets/files/`) with a sanitized, collision-resistant filename; the UI reports the stored path once the upload completes. The `assets/` directory is not tracked by git or baked into the Docker image.

## Example Queries

### Full development lifecycle

```
> Build a REST API for user management with authentication
```

Response:
```json
{
  "workflow_id": "wf-usr01",
  "execution_mode": "sequential",
  "steps": [
    {
      "step_id": "s1",
      "agent_id": "KIO3",
      "capability": "requirements_engineering",
      "task": "Define requirements for user management REST API with authentication"
    },
    {
      "step_id": "s2",
      "agent_id": "KIO4",
      "capability": "architecture_design",
      "task": "Design API architecture and authentication flow"
    },
    {
      "step_id": "s3",
      "agent_id": "KIO7",
      "capability": "code_generation",
      "task": "Generate REST API code with auth endpoints"
    },
    {
      "step_id": "s4",
      "agent_id": "KIO12",
      "capability": "cybersecurity_validation",
      "task": "Validate authentication security"
    },
    {
      "step_id": "s5",
      "agent_id": "KIO11",
      "capability": "test_automation",
      "task": "Create automated tests for all endpoints"
    },
    {
      "step_id": "s6",
      "agent_id": "KIO8",
      "capability": "deployment",
      "task": "Package and deploy the API"
    }
  ],
  "explanation": "Full lifecycle for a user management API with security review."
}
```

The JSON is rendered exactly as the model returned it (pretty-printed, syntax-highlighted) and appended to the chat history — nothing is validated or dispatched after this point.

### Bug fix request

```
> Fix the null pointer exception in the payment module
```

### Testing only

```
> Write unit tests for the notification service
```

## Multi-turn Conversations

The orchestrator maintains conversation history within a session. Follow-up queries have access to previous context:

```
> Design a drone telemetry system
> Now add security review to the plan
```

## Exiting

Close the browser tab, or stop the server with `Ctrl+C` in the terminal running `main.py`.

## Chat History Files

Each session produces a JSONL file in `chats/`. See [Chat History Format](chat-history.md) for details.

