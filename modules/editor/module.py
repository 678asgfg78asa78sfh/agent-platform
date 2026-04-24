"""Smart Code/File Editor — Token-effizient: View per Zeilbereich, Replace per Search-and-Replace, automatische Backups."""
import json, sys, os, hashlib, shutil, glob as globmod
from datetime import datetime

MODULE = {
    "name": "editor",
    "description": "Token-effizienter Code-Editor mit Zeilenbereichen, Search-and-Replace, Backup und Undo",
    "version": "1.0",
    "settings": {
        "max_view_lines": {"type": "number", "label": "Max Zeilen pro View", "default": 80},
        "max_file_size_kb": {"type": "number", "label": "Max Dateigroesse KB", "default": 512},
        "max_backups": {"type": "number", "label": "Max Backups pro Datei", "default": 5},
        "blocked_extensions": {"type": "list", "label": "Blockierte Endungen", "default": [".exe",".bin",".so",".dll",".zip",".tar",".gz",".png",".jpg",".gif",".pdf",".mp3",".mp4"]},
    },
    "tools": [
        {"name": "editor.view", "description": "Zeigt Datei-Ausschnitt mit Zeilennummern. Format: pfad:start-ende oder nur pfad fuer erste 50 Zeilen", "params": ["pfad_und_bereich"]},
        {"name": "editor.replace", "description": "Ersetzt EXAKTEN Text. Param1=pfad, Param2=ALTER_TEXT dann ===REPLACE=== dann NEUER_TEXT", "params": ["pfad", "aenderung"]},
        {"name": "editor.insert", "description": "Fuegt Text NACH Zeile ein. Format: pfad:zeilennummer, neuer_text", "params": ["pfad_und_zeile", "neuer_text"]},
        {"name": "editor.create", "description": "Erstellt neue Datei", "params": ["pfad", "inhalt"]},
        {"name": "editor.delete_lines", "description": "Loescht Zeilen. Format: pfad:start-ende", "params": ["pfad_und_bereich"]},
        {"name": "editor.undo", "description": "Macht letzte Aenderung rueckgaengig", "params": ["pfad"]},
        {"name": "editor.info", "description": "Zeigt Datei-Info: Groesse, Zeilen, Encoding, Backups", "params": ["pfad"]},
    ],
}

BLOCKED_EXT = [".exe",".bin",".so",".dll",".zip",".tar",".gz",".png",".jpg",".gif",".pdf",".mp3",".mp4"]

# ═══ Path Security ═══════════════════════════════════

def _resolve_path(raw, config):
    """Pfad aufloesen und validieren. Returns (abs_path, error)."""
    p = raw.strip().strip('"').strip("'")
    if p.startswith("./") and len(p) > 2:
        rest = p[2:]
        if rest.startswith(("home","tmp","etc","var","usr")):
            p = "/" + rest

    # Home-Verzeichnis als Basis
    home = config.get("home_dir", "")
    if home:
        home = os.path.abspath(home)
    # Relativer Pfad → im Home joinen
    if not os.path.isabs(p) and home:
        p = os.path.join(home, p)
    # Absolut machen
    p = os.path.abspath(p)

    # Blocked extensions
    blocked = config.get("blocked_extensions", BLOCKED_EXT)
    ext = os.path.splitext(p)[1].lower()
    if ext in blocked:
        return None, f"Blockierte Dateiendung: {ext}"

    # Allowed paths check — alle absolut machen
    allowed = [os.path.abspath(a) for a in config.get("allowed_paths", [])]
    if home:
        allowed.append(home)

    if not allowed:
        return None, "Keine erlaubten Pfade konfiguriert"

    try:
        # Fuer existierende Dateien
        if os.path.exists(p):
            canonical = os.path.realpath(p)
        else:
            # Fuer neue Dateien: Parent pruefen
            parent = os.path.dirname(p)
            if parent and os.path.exists(parent):
                canonical = os.path.join(os.path.realpath(parent), os.path.basename(p))
            else:
                canonical = os.path.abspath(p)

        ok = False
        for a in allowed:
            try:
                a_real = os.path.realpath(a) if os.path.exists(a) else os.path.abspath(a)
                if canonical.startswith(a_real):
                    ok = True
                    break
            except Exception:
                continue
        if not ok:
            return None, f"Pfad nicht erlaubt: {p}"
        return canonical, None
    except Exception as e:
        return None, f"Pfad-Fehler: {e}"


