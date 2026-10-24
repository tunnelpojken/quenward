"""Stack actions (start, stop, restart, update) as background jobs.

- One action per stack at a time, and at most MAX_RUNNING overall.
- The app's own stack is refused: stopping it would cut the user off mid-action.
- Every action is audited when it starts and when it ends. Command output is
  shown to the user but never stored in the audit log.
- Update keeps exactly one previous image per service, under an explicit
  rollback tag (an untagged image may be deleted by Docker at any moment).
"""

import contextlib
import logging
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app import audit, clock, db
from app.stacks import compose
from app.stacks.gateway import DockerGateway, DockerUnavailable
from app.stacks.service import StackState

log = logging.getLogger("app.actions")

ACTIONS = ("start", "stop", "restart", "update")
_COMMANDS = {"start": ["up", "--detach"], "stop": ["stop"], "restart": ["restart"]}
_TIMEOUTS = {"start": 600, "stop": 300, "restart": 300, "pull": 900, "up": 600}
MAX_RUNNING = 2
MAX_LINES = 2000
_KEEP_FINISHED = timedelta(hours=1)

Runner = Callable[..., compose.Result]


class ActionRefused(Exception):
    """The action may not run. The message is safe to show; status is the HTTP code."""

    def __init__(self, message: str, status: int, job_id: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.job_id = job_id


@dataclass
class Job:
    id: str
    stack: str
    action: str
    user_id: int
    started_at: str
    status: str = "running"  # running | ok | error
    finished_at: str | None = None
    lines: list[str] = field(default_factory=list)
    skipped_lines: int = 0
    summary: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LINES:
                del self.lines[0]
                self.skipped_lines += 1

    def public(self, after: int = 0) -> dict[str, Any]:
        with self._lock:
            first = self.skipped_lines
            start = max(after, first)
            return {
                "id": self.id,
                "stack": self.stack,
                "action": self.action,
                "status": self.status,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "summary": list(self.summary),
                "lines": self.lines[start - first :],
                "next": first + len(self.lines),
            }


def repository(ref: str) -> str | None:
    """'ghcr.io/x/app:1.2@sha256:..' -> 'ghcr.io/x/app'. None for a bare image id."""
    ref = ref.split("@", 1)[0]
    if not ref or ref.startswith("sha256:"):
        return None
    name, _, tag = ref.rpartition(":")
    if name and "/" not in tag:  # a ':' after the last '/' starts the tag (not a port)
        ref = name
    return ref


def _tag(kind: str, project: str, service_name: str) -> str:
    return f"{kind}-{project}-{service_name}"[:128]


class JobManager:
    def __init__(
        self,
        settings: Any,
        gateway: Callable[[], DockerGateway],
        runner: Runner = compose.run,
        max_running: int = MAX_RUNNING,
    ) -> None:
        self.settings = settings
        self._gateway = gateway  # a getter, so a replaced gateway (tests) is picked up
        self.runner = runner
        self.max_running = max_running
        self._jobs: dict[str, Job] = {}
        self._running: dict[str, str] = {}  # stack name -> job id
        self._editing: set[str] = set()  # stacks whose compose file is being saved
        self._lock = threading.Lock()

    @property
    def gateway(self) -> DockerGateway:
        return self._gateway()

    # ------------------------------------------------------------ public

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def running_job(self, stack: str) -> str | None:
        with self._lock:
            return self._running.get(stack)

    def start(self, state: StackState, action: str, user_id: int, ip: str | None) -> Job:
        name = state.stack.name
        if action not in ACTIONS:
            raise ActionRefused("Unknown action", 404)
        if state.is_self:
            self._audit(action, name, "denied", user_id, reason="own stack", ip=ip)
            raise ActionRefused(
                "This is the stack this app runs in. Change it over SSH instead.", 403
            )
        with self._lock:
            self._prune()
            if name in self._running:
                raise ActionRefused(
                    "Another action is already running on this stack.", 409, self._running[name]
                )
            if name in self._editing:
                raise ActionRefused("The compose file is being saved. Try again in a moment.", 409)
            if len(self._running) >= self.max_running:
                raise ActionRefused(
                    "Other actions are running. Try again when they have finished.", 429
                )
            job = Job(
                id=secrets.token_hex(8),
                stack=name,
                action=action,
                user_id=user_id,
                started_at=clock.to_iso(clock.now()),
            )
            self._jobs[job.id] = job
            self._running[name] = job.id
        self._audit(action, name, "started", user_id, job=job.id, ip=ip)
        threading.Thread(
            target=self._run, args=(job, state), name=f"action-{name}", daemon=True
        ).start()
        return job

    @contextlib.contextmanager
    def editing(self, stack: str) -> Iterator[None]:
        """Hold the stack while its compose file is saved: no action may start,
        and no save happens while an action (which reads the file) runs."""
        with self._lock:
            if stack in self._running:
                raise ActionRefused(
                    "An action is running on this stack. Save when it has finished.",
                    409,
                    self._running[stack],
                )
            if stack in self._editing:
                raise ActionRefused("The compose file is already being saved.", 409)
            self._editing.add(stack)
        try:
            yield
        finally:
            with self._lock:
                self._editing.discard(stack)

    # ------------------------------------------------------------ internals

    def _prune(self) -> None:
        cutoff = clock.to_iso(clock.now() - _KEEP_FINISHED)
        for job_id in [
            j.id
            for j in self._jobs.values()
            if j.finished_at is not None and j.finished_at < cutoff
        ]:
            del self._jobs[job_id]

    def _audit(self, action: str, stack: str, result: str, user_id: int, **detail: Any) -> None:
        conn = db.connect(self.settings.db_path)
        try:
            audit.record(
                conn, f"stack_{action}", result, user_id=user_id, target_stack=stack, **detail
            )
        finally:
            conn.close()

    def _project(self, state: StackState) -> str:
        """Use the project name the containers were created with, if there are any."""
        from app.stacks import service  # late import: service imports nothing from here

        return service.compose_project(state.stack, state.containers)

    def _compose(
        self, job: Job, state: StackState, base: list[str], command: list[str], step: str
    ) -> compose.Result:
        job.add(f"$ docker compose {' '.join(command)}")
        return self.runner(
            [*base, *command], cwd=state.stack.path, on_line=job.add, timeout=_TIMEOUTS[step]
        )

    def _run(self, job: Job, state: StackState) -> None:
        started = time.monotonic()
        ok = False
        try:
            project = self._project(state)
            base = compose.base_args(state.stack, project)
            if job.action == "update":
                ok = self._update(job, state, base, project)
            else:
                ok = self._compose(job, state, base, _COMMANDS[job.action], job.action).ok
                if ok and job.action in ("start", "restart"):
                    self._mark_current(state.stack.name, project)
        except (compose.ComposeError, DockerUnavailable) as e:
            job.add(str(e))
        except Exception:
            log.exception("Action %s on %s failed", job.action, job.stack)
            job.add("The action failed unexpectedly. See the app's log for details.")
        finally:
            # The audit entry is written before the job is reported finished and the
            # stack is released. Otherwise someone seeing "done" could read the audit
            # log before the entry is there, and a next action on the stack could be
            # audited before this one has ended.
            try:
                self._audit(
                    job.action,
                    job.stack,
                    "ok" if ok else "error",
                    job.user_id,
                    job=job.id,
                    seconds=round(time.monotonic() - started, 1),
                    changes=job.summary,
                )
            except Exception:
                log.exception("Could not write the audit entry for job %s", job.id)
            job.finished_at = clock.to_iso(clock.now())
            job.status = "ok" if ok else "error"
            with self._lock:
                self._running.pop(job.stack, None)

    def _mark_current(self, stack_name: str, project: str) -> None:
        """Tag each running service's image as `current-<project>-<service>`.

        On Docker's containerd image store an image disappears as soon as its tag
        moves, even while containers still run on it. A manual `docker pull`, or
        another tool updating the stack, would then leave nothing to keep as the
        previous version. This tag keeps the image alive until the next update.
        """
        from app.stacks import service  # late import: service imports nothing from here

        gw = self.gateway
        state = service.get_stack(self.settings, gw, stack_name)
        for c in state.containers:
            if c.state != "running" or not c.service or c.service in state.stack.buildable:
                continue
            repo = repository(gw.inspect(c.id)["image"])
            if repo and c.image_id:
                gw.tag_image(c.image_id, repo, _tag("current", project, c.service))

    def _update(self, job: Job, state: StackState, base: list[str], project: str) -> bool:
        gw = self.gateway
        # 1. Find each service's running image and protect it with a temporary tag,
        #    so it cannot be deleted while the update runs.
        protected: dict[str, tuple[str, str, str]] = {}  # service -> (ref, repo, old id)
        for c in state.containers:
            if not c.service or c.service in state.stack.buildable:
                continue
            ref = gw.inspect(c.id)["image"]
            repo = repository(ref)
            if not repo:
                continue
            # The container's image, or, if its tag was moved outside this app,
            # the one the `current` tag kept alive.
            old_id = (c.image_id and gw.image_id(c.image_id)) or gw.image_id(
                f"{repo}:{_tag('current', project, c.service)}"
            )
            if old_id and gw.tag_image(old_id, repo, _tag("pending", project, c.service)):
                protected[c.service] = (ref, repo, old_id)
            else:
                job.summary.append(
                    f"{c.service}: the previous image could not be kept "
                    "(its tag was changed outside this app before the update)"
                )

        ok = False
        try:
            # 2. Pull, then recreate whatever changed.
            if self._compose(job, state, base, ["pull", "--ignore-buildable"], "pull").ok:
                ok = self._compose(job, state, base, ["up", "--detach"], "up").ok
        finally:
            # 3. Keep the previous image of every service whose tag moved, even if
            #    `up` failed: it is then the one still running.
            for service_name, (ref, repo, old_id) in sorted(protected.items()):
                self._keep_previous(job, state, project, service_name, ref, repo, old_id)
            for service_name in state.stack.buildable:
                job.summary.append(f"{service_name}: skipped (built locally)")
        return ok

    def _keep_previous(
        self,
        job: Job,
        state: StackState,
        project: str,
        service_name: str,
        ref: str,
        repo: str,
        old_id: str,
    ) -> None:
        gw = self.gateway
        pending = f"{repo}:{_tag('pending', project, service_name)}"
        current = _tag("current", project, service_name)
        now_id = gw.image_id(ref)
        if now_id is None or now_id == old_id:
            gw.tag_image(old_id, repo, current)
            gw.remove_image(pending)  # only untags: the image keeps its other tags
            job.summary.append(f"{service_name}: already up to date")
            return

        rollback = _tag("rollback", project, service_name)
        before = gw.image_id(f"{repo}:{rollback}")
        gw.tag_image(old_id, repo, rollback)
        gw.tag_image(now_id, repo, current)
        gw.remove_image(pending)
        line = f"{service_name}: {old_id.removeprefix('sha256:')[:12]} → "
        job.summary.append(line + now_id.removeprefix("sha256:")[:12])
        if before and before not in (old_id, now_id):
            # Only if no other tag (another service's or stack's) still uses it.
            gone = gw.remove_untagged(before) or gw.image_id(before) is None
            if gone:
                job.summary.append(f"{service_name}: removed the older previous image")

        conn = db.connect(self.settings.db_path)
        try:
            conn.execute(
                "INSERT INTO image_history (stack, service, image_ref, old_image_id, "
                "new_image_id, rollback_tag, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (state.stack.name, service_name, ref, old_id, now_id, rollback, job.user_id),
            )
        finally:
            conn.close()
