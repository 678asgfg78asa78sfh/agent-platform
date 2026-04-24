"""Healthcheck Modul — prueft ob URLs/Services erreichbar sind."""
import json, sys, urllib.request, urllib.error, time, socket

MODULE = {
    "name": "healthcheck",
    "description": "Prueft ob URLs und Services erreichbar sind",
    "version": "1.0",
    "settings": {
        "default_timeout": {"type": "number", "label": "Timeout (Sekunden)", "default": 5},
    },
    "tools": [
        {"name": "healthcheck.url", "description": "Prueft ob eine URL erreichbar ist und gibt Status-Code zurueck", "params": ["url"]},
        {"name": "healthcheck.port", "description": "Prueft ob ein Host:Port erreichbar ist (TCP connect)", "params": ["host", "port"]},
        {"name": "healthcheck.multi", "description": "Prueft mehrere URLs auf einmal (komma-getrennt)", "params": ["urls"]},
    ],
}

def handle_tool(tool_name, params, config):
    timeout = config.get("default_timeout", 5)

    if tool_name == "healthcheck.url":
        url = params[0] if params else ""
        return _check_url(url, timeout)

    elif tool_name == "healthcheck.port":
        host = params[0] if params else ""
        port = int(params[1]) if len(params) > 1 else 80
        return _check_port(host, port, timeout)

    elif tool_name == "healthcheck.multi":
        urls_str = params[0] if params else ""
        urls = [u.strip() for u in urls_str.split(",") if u.strip()]
        results = []
        for url in urls:
            r = _check_url(url, timeout)
            status = "OK" if r["success"] else "FAIL"
            results.append(f"  [{status}] {url} — {r['data']}")
        return {"success": True, "data": f"{len(urls)} URLs geprueft:\n" + "\n".join(results)}

    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}

def _check_url(url, timeout):
    if not url:
        return {"success": False, "data": "Keine URL angegeben"}
    if not url.startswith("http"):
        url = "https://" + url
    try:
        start = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "AgentPlatform-Healthcheck/1.0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        ms = int((time.time() - start) * 1000)
        return {"success": True, "data": f"HTTP {resp.status} — {ms}ms"}
    except urllib.error.HTTPError as e:
        return {"success": False, "data": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"success": False, "data": f"Nicht erreichbar: {e.reason}"}
    except Exception as e:
        return {"success": False, "data": str(e)}

def _check_port(host, port, timeout):
    if not host:
        return {"success": False, "data": "Kein Host angegeben"}
    try:
        start = time.time()
        sock = socket.create_connection((host, port), timeout=timeout)
        ms = int((time.time() - start) * 1000)
        sock.close()
        return {"success": True, "data": f"{host}:{port} erreichbar — {ms}ms"}
    except socket.timeout:
        return {"success": False, "data": f"{host}:{port} Timeout nach {timeout}s"}
    except ConnectionRefusedError:
        return {"success": False, "data": f"{host}:{port} Connection refused"}
    except Exception as e:
        return {"success": False, "data": f"{host}:{port} Fehler: {e}"}


if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            if req.get("action") == "describe":
                print(json.dumps(MODULE), flush=True)
            elif req.get("action") == "handle_tool":
                result = handle_tool(req["tool"], req.get("params", []), req.get("config", {}))
                print(json.dumps(result), flush=True)
            else:
                print(json.dumps({"error": f"Unknown action: {req.get('action')}"}), flush=True)
        except Exception as e:
            print(json.dumps({"error": str(e)}), flush=True)
