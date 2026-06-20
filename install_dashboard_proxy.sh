#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${DASHBOARD_DOMAIN:-${1:-}}"
EMAIL="${DASHBOARD_EMAIL:-${2:-}}"
UPSTREAM_HOST="${DASHBOARD_UPSTREAM_HOST:-127.0.0.1}"
UPSTREAM_PORT="${DASHBOARD_UPSTREAM_PORT:-8080}"
AUTH_ENABLED="${DASHBOARD_AUTH_ENABLED:-true}"
AUTH_USER="${DASHBOARD_AUTH_USER:-growatt}"
AUTH_FILE="/etc/nginx/.htpasswd-growatt-dashboard"
SITE_FILE="/etc/nginx/sites-available/growatt-dashboard.conf"

if [[ -z "${DOMAIN}" || -z "${EMAIL}" ]]; then
  echo "Usage: DASHBOARD_DOMAIN=dashboard.example.com DASHBOARD_EMAIL=you@example.com ./install_dashboard_proxy.sh"
  echo "Or: ./install_dashboard_proxy.sh dashboard.example.com you@example.com"
  exit 2
fi

case "${AUTH_ENABLED,,}" in
  1|true|yes|y|on)
    AUTH_ENABLED="true"
    ;;
  0|false|no|n|off)
    AUTH_ENABLED="false"
    ;;
  *)
    echo "DASHBOARD_AUTH_ENABLED must be true or false."
    exit 2
    ;;
esac

echo "This assumes DNS already points ${DOMAIN} to this VPS."
sudo -v

if [[ "${AUTH_ENABLED}" == "true" ]]; then
  read -rsp "Dashboard password for user '${AUTH_USER}': " AUTH_PASS
  echo
  if [[ -z "${AUTH_PASS}" ]]; then
    echo "Password cannot be empty."
    exit 2
  fi
fi

sudo apt update
if [[ "${AUTH_ENABLED}" == "true" ]]; then
  sudo apt install -y nginx apache2-utils certbot python3-certbot-nginx
  printf '%s\n' "${AUTH_PASS}" | sudo htpasswd -B -i -c "${AUTH_FILE}" "${AUTH_USER}"
  AUTH_CONFIG="    auth_basic \"Growatt Dashboard\";
    auth_basic_user_file ${AUTH_FILE};
"
else
  sudo apt install -y nginx certbot python3-certbot-nginx
  AUTH_CONFIG=""
  echo "Basic auth disabled. Anyone who can reach https://${DOMAIN}/dashboard.html can view the dashboard."
fi

sudo tee "${SITE_FILE}" > /dev/null <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

${AUTH_CONFIG}
    location / {
        proxy_pass http://${UPSTREAM_HOST}:${UPSTREAM_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

sudo ln -sf "${SITE_FILE}" /etc/nginx/sites-enabled/growatt-dashboard.conf
sudo nginx -t
sudo systemctl reload nginx

sudo certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "${EMAIL}" --redirect

echo "Dashboard proxy installed."
echo "Open: https://${DOMAIN}/dashboard.html"
if [[ "${AUTH_ENABLED}" == "true" ]]; then
  echo "Username: ${AUTH_USER}"
else
  echo "Basic auth: disabled"
fi
