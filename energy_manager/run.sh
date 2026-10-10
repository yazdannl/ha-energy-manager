#!/bin/sh
# Add-on entrypoint (SPEC.md section 15): POSIX sh, fail fast, exec the app.
#
# The Supervisor injects SUPERVISOR_TOKEN (HASSIO_TOKEN is the legacy alias) and passes add-on
# options as /data/options.json. The app has no token option: without one of those two variables it
# cannot reach Home Assistant, so this wrapper refuses to start rather than run blind.
#
# To run the image outside Home Assistant for development, do not use this wrapper: run
# `python3 -m app.main` from the app directory with HA_URL (and HA_TOKEN) pointing at a test
# instance.
set -eu

if [ -z "${SUPERVISOR_TOKEN:-}" ] && [ -z "${HASSIO_TOKEN:-}" ]; then
    echo "energy_manager: neither SUPERVISOR_TOKEN nor HASSIO_TOKEN is set." >&2
    echo "This add-on runs under Home Assistant Supervisor (HAOS or Supervised), which injects" >&2
    echo "SUPERVISOR_TOKEN; there is no token option in the add-on configuration." >&2
    echo "For local development run 'python3 -m app.main' from the app directory with HA_URL" >&2
    echo "set to your test instance instead of using this wrapper." >&2
    exit 1
fi

exec python3 -m app.main
