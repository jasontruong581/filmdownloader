"""Run the server: python -m videotrack.server"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from .app import create_app, openapi_json
from .security import InsecureConfiguration
from .settings import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="filmdownloader-server", description="Serve the download UI and API")
    parser.add_argument("--host", default="", help="Bind address. Default: loopback")
    parser.add_argument("--port", type=int, default=0, help="Bind port")
    parser.add_argument("--open-browser", action="store_true", help="Open the UI once the server is listening")
    parser.add_argument("--reload", action="store_true", help="Reload on code changes (development)")
    parser.add_argument(
        "--dump-openapi",
        default="",
        help="Write the OpenAPI schema to this path and exit, without starting a server",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.dump_openapi:
        from pathlib import Path

        Path(args.dump_openapi).write_text(openapi_json(), encoding="utf-8")
        print(f"[+] wrote {args.dump_openapi}")
        return 0

    settings = load_settings()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port

    try:
        app = create_app(settings)
    except InsecureConfiguration as exc:
        print(f"[!] {exc}")
        return 2

    url = f"http://{settings.host}:{settings.port}/"
    print(f"[+] Serving on {url}")
    if not settings.is_loopback:
        print("[i] Bound off loopback; every /api request needs the configured token.")

    if args.open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
