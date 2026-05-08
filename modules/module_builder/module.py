"""Module Builder — Ermoeglicht der KI neue Python-Module zu erstellen, testen und deployen."""
import json, sys, os, subprocess, re, difflib, shutil, uuid
from datetime import datetime, timezone

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
        {"name": "module_builder.scaffold", "description": "Erstellt ein neues Modul-Geruest. Nicht fuer existierende Module nutzen; dort editor.* oder Draft-Workflow verwenden.", "params": ["name", "description", "tools_komma_getrennt"]},
        {"name": "module_builder.write", "description": "Schreibt kleine/neue modules/name/module.py komplett. Bei grossen existierenden Modulen wird automatisch ein Draft angelegt statt aktiv zu ueberschreiben.", "params": ["name", "code"]},
        {"name": "module_builder.draft_write", "description": "Speichert grossen Modulcode als Draft unter modules/name/.drafts, testet ihn und laesst das aktive module.py unveraendert.", "params": ["name", "code"]},
        {"name": "module_builder.draft_list", "description": "Listet gespeicherte Drafts eines Moduls", "params": ["name"]},
        {"name": "module_builder.draft_test", "description": "Testet einen gespeicherten Draft ohne das aktive Modul zu veraendern", "params": ["name", "draft_id"]},
        {"name": "module_builder.draft_diff", "description": "Zeigt einen kompakten Diff zwischen aktivem module.py und einem Draft", "params": ["name", "draft_id"]},
        {"name": "module_builder.draft_promote", "description": "Promoted einen getesteten Draft nach module.py, legt Backup an und testet danach erneut", "params": ["name", "draft_id"]},
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
MODULE_NAME_RE = re.compile(r"^[a-z0-9_]+$")
TOOL_ACTION_RE = re.compile(r"^[a-z0-9_]+$")
TOOL_REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
DRAFT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
LARGE_EXISTING_WRITE_CHARS = 12000
MAX_DIFF_CHARS = 20000


def _canonical_name(name):
    return (name or "").strip().lower()


def _validate_module_name(name):
    if not name:
        return "Modulname angeben"
    if not MODULE_NAME_RE.match(name):
        return f"Ungueltiger Modulname: {name} (nur lowercase a-z, Zahlen, Unterstrich)"
    return None


def _existing_module_entry(name):
    """Case-insensitive lookup. Prefer the canonical lowercase directory."""
    canonical = _canonical_name(name)
    if not os.path.isdir(MODULES_DIR):
        return None
    exact = os.path.join(MODULES_DIR, canonical, "module.py")
    if os.path.isfile(exact):
        return canonical
    for entry in sorted(os.listdir(MODULES_DIR)):
        if entry.lower() == canonical and os.path.isfile(os.path.join(MODULES_DIR, entry, "module.py")):
            return entry
    return None


def _looks_like_tool_ref(value):
    return bool(TOOL_REF_RE.match((value or "").strip()))


def _split_scaffold_params(params):
    """Recover name/description/tools when an LLM split a comma-heavy description."""
    if not params:
        return "", ""
    if len(params) == 1:
        return params[0], ""
    if len(params) == 2:
        return params[0], params[1]

    rest = [p.strip() for p in params if p and p.strip()]
    tool_parts = []
    while rest and _looks_like_tool_ref(rest[-1]):
        tool_parts.insert(0, rest.pop())

    if not tool_parts:
        return ", ".join(params[:-1]).strip(), params[-1].strip()
    return ", ".join(rest).strip(), ", ".join(tool_parts)


def _normalise_tool_action(raw):
    action = (raw or "").strip()
    if not action:
        return None
    # LLMs often pass dependency refs like tavily.search. The new module tool
    # action is the last segment; dependencies are configured on the agent.
    if "." in action:
        action = action.split(".")[-1]
    action = action.lower()
    if not TOOL_ACTION_RE.match(action):
        return None
    return action