def _parse_path_range(param):
    """Parst 'pfad:start-ende' oder 'pfad:zeile' oder 'pfad'. Returns (pfad, start, end)."""
    param = param.strip().strip('"').strip("'")
    # Letzten Doppelpunkt finden (nicht Teil von C:\)
    last_colon = param.rfind(':')
    if last_colon <= 0 or last_colon == len(param) - 1:
        return param, None, None

    path_part = param[:last_colon]
    range_part = param[last_colon+1:]

    try:
        if '-' in range_part:
            parts = range_part.split('-', 1)
            start = int(parts[0])
            end = int(parts[1])
            return path_part, start, end
        else:
            line = int(range_part)
            return path_part, line, line
    except ValueError:
        # Kein gueltiger Bereich → alles ist der Pfad
        return param, None, None


# ═══ Backup System ═══════════════════════════════════

def _backup_path(filepath, n):
    d = os.path.dirname(filepath)
    name = os.path.basename(filepath)
    return os.path.join(d, f".{name}.bak.{n}")

def _create_backup(filepath, max_backups=5):
    if not os.path.exists(filepath):
        return None
    # Naechste freie Nummer finden
    for i in range(1, max_backups + 10):
        bp = _backup_path(filepath, i)
        if not os.path.exists(bp):
            shutil.copy2(filepath, bp)
            # Alte Backups rotieren
            _rotate_backups(filepath, max_backups)
            return bp
    return None

def _rotate_backups(filepath, max_backups):
    d = os.path.dirname(filepath)
    name = os.path.basename(filepath)
    pattern = os.path.join(d, f".{name}.bak.*")
    backups = sorted(globmod.glob(pattern))
    while len(backups) > max_backups:
        os.remove(backups.pop(0))

def _latest_backup(filepath):
    d = os.path.dirname(filepath)
    name = os.path.basename(filepath)
    pattern = os.path.join(d, f".{name}.bak.*")
    backups = sorted(globmod.glob(pattern))
    return backups[-1] if backups else None

def _count_backups(filepath):
    d = os.path.dirname(filepath) or "."
    name = os.path.basename(filepath)
    pattern = os.path.join(d, f".{name}.bak.*")
    return len(globmod.glob(pattern))


# ═══ File I/O ════════════════════════════════════════

def _read_file(filepath, max_kb=512):
    size = os.path.getsize(filepath)
    if size > max_kb * 1024:
        return None, f"Datei zu gross: {size//1024}KB (max {max_kb}KB)"
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read(), None
    except UnicodeDecodeError:
        try:
            with open(filepath, 'r', encoding='latin-1') as f:
                return f.read(), None
        except Exception:
            return None, "Encoding-Fehler: Keine Textdatei"

def _write_file(filepath, content):
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)


# ═══ Tool Handlers ═══════════════════════════════════

def handle_tool(tool_name, params, config):
    try:
        if tool_name == "editor.view":
            return _view(params, config)
        elif tool_name == "editor.replace":
            return _replace(params, config)
        elif tool_name == "editor.insert":
            return _insert(params, config)
        elif tool_name == "editor.create":
            return _create(params, config)
        elif tool_name == "editor.delete_lines":
            return _delete_lines(params, config)
        elif tool_name == "editor.undo":
            return _undo(params, config)
        elif tool_name == "editor.info":
            return _info(params, config)
        return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}
    except Exception as e:
        return {"success": False, "data": f"Editor Fehler: {e}"}


