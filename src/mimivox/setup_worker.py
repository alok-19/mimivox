"""Child process that fetches model repos into the Hugging Face cache.

    python -m mimivox.setup_worker REPO_ID [REPO_ID ...]

Run as a child so the setup job can stop it. On failure it prints one JSON line
``{"reason": ..., "message": ...}`` to stderr and exits 1.
"""

from __future__ import annotations

import json
import os
import sys

from mimivox.models_service import fetch_repos
from mimivox.parent_watch import watch_parent
from mimivox.setup_service import classify_error


def main(repo_ids: list[str]) -> int:
    watch_parent(int(os.environ["MIMIVOX_SERVER_PID"]))
    try:
        fetch_repos(repo_ids)
    except Exception as exc:
        print(json.dumps({"reason": classify_error(exc), "message": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
