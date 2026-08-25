from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .console import print_event
from .core.capture import capture_page
from .sites.flowplayer import download_collection, fetch_collection, parse_flowplayer_collection
from .crawl import (
    crawl_preset_choices,
    crawl_site_links,
    read_csv_urls,
    resolve_crawl_preset,
    save_urls_to_csv,
)
from .core.io import load_capture, save_candidates, save_capture, save_json
from .core.models import CaptureResult, StreamCandidate
from .core.options import PipelineOptions
from .core.pipeline import (
    download_with_fallback,
    prepare_candidates,
    resolve_download_capture,
)
from .core.pipeline import run as pipeline_run
from .core.env import load_env_file
from .core.preflight import ENV_FFMPEG, check_tools, ffmpeg_location, format_report
from .engines import ytdlp_version
from .engines.batch import probe as batch_probe, sample_verify
from .engines.browser_resolver import BrowserOptions
from .engines.chain import (
    DEFAULT_ENGINE_ORDER,
    RESOLVER_ALIASES,
    ChainOptions,
    engine_choices,
)
from .engines.chain import resolve as chain_resolve
from .engines.chain import resolve_by_engine
from .engines.ytdlp_resolver import YtDlpOptions
from .core.resolvers import capture_from_resolution
from .hosts import DEFAULT_HOST_BONUSES
from .jobs.bus import EventBus
from .jobs.manager import DEFAULT_CONCURRENCY, DuplicateJob, JobManager
from .jobs.models import TERMINAL_STATUSES, JobStatus
from .jobs.store import JobStore
from .sites import plugin_names


def _add_shared_capture_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("url", help="Video page URL that you are authorized to analyze/download")
    parser.add_argument("--wait", type=int, default=15, help="Seconds to wait for network traffic")
    parser.add_argument("--headed", action="store_true", help="Run Chrome with UI (default is headless)")


def _add_autonomous_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--autonomous",
        action="store_true",
        help="Force headless + non-interactive mode, keep artifacts/logs in logs/",
    )


def _apply_autonomous_overrides(args: argparse.Namespace) -> None:
    if not getattr(args, "autonomous", False):
        return
    if hasattr(args, "headed"):
        args.headed = False
    if hasattr(args, "interactive_pick"):
        args.interactive_pick = False


def _serialize_candidates(candidates: list[StreamCandidate]) -> list[dict]:
    return [c.to_dict() for c in candidates]


def _interactive_reorder(candidates: list[StreamCandidate]) -> list[StreamCandidate]:
    """Prompt for a candidate choice.

    Stays in the CLI: it reads stdin, and the pipeline must never be able to
    block on a terminal.
    """
    if not candidates:
        return candidates

    print("[i] Interactive pick enabled. Choose candidate index:")
    for idx, c in enumerate(candidates[:20], start=1):
        dur = f"{c.probe_duration:.1f}s" if c.probe_duration else "?"
        br = f"{c.probe_bitrate}" if c.probe_bitrate else "?"
        print(f"{idx:2}. score={c.score:3} kind={c.kind:8} host={c.host or '-'} dur={dur} br={br} url={c.url}")

    try:
        raw = input("Pick index (Enter=default 1): ").strip()
    except EOFError:
        return candidates

    if not raw:
        return candidates

    try:
        selected = int(raw)
    except ValueError:
        print("[!] Invalid input. Keep default order.")
        return candidates

    if selected < 1 or selected > len(candidates):
        print("[!] Out of range. Keep default order.")
        return candidates

    idx = selected - 1
    return [candidates[idx]] + candidates[:idx] + candidates[idx + 1 :]


def _pipeline_options(args: argparse.Namespace) -> PipelineOptions:
    return PipelineOptions.from_args(args, host_bonuses=DEFAULT_HOST_BONUSES)


def _reorder_for(args: argparse.Namespace):
    return _interactive_reorder if getattr(args, "interactive_pick", False) else None


