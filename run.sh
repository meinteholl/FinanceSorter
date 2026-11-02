#!/usr/bin/env bash
set -e
# exec so SIGTERM reaches Python: without it the bash wrapper catches the signal
# and App Hub waits the full 10s grace before SIGKILL on every stop.
exec python app.py
