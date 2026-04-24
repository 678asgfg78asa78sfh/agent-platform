"""Module Builder — Ermoeglicht der KI neue Python-Module zu erstellen, testen und deployen."""
import json, sys, os, subprocess

# Modul-Verzeichnis ermitteln
MODULES_DIR = os.path.join(os.path.dirname(__file__), "..")
README_PATH = os.path.join(MODULES_DIR, "README.md")

MODULE = {
    "name": "module_builder",
    "description": "Erstellt, testet und deployt neue Python-Module fuer den Agent",
    "version": "1.0",
    "settings": {},
    "tools": [
        {"name": "module_builder.docs", "description": "Zeigt die komplette Dokumentation wie ein Python-Modul aufgebaut sein muss (Interface, Format, Beispiele)", "params": []},
        {"name": "module_builder.list", "description": "Listet alle vorhandenen Module mit ihren Tools", "params": []},
        {"name": "module_builder.inspect", "description": "Zeigt den Quellcode eines bestehenden Moduls als Referenz", "params": ["modul_name"]},
        {"name": "module_builder.scaffold", "description": "Erstellt ein Modul-Geruest mit Name, Beschreibung und Tool-Liste. Du schreibst den Code danach mit editor.create", "params": ["name", "description", "tools_komma_getrennt"]},
        {"name": "module_builder.activate", "description": "Testet und aktiviert ein Modul das bereits als modules/name/module.py existiert", "params": ["name"]},
        {"name": "module_builder.test", "description": "Testet ein Modul: ruft describe auf und prueft ob handle_tool funktioniert", "params": ["modul_name"]},
        {"name": "module_builder.delete", "description": "Loescht ein Modul (nur selbst erstellte, keine System-Module)", "params": ["modul_name"]},
    ],
}

TEMPLATE = '''"""{{DESCRIPTION}}"""
import json, sys

MODULE = {
    "name": "{{NAME}}",
    "description": "{{DESCRIPTION}}",
    "version": "1.0",
    "settings": {},
    "tools": [
        # {"name": "{{NAME}}.action", "description": "Was es tut", "params": ["param1"]},
    ],
}

def handle_tool(tool_name, params, config):
    return {"success": False, "data": f"Tool {tool_name} noch nicht implementiert"}


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
'''

SYSTEM_MODULES = ["sysinfo", "healthcheck", "agent_meta", "mailstore", "imap", "smtp", "pop3", "editor", "module_builder"]


def handle_tool(tool_name, params, config):
    try:
        if tool_name == "module_builder.docs":
            return _docs()
        elif tool_name == "module_builder.list":
            return _list()
        elif tool_name == "module_builder.inspect":
            return _inspect(params[0] if params else "")
        elif tool_name == "module_builder.scaffold":
            name = params[0] if params else ""
            desc = params[1] if len(params) > 1 else ""
            tools_str = params[2] if len(params) > 2 else ""
            return _scaffold(name, desc, tools_str)
        elif tool_name == "module_builder.activate":
            return _activate(params[0] if params else "")
        elif tool_name == "module_builder.test":
            return _test(params[0] if params else "")
        elif tool_name == "module_builder.delete":
            return _delete(params[0] if params else "")
        return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}
    except Exception as e:
        return {"success": False, "data": f"Fehler: {e}"}


def _docs():
    lines = [
        "=== PYTHON MODUL FORMAT ===",
        "",
        "Jedes Modul ist ein Ordner in modules/ mit einer module.py Datei.",
        "",
        "PFLICHT-STRUKTUR:",
        "",
        "1. MODULE dict (global):",
        '   MODULE = {',
        '       "name": "mein_modul",',
        '       "description": "Was es tut",',
        '       "version": "1.0",',
        '       "settings": {',
        '           "key": {"type": "string|number|bool|list|password|select", "label": "Anzeigename", "default": "wert"},',
        '       },',
        '       "tools": [',
        '           {"name": "mein_modul.aktion", "description": "Was es tut", "params": ["param1"]},',
        '       ],',
        '   }',
        "",
        "2. handle_tool Funktion:",
        '   def handle_tool(tool_name: str, params: list, config: dict) -> dict:',
        '       """Return: {"success": True/False, "data": "Ergebnis-Text"}"""',
        "",
        "3. stdin/stdout Interface (am Ende der Datei):",
        '   if __name__ == "__main__":',
        '       for line in sys.stdin:',
        '           req = json.loads(line.strip())',
        '           if req.get("action") == "describe":',
        '               print(json.dumps(MODULE), flush=True)',
        '           elif req.get("action") == "handle_tool":',
        '               result = handle_tool(req["tool"], req.get("params", []), req.get("config", {}))',
        '               print(json.dumps(result), flush=True)',
        "",
        "REGELN:",
        "- Modulname = Ordnername = MODULE['name']",
        "- Tool-Namen: modulname.aktion (z.B. wetter.aktuell)",
        "- Nur stdlib verwenden oder pruefen ob Pakete installiert sind",
        "- handle_tool muss IMMER dict mit 'success' und 'data' zurueckgeben",
        "- Keine globalen Side-Effects beim Import",
        "- config enthaelt die Settings + home_dir des aufrufenden Moduls",
        "",
        "SETTINGS TYPEN:",
        "  string  → Textfeld",
        "  number  → Zahlenfeld",
        "  bool    → Checkbox",
        "  list    → Komma-getrennte Liste",
        "  password → Passwortfeld (versteckt)",
        "  select  → Dropdown (braucht 'options': ['a','b','c'])",
        "",
        "Nach dem Erstellen muss der Agent neugestartet werden damit das Modul geladen wird.",
    ]
    return {"success": True, "data": "\n".join(lines)}


