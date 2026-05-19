#!/bin/sh
set -eu

mkdir -p /config /config/art-cache
chown -R mediaops:mediaops /config 2>/dev/null || true

exec gosu mediaops "$@"
