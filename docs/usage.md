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
5. Presents the interactive prompt

## Interactive Session

```
Loading Ollama model ministral-3:8b...
KIO1 Orchestrator (type 'exit' to quit)
----------------------------------------

>
```

Type your request and press Enter. The orchestrator sends it to Ollama and prints the model's JSON reply — that is the end of the pipeline; the prompt immediately returns for another query.

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

The JSON is printed exactly as the model returned it (pretty-printed) and appended to the chat history — nothing is validated or dispatched after this point.

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

Type `exit` or press `Ctrl+C` to quit.

## Chat History Files

Each session produces a JSONL file in `chats/`. See [Chat History Format](chat-history.md) for details.

