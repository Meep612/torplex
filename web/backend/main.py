from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import subprocess, json, re

app = FastAPI(title="Torplex API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def run(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout.strip(), result.returncode

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/proxies")
def list_proxies():
    out, _ = run("docker ps -a --filter 'name=tor-proxy' --format '{{json .}}'")
    proxies = []
    for line in out.splitlines():
        if line:
            c = json.loads(line)
            proxies.append({
                "id": c["ID"],
                "name": c["Names"],
                "status": c["Status"],
                "ports": c["Ports"],
            })
    return proxies

@app.post("/proxies")
def create_proxy():
    out, _ = run("docker ps -a --filter 'name=tor-proxy' --format '{{.Names}}'")
    existing = [l for l in out.splitlines() if l]
    index = len(existing) + 1
    port = 10800 + index
    name = f"tor-proxy-{index}"
    _, rc = run(
        f"docker run -d --name {name} --network torplex-net "
        f"-p {port}:9050 torplex-tor:latest"
    )
    if rc != 0:
        raise HTTPException(status_code=500, detail="Failed to start proxy")
    return {"name": name, "socks_port": port}

@app.delete("/proxies/{name}")
def delete_proxy(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid proxy name")
    run(f"docker stop {name}")
    run(f"docker rm {name}")
    return {"deleted": name}

@app.post("/proxies/{name}/renew")
def renew_circuit(name: str):
    if not re.match(r'^tor-proxy-\d+$', name):
        raise HTTPException(status_code=400, detail="Invalid proxy name")
    run(f"docker restart {name}")
    return {"renewed": name}
