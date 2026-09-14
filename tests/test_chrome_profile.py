"""Capturing with a session the operator already has.

A page behind a login is captured with a real profile or not at all. yt-dlp
could already borrow one through `--cookies-from-browser`, but browser capture
started a fresh Chrome with no profile, and the cookies a capture collects are
what reach FFmpeg - so the whole download side was locked out of those pages
even when yt-dlp was not.

No Chrome here: the driver is stubbed and the profile is a directory layout.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from videotrack.core import capture as capture_module
from videotrack.core.capture import chrome_profile_arguments
from videotrack.core.options import PipelineOptions


def _user_data_dir(root: Path) -> Path:
    """What Chrome's own directory looks like from the outside."""
    path = root / "User Data"
    (path / "Default").mkdir(parents=True)
    (path / "Local State").write_text("{}", encoding="utf-8")
    (path / "Profile 1").mkdir()
    return path


class ProfileArgumentTests(unittest.TestCase):
    def test_a_user_data_directory_is_passed_as_one(self) -> None:
        with TemporaryDirectory() as temp:
            path = _user_data_dir(Path(temp))

            self.assertEqual(chrome_profile_arguments(str(path)), (f"--user-data-dir={path}",))

    def test_a_profile_inside_it_is_split_into_both_flags(self) -> None:
        # What a person means by "my second profile" is this path, and passing
        # it whole as the user-data directory makes Chrome build a new empty
        # profile inside it rather than opening the one that was named.
        with TemporaryDirectory() as temp:
            root = _user_data_dir(Path(temp))

            self.assertEqual(
                chrome_profile_arguments(str(root / "Profile 1")),
                (f"--user-data-dir={root}", "--profile-directory=Profile 1"),
            )

    def test_a_directory_with_a_default_profile_counts_as_the_root(self) -> None:
        # Some installations have no `Local State` to read.
        with TemporaryDirectory() as temp:
            path = Path(temp) / "chrome"
            (path / "Default").mkdir(parents=True)

            self.assertEqual(chrome_profile_arguments(str(path)), (f"--user-data-dir={path}",))

    def test_a_path_that_does_not_exist_is_read_as_a_profile(self) -> None:
        # Chrome creates what is missing; what must not happen is silently
        # treating a named profile as a whole user-data directory.
        arguments = chrome_profile_arguments(str(Path("nowhere") / "User Data" / "Profile 9"))

        self.assertIn("--profile-directory=Profile 9", arguments)


class _Options:
    def __init__(self) -> None:
        self.arguments: list[str] = []
        self.capabilities: dict = {}

    def add_argument(self, value: str) -> None:
        self.arguments.append(value)

    def set_capability(self, name: str, value) -> None:
        self.capabilities[name] = value


class _Driver:
    def __init__(self, options=None) -> None:
        self.options = options

    def execute_cdp_cmd(self, *args, **kwargs) -> None:
        return None


def _stub_selenium(driver_factory):
    api = capture_module.SeleniumApi(
        webdriver=type("_WebDriver", (), {"Chrome": staticmethod(driver_factory)}),
        Options=_Options,
        By=None,
        EC=None,
        WebDriverWait=None,
    )
    return patch.object(capture_module, "selenium_api", return_value=api)


class DriverTests(unittest.TestCase):
    def _arguments(self, chrome_profile: str | None) -> list[str]:
        captured: list[_Options] = []

        def factory(options=None):
            captured.append(options)
            return _Driver(options)

        with _stub_selenium(factory):
            capture_module._build_driver(headless=True, chrome_profile=chrome_profile)
        return captured[0].arguments

    def test_no_profile_adds_no_profile_flags(self) -> None:
        arguments = self._arguments(None)

        self.assertFalse([item for item in arguments if "user-data-dir" in item])

    def test_a_profile_reaches_chrome(self) -> None:
        with TemporaryDirectory() as temp:
            path = _user_data_dir(Path(temp))

            arguments = self._arguments(str(path))

        self.assertIn(f"--user-data-dir={path}", arguments)

    def test_a_profile_already_open_is_explained_rather_than_dumped(self) -> None:
        # Chrome answers this with a wall of diagnostics that never mentions the
        # one thing the operator has to do about it.
        def factory(options=None):
            raise RuntimeError(
                "session not created: probably user data directory is already in use, "
                "please specify a unique value for --user-data-dir"
            )

        with TemporaryDirectory() as temp:
            path = _user_data_dir(Path(temp))
            with _stub_selenium(factory):
                with self.assertRaises(RuntimeError) as caught:
                    capture_module._build_driver(headless=True, chrome_profile=str(path))

        message = str(caught.exception)
        self.assertIn("already running", message)
        self.assertIn(str(path), message)

    def test_any_other_startup_failure_is_left_alone(self) -> None:
        # Rewriting it would hide what Chrome actually said.
        def factory(options=None):
            raise RuntimeError("chromedriver only supports Chrome version 140")

        with _stub_selenium(factory):
            with self.assertRaises(RuntimeError) as caught:
                capture_module._build_driver(headless=True, chrome_profile=None)

        self.assertIn("only supports Chrome version", str(caught.exception))


class WiringTests(unittest.TestCase):
    """A flag that reaches the parser and not the capture is not a feature."""

    def test_the_pipeline_carries_the_profile(self) -> None:
        options = PipelineOptions.from_args(_Args(chrome_profile="C:/profiles/User Data"))

        self.assertEqual(options.chrome_profile, "C:/profiles/User Data")

    def test_an_empty_flag_means_no_profile(self) -> None:
        self.assertIsNone(PipelineOptions.from_args(_Args(chrome_profile="")).chrome_profile)

    def test_an_absent_flag_means_no_profile(self) -> None:
        # Subcommands expose different subsets of the flags.
        self.assertIsNone(PipelineOptions.from_args(_Args()).chrome_profile)

    def test_the_browser_engine_passes_it_to_the_capture(self) -> None:
        from videotrack.engines.browser_resolver import BrowserOptions, BrowserResolver

        seen: dict = {}

        def fake_capture(**kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here: the call is the assertion")

        resolver = BrowserResolver(BrowserOptions(chrome_profile="C:/profiles/User Data"))
        with patch("videotrack.engines.browser_resolver.capture_page", fake_capture):
            with self.assertRaises(RuntimeError):
                resolver.resolve("https://site.example.test/watch")

        self.assertEqual(seen.get("chrome_profile"), "C:/profiles/User Data")


class _Args:
    def __init__(self, **values) -> None:
        for name, value in values.items():
            setattr(self, name, value)


if __name__ == "__main__":
    unittest.main()
