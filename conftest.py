"""Root pytest configuration.

This monorepo **cannot be collected by a single pytest process**, and this file exists
to say so clearly rather than let someone read a wall of `ImportPathMismatchError`.

Every service is an independent deployable that runs with its own directory on
`PYTHONPATH`, so each defines top-level `models`, `schemas`, `settings`, `services`,
`routers` and `tests` modules. Python caches imports by name: the first service to
import `models` wins, and every service collected after it would silently get somebody
else's tables. In practice collection fails before that, because two
`tests/conftest.py` files under one rootdir are an import-path conflict.

That is a property of the architecture, not a defect in it. The fix is to run one
pytest process per service — which is what `scripts/test-python.sh` does, setting the
same `PYTHONPATH` the service has inside its container.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).parent

#: Set by `scripts/test-python.sh`, which has already cd'd into one service and set
#: its PYTHONPATH. Without it, a collection reaching into a service directory is
#: someone running pytest from the root, and it will not work.
_PER_SERVICE = "KOS_SERVICE_TEST_RUN"


def pytest_ignore_collect(collection_path: Path) -> bool | None:
    """Skip service test directories when pytest was not launched per service."""
    if os.environ.get(_PER_SERVICE):
        return None

    try:
        relative = collection_path.relative_to(ROOT)
    except ValueError:
        return None

    parts = relative.parts
    if len(parts) >= 3 and parts[0] in {"apps", "packages"} and parts[2] == "tests":
        return True
    return None


def pytest_collection_finish(session: pytest.Session) -> None:
    if os.environ.get(_PER_SERVICE) or session.items:
        return
    # Only reachable when someone ran pytest from the root and got nothing, which is
    # exactly the moment the explanation is useful.
    print(
        "\nNo tests were collected: this monorepo needs one pytest process per "
        "service.\nRun `scripts/test-python.sh` (or `scripts/test-python.sh books`)."
    )
