"""TaskLoop — Steuert iterative Arbeit: Erstellt Sub-Tasks, validiert Ergebnisse, loopt bis Ziel erreicht."""
import json, sys, os, urllib.request, time

MODULE = {
    "name": "taskloop",
    "description": "Steuert iterative Arbeit: Ziel setzen, Sub-Tasks erstellen, validieren, wiederholen bis erfuellt",
    "version": "1.0",
    "settings": {
        "admin_port": {"type": "number", "label": "Admin Port", "default": 8090},
        "max_iterations": {"type": "number", "label": "Max Iterationen", "default": 10},
    },
    "tools": [
        {"name": "taskloop.start", "description": "Startet einen iterativen Work-Loop mit Ziel und Validierungskriterien. Gibt eine Loop-ID zurueck.", "params": ["ziel", "kriterien"]},
        {"name": "taskloop.check", "description": "Prueft ob ein Ziel erreicht wurde. Gibt Status und fehlende Kriterien zurueck.", "params": ["loop_id"]},
        {"name": "taskloop.progress", "description": "Zeigt den Fortschritt eines Loops: Iterationen, erfuellte/offene Kriterien, Score.", "params": ["loop_id"]},
        {"name": "taskloop.validate_file", "description": "Validiert eine Datei gegen Kriterien (Groesse, Woerter, Abschnitte). Gibt Score 1-10.", "params": ["pfad", "kriterien"]},
    ],
}

LOOPS_DIR = None

def _loops_dir(config):
    global LOOPS_DIR
    if LOOPS_DIR:
        return LOOPS_DIR
    home = config.get("home_dir", "")
    if home:
        d = os.path.join(os.path.abspath(home), ".taskloops")
    else:
        d = os.path.join("agent-data", ".taskloops")
    os.makedirs(d, exist_ok=True)
    LOOPS_DIR = d
    return d

def _api(config, path, method="GET", body=None):
    port = config.get("admin_port", 8090)
    url = f"http://127.0.0.1:{port}{path}"
    try:
        if body:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
        else:
            req = urllib.request.Request(url, method=method)
        resp = urllib.request.urlopen(req, timeout=5)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def handle_tool(tool_name, params, config):
    try:
        if tool_name == "taskloop.start":
            return _start(params, config)
        elif tool_name == "taskloop.check":
            return _check(params, config)
        elif tool_name == "taskloop.progress":
            return _progress(params, config)
        elif tool_name == "taskloop.validate_file":
            return _validate_file(params, config)
        return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}
    except Exception as e:
        return {"success": False, "data": f"TaskLoop Fehler: {e}"}


def _start(params, config):
    ziel = params[0] if params else ""
    kriterien_str = params[1] if len(params) > 1 else ""
    if not ziel:
        return {"success": False, "data": "Kein Ziel angegeben"}

    # Kriterien parsen (komma-getrennt oder zeilenweise)
    kriterien = []
    for k in kriterien_str.replace("\n", ",").split(","):
        k = k.strip()
        if k:
            kriterien.append({"text": k, "erfuellt": False, "score": 0})

    loop_id = f"loop_{int(time.time())}_{os.getpid()}"
    loop = {
        "id": loop_id,
        "ziel": ziel,
        "kriterien": kriterien,
        "iteration": 0,
        "max_iterations": config.get("max_iterations", 10),
        "status": "running",
        "history": [],
        "gesamtscore": 0,
    }

    path = os.path.join(_loops_dir(config), f"{loop_id}.json")
    with open(path, "w") as f:
        json.dump(loop, f, indent=2)

    kriterien_text = "\n".join(f"  [ ] {k['text']}" for k in kriterien) if kriterien else "  (keine spezifischen Kriterien)"

    return {"success": True, "data": f"Work-Loop gestartet: {loop_id}\n"
        f"Ziel: {ziel}\n"
        f"Kriterien:\n{kriterien_text}\n"
        f"Max Iterationen: {loop['max_iterations']}\n\n"
        f"NAECHSTER SCHRITT: Arbeite am Ziel und rufe danach taskloop.check({loop_id}) auf um zu pruefen ob die Kriterien erfuellt sind."}


