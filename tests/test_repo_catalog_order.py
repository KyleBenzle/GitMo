from pathlib import Path
import unittest

from gitmo.app import RepoSelection, repo_catalog_sort_key


def selection(name: str, *, github: bool, local: bool) -> RepoSelection:
    return RepoSelection(
        name=name,
        local_path=Path("/tmp") / name,
        exists_local=local,
        exists_remote=github,
        local_is_repo=local,
        source="test",
    )


class RepoCatalogOrderTests(unittest.TestCase):
    def test_groups_repositories_then_sorts_each_group_alphabetically(self) -> None:
        repos = [
            selection("Zulu-rest", github=False, local=False),
            selection("Zulu-local", github=False, local=True),
            selection("beta-both", github=True, local=True),
            selection("Zulu-github", github=True, local=False),
            selection("Alpha-rest", github=False, local=False),
            selection("Alpha-local", github=False, local=True),
            selection("Alpha-both", github=True, local=True),
            selection("Alpha-github", github=True, local=False),
        ]

        ordered = sorted(repos, key=repo_catalog_sort_key)

        self.assertEqual(
            [repo.name for repo in ordered],
            [
                "Alpha-both",
                "beta-both",
                "Alpha-github",
                "Zulu-github",
                "Alpha-local",
                "Zulu-local",
                "Alpha-rest",
                "Zulu-rest",
            ],
        )


if __name__ == "__main__":
    unittest.main()