def _strip_code_wrapper(raw):
    code = (raw or "").strip()
    for sep in (":", "="):
        if sep in code:
            key, value = code.split(sep, 1)
            if key.strip().lower() in {"code", "inhalt", "content"}:
                code = value.strip()
                break
    for quote in ('"""', "'''"):
        if code.startswith(quote) and code.endswith(quote) and len(code) >= 6:
            return code[3:-3].lstrip("\n")
    if (code.startswith('"') and code.endswith('"')) or (code.startswith("'") and code.endswith("'")):
        return code[1:-1]
    return code


def _utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _atomic_write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _module_dir(name):
    return os.path.join(MODULES_DIR, name)


def _drafts_dir(name):
    return os.path.join(_module_dir(name), ".drafts")


def _new_draft_id():
    return f"draft_{_utc_stamp()}_{uuid.uuid4().hex[:8]}.py"


def _normalise_draft_id(draft_id):
    draft_id = (draft_id or "").strip()
    if draft_id.startswith("draft_id:"):
        draft_id = draft_id.split(":", 1)[1].strip()
    draft_id = os.path.basename(draft_id)
    if not draft_id.endswith(".py"):
        draft_id = draft_id + ".py"
    if not DRAFT_ID_RE.match(draft_id):
        return None
    return draft_id


def _draft_path(name, draft_id):
    draft_id = _normalise_draft_id(draft_id)
    if not draft_id:
        return None
    return os.path.join(_drafts_dir(name), draft_id)


def _existing_module_path(name):
    entry = _existing_module_entry(name)
    if not entry:
        return None
    mod_path = os.path.join(MODULES_DIR, entry, "module.py")
    return mod_path if os.path.isfile(mod_path) else None


