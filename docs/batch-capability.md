# Batch capability

Read this before adding a detector.

## The claim being made

A batch probe answers one narrow question: **does this URL enumerate more than
one item, and can the operator see the list before anything is queued?**

It proves **enumeration, not downloadability**. Items were found; whether each
one can actually be fetched is only known when its job runs. Three consequences,
all enforced:

1. Every enumerated item becomes an **independent job**. A batch is a grouping,
   not a transaction, so one failure never fails the rest.
2. No response and no UI string may claim a site "supports batch download". The
   strongest available claim is "found N items"; after a sample verify, "N items,
   first few resolve".
3. A negative answer carries a **specific reason**, rendered verbatim, so the
   operator learns why rather than seeing a generic "unsupported".

## Why not an allowlist

A hardcoded list of batchable sites would be wrong on both sides: stale for sites
that gained a playlist, and confidently wrong for a site whose page happens not
to enumerate this time. The evidence is the enumerated list itself, so that is
what gates the control.

## Tiers

| Tier | Trigger | Capability | Confidence | Gate |
|---|---|---|---|---|
| 0 | Operator pastes N URLs | n/a | n/a | **Always enabled.** Each URL resolves on its own; nothing is inferred about any site. |
| 1 | yt-dlp playlist or channel | `playlist` | `proven` | Enabled once >= 2 items are enumerated and listed. |
| 1 | A site plugin's own collection markup | `collection` | `proven` | Same. |
| 2 | The host has crawl rules and the page has matching child links | `crawl` | `possible` | Explicit confirmation, with a required item bound. |
| - | Nothing enumerated | `none` | `none` | Disabled; the reason is shown. |

Tier 2 is separated deliberately. Links on a page are not media. Treating them as
`proven` would let a mis-click queue a large amount of work that resolves to
nothing.

## Detectors

Run in order of cost and confidence, in `engines/batch.py`. The first that
enumerates wins; reasons from the others are collected for the negative case.

### 1. yt-dlp flat extraction

`extract_info(extract_flat="in_playlist", playlistend=N)`. Flat extraction skips
per-entry format resolution, so this costs a fraction of a real resolve, which is
what makes it cheap enough to run on every paste.

### 2. Site plugin

`SitePlugin.probe_batch(url)`. One request. The flowplayer plugin counts the
`data-item` entries a collection page embeds, reusing the same parser the
`collect` command uses.

### 3. Crawl prefilter

Fetches **exactly one** page and counts links its host's crawl preset would
match. It never invokes the multi-page crawler; a test asserts that with a spy.

## Bounds

Every detector is bounded, and this is a requirement rather than a courtesy: a
probe runs whenever an operator pastes a URL, so an unbounded one would turn the
UI into a crawler.

| Detector | Bound |
|---|---|
| yt-dlp | `playlistend`, default 200 entries |
| Site plugin | one request |
| Crawl prefilter | one page fetch, 200 links max, no recursion |

When a cap is hit, the result is flagged `truncated` so the UI can say more items
exist than are shown. Silent truncation would read as "this is everything".

## Sample verification

`sample_verify(items, n)` fully resolves the first n items. It raises confidence
from "found N items" to "N items, first n resolve" without paying N resolves,
which is the strongest assurance available at a proportionate cost.

## Adding a detector

1. Return a `BatchProbe`. Use `confidence="proven"` only when concrete items were
   enumerated, `"possible"` when something weaker was found, and `"none"` with a
   reason otherwise.
2. Keep it to a single request. If it needs more, it belongs in the crawler, not
   here.
3. Set `truncated` whenever a cap was reached.
4. Write the reason in the operator's terms, not the implementation's:
   "yt-dlp resolved a single video, not a playlist" rather than
   "_type != playlist".
5. Add it to `_detectors()`, which is bound at call time so it stays
   substitutable in tests.
