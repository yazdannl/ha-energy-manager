# Smart Energy Manager add-on image (SPEC.md section 15).
#
# Home Assistant builds this image with --build-arg BUILD_ARCH=<one of the manifest archs>,
# using the matching prebuilt Python base image. There is no pip layer: the add-on is standard
# library only (SPEC.md R3), so requirements.txt is intentionally empty.
ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base-python:3.13-alpine3.21

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# One package, imported as `app` (see SPEC.md section 2). /data comes from the Supervisor.
COPY app/ /app/app/
COPY run.sh /run.sh

# Fail loudly instead of hanging when the health endpoint is missing.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8099/api/v1/health', timeout=5); sys.exit(0 if r.status == 200 else 1)"

# Runs as root, as Home Assistant add-ons expect. The entrypoint is run.sh so a container without a
# Supervisor token fails fast (SPEC.md section 15) instead of running blind; the wrapper execs
# `python3 -m app.main`, which is also the documented way to run outside Home Assistant.
CMD ["/bin/sh", "/run.sh"]
