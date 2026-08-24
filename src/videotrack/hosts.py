"""Operator-tunable host preferences.

These are shipped defaults, not core policy: `videotrack.core.detect` takes the
bonuses as an argument and knows no hostnames of its own. Kept as a module for
now; the settings surface takes ownership once the server exists.
"""

from __future__ import annotations

# (host regex, score bonus). Applied to a candidate's hostname.
# Media served through a known-good CDN outranks an equally-scored unknown host.
DEFAULT_HOST_BONUSES: tuple[tuple[str, int], ...] = (
    (r"tiktokcdn\.com$", 18),
    (r"bytecdn", 12),
)