def _read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


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
            desc, tools_str = _split_scaffold_params(params[1:])
            return _scaffold(name, desc, tools_str)
        elif tool_name == "module_builder.write":
            name = params[0] if params else ""
            code = params[1] if len(params) > 1 else ""
            return _write_module(name, code)
        elif tool_name == "module_builder.draft_write":
            name = params[0] if params else ""
            code = params[1] if len(params) > 1 else ""
            return _draft_write(name, code)
        elif tool_name == "module_builder.draft_list":
            return _draft_list(params[0] if params else "")
        elif tool_name == "module_builder.draft_test":
            name = params[0] if params else ""
            draft_id = params[1] if len(params) > 1 else ""
            return _draft_test(name, draft_id)
        elif tool_name == "module_builder.draft_diff":
            name = params[0] if params else ""
            draft_id = params[1] if len(params) > 1 else ""
            return _draft_diff(name, draft_id)
        elif tool_name == "module_builder.draft_promote":
            name = params[0] if params else ""
            draft_id = params[1] if len(params) > 1 else ""
            return _draft_promote(name, draft_id)
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
        "- handle_tool bekommt params IMMER als Liste, nicht als Dict",
        "- Python-Module bekommen NICHT automatisch Python-Funktionen fuer andere Tools wie tavily.search oder rag.speichern",
        "- Wenn ein Modul andere Plattform-Tools braucht, muss dafuer ein expliziter Tool-Bus/API-Zugriff implementiert werden",
        "- Nur stdlib verwenden oder pruefen ob Pakete installiert sind",
        "- handle_tool muss IMMER dict mit 'success' und 'data' zurueckgeben",
        "- Keine globalen Side-Effects beim Import",
        "- config enthaelt die Settings + home_dir des aufrufenden Moduls",
        "",
        "CODING-WORKFLOW FUER BESTEHENDE/GROSSE MODULE:",
        "- Erst module_builder.inspect(name) oder editor.view(...) nutzen",
        "- Grosse Rewrite-Ideen NICHT direkt in module_builder.write pressen",
        "- Nutze module_builder.draft_write(name, code), um grossen Code zwischenzulagern",
        "- Nutze module_builder.draft_test(name, draft_id), um Fehler mit Zeilen/Stderr zu sehen",
        "- Repariere Drafts mit editor.view/editor.replace in modules/name/.drafts/<draft_id>",
        "- Erst wenn der Draft testet: module_builder.draft_promote(name, draft_id)",
        "- Fuer kleine Aenderungen an bestehendem Code: editor.replace/editor.insert + module_builder.test(name)",
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
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}
    entry = _existing_module_entry(name)
    if not entry:
        return {"success": False, "data": f"Modul '{name}' nicht gefunden"}
    mod_path = os.path.join(MODULES_DIR, entry, "module.py")
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
    requested_name = name
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}

    mod_dir = os.path.join(MODULES_DIR, name)
    mod_path = os.path.join(mod_dir, "module.py")

    existing = _existing_module_entry(name)
    if existing:
        return {"success": False, "data": f"Modul '{name}' existiert bereits in modules/{existing}/module.py.\n"
            f"Hinweis: Modulnamen sind case-insensitive und werden lowercase gespeichert.\n"
            f"NAECHSTE SCHRITTE:\n"
            f"1. Nutze editor.view(modules/{existing}/module.py) oder module_builder.inspect({name}) um den bestehenden Code zu sehen\n"
            f"2. Fuer kleine Aenderungen nutze editor.replace/editor.insert\n"
            f"3. Fuer grosse Rewrites nutze module_builder.draft_write({name}, CODE), dann draft_test und draft_promote\n"
            f"4. Nutze module_builder.test({name}) und danach module_builder.activate({name})"}

    # Tools parsen
    tools = []
    invalid_tools = []
    if tools_str.strip():
        for t in tools_str.split(','):
            action = _normalise_tool_action(t)
            if action:
                tools.append({"name": f"{name}.{action}", "description": f"TODO: Beschreibung fuer {action}", "params": []})
            elif t.strip():
                invalid_tools.append(t.strip())
    if invalid_tools:
        return {"success": False, "data": "Ungueltige Toolnamen: " + ", ".join(invalid_tools) +
            ". tools_komma_getrennt erwartet Actions wie search, ingest, synthesize; keine Beschreibungstexte."}

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

    normalised_note = ""
    if requested_name and requested_name != name:
        normalised_note = f"Name wurde zu lowercase normalisiert: {requested_name} -> {name}\n"
    return {"success": True, "data": normalised_note + f"Modul '{name}' Geruest erstellt in modules/{name}/module.py\n"
        f"Tools: {', '.join(t['name'] for t in tools)}\n\n"
        f"NAECHSTE SCHRITTE:\n"
        f"1. Kleine Module: module_builder.write({name}, KOMPLETTER_CODE)\n"
        f"2. Grosse Module/Rewrites: module_builder.draft_write({name}, CODE), dann draft_test und draft_promote\n"
        f"3. Alternativ kleine Patches: editor.replace/editor.insert\n"
        f"4. Nutze module_builder.activate({name}) um zu testen und zu aktivieren\n"
        f"5. Agent muss neugestartet werden damit das Modul geladen wird"}


def _draft_write(name, code, reason=None):
    """Speichert grossen Code als Draft und testet ihn ohne aktive Datei zu aendern."""
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}
    code = _strip_code_wrapper(code)
    if not code.strip():
        return {"success": False, "data": "Code angeben (kompletter Draft-Inhalt)"}

    os.makedirs(_drafts_dir(name), exist_ok=True)
    draft_id = _new_draft_id()
    path = _draft_path(name, draft_id)
    _atomic_write_text(path, code)

    test_result = _test_file(name, path)
    test_label = "DRAFT_TEST_OK" if test_result["success"] else "DRAFT_TEST_FAILED"
    reason_line = f"{reason}\n" if reason else ""
    next_steps = (
        f"NAECHSTE SCHRITTE:\n"
        f"1. Bei Fehlern: editor.view(modules/{name}/.drafts/{draft_id}:<zeilen>)\n"
        f"2. Reparieren: editor.replace/editor.insert auf modules/{name}/.drafts/{draft_id}\n"
        f"3. Erneut testen: module_builder.draft_test({name}, {draft_id})\n"
        f"4. Wenn OK: module_builder.draft_promote({name}, {draft_id})"
    )
    return {
        "success": True,
        "data": (
            f"{reason_line}Draft gespeichert.\n"
            f"module: {name}\n"
            f"draft_id: {draft_id}\n"
            f"path: modules/{name}/.drafts/{draft_id}\n"
            f"chars: {len(code)}\n"
            f"{test_label}:\n{test_result['data']}\n\n"
            f"{next_steps}"
        ),
    }


