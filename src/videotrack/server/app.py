"""The FastAPI application.

Routes are mounted under /api. The built frontend is served from `static/` when
it exists, so the API runs before anything has been built, which is what lets a
clean checkout start the server and read /api/health first.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .routes import router
from .security import make_guard, verify_configuration
from .settings import Settings, load_settings
from .state import ServerState

STATIC_DIR = Path(__file__).resolve().parent / "static"

API_PREFIX = "/api"


def static_dir() -> Path:
    return STATIC_DIR


def create_app(
    settings: Settings | None = None,
    db_path: Path | str | None = None,
    token: str | None = None,
    settings_path: Path | str | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    verify_configuration(settings, token)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = ServerState.build(settings, db_path=db_path, settings_path=settings_path)
        # A job left running by a previous process is not complete.
        state.manager.recover_interrupted()
        app.state.server = state
        try:
            yield
        finally:
            state.shutdown()

    app = FastAPI(
        title="FilmDownloader",
        version="0.2.0",
        summary="Authorized media resolution, queueing, and download",
        lifespan=lifespan,
    )
    app.include_router(router, prefix=API_PREFIX, dependencies=[Depends(make_guard(settings, token))])
    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    directory = static_dir()
    index = directory / "index.html"
    if not index.exists():
        @app.get("/", include_in_schema=False)
        async def missing_frontend() -> JSONResponse:
            return JSONResponse(
                {
                    "reason": "frontend_not_built",
                    "message": "Build the web UI with: cd web && npm ci && npm run build",
                    "api": f"{API_PREFIX}/health",
                }
            )

        return

    app.mount("/assets", StaticFiles(directory=directory / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        """Serve a real file when it exists, otherwise the SPA entry point.

        The fallback is what makes a hard refresh on a client route work. It must
        not swallow the API surface, though: an unknown /api path has to answer
        with a JSON 404, not with the SPA shell, or a client bug looks like a
        successful request.
        """
        if path == "api" or path.startswith("api/"):
            return JSONResponse(
                {"reason": "unknown_endpoint", "message": "No such API endpoint."},
                status_code=404,
            )

        candidate = (directory / path).resolve()
        if path and candidate.is_file() and str(candidate).startswith(str(directory.resolve())):
            return FileResponse(candidate)
        return FileResponse(index)


def openapi_json(app: FastAPI | None = None) -> str:
    """Dump the schema without starting a server.

    Frontend types are generated from this file, so neither the build nor CI has
    to bring a server up and race on a port.
    """
    app = app or create_app(Settings())
    return json.dumps(app.openapi(), indent=2)
