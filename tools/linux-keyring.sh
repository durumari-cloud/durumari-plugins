#!/bin/sh
# Run a command inside a disposable D-Bus session with an unlocked Secret Service, so
# the credential-store tests use a real Linux keyring. Needs dbus and gnome-keyring.
# The keyring lives in a temporary XDG_DATA_HOME and its password is random.
set -eu
exec dbus-run-session -- sh -c '
  set -eu
  XDG_DATA_HOME="$(mktemp -d)"
  export XDG_DATA_HOME
  head -c 16 /dev/urandom | od -An -tx1 | tr -d " \n" |
    gnome-keyring-daemon --daemonize --unlock --components=secrets >/dev/null
  i=0
  until dbus-send --session --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus \
      org.freedesktop.DBus.NameHasOwner string:org.freedesktop.secrets 2>/dev/null |
      grep -q "boolean true"; do
    i=$((i + 1))
    [ "$i" -lt 100 ] || { echo "Secret Service did not start" >&2; exit 1; }
    sleep 0.1
  done
  exec "$@"
' sh "$@"
