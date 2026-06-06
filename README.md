# Torplex

Multi Tor proxy orchestrator with web UI — Docker-based, self-hosted.

## Overview

Torplex lets you spin up multiple isolated Tor SOCKS5/HTTP proxies, each in its own Docker container, and manage them from a single web interface. A HAProxy load balancer provides a single entry point across all running proxies.

```
┌─────────────────────────────────────────────────────┐
│                    Torplex Host                      │
│                                                     │
│  Browser ──► nginx:80 ──► Frontend (static HTML)   │
│                      └──► FastAPI:8000 (REST API)   │
│                                                     │
│  Client ──► HAProxy:9050 ──► tor-proxy-1:9050      │
│          (balanced)    └──► tor-proxy-2:9050      │
│                         └──► tor-proxy-N:9050      │
│                                                     │
│  Client ──► tor-proxy-N:1080X  (direct mode)       │
└─────────────────────────────────────────────────────┘
```

## Quick Start

```bash
# Clone and install everything
git clone https://github.com/YOUR_USER/torplex.git /opt/torplex
bash /opt/torplex/scripts/install.sh
```

Install time: ~90 seconds on a fresh Rocky Linux 9 VM.

## Access

| Service       | URL / Address              |
|---------------|----------------------------|
| Web UI        | http://\<host\>:80          |
| REST API      | http://\<host\>:8000        |
| HAProxy stats | http://\<host\>:8404/stats  |

## Features

- **Add/remove proxies** on-the-fly — each runs in an isolated Docker container
- **Exit country selection** — choose exit node country per proxy (US, DE, FR, NL, …)
- **Strict mode** — force StrictNodes 1 to guarantee the selected country
- **Circuit renewal** — SIGNAL NEWNYM via Tor control port (no container restart needed)
- **Live exit IP display** — shows real exit IP + country from inside the Tor circuit
- **Two operating modes:**
  - *Balanced* — HAProxy round-robins across all proxies on port 9050
  - *Direct* — each proxy has its own port (10801, 10802, …)
- **Settings panel** — configure load balancer mode, control password, port allocation

## Port Allocation

| Port range  | Role                        |
|-------------|-----------------------------|
| 9050        | HAProxy SOCKS5 entry point  |
| 8404        | HAProxy stats               |
| 10801–10899 | SOCKS5 per proxy            |
| 11801–11899 | HTTP (Privoxy) per proxy    |
| 12801–12899 | Tor control port per proxy  |

## REST API

```bash
GET  /proxies                  # List all proxies
POST /proxies                  # Create proxy {country, strict}
DELETE /proxies/{name}         # Remove proxy
POST /proxies/{name}/renew     # Renew Tor circuit
GET  /proxies/{name}/ip        # Get current exit IP + country
GET  /countries                # List available exit countries
GET  /settings                 # Get current configuration
GET  /health                   # Health check
```

### Example — create a German exit proxy

```bash
curl -X POST http://localhost:8000/proxies \
  -H "Content-Type: application/json" \
  -d '{"country": "DE", "strict": true}'
```

### Example — use a proxy

```bash
# SOCKS5
curl --socks5-hostname 127.0.0.1:10801 https://ipapi.co/json/

# HTTP (Privoxy)
curl -x http://127.0.0.1:11801 https://ipapi.co/json/
```

## Stack

| Component      | Role                                    |
|----------------|-----------------------------------------|
| `dperson/torproxy` | Tor SOCKS5 + HTTP proxy (Alpine)    |
| HAProxy 2.8    | TCP load balancer across Tor proxies    |
| FastAPI        | REST API — proxy lifecycle management   |
| nginx          | Serves web UI + reverse proxy to API    |
| Rocky Linux 9  | Host OS                                 |

## Project Structure

```
torplex/
├── docker/tor-proxy/      # (legacy) custom Tor image
├── haproxy/haproxy.cfg    # HAProxy config template
├── nginx/nginx.conf       # nginx vhost reference
├── web/
│   ├── backend/main.py    # FastAPI REST API
│   └── frontend/index.html
├── scripts/install.sh     # Full install script
├── docker-compose.yml
└── README.md
```

## Reinstall from scratch

To test a clean install, wipe `/opt/torplex` and re-run `install.sh`.
