from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

from .capture import capture_page
from .collection import download_collection, fetch_collection, parse_flowplayer_collection
from .crawl import crawl_site_links, resolve_crawl_preset, save_urls_to_csv
from .detect import (
    build_request_headers,
    detect_candidates,
    extract_embed_urls,
    filter_candidates_by_host,
    precheck_hls_candidates,
)
from .download import download_with_ffmpeg
from .io import load_capture, save_candidates, save_capture, save_json
from .models import CaptureResult, StreamCandidate
from .resolvers import capture_from_resolution
from .static_player import StaticPlayerResolver


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


def _clone_candidate(candidate: StreamCandidate, source: str) -> StreamCandidate:
    return StreamCandidate(
        url=candidate.url,
        kind=candidate.kind,
        score=candidate.score,
        source=source,
        status_code=candidate.status_code,
        content_type=candidate.content_type,
        host=candidate.host,
        probe_duration=candidate.probe_duration,
        probe_bitrate=candidate.probe_bitrate,
        validation_note=candidate.validation_note,
        referer=candidate.referer,
    )


def _merge_candidates(
    merged: OrderedDict[str, StreamCandidate],
    incoming: list[StreamCandidate],
    source: str,
) -> None:
    for candidate in incoming:
        new_item = _clone_candidate(candidate, source)
        existing = merged.get(new_item.url)
        if existing is None:
            merged[new_item.url] = new_item
            continue

        if source not in existing.source.split(","):
            existing.source = f"{existing.source},{source}"

        if new_item.score > existing.score:
            existing.score = new_item.score
            existing.kind = new_item.kind
            existing.referer = new_item.referer

        if new_item.status_code is not None:
            existing.status_code = new_item.status_code
        if new_item.content_type:
            existing.content_type = new_item.content_type
        if new_item.validation_note:
            existing.validation_note = new_item.validation_note


def _collect_candidates(
    base_capture: CaptureResult,
    probe: bool,
    headed: bool,
    wait_seconds: int,
    extra_wait: int,
) -> tuple[list[StreamCandidate], dict[str, int]]:
    merged: OrderedDict[str, StreamCandidate] = OrderedDict()
    stage_counts: dict[str, int] = {}

    main_candidates = detect_candidates(capture=base_capture, probe=probe)
    _merge_candidates(merged, main_candidates, "main")
    stage_counts["main"] = len(main_candidates)

    if main_candidates:
        return sorted(merged.values(), key=lambda x: x.score, reverse=True), stage_counts

    embed_urls = extract_embed_urls(base_capture)
    stage_counts["embed_urls"] = len(embed_urls)

    if embed_urls:
        print(f"[i] No direct stream detected. Deep scan {len(embed_urls)} embed URL(s).")

    for embed_idx, embed_url in enumerate(embed_urls[:3], start=1):
        print(f"[i] Analyze embed: {embed_url}")
        waits = [max(wait_seconds, 12)]
        if extra_wait > 0:
            waits.append(max(wait_seconds, 12) + extra_wait)

        for phase, phase_wait in enumerate(waits, start=1):
            stage = f"embed{embed_idx}_phase{phase}"
            if len(waits) > 1:
                print(f"[i] Embed phase {phase}/{len(waits)} wait={phase_wait}s")

            embed_capture = capture_page(
                url=embed_url,
                wait_seconds=phase_wait,
                headless=not headed,
                try_play=True,
            )
            embed_candidates = detect_candidates(capture=embed_capture, probe=probe)
            _merge_candidates(merged, embed_candidates, stage)
            stage_counts[stage] = len(embed_candidates)

    return sorted(merged.values(), key=lambda x: x.score, reverse=True), stage_counts


def _probe_duration_seconds(path: Path) -> float | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout or "{}")
        duration = float(data.get("format", {}).get("duration", 0))
        return duration if duration > 0 else None
    except Exception:
        return None


def _headers_block_for_ffprobe(capture: CaptureResult, candidate: StreamCandidate) -> str:
    headers = build_request_headers(capture, candidate.referer)
    headers.pop("User-Agent", None)
    if not headers:
        return ""
    return "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n"


def _probe_candidate_media(capture: CaptureResult, candidate: StreamCandidate) -> None:
    cmd = ["ffprobe", "-v", "error"]

    if capture.user_agent:
        cmd.extend(["-user_agent", capture.user_agent])

    header_block = _headers_block_for_ffprobe(capture, candidate)
    if header_block:
        cmd.extend(["-headers", header_block])

    cmd.extend(
        [
            "-show_entries",
            "format=duration,bit_rate",
            "-of",
            "json",
            candidate.url,
        ]
    )

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout or "{}")
        fmt = data.get("format", {})
        duration = float(fmt.get("duration", 0) or 0)
        bitrate = int(float(fmt.get("bit_rate", 0) or 0))

        if duration > 0:
            candidate.probe_duration = duration
            if duration < 90:
                candidate.score -= 45
            elif duration < 180:
                candidate.score -= 20
            candidate.score += min(int(duration / 60), 80)
        if bitrate > 0:
            candidate.probe_bitrate = bitrate
            candidate.score += min(int(bitrate / 500_000), 25)
    except Exception:
        return


