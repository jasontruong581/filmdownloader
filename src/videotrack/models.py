from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass
class NetworkRequest:
    url: str
    method: str
    headers: dict[str, str]
    resource_type: str | None = None
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class CaptureResult:
    page_url: str
    final_url: str
    title: str
    user_agent: str
    cookies: dict[str, str]
    requests: list[NetworkRequest]

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_url": self.page_url,
            "final_url": self.final_url,
            "title": self.title,
            "user_agent": self.user_agent,
            "cookies": self.cookies,
            "requests": [
                {
                    "url": req.url,
                    "method": req.method,
                    "headers": req.headers,
                    "resource_type": req.resource_type,
                    "status": req.status,
                    "response_headers": req.response_headers,
                }
                for req in self.requests
            ],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "CaptureResult":
        requests = [
            NetworkRequest(
                url=req["url"],
                method=req.get("method", "GET"),
                headers=req.get("headers", {}),
                resource_type=req.get("resource_type"),
                status=req.get("status"),
                response_headers=req.get("response_headers", {}),
            )
            for req in data.get("requests", [])
        ]
        return CaptureResult(
            page_url=data["page_url"],
            final_url=data.get("final_url", data["page_url"]),
            title=data.get("title", ""),
            user_agent=data.get("user_agent", ""),
            cookies=data.get("cookies", {}),
            requests=requests,
        )


@dataclass
class StreamCandidate:
    url: str
    kind: str
    score: int
    source: str
    status_code: int | None = None
    content_type: str | None = None
    host: str | None = None
    probe_duration: float | None = None
    probe_bitrate: int | None = None
    validation_note: str | None = None

    def __post_init__(self) -> None:
        if self.host is None:
            self.host = (urlparse(self.url).hostname or "").lower() or None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "score": self.score,
            "source": self.source,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "host": self.host,
            "probe_duration": self.probe_duration,
            "probe_bitrate": self.probe_bitrate,
            "validation_note": self.validation_note,
        }
