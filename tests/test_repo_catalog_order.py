from pathlib import Path
import unittest
import tempfile

from gitmo.app import (
    LocalValue,
    RepoSelection,
    repo_catalog_sort_key,
    repo_settings_state,
    repo_targets_changed,
    sync_button_presentation,
    tail_text_lines,
)


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
    def test_tail_text_lines_reads_only_requested_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gitmo.log"
            path.write_text("\n".join(f"line {index}" for index in range(200)), encoding="utf-8")

            self.assertEqual(
                tail_text_lines(path, 3),
                ["line 197", "line 198", "line 199"],
            )

    def test_local_value_notifies_without_creating_tk_state(self) -> None:
        values: list[str] = []
        value = LocalValue("old")
        value.trace_add("write", lambda: values.append(value.get()))

        value.set("new")

        self.assertEqual(value.get(), "new")
        self.assertEqual(values, ["new"])

    def test_sync_button_presentation_tracks_running_state(self) -> None:
        self.assertEqual(sync_button_presentation(True)[:2], ("■", "Stop Sync"))
        self.assertEqual(sync_button_presentation(False)[:2], ("▶", "Start Sync"))

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

    def test_unchanged_targets_do_not_require_repository_work(self) -> None:
        repo = selection("example", github=True, local=True)

        self.assertFalse(
            repo_targets_changed(repo, wants_github=True, wants_local=True)
        )
        self.assertTrue(
            repo_targets_changed(repo, wants_github=False, wants_local=True)
        )

    def test_repo_settings_state_detects_pending_option_changes(self) -> None:
        baseline = repo_settings_state(
            github=True,
            local=True,
            enabled=True,
            sync_mode="two-way",
            sync_schedule="idle-1m",
            commit_message_mode="summary",
            local_path="/tmp/example",
        )
        changed = repo_settings_state(
            github=True,
            local=True,
            enabled=True,
            sync_mode="two-way",
            sync_schedule="interval-5m",
            commit_message_mode="summary",
            local_path="/tmp/example",
        )

        self.assertNotEqual(baseline, changed)


if __name__ == "__main__":
    unittest.main()
