"""Resolution and download engines.

An engine turns a page URL into concrete media plus the request context needed to
fetch it. The chain tries them in order and each declines cleanly when it does
not recognize a page.

`videotrack.core` must not import this package: core owns the neutral pipeline
and knows nothing about which engine produced its input.
"""

from __future__ import annotations


def ytdlp_version() -> str | None:
    """Installed yt-dlp version, or None when it is not installed.

    Lives here rather than in core preflight so core carries no yt-dlp reference
    at all, not even an optional one.
    """
    try:
        import yt_dlp
    except ImportError:
        return None
    return getattr(getattr(yt_dlp, "version", None), "__version__", None)