def _rank_candidates_with_ffprobe(
    capture: CaptureResult,
    candidates: list[StreamCandidate],
    top_n: int,
) -> list[StreamCandidate]:
    for candidate in candidates[: max(top_n, 0)]:
        _probe_candidate_media(capture, candidate)
    return sorted(candidates, key=lambda x: x.score, reverse=True)


def _boost_preferred_hosts(candidates: list[StreamCandidate], prefer_hosts: list[str]) -> list[StreamCandidate]:
    if not prefer_hosts:
        return candidates

    normalized = [h.lower().strip() for h in prefer_hosts if h.strip()]
    if not normalized:
        return candidates

    for candidate in candidates:
        host = (candidate.host or "").lower()
        if any(host == h or host.endswith(f".{h}") for h in normalized):
            candidate.score += 35

    return sorted(candidates, key=lambda x: x.score, reverse=True)


def _serialize_candidates(candidates: list[StreamCandidate]) -> list[dict]:
    return [c.to_dict() for c in candidates]


def _interactive_reorder(candidates: list[StreamCandidate]) -> list[StreamCandidate]:
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


def _download_with_fallback(
    capture: CaptureResult,
    candidates: list[StreamCandidate],
    pick: int,
    output_dir: Path,
    max_attempts: int,
    min_duration: int,
    interactive_pick: bool,
    metadata_capture: CaptureResult | None = None,
) -> int:
    start = pick - 1 if pick > 0 and pick <= len(candidates) else 0
    ordered = candidates[start:] + candidates[:start]

    if interactive_pick:
        ordered = _interactive_reorder(ordered)

    attempts = min(max_attempts, len(ordered))

    last_error: Exception | None = None
    for idx, selected in enumerate(ordered[:attempts], start=1):
        print(f"[+] Try {idx}/{attempts}: {selected.kind} | {selected.url}")
        try:
            out_file = download_with_ffmpeg(
                capture=capture,
                candidate=selected,
                output_dir=output_dir,
                metadata_capture=metadata_capture,
            )
            duration = _probe_duration_seconds(out_file)
            if duration is not None and duration < float(min_duration):
                print(
                    f"[!] Reject short output ({duration:.1f}s < {min_duration}s). "
                    "Try next candidate."
                )
                try:
                    out_file.unlink(missing_ok=True)
                except Exception:
                    pass
                continue
            print(f"[+] Downloaded: {out_file}")
            return 0
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"[!] Failed candidate: {exc}")

    print("[!] All candidate attempts failed.")
    if last_error:
        print(f"[!] Last error: {last_error}")
    return 3


def _prepare_candidates(
    capture: CaptureResult,
    probe: bool,
    headed: bool,
    wait_seconds: int,
    extra_wait: int,
    allow_hosts: list[str],
    precheck_hls: bool,
    rank_with_ffprobe: bool,
    rank_top_n: int,
    prefer_hosts: list[str],
) -> tuple[list[StreamCandidate], dict[str, int], list[StreamCandidate]]:
    all_candidates, stage_counts = _collect_candidates(
        base_capture=capture,
        probe=probe,
        headed=headed,
        wait_seconds=wait_seconds,
        extra_wait=extra_wait,
    )

    selected = list(all_candidates)

    if allow_hosts:
        before = len(selected)
        selected = filter_candidates_by_host(selected, allow_hosts)
        print(f"[i] allow-host filter: {before} -> {len(selected)}")

    if prefer_hosts and selected:
        selected = _boost_preferred_hosts(selected, prefer_hosts)
        print(f"[i] prefer-host boost applied: {', '.join(prefer_hosts)}")

    if precheck_hls and selected:
        selected = precheck_hls_candidates(selected, capture)

    if rank_with_ffprobe and selected:
        selected = _rank_candidates_with_ffprobe(capture, selected, rank_top_n)

    return selected, stage_counts, all_candidates


