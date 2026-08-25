from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import CaptureResult, StreamCandidate


def save_capture(capture: CaptureResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(capture.to_dict(), f, ensure_ascii=False, indent=2)


def load_capture(path: Path) -> CaptureResult:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return CaptureResult.from_dict(data)


def save_candidates(candidates: list[StreamCandidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump([c.to_dict() for c in candidates], f, ensure_ascii=False, indent=2)


def save_json(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
