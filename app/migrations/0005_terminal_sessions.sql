-- One row per terminal session, recorded or not. The audit log keeps the
-- permanent open/close entries; these rows are deleted after the retention time.
CREATE TABLE terminal_sessions (
    id                INTEGER PRIMARY KEY,
    public_id         TEXT    NOT NULL UNIQUE,  -- random, used in URLs and as AAD
    user_id           INTEGER REFERENCES users(id) ON DELETE SET NULL,
    stack             TEXT    NOT NULL,
    service           TEXT,
    container_id      TEXT    NOT NULL,
    container_name    TEXT    NOT NULL,
    shell             TEXT,
    exec_id           TEXT    NOT NULL,         -- Docker's id for the shell process
    shell_pid         INTEGER,                  -- inside the container; to end it after a crash
    recorded          INTEGER NOT NULL DEFAULT 0,
    transcript_bytes  INTEGER NOT NULL DEFAULT 0,
    truncated         INTEGER NOT NULL DEFAULT 0,
    started_at        TEXT    NOT NULL,
    ended_at          TEXT,
    end_reason        TEXT,
    exit_code         INTEGER
);

CREATE INDEX terminal_sessions_started ON terminal_sessions (started_at);

-- Encrypted transcript pieces (AES-256-GCM). The key is never in the database.
CREATE TABLE transcript_chunks (
    session_id  INTEGER NOT NULL REFERENCES terminal_sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    data        BLOB    NOT NULL,  -- 12-byte nonce || ciphertext with tag
    PRIMARY KEY (session_id, seq)
);
