"""Mailstore Modul — SQLite-Datenbank fuer E-Mails. Token-effizient: KI sucht per SQL."""
import json, sys, os, sqlite3

MODULE = {
    "name": "mailstore",
    "description": "Mail-Datenbank: Suchen, Lesen, Auflisten von gespeicherten E-Mails",
    "version": "1.0",
    "settings": {
        "db_path": {"type": "string", "label": "DB Pfad", "default": "agent-data/mail.db"},
    },
    "tools": [
        {"name": "mail.search", "description": "Sucht Mails (FROM:, SUBJECT:, BODY: Prefix oder Volltext). Gibt max 20 Ergebnisse als Tabelle.", "params": ["query"]},
        {"name": "mail.read", "description": "Liest eine Mail vollstaendig nach ID", "params": ["id"]},
        {"name": "mail.list", "description": "Listet Mails in einem Ordner (Standard: INBOX)", "params": ["folder", "limit"]},
        {"name": "mail.stats", "description": "Zeigt Mail-Statistiken (Anzahl, ungelesen pro Quelle)", "params": []},
    ],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS mails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE,
    from_addr TEXT NOT NULL DEFAULT '',
    to_addr TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT 'INBOX',
    read INTEGER NOT NULL DEFAULT 0,
    flags TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    raw_headers TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mails_from ON mails(from_addr);
CREATE INDEX IF NOT EXISTS idx_mails_subject ON mails(subject);
CREATE INDEX IF NOT EXISTS idx_mails_date ON mails(date);
CREATE INDEX IF NOT EXISTS idx_mails_folder ON mails(folder);
CREATE INDEX IF NOT EXISTS idx_mails_message_id ON mails(message_id);
"""

def _db(config):
    db_path = config.get("db_path", "agent-data/mail.db")
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn

def handle_tool(tool_name, params, config):
    if tool_name == "mail.search":
        return _search(config, params[0] if params else "")
    elif tool_name == "mail.read":
        return _read(config, params[0] if params else "")
    elif tool_name == "mail.list":
        folder = params[0] if params else "INBOX"
        limit = int(params[1]) if len(params) > 1 and params[1] else 20
        return _list(config, folder, limit)
    elif tool_name == "mail.stats":
        return _stats(config)
    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}

def _search(config, query):
    if not query.strip():
        return {"success": False, "data": "Keine Suchanfrage"}
    conn = _db(config)
    try:
        q = query.strip()
        if q.upper().startswith("FROM:"):
            where = "from_addr LIKE ?"
            param = f"%{q[5:].strip()}%"
        elif q.upper().startswith("SUBJECT:"):
            where = "subject LIKE ?"
            param = f"%{q[8:].strip()}%"
        elif q.upper().startswith("BODY:"):
            where = "body LIKE ?"
            param = f"%{q[5:].strip()}%"
        else:
            where = "from_addr LIKE ? OR subject LIKE ? OR body LIKE ?"
            param = f"%{q}%"
            rows = conn.execute(
                f"SELECT id, from_addr, subject, date, folder FROM mails WHERE {where} ORDER BY date DESC LIMIT 20",
                (param, param, param)
            ).fetchall()
            return _format_results(rows, query)

        rows = conn.execute(
            f"SELECT id, from_addr, subject, date, folder FROM mails WHERE {where} ORDER BY date DESC LIMIT 20",
            (param,)
        ).fetchall()
        return _format_results(rows, query)
    finally:
        conn.close()

def _format_results(rows, query):
    if not rows:
        return {"success": True, "data": f"Keine Mails gefunden fuer: {query}"}
    lines = [f"{len(rows)} Ergebnis(se):"]
    for r in rows:
        lines.append(f"  ID:{r['id']} | {r['from_addr'][:30]:30s} | {r['subject'][:40]:40s} | {r['date'][:16]} | {r['folder']}")
    return {"success": True, "data": "\n".join(lines)}

def _read(config, mail_id):
    if not mail_id:
        return {"success": False, "data": "Keine Mail-ID angegeben"}
    conn = _db(config)
    try:
        row = conn.execute("SELECT * FROM mails WHERE id = ?", (int(mail_id),)).fetchone()
        if not row:
            return {"success": False, "data": f"Mail {mail_id} nicht gefunden"}
        # Als gelesen markieren
        conn.execute("UPDATE mails SET read = 1 WHERE id = ?", (int(mail_id),))
        conn.commit()
        lines = [
            f"Von: {row['from_addr']}",
            f"An: {row['to_addr']}",
            f"Betreff: {row['subject']}",
            f"Datum: {row['date']}",
            f"Ordner: {row['folder']}",
            f"Quelle: {row['source']}",
            "",
            row['body'][:4000] if row['body'] else "(kein Inhalt)",
        ]
        return {"success": True, "data": "\n".join(lines)}
    finally:
        conn.close()

def _list(config, folder, limit):
    conn = _db(config)
    try:
        rows = conn.execute(
            "SELECT id, from_addr, subject, date, read FROM mails WHERE folder = ? ORDER BY date DESC LIMIT ?",
            (folder, limit)
        ).fetchall()
        if not rows:
            return {"success": True, "data": f"Keine Mails in Ordner '{folder}'"}
        lines = [f"{len(rows)} Mail(s) in '{folder}':"]
        for r in rows:
            unread = " [NEU]" if not r['read'] else ""
            lines.append(f"  ID:{r['id']} | {r['from_addr'][:30]:30s} | {r['subject'][:40]:40s} | {r['date'][:16]}{unread}")
        return {"success": True, "data": "\n".join(lines)}
    finally:
        conn.close()

def _stats(config):
    conn = _db(config)
    try:
        rows = conn.execute("""
            SELECT source, folder, COUNT(*) as total,
                   SUM(CASE WHEN read = 0 THEN 1 ELSE 0 END) as unread
            FROM mails GROUP BY source, folder ORDER BY source, folder
        """).fetchall()
        if not rows:
            return {"success": True, "data": "Keine Mails in der Datenbank"}
        lines = ["Mail-Statistiken:"]
        for r in rows:
            lines.append(f"  [{r['source']:6s}] {r['folder']:15s} — {r['total']} Mails ({r['unread']} ungelesen)")
        total = conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0]
        unread = conn.execute("SELECT COUNT(*) FROM mails WHERE read = 0").fetchone()[0]
        lines.append(f"\n  Gesamt: {total} Mails, {unread} ungelesen")
        return {"success": True, "data": "\n".join(lines)}
    finally:
        conn.close()


# === Shared function fuer IMAP/POP3 Module ===
def store_mail(db_path, message_id, from_addr, to_addr, subject, body, date, folder, source, raw_headers=""):
    """Speichert eine Mail in der SQLite DB. Returns True wenn neu, False wenn Duplikat."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO mails (message_id, from_addr, to_addr, subject, body, date, folder, source, raw_headers) VALUES (?,?,?,?,?,?,?,?,?)",
            (message_id, from_addr, to_addr, subject, body, date, folder, source, raw_headers)
        )
        conn.commit()
        return conn.execute("SELECT changes()").fetchone()[0] > 0
    finally:
        conn.close()


# === stdin/stdout Interface ===
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
