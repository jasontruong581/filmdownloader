"""Batch capability probing.

The question this answers is narrow: does one URL enumerate more than one item,
and can we show that list to the operator before anything is queued?

Three detectors, tried in order of both cost and confidence:

1. yt-dlp flat extraction. Playlists and channels. `proven`.
2. Site plugin. A collection page's own markup. One request. `proven`.
3. Crawl prefilter. Exactly one page fetched, matching child links counted.
   `possible`, because links on a page are not confirmed media.

Every detector is bounded. The multi-page crawler is never invoked from here.

A probe proves **enumeration, not downloadability**. Nothing in this module may
report that a site "supports batch download": the strongest available claim is
that N items were found.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

from ..core.models import BatchItem, BatchProbe
from ..core.resolvers import DEFAULT_USER_AGENT
from .ytdlp_resolver import YtDlpOptions, YtDlpResolver

#: Hard cap on links a single-page crawl prefilter will report.
CRAWL_PREFILTER_LIMIT = 200

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchOptions:
    ytdlp: YtDlpOptions = field(default_factory=YtDlpOptions)
    timeout: int = 20
    crawl_limit: int = CRAWL_PREFILTER_LIMIT
    user_agent: str = DEFAULT_USER_AGENT


def _probe_ytdlp(url: str, options: BatchOptions) -> BatchProbe:
    resolver = YtDlpResolver(options.ytdlp)
    if not resolver.available:
        return BatchProbe(reason="yt-dlp is not installed")
    return resolver.probe_batch(url)


def _probe_site_plugin(url: str, options: BatchOptions) -> BatchProbe:
    from ..sites import registered

    for plugin in registered():
        try:
            result = plugin.probe_batch(url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("plugin %s batch probe failed on %s: %s", plugin.name, url, exc)
            continue
        if result is not None and result.confidence != "none":
            return result
    return BatchProbe(reason="no site plugin enumerated entries on this page")


def _probe_crawl_prefilter(url: str, options: BatchOptions) -> BatchProbe:
    """Fetch one page and count links its host's crawl preset would match.

    Deliberately not a crawl: a single request, no queue, no recursion.
    """
    from ..crawl import _extract_links, _matches_target_link, _normalize_url
    from ..sites import crawl_preset_for

    normalized = _normalize_url(url, url)
    if not normalized:
        return BatchProbe(reason="not a usable http(s) URL")

    preset = crawl_preset_for(normalized)
    if preset is None:
        return BatchProbe(reason="no site plugin claims this host, so there are no crawl rules")

    try:
        response = requests.get(
            normalized,
            headers={"User-Agent": options.user_agent},
            timeout=options.timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.debug("crawl prefilter request failed for %s: %s", normalized, exc)
        return BatchProbe(reason="the page could not be fetched")

    if "text/html" not in (response.headers.get("Content-Type") or "").lower():
        return BatchProbe(reason="the URL did not return HTML")

    from urllib.parse import urlparse

    host = urlparse(normalized).netloc.lower()
    seen: dict[str, None] = {}
    for link in _extract_links(response.text or "", normalized):
        if link == normalized or link in seen:
            continue
        if _matches_target_link(
            link,
            host,
            preset.include_substring,
            preset.exclude_substrings,
            preset.url_filter,
        ):
            seen[link] = None
        if len(seen) >= options.crawl_limit:
            break

    links = list(seen)
    if len(links) < 2:
        return BatchProbe(reason="this page lists fewer than two matching child links")

    return BatchProbe(
        capability="crawl",
        confidence="possible",
        items=tuple(BatchItem(url=link) for link in links),
        total_estimate=len(links),
        truncated=len(links) >= options.crawl_limit,
        reason="",
    )


def _detectors() -> tuple[tuple[str, object], ...]:
    """Detectors in cost-and-confidence order, bound at call time.

    A module-level table would capture the original functions at import, making
    the network-touching detectors impossible to substitute.
    """
    return (
        ("playlist", _probe_ytdlp),
        ("collection", _probe_site_plugin),
        ("crawl", _probe_crawl_prefilter),
    )


def probe(url: str, options: BatchOptions | None = None) -> BatchProbe:
    """Ask whether this URL enumerates several items.

    Detectors run once each. When none enumerates, their individual reasons are
    joined, so the UI can say why the batch control stays disabled rather than
    showing a generic "unsupported".
    """
    options = options or BatchOptions()
    reasons: list[str] = []

    for name, detector in _detectors():
        try:
            result = detector(url, options)
        except Exception as exc:  # noqa: BLE001 - a failing detector is a decline
            logger.debug("batch detector %s raised on %s: %s", name, url, exc)
            continue
        if result.is_batchable:
            return result
        if result.reason:
            reasons.append(result.reason)

    return BatchProbe(reason="; ".join(dict.fromkeys(reasons)) or "nothing enumerable was found at this URL")


def sample_verify(items: tuple[BatchItem, ...], count: int = 2) -> tuple[int, int]:
    """Fully resolve the first `count` items.

    Raises confidence from "found N items" to "N items, first few resolve"
    without paying N resolves. Returns (verified, attempted).
    """
    from .chain import resolve as chain_resolve

    attempted = 0
    verified = 0
    for item in items[: max(count, 0)]:
        attempted += 1
        try:
            if chain_resolve(item.url):
                verified += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("sample verify failed for %s: %s", item.url, exc)
    return verified, attempted
