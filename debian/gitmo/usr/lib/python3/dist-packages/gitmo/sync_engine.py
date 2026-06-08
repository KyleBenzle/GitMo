from __future__ import annotations

"""Background sync loop for selected repositories.

The UI owns selection and setup. This module owns the repeated "look at git
state, decide what to do, then commit/push/pull when safe" workflow.
"""

import threading
import time
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path

from gitmo.config import AppConfig, RepoConfig
from gitmo.git_cli import (
    GitCommandError,
    add_remote,
    ahead_behind,
    commit,
    current_branch,
    fetch,
    has_remote,
    has_commits,
    has_staged_or_unstaged_changes,
    init_repo,
    is_git_repo,
    pull_ff_only,
    push,
    sanitize_remote_url,
    set_remote_url,
    stage_all,
    upstream_branch,
    working_tree_status,
)


AUTOSAVE_MESSAGE = "GitMo autosave"
POLL_INTERVAL_SECONDS = 10
SCHEDULE_DELAYS = {
    "idle-1m": 60,
    "interval-5m": 5 * 60,
    "interval-10m": 10 * 60,
    "interval-30m": 30 * 60,
}


@dataclass
class RepoStatus:
    state: str = "idle"
    detail: str = ""
    last_event_at: float = 0.0
    last_sync_at: float = 0.0
    change_signature: str = ""
    dirty_since: float = 0.0


