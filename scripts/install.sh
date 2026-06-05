#!/bin/bash
set -euo pipefail

TORPLEX_DIR="/opt/torplex"
REPO_URL="https://github.com/Meep612/torplex.git"
LOG_FILE="/var/log/torplex-install.log"
NGINX_CONF="/etc/nginx/nginx.conf"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
ok()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✓ $*" | tee -a "$LOG_FILE"; }
err() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ ERROR: $*" | tee -a "$LOG_FILE"; exit 1; }

mkdir -p "$(dirname $LOG_FILE)"
log "=========================================="
log "Torplex installation started"
log "Host: $(hostname -f) | IP: $(hostname -I | awk '{print $1}')"
log "OS: $(cat /etc/rocky-release)"
log "=========================================="

# --- Step 1: System prerequisites ---
log "[1/6] Installing system prerequisites (git, curl, python3, pip, nginx)..."
dnf install -y git curl python3 python3-pip nginx >> "$LOG_FILE" 2>&1
ok "System prerequisites installed"

# --- Step 2: Docker ---
log "[2/6] Installing Docker CE..."
dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo >> "$LOG_FILE" 2>&1
dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin >> "$LOG_FILE" 2>&1
systemctl enable --now docker >> "$LOG_FILE" 2>&1
ok "Docker installed: $(docker --version)"

# --- Step 3: HAProxy ---
log "[3/6] Installing HAProxy..."
dnf install -y haproxy >> "$LOG_FILE" 2>&1
systemctl enable haproxy >> "$LOG_FILE" 2>&1
ok "HAProxy installed: $(haproxy -v 2>&1 | head -1)"

# --- Step 4: Clone repo ---
log "[4/6] Cloning torplex repository..."
if [ -d "$TORPLEX_DIR/.git" ]; then
    git -C "$TORPLEX_DIR" pull >> "$LOG_FILE" 2>&1
    ok "Repository updated"
else
    git clone "$REPO_URL" "$TORPLEX_DIR" >> "$LOG_FILE" 2>&1
    ok "Repository cloned to $TORPLEX_DIR"
fi

# --- Step 5: Python backend ---
log "[5/6] Installing Python backend dependencies..."
pip3 install -r "$TORPLEX_DIR/web/backend/requirements.txt" >> "$LOG_FILE" 2>&1
ok "Python dependencies installed"

# --- Step 6: Docker network + Tor image ---
log "[6/6] Creating Docker network and building Tor proxy image..."
docker network create torplex-net >> "$LOG_FILE" 2>&1 || log "  Network torplex-net already exists, skipping"
docker build -t torplex-tor:latest "$TORPLEX_DIR/docker/tor-proxy/" >> "$LOG_FILE" 2>&1
ok "Docker image torplex-tor:latest built"

# --- Nginx config ---
log "Configuring nginx..."
cat > "$NGINX_CONF" << 'NGINX'
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log;
pid /run/nginx.pid;
include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 1024;
}

http {
    log_format main '$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent';
    access_log /var/log/nginx/access.log main;
    sendfile on;
    keepalive_timeout 65;
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    include /etc/nginx/conf.d/*.conf;
}
NGINX

cat > /etc/nginx/conf.d/torplex.conf << 'NGINXVHOST'
server {
    listen 80 default_server;
    server_name _;

    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location / {
        root /opt/torplex/web/frontend;
        index index.html;
        try_files $uri $uri/ /index.html;
    }
}
NGINXVHOST

setsebool -P httpd_can_network_connect 1 >> "$LOG_FILE" 2>&1
log "Configuring firewall..."
firewall-cmd --add-service=http --permanent >> "$LOG_FILE" 2>&1
firewall-cmd --add-port=8000/tcp --permanent >> "$LOG_FILE" 2>&1
firewall-cmd --add-port=8404/tcp --permanent >> "$LOG_FILE" 2>&1
firewall-cmd --reload >> "$LOG_FILE" 2>&1
ok "Firewall rules applied (http, 8000, 8404)" >> "$LOG_FILE" 2>&1
nginx -t >> "$LOG_FILE" 2>&1 || err "nginx config test failed"
systemctl enable --now nginx >> "$LOG_FILE" 2>&1
ok "nginx configured and started"

# --- Start backend ---
log "Starting torplex backend (FastAPI)..."
pkill -f "uvicorn web.backend.main" 2>/dev/null || true
cd "$TORPLEX_DIR"
nohup uvicorn web.backend.main:app --host 0.0.0.0 --port 8000 >> /var/log/torplex-backend.log 2>&1 &
sleep 3
curl -sf http://localhost:8000/health > /dev/null || err "Backend health check failed"
ok "Backend running on port 8000"

curl -sf http://localhost/api/health > /dev/null || err "nginx → backend proxy check failed"
ok "nginx proxy to backend: OK"

curl -sf http://localhost/ > /dev/null || err "Frontend check failed"
ok "Frontend served via nginx: OK"

log "=========================================="
log "Torplex installation SUCCESSFUL"
log "  Web UI  : http://$(hostname -I | awk '{print $1}'):80"
log "  API     : http://$(hostname -I | awk '{print $1}'):8000"
log "  HAProxy stats : http://$(hostname -I | awk '{print $1}'):8404/stats"
log "  Install log   : $LOG_FILE"
log "  Backend log   : /var/log/torplex-backend.log"
log "=========================================="
