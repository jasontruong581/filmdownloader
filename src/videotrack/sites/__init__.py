"""Site plugins and the registry that routes work to them.

Everything site-specific lives here: hostnames, page markup conventions, media
naming quirks, crawl rules. `videotrack.core` stays neutral and asks this
registry rather than knowing any of it.

`handles(url)` is deliberately a **cheap URL-only prefilter**. It must not fetch
anything. Deciding whether a page's markup is recognized belongs inside
`resolver().resolve()`, which returns None when it is not: that check already
needs the page body, and doing it in `handles()` would cost one extra GET per
plugin on every resolve.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from ..core.models import (
        BatchProbe,
        CaptureResult,
        CrawlPreset,
        PageMetadata,
        StreamCandidate,
    )
    from ..core.resolvers import Resolver


@runtime_checkable
class SitePlugin(Protocol):
    """What a site plugin may contribute. Every hook past `name` is optional."""

    name: str

    def handles(self, url: str) -> bool:
        """Cheap URL-only prefilter. Must not perform I/O."""

    def resolver(self) -> Resolver | None:
        """A static resolver for this site, or None."""

    def claims_kind(self, kind: str) -> bool:
        """True when this plugin owns postprocessing for a candidate kind."""

    def metadata(self, capture: CaptureResult, page_html: str | None = None) -> PageMetadata | None:
        """Descriptive fields for naming, or None when unavailable."""

    def postprocess(
        self,
        capture: CaptureResult,
        candidate: StreamCandidate,
        out_file: Path,
    ) -> Path | None:
        """Convert a site-specific asset into playable output, or None."""

    def output_base(
        self,
        candidate: StreamCandidate,
        capture: CaptureResult,
        base: str | None,
    ) -> str | None:
        """Refine the output filename stem, or None to keep the core default."""

    def write_sidecar(self, out_file: Path, metadata: PageMetadata) -> None:
        """Write a descriptive sidecar next to the output, if the site has one."""

    def crawl_preset(self) -> CrawlPreset | None:
        """Link-discovery rules for this host, or None."""

    def probe_batch(self, url: str) -> BatchProbe | None:
        """Cheaply enumerate multiple items on this page, or None.

        Must stay bounded: a single request. Returning items proves enumeration,
        not that each item can be downloaded.
        """


class BaseSitePlugin:
    """Convenience base so a plugin only implements the hooks it needs."""

    name = "base"

    def handles(self, url: str) -> bool:
        return False

    def resolver(self) -> Resolver | None:
        return None

    def claims_kind(self, kind: str) -> bool:
        return False

    def metadata(self, capture: CaptureResult, page_html: str | None = None) -> PageMetadata | None:
        return None

    def postprocess(
        self,
        capture: CaptureResult,
        candidate: StreamCandidate,
        out_file: Path,
    ) -> Path | None:
        return None

    def output_base(
        self,
        candidate: StreamCandidate,
        capture: CaptureResult,
        base: str | None,
    ) -> str | None:
        return None

    def write_sidecar(self, out_file: Path, metadata: PageMetadata) -> None:
        return None

    def crawl_preset(self) -> CrawlPreset | None:
        return None

    def probe_batch(self, url: str) -> BatchProbe | None:
        return None


_REGISTRY: list[SitePlugin] = []


def register(plugin: SitePlugin) -> SitePlugin:
    """Register a plugin, replacing any earlier one with the same name."""
    for index, existing in enumerate(_REGISTRY):
        if existing.name == plugin.name:
            _REGISTRY[index] = plugin
            return plugin
    _REGISTRY.append(plugin)
    return plugin


def unregister(name: str) -> None:
    """Remove a plugin by name. Primarily for tests."""
    _REGISTRY[:] = [plugin for plugin in _REGISTRY if plugin.name != name]


def registered() -> tuple[SitePlugin, ...]:
    return tuple(_REGISTRY)


def plugin_names() -> tuple[str, ...]:
    return tuple(plugin.name for plugin in _REGISTRY)


def plugin_for(url: str) -> SitePlugin | None:
    """First plugin whose URL prefilter accepts this URL."""
    for plugin in _REGISTRY:
        try:
            if plugin.handles(url):
                return plugin
        except Exception:  # noqa: BLE001 - a broken plugin must not break resolution
            continue
    return None


def plugin_for_kind(kind: str) -> SitePlugin | None:
    """Plugin that owns postprocessing for a candidate kind."""
    for plugin in _REGISTRY:
        try:
            if plugin.claims_kind(kind):
                return plugin
        except Exception:  # noqa: BLE001
            continue
    return None


def iter_resolvers() -> tuple[Resolver, ...]:
    """Every registered plugin's resolver, in registration order."""
    resolvers = []
    for plugin in _REGISTRY:
        try:
            resolver = plugin.resolver()
        except Exception:  # noqa: BLE001
            continue
        if resolver is not None:
            resolvers.append(resolver)
    return tuple(resolvers)


def crawl_preset_for(url: str) -> CrawlPreset | None:
    """Crawl rules contributed by the plugin that claims this URL."""
    plugin = plugin_for(url)
    if plugin is None:
        return None
    try:
        return plugin.crawl_preset()
    except Exception:  # noqa: BLE001
        return None


def crawl_preset_names() -> tuple[str, ...]:
    """Plugins that actually contribute crawl rules.

    Not every plugin does, and offering a name the lookup then rejects would be
    a broken command-line choice.
    """
    names = []
    for plugin in _REGISTRY:
        try:
            if plugin.crawl_preset() is not None:
                names.append(plugin.name)
        except Exception:  # noqa: BLE001
            continue
    return tuple(names)


def crawl_preset_named(name: str) -> CrawlPreset | None:
    for plugin in _REGISTRY:
        if plugin.name != name:
            continue
        try:
            return plugin.crawl_preset()
        except Exception:  # noqa: BLE001
            return None
    return None


def _load_builtin_plugins() -> None:
    """Import the bundled plugins so importing this package registers them.

    Kept at the bottom so the registry API above is fully defined first: the
    plugin modules import `register` from here.
    """
    # Import order is registration order, which is also the order presets are
    # offered on the command line.
    from . import vlxx, quatvn, flowplayer  # noqa: F401


_load_builtin_plugins()