class SyncEngine:
    def __init__(self, config: AppConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self.repo_statuses: dict[str, RepoStatus] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def status_for(self, repo_name: str) -> RepoStatus:
        with self._lock:
            return self.repo_statuses.setdefault(repo_name, RepoStatus())

    def _set_status(self, repo_name: str, state: str, detail: str) -> None:
        with self._lock:
            status = self.repo_statuses.setdefault(repo_name, RepoStatus())
            status.state = state
            status.detail = detail
        self.logger(repo_name, f"[{state}] {detail}")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(POLL_INTERVAL_SECONDS)

    def run_once(self, *, force_autosave: bool = False) -> None:
        for repo_name, repo_config in list(self.config.repos.items()):
            if self._stop_event.is_set():
                break
            if not repo_config.enabled:
                continue
            repo_path = self.repo_path_for(repo_name, repo_config)
            if not repo_path.exists() or not is_git_repo(repo_path):
                self._set_status(repo_name, "missing", "Local repository is missing.")
                continue
            try:
                if sanitize_remote_url(repo_path):
                    self.logger(repo_name, "Removed saved credentials from the Git remote URL.")
                self._sync_repo(
                    repo_path,
                    repo_name,
                    repo_config,
                    force_autosave=force_autosave,
                )
            except GitCommandError as exc:
                self._set_status(repo_name, "error", str(exc))
            except Exception as exc:  # pragma: no cover
                self._set_status(repo_name, "error", f"Unexpected error: {exc}")

    def repo_path_for(self, repo_name: str, repo_config: RepoConfig) -> Path:
        if repo_config.local_path:
            return Path(repo_config.local_path).expanduser()
        return Path(self.config.gitmo_path).expanduser() / repo_name

    def _sync_repo(
        self,
        repo_path: Path,
        repo_name: str,
        repo_config: RepoConfig,
        *,
        force_autosave: bool = False,
    ) -> None:
        status = self.status_for(repo_name)
        now = time.time()
        status_text = working_tree_status(repo_path)
        change_signature = working_tree_fingerprint(repo_path, status_text)
        if status_text:
            if not status.dirty_since:
                status.dirty_since = now
            if change_signature != status.change_signature:
                status.last_event_at = now
                status.change_signature = change_signature
        else:
            status.dirty_since = 0.0
            status.last_event_at = 0.0
            status.change_signature = ""

        if repo_config.sync_mode == "two-way":
            can_autosave = self._sync_two_way(repo_path, repo_name)
        else:
            can_autosave = self._sync_one_way(repo_path, repo_name)
        status.last_sync_at = now

        if can_autosave and change_signature and (
            force_autosave or self._schedule_is_due(repo_config, status, now)
        ):
            self._autosave(repo_path, repo_name, repo_config, status_text)
            status.dirty_since = 0.0
            status.last_event_at = 0.0
            status.change_signature = ""

    def _sync_one_way(self, repo_path: Path, repo_name: str) -> bool:
        fetch(repo_path, token=self.config.github_token)
        ahead, behind = ahead_behind(repo_path)
        if behind > 0:
            self._set_status(
                repo_name,
                "warning",
                f"Remote is ahead by {behind} commit(s); one-way mode will not pull.",
            )
            return False
        elif ahead > 0:
            push(repo_path, token=self.config.github_token)
            self._set_status(repo_name, "synced", f"Pushed {ahead} local commit(s).")
        else:
            self._set_status(repo_name, "idle", "Watching for local changes.")
        return True

    def _sync_two_way(self, repo_path: Path, repo_name: str) -> bool:
        fetch(repo_path, token=self.config.github_token)
        ahead, behind = ahead_behind(repo_path)
        if ahead > 0 and behind > 0:
            self._set_status(
                repo_name,
                "conflict",
                f"Both local and remote changed ({ahead} ahead, {behind} behind).",
            )
            return False
        if behind > 0:
            pull_ff_only(repo_path, token=self.config.github_token)
            self._set_status(repo_name, "pulled", f"Pulled {behind} remote commit(s).")
        elif ahead > 0:
            push(repo_path, token=self.config.github_token)
            self._set_status(repo_name, "synced", f"Pushed {ahead} local commit(s).")
        else:
            self._set_status(repo_name, "idle", "Watching for local and remote changes.")
        return True

    def _schedule_is_due(
        self,
        repo_config: RepoConfig,
        status: RepoStatus,
        now: float,
    ) -> bool:
        schedule = repo_config.sync_schedule
        if schedule == "manual":
            return False
        delay = SCHEDULE_DELAYS.get(schedule, SCHEDULE_DELAYS["idle-1m"])
        started_at = status.last_event_at if schedule == "idle-1m" else status.dirty_since
        return bool(started_at and now - started_at >= delay)

    def _autosave(
        self,
        repo_path: Path,
        repo_name: str,
        repo_config: RepoConfig,
        status_text: str,
    ) -> None:
        if not has_staged_or_unstaged_changes(repo_path):
            return
        message = commit_message_for(repo_config.commit_message_mode, status_text)
        if commit(repo_path, message):
            if upstream_branch(repo_path):
                push(repo_path, token=self.config.github_token)
            else:
                branch = current_branch(repo_path) or "main"
                push(
                    repo_path,
                    set_upstream=True,
                    branch=branch,
                    token=self.config.github_token,
                )
            self._set_status(repo_name, "synced", "Committed and pushed local changes.")

    def prepare_new_remote_repo(self, repo_path: Path, clone_url: str, force: bool = False) -> None:
        if not is_git_repo(repo_path):
            init_repo(repo_path)
        if has_remote(repo_path):
            set_remote_url(repo_path, clone_url)
        else:
            add_remote(repo_path, clone_url)
        if has_staged_or_unstaged_changes(repo_path) or has_commits(repo_path):
            self._initial_push(repo_path, force=force)

    def _initial_push(self, repo_path: Path, force: bool = False) -> None:
        stage_all(repo_path)
        if not has_commits(repo_path):
            commit(repo_path, AUTOSAVE_MESSAGE)
        branch = current_branch(repo_path) or "main"
        if force:
            from gitmo.git_cli import force_push

            force_push(repo_path, branch, token=self.config.github_token)
        else:
            push(
                repo_path,
                set_upstream=True,
                branch=branch,
                token=self.config.github_token,
            )

def commit_message_for(mode: str, status_text: str, now: datetime | None = None) -> str:
    if mode == "datetime":
        timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
        return f"{AUTOSAVE_MESSAGE} - {timestamp}"
    if mode != "summary":
        return AUTOSAVE_MESSAGE

    paths = changed_paths(status_text)
    if not paths:
        return AUTOSAVE_MESSAGE
    shown = ", ".join(paths[:3])
    remaining = len(paths) - 3
    suffix = f" and {remaining} more" if remaining > 0 else ""
    noun = "file" if len(paths) == 1 else "files"
    return f"GitMo: update {len(paths)} {noun} ({shown}{suffix})"


def changed_paths(status_text: str) -> list[str]:
    paths = []
    for line in status_text.splitlines():
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if path:
            paths.append(path)
    return paths


def working_tree_fingerprint(repo_path: Path, status_text: str) -> str:
    if not status_text:
        return ""
    parts = [status_text]
    for relative_path in changed_paths(status_text):
        path = repo_path / relative_path
        try:
            stat = path.stat()
        except FileNotFoundError:
            parts.append(f"{relative_path}:missing")
            continue
        parts.append(f"{relative_path}:{stat.st_mtime_ns}:{stat.st_size}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