def _draft_list(name):
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}
    ddir = _drafts_dir(name)
    if not os.path.isdir(ddir):
        return {"success": True, "data": f"Keine Drafts fuer Modul '{name}'."}
    files = []
    for entry in sorted(os.listdir(ddir)):
        path = os.path.join(ddir, entry)
        if not os.path.isfile(path) or not entry.endswith(".py"):
            continue
        st = os.stat(path)
        ts = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        first = ""
        try:
            first = _read_text(path).splitlines()[0][:120]
        except Exception:
            pass
        files.append((st.st_mtime, f"{entry} | {st.st_size} bytes | {ts} | {first}"))
    if not files:
        return {"success": True, "data": f"Keine Drafts fuer Modul '{name}'."}
    files.sort(reverse=True)
    return {"success": True, "data": f"Drafts fuer {name}:\n" + "\n".join(line for _, line in files)}


def _draft_test(name, draft_id):
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}
    path = _draft_path(name, draft_id)
    if not path or not os.path.isfile(path):
        return {"success": False, "data": f"Draft '{draft_id}' fuer Modul '{name}' nicht gefunden"}
    test_result = _test_file(name, path)
    label = "DRAFT_TEST_OK" if test_result["success"] else "DRAFT_TEST_FAILED"
    return {"success": True, "data": f"{label}: {name}/{os.path.basename(path)}\n{test_result['data']}"}


def _draft_diff(name, draft_id):
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}
    draft = _draft_path(name, draft_id)
    if not draft or not os.path.isfile(draft):
        return {"success": False, "data": f"Draft '{draft_id}' fuer Modul '{name}' nicht gefunden"}
    active = _existing_module_path(name)
    active_lines = _read_text(active).splitlines(keepends=True) if active else []
    draft_lines = _read_text(draft).splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(
        active_lines,
        draft_lines,
        fromfile=f"modules/{name}/module.py",
        tofile=f"modules/{name}/.drafts/{os.path.basename(draft)}",
        n=3,
    ))
    if not diff:
        diff = "(kein Unterschied)"
    truncated = ""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS].rstrip()
        truncated = "\n...[diff gekuerzt; nutze editor.view fuer gezielte Bereiche]"
    return {"success": True, "data": f"Diff fuer {name}/{os.path.basename(draft)}:\n{diff}{truncated}"}


def _draft_promote(name, draft_id):
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}
    draft = _draft_path(name, draft_id)
    if not draft or not os.path.isfile(draft):
        return {"success": False, "data": f"Draft '{draft_id}' fuer Modul '{name}' nicht gefunden"}

    preflight = _test_file(name, draft)
    if not preflight["success"]:
        return {"success": False, "data": f"Draft nicht promotbar, Test fehlgeschlagen:\n{preflight['data']}\n\nAktives module.py bleibt unveraendert."}

    mod_dir = _module_dir(name)
    mod_path = os.path.join(mod_dir, "module.py")
    os.makedirs(mod_dir, exist_ok=True)
    backup = None
    if os.path.isfile(mod_path):
        backup = os.path.join(mod_dir, f".module.py.bak.{_utc_stamp()}")
        shutil.copy2(mod_path, backup)

    _atomic_write_text(mod_path, _read_text(draft))
    active_test = _test(name)
    if not active_test["success"]:
        if backup and os.path.isfile(backup):
            shutil.copy2(backup, mod_path)
            restored = f"Backup wiederhergestellt: {os.path.basename(backup)}"
        else:
            try:
                os.remove(mod_path)
            except OSError:
                pass
            restored = "Kaputte neue module.py entfernt."
        return {"success": False, "data": f"Promote hat aktiven Test nicht bestanden. {restored}\n{active_test['data']}"}

    backup_line = f"\nBackup: {os.path.basename(backup)}" if backup else ""
    return {"success": True, "data": f"Draft promoted nach modules/{name}/module.py.{backup_line}\n{active_test['data']}\n\nAgent-Neustart noetig damit neue Tools geladen werden."}


