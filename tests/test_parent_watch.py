"""The server exits once the app that launched it is gone."""

import contextlib
import os
import signal
import subprocess
import sys
import time

CHILD = """
import sys, time
from mimivox.parent_watch import watch_parent
watch_parent(int(sys.argv[1]), interval=0.1)
time.sleep(30)
"""


def _wait(proc: subprocess.Popen, seconds: float) -> int | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return proc.returncode
        time.sleep(0.05)
    return None


def test_child_exits_when_parent_dies() -> None:
    parent = subprocess.Popen(["sleep", "30"])
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(parent.pid)])
    try:
        assert _wait(child, 0.5) is None, "child must keep running while the parent lives"
        parent.kill()
        parent.wait()  # reap, or the pid still exists as a zombie
        assert _wait(child, 2.0) == 0
    finally:
        parent.kill()
        child.kill()


WORKER_PARENT = """
import os, subprocess, sys, time
worker = subprocess.Popen(
    [sys.executable, "-c", sys.argv[1]], env={**os.environ, "MIMIVOX_SERVER_PID": str(os.getpid())}
)
print(worker.pid, flush=True)
time.sleep(30)
"""

STALLED_WORKER = """
import sys, time
from mimivox import setup_worker
setup_worker.fetch_repos = lambda repo_ids: time.sleep(30)
sys.exit(setup_worker.main(["org/model"]))
"""


def _gone(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_setup_worker_exits_when_the_server_is_killed() -> None:
    server = subprocess.Popen(
        [sys.executable, "-c", WORKER_PARENT, STALLED_WORKER], stdout=subprocess.PIPE, text=True
    )
    worker_pid = int(server.stdout.readline())
    try:
        assert not _gone(worker_pid, 0.5), "the worker must keep running while the server lives"
        server.send_signal(signal.SIGKILL)
        server.wait()
        assert _gone(worker_pid, 2.5)
    finally:
        server.kill()
        with contextlib.suppress(ProcessLookupError):
            os.kill(worker_pid, signal.SIGKILL)
