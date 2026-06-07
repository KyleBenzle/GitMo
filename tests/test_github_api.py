from __future__ import annotations

import unittest
from unittest.mock import patch

from gitmo.github_api import GitHubClient


class CreateRepoTests(unittest.TestCase):
    def test_new_repository_is_public_by_default(self) -> None:
        client = GitHubClient("test-token")
        response = {
            "name": "example",
            "clone_url": "https://github.com/example/example.git",
            "private": False,
            "default_branch": "main",
        }

        with patch.object(client, "_request", return_value=response) as request:
            repo = client.create_repo("example")

        request.assert_called_once_with(
            "POST",
            "/user/repos",
            payload={"name": "example", "private": False, "auto_init": False},
        )
        self.assertFalse(repo.private)

    def test_repository_description_is_sent_when_provided(self) -> None:
        client = GitHubClient("test-token")
        response = {
            "name": "example",
            "clone_url": "https://github.com/example/example.git",
            "private": False,
            "default_branch": "main",
        }

        with patch.object(client, "_request", return_value=response) as request:
            client.create_repo("example", description="An example project.")

        request.assert_called_once_with(
            "POST",
            "/user/repos",
            payload={
                "name": "example",
                "private": False,
                "auto_init": False,
                "description": "An example project.",
            },
        )


if __name__ == "__main__":
    unittest.main()