def _write_module(name, code):
    """Schreibt module.py komplett und testet. Bei Testfehlern wird restored."""
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}
    code = _strip_code_wrapper(code)
    if not code.strip():
        return {"success": False, "data": "Code angeben (kompletter module.py Inhalt)"}

    mod_dir = os.path.join(MODULES_DIR, name)
    mod_path = os.path.join(mod_dir, "module.py")
    os.makedirs(mod_dir, exist_ok=True)

    if os.path.exists(mod_path) and len(code) > LARGE_EXISTING_WRITE_CHARS:
        return _draft_write(
            name,
            code,
            reason=(
                "DRAFT_STAGED_POLICY: module_builder.write wurde fuer ein grosses "
                f"existierendes Modul ({len(code)} chars) abgefangen. Aktives module.py "
                "wurde nicht ueberschrieben."
            ),
        )

    old_code = None
    if os.path.exists(mod_path):
        with open(mod_path, "r", encoding="utf-8") as f:
            old_code = f.read()

    with open(mod_path, "w", encoding="utf-8") as f:
        f.write(code)

    test_result = _test(name)
    if not test_result["success"]:
        if old_code is not None:
            with open(mod_path, "w", encoding="utf-8") as f:
                f.write(old_code)
            restored = "Alter Code wurde wiederhergestellt."
        else:
            try:
                os.remove(mod_path)
                os.rmdir(mod_dir)
            except OSError:
                pass
            restored = "Kaputte neue Datei wurde entfernt."
        return {"success": False, "data": f"Modul-Code geschrieben, aber Test fehlgeschlagen. {restored}\n{test_result['data']}"}

    action = "ueberschrieben" if old_code is not None else "erstellt"
    return {"success": True, "data": f"Modul '{name}' {action} und getestet.\n{test_result['data']}\n\nAgent-Neustart noetig damit es geladen wird."}


def _activate(name):
    """Testet ein bestehendes Modul und markiert es als aktiv."""
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}
    entry = _existing_module_entry(name)
    if not entry:
        return {"success": False, "data": f"Modul '{name}' nicht gefunden. Erstelle es zuerst mit module_builder.scaffold()"}
    mod_path = os.path.join(MODULES_DIR, entry, "module.py")
    if not os.path.isfile(mod_path):
        return {"success": False, "data": f"Modul '{name}' nicht gefunden. Erstelle es zuerst mit module_builder.scaffold()"}

    test_result = _test(name)
    if not test_result["success"]:
        return {"success": False, "data": f"Modul nicht aktivierbar:\n{test_result['data']}"}

    return {"success": True, "data": f"Modul '{name}' ist bereit!\n{test_result['data']}\n\nAgent-Neustart noetig damit es geladen wird."}


def _create(name, code):
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}
    if not code:
        return {"success": False, "data": "Code angeben (kompletter module.py Inhalt)"}

    mod_dir = os.path.join(MODULES_DIR, name)
    mod_path = os.path.join(mod_dir, "module.py")

    if _existing_module_entry(name):
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
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}
    mod_path = _existing_module_path(name)
    if not mod_path:
        return {"success": False, "data": f"Modul '{name}' nicht gefunden"}
    return _test_file(name, mod_path)


