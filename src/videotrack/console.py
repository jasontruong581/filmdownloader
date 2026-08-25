"""Render pipeline events as the terminal output the CLI has always produced.

Every field is read defensively. A renderer is the last thing that should be
able to end a download, so a malformed payload degrades to "?" rather than
raising out of the command.

The strings are moved verbatim from where the pipeline used to print them, not
rewritten: a difference here would read as a regression to anyone watching the
command run.
"""

from __future__ import annotations

from .core.events import (
    CANDIDATE_ATTEMPT,
    CANDIDATE_REJECTED,
    CANDIDATES_FOUND,
    DOWNLOAD_COMPLETED,
    FAILED,
    INFO,
    PROGRESS,
    STAGE_STARTED,
    PipelineEvent,
)

_NO_CANDIDATE_MESSAGES = {
    "no_candidates": "No stream candidate found after analysis.",
    "all_candidates_failed": "All candidate attempts failed.",
}


def print_event(event: PipelineEvent) -> None:
    payload = event.payload

    if event.kind in {INFO, STAGE_STARTED}:
        message = payload.get("message")
        if message:
            print(f"[i] {message}")
        return

    if event.kind == CANDIDATES_FOUND:
        return

    if event.kind == CANDIDATE_ATTEMPT:
        index = payload.get("index", "?")
        total = payload.get("total", "?")
        print(f"[+] Try {index}/{total}: {payload.get('kind', '?')} | {payload.get('url', '?')}")
        return

    if event.kind == CANDIDATE_REJECTED:
        if payload.get("reason") == "too_short":
            duration = payload.get("duration")
            rendered = f"{duration:.1f}s" if isinstance(duration, (int, float)) else "?"
            print(
                f"[!] Reject short output ({rendered} < {payload.get('minimum', '?')}s). "
                "Try next candidate."
            )
        else:
            print(f"[!] Failed candidate: {payload.get('error')}")
        return

    if event.kind == DOWNLOAD_COMPLETED:
        print(f"[+] Downloaded: {payload.get('path', '?')}")
        return

    if event.kind == FAILED:
        message = _NO_CANDIDATE_MESSAGES.get(payload.get("reason", ""))
        if message:
            print(f"[!] {message}")
        if payload.get("error"):
            print(f"[!] Last error: {payload['error']}")
        return

    if event.kind == PROGRESS:
        percent = payload.get("percent")
        phase = payload.get("phase", "downloading")
        rendered = f"{percent:.1f}%" if isinstance(percent, (int, float)) else "?"
        print(f"[i] {phase}: {rendered}", end="\r", flush=True)
        return
