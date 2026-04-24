"""Agent Meta-Modul — Zeigt dem LLM was der Agent kann, welche Module und Tools verfuegbar sind."""
import json, sys, urllib.request

MODULE = {
    "name": "agent_meta",
    "description": "Zeigt verfuegbare Module, Tools und Faehigkeiten des Agenten",
    "version": "1.0",
    "settings": {
        "admin_port": {"type": "number", "label": "Admin Port", "default": 8090},
    },
    "tools": [
        {"name": "agent.capabilities", "description": "Zeigt ALLE verfuegbaren Module und deren Tools. Nutze das wenn du wissen willst was du kannst.", "params": []},
        {"name": "agent.module_info", "description": "Zeigt Details zu einem bestimmten Modul (Settings, Tools, Beschreibung)", "params": ["modul_name"]},
        {"name": "agent.instances", "description": "Zeigt alle aktiven Modul-Instanzen und deren Konfiguration", "params": []},
        {"name": "agent.aufgaben", "description": "Zeigt aktuelle Aufgaben im Pool (wartend, laufend, erledigt)", "params": []},
    ],
}

def handle_tool(tool_name, params, config):
    port = config.get("admin_port", 8090)

    if tool_name == "agent.capabilities":
        return _capabilities(port)
    elif tool_name == "agent.module_info":
        name = params[0] if params else ""
        return _module_info(port, name)
    elif tool_name == "agent.instances":
        return _instances(port)
    elif tool_name == "agent.aufgaben":
        return _aufgaben(port)
    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}

def _api(port, path):
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)
        return json.loads(resp.read())
    except Exception as e:
        return None

def _capabilities(port):
    data = _api(port, "/api/modules")
    if not data:
        return {"success": False, "data": "Konnte /api/modules nicht laden"}

    lines = ["Verfuegbare Module:\n"]
    for m in data.get("modules", []):
        src = "Python" if m.get("source") == "python" else "Rust"
        tools = [t["name"] for t in m.get("tools", [])]
        tools_str = ", ".join(tools) if tools else "(keine)"
        lines.append(f"  [{src}] {m['name']} — {m.get('description', '')}")
        lines.append(f"         Tools: {tools_str}")
        lines.append("")

    return {"success": True, "data": "\n".join(lines)}

def _module_info(port, name):
    if not name:
        return {"success": False, "data": "Kein Modulname angegeben"}
    data = _api(port, "/api/modules")
    if not data:
        return {"success": False, "data": "Konnte /api/modules nicht laden"}

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

def _instances(port):
    data = _api(port, "/api/config")
    if not data:
        return {"success": False, "data": "Konnte /api/config nicht laden"}

    lines = [f"Platform: {data.get('name', '?')}",
             f"Admin-Port: {data.get('web_port', '?')}",
             f"LLM Backends: {len(data.get('llm_backends', []))}",
             f"Aktive Instanzen: {len(data.get('module', []))}", ""]

    for m in data.get("module", []):
        port_info = ""
        if m.get("settings", {}).get("port"):
            port_info = f" (Port {m['settings']['port']})"
        lines.append(f"  {m['id']:20s} typ={m['typ']:12s} llm={m.get('llm_backend', '?')}{port_info}")

    return {"success": True, "data": "\n".join(lines)}

def _aufgaben(port):
    data = _api(port, "/api/aufgaben")
    if not data:
        return {"success": False, "data": "Konnte /api/aufgaben nicht laden"}

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
