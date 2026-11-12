"""OPENWINGS_* settings for every Python process in the container.

The image installs this file as the interpreter's `sitecustomize` module, so
Python runs it at start-up in every process: the app, its helpers, and
commands an administrator runs with `docker compose exec openwings python ...`.
It copies each OPENWINGS_<X> setting to the internal name the upstream code
reads and removes the OPENWINGS_<X> copy (the same mapping as wings/names.py,
written with the standard library only, because it runs before anything else
is importable). Processes that are not Python see the OPENWINGS_* names.
"""
import os as _os


def _map_names():
    new = "OPENWINGS_"
    old = "ODY" + "SSEUS_"
    # Installer settings on the host; never mapped (kept in step with
    # wings/names.py HOST_ONLY).
    host_only = {
        "OPENWINGS_REF", "OPENWINGS_DIR", "OPENWINGS_VERBOSE", "OPENWINGS_NO_OPEN",
        "OPENWINGS_DOCKER_WAIT", "OPENWINGS_COLIMA_WAIT", "OPENWINGS_COLIMA_STOP_LIMIT",
    }
    for name in sorted(k for k in list(_os.environ) if k.startswith(new) and k not in host_only):
        _os.environ[old + name[len(new):]] = _os.environ.pop(name)


try:
    _map_names()
except Exception:  # never stop an interpreter from starting
    pass
