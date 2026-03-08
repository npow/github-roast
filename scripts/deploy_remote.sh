#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/root/github-roast}"
SERVICE_NAME="${SERVICE_NAME:-github-roast}"
DOMAIN_NAME="${DOMAIN_NAME:-gh-roast.deeprecon.app}"
APP_PORT="${APP_PORT:-8011}"
CLIPROXY_BASE_URL="${CLIPROXY_BASE_URL:-http://127.0.0.1:8317}"
CLIPROXY_API_KEY="${CLIPROXY_API_KEY:-}"
GH_TOKEN="${GH_TOKEN:-}"
CADDY_REPO_DIR="${CADDY_REPO_DIR:-/root/recon}"
CADDY_CONTAINER="${CADDY_CONTAINER:-recon-caddy-1}"

cd "$APP_DIR"

if ! curl -fsS "${CLIPROXY_BASE_URL%/}/" >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1; then
    CLIPROXY_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' recon-cliproxy-1 2>/dev/null || true)"
    if [[ -n "${CLIPROXY_IP}" ]]; then
      CLIPROXY_BASE_URL="http://${CLIPROXY_IP}:8317"
    fi
  fi
fi

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install fastapi uvicorn anthropic httpx python-multipart

cat > .env.production <<ENV
ANTHROPIC_BASE_URL=$CLIPROXY_BASE_URL
LLM_BASE_URL=$CLIPROXY_BASE_URL
ENV

if [[ -n "$CLIPROXY_API_KEY" ]]; then
  echo "ANTHROPIC_API_KEY=$CLIPROXY_API_KEY" >> .env.production
  echo "LLM_API_KEY=$CLIPROXY_API_KEY" >> .env.production
fi

if [[ -n "$GH_TOKEN" ]]; then
  echo "GH_TOKEN=$GH_TOKEN" >> .env.production
fi

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<SERVICE
[Unit]
Description=GitHub Roast FastAPI service
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env.production
Environment=HOME=/root
Environment=XDG_CONFIG_HOME=/root/.config
ExecStart=$APP_DIR/.venv/bin/uvicorn webapp:app --host 0.0.0.0 --port $APP_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

CADDYFILE="$CADDY_REPO_DIR/Caddyfile"
if [[ -f "$CADDYFILE" ]]; then
  START_MARKER="# BEGIN ${SERVICE_NAME}"
  END_MARKER="# END ${SERVICE_NAME}"
  BLOCK="$START_MARKER
$DOMAIN_NAME {
\treverse_proxy 172.17.0.1:$APP_PORT
}
$END_MARKER"

  TMP_FILE="$(mktemp)"
  awk -v s="$START_MARKER" -v e="$END_MARKER" '
    $0 == s {skip=1; next}
    $0 == e {skip=0; next}
    !skip {print}
  ' "$CADDYFILE" > "$TMP_FILE"
  printf '\n%s\n' "$BLOCK" >> "$TMP_FILE"
  mv "$TMP_FILE" "$CADDYFILE"

  docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile
fi

echo "Deploy complete"
