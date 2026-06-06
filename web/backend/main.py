from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import subprocess, json, re, socket, time, asyncio
from concurrent.futures import ThreadPoolExecutor
import os

app = FastAPI(title="Torplex API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CONTROL_PASSWORD  = "torplex"
BASE_SOCKS_PORT   = 10800
BASE_HTTP_PORT    = 11800
BASE_CTRL_PORT    = 12800
MAX_PROXIES       = 50
WATCHDOG_INTERVAL = 30    # seconds between watchdog cycles
HAPROXY_CFG       = "/etc/haproxy/haproxy.cfg"
IP_TTL            = 120   # seconds before re-checking a known IP

_last_proxy_set: set = set()

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

# ─── state caches ─────────────────────────────────────────────────────────────
# _proxy_state: name → full proxy dict (updated by watchdog + CRUD ops)
# _ip_cache:    name → {ip, country, country_name, city, checked_at, ok}
_proxy_state: dict = {}
_ip_cache:    dict = {}

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
            _proxy_state.pop(n, None)
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
        # Step 1: get the exit IP via api.ipify.org (same URL as manual test)
        ip_out, rc = run(
            f'curl -s --socks5-hostname 127.0.0.1:{socks_port} --max-time 12 https://api.ipify.org',
            timeout=20
        )
        if rc != 0 or not ip_out or not ip_out.strip():
            return {"ip": None, "country": None, "country_name": None, "city": None, "ok": False}
        ip = ip_out.strip()

        # Step 2: geolocate that IP (direct, no proxy needed — it's public info)
        geo_out, rc2 = run(f'curl -s --max-time 5 http://ip-api.com/json/{ip}?fields=countryCode,country,city', timeout=10)
        country, country_name, city = None, None, None
        if rc2 == 0 and geo_out:
            try:
                g = json.loads(geo_out)
                country      = g.get("countryCode")
                country_name = g.get("country")
                city         = g.get("city")
            except Exception:
                pass
        return {"ip": ip, "country": country, "country_name": country_name, "city": city, "ok": True}
    except Exception:
        pass
    return {"ip": None, "country": None, "country_name": None, "city": None, "ok": False}

def refresh_ip(name: str, socks_port: int):
    result = fetch_ip(socks_port)
    result["checked_at"] = time.time()
    _ip_cache[name] = result
    status = f"{result['ip']} ({result['country']})" if result["ok"] else "no route yet"
    print(f"[watchdog] {name}: {status}")

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
        f"docker run -d --name {name} --network torplex-net --sysctl net.ipv6.conf.all.disable_ipv6=1 "
        f"-p 0.0.0.0:{socks_port}:9050 -p 0.0.0.0:{http_port}:8118 -p 0.0.0.0:{ctrl_port}:9051 "
        f"{env_vars} dperson/torproxy {country_flag} {strict_flag} "
        f'-p "{CONTROL_PASSWORD}"'
    )
    out, rc = run(cmd)
    if rc != 0:
        return None, f"{name}: {out}"
    proxy = {"name": name, "socks_port": socks_port, "http_port": http_port,
             "ctrl_port": ctrl_port, "country": country, "strict": strict,
             "status": "Up", "running": True,
             "exit_ip": None, "exit_country": None, "exit_country_name": None,
             "ip_ok": False, "ip_checked_at": None}
    _proxy_state[name] = proxy
    return proxy, None

# ─── docker state refresh (runs in thread, never blocks event loop) ────────────

def _refresh_proxy_state():
    """Full docker ps + inspect → update _proxy_state. Called from executor."""
    out, _ = run("docker ps -a --filter 'name=tor-proxy-' --format '{{json .}}'")
    containers = [json.loads(l) for l in out.splitlines() if l]
    if not containers:
        _proxy_state.clear()
        return []

    names = " ".join(c["Names"] for c in containers)
    inspect_out, _ = run(f"docker inspect {names}")
    try:
        inspected = {d["Name"].lstrip("/"): d for d in json.loads(inspect_out)}
    except Exception:
        inspected = {}

    seen = set()
    for c in containers:
        name = c["Names"]
        idx  = get_index(name)
        if not idx:
            continue
        seen.add(name)
        country, strict = "any", False
        try:
            for e in inspected.get(name, {}).get("Config", {}).get("Env", []):
                if e.startswith("EXIT_COUNTRY="): country = e.split("=", 1)[1]
                if e.startswith("STRICT_NODES="):  strict  = e.split("=", 1)[1] == "1"
        except Exception:
            pass
        ip_data = _ip_cache.get(name, {})
        _proxy_state[name] = {
            "id": c["ID"], "name": name,
            "status": c["Status"], "running": c["State"] == "running",
            "socks_port": BASE_SOCKS_PORT + idx,
            "http_port":  BASE_HTTP_PORT  + idx,
            "ctrl_port":  BASE_CTRL_PORT  + idx,
            "country": country, "strict": strict,
            "exit_ip":           ip_data.get("ip"),
            "exit_country":      ip_data.get("country"),
            "exit_country_name": ip_data.get("country_name"),
            "ip_ok":             ip_data.get("ok", False),
            "ip_checked_at":     ip_data.get("checked_at"),
        }
    # remove stale entries
    for stale in set(_proxy_state) - seen:
        _proxy_state.pop(stale, None)

    return [_proxy_state[n] for n in seen]

