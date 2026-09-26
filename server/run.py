#!/usr/bin/env python3
"""LabSynch — production entry point.

Runs the app with Waitress (a cross-platform production WSGI server) by
default so it is NOT on Flask's threaded dev server.

Environment variables:
    PORT                         listen port (default 8000)
    HOST                         bind address (default 0.0.0.0)
    LABCARE_DB                   SQLite file path (default <server>/labcare.db)
    LABCARE_TICKET_CAP           FIFO cap per ticket type (default 2000)
    LABCARE_HISTORY_LOG          history log file path (default <server>/ticket_history.log)
    LABCARE_STATIC_DIR           optional override for the static web-app dir

Example:
    PORT=8080 python3 run.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# init schema + seed before serving
from database import init_db
init_db()

try:
    from seed import seed
    seed()
except Exception:
    pass

from app import app  # noqa: E402

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8000))

if __name__ == "__main__":
    from waitress import serve
    print(f"* LabSynch running on http://{HOST}:{PORT} (waitress)")
    serve(app, host=HOST, port=PORT, threads=8)
