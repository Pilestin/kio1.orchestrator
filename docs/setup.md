# First-time setup

The first time the console opens with a working token, **Set up KIO1**
appears once. It can be opened again from the command palette (Ctrl K →
*Set up KIO1*).

1. **Model.** One line on this computer — memory, cores, graphics card,
   whether Ollama is running — and a short list of local models that work
   with KIO1, marked for this machine: the recommended ones highlighted,
   ones too large for the memory dimmed. **Download** pulls a model through
   Ollama with a progress bar. **Use recommended** sets the planning model
   and the answer model in one step. **Use a cloud model instead** goes to
   Settings, where OpenRouter, OpenAI, or Anthropic can be chosen.
2. **Your code.** The folder the agents work on.
3. **Try it.** Runs an example request and shows it live.

## How models are suggested

| Model | Size | Needs | Role |
|-------|------|-------|------|
| `ministral-3:3b` | 3 GB | 8 GB memory | planning — fast, follows the plan format |
| `qwen3.5:4b` | 3.4 GB | 8 GB memory | answers — slower, reads results carefully |
| `llama3.1:8b` | 4.9 GB | 16 GB memory or a GPU | both |
| `qwen2.5:14b` | 9 GB | 32 GB memory or a GPU | both |

The smallest fitting planner and the largest fitting answerer are
recommended. Without a graphics card nothing over about 5 GB is
recommended even when it fits: on a CPU it is minutes per answer. On the
machine KIO1 was measured on — 15 GB, no GPU — that is `ministral-3:3b` to
plan and `qwen3.5:4b` to answer, the pairing that gave correct answers in
[Reviewing a run](review.md).

Only NVIDIA cards are detected (through `nvidia-smi`). Whether Ollama is
actually using a graphics card is read from its loaded models.

## API

| Endpoint | Purpose |
|----------|---------|
| `GET /system` | Hardware, Ollama's state, the suggestions, the current models and workspace, and downloads in progress. |
| `POST /system/models/pull` | `{"model": "qwen3.5:4b"}` starts a download; `409` when Ollama is not running. |
| `GET /system/models/pull` | Every download this service started, with progress. |

All three need the administration token.
