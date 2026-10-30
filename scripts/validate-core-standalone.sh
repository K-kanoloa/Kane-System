#!/bin/sh
# Run ONLY in a disposable container. No host paths, network or Agent source.
set -eu
test -f /component.deb
test ! -e /workspace/Kane-System
test ! -e /app/apps
if command -v python3 >/dev/null 2>&1 || command -v node >/dev/null 2>&1; then
  echo 'Fixture must have no Python/Node runtime' >&2
  exit 1
fi
dpkg --install /component.deb
install -d -o kane-system -g kane-system -m 0700 /run/kane-system
runuser -u kane-system -- kanesys --config /etc/kane-system/core.env serve &
core_pid=$!
trap 'kill "$core_pid" 2>/dev/null || true; wait "$core_pid" 2>/dev/null || true' EXIT
ready=false
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if test -S /run/kane-system/core.sock; then ready=true; break; fi
  sleep 0.1
done
test "$ready" = true
printf '%s' '{"op":"health"}' | runuser -u kane-system -- kanesys --socket /run/kane-system/core.sock host-request
printf '%s' '{"op":"create_workspace","kind":"document"}' | runuser -u kane-system -- kanesys --socket /run/kane-system/core.sock host-request
printf '%s' '{"op":"bindings"}' | runuser -u kane-system -- kanesys --socket /run/kane-system/core.sock host-request
if printf '%s' '{"op":"health"}' | runuser -u nobody -- kanesys --socket /run/kane-system/core.sock host-request; then
  echo 'Unexpected unprivileged access' >&2
  exit 1
fi
# Exercise kernel peer identity separately from filesystem permissions. This
# intentionally weakens only the disposable container's socket directory.
chmod 0755 /run/kane-system
chmod 0666 /run/kane-system/core.sock
if printf '%s' '{"op":"create_workspace","kind":"unauthorized"}' | runuser -u nobody -- kanesys --socket /run/kane-system/core.sock host-request; then
  echo 'Unexpected access after socket permissions widened' >&2
  exit 1
fi
chmod 0700 /run/kane-system
chmod 0600 /run/kane-system/core.sock
printf '%s' '{"op":"health"}' | runuser -u kane-system -- kanesys --socket /run/kane-system/core.sock host-request
echo KANE_CORE_STANDALONE_RUNTIME_PASS
