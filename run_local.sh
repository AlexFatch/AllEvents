#!/bin/sh
set -eu
cd "$(dirname "$0")"
export LOCAL_DEV=1
export DOMAIN_URL="http://127.0.0.1:5001"
exec .venv/bin/python -m flask --app flask_app run --host 127.0.0.1 --port 5001
