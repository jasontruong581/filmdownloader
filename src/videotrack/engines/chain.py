"""The resolver chain.

One entry point, one return type: `resolve()` always answers with a list, where
a single video is a list of one. There is deliberately no scalar variant, so no
caller has to decide which to call.

Order matters. yt-dlp first because it is fast, needs no browser, and covers the
most sites. Site plugins next, for pages yt-dlp has no extractor for. Browser
capture last because it is slow, needs Chrome, and is the engine that works
without recognizing anything.

An engine that declines returns an empty list. An engine that raises is logged
and skipped: a broken engine must not stop the chain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from ..core.resolvers import Resolution
from .browser_resolver import BrowserOptions, BrowserResolver
from .ytdlp_resolver import YtDlpOptions, YtDlpResolver

DEFAULT_ENGINE_ORDER: tuple[str, ...] = ("ytdlp", "site", "browser")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChainOptions:
    engines: tuple[str, ...] = DEFAULT_ENGINE_ORDER
    ytdlp: YtDlpOptions = field(default_factory=YtDlpOptions)
    browser: BrowserOptions = field(default_factory=BrowserOptions)


#: Legacy --resolver values mapped onto engine subsets, so existing scripts keep
#: working after the flag was superseded by --engine.
RESOLVER_ALIASES: dict[str, tuple[str, ...]] = {
    "auto": DEFAULT_ENGINE_ORDER,
    "static": ("ytdlp", "site"),
    "browser": ("browser",),
}


def engine_choices() -> tuple[str, ...]:
    return DEFAULT_ENGINE_ORDER


def _resolve_with_ytdlp(url: str, options: ChainOptions) -> list[Resolution]:
    resolver = YtDlpResolver(options.ytdlp)
    if not resolver.available:
        logger.debug("yt-dlp is not installed, skipping that engine")
        return []
    return resolver.resolve_many(url)


def _resolve_with_sites(url: str, options: ChainOptions) -> list[Resolution]:
    from ..sites import iter_resolvers

    for resolver in iter_resolvers():
        name = getattr(resolver, "name", type(resolver).__name__)
        try:
            resolve_many = getattr(resolver, "resolve_many", None)
            results = resolve_many(url) if callable(resolve_many) else None
            if results is None:
                single = resolver.resolve(url)
                results = [single] if single is not None else []
        except Exception as exc:  # noqa: BLE001
            logger.debug("site resolver %s failed on %s: %s", name, url, exc)
            continue
        results = [item for item in results if item is not None]
        if results:
            return [replace(item, engine="site") for item in results]
    return []


def _resolve_with_browser(url: str, options: ChainOptions) -> list[Resolution]:
    return BrowserResolver(options.browser).resolve_many(url)


def _runner_for(engine_name: str):
    """Resolve an engine name to its runner at call time.

    Looked up per call rather than held in a module-level table so the runners
    stay substitutable: a table built at import time captures the original
    function objects, which makes the browser engine impossible to stub and lets
    an offline test launch a real Chrome.
    """
    return {
        "ytdlp": _resolve_with_ytdlp,
        "site": _resolve_with_sites,
        "browser": _resolve_with_browser,
    }.get(engine_name)


def resolve_by_engine(url: str, options: ChainOptions | None = None):
    """Yield (engine_name, resolutions) for each engine that produced something.

    Lazy on purpose. `resolve()` answers "what is this URL", but a caller that
    also *downloads* needs to fall through to the next engine when the download
    fails, not only when resolution fails. Iterating keeps the expensive engines
    unrun until they are actually needed.
    """
    options = options or ChainOptions()

    for engine_name in options.engines:
        runner = _runner_for(engine_name)
        if runner is None:
            logger.warning("unknown engine %r, skipping", engine_name)
            continue
        try:
            results = runner(url, options)
        except Exception as exc:  # noqa: BLE001
            # A broken engine still must not stop the chain, but it must not be
            # invisible either: at debug level this was indistinguishable from
            # "the page is not supported", which is a different problem with a
            # different fix.
            logger.warning(
                "engine %s failed on %s: %s: %s",
                engine_name,
                url,
                type(exc).__name__,
                exc,
            )
            continue
        if results:
            yield engine_name, results


def resolve(url: str, options: ChainOptions | None = None) -> list[Resolution]:
    """Run the engine order until one produces something."""
    for engine_name, results in resolve_by_engine(url, options):
        logger.debug("engine %s resolved %s into %d item(s)", engine_name, url, len(results))
        return results
    return []