def _view(params, config):
    raw = params[0] if params else ""
    path, start, end = _parse_path_range(raw)
    filepath, err = _resolve_path(path, config)
    if err: return {"success": False, "data": err}
    if not os.path.exists(filepath):
        return {"success": False, "data": f"Datei existiert nicht: {path}. Nutze editor.create zum Erstellen."}

    max_kb = config.get("max_file_size_kb", 512)
    content, err = _read_file(filepath, max_kb)
    if err: return {"success": False, "data": err}

    lines = content.splitlines()
    total = len(lines)
    max_view = config.get("max_view_lines", 80)

    if start is None:
        start, end = 1, min(total, max_view)
    else:
        start = max(1, start)
        end = min(total, end)
        if end - start + 1 > max_view:
            end = start + max_view - 1

    if start > total:
        return {"success": False, "data": f"Datei hat nur {total} Zeilen. Start={start} ist ungueltig."}

    # Zeilennummern-Breite
    width = len(str(end))
    output = [f"{os.path.basename(filepath)} (Zeilen {start}-{end} von {total})"]
    for i in range(start - 1, end):
        output.append(f"{i+1:>{width}}| {lines[i]}")

    return {"success": True, "data": "\n".join(output)}


def _replace(params, config):
    path = params[0] if params else ""
    change = params[1] if len(params) > 1 else ""
    filepath, err = _resolve_path(path, config)
    if err: return {"success": False, "data": err}
    if not os.path.exists(filepath):
        return {"success": False, "data": f"Datei existiert nicht: {path}"}

    # Delimiter parsen
    delimiter = "===REPLACE==="
    if delimiter not in change:
        return {"success": False, "data": f"Format-Fehler: Nutze ALTER_TEXT{delimiter}NEUER_TEXT"}

    parts = change.split(delimiter, 1)
    old_text = parts[0].rstrip('\n')
    new_text = parts[1].lstrip('\n')

    max_kb = config.get("max_file_size_kb", 512)
    content, err = _read_file(filepath, max_kb)
    if err: return {"success": False, "data": err}

    # Exakter Match
    count = content.count(old_text)
    if count == 0:
        # Whitespace-tolerant retry: trailing spaces ignorieren
        old_lines = [l.rstrip() for l in old_text.splitlines()]
        content_lines = [l.rstrip() for l in content.splitlines()]
        old_stripped = "\n".join(old_lines)
        content_stripped = "\n".join(content_lines)
        count = content_stripped.count(old_stripped)
        if count == 0:
            # Zeige Kontext um dem LLM zu helfen
            snippet = content[:200].replace('\n', '\\n')
            return {"success": False, "data": f"Text nicht gefunden. Nutze editor.view um den exakten Text zu sehen. Datei beginnt mit: {snippet}..."}
        elif count > 1:
            return {"success": False, "data": f"Text {count}x gefunden — mehrdeutig. Gib mehr Kontext-Zeilen an."}
        # Whitespace-tolerant replace
        content = content_stripped.replace(old_stripped, new_text, 1)
        # Restore original line endings
    elif count > 1:
        return {"success": False, "data": f"Text {count}x gefunden — mehrdeutig. Gib mehr Kontext-Zeilen an."}
    else:
        content = content.replace(old_text, new_text, 1)

    # Backup + Write
    max_backups = config.get("max_backups", 5)
    bak = _create_backup(filepath, max_backups)
    _write_file(filepath, content)

    old_count = len(old_text.splitlines())
    new_count = len(new_text.splitlines())
    delta = new_count - old_count
    delta_str = f" ({'+' if delta > 0 else ''}{delta} Zeilen)" if delta != 0 else ""
    bak_str = f" Backup: {os.path.basename(bak)}" if bak else ""

    return {"success": True, "data": f"Ersetzt in {os.path.basename(filepath)}: {old_count} → {new_count} Zeilen{delta_str}.{bak_str}"}


