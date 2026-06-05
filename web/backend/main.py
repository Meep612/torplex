from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import subprocess, json, re, socket
from concurrent.futures import ThreadPoolExecutor

app = FastAPI(title="Torplex API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CONTROL_PASSWORD = "torplex"
BASE_SOCKS_PORT  = 10800
BASE_HTTP_PORT   = 11800
BASE_CTRL_PORT   = 12800
MAX_PROXIES      = 50

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

class ProxyCreate(BaseModel):
    country: Optional[str] = "any"
    strict:  Optional[bool] = False
    count:   Optional[int]  = 1

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    return r.stdout.strip(), r.returncode

def get_index(name):
    m = re.match(r'^tor-proxy-(\d+)$', name)
    return int(m.group(1)) if m else None

def used_indexes():
    """Return indexes of ALL containers (running + stopped) to avoid port conflicts."""
    out, _ = run("docker ps -a --filter 'name=tor-proxy-' --format '{{.Names}}'")
    return {int(re.search(r'\d+', n).group()) for n in out.splitlines() if n and re.search(r'\d+', n)}

def prune_exited():
    """Remove stopped/exited tor-proxy containers to free up their index slots."""
    out, _ = run("docker ps -a --filter 'name=tor-proxy-' --filter 'status=exited' --filter 'status=created' --format '{{.Names}}'")
    names = [n for n in out.splitlines() if n]
    if names:
        run("docker rm " + " ".join(names))
        print(f"[torplex] pruned {len(names)} dead containers: {', '.join(names)}")
    return len(names)

def reserve_indexes(count):
    """Prune dead containers, then return `count` free indexes atomically."""
    prune_exited()
    used = used_indexes()
    result, i = [], 1
    while len(result) < count:
        if i not in used:
            result.append(i)
            used.add(i)  # mark reserved for this batch
        i += 1
        if i > 9999:
            break
    return result

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

def get_proxy_ip(socks_port):
    try:
        out, rc = run(
            f'curl -s --socks5-hostname 127.0.0.1:{socks_port} '
            f'--max-time 10 https://ipapi.co/json/'
        )
        if rc == 0 and out:
            d = json.loads(out)
            return {"ip": d.get("ip"), "country": d.get("country_code"),
                    "country_name": d.get("country_name"), "city": d.get("city")}
    except Exception:
        pass
    return {"ip": None, "country": None, "country_name": None, "city": None}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/countries")
def list_countries():
    return COUNTRIES

@app.get("/proxies")
def list_proxies():
    # Single docker ps call
    out, _ = run("docker ps -a --filter 'name=tor-proxy-' --format '{{json .}}'")
    containers = [json.loads(l) for l in out.splitlines() if l]
    if not containers:
        return []

    # Batch docker inspect — one call for all containers
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
        socks_port = BASE_SOCKS_PORT + idx
        http_port  = BASE_HTTP_PORT  + idx
        ctrl_port  = BASE_CTRL_PORT  + idx
        country, strict = "any", False
        try:
            envs = inspected.get(name, {}).get("Config", {}).get("Env", [])
            for e in envs:
                if e.startswith("EXIT_COUNTRY="): country = e.split("=",1)[1]
                if e.startswith("STRICT_NODES="):  strict  = e.split("=",1)[1] == "1"
        except Exception:
            pass
        proxies.append({
            "id": c["ID"], "name": name,
            "status": c["Status"], "running": c["State"] == "running",
            "socks_port": socks_port, "http_port": http_port, "ctrl_port": ctrl_port,
            "country": country, "strict": strict,
        })
    return sorted(proxies, key=lambda p: get_index(p["name"]) or 0)

@app.post("/proxies")
def create_proxy(body: ProxyCreate):
    count   = max(1, min(body.count or 1, MAX_PROXIES))
    country = body.country or "any"
    strict  = body.strict  or False

    # Reserve all indexes first (no race condition)
    indexes = reserve_indexes(count)
    if not indexes:
        raise HTTPException(status_code=500, detail="Could not reserve proxy slots")

    # Spawn in parallel
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=min(count, 10)) as ex:
        futures = {ex.submit(spawn_one, idx, country, strict): idx for idx in indexes}
        for fut in futures:
            res, err = fut.result()
            if res:
                results.append(res)
            else:
                errors.append(err)

    if not results:
        raise HTTPException(status_code=500, detail=f"All spawns failed: {errors}")
    return {"created": len(results), "proxies": results, "errors": errors}

@app.post("/proxies/prune")
def prune_proxies():
    """Remove all stopped/exited proxy containers."""
    count = prune_exited()
    return {"pruned": count}

@app.delete("/proxies/{name}")
def delete_proxy(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid name")
    run(f"docker stop {name}"); run(f"docker rm {name}")
    return {"deleted": name}

@app.post("/proxies/{name}/renew")
def renew_circuit(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid name")
    idx = get_index(name)
    ok  = tor_control(BASE_CTRL_PORT + idx, "NEWNYM")
    if not ok:
        run(f"docker restart {name}")
        return {"renewed": name, "method": "restart"}
    return {"renewed": name, "method": "newnym"}

@app.get("/proxies/{name}/ip")
def proxy_ip(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid name")
    return get_proxy_ip(BASE_SOCKS_PORT + get_index(name))

@app.get("/settings")
def get_settings():
    return {
        "haproxy_port": 9050, "mode": "balanced",
        "control_password": CONTROL_PASSWORD,
        "base_socks_port": BASE_SOCKS_PORT, "base_http_port": BASE_HTTP_PORT,
        "base_ctrl_port": BASE_CTRL_PORT, "newnym_cooldown_s": 10,
        "max_proxies": MAX_PROXIES,
    }
