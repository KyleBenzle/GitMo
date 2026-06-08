from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gitmo import config as config_module
from gitmo.config import AppConfig
from gitmo.git_cli import public_clone_url


class CredentialStorageTests(unittest.TestCase):
    def test_token_is_saved_separately_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir) / "gitmo"
            config_path = app_dir / "config.json"
            credentials_path = app_dir / "credentials.json"
            with (
                patch.object(config_module, "APP_DIR", app_dir),
                patch.object(config_module, "CONFIG_PATH", config_path),
                patch.object(config_module, "CREDENTIALS_PATH", credentials_path),
            ):
                config_module.save_config(AppConfig(github_token="secret-token"))

            saved_config = json.loads(config_path.read_text(encoding="utf-8"))
            saved_credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
            self.assertNotIn("github_token", saved_config)
            self.assertEqual(saved_credentials["github_token"], "secret-token")
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(credentials_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(app_dir.stat().st_mode & 0o777, 0o700)

    def test_old_inline_token_is_migrated_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir) / "gitmo"
            app_dir.mkdir()
            config_path = app_dir / "config.json"
            credentials_path = app_dir / "credentials.json"
            legacy_path = Path(temp_dir) / "legacy.json"
            config_path.write_text(
                json.dumps({"github_token": "old-token", "repos": {}}),
                encoding="utf-8",
            )
            with (
                patch.object(config_module, "APP_DIR", app_dir),
                patch.object(config_module, "CONFIG_PATH", config_path),
                patch.object(config_module, "CREDENTIALS_PATH", credentials_path),
                patch.object(config_module, "LEGACY_CONFIG_PATH", legacy_path),
            ):
                loaded = config_module.load_config()

            self.assertEqual(loaded.github_token, "old-token")
            self.assertNotIn(
                "github_token",
                json.loads(config_path.read_text(encoding="utf-8")),
            )
            self.assertEqual(
                json.loads(credentials_path.read_text(encoding="utf-8"))["github_token"],
                "old-token",
            )


class RemoteUrlTests(unittest.TestCase):
    def test_authenticated_https_url_is_cleaned(self) -> None:
        self.assertEqual(
            public_clone_url("https://github_pat_secret@github.com/KyleBenzle/GitMo.git"),
            "https://github.com/KyleBenzle/GitMo.git",
        )

    def test_public_https_url_is_unchanged(self) -> None:
        url = "https://github.com/KyleBenzle/GitMo.git"
        self.assertEqual(public_clone_url(url), url)


if __name__ == "__main__":
    unittest.main()
