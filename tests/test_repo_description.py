from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gitmo.app import repo_description


class RepoDescriptionTests(unittest.TestCase):
    def test_uses_first_prose_sentence_after_markdown_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo_path = Path(directory)
            (repo_path / "README.md").write_text(
                "# Example\n\n"
                "Example keeps local projects synchronized with GitHub. "
                "It also runs in the background.\n",
                encoding="utf-8",
            )

            self.assertEqual(
                repo_description(repo_path, "example"),
                "Example keeps local projects synchronized with GitHub.",
            )

    def test_skips_badges_lists_and_code_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo_path = Path(directory)
            (repo_path / "README.md").write_text(
                "# Example\n"
                "![Build](https://example.test/badge.svg)\n\n"
                "- Fast\n"
                "- Simple\n\n"
                "```python\n"
                "print('not a description')\n"
                "```\n\n"
                "A **useful** project with [documentation](https://example.test). More details follow.\n",
                encoding="utf-8",
            )

            self.assertEqual(
                repo_description(repo_path, "example"),
                "A useful project with documentation.",
            )

    def test_falls_back_to_humanized_repository_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                repo_description(Path(directory), "my-useful_project"),
                "Project files for my useful project.",
            )


if __name__ == "__main__":
    unittest.main()
