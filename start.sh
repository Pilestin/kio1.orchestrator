#!/usr/bin/env sh
# Starts KIO1. Everything it does is in start.py; this only finds a Python.
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    exec python3 start.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python start.py "$@"
fi

echo "Python 3.10 or newer is needed: https://www.python.org/downloads/" >&2
exit 1
