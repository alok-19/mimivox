"""mimivox.server — FastAPI + SSE web interface for the JA→EN pipeline."""

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
import threading
from collections.abc import AsyncGenerator
from contextlib import ExitStack, asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from mimivox import learning
from mimivox.api_adapter import api_state, models_not_ready
from mimivox.api_adapter import router as api_router
from mimivox.languages import UnsupportedPair, resolve_pair
from mimivox.pair_translate import unready_reason
from mimivox.parent_watch import watch_parent
from mimivox.pipeline import run
from mimivox.privacy import RetentionSweeper, apply_offline_lock, saves_transcripts
from mimivox.settings_store import apply_settings_for_session
from mimivox.setup_service import setup_manager


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------
class _AppState:
    def __init__(self) -> None:
        self.pipeline_task: asyncio.Task | None = None
        self._clients: list[asyncio.Queue] = []
        self._lock = threading.Lock()

    def add_client(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._clients.append(q)

    def remove_client(self, q: asyncio.Queue) -> None:
        with self._lock, suppress(ValueError):
            self._clients.remove(q)

    def broadcast(self, event: dict) -> None:
        data = json.dumps(event, ensure_ascii=False)
        with self._lock:
            clients = list(self._clients)
        dead: list[asyncio.Queue] = []
        for q in clients:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.remove_client(q)


_app_state = _AppState()


def _dual_broadcast(event: dict) -> None:
    """Web UI + Swift /api/events share the same pipeline events."""
    api_state.dual_broadcast(event)


# ---------------------------------------------------------------------------
# App lifespan — bind first; models load in the background (setup_service)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Wire adapter hooks (avoids circular imports at module load).
    api_state.web_broadcast = _app_state.broadcast
    api_state.on_pipeline_task = lambda task: setattr(_app_state, "pipeline_task", task)

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
    if _app_state.pipeline_task and not _app_state.pipeline_task.done():
        _app_state.pipeline_task.cancel()
    if api_state.pipeline_task and not api_state.pipeline_task.done():
        api_state.pipeline_task.cancel()


API_TOKEN_ENV = "MIMIVOX_API_TOKEN"
PUBLIC_PATHS = frozenset({"/"})

app = FastAPI(title="MimiVox", lifespan=lifespan)
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

    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

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


# ---------------------------------------------------------------------------
# Static HTML
# ---------------------------------------------------------------------------
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = _STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------
@app.get("/stream")
async def stream(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _app_state.add_client(q)

    async def event_generator() -> AsyncGenerator[str, None]:
        yield ": connected\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _app_state.remove_client(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Control endpoint
# ---------------------------------------------------------------------------
@app.get("/devices")
async def devices():
    try:
        import sounddevice as sd
    except ImportError:
        return JSONResponse({"devices": []})
    inputs = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            raw = d["name"]
            clean = raw.split()[0]
            inputs.append({"index": i, "name": clean, "raw": raw})
    return JSONResponse({"devices": inputs})


@app.post("/control")
async def control(body: dict):
    action = body.get("action", "")

    if action == "start":
        if not setup_manager.is_ready:
            return models_not_ready()
        if _app_state.pipeline_task and not _app_state.pipeline_task.done():
            return JSONResponse({"status": "already_running"})

        wav = body.get("wav") or None
        live = bool(body.get("live", False))
        device = body.get("device") or None

        if not live and not wav:
            return JSONResponse(
                {"status": "error", "detail": "Provide 'wav' path or set 'live':true"},
                status_code=400,
            )

        if wav and not Path(wav).exists():
            return JSONResponse(
                {"status": "error", "detail": f"WAV not found: {wav}"}, status_code=400
            )

        try:
            pair = resolve_pair(api_state.settings, body)
        except UnsupportedPair as exc:
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=400)
        blocked = unready_reason(pair)
        if blocked:
            return JSONResponse({"status": "error", "detail": blocked}, status_code=400)
        apply_settings_for_session(
            {**api_state.settings, "source_lang": pair.source, "target_lang": pair.target}
        )
        api_state.session_pair = pair

        _dual_broadcast(
            {
                "kind": "session_start",
                "wav": wav or "live",
                "source_lang": pair.source,
                "target_lang": pair.target,
            }
        )

        _app_state.pipeline_task = asyncio.create_task(
            run(
                wav_path=wav,
                live=live,
                asr_only=False,
                audio_device=device,
                sse_broadcast=_dual_broadcast,
                save_log=saves_transcripts(api_state.settings),
                show_transcript=False,
                pair=pair,
            )
        )
        api_state.pipeline_task = _app_state.pipeline_task
        api_state.state = "listening"

        def _on_done(task: asyncio.Task):
            with suppress(asyncio.CancelledError, Exception):
                task.result()
            api_state.state = "completed"
            _dual_broadcast({"kind": "session_end"})

        _app_state.pipeline_task.add_done_callback(_on_done)
        return JSONResponse({"status": "started"})

    elif action == "stop":
        if _app_state.pipeline_task and not _app_state.pipeline_task.done():
            _app_state.pipeline_task.cancel()
            api_state.state = "stopping"
            return JSONResponse({"status": "stopped"})
        return JSONResponse({"status": "not_running"})

    return JSONResponse({"status": "error", "detail": f"Unknown action: {action}"}, status_code=400)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    running = bool(_app_state.pipeline_task and not _app_state.pipeline_task.done())
    mem_mb: float | None = None
    try:
        import resource

        mem_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1)
    except Exception:
        pass
    return JSONResponse(
        {
            "status": "ok",
            "running": running,
            "clients": len(_app_state._clients),
            "mem_mb": mem_mb,
        }
    )


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
    parser = argparse.ArgumentParser(description="MimiVox Web Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser-link", action="store_true", help=argparse.SUPPRESS)
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
        elif not args.no_browser_link:
            host = f"[{args.host}]" if ":" in args.host else args.host
            token = quote(app.state.api_token, safe="")
            print(f"Open MimiVox in your browser: http://{host}:{args.port}/#token={token}")

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