def _dump_candidates(args: argparse.Namespace, stage_counts, all_candidates, candidates) -> None:
    target = getattr(args, "dump_all_candidates", "")
    if not target:
        return
    save_json(
        {
            "stage_counts": stage_counts,
            "all_candidates": _serialize_candidates(all_candidates),
            "final_candidates": _serialize_candidates(candidates),
        },
        Path(target),
    )
    print(f"[+] Saved dump: {target}")


def cmd_analyze(args: argparse.Namespace) -> int:
    _apply_autonomous_overrides(args)
    capture = capture_page(
        url=args.url,
        wait_seconds=args.wait,
        headless=not args.headed,
    )
    save_capture(capture, Path(args.capture_out))
    print(f"[+] Saved capture: {args.capture_out}")
    print(f"[+] Requests captured: {len(capture.requests)}")
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    _apply_autonomous_overrides(args)
    capture = load_capture(Path(args.capture))
    candidates, stage_counts, all_candidates = prepare_candidates(
        capture, _pipeline_options(args), print_event
    )

    save_candidates(candidates, Path(args.candidates_out))
    _dump_candidates(args, stage_counts, all_candidates, candidates)

    print(f"[+] Candidates: {len(candidates)}")
    for idx, candidate in enumerate(candidates[:20], start=1):
        print(
            f"{idx}. kind={candidate.kind:8} score={candidate.score:3} host={candidate.host} "
            f"status={candidate.status_code} url={candidate.url}"
        )
    print(f"[+] Saved candidates: {args.candidates_out}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    _apply_autonomous_overrides(args)
    capture = load_capture(Path(args.capture))
    options = _pipeline_options(args)

    candidates, stage_counts, all_candidates = prepare_candidates(capture, options, print_event)
    _dump_candidates(args, stage_counts, all_candidates, candidates)

    if not candidates:
        print("[!] No stream candidate found.")
        return 2

    download_capture = resolve_download_capture(capture, candidates, options, print_event)
    return download_with_fallback(
        download_capture,
        candidates,
        options,
        metadata_capture=capture,
        on_event=print_event,
        reorder=_reorder_for(args),
    )


def _run_capture_pipeline(args: argparse.Namespace, capture: CaptureResult) -> int:
    save_capture(capture, Path(args.capture_out))
    options = _pipeline_options(args)

    code, candidates, stage_counts, all_candidates = pipeline_run(
        capture, options, print_event, reorder=_reorder_for(args)
    )
    save_candidates(candidates, Path(args.candidates_out))
    _dump_candidates(args, stage_counts, all_candidates, candidates)
    return code


def cmd_run(args: argparse.Namespace) -> int:
    _apply_autonomous_overrides(args)
    options = _chain_options(args)

    attempted = False
    last_result = 2
    for engine_name, resolutions in resolve_by_engine(args.url, options):
        attempted = True
        resolution = resolutions[0]
        if len(resolutions) > 1:
            print(
                f"[i] {engine_name} enumerated {len(resolutions)} items; downloading the first. "
                "Use probe-batch to list them all."
            )
        print(f"[+] Engine {engine_name} resolved: {resolution.title or resolution.final_url}")
        if resolution.formats:
            print(f"[i] {len(resolution.formats)} selectable format(s); best: {resolution.formats[0].label()}")

        capture = capture_from_resolution(resolution)
        last_result = _run_capture_pipeline(args, capture)
        if last_result == 0:
            return 0
        print(f"[i] {engine_name} download path failed; trying the next engine.")

    if not attempted:
        print("[!] No engine resolved media on this page.")
        return 2
    print("[!] Every engine that resolved this page failed to download it.")
    return last_result


def cmd_crawl_links(args: argparse.Namespace) -> int:
    preset = resolve_crawl_preset(args.url, args.site_preset)
    include_substring = preset.include_substring if args.include_substring is None else args.include_substring
    exclude_substrings = [*preset.exclude_substrings, *args.exclude_substring]
    result = crawl_site_links(
        start_url=args.url,
        include_substring=include_substring,
        max_pages=args.max_pages,
        timeout=args.timeout,
        user_agent=args.user_agent,
        exclude_substrings=exclude_substrings,
        url_filter=preset.url_filter,
    )
    save_urls_to_csv(result.matched_urls, Path(args.output_csv))

    print(f"[+] Crawl preset: {preset.name}")
    print(f"[+] Visited pages: {result.visited_pages}")
    print(f"[+] Matched links: {len(result.matched_urls)}")
    print(f"[+] Saved CSV: {args.output_csv}")
    for url in result.matched_urls[:20]:
        print(url)
    if len(result.matched_urls) > 20:
        print(f"... ({len(result.matched_urls) - 20} more)")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    if args.html_file:
        html_text = Path(args.html_file).read_text(encoding="utf-8")
        collection = parse_flowplayer_collection(html_text, args.source_url or "https://example.invalid/collection")
    else:
        collection = fetch_collection(args.url)
    if not collection.videos:
        print("[!] No Flowplayer collection entries found.")
        return 2
    manifest = download_collection(
        collection,
        output_dir=Path(args.output_dir),
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )
    completed = sum(item["status"] in {"downloaded", "skipped_existing", "dry_run"} for item in manifest["videos"])
    print(f"[+] Collection: {collection.title}")
    print(f"[+] Items: {completed}/{manifest['expected_count']}")
    return 0 if completed == manifest["expected_count"] else 3


def cmd_doctor(args: argparse.Namespace) -> int:
    statuses = check_tools()

    print("External tools")
    print(format_report(statuses))

    print("\nOptional Python packages")
    ytdlp = ytdlp_version()
    if ytdlp:
        print(f"[ok  ] yt-dlp       {ytdlp}")
        print("            extractors go stale quickly: pip install -U yt-dlp")
    else:
        print("[--  ] yt-dlp       not installed (optional)")

    print("\nSite plugins")
    print(f"  registered: {', '.join(plugin_names()) or 'none'}")
    print(f"  crawl presets: {', '.join(crawl_preset_choices())}")

    location = ffmpeg_location()
    if location is not None:
        print(f"\nFFmpeg location override: {location}")

    blocking = [status.name for status in statuses if status.blocking]
    if blocking:
        print(f"\n[!] Missing required tool(s): {', '.join(blocking)}")
        print(f"[i] Set {ENV_FFMPEG} to an ffmpeg directory or executable if it is installed elsewhere.")
        return 1

    print("\nAll required tools available.")
    return 0


def _chain_options(args: argparse.Namespace) -> ChainOptions:
    """Build chain options from the flags, honoring the retired --resolver alias."""
    engines = DEFAULT_ENGINE_ORDER
    selected = getattr(args, "engine", None)
    if selected:
        engines = tuple(selected)
    else:
        legacy = getattr(args, "resolver", None)
        if legacy:
            engines = RESOLVER_ALIASES.get(legacy, DEFAULT_ENGINE_ORDER)

    return ChainOptions(
        engines=engines,
        ytdlp=YtDlpOptions(cookies_from_browser=getattr(args, "cookies_from_browser", None) or None),
        browser=BrowserOptions(
            wait_seconds=getattr(args, "wait", 15),
            headless=not getattr(args, "headed", False),
        ),
    )


def cmd_list_formats(args: argparse.Namespace) -> int:
    resolutions = chain_resolve(args.url, _chain_options(args))
    if not resolutions:
        print("[!] No engine resolved this URL.")
        return 2

    for index, resolution in enumerate(resolutions, start=1):
        header = f"{index}. {resolution.title or resolution.final_url}"
        print(f"\n{header}")
        print(f"   engine={resolution.engine} duration={resolution.duration or '?'}")
        if not resolution.formats:
            print("   (this engine reports no selectable formats; it found direct media only)")
            for media in resolution.media[:5]:
                print(f"   - {media.kind:6} {media.url}")
            continue
        for fmt in resolution.formats:
            print(f"   - {fmt.format_id:>8}  {fmt.label()}")
    return 0


def cmd_probe_batch(args: argparse.Namespace) -> int:
    result = batch_probe(args.url)

    print(f"capability : {result.capability}")
    print(f"confidence : {result.confidence}")
    print(f"items      : {len(result.items)}" + (f" of ~{result.total_estimate}" if result.total_estimate else ""))
    if result.truncated:
        print("             (list was capped; more items exist)")

    if not result.is_batchable:
        print("batchable  : no")
        print(f"reason     : {result.reason}")
        return 2

    print("batchable  : yes")
    if result.confidence == "possible":
        print("note       : these are page links, not confirmed media")
    for index, item in enumerate(result.items[:40], start=1):
        print(f"  {index:3}. {item.title or item.url}")
    if len(result.items) > 40:
        print(f"  ... ({len(result.items) - 40} more)")

    if args.verify:
        verified, attempted = sample_verify(result.items, args.verify)
        print(f"verified   : {verified}/{attempted} sampled item(s) resolve")
    return 0


def _open_manager(args: argparse.Namespace):
    """Build a manager over the real store. Caller must shut it down."""
    store = JobStore(getattr(args, "db", None) or None)
    bus = EventBus()
    manager = JobManager(
        store=store,
        bus=bus,
        options=_pipeline_options(args),
        concurrency=getattr(args, "concurrency", DEFAULT_CONCURRENCY),
    )
    return manager, store


def _print_jobs(jobs) -> None:
    if not jobs:
        print("(no jobs)")
        return
    for job in jobs:
        percent = f"{job.percent:5.1f}%" if job.percent is not None else "    ?"
        print(f"{job.id[:8]}  {job.status.value:13} {percent}  {job.title or job.url}")
        if job.error:
            print(f"          error: {job.error}")


def _drain_until_idle(manager, store, job_ids: list[str], timeout: float) -> int:
    """Block until the given jobs leave the active statuses."""
    deadline = time.monotonic() + timeout
    pending = set(job_ids)
    while pending and time.monotonic() < deadline:
        for job_id in list(pending):
            job = store.get(job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                pending.discard(job_id)
        if pending:
            time.sleep(0.5)

    failed = 0
    for job_id in job_ids:
        job = store.get(job_id)
        if job is None:
            continue
        if job.status == JobStatus.COMPLETED:
            print(f"[+] {job.id[:8]} {job.output_path}")
        else:
            failed += 1
            print(f"[!] {job.id[:8]} {job.status.value}: {job.error or ''}")
    return failed


def cmd_queue(args: argparse.Namespace) -> int:
    manager, store = _open_manager(args)
    try:
        if args.queue_action == "list":
            _print_jobs(store.list(status=args.status or None))
            counts = store.counts_by_status()
            if counts:
                print("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
            return 0

        if args.queue_action == "add":
            urls = [url for url in args.url if url.strip()]
            job_ids = []
            for url in urls:
                try:
                    job = manager.submit(url)
                except DuplicateJob as exc:
                    print(f"[!] skip {url}: {exc}")
                    continue
                job_ids.append(job.id)
                print(f"[+] queued {job.id[:8]} {url}")
            if not job_ids:
                return 2
            if args.wait:
                return 3 if _drain_until_idle(manager, store, job_ids, args.timeout) else 0
            return 0

        if args.queue_action == "batch":
            result = batch_probe(args.url)
            if not result.is_batchable:
                print(f"[!] not batchable: {result.reason}")
                return 2
            if result.confidence == "possible" and not args.accept_possible:
                print(f"[!] {len(result.items)} link(s) found, but these are page links, not confirmed media.")
                print("[i] Re-run with --accept-possible to queue them anyway.")
                return 2
            items = result.items[: args.limit] if args.limit else result.items
            batch, jobs, skipped = manager.submit_batch(
                items,
                source_url=args.url,
                capability=result.capability,
                confidence=result.confidence,
            )
            print(f"[+] batch {batch.id[:8]}: queued {len(jobs)}, skipped {len(skipped)}")
            if args.wait:
                return 3 if _drain_until_idle(manager, store, [j.id for j in jobs], args.timeout) else 0
            return 0

        if args.queue_action == "cancel":
            return 0 if manager.cancel(args.job_id) else 2

        if args.queue_action == "retry":
            job = manager.retry(args.job_id)
            if job is None:
                print(f"[!] unknown job: {args.job_id}")
                return 2
            print(f"[+] requeued {job.id[:8]}")
            if args.wait:
                return 3 if _drain_until_idle(manager, store, [job.id], args.timeout) else 0
            return 0

        if args.queue_action == "import-csv":
            urls = read_csv_urls(Path(args.csv))
            print(f"[i] {len(urls)} URL(s) in {args.csv}")
            if args.dry_run:
                for url in urls[:40]:
                    print(f"  would queue: {url}")
                if len(urls) > 40:
                    print(f"  ... ({len(urls) - 40} more)")
                return 0
            job_ids = []
            for url in urls:
                try:
                    job_ids.append(manager.submit(url).id)
                except DuplicateJob:
                    continue
            print(f"[+] queued {len(job_ids)} job(s)")
            if args.wait:
                return 3 if _drain_until_idle(manager, store, job_ids, args.timeout) else 0
            return 0

        if args.queue_action == "recover":
            recovered = manager.recover_interrupted()
            print(f"[i] marked {len(recovered)} job(s) interrupted")
            return 0

        print(f"[!] unknown queue action: {args.queue_action}")
        return 2
    finally:
        manager.shutdown()
        store.close()


def _add_selection_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--allow-host", action="append", default=[], help="Allow only candidates from this host (repeatable)")
    parser.add_argument("--prefer-host", action="append", default=[], help="Boost candidates from this host (repeatable)")
    parser.add_argument("--pick", type=int, default=1, help="Pick candidate index from detected list (1-based)")
    parser.add_argument("--interactive-pick", action="store_true", help="Prompt manual candidate choice before download")
    parser.add_argument("--max-attempts", type=int, default=5, help="Max candidate retries when ffmpeg fails")
    parser.add_argument("--min-duration", type=int, default=120, help="Reject downloaded outputs shorter than N seconds")
    parser.add_argument("--no-precheck-hls", action="store_true", help="Disable HLS playlist segment precheck")
    parser.add_argument("--no-rank-with-ffprobe", action="store_true", help="Disable ffprobe pre-ranking")
    parser.add_argument("--rank-top-n", type=int, default=6, help="Apply ffprobe ranking to top N candidates")
    parser.add_argument(
        "--dump-all-candidates",
        default="",
        help="Write full candidate dump (main/embed/phase) JSON",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="videotrack",
        description="Analyze website video network traffic and download authorized streams.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="Capture browser network logs")
    _add_shared_capture_args(p_analyze)
    _add_autonomous_flag(p_analyze)
    p_analyze.add_argument("--capture-out", default="logs/capture.json")
    p_analyze.set_defaults(func=cmd_analyze)

    p_detect = sub.add_parser("detect", help="Detect stream URLs from a capture file")
    p_detect.add_argument("--capture", default="logs/capture.json")
    p_detect.add_argument("--candidates-out", default="logs/candidates.json")
    p_detect.add_argument("--wait", type=int, default=15, help="Seconds to wait when deep-scanning embed URLs")
    p_detect.add_argument("--extra-wait", type=int, default=45, help="Extra seconds for second deep-scan pass")
    p_detect.add_argument("--headed", action="store_true", help="Run Chrome with UI during deep-scan")
    p_detect.add_argument("--no-probe", action="store_true", help="Skip HTTP probe for candidate validation")
    p_detect.add_argument("--allow-host", action="append", default=[], help="Allow only candidates from this host (repeatable)")
    p_detect.add_argument("--prefer-host", action="append", default=[], help="Boost candidates from this host (repeatable)")
    p_detect.add_argument("--no-precheck-hls", action="store_true", help="Disable HLS playlist segment precheck")
    p_detect.add_argument("--no-rank-with-ffprobe", action="store_true", help="Disable ffprobe pre-ranking")
    p_detect.add_argument("--rank-top-n", type=int, default=6, help="Apply ffprobe ranking to top N candidates")
    p_detect.add_argument("--dump-all-candidates", default="", help="Write full candidate dump (main/embed/phase) JSON")
    _add_autonomous_flag(p_detect)
    p_detect.set_defaults(func=cmd_detect)

    p_download = sub.add_parser("download", help="Download using best detected candidate")
    p_download.add_argument("--capture", default="logs/capture.json")
    p_download.add_argument("--output-dir", default="output")
    p_download.add_argument("--wait", type=int, default=15, help="Seconds to wait when deep-scanning embed URLs")
    p_download.add_argument("--extra-wait", type=int, default=45, help="Extra seconds for second deep-scan pass")
    p_download.add_argument("--headed", action="store_true", help="Run Chrome with UI during deep-scan")
    p_download.add_argument("--no-probe", action="store_true")
    _add_autonomous_flag(p_download)
    _add_selection_flags(p_download)
    p_download.set_defaults(func=cmd_download)

    p_run = sub.add_parser("run", help="Run full pipeline: analyze -> detect -> download")
    _add_shared_capture_args(p_run)
    p_run.add_argument("--capture-out", default="logs/capture.json")
    p_run.add_argument("--candidates-out", default="logs/candidates.json")
    p_run.add_argument("--output-dir", default="output")
    p_run.add_argument("--extra-wait", type=int, default=45, help="Extra seconds for second deep-scan pass")
    p_run.add_argument("--no-probe", action="store_true")
    p_run.add_argument(
        "--engine",
        action="append",
        choices=list(engine_choices()),
        help="Engine chain to try, in order (repeatable). Default: yt-dlp, then site plugins, then browser",
    )
    p_run.add_argument(
        "--resolver",
        choices=list(RESOLVER_ALIASES),
        default="auto",
        help="Deprecated alias for --engine, kept for existing scripts",
    )
    p_run.add_argument(
        "--format",
        dest="format_id",
        default="",
        help="Format id to download, as listed by list-formats",
    )
    p_run.add_argument(
        "--cookies-from-browser",
        default="",
        help="Reuse a browser profile's cookies, e.g. chrome (best effort, off by default)",
    )
    _add_autonomous_flag(p_run)
    _add_selection_flags(p_run)
    p_run.set_defaults(func=cmd_run)

    p_doctor = sub.add_parser("doctor", help="Report external tool and plugin availability")
    p_doctor.set_defaults(func=cmd_doctor)

    p_formats = sub.add_parser("list-formats", help="Show the selectable formats an engine reports")
    p_formats.add_argument("url", help="Page URL you are authorized to analyze")
    p_formats.add_argument("--engine", action="append", choices=list(engine_choices()), help="Restrict the engine chain (repeatable, in order)")
    p_formats.add_argument("--wait", type=int, default=15, help="Seconds to wait when the browser engine is used")
    p_formats.add_argument("--headed", action="store_true", help="Run Chrome with UI when the browser engine is used")
    p_formats.add_argument("--cookies-from-browser", default="", help="Reuse a browser profile's cookies, e.g. chrome (best effort)")
    p_formats.set_defaults(func=cmd_list_formats)

    p_probe = sub.add_parser("probe-batch", help="Check whether a URL enumerates multiple downloadable items")
    p_probe.add_argument("url", help="Page URL you are authorized to analyze")
    p_probe.add_argument("--verify", type=int, default=0, help="Fully resolve the first N items to raise confidence")
    p_probe.set_defaults(func=cmd_probe_batch)

    p_queue = sub.add_parser("queue", help="Manage the persistent download queue")
    p_queue.add_argument("--db", default="", help="Job database path (default: the state directory)")
    p_queue.add_argument("--output-dir", default="output", help="Where finished media goes")
    p_queue.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="How many jobs run at once")
    queue_sub = p_queue.add_subparsers(dest="queue_action", required=True)

    q_add = queue_sub.add_parser("add", help="Queue one or more URLs")
    q_add.add_argument("url", nargs="+", help="URLs you are authorized to download")
    q_add.add_argument("--wait", action="store_true", help="Block until the queued jobs finish")
    q_add.add_argument("--timeout", type=float, default=3600.0, help="Seconds to wait when --wait is used")

    q_batch = queue_sub.add_parser("batch", help="Probe a URL and queue every item it enumerates")
    q_batch.add_argument("url", help="Playlist, collection, or listing URL")
    q_batch.add_argument("--limit", type=int, default=0, help="Queue at most N items (0 = all)")
    q_batch.add_argument(
        "--accept-possible",
        action="store_true",
        help="Also queue crawl-derived links, which are page links rather than confirmed media",
    )
    q_batch.add_argument("--wait", action="store_true", help="Block until the queued jobs finish")
    q_batch.add_argument("--timeout", type=float, default=7200.0, help="Seconds to wait when --wait is used")

    q_list = queue_sub.add_parser("list", help="Show queued and finished jobs")
    q_list.add_argument("--status", default="", help="Filter by status")

    q_cancel = queue_sub.add_parser("cancel", help="Cancel a job")
    q_cancel.add_argument("job_id", help="Job id")

    q_retry = queue_sub.add_parser("retry", help="Requeue a failed or interrupted job")
    q_retry.add_argument("job_id", help="Job id")
    q_retry.add_argument("--wait", action="store_true", help="Block until the job finishes")
    q_retry.add_argument("--timeout", type=float, default=3600.0, help="Seconds to wait when --wait is used")

    q_import = queue_sub.add_parser("import-csv", help="Queue every URL in a crawl CSV")
    q_import.add_argument("csv", help="CSV produced by crawl-links")
    q_import.add_argument("--dry-run", action="store_true", help="List what would be queued")
    q_import.add_argument("--wait", action="store_true", help="Block until the queued jobs finish")
    q_import.add_argument("--timeout", type=float, default=7200.0, help="Seconds to wait when --wait is used")

    queue_sub.add_parser("recover", help="Mark jobs left running by a dead process as interrupted")

    p_queue.set_defaults(func=cmd_queue)

    p_crawl = sub.add_parser("crawl-links", help="Crawl a website and export discovered child URLs to CSV")
    p_crawl.add_argument("url", help="Start URL to crawl")
    p_crawl.add_argument("--output-csv", default="output/links.csv", help="CSV output path")
    p_crawl.add_argument(
        "--site-preset",
        choices=list(crawl_preset_choices()),
        default="auto",
        help="Apply site-specific crawl rules. Default: auto-detect from the input URL host",
    )
    p_crawl.add_argument(
        "--include-substring",
        default=None,
        help="Only collect links containing this substring. If omitted, the selected site preset decides",
    )
    p_crawl.add_argument(
        "--exclude-substring",
        action="append",
        default=[],
        help="Skip links containing this substring (repeatable)",
    )
    p_crawl.add_argument("--max-pages", type=int, default=300, help="Maximum number of pages to crawl")
    p_crawl.add_argument("--timeout", type=int, default=12, help="HTTP timeout per page in seconds")
    p_crawl.add_argument(
        "--user-agent",
        default="Mozilla/5.0 (compatible; videotrack-crawler/1.0)",
        help="Custom User-Agent for crawl requests",
    )
    p_crawl.set_defaults(func=cmd_crawl_links)

    p_collect = sub.add_parser("collect", help="Download direct media listed in a static Flowplayer collection")
    source_group = p_collect.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--url", help="Authorized collection page URL")
    source_group.add_argument("--html-file", help="Saved collection HTML for offline parsing")
    p_collect.add_argument("--source-url", default="", help="Source URL when parsing a saved HTML file")
    p_collect.add_argument("--output-dir", default="output/collections")
    p_collect.add_argument("--dry-run", action="store_true")
    p_collect.add_argument("--overwrite", action="store_true")
    p_collect.set_defaults(func=cmd_collect)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Before the parser builds its defaults from the paths module, so the
    # documented `.env` file actually reaches them.
    load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
