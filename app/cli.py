"""Command line tools, run inside the container:

    docker exec -it <container> reset-password [--username NAME]

Creating a reset link logs the user out everywhere at once. The link is printed
here only; it is never stored in plain text and works once, for 15 minutes.
"""

import argparse
import sys

from app import alerts, audit, db
from app.auth import resets, sessions
from app.config import ConfigError, Settings


def reset_password(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="reset-password", description="Create a one-time password reset link."
    )
    parser.add_argument("--username", help="needed only when there is more than one user")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    conn = db.connect(settings.db_path)
    db.migrate(conn)
    if args.username:
        users = conn.execute(
            "SELECT id, username FROM users WHERE username = ?", (args.username,)
        ).fetchall()
    else:
        users = conn.execute("SELECT id, username FROM users").fetchall()

    if not users:
        print("No such user." if args.username else "No users exist yet.", file=sys.stderr)
        return 1
    if len(users) > 1:
        names = ", ".join(u["username"] for u in users)
        print(f"More than one user ({names}); pass --username.", file=sys.stderr)
        return 1
    user = users[0]

    conn.execute("BEGIN IMMEDIATE")
    try:
        sessions.delete_all_for_user(conn, user["id"])
        token = resets.create(conn, user["id"], settings)
        audit.record(conn, "password_reset_requested", "ok", user_id=user["id"], via="cli")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    minutes = int(settings.reset_link_lifetime.total_seconds() // 60)
    print(f"All sessions for '{user['username']}' have been logged out.")
    print(f"Open this link within {minutes} minutes to choose a new password (works once):")
    print()
    print(f"    {settings.public_url}/#/reset/{token}")
    print()

    alerter = alerts.from_config(settings.ntfy)
    alerter.send(
        alerts.Alert(
            "Password reset link created",
            f"A reset link for '{user['username']}' was created from the server command line. "
            "If this was not you, someone has shell access to the server.",
            priority="high",
            tags=("key",),
        )
    )
    if isinstance(alerter, alerts.NtfyAlerter):
        alerter.flush()
    return 0


def main() -> int:
    commands = {"reset-password": reset_password}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(f"usage: python -m app.cli {{{','.join(commands)}}} ...", file=sys.stderr)
        return 2
    return commands[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
