from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import subprocess, json, re, socket, time, asyncio
from concurrent.futures import ThreadPoolExecutor
import os, shutil

app = FastAPI(title="Torplex API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CONTROL_PASSWORD  = "torplex"
BASE_SOCKS_PORT   = 10800
BASE_HTTP_PORT    = 11800
BASE_CTRL_PORT    = 12800
MAX_PROXIES       = 50
WATCHDOG_INTERVAL = 30   # seconds between watchdog cycles
HAPROXY_CFG      = "/etc/haproxy/haproxy.cfg"
HAPROXY_CFG_BAK  = "/etc/haproxy/haproxy.cfg.bak"
_last_proxy_set: set = set()   # track changes to avoid unnecessary reloads
IP_TTL            = 120  # seconds before re-checking a known IP

COUNTRIES = {
    "any": "Any",
    "US": "United States", "DE": "Germany", "FR": "France",
    "GB": "United Kingdom", "NL": "Netherlands", "CH": "Switzerland",
    "SE": "Sweden", "NO": "Norway", "FI": "Finland", "CA": "Canada",
    "JP": "Japan", "AU": "Australia", "BR": "Brazil", "SG": "Singapore",
    "AT": "Austria", "BE": "Belgium", "CZ": "Czech Republic",
    "DK": "Denmark", "ES": "Spain", "IT": "Italy", "PL": "Poland",
    "RO": "Romania", "RU": "Russia", "UA": "Ukraine",
}

# In-memory IP cache: name → {ip, country, country_name, city, checked_at, ok}
_ip_cache: dict = {}

class ProxyCreate(BaseModel):
    country: Optional[str] = "any"
    strict:  Optional[bool] = False
    count:   Optional[int]  = 1

# ─── helpers ──────────────────────────────────────────────────────────────────

def run(cmd, timeout=60):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.returncode

def get_index(name):
    m = re.match(r'^tor-proxy-(\d+)$', name)
    return int(m.group(1)) if m else None

def used_indexes():
    out, _ = run("docker ps -a --filter 'name=tor-proxy-' --format '{{.Names}}'")
    return {int(re.search(r'\d+', n).group()) for n in out.splitlines() if n and re.search(r'\d+', n)}

def prune_exited():
    out, _ = run("docker ps -a --filter 'name=tor-proxy-' --filter 'status=exited' --filter 'status=created' --format '{{.Names}}'")
    names = [n for n in out.splitlines() if n]
    if names:
        run("docker rm " + " ".join(names))
        for n in names:
            _ip_cache.pop(n, None)
        print(f"[watchdog] pruned {len(names)} dead containers")
    return len(names)

def reserve_indexes(count):
    prune_exited()
    used = used_indexes()
    result, i = [], 1
    while len(result) < count:
        if i not in used:
            result.append(i)
            used.add(i)
        i += 1
        if i > 9999:
            break
    return result

def fetch_ip(socks_port: int) -> dict:
    try:
        out, rc = run(
            f'curl -s --socks5-hostname 127.0.0.1:{socks_port} --max-time 12 https://ipapi.co/json/',
            timeout=20
        )
        if rc == 0 and out:
            d = json.loads(out)
            if d.get("ip"):
                return {"ip": d.get("ip"), "country": d.get("country_code"),
                        "country_name": d.get("country_name"), "city": d.get("city"), "ok": True}
    except Exception:
        pass
    return {"ip": None, "country": None, "country_name": None, "city": None, "ok": False}

def refresh_ip(name: str, socks_port: int):
    result = fetch_ip(socks_port)
    result["checked_at"] = time.time()
    _ip_cache[name] = result
    status = f"{result['ip']} ({result['country']})" if result["ok"] else "no route yet"
    print(f"[watchdog] {name}: {status}")

def get_running_proxies() -> list:
    out, _ = run("docker ps --filter 'name=tor-proxy-' --format '{{json .}}'")
    proxies = []
    for line in out.splitlines():
        if not line:
            continue
        c = json.loads(line)
        idx = get_index(c["Names"])
        if idx:
            proxies.append({"name": c["Names"], "idx": idx,
                            "socks_port": BASE_SOCKS_PORT + idx})
    return proxies

def tor_control(ctrl_port, signal):
    try:
        with socket.create_connection(("127.0.0.1", ctrl_port), timeout=5) as s:
            s.sendall(f'AUTHENTICATE "{CONTROL_PASSWORD}"\r\n'.encode())
            s.recv(1024)
            s.sendall(f'SIGNAL {signal}\r\n'.encode())
            resp = s.recv(1024).decode()
            s.sendall(b'QUIT\r\n')
            return "250" in resp
    except Exception:
        return False

def spawn_one(idx, country, strict):
    name       = f"tor-proxy-{idx}"
    socks_port = BASE_SOCKS_PORT + idx
    http_port  = BASE_HTTP_PORT  + idx
    ctrl_port  = BASE_CTRL_PORT  + idx
    country_flag = f'-l "{country}"' if country and country != "any" else ""
    strict_flag  = "-t" if strict else ""
    env_vars     = f"-e EXIT_COUNTRY={country} -e STRICT_NODES={'1' if strict else '0'}"
    cmd = (
        f"docker run -d --name {name} --network torplex-net "
        f"-p {socks_port}:9050 -p {http_port}:8118 -p {ctrl_port}:9051 "
        f"{env_vars} dperson/torproxy {country_flag} {strict_flag} "
        f'-p "{CONTROL_PASSWORD}"'
    )
    out, rc = run(cmd)
    if rc != 0:
        return None, f"{name}: {out}"
    return {"name": name, "socks_port": socks_port, "http_port": http_port,
            "ctrl_port": ctrl_port, "country": country, "strict": strict}, None

# ─── watchdog ─────────────────────────────────────────────────────────────────

def generate_haproxy_cfg(proxy_names: list) -> str:
    """Generate HAProxy config with all running tor proxies as backends."""
    servers = ""
    for name in sorted(proxy_names):
        idx = get_index(name)
        if idx:
            servers += f"    server {name} 127.0.0.1:{BASE_SOCKS_PORT + idx} check\n"
    return f"""global
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
{servers}
frontend stats
    bind *:8404
    mode http
    stats enable
    stats uri /stats
    stats refresh 5s
    stats admin if TRUE
"""

def reload_haproxy(proxy_names: list) -> bool:
    """Regenerate HAProxy config and reload if proxies changed."""
    global _last_proxy_set
    current_set = set(proxy_names)
    if current_set == _last_proxy_set:
        return False   # no change

    cfg = generate_haproxy_cfg(proxy_names)
    try:
        # Write new config
        with open(HAPROXY_CFG, "w") as f:
            f.write(cfg)
        # Validate
        _, rc = run("haproxy -c -f " + HAPROXY_CFG)
        if rc != 0:
            print("[haproxy] config validation failed, keeping previous")
            return False
        # Reload (graceful)
        # Try reload first, fall back to restart
    _, rc = run("systemctl reload haproxy 2>/dev/null || systemctl restart haproxy")
        if rc == 0:
            _last_proxy_set = current_set
            print(f"[haproxy] reloaded with {len(proxy_names)} backends: {', '.join(sorted(proxy_names))}")
            return True
        else:
            print("[haproxy] reload failed")
            return False
    except Exception as e:
        print(f"[haproxy] error: {e}")
        return False

async def watchdog():
    """Background task: refresh IPs for running proxies every WATCHDOG_INTERVAL seconds."""
    print(f"[watchdog] started — interval={WATCHDOG_INTERVAL}s, IP TTL={IP_TTL}s")
    await asyncio.sleep(15)   # initial delay — let containers bootstrap first
    loop = asyncio.get_event_loop()
    while True:
        try:
            proxies = get_running_proxies()
            now = time.time()

            # Update HAProxy if proxy list changed
            reload_haproxy([p["name"] for p in proxies])

            # Refresh IPs for proxies without valid cached data
            to_refresh = []
            for p in proxies:
                cached = _ip_cache.get(p["name"])
                if not cached:
                    to_refresh.append(p)
                elif not cached["ok"] or (now - cached["checked_at"]) > IP_TTL:
                    to_refresh.append(p)

            if to_refresh:
                print(f"[watchdog] refreshing IPs {len(to_refresh)}/{len(proxies)}")
                with ThreadPoolExecutor(max_workers=8) as ex:
                    for p in to_refresh:
                        ex.submit(refresh_ip, p["name"], p["socks_port"])
        except Exception as e:
            print(f"[watchdog] error: {e}")
        await asyncio.sleep(WATCHDOG_INTERVAL)

@app.on_event("startup")
async def startup():
    asyncio.create_task(watchdog())

# ─── routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    cached = sum(1 for v in _ip_cache.values() if v.get("ok"))
    return {"status": "ok", "ip_cache_size": len(_ip_cache), "ip_resolved": cached}

@app.get("/countries")
def list_countries():
    return COUNTRIES

@app.get("/proxies")
def list_proxies():
    out, _ = run("docker ps -a --filter 'name=tor-proxy-' --format '{{json .}}'")
    containers = [json.loads(l) for l in out.splitlines() if l]
    if not containers:
        return []

    names = " ".join(c["Names"] for c in containers)
    inspect_out, _ = run(f"docker inspect {names}")
    try:
        inspected = {d["Name"].lstrip("/"): d for d in json.loads(inspect_out)}
    except Exception:
        inspected = {}

    proxies = []
    for c in containers:
        name = c["Names"]
        idx  = get_index(name)
        if not idx:
            continue
        country, strict = "any", False
        try:
            for e in inspected.get(name, {}).get("Config", {}).get("Env", []):
                if e.startswith("EXIT_COUNTRY="): country = e.split("=",1)[1]
                if e.startswith("STRICT_NODES="):  strict  = e.split("=",1)[1] == "1"
        except Exception:
            pass
        ip_data = _ip_cache.get(name, {})
        proxies.append({
            "id": c["ID"], "name": name,
            "status": c["Status"], "running": c["State"] == "running",
            "socks_port": BASE_SOCKS_PORT + idx,
            "http_port":  BASE_HTTP_PORT  + idx,
            "ctrl_port":  BASE_CTRL_PORT  + idx,
            "country": country, "strict": strict,
            "exit_ip":          ip_data.get("ip"),
            "exit_country":     ip_data.get("country"),
            "exit_country_name":ip_data.get("country_name"),
            "ip_ok":            ip_data.get("ok", False),
            "ip_checked_at":    ip_data.get("checked_at"),
        })
    return sorted(proxies, key=lambda p: get_index(p["name"]) or 0)

@app.post("/proxies")
def create_proxy(body: ProxyCreate):
    count   = max(1, min(body.count or 1, MAX_PROXIES))
    country = body.country or "any"
    strict  = body.strict  or False
    indexes = reserve_indexes(count)
    if not indexes:
        raise HTTPException(status_code=500, detail="Could not reserve proxy slots")
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=min(count, 10)) as ex:
        futures = {ex.submit(spawn_one, idx, country, strict): idx for idx in indexes}
        for fut in futures:
            res, err = fut.result()
            if res: results.append(res)
            else:   errors.append(err)
    if not results:
        raise HTTPException(status_code=500, detail=f"All spawns failed: {errors}")
    return {"created": len(results), "proxies": results, "errors": errors}

