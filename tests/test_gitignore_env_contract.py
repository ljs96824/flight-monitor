"""Exercise the actual ignore rules using only synthetic, disposable Git repos."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FILE_PATHS = ("env", "nested/env", ".env", ".env.txt")
DIRECTORY_PATHS = (
    "env/", "env/canary.txt", "nested/env/", "nested/env/canary.txt",
)
CONTROL_PATHS = (
    ".env.example", "env.example", "environment.py", "config/env_schema.py",
    "venv/lib", "envs",
)


class GitignoreEnvContractTest(unittest.TestCase):
    def setUp(self):
        self.rules = (PROJECT_ROOT / ".gitignore").read_bytes()
        # patch.dict returns a mapping; copy it without retaining process changes.
        with mock.patch.dict("os.environ") as inherited:
            self.git_env = dict(inherited)
        # Includes GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE and GIT_CEILING_DIRECTORIES.
        for name in tuple(self.git_env):
            if name.upper().startswith("GIT_"):
                self.git_env.pop(name)

    def _git(self, repo, env, *args):
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, capture_output=True,
            text=True, encoding="utf-8", timeout=20,
        )

    def _run_ok(self, repo, env, *args):
        result = self._git(repo, env, *args)
        if result.returncode != 0:
            raise RuntimeError(
                f"GIT_EXECUTION_ERROR args={args!r} exit={result.returncode}: {result.stderr}"
            )
        return result

    def _make_repo(self, layout="files", rules=None):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        repo = root / layout
        repo.mkdir()
        env = self.git_env.copy()
        for kind in ("GLOBAL", "SYSTEM"):
            empty_config = root / (kind.lower() + "-config")
            empty_config.write_bytes(b"")
            env["GIT_CONFIG_" + kind] = str(empty_config)
        excludes = root / "global-excludes"
        excludes.write_bytes(b"")
        self._run_ok(repo, env, "init", "-b", "main")
        (repo / ".git" / "info" / "exclude").write_bytes(b"")
        self._run_ok(repo, env, "config", "core.excludesFile", str(excludes))
        self._run_ok(repo, env, "config", "core.autocrlf", "false")
        (repo / ".gitignore").write_bytes(self.rules if rules is None else rules)
        paths = FILE_PATHS if layout == "files" else DIRECTORY_PATHS + (".env", ".env.txt")
        for name in paths + CONTROL_PATHS:
            path = repo / name
            if name.endswith("/"):
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("SYNTHETIC_GITIGNORE_CONTRACT\n", encoding="utf-8")
        return repo, env

    def _assert_rule(self, repo, env, path, expected):
        result = self._git(repo, env, "check-ignore", "--no-index", "-q", "--", path)
        if result.returncode not in (0, 1):
            raise RuntimeError(
                f"GIT_EXECUTION_ERROR path={path!r} exit={result.returncode}: {result.stderr}"
            )
        self.assertEqual(
            result.returncode, expected,
            f"IGNORE_CONTRACT path={path!r} expected_exit={expected} actual_exit={result.returncode}",
        )

    def _assert_matrix(self, repo, env, protected):
        for path, expected in (
            *((p, 0) for p in protected), *((p, 1) for p in CONTROL_PATHS),
        ):
            with self.subTest(path=path):
                self._assert_rule(repo, env, path, expected)

    def _assert_expected_mutation_failures(self, repo, env, cases, expected):
        failures = {}
        for path, wanted in cases:
            try:
                self._assert_rule(repo, env, path, wanted)
            except AssertionError as error:
                failures[path] = str(error)
        self.assertEqual(set(failures), set(expected))
        for path, reason in expected.items():
            self.assertIn(reason, failures[path])

    def test_file_rule_matrix(self):
        repo, env = self._make_repo()
        self._assert_matrix(repo, env, FILE_PATHS)

    def test_directory_rule_matrix(self):
        repo, env = self._make_repo("directories")
        self._assert_matrix(repo, env, DIRECTORY_PATHS + (".env", ".env.txt"))

    def test_example_rule_before_and_after_staging(self):
        repo, env = self._make_repo()
        self._assert_rule(repo, env, ".env.example", 1)
        self._run_ok(repo, env, "add", "--", ".env.example")
        self._run_ok(repo, env, "ls-files", "--error-unmatch", "--", ".env.example")
        self._assert_rule(repo, env, ".env.example", 1)

    def test_example_is_tracked_in_real_repository(self):
        if not (PROJECT_ROOT / ".git").exists():
            self.skipTest("Real .git metadata is absent; temporary rule-layer tests still run")
        _, env = self._make_repo()
        self._run_ok(PROJECT_ROOT, env, "ls-files", "--error-unmatch", "--", ".env.example")

    def test_removed_env_rule_mutation_is_rejected(self):
        mutated = b"".join(line for line in self.rules.splitlines(keepends=True) if line.strip() != b"env")
        repo, env = self._make_repo(rules=mutated)
        cases = tuple((p, 0) for p in FILE_PATHS) + tuple((p, 1) for p in CONTROL_PATHS)
        self._assert_expected_mutation_failures(repo, env, cases, {
            "env": "IGNORE_CONTRACT path='env' expected_exit=0 actual_exit=1",
            "nested/env": "IGNORE_CONTRACT path='nested/env' expected_exit=0 actual_exit=1",
        })

    def test_broad_dotenv_rule_mutation_is_rejected(self):
        repo, env = self._make_repo()
        self._run_ok(repo, env, "add", "--", ".env.example")
        self._run_ok(repo, env, "ls-files", "--error-unmatch", "--", ".env.example")
        (repo / ".gitignore").write_bytes(self.rules + b"\n.env*\n")
        self._assert_expected_mutation_failures(repo, env, ((".env.example", 1),), {
            ".env.example": "IGNORE_CONTRACT path='.env.example' expected_exit=1 actual_exit=0",
        })

    def test_plain_add_excludes_sensitive_paths(self):
        for layout in ("files", "directories"):
            with self.subTest(layout=layout):
                repo, env = self._make_repo(layout)
                self._run_ok(repo, env, "add", ".")
                result = self._run_ok(repo, env, "ls-files", "-z")
                actual = {path for path in result.stdout.split("\0") if path}
                expected = {".gitignore", *CONTROL_PATHS}
                self.assertEqual(
                    actual, expected,
                    f"STAGING_CONTRACT layout={layout} unexpected={sorted(actual - expected)!r} "
                    f"missing={sorted(expected - actual)!r}",
                )

    def test_git_execution_error_is_not_an_ignore_result(self):
        failed = subprocess.CompletedProcess(["git"], 128, "", "fatal: synthetic error")
        with mock.patch.object(self, "_git", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "GIT_EXECUTION_ERROR.*exit=128"):
                self._assert_rule(Path("unused"), {}, "env", 0)


if __name__ == "__main__":
    unittest.main()
