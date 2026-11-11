# Wings code runner: where the agent's Python and shell code runs, isolated
# from the Wings app (no network, no secrets, only the workspaces volume is
# writable). See runner_server.py.
ARG WINGS_RUNNER_BASE_IMAGE=docker.io/library/python:3.14.9-slim@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8
FROM ${WINGS_RUNNER_BASE_IMAGE}

COPY docker/runner/requirements.txt /tmp/runner-requirements.txt
# The optional "ca" build secret only exists for builds behind a
# TLS-intercepting proxy; normal builds do not need it.
RUN --mount=type=secret,id=ca,required=false \
    set -eu; \
    if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    python -m pip install --no-cache-dir --disable-pip-version-check --only-binary=:all: -r /tmp/runner-requirements.txt; \
    rm -f /tmp/runner-requirements.txt; \
    MPLCONFIGDIR=/tmp/mpl python -c "import numpy, pandas, scipy, matplotlib.pyplot, openpyxl, xlsxwriter, docx, pptx, pypdf, tabulate, PIL"; \
    rm -rf /tmp/mpl; \
    install -d -m 0755 /workspaces

COPY docker/runner/runner_server.py /opt/wings/runner_server.py
RUN python -m py_compile /opt/wings/runner_server.py && chmod 0644 /opt/wings/runner_server.py

# docker-compose.yml runs it as PUID:PGID, the owner of the app's data, so
# files it writes in the shared workspace belong to the same user.
USER 1000:1000
WORKDIR /workspaces
EXPOSE 8790
HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=4).read(1)"]
CMD ["python", "/opt/wings/runner_server.py"]

LABEL org.opencontainers.image.title="wings-runner" \
      org.opencontainers.image.description="Isolated code runner for the Wings agent"
