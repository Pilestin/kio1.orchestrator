# Agents in the database

The registered targets live in the database by default — the same
`data/kio1.db` SQLite file that keeps the run history — rather than in
`config.json`.

## Moving over

On the first start with this version, the agents in `config.json`'s
`dispatch.agents` are imported, in their order, once. After that the
database is the source of truth: adding, editing, enabling, and removing in
the console change the database, and `config.json`'s `dispatch.agents` is no
longer read. The file itself is left as it was.

The terminal application (`main.py`) reads the same agents.

## History

Every change is kept as a version: `imported`, `created`, `updated`,
`removed`, `restored`. Editing a target in the console offers its earlier
versions and a **Restore** button; restoring is itself a new version, so it
can be undone the same way. A removed target can be brought back from the
version before its removal. The latest 200 versions of each target are kept.

| Endpoint | Purpose |
|----------|---------|
| `GET /registry/agents/{id}/history` | The versions, newest first. |
| `POST /registry/agents/{id}/restore` | `{"version": 12}` brings that version back. |
| `GET /registry/export` | Every definition in `dispatch.agents` form: a backup, or a way to move agents to another KIO1. |

All three need the administration token.

## Keeping them in config.json instead

```bash
KIO1_AGENT_STORE=file
```

Agents then live in `config.json` as before — useful when the configuration
is versioned in git. There is no history in that mode.

The database store needs SQLite. With `KIO1_DATABASE_URL` pointing at
PostgreSQL, agents stay in `config.json` and the log says so; a PostgreSQL
agent store is not written yet. In the Docker Compose setup the Workflow API
keeps its own SQLite file inside its container, so mount `/app/data` to keep
agents across rebuilds.
