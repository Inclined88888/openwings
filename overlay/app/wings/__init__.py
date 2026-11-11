"""OpenWings additions to Odysseus (AGPL-3.0-or-later, like upstream).

Small, dependency-free helpers that the OpenWings patches in patches/ call into.
Nothing here imports the upstream `src` package at module load time, because
importing `src` runs database migrations as a side effect.
"""
