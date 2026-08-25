"""FilmDownloader.

Layers, outermost first:

- ``cli`` and ``server`` are the two front ends.
- ``jobs`` holds the persistent queue and worker pool both front ends drive.
- ``engines`` resolves a page URL into media: yt-dlp, then site plugins, then
  browser capture.
- ``sites`` holds everything site-specific: hostnames, page markup, media quirks.
- ``core`` is the source-neutral pipeline. It knows no site and imports neither
  Selenium nor yt-dlp at module scope.
"""

__all__ = ["cli", "core", "engines", "jobs", "sites"]
