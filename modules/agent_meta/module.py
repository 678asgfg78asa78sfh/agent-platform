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
        {"name": "agent.module_graph", "description": "Zeigt Modul-Abhaengigkeiten, Links, Berechtigungen und Risiko-Markierungen. Nutze das, wenn unklar ist welches Modul worauf zugreift.", "params": ["modul_id_optional"]},
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
    elif tool_name == "agent.module_graph":
        name = params[0] if params else ""
        return _module_graph(port, token, name)
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
        "AGENT_CAPABILITIES",
        f"Modul: {data.get('id', caller)}",
        f"LLM Backend: {data.get('llm_backend', '-')}",
        f"RAG Pool: {data.get('rag_pool') or '-'}",
        "",
    ]

    rust_tools = data.get("rust_tools") or []
    py_tools = data.get("python_tools") or []
    if rust_tools:
        lines.append(f"Rust-Tools ({len(rust_tools)}): " + _join_tool_names(rust_tools))
    if py_tools:
        if rust_tools:
            lines.append("")
        lines.append(f"Python-Tools ({len(py_tools)}):")
        grouped = {}
        for t in py_tools:
            via = t.get("via_python_module") or "python"
            grouped.setdefault(via, []).append(t)
        for via in sorted(grouped):
            lines.append(f"- {via}: {_join_tool_names(grouped[via])}")
    if not rust_tools and not py_tools:
        lines.append("(keine Tools verfuegbar)")

    linked = data.get("linked_modules") or []
    if linked:
        shown = ", ".join(str(x) for x in linked[:30])
        suffix = f" (+{len(linked) - 30} weitere)" if len(linked) > 30 else ""
        lines.extend(["", f"Verlinkte Modulinstanzen ({len(linked)}):", "- " + shown + suffix])
    lines.extend([
        "",
        "Details zu einem Modul: agent.module_info(modul_name).",
    ])

    return {"success": True, "data": _cap_text("\n".join(lines), 3600)}

def _join_tool_names(tools):
    names = []
    for t in tools:
        name = str(t.get("name") or "?")
        params = t.get("params") or []
        if params:
            name += "(" + ", ".join(str(p) for p in params[:4]) + (", ..." if len(params) > 4 else "") + ")"
        else:
            name += "()"
        names.append(name)
    return ", ".join(names)

def _cap_text(text, max_chars):
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[gekuerzt; Details mit agent.module_info(modul_name)]"

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

def _module_graph(port, token, name):
    cfg, err = _snapshot_or_api("config_snapshot", port, "/api/config", token)
    if not cfg:
        return _load_failed("/api/config", err)
    modules_meta, meta_err = _snapshot_or_api("modules_snapshot", port, "/api/modules", token)
    if not modules_meta:
        modules_meta = {"modules": []}

    modules = cfg.get("module") or []
    by_id = {m.get("id"): m for m in modules if isinstance(m, dict) and m.get("id")}
    py_by_name = {m.get("name"): m for m in modules_meta.get("modules", []) if isinstance(m, dict) and m.get("name")}
    py_tool_counts = {name: len((meta.get("tools") or [])) for name, meta in py_by_name.items()}
    selected = _select_graph_modules(modules, name)
    if name and not selected:
        return {"success": False, "data": f"Modul/Filter '{name}' nicht gefunden"}

    lines = [
        "MODULE_GRAPH",
        "Legende: LINK = explizite linked_modules-Kante, PERM = direkte Berechtigung, PY = dadurch sichtbares Python-Toolset, RISK = breiter/gefährlicher Zugriff.",
        f"module_count: {len(modules)}",
        f"shown: {len(selected)}",
        "",
    ]

    for m in selected:
        mid = m.get("id") or "?"
        typ = m.get("typ") or "?"
        llm = m.get("llm_backend") or "-"
        persistent = m.get("persistent", True)
        perms = [str(x) for x in (m.get("berechtigungen") or [])]
        links = [str(x) for x in (m.get("linked_modules") or [])]
        input_enhancers = [str(x) for x in (m.get("input_enhancers") or [])]
        output_enhancers = [str(x) for x in (m.get("output_enhancers") or [])]
        combined_enhancers = [str(x) for x in (m.get("combined_enhancers") or [])]
        risks = _risk_labels(perms, links, typ, persistent)
        lines.append(f"[{mid}] typ={typ} llm={llm} persistent={persistent}")
        lines.append("  PERM: " + (", ".join(perms) if perms else "-"))
        if risks:
            lines.append("  RISK: " + ", ".join(risks))
        if links:
            lines.append("  LINK:")
            for link in links:
                target = by_id.get(link)
                target_typ = (target or {}).get("typ") or _py_type_from_link(link, py_by_name) or "unknown"
                py_name = _py_type_from_link(link, py_by_name)
                py_suffix = ""
                if py_name:
                    py_suffix = f" | PY {py_name} tools={py_tool_counts.get(py_name, 0)}"
                lines.append(f"    -> {link} typ={target_typ}{py_suffix}")
        else:
            lines.append("  LINK: -")
        if input_enhancers or output_enhancers or combined_enhancers:
            lines.append("  ENHANCER:")
            if input_enhancers:
                lines.append("    input: " + ", ".join(input_enhancers))
            if output_enhancers:
                lines.append("    output: " + ", ".join(output_enhancers))
            if combined_enhancers:
                lines.append("    combined: " + ", ".join(combined_enhancers))
        inbound = [src.get("id") for src in modules if mid in (src.get("linked_modules") or [])]
        if inbound:
            lines.append("  INBOUND: " + ", ".join(str(x) for x in inbound[:12]))
        lines.append("")

    if meta_err:
        lines.append(f"Hinweis: /api/modules nicht geladen: {meta_err}")
    return {"success": True, "data": "\n".join(lines).rstrip()}

