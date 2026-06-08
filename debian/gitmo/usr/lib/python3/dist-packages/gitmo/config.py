from __future__ import annotations

"""Read and write GitMo's user settings.

The config file lives outside the project so installed copies and source-tree
copies share the same saved token, GitMo folder, and selected repos.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


APP_DIR = Path.home() / ".config" / "gitmo"
CONFIG_PATH = APP_DIR / "config.json"
CREDENTIALS_PATH = APP_DIR / "credentials.json"
LOG_PATH = APP_DIR / "gitmo.log"
LEGACY_CONFIG_PATH = Path.home() / ".config" / "gitlo" / "config.json"


@dataclass
class RepoConfig:
    name: str
    local_path: str = ""
    sync_mode: str = "two-way"
    enabled: bool = True
    sync_schedule: str = "idle-1m"
    commit_message_mode: str = "summary"


@dataclass
class CachedGitHubRepo:
    name: str
    clone_url: str
    private: bool = False
    default_branch: str = "main"


@dataclass
class AppConfig:
    github_token: str = ""
    github_login: str = ""
    github_email: str = ""
    gitmo_path: str = ""
    font_size_delta: int = 0
    repos: dict[str, RepoConfig] = field(default_factory=dict)
    cached_github_login: str = ""
    cached_github_repos: dict[str, CachedGitHubRepo] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        repos = {
            name: RepoConfig(**repo_data)
            for name, repo_data in data.get("repos", {}).items()
        }
        cached_github_repos = {
            name: CachedGitHubRepo(**repo_data)
            for name, repo_data in data.get("cached_github_repos", {}).items()
        }
        return cls(
            github_token=data.get("github_token", ""),
            github_login=data.get("github_login", ""),
            github_email=data.get("github_email", ""),
            gitmo_path=data.get("gitmo_path", ""),
            font_size_delta=int(data.get("font_size_delta", 0)),
            repos=repos,
            cached_github_login=data.get("cached_github_login", ""),
            cached_github_repos=cached_github_repos,
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("github_token", None)
        data["repos"] = {
            name: asdict(repo_config)
            for name, repo_config in self.repos.items()
        }
        data["cached_github_repos"] = {
            name: asdict(repo)
            for name, repo in self.cached_github_repos.items()
        }
        return data


def load_config() -> AppConfig:
    config_path = CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_CONFIG_PATH
    if not config_path.exists():
        return AppConfig(github_token=load_github_token())
    data = json.loads(config_path.read_text(encoding="utf-8"))
    config = AppConfig.from_dict(data)
    saved_token = load_github_token()
    if saved_token:
        config.github_token = saved_token
    elif config.github_token:
        save_github_token(config.github_token)
        save_config(config)
    return config


def save_config(config: AppConfig) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    APP_DIR.chmod(0o700)
    if config.github_token:
        save_github_token(config.github_token)
    write_private_json(CONFIG_PATH, config.to_dict())


def load_github_token() -> str:
    try:
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    token = data.get("github_token", "")
    return token if isinstance(token, str) else ""


def save_github_token(token: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    APP_DIR.chmod(0o700)
    write_private_json(CREDENTIALS_PATH, {"github_token": token})


def write_private_json(path: Path, data: dict) -> None:
    content = json.dumps(data, indent=2, sort_keys=True) + "\n"
    file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o600)
