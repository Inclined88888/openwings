"""OpenWings additions to the upstream program (AGPL-3.0-or-later, like upstream).

Small, dependency-free helpers that the OpenWings patches in patches/ call into.
Nothing here imports the upstream `src` package at module load time, because
importing `src` runs database migrations as a side effect.

Importing the package maps any OPENWINGS_* settings still in the environment
to the internal names (wings.names), so `python -m wings.<tool>` works however
it is started.
"""
import os as _os

if any(_k.startswith("OPENWINGS_") for _k in _os.environ):
    from wings import names as _names

    _names.apply()
