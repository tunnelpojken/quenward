"""Phase 5: the compose editor.

`docker compose config` is replaced by a fake that reads the temporary file it
is given; tests/test_docker_integration.py runs the real one.
"""

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import Settings
from app.stacks import compose
from app.stacks.actions import JobManager
from tests.fake_docker import FakeGateway, container_attrs

ORIGINAL = (
    "services:\n  pihole:\n    image: pihole/pihole:2026.10\n"
    "    environment:\n      WEBPASSWORD: inline-compose-secret\n"
)
NEW = ORIGINAL.replace("2026.10", "2026.11") + "  extra:\n    image: busybox:1.37\n"
ENV_SECRET = "dotenv-file-secret"


class FakeConfig:
    """Stands in for compose.run for `docker compose ... config --quiet`."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.checked: list[str] = []  # the text compose was shown
        self.check_modes: list[int] = []
        self.reject: str | None = None  # text that makes compose fail
        self.during: Any = None  # called while "compose" runs
        self.block = threading.Event()
        self.block.set()

    def __call__(self, args: list[str], cwd: Path, on_line: Any, timeout: float):
        self.calls.append(args)
        if "config" not in args:  # an action (up, stop, ...)
            return compose.Result(exit_code=0)
        checked = Path(args[args.index("--file") + 1])
        self.checked.append(checked.read_text())
        self.check_modes.append(checked.stat().st_mode & 0o777)
        self.block.wait(10)
        if self.during:
            self.during()
        if self.reject and self.reject in self.checked[-1]:
            on_line(
                f"validating {checked}: services.pihole Additional property nope is not allowed"
            )
            return compose.Result(exit_code=15)
        on_line(
            'level=warning msg="The \\"TZ\\" variable is not set. Defaulting to a blank string."'
        )
        return compose.Result(exit_code=0)


@pytest.fixture
def stack_file(settings: Settings) -> Path:
    folder = settings.stacks_dir / "pihole"
    folder.mkdir(parents=True)
    (folder / "compose.yaml").write_text(ORIGINAL)
    (folder / ".env").write_text(f"PASSWORD={ENV_SECRET}\n")
    return folder / "compose.yaml"


@pytest.fixture
def fake_config(
    client: TestClient, settings: Settings, stack_file: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeConfig:
    gateway = FakeGateway(
        containers=[container_attrs("aaaaaaaaaaaa", "pihole", "pihole", str(stack_file.parent))]
    )
    runner = FakeConfig()
    client.app.state.docker = gateway
    client.app.state.jobs = JobManager(settings, lambda: client.app.state.docker, runner)
    monkeypatch.setattr(compose, "docker_binary", lambda: "/usr/bin/docker")
    return runner


def _get(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/stacks/pihole/compose")
    assert response.status_code == 200, response.text
    return response.json()


def _put(client: TestClient, csrf: str, content: str, etag: str, stack: str = "pihole"):
    return client.put(
        f"/api/stacks/{stack}/compose",
        json={"content": content, "etag": etag},
        headers={"X-CSRF-Token": csrf},
    )


def _audit(settings: Settings) -> list[dict[str, Any]]:
    conn = db.connect(settings.db_path)
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'compose_edit' ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _leftovers(folder: Path) -> list[str]:
    return [p.name for p in folder.iterdir() if ".check-" in p.name]


# ---------------------------------------------------------------- reading


def test_the_compose_file_is_shown_but_never_the_env_file(
    client: TestClient, csrf: str, fake_config: FakeConfig
) -> None:
    data = _get(client)
    assert data["file"] == "compose.yaml"
    assert data["content"] == ORIGINAL
    assert len(data["etag"]) == 64
    assert data["editable"] is True
    assert ENV_SECRET not in client.get("/api/stacks/pihole").text
    assert ENV_SECRET not in json.dumps(data)


def test_unknown_stacks_and_traversal(
    client: TestClient, csrf: str, fake_config: FakeConfig
) -> None:
    # ".." itself is resolved by the HTTP client before sending (405: no such route).
    for name in ("nope", "..", "..%2F..%2Fetc", "pihole%2F..%2F.."):
        assert client.get(f"/api/stacks/{name}/compose").status_code in (404, 405), name
        assert _put(client, csrf, NEW, "0" * 64, stack=name).status_code in (404, 405), name


def test_the_apps_own_stack_is_shown_read_only(
    client: TestClient, csrf: str, fake_config: FakeConfig, settings: Settings, stack_file: Path
) -> None:
    client.app.state.docker.self_id = "aaaaaaaaaaaa"
    data = _get(client)
    assert data["editable"] is False
    assert data["self"] is True
    response = _put(client, csrf, NEW, data["etag"])
    assert response.status_code == 403
    assert stack_file.read_text() == ORIGINAL
    assert _audit(settings)[0]["result"] == "denied"


# ---------------------------------------------------------------- saving


def test_saving_validates_backs_up_and_writes_in_place(
    client: TestClient, csrf: str, fake_config: FakeConfig, settings: Settings, stack_file: Path
) -> None:
    stack_file.chmod(0o640)
    inode = stack_file.stat().st_ino
    etag = _get(client)["etag"]
    response = _put(client, csrf, NEW, etag)
    assert response.status_code == 200, response.text
    assert stack_file.read_text() == NEW
    backup = stack_file.parent / "compose.yaml.bak"
    assert backup.read_text() == ORIGINAL
    assert backup.stat().st_mode & 0o777 == 0o640, "readable like the original, despite umask"
    # Rewritten in place: same file, same permissions (and owner), not a new file.
    assert stack_file.stat().st_ino == inode
    assert stack_file.stat().st_mode & 0o777 == 0o640
    assert response.json()["etag"] == _get(client)["etag"] != etag
    assert "TZ" in response.json()["warnings"][0]
    # compose checked the new text, in the stack's own folder, from a private file.
    assert fake_config.checked == [NEW]
    assert fake_config.check_modes == [0o600]
    args = fake_config.calls[0]
    assert args[args.index("--project-directory") + 1] == str(stack_file.parent)
    assert _leftovers(stack_file.parent) == []


def test_the_audit_has_hashes_and_counts_but_no_content(
    client: TestClient, csrf: str, fake_config: FakeConfig, settings: Settings
) -> None:
    etag = _get(client)["etag"]
    _put(client, csrf, NEW, etag)
    entry = _audit(settings)[0]
    detail = json.loads(entry["detail_json"])
    assert entry["result"] == "ok"
    assert entry["target_stack"] == "pihole"
    assert detail["before"] == etag[:12]
    assert (detail["added"], detail["removed"]) == (3, 1)
    assert "inline-compose-secret" not in json.dumps(entry)


def test_a_final_newline_is_added(
    client: TestClient, csrf: str, fake_config: FakeConfig, stack_file: Path
) -> None:
    response = _put(client, csrf, NEW.rstrip("\n"), _get(client)["etag"])
    assert response.status_code == 200
    assert stack_file.read_text() == NEW
    assert response.json()["content"] == NEW, "the editor is told what was written"
    assert response.json()["etag"] == _get(client)["etag"]


def test_a_second_save_replaces_the_backup(
    client: TestClient, csrf: str, fake_config: FakeConfig, stack_file: Path
) -> None:
    _put(client, csrf, NEW, _get(client)["etag"])
    third = NEW.replace("busybox", "alpine")
    assert _put(client, csrf, third, _get(client)["etag"]).status_code == 200
    assert (stack_file.parent / "compose.yaml.bak").read_text() == NEW


def test_a_change_on_disk_is_never_overwritten(
    client: TestClient, csrf: str, fake_config: FakeConfig, stack_file: Path
) -> None:
    etag = _get(client)["etag"]
    stack_file.write_text(ORIGINAL + "# edited over ssh\n")
    response = _put(client, csrf, NEW, etag)
    assert response.status_code == 409
    assert stack_file.read_text().endswith("# edited over ssh\n")
    assert not (stack_file.parent / "compose.yaml.bak").exists()
    assert fake_config.checked == [], "a stale save is refused before compose runs"


def test_a_change_while_compose_checks_is_not_overwritten(
    client: TestClient, csrf: str, fake_config: FakeConfig, stack_file: Path
) -> None:
    etag = _get(client)["etag"]
    fake_config.during = lambda: stack_file.write_text("services:\n  changed: {}\n")
    assert _put(client, csrf, NEW, etag).status_code == 409
    assert stack_file.read_text() == "services:\n  changed: {}\n"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("services:\n  pihole: [unclosed\n", "line 3"),
        ("services:\n\tpihole: {}\n", "Not valid YAML"),
        ("- just\n- a list\n", "mapping"),
        ("name: x\n", "services"),
        ("services: {}\n", "services"),
        ("services:\n  a: {}\n\x00", "NUL"),
        ("services: !!python/object/apply:os.system ['id']\n", "Not valid YAML"),
    ],
)
def test_invalid_yaml_is_refused_and_the_file_kept(
    client: TestClient,
    csrf: str,
    fake_config: FakeConfig,
    stack_file: Path,
    text: str,
    message: str,
) -> None:
    response = _put(client, csrf, text, _get(client)["etag"])
    assert response.status_code == 422
    assert message in response.json()["detail"]["message"]
    assert stack_file.read_text() == ORIGINAL
    assert fake_config.checked == [], "compose is not even asked"


def test_what_compose_rejects_is_refused_with_its_message(
    client: TestClient, csrf: str, fake_config: FakeConfig, stack_file: Path
) -> None:
    fake_config.reject = "nope:"
    bad = ORIGINAL + "    nope: 1\n"
    response = _put(client, csrf, bad, _get(client)["etag"])
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "docker compose did not accept the file"
    assert "Additional property nope" in detail["output"][0]
    assert ".check-" not in detail["output"][0], "the temporary name is not shown"
    assert "compose.yaml" in detail["output"][0]
    assert stack_file.read_text() == ORIGINAL
    assert _leftovers(stack_file.parent) == []


def test_too_large_is_refused(client: TestClient, csrf: str, fake_config: FakeConfig) -> None:
    big = ORIGINAL + "#" * (256 * 1024)
    assert _put(client, csrf, big, _get(client)["etag"]).status_code == 422


def test_bad_etags_are_refused(client: TestClient, csrf: str, fake_config: FakeConfig) -> None:
    for etag in ("", "x" * 64, "A" * 64, "0" * 63):
        assert _put(client, csrf, NEW, etag).status_code == 422


def test_saving_needs_the_csrf_token(
    client: TestClient, csrf: str, fake_config: FakeConfig, stack_file: Path
) -> None:
    etag = _get(client)["etag"]
    response = client.put("/api/stacks/pihole/compose", json={"content": NEW, "etag": etag})
    assert response.status_code == 403
    assert stack_file.read_text() == ORIGINAL


def test_no_save_while_an_action_runs_and_no_action_while_saving(
    client: TestClient, csrf: str, fake_config: FakeConfig, stack_file: Path
) -> None:
    etag = _get(client)["etag"]
    jobs = client.app.state.jobs
    with jobs.editing("pihole"):  # as if another save were in progress
        assert _put(client, csrf, NEW, etag).status_code == 409
        start = client.post("/api/stacks/pihole/start", headers={"X-CSRF-Token": csrf})
        assert start.status_code == 409
    jobs._running["pihole"] = "0123456789abcdef"  # as if an action were running
    response = _put(client, csrf, NEW, etag)
    assert response.status_code == 409
    assert response.json()["detail"]["job"] == "0123456789abcdef"
    del jobs._running["pihole"]
    assert _put(client, csrf, NEW, etag).status_code == 200


def test_a_planted_backup_symlink_is_replaced_not_followed(
    client: TestClient, csrf: str, fake_config: FakeConfig, stack_file: Path, tmp_path: Path
) -> None:
    target = tmp_path / "important"
    target.write_text("do not overwrite")
    backup = stack_file.parent / "compose.yaml.bak"
    backup.symlink_to(target)
    assert _put(client, csrf, NEW, _get(client)["etag"]).status_code == 200
    assert target.read_text() == "do not overwrite"
    assert not backup.is_symlink()
    assert backup.read_text() == ORIGINAL


def test_the_backup_is_never_group_or_world_writable(
    client: TestClient, csrf: str, fake_config: FakeConfig, stack_file: Path
) -> None:
    stack_file.chmod(0o664)  # group-writable for the stack owner's group
    assert _put(client, csrf, NEW, _get(client)["etag"]).status_code == 200
    assert (stack_file.parent / "compose.yaml.bak").stat().st_mode & 0o777 == 0o644
    assert stack_file.stat().st_mode & 0o777 == 0o664, "the compose file keeps its own mode"


def test_an_old_backup_the_app_cannot_write_is_replaced(
    client: TestClient, csrf: str, fake_config: FakeConfig, stack_file: Path
) -> None:
    """Like a .bak left by an earlier version of the app running as another user."""
    backup = stack_file.parent / "compose.yaml.bak"
    backup.write_text("an old backup")
    backup.chmod(0o444)
    assert _put(client, csrf, NEW, _get(client)["etag"]).status_code == 200
    assert backup.read_text() == ORIGINAL
    assert [p.name for p in stack_file.parent.iterdir() if ".bak-" in p.name] == []


def test_a_symlinked_compose_file_is_not_a_stack(
    client: TestClient, csrf: str, fake_config: FakeConfig, settings: Settings, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(ORIGINAL)
    (settings.stacks_dir / "linked").mkdir()
    (settings.stacks_dir / "linked" / "compose.yaml").symlink_to(outside)
    assert client.get("/api/stacks/linked/compose").status_code == 404
    assert _put(client, csrf, NEW, "0" * 64, stack="linked").status_code == 404
    assert outside.read_text() == ORIGINAL


@pytest.mark.skipif(os.geteuid() == 0, reason="root may write any file")
def test_a_file_the_app_may_not_write_is_shown_read_only(
    client: TestClient, csrf: str, fake_config: FakeConfig, stack_file: Path
) -> None:
    stack_file.chmod(0o444)
    data = _get(client)
    assert data["editable"] is False
    assert _put(client, csrf, NEW, data["etag"]).status_code == 403
    assert stack_file.read_text() == ORIGINAL
