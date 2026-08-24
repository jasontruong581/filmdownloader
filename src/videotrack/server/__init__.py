"""HTTP surface for the downloader.

A single-operator local server. It binds loopback by default; a wider bind
requires a token and is refused without one, because the API resolves
operator-supplied URLs server-side and writes files to disk.
"""
