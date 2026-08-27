"""Run the server: python -m videotrack.server"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from .app import create_app, openapi_json
from .security import InsecureConfiguration
from ..core.env import load_env_file
from ..logs import ENV_LOG_LEVEL, configure_console_encoding, configure_logging
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
    parser.add_argument(
        "--log-level",
        default="",
        help=f"Progress detail: DEBUG, INFO, WARNING. Default INFO, or ${ENV_LOG_LEVEL}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Before anything reads a setting or a path, so the documented `.env` file
    # actually reaches them. Real environment variables still win.
    load_env_file()
    # Before the first print, for the reason the CLI does it: a title from
    # the page being downloaded is not encodable in a Windows codepage.
    configure_console_encoding()
    args = build_parser().parse_args(argv)

    if args.dump_openapi:
        # No handler here: stdout is the deliverable and stderr should stay quiet.
        from pathlib import Path

        Path(args.dump_openapi).write_text(openapi_json(), encoding="utf-8")
        print(f"[+] wrote {args.dump_openapi}")
        return 0

    level = configure_logging(args.log_level)

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
    print(f"[i] Progress detail: {level}. A browser capture takes 30-60s and says so as it goes.")
    if not settings.is_loopback:
        print("[i] Bound off loopback; every /api request needs the configured token.")

    if args.open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