def _merge_ip_into_state():
    """Merge current _ip_cache into _proxy_state (fast, no docker calls)."""
    for name, proxy in _proxy_state.items():
        ip_data = _ip_cache.get(name, {})
        proxy["exit_ip"]           = ip_data.get("ip")
        proxy["exit_country"]      = ip_data.get("country")
        proxy["exit_country_name"] = ip_data.get("country_name")
        proxy["ip_ok"]             = ip_data.get("ok", False)
        proxy["ip_checked_at"]     = ip_data.get("checked_at")

# ─── HAProxy ──────────────────────────────────────────────────────────────────

def generate_haproxy_cfg(proxy_names: list) -> str:
    servers = ""
    for name in sorted(proxy_names):
        idx = get_index(name)
        if idx:
            servers += f"    server {name} 127.0.0.1:{BASE_SOCKS_PORT + idx}\n"
    return f"""global
    log stdout format raw local0
    maxconn 50000
defaults
    log     global
    mode    tcp
    timeout connect 10s
    timeout client  120s
    timeout server  120s
    option  tcplog
    retries         3
    option  redispatch

frontend tor_in
    bind *:9050
    default_backend tor_pool

backend tor_pool
    balance roundrobin
    timeout connect 10s
    retries         2
    option  redispatch
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
    global _last_proxy_set
    current_set = set(proxy_names)
    if current_set == _last_proxy_set:
        return False

    cfg = generate_haproxy_cfg(proxy_names)
    try:
        with open(HAPROXY_CFG, "w") as f:
            f.write(cfg)
        _, rc = run("haproxy -c -f " + HAPROXY_CFG)
        if rc != 0:
            print("[haproxy] config validation failed")
            return False
        # USR2 = graceful reload (works with systemd Type=simple + -db flag)
        pid_out, _ = run("systemctl show -p MainPID --value torplex-haproxy")
        pid = pid_out.strip()
        if pid and pid != "0":
            _, rc = run(f"kill -USR2 {pid}")
        else:
            _, rc = run("systemctl restart torplex-haproxy")
        if rc == 0:
            _last_proxy_set = current_set
            print(f"[haproxy] reloaded — {len(proxy_names)} backends: {', '.join(sorted(proxy_names))}")
            return True
        print("[haproxy] reload failed")
        return False
    except Exception as e:
        print(f"[haproxy] error: {e}")
        return False

# ─── watchdog ─────────────────────────────────────────────────────────────────

async def watchdog():
    print(f"[watchdog] started — interval={WATCHDOG_INTERVAL}s, IP TTL={IP_TTL}s")
    await asyncio.sleep(15)
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=10)

    while True:
        try:
            # Refresh docker state in thread (non-blocking)
            proxies = await loop.run_in_executor(executor, _refresh_proxy_state)

            # HAProxy sync (fast file write + kill -USR2, ok in thread)
            await loop.run_in_executor(executor, reload_haproxy, [p["name"] for p in proxies])

            # IP refresh for stale/missing entries
            now = time.time()
            to_refresh = []
            for p in proxies:
                if not p.get("running"):
                    continue
                cached = _ip_cache.get(p["name"])
                if not cached or not cached["ok"] or (now - cached.get("checked_at", 0)) > IP_TTL:
                    to_refresh.append(p)

            if to_refresh:
                print(f"[watchdog] refreshing IPs {len(to_refresh)}/{len(proxies)}")
                # Submit all in executor, gather results
                futs = [
                    loop.run_in_executor(executor, refresh_ip, p["name"], p["socks_port"])
                    for p in to_refresh
                ]
                await asyncio.gather(*futs)
                # Merge fresh IPs into state cache immediately
                _merge_ip_into_state()

        except Exception as e:
            print(f"[watchdog] error: {e}")

        await asyncio.sleep(WATCHDOG_INTERVAL)

@app.on_event("startup")
async def startup():
    # Bootstrap state cache from running containers (non-blocking)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _refresh_proxy_state)
    asyncio.create_task(watchdog())

# ─── routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    cached = sum(1 for v in _ip_cache.values() if v.get("ok"))
    return {"status": "ok", "proxies": len(_proxy_state),
            "ip_cache_size": len(_ip_cache), "ip_resolved": cached}

@app.get("/countries")
def list_countries():
    return COUNTRIES

@app.get("/proxies")
def list_proxies():
    """Returns from memory cache — instant, no Docker calls."""
    _merge_ip_into_state()
    return sorted(_proxy_state.values(), key=lambda p: get_index(p["name"]) or 0)

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
    _proxy_state.pop(name, None)
    run(f"docker stop {name}"); run(f"docker rm {name}")
    return {"deleted": name}

@app.post("/proxies/{name}/renew")
def renew_circuit(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid name")
    idx = get_index(name)
    _ip_cache.pop(name, None)
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
