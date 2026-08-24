"""Deprecated import path. Use `videotrack.core.detect` instead.

Kept so out-of-package scripts keep working during the core refactor; removed
once every caller has moved.
"""

from .core.detect import *  # noqa: F401,F403
from .core import detect as _module

__all__ = getattr(_module, "__all__", [name for name in dir(_module) if not name.startswith("_")])