def _check(params, config):
    loop_id = params[0] if params else ""
    loop = _load_loop(loop_id, config)
    if not loop:
        return {"success": False, "data": f"Loop '{loop_id}' nicht gefunden"}

    loop["iteration"] += 1

    if loop["iteration"] > loop["max_iterations"]:
        loop["status"] = "max_iterations"
        _save_loop(loop, config)
        return {"success": True, "data": f"Max Iterationen ({loop['max_iterations']}) erreicht. Loop beendet.\n"
            f"Gesamtscore: {loop['gesamtscore']}/10\n"
            f"Erfuellte Kriterien: {sum(1 for k in loop['kriterien'] if k['erfuellt'])}/{len(loop['kriterien'])}"}

    # Automatische Validierung: checke ob Dateien gewachsen sind etc.
    offene = [k for k in loop["kriterien"] if not k["erfuellt"]]
    erfuellte = [k for k in loop["kriterien"] if k["erfuellt"]]

    # Score berechnen
    if loop["kriterien"]:
        score = round(10 * len(erfuellte) / len(loop["kriterien"]), 1)
    else:
        score = 5  # Ohne Kriterien: neutral

    loop["gesamtscore"] = score
    loop["history"].append({
        "iteration": loop["iteration"],
        "score": score,
        "offene": len(offene),
        "erfuellte": len(erfuellte),
    })

    if not offene:
        loop["status"] = "completed"
        _save_loop(loop, config)
        emoji = "🏆" if score >= 9 else "✅" if score >= 7 else "👍"
        return {"success": True, "data": f"{emoji} ALLE KRITERIEN ERFUELLT! Score: {score}/10\n"
            f"Loop abgeschlossen nach {loop['iteration']} Iteration(en).\n"
            f"Gute Arbeit!"}

    _save_loop(loop, config)

    offene_text = "\n".join(f"  [ ] {k['text']}" for k in offene)
    erfuellte_text = "\n".join(f"  [x] {k['text']}" for k in erfuellte) if erfuellte else "  (noch keine)"

    return {"success": True, "data": f"Iteration {loop['iteration']}/{loop['max_iterations']} — Score: {score}/10\n\n"
        f"Erfuellt:\n{erfuellte_text}\n\n"
        f"NOCH OFFEN:\n{offene_text}\n\n"
        f"AKTION: Arbeite an den offenen Kriterien und rufe dann erneut taskloop.check({loop_id}) auf."}


def _progress(params, config):
    loop_id = params[0] if params else ""
    loop = _load_loop(loop_id, config)
    if not loop:
        return {"success": False, "data": f"Loop '{loop_id}' nicht gefunden"}

    lines = [
        f"Loop: {loop['id']}",
        f"Ziel: {loop['ziel']}",
        f"Status: {loop['status']}",
        f"Iteration: {loop['iteration']}/{loop['max_iterations']}",
        f"Score: {loop['gesamtscore']}/10",
        f"Kriterien: {sum(1 for k in loop['kriterien'] if k['erfuellt'])}/{len(loop['kriterien'])} erfuellt",
        "",
    ]
    for k in loop["kriterien"]:
        mark = "x" if k["erfuellt"] else " "
        lines.append(f"  [{mark}] {k['text']}")

    if loop["history"]:
        lines.append("")
        lines.append("Verlauf:")
        for h in loop["history"]:
            lines.append(f"  Iteration {h['iteration']}: Score {h['score']}/10, {h['erfuellte']} erfuellt, {h['offene']} offen")

    return {"success": True, "data": "\n".join(lines)}