@app.post("/proxies/prune")
def prune_proxies():
    count = prune_exited()
    return {"pruned": count}

@app.delete("/proxies/{name}")
def delete_proxy(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid name")
    _ip_cache.pop(name, None)
    run(f"docker stop {name}"); run(f"docker rm {name}")
    return {"deleted": name}

@app.post("/proxies/{name}/renew")
def renew_circuit(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid name")
    idx = get_index(name)
    _ip_cache.pop(name, None)   # invalidate cache → watchdog will re-fetch
    ok  = tor_control(BASE_CTRL_PORT + idx, "NEWNYM")
    if not ok:
        run(f"docker restart {name}")
        return {"renewed": name, "method": "restart"}
    return {"renewed": name, "method": "newnym"}

@app.get("/proxies/{name}/ip")
def proxy_ip(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid name")
    return _ip_cache.get(name, {"ip": None, "ok": False})

@app.get("/settings")
def get_settings():
    return {
        "haproxy_port": 9050, "mode": "balanced",
        "control_password": CONTROL_PASSWORD,
        "base_socks_port": BASE_SOCKS_PORT, "base_http_port": BASE_HTTP_PORT,
        "base_ctrl_port": BASE_CTRL_PORT, "newnym_cooldown_s": 10,
        "max_proxies": MAX_PROXIES,
        "watchdog_interval_s": WATCHDOG_INTERVAL, "ip_ttl_s": IP_TTL,
    }
