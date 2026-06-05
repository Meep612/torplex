from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import subprocess, json, re, socket

app = FastAPI(title="Torplex API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CONTROL_PASSWORD = "torplex"
BASE_SOCKS_PORT  = 10800
BASE_HTTP_PORT   = 11800
BASE_CTRL_PORT   = 12800

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
    strict: Optional[bool] = False

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip(), r.returncode

def get_index(name):
    m = re.match(r'^tor-proxy-(\d+)$', name)
    return int(m.group(1)) if m else None

def next_index():
    out, _ = run("docker ps -a --filter 'name=tor-proxy-' --format '{{.Names}}'")
    used = [int(re.search(r'\d+', n).group()) for n in out.splitlines() if n and re.search(r'\d+', n)]
    i = 1
    while i in used:
        i += 1
    return i

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

def get_proxy_ip(socks_port: int):
    try:
        out, rc = run(
            f'curl -s --socks5-hostname 127.0.0.1:{socks_port} '
            f'--max-time 10 https://ipapi.co/json/'
        )
        if rc == 0 and out:
            d = json.loads(out)
            return {
                "ip":          d.get("ip"),
                "country":     d.get("country_code"),
                "country_name":d.get("country_name"),
                "city":        d.get("city"),
            }
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
    out, _ = run("docker ps -a --filter 'name=tor-proxy-' --format '{{json .}}'")
    proxies = []
    for line in out.splitlines():
        if not line:
            continue
        c = json.loads(line)
        name = c["Names"]
        idx = get_index(name)
        socks_port = BASE_SOCKS_PORT + idx if idx else 0
        http_port  = BASE_HTTP_PORT  + idx if idx else 0
        ctrl_port  = BASE_CTRL_PORT  + idx if idx else 0
        env_out, _ = run(f"docker inspect {name} --format '{{{{json .Config.Env}}}}'")
        country, strict = "any", False
        try:
            for e in json.loads(env_out):
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
    return proxies

@app.post("/proxies")
def create_proxy(body: ProxyCreate):
    idx        = next_index()
    name       = f"tor-proxy-{idx}"
    socks_port = BASE_SOCKS_PORT + idx
    http_port  = BASE_HTTP_PORT  + idx
    ctrl_port  = BASE_CTRL_PORT  + idx
    country_flag = f'-l "{body.country}"' if body.country and body.country != "any" else ""
    strict_flag  = "-t" if body.strict else ""
    env_vars     = f"-e EXIT_COUNTRY={body.country} -e STRICT_NODES={'1' if body.strict else '0'}"
    cmd = (
        f"docker run -d --name {name} --network torplex-net "
        f"-p {socks_port}:9050 -p {http_port}:8118 -p {ctrl_port}:9051 "
        f"{env_vars} dperson/torproxy {country_flag} {strict_flag} "
        f'-p "{CONTROL_PASSWORD}"'
    )
    out, rc = run(cmd)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"Failed to start proxy: {out}")
    return {"name": name, "socks_port": socks_port, "http_port": http_port,
            "ctrl_port": ctrl_port, "country": body.country, "strict": body.strict}

@app.delete("/proxies/{name}")
def delete_proxy(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid proxy name")
    run(f"docker stop {name}"); run(f"docker rm {name}")
    return {"deleted": name}

@app.post("/proxies/{name}/renew")
def renew_circuit(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid proxy name")
    idx = get_index(name)
    ok  = tor_control(BASE_CTRL_PORT + idx, "NEWNYM")
    if not ok:
        run(f"docker restart {name}")
        return {"renewed": name, "method": "restart"}
    return {"renewed": name, "method": "newnym"}

@app.get("/proxies/{name}/ip")
def proxy_ip(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid proxy name")
    idx = get_index(name)
    return get_proxy_ip(BASE_SOCKS_PORT + idx)

@app.get("/settings")
def get_settings():
    return {
        "haproxy_port": 9050, "mode": "balanced",
        "control_password": CONTROL_PASSWORD,
        "base_socks_port": BASE_SOCKS_PORT,
        "base_http_port":  BASE_HTTP_PORT,
        "base_ctrl_port":  BASE_CTRL_PORT,
        "newnym_cooldown_s": 10,
    }
