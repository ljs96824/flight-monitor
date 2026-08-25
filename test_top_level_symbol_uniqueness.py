import ast
import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts.check_top_level_symbols import (
    duplicate_top_level_symbols,
    scan_repository,
)


ROOT = Path(__file__).resolve().parent

# 仅用于有明确平台条件定义理由的例外，键=(文件路径, 符号名, 原因)。
EXPLICIT_ALLOWLIST: tuple[tuple[str, str, str], ...] = ()

# 基线扫描后确认的历史债务；它们记录在 docs 中，不属于白名单。
KNOWN_TOP_LEVEL_DUPLICATES: set[tuple[str, str]] = set()


class TopLevelSymbolScannerTest(unittest.TestCase):
    def test_scanner_traverses_module_control_flow_but_not_local_scopes(self):
        source = textwrap.dedent(
            """
            if FLAG:
                def shared():
                    def nested():
                        pass
            else:
                class shared:
                    pass

            try:
                async def branch_name():
                    pass
            except ImportError:
                def branch_name():
                    pass

            match value:
                case 1:
                    class matched:
                        pass
                case _:
                    def matched():
                        pass
            """
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.py"
            path.write_text(source, encoding="utf-8")
            duplicates = duplicate_top_level_symbols(path)

        self.assertEqual(set(duplicates), {"shared", "branch_name", "matched"})
        self.assertNotIn("nested", duplicates)
        self.assertEqual(
            [row.kind for row in duplicates["shared"]],
            ["FunctionDef", "ClassDef"],
        )

    def test_repository_has_no_undocumented_duplicate_top_level_symbols(self):
        self.assertEqual(EXPLICIT_ALLOWLIST, ())
        duplicates = set(scan_repository(ROOT))
        allowed = KNOWN_TOP_LEVEL_DUPLICATES | {
            (path, name) for path, name, _reason in EXPLICIT_ALLOWLIST
        }
        self.assertEqual(duplicates, allowed)


if __name__ == "__main__":
    unittest.main()