def _select_graph_modules(modules, name):
    needle = str(name or "").strip().lower()
    if not needle:
        return modules
    out = []
    for m in modules:
        mid = str(m.get("id") or "")
        typ = str(m.get("typ") or "")
        if needle in mid.lower() or needle == typ.lower():
            out.append(m)
            continue
        if any(needle in str(link).lower() for link in (m.get("linked_modules") or [])):
            out.append(m)
    return out

def _py_type_from_link(link, py_by_name):
    value = str(link or "")
    for py_name in py_by_name:
        if value == py_name or value.startswith(f"{py_name}."):
            return py_name
    return ""

def _risk_labels(perms, links, typ, persistent):
    risks = []
    perm_set = set(perms or [])
    if "py.*" in perm_set:
        risks.append("py.*")
    if "files.*" in perm_set or "files" in perm_set:
        risks.append("files")
    if "shell" in perm_set:
        risks.append("shell")
    if "agent.spawn" in perm_set or "agent.*" in perm_set:
        risks.append("spawns_agents")
    if "aufgaben" in perm_set:
        risks.append("can_create_tasks")
    if any(str(link).startswith("telegram_bot.") for link in links or []):
        risks.append("telegram_channel_link")
    if typ == "chat" and len(links or []) >= 10:
        risks.append("large_tool_surface")
    if not persistent:
        risks.append("temporary")
    return risks

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

    erstellt = _list_or_empty(data.get("erstellt"))
    gestartet = _list_or_empty(data.get("gestartet"))
    erledigt = _list_or_empty(data.get("erledigt"))

    lines = [f"Aufgaben: {len(erstellt)} wartend, {len(gestartet)} laufend, {len(erledigt)} erledigt", ""]

    for t in gestartet:
        lines.append(f"  [LAUFEND]  {_task_label(t)}")
    for t in erstellt:
        lines.append(f"  [WARTEND]  {_task_label(t)}")
    for t in erledigt[-5:]:
        status = _task_value(t, "status", "?")[:8]
        tool = _task_label(t, 30)
        result = _task_value(t, "ergebnis", "")[:60]
        lines.append(f"  [{status:8s}] {tool:30s} → {result}")

    return {"success": True, "data": "\n".join(lines)}

def _list_or_empty(value):
    return value if isinstance(value, list) else []

def _task_value(task, key, default=""):
    if not isinstance(task, dict):
        return default
    value = task.get(key)
    if value is None:
        return default
    return str(value)

def _task_label(task, limit=60):
    if not isinstance(task, dict):
        return "?"
    label = task.get("tool") or task.get("anweisung") or task.get("id") or "?"
    return str(label)[:limit]


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
