"""Deterministic shell-level tests for the persistent CLI installer."""

from pathlib import Path
import os
import platform
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
                "--no-configure-shell",
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
                "--no-configure-shell",
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
                "--no-configure-shell",
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

    def test_no_configure_shell_skips_rc_and_prints_manual_fallback(self):
        output = self.install_output.stdout
        self.assertIn(
            "Automatic PATH configuration skipped (--no-configure-shell).",
            output,
        )
        self.assertIn("export PATH=", output)

    def test_configure_shell_is_legacy_equivalent_to_default_and_idempotent(self):
        root = Path(self.temporary.name) / "configure"
        prefix = root / "share" / "code-mule"
        bin_dir = root / "bin"
        rc_file = root / ".zshrc"
        environment = {
            "HOME": str(self.root / "home"),
            "CODE_MULE_SHELL_RC": str(rc_file),
            "SHELL": "/bin/zsh",
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
            environment={
                "HOME": str(self.root / "home"),
                "CODE_MULE_SHELL_RC": str(rc_file),
                "SHELL": "/bin/zsh",
            },
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


class AutomaticPathConfigTests(unittest.TestCase):
    """Default automatic PATH behavior across shells, rc states, and safety."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.app_prefix = cls.root / "share" / "code-mule"
        cls.bin_dir = cls.root / "bin"
        cls.system_path = ":".join(
            path
            for path in (
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
                "/usr/local/bin",
                "/opt/homebrew/bin",
            )
            if os.path.isdir(path)
        )
        cls.outputs = {}
        homes = {
            "zsh": "zsh-home",
            "bash": "bash-home",
            "existing": "existing-home",
            "path_present": "path-home",
            "unknown": "unknown-home",
            "no_config": "no-config-home",
            "legacy": "legacy-home",
            "symlink": "symlink-home",
            "unwritable": "unwritable-home",
        }
        cls.homes = {
            name: cls.root / directory for name, directory in homes.items()
        }
        for home in cls.homes.values():
            home.mkdir(parents=True, exist_ok=True)

        user_content = (
            "# existing user configuration\n"
            "alias ll='ls -la'\n"
            "export EDITOR=code\n"
        )
        (cls.homes["existing"] / ".zshrc").write_text(
            user_content,
            encoding="utf-8",
        )

        symlink_target = cls.homes["symlink"] / "real-rc.txt"
        symlink_target.write_text("keep me\n", encoding="utf-8")
        (cls.homes["symlink"] / ".zshrc").symlink_to(symlink_target)

        unwritable_rc = cls.homes["unwritable"] / ".zshrc"
        unwritable_rc.write_text("read only\n", encoding="utf-8")
        unwritable_rc.chmod(0o444)

        cls.outputs["zsh"] = cls._install(
            cls.homes["zsh"],
            "/bin/zsh",
            name="zsh",
        )
        cls.outputs["bash"] = cls._install(
            cls.homes["bash"],
            "/bin/bash",
            name="bash",
        )
        cls.outputs["existing"] = cls._install(
            cls.homes["existing"],
            "/bin/zsh",
            name="existing",
        )
        cls.outputs["existing_second"] = cls._install(
            cls.homes["existing"],
            "/bin/zsh",
            name="existing_second",
        )
        cls.outputs["path_present"] = cls._install(
            cls.homes["path_present"],
            "/bin/zsh",
            name="path_present",
            path_present=True,
        )
        cls.outputs["unknown"] = cls._install(
            cls.homes["unknown"],
            "/bin/fish",
            name="unknown",
        )
        cls.outputs["no_config"] = cls._install(
            cls.homes["no_config"],
            "/bin/zsh",
            name="no_config",
            flags=("--no-configure-shell",),
        )
        cls.outputs["legacy"] = cls._install(
            cls.homes["legacy"],
            "/bin/zsh",
            name="legacy",
            flags=("--configure-shell",),
        )
        cls.outputs["symlink"] = cls._install(
            cls.homes["symlink"],
            "/bin/zsh",
            name="symlink",
        )
        cls.outputs["unwritable"] = cls._install(
            cls.homes["unwritable"],
            "/bin/zsh",
            name="unwritable",
        )

    @classmethod
    def _install(cls, home, shell, *, name, path_present=False, flags=()):
        path = cls.system_path
        if path_present:
            path = f"{cls.bin_dir}:{cls.system_path}"
        environment = {
            "HOME": str(home),
            "SHELL": shell,
            "PATH": path,
            "PYTHONPATH": "",
            "VIRTUAL_ENV": "",
            "DEEPSEEK_API_KEY": "sk-auto-path-secret",
        }
        completed = run_command(
            (
                "bash",
                str(INSTALL_SCRIPT),
                "--prefix",
                str(cls.app_prefix),
                "--bin-dir",
                str(cls.bin_dir),
                "--no-deps",
                *flags,
            ),
            environment=environment,
            timeout=180,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"install fixture {name} failed: "
                f"{completed.stdout}\n{completed.stderr}"
            )
        return completed.stdout

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def expected_rc(self, home: Path, shell: str) -> Path:
        if shell.endswith("zsh"):
            return home / ".zshrc"
        if shell.endswith("bash"):
            return (
                home / ".bash_profile"
                if platform.system() == "Darwin"
                else home / ".bashrc"
            )
        raise AssertionError(f"unsupported shell fixture: {shell}")

    def assert_block_present(self, rc: Path, *, expected_count: int = 1):
        content = rc.read_text(encoding="utf-8")
        self.assertEqual(content.count("# >>> code-mule >>>"), expected_count)
        self.assertEqual(content.count("# <<< code-mule <<<"), expected_count)
        self.assertIn(f'export PATH="{self.bin_dir}:$PATH"', content)
        return content

    def test_default_install_configures_zsh_rc(self):
        output = self.outputs["zsh"]
        self.assertIn("Shell PATH configured:", output)
        self.assertIn("Code Mule installed successfully.", output)
        self.assertIn("Open a new terminal and run:", output)
        self.assert_block_present(self.expected_rc(self.homes["zsh"], "/bin/zsh"))

    def test_default_install_configures_bash_rc(self):
        output = self.outputs["bash"]
        self.assertIn("Shell PATH configured:", output)
        rc = self.expected_rc(self.homes["bash"], "/bin/bash")
        self.assert_block_present(rc)

    def test_path_already_present_does_not_modify_rc(self):
        output = self.outputs["path_present"]
        self.assertIn("PATH already configured.", output)
        rc = self.expected_rc(self.homes["path_present"], "/bin/zsh")
        self.assertFalse(rc.exists())

    def test_second_install_does_not_duplicate_block_or_backup(self):
        second = self.outputs["existing_second"]
        self.assertIn("PATH already configured in shell rc:", second)
        rc = self.expected_rc(self.homes["existing"], "/bin/zsh")
        self.assert_block_present(rc)
        backup = rc.with_name(rc.name + ".code-mule.bak")
        self.assertTrue(backup.exists())
        self.assertEqual(
            list(self.homes["existing"].glob("*.code-mule.bak")),
            [backup],
        )

    def test_existing_user_content_is_preserved_except_appended_block(self):
        original = (
            "# existing user configuration\n"
            "alias ll='ls -la'\n"
            "export EDITOR=code\n"
        )
        rc = self.expected_rc(self.homes["existing"], "/bin/zsh")
        content = rc.read_text(encoding="utf-8")
        self.assertTrue(content.startswith(original))
        remainder = content[len(original):]
        self.assertIn("# >>> code-mule >>>", remainder)
        self.assertIn("# <<< code-mule <<<", remainder)
        self.assertNotIn("existing user configuration", remainder)

    def test_backup_is_created_before_modifying_existing_rc(self):
        rc = self.expected_rc(self.homes["existing"], "/bin/zsh")
        backup = rc.with_name(rc.name + ".code-mule.bak")
        original = (
            "# existing user configuration\n"
            "alias ll='ls -la'\n"
            "export EDITOR=code\n"
        )
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        self.assertIn("Backup:", self.outputs["existing"])
        self.assertNotIn("Backup:", self.outputs["existing_second"])

    def test_no_configure_shell_does_not_modify_rc(self):
        output = self.outputs["no_config"]
        self.assertIn("Automatic PATH configuration skipped", output)
        rc = self.expected_rc(self.homes["no_config"], "/bin/zsh")
        self.assertFalse(rc.exists())

    def test_legacy_configure_shell_matches_default(self):
        output = self.outputs["legacy"]
        self.assertIn("Shell PATH configured:", output)
        self.assert_block_present(
            self.expected_rc(self.homes["legacy"], "/bin/zsh")
        )

    def test_unknown_shell_installs_with_manual_fallback(self):
        output = self.outputs["unknown"]
        self.assertIn("Code Mule installed successfully.", output)
        self.assertIn("Automatic PATH configuration could not be completed", output)
        self.assertIn("export PATH=", output)
        rc = self.homes["unknown"] / ".zshrc"
        self.assertFalse(rc.exists())
        self.assertFalse((self.homes["unknown"] / ".bashrc").exists())
        self.assertFalse((self.homes["unknown"] / ".bash_profile").exists())

    def test_unwritable_rc_fails_closed_but_keeps_installation(self):
        output = self.outputs["unwritable"]
        self.assertIn("Code Mule installed successfully.", output)
        self.assertIn("Automatic PATH configuration could not be completed", output)
        rc = self.homes["unwritable"] / ".zshrc"
        self.assertEqual(rc.read_text(encoding="utf-8"), "read only\n")

    def test_symlink_rc_is_not_followed_or_overwritten(self):
        output = self.outputs["symlink"]
        self.assertIn("symlink or special file", output)
        self.assertIn("Code Mule installed successfully.", output)
        target = self.homes["symlink"] / "real-rc.txt"
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")

    def test_equivalent_existing_path_exports_are_not_duplicated(self):
        variant_builders = (
            lambda home: 'export PATH="$HOME/.local/bin:$PATH"\n',
            lambda home: 'export PATH="~/.local/bin:$PATH"\n',
            lambda home: f'export PATH="{home}/.local/bin:$PATH"\n',
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, builder in enumerate(variant_builders):
                home = root / f"home-{index}"
                home.mkdir()
                export_line = builder(home)
                rc = home / ".zshrc"
                rc.write_text(export_line, encoding="utf-8")
                completed = run_command(
                    ("bash", str(INSTALL_SCRIPT), "--no-deps"),
                    environment={
                        "HOME": str(home),
                        "SHELL": "/bin/zsh",
                        "PATH": self.system_path,
                        "PYTHONPATH": "",
                        "VIRTUAL_ENV": "",
                    },
                    timeout=180,
                )
                with self.subTest(export=export_line.strip()):
                    self.assertEqual(
                        completed.returncode,
                        0,
                        completed.stdout + completed.stderr,
                    )
                    self.assertEqual(
                        rc.read_text(encoding="utf-8"),
                        export_line,
                    )
                    self.assertIn(
                        "PATH already configured in shell rc:",
                        completed.stdout,
                    )
                    self.assertFalse(list(home.glob("*.code-mule.bak")))

    def test_installer_never_sources_or_evals_user_rc(self):
        script = INSTALL_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("source $shell_rc", script)
        self.assertNotIn("source \"$shell_rc\"", script)
        self.assertNotIn("eval ", script)
        self.assertNotIn("sudo ", script)

    def test_no_secret_output_and_launcher_verification_passes(self):
        for name, output in self.outputs.items():
            self.assertNotIn("sk-auto-path-secret", output, name)
        self.assertIn("Launcher verification passed:", self.outputs["zsh"])

    def test_application_environment_and_launcher_survive_all_config_runs(self):
        launcher = self.bin_dir / "code-mule"
        self.assertTrue(launcher.is_symlink())
        self.assertTrue(
            launcher.resolve().is_relative_to(self.app_prefix.resolve())
        )
        marker = self.app_prefix / "CODE_MULE_INSTALL"
        self.assertIn("status: ready", marker.read_text(encoding="utf-8"))
        completed = run_command(
            (str(launcher), "--help"),
            environment={
                "HOME": str(self.homes["zsh"]),
                "PATH": f"{self.bin_dir}:{self.system_path}",
                "PYTHONPATH": "",
                "VIRTUAL_ENV": "",
            },
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("usage: code-mule", completed.stdout)

    def test_default_install_configures_home_paths_and_restart_shell(self):
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = self.system_path
            environment = {
                "HOME": str(home),
                "SHELL": "/bin/zsh",
                "PATH": path,
                "PYTHONPATH": "",
                "VIRTUAL_ENV": "",
            }
            completed = run_command(
                ("bash", str(INSTALL_SCRIPT), "--no-deps"),
                environment=environment,
                timeout=180,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            rc = home / ".zshrc"
            self.assertTrue(rc.exists())
            content = rc.read_text(encoding="utf-8")
            self.assertIn("# >>> code-mule >>>", content)
            self.assertIn(
                'export PATH="$HOME/.local/bin:$PATH"',
                content,
            )
            launcher = home / ".local" / "bin" / "code-mule"
            self.assertTrue(launcher.exists())

            original_parts = os.environ.get("PATH", "").split(":")
            filtered = [
                part
                for part in original_parts
                if part
                and not part.endswith(".venv/bin")
                and not part.endswith(".venv/Scripts")
            ]
            new_shell_path = (
                f"{home}/.local/bin:"
                + ":".join(dict.fromkeys([*filtered, path]))
            )
            new_shell = {
                "HOME": str(home),
                "SHELL": "/bin/zsh",
                "PATH": new_shell_path,
                "PYTHONPATH": "",
                "VIRTUAL_ENV": "",
            }
            found = run_command(
                ("bash", "-c", "command -v code-mule"),
                environment=new_shell,
            )
            self.assertEqual(found.returncode, 0, found.stdout + found.stderr)
            self.assertEqual(found.stdout.strip(), str(launcher))
            help_run = run_command(
                ("bash", "-c", "code-mule --help"),
                environment=new_shell,
            )
            self.assertEqual(help_run.returncode, 0, help_run.stderr)
            self.assertIn("usage: code-mule", help_run.stdout)
            self.assertNotIn(".venv/bin", new_shell["PATH"])


if __name__ == "__main__":
    unittest.main()
