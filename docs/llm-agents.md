# Agents made of instructions

The fifth kind of target, and the only one that needs nothing built: an
`llm` agent is a role written in plain language — *"You are a careful code
reviewer…"* — carried out by a language model inside KIO1. No server, no
code, no address.

## Making one

**Targets → Add a target → Agent.** Four fields:

| Field | What to write |
|-------|---------------|
| Name | How plans address it: `CodeReviewer`. Letters, digits, `-`, `_`. |
| Skill | The capability plans ask for: `code_review`. Left empty, it is made from the name. |
| What it does | One sentence the planner reads when choosing. |
| Instructions | The role: what it is, what to do with the task, what to return. |

Three templates fill the form to start from: *Code reviewer*, *Requirements
writer*, *Documentation writer*. Editing an `llm` agent reopens the same
form; **JSON** shows the definition it makes, for the fields the form
leaves out.

```json
"CodeReviewer": {
    "type": "llm",
    "description": "Reviews code for bugs and risky patterns.",
    "instructions": "You are a careful senior code reviewer. …",
    "capabilities": ["code_review"],
    "provider": "openrouter",
    "model": "anthropic/claude-sonnet-4.5",
    "temperature": 0.1
}
```

`provider`, `model`, and `temperature` are optional; without them the agent
runs on the planning model. A `provider` needs a `model`.

## What it is given, and what it returns

The instructions are the system prompt. The message is the step: its task,
its capability, any arguments, and what earlier steps returned (bounded to a
few thousand characters). It is asked to answer as `{"result": "…"}` — every
provider can be held to JSON — and may add a `verdict`. A reply that is not
JSON is kept whole as the result.

**An agent sees only its task, never the user's request.** The planner is
told to copy into each task the code, text, or values the target needs. In a
live test the first plan wrote *"Review the provided function"* and the
reviewer guessed; with that rule the task carried the function and the
reviewer found the real division by zero.

What the agent writes streams into the run's live view, under its name.

## Limits

- The model call is bounded by the planning timeout, not the step timeout:
  a local model on a CPU takes tens of seconds per agent.
- It cannot call tools or read files itself. Give it what it needs through
  earlier steps — a `cli` step that prints a file, an MCP read — and
  `depends_on`.

---

See also: [Agents in the database](agent-store.md) for where definitions are
kept, and [Connecting your own module](connecting-a-tool.md) for the other
kinds.
