from __future__ import annotations

import io
import unittest
import urllib.error
from unittest.mock import patch

from gitmo.git_cli import is_auth_error
from gitmo.github_api import GitHubAPIError, GitHubAuthError, GitHubClient


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


class TokenRejectionTests(unittest.TestCase):
    def _http_error(self, code: int, body: str) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://api.github.com/user", code, "error", {}, io.BytesIO(body.encode("utf-8"))
        )

    def test_401_raises_auth_error(self) -> None:
        error = self._http_error(401, '{"message": "Bad credentials"}')
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(GitHubAuthError):
                GitHubClient("expired").get_authenticated_user()

    def test_other_errors_show_github_message_not_raw_json(self) -> None:
        error = self._http_error(422, '{"message": "name already exists on this account"}')
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(GitHubAPIError) as context:
                GitHubClient("token").create_repo("example")
        self.assertNotIsInstance(context.exception, GitHubAuthError)
        self.assertIn("name already exists on this account", str(context.exception))
        self.assertNotIn("{", str(context.exception))

    def test_git_auth_failures_are_recognised(self) -> None:
        self.assertTrue(is_auth_error(
            "git push failed: remote: Invalid username or token. "
            "fatal: Authentication failed for 'https://github.com/x/y.git/'"
        ))
        self.assertFalse(is_auth_error("git fetch origin failed: Could not resolve host: github.com"))


if __name__ == "__main__":
    unittest.main()
