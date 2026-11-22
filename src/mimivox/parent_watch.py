"""Exit when the process that launched us is gone, so no server outlives its app."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watch_parent(
    pid: int,
    interval: float = 1.0,
    on_gone: Callable[[], None] = lambda: os._exit(0),
) -> threading.Thread:
    """Poll ``pid`` every ``interval`` seconds on a daemon thread; call ``on_gone`` once it exits."""

    def loop() -> None:
        while _alive(pid):
            time.sleep(interval)
        on_gone()

    thread = threading.Thread(target=loop, name="parent-watch", daemon=True)
    thread.start()
    return thread
