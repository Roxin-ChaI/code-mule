"""Deterministic shell-level tests for the persistent CLI installer."""

from pathlib import Path
import os
from tempfile import TemporaryDirectory
import unittest

from tests.onboarding.helpers import INSTALL_SCRIPT, git_repo, run_command


class PersistentInstallScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.prefix = cls.root / "share" / "code-mule"
        cls.bin_dir = cls.root / "bin"
        cls.launcher = cls.bin_dir / "code-mule"
        cls.install_output = run_command(
            (
                "bash",
                str(INSTALL_SCRIPT),
                "--prefix",
                str(cls.prefix),
                "--bin-dir",
                str(cls.bin_dir),
                "--no-deps",
            ),
            timeout=180,
        )
        if cls.install_output.returncode != 0:
            raise AssertionError(cls.install_output.stdout + cls.install_output.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def clean_environment(self, *, include_git_system=False):
        parts = [str(self.bin_dir)]
        if include_git_system:
            for candidate in (
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
                "/opt/homebrew/bin",
            ):
                if os.path.isdir(candidate):
                    parts.append(candidate)
        environment = {
            "HOME": str(self.root / "home"),
            "PATH": ":".join(parts),
            "PYTHONPATH": "",
            "VIRTUAL_ENV": "",
        }
        return environment

    def test_install_creates_isolated_prefix_and_launcher(self):
        self.assertTrue(self.launcher.is_symlink())
        self.assertTrue(
            self.launcher.resolve().is_relative_to(self.prefix.resolve())
        )
        marker = self.prefix / "CODE_MULE_INSTALL"
        self.assertTrue(marker.exists())
        marker_text = marker.read_text(encoding="utf-8")
        self.assertIn("Code Mule persistent installation", marker_text)
        self.assertIn("status: ready", marker_text)
        self.assertIn("model_dependency: skipped", marker_text)
        self.assertTrue((self.prefix / "venv" / "bin" / "python").exists())
        package = self.prefix / "venv" / "lib"
        package_init = next(package.glob("python*/site-packages/code_mule/__init__.py"))
        self.assertTrue(package_init.exists())
        self.assertNotIn("repo .venv", marker_text)

    def test_launcher_works_without_any_virtualenv_activation(self):
        completed = run_command(
            (str(self.launcher), "--help"),
            cwd=self.root,
            environment=self.clean_environment(),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("usage: code-mule", completed.stdout)

    def test_restart_shell_regression_without_repo_venv(self):
        project = git_repo(self.root / "target-repo")
        original_parts = os.environ.get("PATH", "").split(":")
        filtered = [
            part
            for part in original_parts
            if part
            and not part.endswith(".venv/bin")
            and not part.endswith(".venv/Scripts")
        ]
        env = self.clean_environment(include_git_system=True)
        env["PATH"] = str(self.bin_dir) + ":" + ":".join(filtered)
        env["PYTHONPATH"] = ""
        env["VIRTUAL_ENV"] = ""

        other_dir = self.root / "other-directory"
        other_dir.mkdir()
        help_run = run_command(
            (str(self.launcher), "--help"),
            cwd=other_dir,
            environment=env,
        )
        self.assertEqual(help_run.returncode, 0)
        self.assertIn("usage: code-mule", help_run.stdout)

        init_run = run_command(
            (
                str(self.launcher),
                "init",
                "--project-id",
                "target",
                "--name",
                "Target",
                "--workspace",
                str(project),
            ),
            cwd=project,
            environment=env,
        )
        self.assertEqual(init_run.returncode, 0, init_run.stdout + init_run.stderr)

        status_run = run_command(
            (str(self.launcher), "status"),
            cwd=project,
            environment=env,
        )
        self.assertEqual(status_run.returncode, 0, status_run.stdout + status_run.stderr)
        self.assertIn("PROJECT", status_run.stdout)
        self.assertNotIn(".venv/bin", env["PATH"])

    def test_reinstall_refreshes_in_place_without_deleting_environment(self):
        before_python = self.prefix / "venv" / "bin" / "python"
        refreshed = run_command(
            (
                "bash",
                str(INSTALL_SCRIPT),
                "--prefix",
                str(self.prefix),
                "--bin-dir",
                str(self.bin_dir),
                "--no-deps",
                "--reinstall",
            ),
            timeout=180,
        )
        self.assertEqual(
            refreshed.returncode,
            0,
            refreshed.stdout + refreshed.stderr,
        )
        self.assertTrue(before_python.exists())
        self.assertTrue(self.launcher.is_symlink())
        marker = self.prefix / "CODE_MULE_INSTALL"
        self.assertIn("status: ready", marker.read_text(encoding="utf-8"))
        completed = run_command(
            (str(self.launcher), "--help"),
            environment=self.clean_environment(),
        )
        self.assertEqual(completed.returncode, 0)

    def test_existing_unrelated_launcher_conflict_fails_closed(self):
        root = Path(self.temporary.name) / "conflict"
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True)
        conflicting = bin_dir / "code-mule"
        conflicting.write_text("#!/bin/sh\necho unrelated\nexit 0\n", encoding="utf-8")
        conflicting.chmod(0o755)
        prefix = root / "prefix"
        completed = run_command(
            (
                "bash",
                str(INSTALL_SCRIPT),
                "--prefix",
                str(prefix),
                "--bin-dir",
                str(bin_dir),
                "--no-deps",
            ),
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Refusing to overwrite unrelated executable", completed.stderr)
        self.assertEqual(
            conflicting.read_text(encoding="utf-8"),
            "#!/bin/sh\necho unrelated\nexit 0\n",
        )
        self.assertFalse((prefix / "CODE_MULE_INSTALL").exists())

    def test_missing_python_3_12_fails_before_any_changes(self):
        root = Path(self.temporary.name) / "python-missing"
        python_bin = root / "python3.12"
        python_bin.parent.mkdir(parents=True)
        python_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python_bin.chmod(0o755)
        prefix = root / "prefix"
        bin_dir = root / "bin"
        completed = run_command(
            (
                "bash",
                str(INSTALL_SCRIPT),
                "--prefix",
                str(prefix),
                "--bin-dir",
                str(bin_dir),
                "--python",
                str(python_bin),
                "--no-deps",
            ),
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("requires Python 3.12", completed.stderr)
        self.assertFalse(prefix.exists())

    def test_path_missing_bin_dir_prints_export_suggestion(self):
        output = self.install_output.stdout
        self.assertIn("Add this directory to PATH:", output)
        self.assertIn("export PATH=", output)
        self.assertIn("--configure-shell", output)

    def test_configure_shell_is_explicit_and_idempotent(self):
        root = Path(self.temporary.name) / "configure"
        prefix = root / "share" / "code-mule"
        bin_dir = root / "bin"
        rc_file = root / ".zshrc"
        environment = {
            "HOME": str(self.root / "home"),
            "CODE_MULE_SHELL_RC": str(rc_file),
        }
        first = run_command(
            (
                "bash",
                str(INSTALL_SCRIPT),
                "--prefix",
                str(prefix),
                "--bin-dir",
                str(bin_dir),
                "--no-deps",
                "--configure-shell",
            ),
            environment=environment,
            timeout=180,
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        content = rc_file.read_text(encoding="utf-8")
        self.assertIn("export PATH=", content)
        self.assertEqual(content.count("export PATH="), 1)

        without_flag = run_command(
            (
                "bash",
                str(INSTALL_SCRIPT),
                "--prefix",
                str(prefix),
                "--bin-dir",
                str(bin_dir),
                "--no-deps",
            ),
            environment={"HOME": str(self.root / "home")},
            timeout=180,
        )
        self.assertEqual(
            without_flag.returncode,
            0,
            without_flag.stdout + without_flag.stderr,
        )
        self.assertEqual(
            rc_file.read_text(encoding="utf-8"),
            content,
        )


if __name__ == "__main__":
    unittest.main()
