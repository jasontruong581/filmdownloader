"""Source-neutral download pipeline.

Nothing in this package may reference a specific site: no hostnames, no
site-specific HTML or CSS selectors, and no site-specific media conventions.
Those belong in `videotrack.sites`. Nothing here may import Selenium or yt-dlp
at module scope either, so the CLI and the server start without a browser stack.
"""
