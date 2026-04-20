"""FastAPI app factory for the satdeploy dashboard.

The module-level ``app`` is constructed from env vars when this module is
imported as ``satdeploy.dashboard.app:app`` (the uvicorn entrypoint).
Tests call :func:`create_app` directly with explicit parameters to avoid
env-var setup.
"""

from __future__ import annotations

import hmac as hmac_mod
import os
import subprocess
import sys
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from satdeploy.dashboard import git_utils, security
from satdeploy.history import History


def _tile_state(record) -> str:
    """Classify a DeploymentRecord into the tile's colour bucket."""
    if not record:
        return "unknown"
    if record.action == "rollback":
        return "rolled-back"
    if not record.success:
        return "failed"
    return "deployed"


def _fetch_iteration_rows(db_path: Path, file_hash: str) -> list[dict]:
    """Return every deployment row carrying ``file_hash``, newest first."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "SELECT * FROM deployments WHERE file_hash = ? ORDER BY timestamp DESC",
            (file_hash,),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _is_hash_live(db_path: Path, module: str, app_name: str, file_hash: str) -> tuple[bool, Optional[str]]:
    """Is ``file_hash`` still the current deployment for (module, app)?

    Returns ``(is_live, superseding_hash)``. ``superseding_hash`` is ``None``
    when live, otherwise the file_hash that currently runs on that target.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT file_hash FROM deployments "
            "WHERE module = ? AND app = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (module, app_name),
        ).fetchone()
        if row is None:
            return (False, None)
        current = row[0]
        return (current == file_hash, current if current != file_hash else None)
    finally:
        conn.close()


def _resolve_diff(git_hash: Optional[str]) -> tuple[str, Optional[str]]:
    """Determine which of the 7 design-review states the diff section is in.

    Returns ``(state, content)`` where state is one of:
    ``"ok"`` (diff renders), ``"none"`` (no git_hash), ``"not_local"``
    (commit not fetchable from the dashboard host's git repo).
    """
    if not git_hash:
        return ("none", None)
    try:
        return ("ok", git_utils.git_show(git_hash))
    except git_utils.GitLookupError:
        return ("not_local", None)


