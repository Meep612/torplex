#!/bin/bash
set -euo pipefail

TORPLEX_DIR="/opt/torplex"
REPO_URL="https://github.com/Meep612/torplex.git"

echo "=== [1/6] System prerequisites ==="
dnf install -y git curl python3 python3-pip nginx

echo "=== [2/6] Install Docker ==="
dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
echo "Docker version: $(docker --version)"

echo "=== [3/6] Install HAProxy ==="
dnf install -y haproxy
systemctl enable haproxy

echo "=== [4/6] Clone / update torplex ==="
if [ -d "$TORPLEX_DIR/.git" ]; then
    git -C "$TORPLEX_DIR" pull
else
    git clone "$REPO_URL" "$TORPLEX_DIR"
fi

echo "=== [5/6] Setup Python backend ==="
pip3 install -r "$TORPLEX_DIR/web/backend/requirements.txt"

echo "=== [6/6] Docker network + build Tor image ==="
docker network create torplex-net 2>/dev/null || true
docker build -t torplex-tor:latest "$TORPLEX_DIR/docker/tor-proxy/"

echo ""
echo "=== Torplex install complete ==="
echo "  Start backend : uvicorn web.backend.main:app --host 0.0.0.0 --port 8000 &"
echo "  Web UI        : http://$(hostname -I | awk '{print $1}'):80"
