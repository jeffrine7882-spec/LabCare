# LabCare backend — production image (Flask + Waitress + SQLite)
#
#   BUILD
#     docker build -t labcare-backend -f deploy/Dockerfile .
#
#   RUN (bind 8000; persist DB + history so data survives restarts)
#     docker run -d --name labcare \
#       -p 127.0.0.1:8000:8000 \
#       -e LABCARE_SECURE_COOKIES=1 \
#       -v /opt/labcare-data:/data \
#       -e LABCARE_DB=/data/labcare.db \
#       -e LABCARE_HISTORY_LOG=/data/ticket_history.log \
#       labcare-backend
#
#   Then put nginx + certbot in front of it (deploy/nginx-labcare.conf) and
#   point Netlify's /api/* proxy at the public HTTPS URL.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY server ./server
COPY static ./static
WORKDIR /app/server

EXPOSE 8000

CMD ["python3", "run.py"]
