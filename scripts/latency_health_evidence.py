"""Drive a replay-file session behind on purpose and record health, catch-up and recovery.

    lockf -k /tmp/claude-501/mimivox-models.lock \\
      env PYTHONPATH=src python scripts/latency_health_evidence.py WAV --delay cat:12:1 --port 8791

Starts its own server with MIMIVOX_DEBUG_DELAY (see mimivox.latency_health.DebugDelay),
starts a replay-file session (never live audio), polls /api/status, presses Catch up
once when the state leaves ok, and after the session ends runs /api/sessions/<id>/recover.
Writes <out>/timeline.tsv, <out>/events.jsonl and <out>/summary.json, and always stops
the server it started (by pid).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def call(base: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, method="POST" if body is not None else "GET"
    )
    if data:
        req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("wav")
    ap.add_argument("--delay", default="cat:12:1", help="MIMIVOX_DEBUG_DELAY value")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--seconds", type=float, default=70, help="replay duration")
    ap.add_argument("--out", type=Path, default=Path("/tmp/claude-501/latency-health"))
    ap.add_argument("--mode", default=None, help="force catch-up mode: fast | skip")
    ap.add_argument("--no-catch-up", action="store_true", help="observe only (control run)")
    args = ap.parse_args()
    if args.port == 8765:
        sys.exit("refusing port 8765")
    args.out.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{args.port}"
    env = {
        **os.environ,
        "MIMIVOX_DEBUG_DELAY": args.delay,
        "PYTHONPATH": os.environ.get("PYTHONPATH", "src"),
    }
    log = open(args.out / "server.log", "w")  # noqa: SIM115 - handle outlives Popen; closed in finally
    server = subprocess.Popen(
        [sys.executable, "-m", "mimivox.server", "--port", str(args.port)],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    print(f"server pid {server.pid} port {args.port} delay {args.delay}", flush=True)
    events: list[tuple[float, str]] = []
    try:
        deadline = time.time() + 300
        while True:
            try:
                call(base, "/api/status", timeout=3)
                break
            except Exception:
                if server.poll() is not None or time.time() > deadline:
                    raise SystemExit("server did not come up; see server.log") from None
                time.sleep(1)

        t0 = time.time()

        def listen() -> None:
            try:
                with urllib.request.urlopen(base + "/api/events", timeout=600) as resp:
                    for raw in resp:
                        line = raw.decode().strip()
                        if line.startswith("data: "):
                            events.append((time.time() - t0, line[6:]))
            except Exception:
                pass

        threading.Thread(target=listen, daemon=True).start()
        call(
            base,
            "/api/settings",
            {"replay_duration_seconds": args.seconds, "catch_up_mode": "manual"},
        )
        started = call(base, "/api/session/start", {"source": str(Path(args.wav).resolve())})
        session_id = started["session_id"]
        print("session", session_id, flush=True)

        timeline: list[str] = ["t_s\tstate\tbacklog_s\tcatching_up\ttop_reason"]
        catch_up: dict | None = None
        pressed_at = None
        states_seen: list[str] = []
        end_by = time.time() + args.seconds + 240
        while time.time() < end_by:
            st = call(base, "/api/status")
            lat = st["health"]["latency"]
            t = time.time() - t0
            reason = (lat["reasons"] or [{"message": ""}])[0]["message"]
            timeline.append(
                f"{t:.1f}\t{lat['state']}\t{lat['backlog_s']:.1f}\t{lat['catching_up']}\t{reason}"
            )
            if not states_seen or states_seen[-1] != lat["state"]:
                states_seen.append(lat["state"])
            if catch_up is None and lat["catch_up_available"] and not args.no_catch_up:
                body = {"mode": args.mode} if args.mode else {}
                catch_up = call(base, "/api/session/catch-up", body)
                pressed_at = t
                timeline.append(
                    f"{t:.1f}\tCATCH-UP\t{json.dumps(catch_up['health']['backlog_s'])}\t{catch_up['mode']}\t"
                    f"skipped {catch_up['translations_skipped']} translations, {catch_up['gaps']} gaps"
                )
            if st["state"] in ("completed", "idle", "failed") and t > 3:
                break
            time.sleep(0.5)
        archived = None
        for _ in range(240):
            try:
                archived = call(base, f"/api/sessions/{session_id}")
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                time.sleep(0.5)
        if archived is None:
            raise SystemExit("session was never archived")
        before = [
            e
            for e in archived["events"]
            if e["type"] in ("AsrCompletedEvent", "TranslationCompletedEvent", "CaptionGapEvent")
        ]
        recovered = call(base, f"/api/sessions/{session_id}/recover", {}, timeout=600)
        after = call(base, f"/api/sessions/{session_id}")["events"]
        skipped_before = [
            e for e in before if e["type"] == "CaptionGapEvent" or e.get("translation_skipped")
        ]
        skipped_after = [
            e for e in after if e["type"] == "CaptionGapEvent" or e.get("translation_skipped")
        ]
        summary = {
            "delay": args.delay,
            "states_seen": states_seen,
            "catch_up_pressed_at_s": pressed_at,
            "catch_up": catch_up,
            "skipped_before_recovery": len(skipped_before),
            "recovery": recovered,
            "skipped_after_recovery": len(skipped_after),
            "gap_events": [e for e in before if e["type"] == "CaptionGapEvent"],
            "asr_lines": sum(1 for e in before if e["type"] == "AsrCompletedEvent"),
            "translated_lines_before": sum(
                1 for e in before if e["type"] == "TranslationCompletedEvent"
            ),
            "translated_lines_after": sum(
                1 for e in after if e["type"] == "TranslationCompletedEvent"
            ),
            "sse_health_events": sum(1 for _, e in events if '"LatencyHealthEvent"' in e),
        }
        (args.out / "timeline.tsv").write_text("\n".join(timeline) + "\n")
        (args.out / "events.jsonl").write_text("".join(f"{t:.2f}\t{e}\n" for t, e in events))
        (args.out / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        print(f"server pid {server.pid} stopped", flush=True)


if __name__ == "__main__":
    main()