def create_app(
    db_path: Path,
    secret: str,
    config_path: Optional[Path] = None,
) -> FastAPI:
    app = FastAPI(title="satdeploy dashboard", docs_url=None, redoc_url=None)

    tpl_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"
    templates = Jinja2Templates(directory=str(tpl_dir))
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.state.db_path = db_path
    app.state.secret = secret
    app.state.config_path = config_path
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        history = History(db_path)
        fleet = history.get_fleet_status()
        tiles = []
        for module, apps in fleet.items():
            for app_name, record in sorted(apps.items()):
                tiles.append({
                    "module": module,
                    "app": app_name,
                    "state": _tile_state(record),
                    "record": record,
                })
        events = history.get_all_history(limit=20)
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context={"tiles": tiles, "events": events},
        )

    @app.get("/api/ticker", response_class=HTMLResponse)
    def ticker(request: Request):
        history = History(db_path)
        events = history.get_all_history(limit=20)
        return templates.TemplateResponse(
            request=request,
            name="_ticker.html",
            context={"events": events},
        )

    @app.get("/iterations/{file_hash}", response_class=HTMLResponse)
    def iteration(request: Request, file_hash: str):
        rows = _fetch_iteration_rows(db_path, file_hash)
        if not rows:
            return templates.TemplateResponse(
                request=request,
                name="iteration_404.html",
                context={"file_hash": file_hash},
                status_code=404,
            )

        primary = rows[0]
        is_live, superseding = _is_hash_live(
            db_path, primary["module"], primary["app"], file_hash
        )

        diff_state, diff_content = _resolve_diff(primary.get("git_hash"))

        # Latest rollback that failed — shown as a red banner. Template scans
        # in reverse-chronological order so the most recent failure wins.
        failed_rollback = next(
            (e for e in rows if e["action"] == "rollback" and not e["success"]),
            None,
        )

        rollback_token = security.sign_rollback(secret, file_hash)
        confirm_string = security.expected_confirm_string(
            primary["app"], primary["module"], file_hash
        )

        return templates.TemplateResponse(
            request=request,
            name="iteration.html",
            context={
                "file_hash": file_hash,
                "primary": primary,
                "events": rows,
                "is_live": is_live,
                "superseding": superseding,
                "diff_state": diff_state,
                "diff": diff_content,
                "failed_rollback": failed_rollback,
                "rollback_token": rollback_token,
                "confirm_string": confirm_string,
            },
        )

    @app.post("/api/rollback")
    @limiter.limit("1/second")
    async def rollback_endpoint(request: Request,
                                 x_satdeploy_token: Optional[str] = Header(default=None)):
        # Layer 1: shared-secret header. Constant-time to avoid timing attacks.
        if not x_satdeploy_token or not hmac_mod.compare_digest(
            x_satdeploy_token.encode(), secret.encode()
        ):
            raise HTTPException(status_code=403, detail="invalid or missing X-Satdeploy-Token")

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="request body must be JSON")

        file_hash = body.get("file_hash")
        hmac_token = body.get("token")
        confirm_str = body.get("confirm")
        if not (file_hash and hmac_token and confirm_str):
            raise HTTPException(
                status_code=400,
                detail="missing required fields: file_hash, token, confirm",
            )

        # Layer 2: hash-scoped HMAC with time-bucket expiry. Defends against a
        # leaked secret being used to roll back an iteration the attacker never
        # visited a page for.
        if not security.verify_rollback(secret, file_hash, hmac_token):
            raise HTTPException(
                status_code=403,
                detail="stale or invalid HMAC token (refresh the iteration page)",
            )

        rows = _fetch_iteration_rows(db_path, file_hash)
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"no iteration with file_hash={file_hash!r}",
            )
        primary = rows[0]

        # Layer 3: the exact confirm string the user typed in the modal. Even
        # with both tokens, a scripted attacker has to know app@target@hash.
        expected_confirm = security.expected_confirm_string(
            primary["app"], primary["module"], file_hash
        )
        if confirm_str.strip() != expected_confirm:
            raise HTTPException(
                status_code=400,
                detail=f"confirmation mismatch; expected {expected_confirm!r}",
            )

        # Shell out to the existing CLI rollback. SATDEPLOY_SOURCE=web makes
        # History.record() write source="web" on the audit row so the iteration
        # timeline distinguishes browser-initiated from CLI-initiated actions.
        cmd = [sys.executable, "-m", "satdeploy", "rollback",
               primary["app"], file_hash]
        if config_path:
            cmd.extend(["--config", str(config_path)])

        subprocess_env = {**os.environ, "SATDEPLOY_SOURCE": "web"}
        try:
            proc = subprocess.run(
                cmd,
                env=subprocess_env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return JSONResponse(
                status_code=504,
                content={"ok": False, "error": "rollback timed out after 60s"},
            )

        if proc.returncode != 0:
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": (proc.stderr.strip() or "rollback failed")[:2000],
                },
            )

        return {"ok": True, "redirect": f"/iterations/{file_hash}"}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


def _from_env() -> FastAPI:
    db = os.environ.get("SATDEPLOY_DASHBOARD_DB")
    secret = os.environ.get("SATDEPLOY_DASHBOARD_SECRET")
    if not db or not secret:
        raise RuntimeError(
            "SATDEPLOY_DASHBOARD_DB and SATDEPLOY_DASHBOARD_SECRET must be set. "
            "Use `satdeploy dashboard` rather than running uvicorn directly."
        )
    config_path = os.environ.get("SATDEPLOY_DASHBOARD_CONFIG")
    return create_app(
        Path(db),
        secret,
        config_path=Path(config_path) if config_path else None,
    )


if os.environ.get("SATDEPLOY_DASHBOARD_DB"):
    app = _from_env()
else:
    app = None  # populated lazily by uvicorn or by tests via create_app()
