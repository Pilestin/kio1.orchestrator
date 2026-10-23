-- Session Manager schema.
--
-- One session per workflow run, each in a conversation: the requests that
-- follow on from each other. Steps record what each KIO agent was asked
-- to do and how it answered. Artifacts hold the agent output verbatim.
-- Lineage records which artifact fed which step.

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id UUID        PRIMARY KEY,
    title           TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversations_updated_idx ON conversations (updated_at);

CREATE TABLE IF NOT EXISTS sessions (
    session_id     UUID        PRIMARY KEY,
    request_text   TEXT        NOT NULL,
    status         TEXT        NOT NULL,
    workflow_id    TEXT,
    execution_mode TEXT,
    error          TEXT,
    answer         TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sessions_status_allowed
        CHECK (status IN ('pending', 'running', 'completed', 'failed'))
);

-- Added after the first release: a database created before it gains the
-- column here, since CREATE TABLE IF NOT EXISTS leaves an old table alone.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS answer TEXT;

-- Null only on a session recorded before conversations existed; the
-- repository gives each such session a conversation once the schema is in.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS conversation_id UUID
    REFERENCES conversations (conversation_id) ON DELETE CASCADE;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS clarifications JSONB;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS attachments JSONB;

CREATE INDEX IF NOT EXISTS sessions_conversation_idx
    ON sessions (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS steps (
    id          BIGSERIAL   PRIMARY KEY,
    session_id  UUID        NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    step_id     TEXT        NOT NULL,
    agent_id    TEXT        NOT NULL,
    capability  TEXT        NOT NULL,
    task        TEXT        NOT NULL,
    status      TEXT        NOT NULL,
    duration_ms INTEGER     NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT steps_unique_per_session UNIQUE (session_id, step_id),
    CONSTRAINT steps_status_allowed
        CHECK (status IN ('ok', 'error', 'skipped', 'not_run'))
);

CREATE INDEX IF NOT EXISTS steps_session_idx ON steps (session_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
    id         BIGSERIAL   PRIMARY KEY,
    session_id UUID        NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    step_id    TEXT        NOT NULL,
    payload    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT artifacts_unique_per_step UNIQUE (session_id, step_id)
);

-- `basis` records how the edge was established, so a link inferred from
-- execution order is never mistaken for a dependency the plan declared.
-- Plans carry no per-step dependencies yet, so every edge is currently
-- 'execution_order', and only for sequential workflows.
CREATE TABLE IF NOT EXISTS lineage (
    id                  BIGSERIAL   PRIMARY KEY,
    session_id          UUID        NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    produced_by_step_id TEXT        NOT NULL,
    consumed_by_step_id TEXT        NOT NULL,
    artifact_id         BIGINT      NOT NULL REFERENCES artifacts (id) ON DELETE CASCADE,
    basis               TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT lineage_unique_edge
        UNIQUE (session_id, produced_by_step_id, consumed_by_step_id),
    CONSTRAINT lineage_basis_allowed
        CHECK (basis IN ('execution_order', 'declared_dependency'))
);

CREATE INDEX IF NOT EXISTS lineage_session_idx ON lineage (session_id);
