#!/usr/bin/env bash
# =============================================================================
# LabCare — one-command InsForge redeploy helper.
#
# Usage:
#   ./deploy.sh            deploy the backend (InsForge compute container)
#   ./deploy.sh all        deploy backend + frontend
#   ./deploy.sh frontend   deploy the frontend (InsForge hosting)
#   ./deploy.sh push       no deploy — push current `main` to GitHub
#
# The script is safe to run from anywhere and recovers from a fresh sandbox /
# container where flyctl, the git remote, and the local env file are missing:
# it re-installs flyctl as needed, re-fetches the Postgres connection string
# from the InsForge CLI (never hardcoded), and rewrites .env.production.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# ---- configuration --------------------------------------------------------
CLI=(npx --yes @insforge/cli)                 # InsForge CLI (npx keeps it current)
SERVICE_NAME="labcare-api"
PORT="${PORT:-8000}"
REGION="${REGION:-sin}"
ENV_FILE=".env.production"
STATIC_DIR="static"

SITE_URL="https://labcare.insforge.site"
BACKEND_ENDPOINT="https://labcare-api-ee5bd3a7-8f78-4005-87ea-6c57ff5728aa.fly.dev"

GH_REPO="jeffrine7882-spec/LabCare"
GIT_NAME="jeffrine philip"
GIT_EMAIL="jeffrine7882@gmail.com"

# ---- helpers ----------------------------------------------------------------
log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m   ✓ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m   ✗ %s\033[0m\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required but not installed"; }

usage() { sed -n '3,12p' "${BASH_SOURCE[0]}"; exit 0; }

ensure_flyctl() {
  # flyctl lives in ~/.fly/bin and may be missing after a sandbox/reset.
  if ! command -v flyctl >/dev/null 2>&1 && [ -x "$HOME/.fly/bin/flyctl" ]; then
    export PATH="$HOME/.fly/bin:$PATH"
  fi
  if ! command -v flyctl >/dev/null 2>&1; then
    log "flyctl missing — installing to ~/.fly"
    local tmp
    tmp="$(mktemp -t fly_install.XXXXXX.sh)"
    curl -fsSL https://fly.io/install.sh -o "$tmp"
    FLYCTL_INSTALL="$HOME/.fly" sh "$tmp" >/dev/null
    rm -f "$tmp"
    export PATH="$HOME/.fly/bin:$PATH"
  fi
}

write_env() {
  log "Fetching the InsForge Postgres connection string"
  local dsn
  dsn="$("${CLI[@]}" db connection-string 2>/dev/null | tr -d '\r\n ')"
  [ -n "$dsn" ] || die "could not fetch the database connection string"
  # Preserve secret keys that don't come from the CLI (VAPID/FCM) across
  # redeploys — only the DSN is dynamic here.
  local keep="" line
  for key in LABCARE_VAPID_PRIVATE LABCARE_VAPID_PUBLIC LABCARE_FCM_PROJECT_ID LABCARE_FCM_SERVICE_JSON LABCARE_FCM_KEY_B64; do
    if [ -f "$ENV_FILE" ]; then
      line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 || true)"
      [ -n "$line" ] && keep="${keep}${line}"$'\n'
    fi
  done
  cat > "$ENV_FILE" <<EOF
LABCARE_DATABASE_URL=$dsn
LABCARE_SECURE_COOKIES=1
LABCARE_PORTAL_URL=$SITE_URL
LABCARE_APP_URL=$SITE_URL
EOF
  [ -n "$keep" ] && printf '%s' "$keep" >> "$ENV_FILE"
  ok "wrote $ENV_FILE (connection string not echoed)"
}

deploy_backend() {
  need npx curl
  ensure_flyctl
  write_env
  log "Deploying backend ($SERVICE_NAME) — always-on, build runs on the remote builder"
  "${CLI[@]}" compute deploy . --name "$SERVICE_NAME" --port "$PORT" \
    --region "$REGION" --env-file "$ENV_FILE" --always-on
  ok "backend deployed — $BACKEND_ENDPOINT"
  log "Health check"
  sleep 3
  local health
  health="$(curl -fsS -m 60 "$BACKEND_ENDPOINT/api/health")" || die "health check failed"
  ok "health: $health"
}

deploy_frontend() {
  need npx curl
  log "Deploying frontend ($STATIC_DIR) to InsForge hosting"
  "${CLI[@]}" deployments deploy "$STATIC_DIR"
  curl -fsS -m 60 -o /dev/null "$SITE_URL/" || die "frontend not reachable at $SITE_URL"
  ok "frontend live — $SITE_URL"
}

gh_push() {
  need git
  local tok="${GH_TOKEN:-}"
  if [ -z "$tok" ]; then
    tok="$(sed -n 's/^[[:space:]]*oauth_token:[[:space:]]*//p' \
           "$HOME/.config/gh/hosts.yml" 2>/dev/null | head -n1)"
  fi
  [ -n "${tok:-}" ] || die "no GitHub token found (set GH_TOKEN or run 'gh auth login')"
  git config user.name  "$GIT_NAME"  || true
  git config user.email "$GIT_EMAIL" || true
  git remote set-url origin "https://github.com/$GH_REPO.git" 2>/dev/null \
    || git remote add origin "https://github.com/$GH_REPO.git"
  log "Pushing main to $GH_REPO"
  git push "https://${tok}@github.com/$GH_REPO.git" main
  ok "pushed"
}

# ---- dispatch ----------------------------------------------------------------
case "${1:-backend}" in
  backend)   deploy_backend ;;
  frontend)  deploy_frontend ;;
  all)       deploy_backend; deploy_frontend ;;
  push)      gh_push ;;
  -h|--help) usage ;;
  *)         echo "unknown command: $1 (try backend | frontend | all | push)" >&2; exit 2 ;;
esac