def _list():
    lines = ["Vorhandene Module:\n"]
    for entry in sorted(os.listdir(MODULES_DIR)):
        mod_path = os.path.join(MODULES_DIR, entry, "module.py")
        if not os.path.isfile(mod_path):
            continue
        try:
            result = subprocess.run(
                ["python3", mod_path],
                input='{"action":"describe"}\n', capture_output=True, text=True, timeout=5
            )
            meta = json.loads(result.stdout.strip().split('\n')[0])
            tools = [t["name"] for t in meta.get("tools", [])]
            system = " [system]" if entry in SYSTEM_MODULES else ""
            lines.append(f"  {meta['name']:20s} v{meta.get('version','?'):5s} {len(tools)} tools{system}")
            lines.append(f"    {meta.get('description','')}")
            if tools:
                lines.append(f"    Tools: {', '.join(tools)}")
            lines.append("")
        except:
            lines.append(f"  {entry:20s} (Fehler beim Laden)")
            lines.append("")
    return {"success": True, "data": "\n".join(lines)}


def _inspect(name):
    if not name:
        return {"success": False, "data": "Modulname angeben"}
    mod_path = os.path.join(MODULES_DIR, name, "module.py")
    if not os.path.isfile(mod_path):
        return {"success": False, "data": f"Modul '{name}' nicht gefunden"}
    try:
        code = open(mod_path, 'r', encoding='utf-8').read()
        lines = code.split('\n')
        # Mit Zeilennummern
        numbered = [f"{i+1:>4}| {l}" for i, l in enumerate(lines)]
        return {"success": True, "data": f"=== {name}/module.py ({len(lines)} Zeilen) ===\n" + "\n".join(numbered)}
    except Exception as e:
        return {"success": False, "data": f"Fehler: {e}"}


def _scaffold(name, description, tools_str):
    """Erstellt ein Modul-Geruest. Die KI schreibt den eigentlichen Code danach mit editor.create."""
    if not name:
        return {"success": False, "data": "Modulname angeben"}
    if not name.replace('_', '').isalnum():
        return {"success": False, "data": f"Ungueltiger Modulname: {name}"}

    mod_dir = os.path.join(MODULES_DIR, name)
    mod_path = os.path.join(mod_dir, "module.py")

    if os.path.exists(mod_path):
        return {"success": False, "data": f"Modul '{name}' existiert bereits"}

    # Tools parsen
    tools = []
    if tools_str.strip():
        for t in tools_str.split(','):
            t = t.strip()
            if t:
                tools.append({"name": f"{name}.{t}", "description": f"TODO: Beschreibung fuer {t}", "params": []})

    # Template generieren
    tools_json = json.dumps(tools, indent=8)
    tool_cases = ""
    for t in tools:
        short = t["name"].split(".")[-1]
        tool_cases += f'    if tool_name == "{t["name"]}":\n        return {{"success": True, "data": "TODO: {short} implementieren"}}\n'

    code = f'''"""{description}"""
import json, sys

MODULE = {{
    "name": "{name}",
    "description": "{description}",
    "version": "1.0",
    "settings": {{}},
    "tools": {tools_json},
}}

def handle_tool(tool_name, params, config):
{tool_cases if tool_cases else '    pass'}
    return {{"success": False, "data": f"Unbekanntes Tool: {{tool_name}}"}}


if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            if req.get("action") == "describe":
                print(json.dumps(MODULE), flush=True)
            elif req.get("action") == "handle_tool":
                result = handle_tool(req["tool"], req.get("params", []), req.get("config", {{}}))
                print(json.dumps(result), flush=True)
            else:
                print(json.dumps({{"error": f"Unknown action: {{req.get(\'action\')}}"}}), flush=True)
        except Exception as e:
            print(json.dumps({{"error": str(e)}}), flush=True)
'''

    os.makedirs(mod_dir, exist_ok=True)
    with open(mod_path, 'w', encoding='utf-8') as f:
        f.write(code)

    # Testen
    test_result = _test(name)
    if not test_result["success"]:
        os.remove(mod_path)
        os.rmdir(mod_dir)
        return {"success": False, "data": f"Scaffold fehlgeschlagen:\n{test_result['data']}"}

    return {"success": True, "data": f"Modul '{name}' Geruest erstellt in modules/{name}/module.py\n"
        f"Tools: {', '.join(t['name'] for t in tools)}\n\n"
        f"NAECHSTE SCHRITTE:\n"
        f"1. Nutze editor.view(modules/{name}/module.py) um den Code zu sehen\n"
        f"2. Nutze editor.replace() um die TODO-Stellen mit echtem Code zu fuellen\n"
        f"3. Nutze module_builder.activate({name}) um zu testen und zu aktivieren\n"
        f"4. Agent muss neugestartet werden damit das Modul geladen wird"}


