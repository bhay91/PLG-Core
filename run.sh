#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
source .venv/bin/activate
PPS_FIREFOX_INBOX_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/pps/firefox-inbox.env"
if [[ ! -r "$PPS_FIREFOX_INBOX_ENV" ]]; then
  echo "PPS Firefox Inbox environment file is missing or unreadable." >&2
  exit 1
fi
set -a
source "$PPS_FIREFOX_INBOX_ENV"
set +a
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
