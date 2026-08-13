import importlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import web_form


class FormBuildMarkerTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()

    def test_quick_and_full_pages_render_same_process_marker_with_request_port(self):
        quick = self.client.get("/", base_url="http://localhost:5432").get_data(as_text=True)
        full = self.client.get("/settings", base_url="http://localhost:5432").get_data(as_text=True)

        for html in (quick, full):
            self.assertEqual(html.count('data-build-marker="true"'), 1)
            self.assertIn("build ", html)
            self.assertIn(" · 启动 ", html)
            self.assertIn(" · :5432", html)

        quick_marker = quick.split('data-build-marker="true">', 1)[1].split("</footer>", 1)[0]
        full_marker = full.split('data-build-marker="true">', 1)[1].split("</footer>", 1)[0]
        self.assertEqual(quick_marker, full_marker)

    def test_build_id_falls_back_to_version_file_when_git_is_unavailable(self):
        build_info = importlib.import_module("build_info")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "VERSION").write_text("ux-test-version\n", encoding="utf-8")

            def unavailable_git(*_args, **_kwargs):
                raise FileNotFoundError("git unavailable")

            self.assertEqual(
                build_info.resolve_build_id(root, runner=unavailable_git),
                "ux-test-version",
            )

    def test_repository_provides_nonempty_version_fallback(self):
        version_path = Path(__file__).parent / "VERSION"
        self.assertTrue(version_path.is_file())
        self.assertTrue(version_path.read_text(encoding="utf-8").strip())

    def test_marker_uses_fixed_process_start_time(self):
        build_info = importlib.import_module("build_info")
        started = datetime(2026, 8, 14, 20, 15)
        info = build_info.BuildInfo(build_id="43d23c4", started_at=started)
        self.assertEqual(
            info.format_marker(5000),
            "build 43d23c4 · 启动 08-14 20:15 · :5000",
        )


if __name__ == "__main__":
    unittest.main()
