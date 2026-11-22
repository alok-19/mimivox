"""mimivox.server — Local API for the MimiVox helper."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import ipaddress
import json
import os
import secrets
import sys
from contextlib import ExitStack, asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mimivox import learning
from mimivox.api_adapter import api_state
from mimivox.api_adapter import router as api_router
from mimivox.parent_watch import watch_parent
from mimivox.privacy import RetentionSweeper, apply_offline_lock
from mimivox.setup_service import setup_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load persisted MeetingSettings + active glossary before serving.
    try:
        api_state.load_persisted_settings()
    except Exception as exc:
        print(f"⚠️  settings load failed (using defaults): {exc}")

    if learning.learning_enabled(api_state.settings):
        await asyncio.to_thread(learning.warm)

    # Lock offline before any model load so a cached model never triggers a fetch.
    lock = apply_offline_lock(api_state.settings)
    print(f"🔒 Offline lock: {'on' if lock['offline_locked'] else 'off'} ({lock['reason']})")
    sweeper = RetentionSweeper(lambda: api_state.settings)
    await asyncio.to_thread(sweeper.start)

    loop = asyncio.get_running_loop()
    try:
        from mimivox.session_archive import backfill_index

        stats = await loop.run_in_executor(None, backfill_index)
        print(f"📚 Meeting library index: {stats}")
    except Exception as exc:
        print(f"⚠️  library index backfill failed: {exc}")
    setup_manager.settings_fn = lambda: api_state.settings
    setup_manager.emit = lambda event: api_state.emit(event, store=False)
    setup_manager.before_load = lambda: apply_offline_lock(api_state.settings)
    await setup_manager.boot()
    print(f"✅ Server accepting requests (model setup: {setup_manager.state.phase})\n")
    yield
    await setup_manager.shutdown()
    sweeper.stop()
    if api_state.pipeline_task and not api_state.pipeline_task.done():
        api_state.pipeline_task.cancel()


API_TOKEN_ENV = "MIMIVOX_API_TOKEN"

app = FastAPI(title="MimiVox", lifespan=lifespan, openapi_url=None)
app.state.api_token = os.environ.get(API_TOKEN_ENV) or secrets.token_urlsafe(32)
app.include_router(api_router)


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@app.middleware("http")
async def require_local_bearer(request: Request, call_next):
    if not _is_loopback_host(request.url.hostname):
        return JSONResponse({"detail": "Invalid host"}, status_code=421)

    authorization = request.headers.get("authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    expected = request.app.state.api_token
    if (
        scheme.lower() != "bearer"
        or not supplied
        or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    ):
        return JSONResponse(
            {"detail": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def _native_credentials() -> tuple[str, bytes]:
    data = sys.stdin.buffer.read(8_193)
    if len(data) > 8_192:
        raise ValueError("credentials exceed the size limit")
    credentials = json.loads(data)
    token = credentials["token"]
    pem = base64.b64decode(credentials["pem"], validate=True)
    if not isinstance(token, str) or not token or not 0 < len(pem) <= 4_096:
        raise ValueError("invalid helper credentials")
    return token, pem


def _tls_pipe(pem: bytes, resources: ExitStack) -> str:
    read_fd, write_fd = os.pipe()
    resources.callback(os.close, read_fd)
    with os.fdopen(write_fd, "wb") as stream:
        stream.write(pem)
    return f"/dev/fd/{read_fd}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="MimiVox local API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help="exit when this process exits (the macOS app passes its pid)",
    )
    args = parser.parse_args()
    if not _is_loopback_host(args.host):
        parser.error("--host must be localhost or a loopback IP address")

    with ExitStack() as resources:
        certfile = keyfile = None
        if args.parent_pid is not None:
            watch_parent(args.parent_pid)
            try:
                token, pem = _native_credentials()
            except (KeyError, OSError, TypeError, ValueError):
                parser.error("the macOS helper requires valid launch credentials on standard input")
            app.state.api_token = token
            certfile = _tls_pipe(pem, resources)
            keyfile = _tls_pipe(pem, resources)
        else:
            token = os.environ.get(API_TOKEN_ENV)
            if not token:
                parser.error(f"set {API_TOKEN_ENV} for standalone API access")
            app.state.api_token = token

        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            reload=False,
            log_level="warning",
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
        )


if __name__ == "__main__":
    main()
