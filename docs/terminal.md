# Terminal

[← Back to the README](../README.md)

Opening a shell inside a running container, and optional encrypted recordings.

**Terminal** on a running container opens a shell inside it (`docker exec`):
bash if the image has it, otherwise sh. Never a shell on the host. It is the most
powerful thing in the app, so:

- **Only running containers of a stack**, never the app's own stack.
- **At most 2 terminals per login, 4 in total.**
- **Closed after 15 minutes without typing.** Typing counts as activity for the
  login session; an open terminal nobody types in does not keep you logged in.
- **Closing the terminal ends the shell and everything started from it.** Docker
  itself leaves an exec running when the connection drops, so the app ends the
  shell's whole session (hangup, then kill a second later), background jobs and
  `nohup` included. Processes that detach themselves completely (daemons calling
  `setsid`) are left alone, as with `docker exec`. Shells left open when the app
  stopped are ended at its next start.
- **Opening and closing are in the audit log**, never what was typed.

## Transcripts (optional, off by default)

With a key, every terminal session is recorded (everything typed and shown,
including what the screen hides, such as a password typed at a prompt) and kept
for 14 days under **Terminal history**. Viewing one is audited.

Transcripts are encrypted (AES-256-GCM) with a key that lives in a file, never in
the database: a copy of the database alone reveals nothing of what was typed.
Create the key once on the server:

```bash
mkdir -p secrets
openssl rand -base64 32 > secrets/transcript.key
sudo chown 46337 secrets/transcript.key && sudo chmod 400 secrets/transcript.key
```

```yaml
    environment:
      TRANSCRIPT_KEY_FILE: /run/secrets/transcript.key
    volumes:
      - ./secrets/transcript.key:/run/secrets/transcript.key:ro
```

**Back up the key and the database separately**, never in the same place (for
example: the database with the server's normal backups, the key in a password
manager). Without the key, old transcripts cannot be read by anyone; with both
together, they can. Changing the key makes older transcripts unreadable.
