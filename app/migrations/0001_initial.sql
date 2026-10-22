-- Initial schema, matching the data model in the build draft.
-- Secrets are never stored in plain text: passwords and tokens are hashes,
-- and the TOTP secret is encrypted with a key that lives outside the database.

CREATE TABLE users (
    id               INTEGER PRIMARY KEY,
    username         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash    TEXT    NOT NULL,              -- Argon2id string (salt + params included)
    totp_secret_enc  BLOB,                          -- encrypted, NULL until TOTP is enabled
    is_admin         INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    failed_logins    INTEGER NOT NULL DEFAULT 0,
    locked_until     TEXT,                          -- ISO 8601 UTC, NULL when not locked
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE sessions (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash       TEXT    NOT NULL UNIQUE,       -- SHA-256 of the cookie token
    csrf_token_hash  TEXT    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at       TEXT    NOT NULL,              -- absolute lifetime limit
    ip               TEXT,
    user_agent       TEXT
);

CREATE INDEX sessions_user_id ON sessions (user_id);

CREATE TABLE audit_log (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER,                       -- no foreign key: entries outlive a deleted user
    action          TEXT    NOT NULL,              -- e.g. login_failed, stack_update
    target_stack     TEXT,
    target_container TEXT,
    detail_json      TEXT,                          -- never contains passwords or tokens
    result           TEXT    NOT NULL,              -- ok / denied / error
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX audit_log_created_at ON audit_log (created_at);

-- The audit log is append-only: the database itself refuses edits and deletes.
-- (Retention cleanup, if ever needed, gets its own migration that changes this.)
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
