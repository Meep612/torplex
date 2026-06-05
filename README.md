# Torplex

Multi Tor proxy orchestrator with web UI — Docker-based.

## Architecture

```
client → HAProxy:9050 (balanced) → tor-proxy-1, tor-proxy-2, ... tor-proxy-N
client → tor-proxy-N:1080X (direct)

HTTP → nginx:80 → frontend (static) + backend FastAPI:8000
```

## Quick start

```bash
bash scripts/install.sh
docker compose up -d
```

## Add a proxy

Via web UI or API:
```bash
curl -X POST http://localhost:8000/proxies
```

## Structure

```
torplex/
├── docker/tor-proxy/     # Tor Docker image
├── haproxy/              # HAProxy config (load balancer)
├── nginx/                # nginx config (web UI)
├── web/
│   ├── backend/          # FastAPI REST API
│   └── frontend/         # HTML/JS UI
├── scripts/install.sh    # Full install script
└── docker-compose.yml
```
