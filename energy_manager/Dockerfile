# Smart Energy Manager add-on image (SPEC.md section 15).
#
# Home Assistant builds this image with --build-arg BUILD_ARCH=<one of the manifest archs>,
# using the matching prebuilt Python base image. There is no pip layer: the add-on is standard
# library only (SPEC.md R3), so requirements.txt is intentionally empty.
ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base-python:3.13-alpine3.21

# Supervisor passes the manifest version into the build. Exposing it here means the API, the log
# banner and the add-on store report one number instead of drifting apart (app/main.py prefers
# this over its own fallback, which is only used when the add-on runs outside Home Assistant).
ARG BUILD_VERSION=""
ENV BUILD_VERSION=${BUILD_VERSION}

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# One package, imported as `app` (see SPEC.md section 2). /data comes from the Supervisor.
COPY app/ /app/app/
COPY run.sh /run.sh

# A locally built extension (or one linked against another libc) must not leak into the image; the
# optional core is compiled here below, or the Python reference in app/optimize/dp.py is used.
RUN rm -f /app/app/optimize/_core*.so

# Optional C accelerator for the SoC dynamic program (SPEC 8.2, docs/OPTIMIZER_V2.md section 6).
# The toolchain lives in a virtual package and is removed again.  A missing toolchain or a failed
# compile must not fail the image: the Python reference is always a complete implementation and
# app/optimize/_fast.py only accepts the extension after validating every result at runtime.
# -ffp-contract=off keeps the C arithmetic bit-identical to the Python reference (no FMA
# contraction), which the differential suite in tests/test_dp_core.py relies on.
RUN set -e; \
    if apk add --no-cache --virtual .build-deps build-base python3-dev; then \
        if ! ( cd /app/app/optimize && \
               gcc -O3 -ffp-contract=off -fPIC -shared \
                   $(python3-config --includes) \
                   -o "_core$(python3-config --extension-suffix)" _core.c ); then \
            echo "optimizer: C core not built; the Python reference stays active"; \
        fi; \
        apk del .build-deps || true; \
    else \
        echo "optimizer: no build toolchain; the Python reference stays active"; \
    fi

# Fail loudly instead of hanging when the health endpoint is missing.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8099/api/v1/health', timeout=5); sys.exit(0 if r.status == 200 else 1)"

# Runs as root, as Home Assistant add-ons expect. The entrypoint is run.sh so a container without a
# Supervisor token fails fast (SPEC.md section 15) instead of running blind; the wrapper execs
# `python3 -m app.main`, which is also the documented way to run outside Home Assistant.
#
# with-contenv is required: the base image's s6-overlay entrypoint runs this command as a service,
# and s6 services do not inherit the container environment, so SUPERVISOR_TOKEN would never reach
# run.sh — the wrapper would then refuse to start the add-on. with-contenv imports the environment
# the Supervisor passes to the container.
CMD ["/command/with-contenv", "/bin/sh", "/run.sh"]
