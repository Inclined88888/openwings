"""Start uvicorn with deployment secrets removed from the environment.

Usage (the image CMD): python -m wings.serve app:app --host 0.0.0.0 --port 7000

* The OPENWINGS_* settings are mapped to the names the app reads
  (wings.names), and internal names in log lines read as OPENWINGS_* again.
* The encryption key (OPENWINGS_APP_KEY) moves into wings.keyholder (memory only).
* WINGS_BROWSER_TOKEN, WINGS_RUNNER_TOKEN and WINGS_RENDERER_TOKEN move into
  wings.keyholder too.
* The admin password (OPENWINGS_ADMIN_PASSWORD) is dropped: the app reads it only in setup.py,
  which the entrypoint runs as a separate process before this one.
Everything else is plain `uvicorn` with the same command line arguments.
"""
from __future__ import annotations

import os
import sys

from wings import keyholder, names


def main() -> None:
    names.apply()
    names.install_output_filter()
    os.environ.pop("OPENWINGS_APP_KEY", None)
    key = os.environ.pop(names.internal_name("OPENWINGS_APP_KEY"), "").strip()
    keyholder.APP_KEY = key or None
    keyholder.BROWSER_TOKEN = os.environ.pop("WINGS_BROWSER_TOKEN", "").strip() or None
    keyholder.RUNNER_TOKEN = os.environ.pop("WINGS_RUNNER_TOKEN", "").strip() or None
    keyholder.RENDERER_TOKEN = os.environ.pop("WINGS_RENDERER_TOKEN", "").strip() or None
    os.environ.pop("OPENWINGS_ADMIN_PASSWORD", None)
    os.environ.pop(names.internal_name("OPENWINGS_ADMIN_PASSWORD"), None)
    sys.path.insert(0, os.getcwd())
    import uvicorn

    sys.argv[0] = "uvicorn"
    uvicorn.main()


if __name__ == "__main__":
    main()