def _insert(params, config):
    raw = params[0] if params else ""
    new_text = params[1] if len(params) > 1 else ""
    path, line_num, _ = _parse_path_range(raw)
    filepath, err = _resolve_path(path, config)
    if err: return {"success": False, "data": err}
    if not os.path.exists(filepath):
        return {"success": False, "data": f"Datei existiert nicht: {path}"}
    if line_num is None:
        return {"success": False, "data": "Zeilennummer fehlt. Format: pfad:zeile"}

    max_kb = config.get("max_file_size_kb", 512)
    content, err = _read_file(filepath, max_kb)
    if err: return {"success": False, "data": err}

    lines = content.splitlines(True)  # keepends
    total = len(lines)

    if line_num < 0 or line_num > total:
        return {"success": False, "data": f"Zeile {line_num} ungueltig. Datei hat {total} Zeilen (0=Anfang)."}

    # Neue Zeilen einfuegen
    new_lines = new_text.splitlines(True)
    if new_lines and not new_lines[-1].endswith('\n'):
        new_lines[-1] += '\n'

    lines[line_num:line_num] = new_lines

    max_backups = config.get("max_backups", 5)
    bak = _create_backup(filepath, max_backups)
    _write_file(filepath, "".join(lines))

    bak_str = f" Backup: {os.path.basename(bak)}" if bak else ""
    return {"success": True, "data": f"{len(new_lines)} Zeile(n) eingefuegt nach Zeile {line_num}.{bak_str}"}


def _create(params, config):
    path = params[0] if params else ""
    content = params[1] if len(params) > 1 else ""
    filepath, err = _resolve_path(path, config)
    if err: return {"success": False, "data": err}
    if os.path.exists(filepath):
        return {"success": False, "data": f"Datei existiert bereits: {path}. Nutze editor.replace zum Bearbeiten."}

    _write_file(filepath, content)
    lines = content.count('\n') + (1 if content and not content.endswith('\n') else 0)
    return {"success": True, "data": f"Datei erstellt: {os.path.basename(filepath)} ({lines} Zeilen, {len(content.encode('utf-8'))} bytes)"}


def _delete_lines(params, config):
    raw = params[0] if params else ""
    path, start, end = _parse_path_range(raw)
    filepath, err = _resolve_path(path, config)
    if err: return {"success": False, "data": err}
    if not os.path.exists(filepath):
        return {"success": False, "data": f"Datei existiert nicht: {path}"}
    if start is None or end is None:
        return {"success": False, "data": "Bereich fehlt. Format: pfad:start-ende"}

    max_kb = config.get("max_file_size_kb", 512)
    content, err = _read_file(filepath, max_kb)
    if err: return {"success": False, "data": err}

    lines = content.splitlines(True)
    total = len(lines)

    if start < 1 or end > total or start > end:
        return {"success": False, "data": f"Bereich {start}-{end} ungueltig. Datei hat {total} Zeilen."}

    max_backups = config.get("max_backups", 5)
    bak = _create_backup(filepath, max_backups)

    del lines[start-1:end]
    _write_file(filepath, "".join(lines))

    count = end - start + 1
    bak_str = f" Backup: {os.path.basename(bak)}" if bak else ""
    return {"success": True, "data": f"{count} Zeile(n) geloescht ({start}-{end}).{bak_str}"}


def _undo(params, config):
    path = params[0] if params else ""
    filepath, err = _resolve_path(path, config)
    if err: return {"success": False, "data": err}

    bak = _latest_backup(filepath)
    if not bak:
        return {"success": False, "data": f"Kein Backup vorhanden fuer {path}"}

    shutil.copy2(bak, filepath)
    os.remove(bak)
    return {"success": True, "data": f"Undo: {os.path.basename(filepath)} wiederhergestellt von {os.path.basename(bak)}"}


def _info(params, config):
    path = params[0] if params else ""
    filepath, err = _resolve_path(path, config)
    if err: return {"success": False, "data": err}
    if not os.path.exists(filepath):
        return {"success": False, "data": f"Datei existiert nicht: {path}"}

    stat = os.stat(filepath)
    size = stat.st_size
    mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    backups = _count_backups(filepath)

    # Zeilen zaehlen + Encoding pruefen
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = sum(1 for _ in f)
            enc = "UTF-8"
    except UnicodeDecodeError:
        try:
            with open(filepath, 'r', encoding='latin-1') as f:
                lines = sum(1 for _ in f)
                enc = "Latin-1"
        except Exception:
            lines = "?"
            enc = "unbekannt"

    size_str = f"{size}B" if size < 1024 else f"{size/1024:.1f}KB"
    return {"success": True, "data": f"{os.path.basename(filepath)}: {lines} Zeilen, {size_str}, {enc}, geaendert: {mtime}, {backups} Backup(s)"}


# ═══ stdin/stdout Interface ══════════════════════════

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
