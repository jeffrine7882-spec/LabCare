# LabCare API + static frontend — InsForge compute container.
# Deployment:  npx -y @insforge/cli compute deploy . --name labcare-api --port 8000
#
# The Flask app serves BOTH the REST API and the static web app (single
# origin), so the image bundles server/ + static/ together.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY server /app/server
COPY static /app/static

RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app/server
EXPOSE 8000

# Boot runs init_db() + seed() (fresh DB -> Master Admin only) before serving.
CMD ["python3", "run.py"]
