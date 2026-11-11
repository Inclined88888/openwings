# OpenWings browser: a real (headful) Chromium on a virtual screen, for the agent
# to drive over the Chrome DevTools Protocol and for you to watch and take over
# through the live view in the OpenWings web app.
#
# Base: the Selenium project's Chromium image, pinned by digest. It already
# carries Chromium, Xvfb, a window manager, x11vnc and noVNC for amd64 and
# arm64, so this file only adds configuration (no package downloads).
# Selenium's own Grid server is not started.
ARG WINGS_BROWSER_BASE_IMAGE=selenium/standalone-chromium:156.0.8078.79-20261101@sha256:b211549a4c35f11826bc1a7559b9f23ff055198517f8cb7d49ef74df01c5e38d
FROM ${WINGS_BROWSER_BASE_IMAGE}

USER root
COPY docker/browser/start.sh docker/browser/chromium.sh docker/browser/vnc.sh docker/browser/guard_proxy.py docker/browser/egress_proxy.py /opt/wings/
COPY docker/browser/supervisord.conf /opt/wings/supervisord.conf
COPY docker/browser/policies.json /etc/chromium/policies/managed/wings.json
# The profile and download folders must exist in the image, owned by the
# browser user, so the named volumes mounted over them inherit that owner.
RUN chmod 0755 /opt/wings/*.sh /opt/wings/guard_proxy.py /opt/wings/egress_proxy.py \
    && chmod 0644 /opt/wings/supervisord.conf /etc/chromium/policies/managed/wings.json \
    && install -d -o 1200 -g 1201 -m 0700 /home/seluser/wings-profile \
    && install -d -o 1200 -g 1201 -m 0755 /home/seluser/Downloads \
    && python3 -m py_compile /opt/wings/guard_proxy.py /opt/wings/egress_proxy.py
USER 1200

ENV SE_START_XVFB=true \
    SE_SCREEN_WIDTH=1440 \
    SE_SCREEN_HEIGHT=900 \
    SE_SCREEN_DEPTH=24 \
    WINGS_CDP_PORT=9223 \
    WINGS_VIEW_PORT=7900

EXPOSE 9223 7900
ENTRYPOINT ["/opt/wings/start.sh"]
HEALTHCHECK --interval=20s --timeout=5s --start-period=40s --retries=5 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9222/json/version', timeout=4).read(1)"]

LABEL org.opencontainers.image.title="openwings-browser" \
      org.opencontainers.image.description="Chromium with live view for OpenWings browser automation"
