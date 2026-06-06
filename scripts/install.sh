#!/bin/bash
set -euo pipefail

TORPLEX_DIR="/opt/torplex"
REPO_URL="https://github.com/YOUR_USER/torplex.git"  # ← set your repo URL
LOG_FILE="/var/log/torplex-install.log"

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
log "[1/8] Installing system prerequisites..."
dnf install -y git curl python3 python3-pip nginx policycoreutils-python-utils >> "$LOG_FILE" 2>&1
ok "System prerequisites installed"

# --- Step 2: Docker ---
log "[2/8] Installing Docker CE..."
dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo >> "$LOG_FILE" 2>&1
dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin >> "$LOG_FILE" 2>&1
systemctl enable --now docker >> "$LOG_FILE" 2>&1
ok "Docker installed: $(docker --version)"

# --- Step 3: HAProxy ---
log "[3/8] Installing HAProxy..."
dnf install -y haproxy >> "$LOG_FILE" 2>&1
ok "HAProxy installed: $(haproxy -v 2>&1 | head -1)"

# --- Step 4: Clone repo ---
log "[4/8] Cloning torplex repository..."
if [ -d "$TORPLEX_DIR/.git" ]; then
    git -C "$TORPLEX_DIR" pull >> "$LOG_FILE" 2>&1
    ok "Repository updated"
else
    git clone "$REPO_URL" "$TORPLEX_DIR" >> "$LOG_FILE" 2>&1
    ok "Repository cloned to $TORPLEX_DIR"
fi

# --- Step 5: Python backend ---
log "[5/8] Installing Python backend dependencies..."
pip3 install -r "$TORPLEX_DIR/web/backend/requirements.txt" >> "$LOG_FILE" 2>&1
ok "Python dependencies installed"

# --- Step 6: Docker network ---
log "[6/8] Creating Docker network..."
docker network create torplex-net >> "$LOG_FILE" 2>&1 || log "  Network torplex-net already exists, skipping"
ok "Docker network torplex-net ready"

# --- Step 7: nginx ---
log "[7/8] Configuring nginx..."
cat > /etc/nginx/nginx.conf << 'NGINX'
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
setsebool -P haproxy_connect_any 1 >> "$LOG_FILE" 2>&1
nginx -t >> "$LOG_FILE" 2>&1 || err "nginx config test failed"
systemctl enable --now nginx >> "$LOG_FILE" 2>&1
ok "nginx configured and started"

# --- Step 8: HAProxy custom service + config ---
log "[8/8] Configuring HAProxy SOCKS5 load balancer..."

# Initial empty config — watchdog will populate backends dynamically
cat > /etc/haproxy/haproxy.cfg << 'HAPCFG'
global
    log stdout format raw local0
    maxconn 50000

defaults
    log     global
    mode    tcp
    timeout connect 5s
    timeout client  120s
    timeout server  120s
    option  tcplog

frontend tor_in
    bind *:9050
    default_backend tor_pool

backend tor_pool
    balance roundrobin

frontend stats
    bind *:8404
    mode http
    stats enable
    stats uri /stats
    stats refresh 5s
    stats admin if TRUE
HAPCFG

# SELinux: allow haproxy to bind on ports 9050 and 8404
semanage port -a -t http_port_t -p tcp 9050 >> "$LOG_FILE" 2>&1 \
  || semanage port -m -t http_port_t -p tcp 9050 >> "$LOG_FILE" 2>&1 || true
semanage port -a -t http_port_t -p tcp 8404 >> "$LOG_FILE" 2>&1 \
  || semanage port -m -t http_port_t -p tcp 8404 >> "$LOG_FILE" 2>&1 || true

# Custom systemd unit (avoids daemon mode issues)
cat > /etc/systemd/system/torplex-haproxy.service << 'SYSD'
[Unit]
Description=Torplex HAProxy SOCKS5 Load Balancer
After=network.target

[Service]
Type=simple
ExecStart=/usr/sbin/haproxy -f /etc/haproxy/haproxy.cfg -db
ExecReload=/bin/kill -USR2 $MAINPID
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SYSD

systemctl daemon-reload
systemctl enable --now torplex-haproxy >> "$LOG_FILE" 2>&1
ok "HAProxy service started (torplex-haproxy)"

# --- Firewall ---
log "Configuring firewall..."
firewall-cmd --add-service=http --permanent >> "$LOG_FILE" 2>&1
firewall-cmd --add-port=8000/tcp  --permanent >> "$LOG_FILE" 2>&1
firewall-cmd --add-port=8404/tcp  --permanent >> "$LOG_FILE" 2>&1
firewall-cmd --add-port=9050/tcp  --permanent >> "$LOG_FILE" 2>&1
firewall-cmd --reload >> "$LOG_FILE" 2>&1
ok "Firewall rules applied (80, 8000, 8404, 9050)"

# --- Start backend ---
log "Starting torplex backend (FastAPI + watchdog)..."
pkill -f "uvicorn web.backend.main" 2>/dev/null || true
sleep 1
cd "$TORPLEX_DIR"
nohup uvicorn web.backend.main:app --host 0.0.0.0 --port 8000 >> /var/log/torplex-backend.log 2>&1 &
sleep 4

curl -sf http://localhost:8000/health > /dev/null || err "Backend health check failed"
ok "Backend running on port 8000 (watchdog active)"

curl -sf http://localhost/api/health > /dev/null || err "nginx → backend proxy check failed"
ok "nginx proxy to backend: OK"

curl -sf http://localhost/ > /dev/null || err "Frontend check failed"
ok "Frontend served via nginx: OK"

HOST_IP=$(hostname -I | awk '{print $1}')
log "=========================================="
log "Torplex installation SUCCESSFUL"
log "  Web UI       : http://${HOST_IP}:80"
log "  API          : http://${HOST_IP}:8000"
log "  HAProxy LB   : socks5://${HOST_IP}:9050  (round-robin, auto-updated)"
log "  HAProxy stats: http://${HOST_IP}:8404/stats"
log "  Install log  : $LOG_FILE"
log "  Backend log  : /var/log/torplex-backend.log"
log "=========================================="
log "Usage: curl --socks5-hostname ${HOST_IP}:9050 https://api.ipify.org"
log "=========================================="
