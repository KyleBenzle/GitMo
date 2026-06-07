from __future__ import annotations

import unittest

from gitmo.config import AppConfig, CachedGitHubRepo


class GitHubRepoCacheTests(unittest.TestCase):
    def test_cache_round_trips_through_config_data(self) -> None:
        config = AppConfig(
            github_login="example",
            cached_github_login="example",
            cached_github_repos={
                "project": CachedGitHubRepo(
                    name="project",
                    clone_url="https://github.com/example/project.git",
                    private=False,
                    default_branch="main",
                )
            },
        )

        restored = AppConfig.from_dict(config.to_dict())

        self.assertEqual(restored.cached_github_login, "example")
        self.assertEqual(
            restored.cached_github_repos["project"].clone_url,
            "https://github.com/example/project.git",
        )

    def test_old_config_without_cache_remains_compatible(self) -> None:
        restored = AppConfig.from_dict({"github_login": "example", "repos": {}})

        self.assertEqual(restored.cached_github_login, "")
        self.assertEqual(restored.cached_github_repos, {})


if __name__ == "__main__":
    unittest.main()
