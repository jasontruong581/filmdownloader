from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from videotrack.core.capture import capture_page
from videotrack.core.download import download_with_ffmpeg
from videotrack.sites.quatvn import discover_quatvn_targets, extract_quatvn_stream_candidates, is_quatvn_stream_url, is_quatvn_url

BASE_FIELDNAMES = ["order", "id", "proceed_status", "url", "target_count", "completed_count", "last_error"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run videotrack sequentially from CSV and mark downloaded rows.",
    )
    parser.add_argument("--csv", default="output/links.csv", help="Path to CSV file")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of links per batch")
    parser.add_argument("--start-order", type=int, default=1, help="Minimum order to process")
    parser.add_argument("--output-dir", default="output", help="Output directory for videos")
    parser.add_argument("--wait", type=int, default=30, help="Wait seconds for analyze")
    parser.add_argument("--extra-wait", type=int, default=90, help="Extra wait seconds for deep scan")
    parser.add_argument("--prefer-host", default="", help="Boost candidates from this host")
    parser.add_argument("--pick", type=int, default=1, help="Candidate pick index")
    parser.add_argument("--dump-all-candidates", default="logs/candidates_all.json", help="Dump all candidates path")
    parser.add_argument("--pending-value", default="not proceed", help="Status value for pending rows")
    parser.add_argument("--downloaded-value", default="downloaded", help="Status value when completed")
    parser.add_argument("--dry-run", action="store_true", help="Print actions only, do not run downloads")
    parser.add_argument("--quatvn-discovery-wait", type=int, default=8, help="Seconds to wait when discovering child targets on quatvn pages")
    parser.add_argument("--quatvn-headed", action="store_true", help="Run Chrome with UI when discovering child targets on quatvn pages")
    return parser.parse_args()


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _slug_from_url(url: str) -> str:
    path = (urlparse(url).path or "/").strip("/")
    slug = path.split("/")[-1] if path else ""
    if not slug:
        slug = "row"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in slug).strip("-_")
    return safe or "row"


def _normalize_rows(rows: list[dict[str, str]], pending_value: str) -> tuple[list[dict[str, str]], list[str]]:
    fieldnames = list(rows[0].keys()) if rows else ["url"]
    for name in BASE_FIELDNAMES:
        if name not in fieldnames:
            fieldnames.append(name)

    normalized: list[dict[str, str]] = []
    for idx, raw in enumerate(rows, start=1):
        row = {name: (raw.get(name, "") or "") for name in fieldnames}
        url = row.get("url", "").strip()
        row["url"] = url
        row["order"] = (row.get("order") or "").strip() or str(idx)
        row["id"] = (row.get("id") or "").strip() or _slug_from_url(url) or str(idx)
        row["proceed_status"] = (row.get("proceed_status") or "").strip() or pending_value
        row["target_count"] = (row.get("target_count") or "").strip()
        row["completed_count"] = (row.get("completed_count") or "").strip()
        row["last_error"] = (row.get("last_error") or "").strip()
        normalized.append(row)

    return normalized, fieldnames


