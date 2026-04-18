from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jitcatch import context  # noqa: E402


class CallersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc_ctx_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_js_require_caller(self) -> None:
        (self.tmp / "a.js").write_text("module.exports = { x: 1 };\n")
        (self.tmp / "b.js").write_text("const a = require('./a');\nconsole.log(a.x);\n")
        callers = context.find_callers(self.tmp, "a.js", "javascript")
        self.assertIn("b.js", callers)

    def test_js_import_from_caller(self) -> None:
        (self.tmp / "a.mjs").write_text("export const x = 1;\n")
        (self.tmp / "b.mjs").write_text("import { x } from './a.mjs';\nconsole.log(x);\n")
        callers = context.find_callers(self.tmp, "a.mjs", "javascript")
        self.assertIn("b.mjs", callers)

    def test_js_skips_node_modules(self) -> None:
        (self.tmp / "a.js").write_text("module.exports = {};\n")
        nm = self.tmp / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("const a = require('../../a');\n")
        callers = context.find_callers(self.tmp, "a.js", "javascript")
        self.assertEqual(callers, [])

    def test_python_caller(self) -> None:
        (self.tmp / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (self.tmp / "use.py").write_text("from calc import add\nadd(1, 2)\n")
        callers = context.find_callers(self.tmp, "calc.py", "python")
        self.assertIn("use.py", callers)

    def test_normalize_relative_fixes_double_dot(self) -> None:
        self.assertEqual(
            context.normalize_relative_import("./app/common/constants.js"),
            "./app/common/constants.js",
        )
        self.assertEqual(
            context.normalize_relative_import("app/common/constants.js"),
            "./app/common/constants.js",
        )
        self.assertEqual(
            context.normalize_relative_import(".//app/common/constants.js"),
            "./app/common/constants.js",
        )


class BundleTest(unittest.TestCase):
    def test_build_bundle_includes_files_and_callers(self) -> None:
        files = [("a.js", "module.exports = { x: 1 };", "@@ -1 +1 @@\n-x:1\n+x:2")]
        callers = [("b.js", "const a = require('./a');")]
        out = context.build_bundle(files, callers, max_bytes=10_000)
        self.assertIn("CHANGED FILES", out)
        self.assertIn("FILE: a.js", out)
        self.assertIn("USAGE CONTEXT", out)
        self.assertIn("CALLER: b.js", out)

    def test_extract_hunk_windows_narrows_large_file(self) -> None:
        src = "\n".join(f"line{i}" for i in range(1, 201))
        diff = "@@ -100,1 +100,1 @@\n-line100\n+lineX"
        windowed = context.extract_hunk_windows(src, diff, context_lines=2)
        # Only a small slice around line 100 should appear
        self.assertIn("line100", windowed)
        self.assertNotIn("line1\n", windowed.split("line100")[0] or "")
        self.assertNotIn("line200", windowed)

    def test_churn_parsing(self) -> None:
        out = "5\t3\tfoo.js\n-\t-\tbinfile\n10\t0\tbar.js\n"
        churn = context.churn_by_file(out)
        self.assertEqual(churn["foo.js"], 8)
        self.assertEqual(churn["bar.js"], 10)
        self.assertEqual(churn.get("binfile"), 0)


if __name__ == "__main__":
    unittest.main()
