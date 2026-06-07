from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest

from gitmo.config import AppConfig, RepoConfig
from gitmo.sync_engine import (
    RepoStatus,
    SyncEngine,
    commit_message_for,
    working_tree_fingerprint,
)


class ConfigCompatibilityTests(unittest.TestCase):
    def test_old_repo_config_receives_new_defaults(self) -> None:
        config = AppConfig.from_dict(
            {
                "repos": {
                    "Example": {
                        "name": "Example",
                        "local_path": "/tmp/Example",
                        "sync_mode": "two-way",
                        "enabled": True,
                    }
                }
            }
        )

        repo = config.repos["Example"]
        self.assertEqual(repo.sync_schedule, "idle-1m")
        self.assertEqual(repo.commit_message_mode, "summary")


class SyncScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SyncEngine(AppConfig(), lambda *_args: None)

    def test_idle_schedule_uses_last_change_time(self) -> None:
        repo = RepoConfig("Example", sync_schedule="idle-1m")
        status = RepoStatus(last_event_at=100.0, dirty_since=20.0)

        self.assertFalse(self.engine._schedule_is_due(repo, status, 159.0))
        self.assertTrue(self.engine._schedule_is_due(repo, status, 160.0))

    def test_interval_schedule_uses_first_dirty_time(self) -> None:
        repo = RepoConfig("Example", sync_schedule="interval-5m")
        status = RepoStatus(last_event_at=290.0, dirty_since=10.0)

        self.assertFalse(self.engine._schedule_is_due(repo, status, 309.0))
        self.assertTrue(self.engine._schedule_is_due(repo, status, 310.0))

    def test_manual_schedule_is_never_automatically_due(self) -> None:
        repo = RepoConfig("Example", sync_schedule="manual")
        status = RepoStatus(last_event_at=1.0, dirty_since=1.0)

        self.assertFalse(self.engine._schedule_is_due(repo, status, 10_000.0))


class CommitMessageTests(unittest.TestCase):
    def test_summary_lists_changed_paths(self) -> None:
        status = " M gitmo/app.py\n?? tests/test_sync_preferences.py\n D old.txt"

        self.assertEqual(
            commit_message_for("summary", status),
            "GitMo: update 3 files (gitmo/app.py, tests/test_sync_preferences.py, old.txt)",
        )

    def test_datetime_message_is_stable(self) -> None:
        now = datetime(2026, 6, 7, 14, 35)

        self.assertEqual(
            commit_message_for("datetime", " M app.py", now),
            "GitMo autosave - 2026-06-07 14:35",
        )

    def test_standard_message_uses_existing_text(self) -> None:
        self.assertEqual(commit_message_for("standard", " M app.py"), "GitMo autosave")


class WorkingTreeFingerprintTests(unittest.TestCase):
    def test_repeated_edit_to_same_file_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            changed = repo / "app.py"
            changed.write_text("one", encoding="utf-8")
            first = working_tree_fingerprint(repo, " M app.py")

            changed.write_text("two", encoding="utf-8")
            stat = changed.stat()
            os.utime(changed, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            second = working_tree_fingerprint(repo, " M app.py")

        self.assertNotEqual(first, second)

    def test_deleted_file_has_a_stable_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)

            fingerprint = working_tree_fingerprint(repo, " D removed.txt")

        self.assertTrue(fingerprint)


if __name__ == "__main__":
    unittest.main()