def save_rows(csv_path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_order(row: dict[str, str]) -> int:
    raw = (row.get("order") or "").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def has_downloaded_file(output_dir: Path, video_id: str) -> bool:
    if not video_id:
        return False
    return any(output_dir.glob(f"{video_id}-*.mp4"))


def build_run_command(args: argparse.Namespace, url: str, output_dir: str, prefer_host: str | None) -> list[str]:
    cmd = [
        sys.executable,
        "main.py",
        "run",
        url,
        "--wait",
        str(args.wait),
        "--extra-wait",
        str(args.extra_wait),
        "--autonomous",
        "--pick",
        str(args.pick),
        "--output-dir",
        output_dir,
        "--dump-all-candidates",
        args.dump_all_candidates,
    ]
    if prefer_host:
        cmd.extend(["--prefer-host", prefer_host])
    return cmd


def _state_dir(output_dir: Path) -> Path:
    path = output_dir / ".batch_state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path(output_dir: Path, row_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in row_id).strip("-_") or "row"
    return _state_dir(output_dir) / f"{safe}.json"


def _load_row_state(output_dir: Path, row_id: str) -> dict:
    path = _state_path(output_dir, row_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_row_state(output_dir: Path, row_id: str, data: dict) -> None:
    path = _state_path(output_dir, row_id)
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")


def _completed_targets_from_state(state: dict) -> set[str]:
    items = state.get("completed_targets") or []
    return {str(item).strip() for item in items if str(item).strip()}


def _resolve_output_dir(args: argparse.Namespace, rows: list[dict[str, str]]) -> str:
    if args.output_dir != "output":
        return args.output_dir
    first_url = next((row.get("url", "").strip() for row in rows if row.get("url", "").strip()), "")
    if is_quatvn_url(first_url):
        return "output/quatvn"
    return args.output_dir


def _row_is_pending(row: dict[str, str], args: argparse.Namespace) -> bool:
    return (row.get("proceed_status") or "").strip().lower() == args.pending_value.lower()


def _run_single_target(args: argparse.Namespace, root: Path, output_dir: str, url: str, prefer_host: str | None) -> int:
    cmd = build_run_command(args, url, output_dir, prefer_host)
    print(f"[i] run: {' '.join(cmd)}")
    if args.dry_run:
        return 0
    result = subprocess.run(cmd, cwd=root)
    print(f"[i] exit_code={result.returncode}")
    return result.returncode


def _process_standard_row(
    args: argparse.Namespace,
    root: Path,
    row: dict[str, str],
    output_dir_path: Path,
    output_dir_arg: str,
) -> None:
    video_id = (row.get("id") or "").strip()
    url = (row.get("url") or "").strip()

    if has_downloaded_file(output_dir_path, video_id):
        row["proceed_status"] = args.downloaded_value
        row["last_error"] = ""
        print(f"[i] already exists -> mark {args.downloaded_value}")
        return

    exit_code = _run_single_target(args, root, output_dir_arg, url, args.prefer_host or None)
    if exit_code == 0 or has_downloaded_file(output_dir_path, video_id):
        row["proceed_status"] = args.downloaded_value
        row["last_error"] = ""
        print(f"[+] found output/{video_id}-*.mp4 -> mark {args.downloaded_value}")
    else:
        row["last_error"] = f"exit_code={exit_code}"
        print(f"[!] no output/{video_id}-*.mp4 -> keep status='{row.get('proceed_status', '')}'")


def _process_quatvn_row(
    args: argparse.Namespace,
    root: Path,
    row: dict[str, str],
    output_dir_path: Path,
    output_dir_arg: str,
) -> None:
    row_id = (row.get("id") or "").strip() or _slug_from_url(row.get("url", ""))
    page_url = (row.get("url") or "").strip()
    state = _load_row_state(output_dir_path, row_id)
    completed = _completed_targets_from_state(state)

    print(f"[i] capture quatvn page: {page_url}")
    discovered_targets = []
    try:
        discovered_targets = discover_quatvn_targets(
            page_url=page_url,
            wait_seconds=args.quatvn_discovery_wait,
            headless=not args.quatvn_headed,
        )
    except Exception as exc:
        print(f"[!] quatvn DOM discovery failed: {exc}")

    page_targets = [target for target in discovered_targets if not is_quatvn_stream_url(target)]
    if page_targets:
        state["page_url"] = page_url
        state["discovered_targets"] = page_targets
        row["target_count"] = str(len(page_targets))
        pending_targets = [target for target in page_targets if target not in completed]
        row["completed_count"] = str(len(completed))

        if not pending_targets:
            row["completed_count"] = str(len(page_targets))
            row["proceed_status"] = args.downloaded_value
            row["last_error"] = ""
            _save_row_state(output_dir_path, row_id, state | {"completed_targets": sorted(completed)})
            print(f"[i] all {len(page_targets)} quatvn page target(s) already done -> mark {args.downloaded_value}")
            return

        for idx, target in enumerate(pending_targets, start=1):
            print(f"[quatvn-page {idx}/{len(pending_targets)}] {target}")
            exit_code = _run_single_target(args, root, output_dir_arg, target, None)
            if exit_code == 0:
                completed.add(target)
                state["completed_targets"] = sorted(completed)
                _save_row_state(output_dir_path, row_id, state)
                row["completed_count"] = str(len(completed))
                row["last_error"] = ""
            else:
                row["last_error"] = f"target_failed={target} exit_code={exit_code}"
                print("[!] quatvn page target failed")

        state["completed_targets"] = sorted(completed)
        _save_row_state(output_dir_path, row_id, state)
        row["completed_count"] = str(len(completed))
        if len(completed) >= len(page_targets):
            row["proceed_status"] = args.downloaded_value
            row["last_error"] = ""
            print(f"[+] completed all {len(page_targets)} quatvn page target(s)")
        else:
            print(f"[!] completed {len(completed)}/{len(page_targets)} quatvn page target(s)")
        return

    try:
        capture = capture_page(
            url=page_url,
            wait_seconds=max(args.wait, args.quatvn_discovery_wait),
            headless=not args.quatvn_headed,
            try_play=True,
        )
    except Exception as exc:
        row["last_error"] = f"capture_failed={exc}"
        print(f"[!] quatvn capture failed: {exc}")
        return

    quatvn_candidates = extract_quatvn_stream_candidates(capture, page_url=page_url)
    targets = [candidate.url for candidate in quatvn_candidates]
    if not targets:
        row["last_error"] = "no_quatvn_stream_candidates"
        print("[!] no quatvn stream assets found in capture")
        return

    state["page_url"] = page_url
    state["discovered_targets"] = targets
    row["target_count"] = str(len(targets))

    pending_targets = [target for target in targets if target not in completed]
    row["completed_count"] = str(len(completed))

    if not pending_targets:
        row["completed_count"] = str(len(targets))
        row["proceed_status"] = args.downloaded_value
        row["last_error"] = ""
        _save_row_state(output_dir_path, row_id, state | {"completed_targets": sorted(completed)})
        print(f"[i] all {len(targets)} quatvn target(s) already done -> mark {args.downloaded_value}")
        return

    for idx, target in enumerate(pending_targets, start=1):
        print(f"[quatvn {idx}/{len(pending_targets)}] {target}")
        candidate = next((item for item in quatvn_candidates if item.url == target), None)
        if candidate is None:
            row["last_error"] = f"missing_candidate={target}"
            print(f"[!] missing candidate for target: {target}")
            continue

        if args.dry_run:
            completed.add(target)
            row["completed_count"] = str(len(completed))
            continue

        try:
            out_file = download_with_ffmpeg(
                capture=capture,
                candidate=candidate,
                output_dir=output_dir_path,
                metadata_capture=capture,
            )
            completed.add(target)
            state["completed_targets"] = sorted(completed)
            _save_row_state(output_dir_path, row_id, state)
            row["completed_count"] = str(len(completed))
            row["last_error"] = ""
            print(f"[+] downloaded: {out_file}")
        except Exception as exc:
            row["last_error"] = f"target_failed={target} error={exc}"
            print(f"[!] quatvn target failed -> {exc}")

    state["completed_targets"] = sorted(completed)
    _save_row_state(output_dir_path, row_id, state)
    row["completed_count"] = str(len(completed))

    if len(completed) >= len(targets):
        row["proceed_status"] = args.downloaded_value
        row["last_error"] = ""
        print(f"[+] completed all {len(targets)} quatvn target(s)")
    else:
        print(f"[!] completed {len(completed)}/{len(targets)} quatvn target(s)")


def main() -> int:
    args = parse_args()
    csv_path = (ROOT / args.csv).resolve()

    if not csv_path.exists():
        print(f"[!] CSV not found: {csv_path}")
        return 2

    raw_rows = load_rows(csv_path)
    rows, fieldnames = _normalize_rows(raw_rows, args.pending_value)
    output_dir_arg = _resolve_output_dir(args, rows)
    output_dir_path = (ROOT / output_dir_arg).resolve()
    output_dir_path.mkdir(parents=True, exist_ok=True)

    print(f"[i] CSV: {csv_path}")
    print(f"[i] output_dir={output_dir_path}")
    print(f"[i] batch_size={args.batch_size}, start_order={args.start_order}")

    batch_no = 0
    while True:
        pending_rows = [
            row
            for row in rows
            if _row_is_pending(row, args) and parse_order(row) >= args.start_order
        ]
        selected = pending_rows[: max(args.batch_size, 0)]
        if not selected:
            print("[+] no pending rows left, done")
            break

        batch_no += 1
        print(f"[i] batch {batch_no}: {len(selected)} row(s)")
        before_batch = [
            (
                row.get("id", ""),
                row.get("proceed_status", ""),
                row.get("completed_count", ""),
                row.get("last_error", ""),
            )
            for row in selected
        ]

        for idx, row in enumerate(selected, start=1):
            order = (row.get("order") or "").strip()
            video_id = (row.get("id") or "").strip()
            url = (row.get("url") or "").strip()
            host = (urlparse(url).hostname or "").lower()
            print(f"[batch {batch_no} {idx}/{len(selected)}] order={order} id={video_id} host={host}")

            if is_quatvn_url(url):
                _process_quatvn_row(args, ROOT, row, output_dir_path, output_dir_arg)
            else:
                _process_standard_row(args, ROOT, row, output_dir_path, output_dir_arg)

            if not args.dry_run:
                save_rows(csv_path, rows, fieldnames)

        if args.dry_run:
            print("[i] dry-run: stop after first batch preview")
            break

        after_batch = [
            (
                row.get("id", ""),
                row.get("proceed_status", ""),
                row.get("completed_count", ""),
                row.get("last_error", ""),
            )
            for row in selected
        ]
        if after_batch == before_batch:
            print("[!] no progress in this batch, stop to avoid infinite retry loop")
            break

    print("[+] batch run complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
