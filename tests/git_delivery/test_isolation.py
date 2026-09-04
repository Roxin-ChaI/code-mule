from datetime import UTC, datetime
from io import StringIO
import subprocess
import tempfile
import unittest
from pathlib import Path

from code_mule.cli.composition import ProductionCliComposition
from code_mule.git_delivery import GitDeliveryService, register_state_exclusion


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def initialize_repository(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.name", "Code Mule Test")
    git(root, "config", "user.email", "code-mule@example.invalid")
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    git(root, "add", "--", "README.md")
    git(root, "commit", "-q", "-m", "initial")


class GitWorkspaceIsolationTests(unittest.TestCase):
    def test_default_state_is_real_clean_and_baseline_stays_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize_repository(root)
            state_file = root / ".code-mule" / "project-state.json"
            composition = ProductionCliComposition(
                state_file,
                environment={},
                stdout=StringIO(),
                stderr=StringIO(),
            )
            composition.init_project("project-1", "Project", root)

            self.assertTrue(state_file.is_file())
            self.assertEqual(
                git(root, "status", "--short", "--untracked-files=all"), ""
            )
            GitDeliveryService(root, clock=lambda: NOW).capture_baseline("TASK-1")

            user_file = root / "user.py"
            user_file.write_text("untracked\n", encoding="utf-8")
            self.assertNotEqual(git(root, "status", "--short"), "")
            user_file.unlink()

            (root / "README.md").write_text("modified\n", encoding="utf-8")
            self.assertNotEqual(git(root, "status", "--short"), "")
            git(root, "checkout", "--", "README.md")

            (root / "README.md").write_text("staged\n", encoding="utf-8")
            git(root, "add", "--", "README.md")
            self.assertNotEqual(git(root, "status", "--short"), "")

    def test_registration_is_idempotent_and_external_state_is_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            container = Path(temporary)
            root = container / "repo"
            root.mkdir()
            initialize_repository(root)
            state_file = root / ".code-mule" / "project-state.json"

            first = register_state_exclusion(root, state_file)
            second = register_state_exclusion(root, state_file)
            exclude_path = Path(
                git(root, "rev-parse", "--git-path", "info/exclude")
            )
            if not exclude_path.is_absolute():
                exclude_path = root / exclude_path
            lines = exclude_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(first, ("/.code-mule/",))
            self.assertEqual(second, first)
            self.assertEqual(lines.count("/.code-mule/"), 1)

            external = container / "state.json"
            before = exclude_path.read_bytes()
            self.assertEqual(register_state_exclusion(root, external), ())
            self.assertEqual(exclude_path.read_bytes(), before)

    def test_linked_worktree_uses_git_reported_exclude_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            container = Path(temporary)
            main = container / "main"
            linked = container / "linked"
            main.mkdir()
            initialize_repository(main)
            git(main, "worktree", "add", "-q", "-b", "linked", str(linked))

            state_file = linked / ".code-mule" / "project-state.json"
            register_state_exclusion(linked, state_file)
            state_file.parent.mkdir()
            state_file.write_text("{}\n", encoding="utf-8")

            self.assertTrue((linked / ".git").is_file())
            self.assertEqual(
                git(linked, "status", "--short", "--untracked-files=all"), ""
            )


if __name__ == "__main__":
    unittest.main()
