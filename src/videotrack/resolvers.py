"""Deprecated import path. Use `videotrack.core.resolvers` instead.

Kept so out-of-package scripts keep working during the core refactor; removed
once every caller has moved.
"""

from .core.resolvers import *  # noqa: F401,F403
from .core import resolvers as _module

__all__ = getattr(_module, "__all__", [name for name in dir(_module) if not name.startswith("_")])