def _resolve_download_capture(
    base_capture: CaptureResult,
    candidates: list[StreamCandidate],
    headed: bool,
    wait_seconds: int,
) -> CaptureResult:
    if not candidates:
        return base_capture

    top = candidates[0]
    if "embed" not in (top.source or ""):
        return base_capture

    embed_urls = extract_embed_urls(base_capture)
    if not embed_urls:
        return base_capture

    embed_url = embed_urls[0]
    print(f"[i] Refresh embed capture context for download: {embed_url}")
    try:
        return capture_page(
            url=embed_url,
            wait_seconds=max(wait_seconds, 15),
            headless=not headed,
            try_play=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Embed recapture failed, fallback to base capture: {exc}")
        return base_capture


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
    candidates, stage_counts, all_candidates = _prepare_candidates(
        capture=capture,
        probe=not args.no_probe,
        headed=args.headed,
        wait_seconds=args.wait,
        extra_wait=args.extra_wait,
        allow_hosts=args.allow_host,
        precheck_hls=not args.no_precheck_hls,
        rank_with_ffprobe=not args.no_rank_with_ffprobe,
        rank_top_n=args.rank_top_n,
        prefer_hosts=args.prefer_host,
    )

    save_candidates(candidates, Path(args.candidates_out))
    if args.dump_all_candidates:
        save_json(
            {
                "stage_counts": stage_counts,
                "all_candidates": _serialize_candidates(all_candidates),
                "final_candidates": _serialize_candidates(candidates),
            },
            Path(args.dump_all_candidates),
        )

    print(f"[+] Candidates: {len(candidates)}")
    for idx, candidate in enumerate(candidates[:20], start=1):
        print(
            f"{idx}. kind={candidate.kind:8} score={candidate.score:3} host={candidate.host} "
            f"status={candidate.status_code} url={candidate.url}"
        )
    print(f"[+] Saved candidates: {args.candidates_out}")
    if args.dump_all_candidates:
        print(f"[+] Saved dump: {args.dump_all_candidates}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    _apply_autonomous_overrides(args)
    capture = load_capture(Path(args.capture))
    candidates, stage_counts, all_candidates = _prepare_candidates(
        capture=capture,
        probe=not args.no_probe,
        headed=args.headed,
        wait_seconds=args.wait,
        extra_wait=args.extra_wait,
        allow_hosts=args.allow_host,
        precheck_hls=not args.no_precheck_hls,
        rank_with_ffprobe=not args.no_rank_with_ffprobe,
        rank_top_n=args.rank_top_n,
        prefer_hosts=args.prefer_host,
    )

    if args.dump_all_candidates:
        save_json(
            {
                "stage_counts": stage_counts,
                "all_candidates": _serialize_candidates(all_candidates),
                "final_candidates": _serialize_candidates(candidates),
            },
            Path(args.dump_all_candidates),
        )
        print(f"[+] Saved dump: {args.dump_all_candidates}")

    if not candidates:
        print("[!] No stream candidate found.")
        return 2

    download_capture = _resolve_download_capture(
        base_capture=capture,
        candidates=candidates,
        headed=args.headed,
        wait_seconds=args.wait,
    )

    return _download_with_fallback(
        download_capture,
        candidates,
        args.pick,
        Path(args.output_dir),
        args.max_attempts,
        args.min_duration,
        args.interactive_pick,
        metadata_capture=capture,
    )


def _run_capture_pipeline(args: argparse.Namespace, capture: CaptureResult) -> int:
    save_capture(capture, Path(args.capture_out))
    candidates, stage_counts, all_candidates = _prepare_candidates(
        capture=capture,
        probe=not args.no_probe,
        headed=args.headed,
        wait_seconds=args.wait,
        extra_wait=args.extra_wait,
        allow_hosts=args.allow_host,
        precheck_hls=not args.no_precheck_hls,
        rank_with_ffprobe=not args.no_rank_with_ffprobe,
        rank_top_n=args.rank_top_n,
        prefer_hosts=args.prefer_host,
    )
    save_candidates(candidates, Path(args.candidates_out))

    if args.dump_all_candidates:
        save_json(
            {
                "stage_counts": stage_counts,
                "all_candidates": _serialize_candidates(all_candidates),
                "final_candidates": _serialize_candidates(candidates),
            },
            Path(args.dump_all_candidates),
        )
        print(f"[+] Saved dump: {args.dump_all_candidates}")

    if not candidates:
        print("[!] No stream candidate found after analysis.")
        return 2

    download_capture = _resolve_download_capture(
        base_capture=capture,
        candidates=candidates,
        headed=args.headed,
        wait_seconds=args.wait,
    )

    return _download_with_fallback(
        download_capture,
        candidates,
        args.pick,
        Path(args.output_dir),
        args.max_attempts,
        args.min_duration,
        args.interactive_pick,
        metadata_capture=capture,
    )


def cmd_run(args: argparse.Namespace) -> int:
    _apply_autonomous_overrides(args)
    if args.resolver in {"auto", "static"}:
        resolution = StaticPlayerResolver().resolve(args.url)
        if resolution:
            capture = capture_from_resolution(resolution)
            print(f"[+] Static resolver matched: {resolution.resolver} ({len(resolution.media)} candidate(s))")
            result = _run_capture_pipeline(args, capture)
            if result == 0 or args.resolver == "static":
                return result
            print("[i] Static download path failed; retrying with browser network capture.")
        elif args.resolver == "static":
            print("[!] Static resolver found no supported media on this page.")
            return 2

    capture = capture_page(
        url=args.url,
        wait_seconds=args.wait,
        headless=not args.headed,
    )
    return _run_capture_pipeline(args, capture)


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
        "--resolver",
        choices=["auto", "static", "browser"],
        default="auto",
        help="Resolution strategy: static first, static only, or browser only",
    )
    _add_autonomous_flag(p_run)
    _add_selection_flags(p_run)
    p_run.set_defaults(func=cmd_run)

    p_crawl = sub.add_parser("crawl-links", help="Crawl a website and export discovered child URLs to CSV")
    p_crawl.add_argument("url", help="Start URL to crawl")
    p_crawl.add_argument("--output-csv", default="output/links.csv", help="CSV output path")
    p_crawl.add_argument(
        "--site-preset",
        choices=["auto", "generic", "vlxx", "quatvn"],
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
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
