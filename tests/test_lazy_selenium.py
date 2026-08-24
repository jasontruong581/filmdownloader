"""Selenium must stay an optional, lazily-resolved dependency.

Only browser capture needs Selenium. If any module imports it at scope, the CLI,
the resolver chain, and the server all require a browser stack before they can
start, and the offline test suite cannot run. These tests import the package in a
subprocess where `selenium` is unimportable, which is the only honest way to
check it: the parent process has Selenium installed.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest

# Poisoning sys.modules with None makes every `import selenium...` raise
# ImportError, which is what a machine without Selenium installed looks like.
_BLOCK_SELENIUM = """
import sys
for _name in ("selenium", "selenium.webdriver", "selenium.webdriver.chrome.options",
              "selenium.webdriver.common.by", "selenium.webdriver.support",
              "selenium.webdriver.support.ui"):
    sys.modules[_name] = None
"""

MODULES_THAT_MUST_IMPORT = [
    "videotrack.cli",
    "videotrack.capture",
    "videotrack.quatvn",
    "videotrack.detect",
    "videotrack.download",
    "videotrack.resolvers",
    "videotrack.static_player",
    "videotrack.collection",
    "videotrack.crawl",
]


def _run_without_selenium(body: str) -> subprocess.CompletedProcess[str]:
    script = _BLOCK_SELENIUM + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )


class LazySeleniumTests(unittest.TestCase):
    def test_the_blocker_actually_blocks(self) -> None:
        # Guards the test itself: a no-op blocker would make everything below pass.
        result = _run_without_selenium(
            """
            try:
                import selenium
            except ImportError:
                print("blocked")
            """
        )

        self.assertEqual(result.stdout.strip(), "blocked", result.stderr)

    def test_every_module_imports_without_selenium(self) -> None:
        for module in MODULES_THAT_MUST_IMPORT:
            with self.subTest(module=module):
                result = _run_without_selenium(
                    f"""
                    import {module}
                    print("ok")
                    """
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "ok")

    def test_selenium_api_raises_a_helpful_runtime_error(self) -> None:
        result = _run_without_selenium(
            """
            from videotrack.capture import selenium_api
            try:
                selenium_api()
            except RuntimeError as exc:
                print(exc)
            """
        )

        self.assertIn("Selenium", result.stdout)
        self.assertIn("pip install selenium", result.stdout)


class SourceWarningTests(unittest.TestCase):
    def test_the_package_compiles_without_syntax_warnings(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-W",
                "error::SyntaxWarning",
                "-c",
                "import compileall, sys; sys.exit(0 if compileall.compile_dir('src', quiet=2, force=True) else 1)",
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