def _test_file(name, mod_path):
    """Testet eine konkrete module.py/Draft-Datei ohne Pfad-Lookup."""
    name = _canonical_name(name)
    errors = []
    meta = None
    tool_count = 0

    # Test 1: describe
    try:
        result = subprocess.run(
            ["python3", mod_path],
            input='{"action":"describe"}\n', capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()[:800]
            stdout = (result.stdout or "").strip()[:300]
            errors.append(f"Python Fehler (returncode {result.returncode}): stderr={stderr!r} stdout={stdout!r}")
        else:
            stdout = (result.stdout or "").strip()
            if not stdout:
                errors.append("Leerer stdout bei describe")
            else:
                meta = json.loads(stdout.split('\n')[0])
            if meta is not None:
                if "name" not in meta:
                    errors.append("MODULE hat kein 'name' Feld")
                elif meta["name"] != name:
                    errors.append(f"MODULE['name'] muss '{name}' sein, ist aber '{meta['name']}'")
                if "tools" not in meta:
                    errors.append("MODULE hat kein 'tools' Feld")
                else:
                    tool_count = len(meta["tools"])
                    for tool in meta["tools"]:
                        tool_name = tool.get("name", "")
                        prefix = f"{name}."
                        if not tool_name.startswith(prefix):
                            errors.append(f"Tool '{tool_name}' muss mit '{prefix}' beginnen")
                            continue
                        action = tool_name[len(prefix):]
                        if not TOOL_ACTION_RE.match(action):
                            errors.append(f"Tool '{tool_name}' hat ungueltige Action '{action}'")
    except json.JSONDecodeError as e:
        errors.append(f"JSON Parse Fehler bei describe: {e}")
    except subprocess.TimeoutExpired:
        errors.append("Timeout bei describe (>5s)")
    except Exception as e:
        errors.append(f"Fehler bei describe: {e}")

    # Test 2: handle_tool mit erstem Tool
    if not errors and meta and meta.get("tools"):
        first_tool = meta["tools"][0]["name"]
        try:
            test_input = json.dumps({"action": "handle_tool", "tool": first_tool, "params": [], "config": {}}) + "\n"
            result = subprocess.run(
                ["python3", mod_path],
                input=test_input, capture_output=True, text=True, timeout=10
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            if result.returncode != 0:
                errors.append(f"Python Fehler bei handle_tool (returncode {result.returncode}): stderr={stderr[:800]!r} stdout={stdout[:300]!r}")
                resp = None
            elif not stdout:
                errors.append(f"Leerer stdout bei handle_tool Test fuer {first_tool}. stderr={stderr[:800]!r}")
                resp = None
            else:
                resp = json.loads(stdout.split('\n')[0])
            if resp is None:
                pass
            elif "success" not in resp or "data" not in resp:
                errors.append(f"handle_tool Response fehlt 'success' oder 'data': {resp}")
        except json.JSONDecodeError as e:
            stdout = (result.stdout or "").strip()[:800] if "result" in locals() else ""
            stderr = (result.stderr or "").strip()[:800] if "result" in locals() else ""
            errors.append(f"JSON Parse Fehler bei handle_tool Test fuer {first_tool}: {e}; stdout={stdout!r}; stderr={stderr!r}")
        except Exception as e:
            errors.append(f"Fehler bei handle_tool Test: {e}")

    if errors:
        return {"success": False, "data": f"Test FEHLGESCHLAGEN:\n" + "\n".join(f"  - {e}" for e in errors)}

    return {"success": True, "data": f"Test OK: {meta['name']} v{meta.get('version','?')} — {tool_count} Tools, describe+handle_tool funktionieren"}


def _delete(name):
    name = _canonical_name(name)
    err = _validate_module_name(name)
    if err:
        return {"success": False, "data": err}
    if name in SYSTEM_MODULES:
        return {"success": False, "data": f"'{name}' ist ein System-Modul und kann nicht geloescht werden"}

    entry = _existing_module_entry(name)
    if not entry:
        draft_only_dir = os.path.join(MODULES_DIR, name)
        if os.path.isdir(draft_only_dir):
            entries = [e for e in os.listdir(draft_only_dir) if e not in {".", ".."}]
            if not entries or entries == [".drafts"]:
                shutil.rmtree(draft_only_dir)
                return {"success": True, "data": f"Draft-only Modulordner '{name}' geloescht."}
        return {"success": False, "data": f"Modul '{name}' nicht gefunden"}
    mod_dir = os.path.join(MODULES_DIR, entry)
    if not os.path.isdir(mod_dir):
        return {"success": False, "data": f"Modul '{name}' nicht gefunden"}

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
