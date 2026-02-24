#!/usr/bin/env bash
# Run this ONCE on a fresh server to prepare it for deployments.
#
# Usage:
#   export CERTBOT_EMAIL=you@example.com        # required for SSL cert
#   export APP_DIR=/opt/auto_screen_api         # optional, default shown
#   bash scripts/server-setup.sh
#
set -e

APP_DIR=${APP_DIR:-/opt/auto_screen_api}
DOMAIN="analogy.postbaby.uz"
REPO_URL="https://github.com/islomkhodjadev/analogy.git"
CERTBOT_EMAIL=${CERTBOT_EMAIL:?"CERTBOT_EMAIL is required. Set it before running this script."}

# ─── 1. Install Docker ────────────────────────────────────────────────────────
echo "==> Installing Docker & Docker Compose plugin"
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl gnupg git

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# ─── 2. Clone repository ──────────────────────────────────────────────────────
echo "==> Cloning repository to $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  echo "    Repo already exists — pulling latest"
  git -C "$APP_DIR" pull origin main
else
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

# ─── 3. Create .env ───────────────────────────────────────────────────────────
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo ""
  echo "  IMPORTANT: Edit $APP_DIR/.env and fill in real secrets, then re-run this script."
  echo "  Stopping here so you can set the secrets before issuing the SSL cert."
  exit 0
fi

# ─── 4. First-time SSL certificate issuance ───────────────────────────────────
#
# We need nginx up on port 80 to serve the ACME challenge.
# Use the http-only config until the cert exists.
#
CERT_PATH="/var/lib/docker/volumes/$(basename $APP_DIR | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_')_certbot_certs/_data/live/$DOMAIN/fullchain.pem"

if [ ! -f "$CERT_PATH" ]; then
  echo "==> No cert found — issuing SSL cert for $DOMAIN"

  # Temporarily use the HTTP-only nginx config
  cp "$APP_DIR/nginx/conf.d/app.conf.http-only" "$APP_DIR/nginx/conf.d/app.conf"

  # Start nginx + api (needed to serve ACME challenge)
  docker compose up -d postgres redis api nginx

  echo "    Waiting for nginx to be ready..."
  sleep 5

  # Issue certificate
  docker compose run --rm certbot certonly \
    --webroot -w /var/www/certbot \
    --email "$CERTBOT_EMAIL" \
    -d "$DOMAIN" \
    --agree-tos --no-eff-email

  # Restore full HTTPS config
  git checkout nginx/conf.d/app.conf

  # Reload nginx with the new HTTPS config
  docker compose exec nginx nginx -s reload

  echo "==> SSL cert issued successfully"
else
  echo "==> SSL cert already exists — skipping certbot"
fi

# ─── 5. Start all services ────────────────────────────────────────────────────
echo "==> Starting all services"
docker compose up -d --build --remove-orphans

# ─── 6. Set up automatic cert renewal via cron ───────────────────────────────
CRON_JOB="0 3 * * * cd $APP_DIR && docker compose run --rm certbot renew --quiet && docker compose exec nginx nginx -s reload"
( crontab -l 2>/dev/null | grep -v "certbot renew"; echo "$CRON_JOB" ) | crontab -
echo "==> Cron job for cert renewal set (daily at 03:00)"

echo ""
echo "==> Setup complete!"
echo "    API is live at: https://$DOMAIN"
echo "    Future deploys: just push to main — GitHub Actions handles the rest."
