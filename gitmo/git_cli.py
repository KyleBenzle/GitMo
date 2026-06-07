from __future__ import annotations

"""Small wrappers around the local git executable.

GitMo deliberately delegates repository behavior to git instead of reimplementing
branch, commit, fetch, pull, and push logic in Python.
"""

import os
import subprocess
import urllib.parse
from pathlib import Path


class GitCommandError(RuntimeError):
    pass


def run_git(args: list[str], cwd: Path | None = None) -> str:
    command = ["git", *args]
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        details = stderr or stdout or "unknown git error"
        raise GitCommandError(f"{' '.join(command)} failed: {details}")
    return result.stdout.strip()


def is_git_repo(path: Path) -> bool:
    try:
        top_level = run_git(["rev-parse", "--show-toplevel"], cwd=path)
    except GitCommandError:
        return False
    return Path(top_level).resolve() == path.resolve()


def clone_repo(clone_url: str, target_path: Path) -> None:
    run_git(["clone", clone_url, str(target_path)])


def init_repo(path: Path) -> None:
    run_git(["init"], cwd=path)


def add_remote(path: Path, clone_url: str) -> None:
    run_git(["remote", "add", "origin", clone_url], cwd=path)


def set_remote_url(path: Path, clone_url: str) -> None:
    run_git(["remote", "set-url", "origin", clone_url], cwd=path)


def has_remote(path: Path, name: str = "origin") -> bool:
    try:
        run_git(["remote", "get-url", name], cwd=path)
        return True
    except GitCommandError:
        return False


def set_local_identity(path: Path, name: str, email: str) -> None:
    run_git(["config", "user.name", name], cwd=path)
    run_git(["config", "user.email", email], cwd=path)


def has_commits(path: Path) -> bool:
    try:
        run_git(["rev-parse", "HEAD"], cwd=path)
        return True
    except GitCommandError:
        return False


def current_branch(path: Path) -> str:
    return run_git(["branch", "--show-current"], cwd=path)


def ensure_tracking_branch(path: Path, branch: str) -> None:
    run_git(["branch", "--set-upstream-to", f"origin/{branch}", branch], cwd=path)


def stage_all(path: Path) -> None:
    run_git(["add", "-A"], cwd=path)


def has_staged_or_unstaged_changes(path: Path) -> bool:
    return bool(working_tree_status(path))


def working_tree_status(path: Path) -> str:
    return run_git(["status", "--porcelain"], cwd=path)


def commit(path: Path, message: str) -> bool:
    if not has_staged_or_unstaged_changes(path):
        return False
    stage_all(path)
    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=path,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "nothing to commit" in stderr:
            return False
        raise GitCommandError(f"git commit failed: {stderr or result.stdout.strip()}")
    return True


def push(path: Path, set_upstream: bool = False, branch: str | None = None) -> None:
    if set_upstream and branch:
        run_git(["push", "-u", "origin", branch], cwd=path)
    else:
        run_git(["push"], cwd=path)


def force_push(path: Path, branch: str) -> None:
    run_git(["push", "--force", "-u", "origin", branch], cwd=path)


def pull_ff_only(path: Path) -> None:
    run_git(["pull", "--ff-only"], cwd=path)


def fetch(path: Path) -> None:
    run_git(["fetch", "origin"], cwd=path)


def upstream_branch(path: Path) -> str | None:
    try:
        return run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=path)
    except GitCommandError:
        return None


def ahead_behind(path: Path) -> tuple[int, int]:
    upstream = upstream_branch(path)
    if not upstream:
        return (0, 0)
    counts = run_git(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], cwd=path)
    ahead_str, behind_str = counts.split()
    return int(ahead_str), int(behind_str)


def authenticated_clone_url(token: str, clone_url: str) -> str:
    parsed = urllib.parse.urlparse(clone_url)
    if parsed.scheme not in {"http", "https"}:
        return clone_url
    auth_netloc = f"{urllib.parse.quote(token, safe='')}@{parsed.netloc}"
    return urllib.parse.urlunparse(parsed._replace(netloc=auth_netloc))
