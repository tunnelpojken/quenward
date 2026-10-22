-- One-time password reset links, created only by the `reset-password` command
-- inside the container. Only the SHA-256 of the link token is stored.
CREATE TABLE password_resets (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash  TEXT    NOT NULL UNIQUE,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at  TEXT    NOT NULL,
    used_at     TEXT
);
