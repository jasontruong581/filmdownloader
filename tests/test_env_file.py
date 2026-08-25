"""Loading the documented `.env` file.

`.env.example` is what the README tells operators to copy, so something has to
read it. Nothing did: the file was inert and every variable had to be exported by
hand. These pin the two properties that keep the loader safe to call from an
entry point - the real environment wins, and a bad line never stops startup.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from videotrack.core.env import load_env_file, parse_env_text


class ParseEnvTextTests(unittest.TestCase):
    def test_blank_lines_and_comments_are_ignored(self) -> None:
        parsed = parse_env_text("\n# a comment\n\nFILMDOWNLOADER_PORT=9000\n")

        self.assertEqual(parsed, {"FILMDOWNLOADER_PORT": "9000"})

    def test_an_export_prefix_and_loose_spacing_are_accepted(self) -> None:
        parsed = parse_env_text("export FILMDOWNLOADER_PORT=9000\n  FILMDOWNLOADER_HOST = 0.0.0.0  \n")

        self.assertEqual(parsed["FILMDOWNLOADER_PORT"], "9000")
        self.assertEqual(parsed["FILMDOWNLOADER_HOST"], "0.0.0.0")

    def test_surrounding_quotes_are_stripped(self) -> None:
        parsed = parse_env_text('FILMDOWNLOADER_FFMPEG="C:\\ffmpeg\\bin"\n')

        self.assertEqual(parsed["FILMDOWNLOADER_FFMPEG"], "C:\\ffmpeg\\bin")

    def test_a_value_may_contain_an_equals_sign(self) -> None:
        parsed = parse_env_text("FILMDOWNLOADER_TOKEN=a=b=c\n")

        self.assertEqual(parsed["FILMDOWNLOADER_TOKEN"], "a=b=c")

    def test_a_line_without_an_assignment_is_skipped(self) -> None:
        parsed = parse_env_text("this line has no assignment\nFILMDOWNLOADER_PORT=9001\n")

        self.assertEqual(parsed, {"FILMDOWNLOADER_PORT": "9001"})

    def test_an_empty_value_is_kept(self) -> None:
        # Commenting a variable out and blanking it are different intentions.
        self.assertEqual(parse_env_text("FILMDOWNLOADER_TOKEN=\n"), {"FILMDOWNLOADER_TOKEN": ""})


class LoadEnvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / ".env"
        saved = dict(os.environ)

        def restore() -> None:
            os.environ.clear()
            os.environ.update(saved)

        self.addCleanup(restore)

    def test_values_reach_the_environment(self) -> None:
        self.path.write_text("FILMDOWNLOADER_OUTPUT=media\n", encoding="utf-8")

        applied = load_env_file(self.path)

        self.assertEqual(applied, {"FILMDOWNLOADER_OUTPUT": "media"})
        self.assertEqual(os.environ["FILMDOWNLOADER_OUTPUT"], "media")

    def test_the_real_environment_wins(self) -> None:
        # A shell override has to stay authoritative. This is also what keeps a
        # stray file from reaching into a test that pins a variable.
        os.environ["FILMDOWNLOADER_OUTPUT"] = "from-shell"
        self.path.write_text("FILMDOWNLOADER_OUTPUT=from-file\n", encoding="utf-8")

        applied = load_env_file(self.path)

        self.assertEqual(applied, {})
        self.assertEqual(os.environ["FILMDOWNLOADER_OUTPUT"], "from-shell")

    def test_a_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(load_env_file(Path(self._temp.name) / "absent"), {})

    def test_a_directory_in_place_of_the_file_is_not_an_error(self) -> None:
        self.assertEqual(load_env_file(Path(self._temp.name)), {})

    def test_the_committed_example_file_parses(self) -> None:
        # The file operators are told to copy has to survive the parser.
        example = Path(__file__).resolve().parents[1] / ".env.example"
        self.assertTrue(example.exists())

        parsed = parse_env_text(example.read_text(encoding="utf-8"))

        self.assertIn("FILMDOWNLOADER_OUTPUT", parsed)
        self.assertTrue(all(key.startswith("FILMDOWNLOADER_") for key in parsed))
