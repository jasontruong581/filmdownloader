"""Recognizing a sign-in wall without mistaking ordinary pages for one.

The refusal exists so the API stops reporting success for a page it cannot read
and stops queueing work against a login form. Nothing here gets past a wall.

The false-positive cases matter more than the positive ones. A resolution with no
media is the *normal* shape for a page that serves its stream from an embed, so
these pin that such a page is never refused.
"""

from __future__ import annotations

import unittest

from videotrack.core.models import CaptureResult, NetworkRequest
from videotrack.core.resolvers import ResolvedMedia, Resolution, auth_wall_reason


def _resolution(final_url: str, *, media: tuple[ResolvedMedia, ...] = ()) -> Resolution:
    return Resolution(
        resolver="browser",
        page_url="https://site.example.test/watch/jb17",
        final_url=final_url,
        title="Sign in",
        media=media,
        engine="browser",
    )


def _media() -> tuple[ResolvedMedia, ...]:
    return (
        ResolvedMedia(
            "https://cdn.example.test/hls/master.m3u8",
            "https://site.example.test/watch/jb17",
            "hls",
        ),
    )


REQUESTED = "https://site.example.test/watch/jb17"


class RecognizedWallTests(unittest.TestCase):
    def test_a_redirect_to_a_login_path_carrying_a_return_parameter(self) -> None:
        reason = auth_wall_reason(
            REQUESTED,
            _resolution("https://site.example.test/auth/login?currentUrl=%2Fwatch%2Fjb17"),
        )

        self.assertIsNotNone(reason)
        self.assertIn("sign-in page", reason)
        self.assertIn("return here afterwards", reason)

    def test_a_redirect_to_a_login_path_without_a_return_parameter(self) -> None:
        reason = auth_wall_reason(REQUESTED, _resolution("https://site.example.test/login"))

        self.assertIsNotNone(reason)
        self.assertNotIn("return here afterwards", reason)

    def test_other_spellings_of_the_same_wall(self) -> None:
        for path in ("/signin", "/sign-in", "/account/register", "/oauth/authorize", "/users/signup"):
            with self.subTest(path=path):
                reason = auth_wall_reason(
                    REQUESTED, _resolution(f"https://site.example.test{path}")
                )
                self.assertIsNotNone(reason, path)

    def test_a_wall_on_another_host_still_counts(self) -> None:
        # Federated sign-in sends the visitor somewhere else entirely.
        reason = auth_wall_reason(
            REQUESTED, _resolution("https://accounts.example.test/oauth?next=%2Fwatch%2Fjb17")
        )

        self.assertIsNotNone(reason)


class NotAWallTests(unittest.TestCase):
    def test_a_page_that_did_not_redirect(self) -> None:
        self.assertIsNone(auth_wall_reason(REQUESTED, _resolution(REQUESTED)))

    def test_a_page_whose_media_lives_in_an_embed(self) -> None:
        # The load-bearing case. The browser engine returns a capture with no
        # direct media so the pipeline can deep-scan the embed; refusing here
        # would break every embed-hosted page.
        embed_capture = CaptureResult(
            page_url=REQUESTED,
            final_url=REQUESTED,
            title="Embedded Clip",
            user_agent="test-agent",
            cookies={},
            requests=[
                NetworkRequest(
                    url="https://frame.example.test/embed/abc",
                    method="GET",
                    headers={},
                    resource_type="Document",
                    status=200,
                )
            ],
        )
        resolution = Resolution(
            resolver="browser",
            page_url=REQUESTED,
            final_url=REQUESTED,
            title="Embedded Clip",
            media=(),
            engine="browser",
            capture=embed_capture,
        )

        self.assertIsNone(auth_wall_reason(REQUESTED, resolution))

    def test_an_auth_word_that_is_only_part_of_a_segment(self) -> None:
        for path in ("/login-guide", "/authors", "/registered-users", "/signage"):
            with self.subTest(path=path):
                self.assertIsNone(
                    auth_wall_reason(REQUESTED, _resolution(f"https://site.example.test{path}")),
                    path,
                )

    def test_a_redirect_that_still_found_media(self) -> None:
        # Whatever the path says, media was found, so this is not a wall.
        self.assertIsNone(
            auth_wall_reason(
                REQUESTED,
                _resolution("https://site.example.test/auth/login", media=_media()),
            )
        )

    def test_a_redirect_that_only_changed_the_query(self) -> None:
        self.assertIsNone(
            auth_wall_reason(REQUESTED, _resolution(f"{REQUESTED}?autoplay=1"))
        )

    def test_a_canonical_redirect_with_no_auth_segment(self) -> None:
        self.assertIsNone(
            auth_wall_reason(REQUESTED, _resolution("https://site.example.test/video/jb17"))
        )

    def test_an_empty_final_url(self) -> None:
        self.assertIsNone(auth_wall_reason(REQUESTED, _resolution("")))

    def test_a_return_parameter_pointing_somewhere_else(self) -> None:
        # Still a wall by path, but the message must not claim a return trip it
        # cannot see.
        reason = auth_wall_reason(
            REQUESTED, _resolution("https://site.example.test/login?next=%2Fhome")
        )

        self.assertIsNotNone(reason)
        self.assertNotIn("return here afterwards", reason)


if __name__ == "__main__":
    unittest.main()