def _activate(name):
    """Testet ein bestehendes Modul und markiert es als aktiv."""
    if not name:
        return {"success": False, "data": "Modulname angeben"}
    mod_path = os.path.join(MODULES_DIR, name, "module.py")
    if not os.path.isfile(mod_path):
        return {"success": False, "data": f"Modul '{name}' nicht gefunden. Erstelle es zuerst mit module_builder.scaffold()"}

    test_result = _test(name)
    if not test_result["success"]:
        return {"success": False, "data": f"Modul nicht aktivierbar:\n{test_result['data']}"}

    return {"success": True, "data": f"Modul '{name}' ist bereit!\n{test_result['data']}\n\nAgent-Neustart noetig damit es geladen wird."}


def _create(name, code):
    if not name:
        return {"success": False, "data": "Modulname angeben"}
    if not code:
        return {"success": False, "data": "Code angeben (kompletter module.py Inhalt)"}

    # Name validieren
    if not name.replace('_', '').isalnum():
        return {"success": False, "data": f"Ungueltiger Modulname: {name} (nur Buchstaben, Zahlen, Unterstriche)"}

    mod_dir = os.path.join(MODULES_DIR, name)
    mod_path = os.path.join(mod_dir, "module.py")

    if os.path.exists(mod_path):
        return {"success": False, "data": f"Modul '{name}' existiert bereits. Nutze module_builder.delete zuerst."}

    # Schreiben
    os.makedirs(mod_dir, exist_ok=True)
    with open(mod_path, 'w', encoding='utf-8') as f:
        f.write(code)

    # Sofort testen
    test_result = _test(name)
    if not test_result["success"]:
        # Kaputtes Modul wieder loeschen
        os.remove(mod_path)
        os.rmdir(mod_dir)
        return {"success": False, "data": f"Modul erstellt aber Test fehlgeschlagen — wurde wieder geloescht.\n{test_result['data']}"}

    return {"success": True, "data": f"Modul '{name}' erstellt und getestet!\n{test_result['data']}\n\nAgent muss neugestartet werden damit das Modul geladen wird."}


def _test(name):
    if not name:
        return {"success": False, "data": "Modulname angeben"}
    mod_path = os.path.join(MODULES_DIR, name, "module.py")
    if not os.path.isfile(mod_path):
        return {"success": False, "data": f"Modul '{name}' nicht gefunden"}

    errors = []

    # Test 1: describe
    try:
        result = subprocess.run(
            ["python3", mod_path],
            input='{"action":"describe"}\n', capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            errors.append(f"Python Fehler: {result.stderr[:200]}")
        else:
            meta = json.loads(result.stdout.strip().split('\n')[0])
            if "name" not in meta:
                errors.append("MODULE hat kein 'name' Feld")
            if "tools" not in meta:
                errors.append("MODULE hat kein 'tools' Feld")
            else:
                tool_count = len(meta["tools"])
    except json.JSONDecodeError as e:
        errors.append(f"JSON Parse Fehler bei describe: {e}")
    except subprocess.TimeoutExpired:
        errors.append("Timeout bei describe (>5s)")
    except Exception as e:
        errors.append(f"Fehler bei describe: {e}")

    # Test 2: handle_tool mit erstem Tool
    if not errors and meta.get("tools"):
        first_tool = meta["tools"][0]["name"]
        try:
            test_input = json.dumps({"action": "handle_tool", "tool": first_tool, "params": [], "config": {}}) + "\n"
            result = subprocess.run(
                ["python3", mod_path],
                input=test_input, capture_output=True, text=True, timeout=10
            )
            resp = json.loads(result.stdout.strip().split('\n')[0])
            if "success" not in resp or "data" not in resp:
                errors.append(f"handle_tool Response fehlt 'success' oder 'data': {resp}")
        except Exception as e:
            errors.append(f"Fehler bei handle_tool Test: {e}")

    if errors:
        return {"success": False, "data": f"Test FEHLGESCHLAGEN:\n" + "\n".join(f"  - {e}" for e in errors)}

    return {"success": True, "data": f"Test OK: {meta['name']} v{meta.get('version','?')} — {tool_count} Tools, describe+handle_tool funktionieren"}


def _delete(name):
    if not name:
        return {"success": False, "data": "Modulname angeben"}
    if name in SYSTEM_MODULES:
        return {"success": False, "data": f"'{name}' ist ein System-Modul und kann nicht geloescht werden"}

    mod_dir = os.path.join(MODULES_DIR, name)
    if not os.path.isdir(mod_dir):
        return {"success": False, "data": f"Modul '{name}' nicht gefunden"}

    import shutil
    shutil.rmtree(mod_dir)
    return {"success": True, "data": f"Modul '{name}' geloescht. Agent-Neustart noetig."}


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