def _validate_file(params, config):
    pfad = params[0] if params else ""
    kriterien_str = params[1] if len(params) > 1 else ""
    if not pfad:
        return {"success": False, "data": "Kein Pfad angegeben"}

    # Pfad aufloesen
    home = config.get("home_dir", "")
    if home and not os.path.isabs(pfad):
        pfad = os.path.join(os.path.abspath(home), pfad)
    pfad = os.path.abspath(pfad)

    if not os.path.exists(pfad):
        return {"success": False, "data": f"Datei nicht gefunden: {pfad}"}

    try:
        with open(pfad, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return {"success": False, "data": "Kann Datei nicht lesen"}

    size_kb = len(content.encode("utf-8")) / 1024
    woerter = len(content.split())
    zeilen = content.count("\n") + 1

    # Kriterien pruefen
    checks = []
    score = 5  # Basis

    # Groesse
    if "kb" in kriterien_str.lower():
        import re
        m = re.search(r'(\d+)\s*kb', kriterien_str.lower())
        if m:
            target_kb = int(m.group(1))
            if size_kb >= target_kb:
                checks.append(f"[OK] Groesse: {size_kb:.1f}KB >= {target_kb}KB")
                score += 2
            else:
                checks.append(f"[FAIL] Groesse: {size_kb:.1f}KB < {target_kb}KB (fehlen {target_kb - size_kb:.1f}KB)")
                score -= 1

    # Woerter
    if "wort" in kriterien_str.lower() or "woert" in kriterien_str.lower() or "word" in kriterien_str.lower():
        import re
        m = re.search(r'(\d+)\s*(?:wort|woert|word)', kriterien_str.lower())
        if m:
            target_words = int(m.group(1))
            if woerter >= target_words:
                checks.append(f"[OK] Woerter: {woerter} >= {target_words}")
                score += 2
            else:
                checks.append(f"[FAIL] Woerter: {woerter} < {target_words} (fehlen {target_words - woerter})")
                score -= 1

    # HTML Struktur
    if pfad.endswith(".html"):
        has_doctype = "<!DOCTYPE" in content or "<!doctype" in content
        has_head = "<head" in content
        has_body = "<body" in content
        has_closing = "</html>" in content
        html_ok = has_doctype and has_head and has_body and has_closing
        if html_ok:
            checks.append("[OK] HTML-Struktur: DOCTYPE, head, body, closing")
            score += 1
        else:
            missing = []
            if not has_doctype: missing.append("DOCTYPE")
            if not has_head: missing.append("<head>")
            if not has_body: missing.append("<body>")
            if not has_closing: missing.append("</html>")
            checks.append(f"[FAIL] HTML-Struktur: fehlt {', '.join(missing)}")
            score -= 1

    # Abschnitte zaehlen
    import re
    h2_count = len(re.findall(r'<h2', content, re.IGNORECASE))
    h3_count = len(re.findall(r'<h3', content, re.IGNORECASE))
    sections = h2_count + h3_count
    checks.append(f"[INFO] Abschnitte: {h2_count} H2 + {h3_count} H3 = {sections} total")
    if sections >= 5:
        score += 1

    # Links/Quellen
    links = len(re.findall(r'https?://', content))
    checks.append(f"[INFO] Links/Quellen: {links}")
    if links >= 10:
        score += 1

    score = max(1, min(10, score))

    result = [
        f"=== Datei-Validierung: {os.path.basename(pfad)} ===",
        f"Groesse: {size_kb:.1f}KB | Woerter: {woerter} | Zeilen: {zeilen}",
        f"Score: {score}/10",
        "",
    ] + checks

    # Auch Loop-Kriterien updaten wenn ein aktiver Loop existiert
    loops_dir = _loops_dir(config)
    for fname in os.listdir(loops_dir):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(loops_dir, fname)) as f:
                    loop = json.load(f)
                if loop.get("status") == "running":
                    for k in loop.get("kriterien", []):
                        kt = k["text"].lower()
                        if "kb" in kt and size_kb >= float(re.search(r'(\d+)', kt).group(1) if re.search(r'(\d+)', kt) else 0):
                            k["erfuellt"] = True
                            k["score"] = 10
                        elif "wort" in kt or "woert" in kt:
                            m = re.search(r'(\d+)', kt)
                            if m and woerter >= int(m.group(1)):
                                k["erfuellt"] = True
                                k["score"] = 10
                        elif "abschnitt" in kt or "section" in kt:
                            m = re.search(r'(\d+)', kt)
                            if m and sections >= int(m.group(1)):
                                k["erfuellt"] = True
                                k["score"] = 10
                        elif "quell" in kt or "link" in kt or "source" in kt:
                            m = re.search(r'(\d+)', kt)
                            if m and links >= int(m.group(1)):
                                k["erfuellt"] = True
                                k["score"] = 10
                    with open(os.path.join(loops_dir, fname), "w") as f:
                        json.dump(loop, f, indent=2)
            except Exception:
                pass

    return {"success": True, "data": "\n".join(result)}


def _load_loop(loop_id, config):
    path = os.path.join(_loops_dir(config), f"{loop_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)

def _save_loop(loop, config):
    path = os.path.join(_loops_dir(config), f"{loop['id']}.json")
    with open(path, "w") as f:
        json.dump(loop, f, indent=2)


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
