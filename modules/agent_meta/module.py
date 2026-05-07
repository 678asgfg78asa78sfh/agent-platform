"""Agent Meta-Modul — Zeigt dem LLM was der Agent kann, welche Module und Tools verfuegbar sind."""
import json, sys, urllib.error, urllib.parse, urllib.request

MODULE = {
    "name": "agent_meta",
    "description": "Zeigt die fuer den aufrufenden Agenten verfuegbaren Tools und Modul-Infos",
    "version": "1.1",
    "settings": {
        "admin_port": {"type": "number", "label": "Admin Port", "default": 8090},
    },
    "tools": [
        {"name": "agent.capabilities", "description": "Zeigt nur die Tools, die dieser Agent wirklich verwenden darf. Nutze das wenn du wissen willst was du kannst.", "params": []},
        {"name": "agent.module_info", "description": "Zeigt Details zu einem bestimmten Modul (Settings, Tools, Beschreibung)", "params": ["modul_name"]},
        {"name": "agent.instances", "description": "Zeigt alle aktiven Modul-Instanzen und deren Konfiguration", "params": []},
        {"name": "agent.aufgaben", "description": "Zeigt aktuelle Aufgaben im Pool (wartend, laufend, erledigt)", "params": []},
    ],
}

_RUNTIME_CONFIG = {}

def handle_tool(tool_name, params, config):
    global _RUNTIME_CONFIG
    _RUNTIME_CONFIG = config if isinstance(config, dict) else {}
    port = config.get("admin_port", 8090)
    token = config.get("api_auth_token") or ""

    if tool_name == "agent.capabilities":
        return _capabilities(port, token)
    elif tool_name == "agent.module_info":
        name = params[0] if params else ""
        return _module_info(port, token, name)
    elif tool_name == "agent.instances":
        return _instances(port, token)
    elif tool_name == "agent.aufgaben":
        return _aufgaben(port, token)
    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}

def _api(port, path, token):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)

def _load_failed(path, err):
    suffix = f": {err}" if err else ""
    return {"success": False, "data": f"Konnte {path} nicht laden{suffix}"}

def _capabilities(port, token):
    caller = str(_RUNTIME_CONFIG.get("modul_id") or "").strip()
    if not caller:
        return {
            "success": False,
            "data": "Aufrufende Modul-ID fehlt. Ich gebe keinen globalen Toolkatalog aus, weil das falsche Tools suggerieren wuerde.",
        }
    data, err = _api(port, f"/api/module-capabilities/{_urlquote(caller)}", token)
    if not data or data.get("error"):
        return _load_failed(f"/api/module-capabilities/{caller}", data.get("error") if isinstance(data, dict) else err)

    lines = [
        f"Verfuegbare Tools fuer '{data.get('id', caller)}':",
        f"LLM Backend: {data.get('llm_backend', '-')}",
        f"RAG Pool: {data.get('rag_pool') or '-'}",
        "",
    ]

    rust_tools = data.get("rust_tools") or []
    py_tools = data.get("python_tools") or []
    if rust_tools:
        lines.append("Rust-Tools:")
        for t in rust_tools:
            params = ", ".join(t.get("params") or [])
            lines.append(f"- {t.get('name')}({params}) — {t.get('description', '')}")
    if py_tools:
        if rust_tools:
            lines.append("")
        lines.append("Python-Tools:")
        for t in py_tools:
            params = ", ".join(t.get("params") or [])
            via = t.get("via_python_module") or "python"
            lines.append(f"- {t.get('name')}({params}) [{via}] — {t.get('description', '')}")
    if not rust_tools and not py_tools:
        lines.append("(keine Tools verfuegbar)")

    linked = data.get("linked_modules") or []
    if linked:
        lines.extend(["", "Verlinkte Modulinstanzen:", "- " + ", ".join(linked)])

    return {"success": True, "data": "\n".join(lines)}

def _module_info(port, token, name):
    if not name:
        return {"success": False, "data": "Kein Modulname angegeben"}
    data, err = _snapshot_or_api("modules_snapshot", port, "/api/modules", token)
    if not data:
        return _load_failed("/api/modules", err)

    for m in data.get("modules", []):
        if m["name"] == name:
            info = [f"Modul: {m['name']}"]
            info.append(f"Beschreibung: {m.get('description', '')}")
            info.append(f"Version: {m.get('version', '?')}")
            info.append(f"Source: {m.get('source', '?')}")
            if m.get("settings"):
                info.append("Settings:")
                for k, v in m["settings"].items():
                    info.append(f"  {k}: {v.get('label', k)} (typ={v.get('type', '?')}, default={v.get('default', '')})")
            if m.get("tools"):
                info.append("Tools:")
                for t in m["tools"]:
                    params = ", ".join(t.get("params", []))
                    info.append(f"  {t['name']}({params}) — {t.get('description', '')}")
            return {"success": True, "data": "\n".join(info)}

    return {"success": False, "data": f"Modul '{name}' nicht gefunden"}

def _instances(port, token):
    data, err = _snapshot_or_api("instances_snapshot", port, "/api/config", token)
    if not data:
        return _load_failed("/api/config", err)

    lines = [f"Platform: {data.get('name', '?')}",
             f"Admin-Port: {data.get('web_port', '?')}",
             f"LLM Backends: {_llm_count(data.get('llm_backends', []))}",
             f"Aktive Instanzen: {len(data.get('module', []))}", ""]

    for m in data.get("module", []):
        port_info = ""
        port = m.get("port") or m.get("settings", {}).get("port")
        if port:
            port_info = f" (Port {port})"
        lines.append(f"  {m['id']:20s} typ={m['typ']:12s} llm={m.get('llm_backend', '?')}{port_info}")

    return {"success": True, "data": "\n".join(lines)}

def _snapshot_or_api(snapshot_key, port, path, token):
    snap = _RUNTIME_CONFIG.get(snapshot_key)
    if snap:
        return snap, None
    return _api(port, path, token)

def _llm_count(value):
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    return 0

def _urlquote(value):
    return urllib.parse.quote(str(value), safe="")

def _aufgaben(port, token):
    data, err = _api(port, "/api/aufgaben", token)
    if not data:
        return _load_failed("/api/aufgaben", err)

    erstellt = data.get("erstellt", [])
    gestartet = data.get("gestartet", [])
    erledigt = data.get("erledigt", [])

    lines = [f"Aufgaben: {len(erstellt)} wartend, {len(gestartet)} laufend, {len(erledigt)} erledigt", ""]

    for t in gestartet:
        lines.append(f"  [LAUFEND]  {t.get('tool', t.get('anweisung', '?'))[:60]}")
    for t in erstellt:
        lines.append(f"  [WARTEND]  {t.get('tool', t.get('anweisung', '?'))[:60]}")
    for t in erledigt[-5:]:
        status = t.get("status", "?")
        tool = t.get("tool") or t.get("anweisung", "?")
        result = (t.get("ergebnis") or "")[:60]
        lines.append(f"  [{status:8s}] {str(tool)[:30]:30s} → {result}")

    return {"success": True, "data": "\n".join(lines)}


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
