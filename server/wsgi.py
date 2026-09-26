"""Production WSGI entry point for LabSynch.

Serving choices (all equivalent — pick one):

    waitress-serve --listen=0.0.0.0:8000 wsgi:application
    gunicorn -b 0.0.0.0:8000 wsgi:application

The database schema is initialised (and seeded on first run) before requests
are served. Any WSGI server can host this module.
"""
from database import init_db

# Make sure the schema exists before the first request hits the app.
# Seeding is handled inside the main block of app.py; call it here too so a
# fresh, empty DB gets the demo data on a stock deployment.
init_db()

try:
    from seed import seed
    seed()
except Exception:
    pass  # already seeded, or no seed module — continue

from app import app as application
