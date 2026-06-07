from __future__ import annotations

"""Small GitHub REST API client used by the UI.

Keeping this wrapper narrow makes it easier to replace or test than spreading
urllib calls through the Tkinter code.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


API_BASE = "https://api.github.com"


class GitHubAPIError(RuntimeError):
    pass


@dataclass
class GitHubRepo:
    name: str
    clone_url: str
    private: bool
    default_branch: str


class GitHubClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        query: dict[str, str | int] | None = None,
    ) -> dict | list:
        url = f"{API_BASE}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            url=url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "GitMo",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise GitHubAPIError(f"GitHub API error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise GitHubAPIError(f"GitHub API request failed: {exc.reason}") from exc

    def get_authenticated_user(self) -> dict:
        return self._request("GET", "/user")

    def list_repos(self) -> list[GitHubRepo]:
        repos: list[GitHubRepo] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                "/user/repos",
                query={
                    "per_page": 100,
                    "page": page,
                    "sort": "full_name",
                    "visibility": "all",
                    "affiliation": "owner,collaborator,organization_member",
                },
            )
            if not isinstance(data, list) or not data:
                break
            repos.extend(
                GitHubRepo(
                    name=item["name"],
                    clone_url=item["clone_url"],
                    private=item["private"],
                    default_branch=item.get("default_branch") or "main",
                )
                for item in data
            )
            page += 1
        return repos

    def create_repo(
        self,
        name: str,
        private: bool = False,
        description: str | None = None,
    ) -> GitHubRepo:
        payload: dict[str, str | bool] = {
            "name": name,
            "private": private,
            "auto_init": False,
        }
        if description:
            payload["description"] = description
        data = self._request(
            "POST",
            "/user/repos",
            payload=payload,
        )
        if not isinstance(data, dict):
            raise GitHubAPIError("Unexpected response while creating repository")
        return GitHubRepo(
            name=data["name"],
            clone_url=data["clone_url"],
            private=data["private"],
            default_branch=data.get("default_branch") or "main",
        )

    def delete_repo(self, owner: str, name: str) -> None:
        owner_path = urllib.parse.quote(owner, safe="")
        repo_path = urllib.parse.quote(name, safe="")
        self._request("DELETE", f"/repos/{owner_path}/{repo_path}")
