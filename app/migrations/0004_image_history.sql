-- One row per service image that changed during an update. old_image_id is the
-- image kept under rollback_tag (one previous generation per service).
CREATE TABLE image_history (
    id            INTEGER PRIMARY KEY,
    stack         TEXT    NOT NULL,
    service       TEXT    NOT NULL,
    image_ref     TEXT    NOT NULL,
    old_image_id  TEXT    NOT NULL,
    new_image_id  TEXT    NOT NULL,
    rollback_tag  TEXT    NOT NULL,
    user_id       INTEGER,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX image_history_stack ON image_history (stack, service, created_at);
