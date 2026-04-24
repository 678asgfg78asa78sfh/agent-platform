"""IMAP Modul — E-Mail Empfang mit Filtern und Auto-Download. Speichert in SQLite via mailstore."""
import json, sys, os, imaplib, email
from email.header import decode_header

MODULE = {
    "name": "imap",
    "description": "E-Mail Empfang via IMAP mit Filtern und Auto-Download",
    "version": "1.0",
    "settings": {
        "host": {"type": "string", "label": "IMAP Host", "default": ""},
        "port": {"type": "number", "label": "IMAP Port", "default": 993},
        "email": {"type": "string", "label": "E-Mail", "default": ""},
        "password": {"type": "password", "label": "Passwort", "default": ""},
        "tls": {"type": "bool", "label": "TLS/SSL", "default": True},
        "folder": {"type": "string", "label": "Ordner", "default": "INBOX"},
        "auto_download": {"type": "bool", "label": "Auto-Download (Cycle)", "default": False},
        "db_path": {"type": "string", "label": "DB Pfad", "default": "agent-data/mail.db"},
        "dkim_required": {"type": "bool", "label": "DKIM erforderlich", "default": False},
        "dmarc_required": {"type": "bool", "label": "DMARC erforderlich", "default": False},
        "arc_required": {"type": "bool", "label": "ARC erforderlich", "default": False},
        "sender_whitelist": {"type": "list", "label": "Absender-Whitelist", "default": []},
        "keyword_filter": {"type": "list", "label": "Betreff-Keywords (mind. 1 muss matchen)", "default": []},
        "secret_phrase": {"type": "string", "label": "Secret Phrase (muss im Body vorkommen)", "default": ""},
        "ip_whitelist": {"type": "list", "label": "IP-Whitelist (Received-Header)", "default": []},
    },
    "tools": [
        {"name": "imap.fetch", "description": "Laedt neue/ungelesene Mails herunter, wendet Filter an, speichert in DB", "params": []},
        {"name": "imap.search", "description": "Durchsucht das IMAP-Postfach direkt auf dem Server", "params": ["query"]},
        {"name": "imap.folders", "description": "Listet verfuegbare IMAP-Ordner", "params": []},
    ],
}

def handle_tool(tool_name, params, config):
    host = config.get("host", "")
    if not host:
        return {"success": False, "data": "IMAP nicht konfiguriert: host fehlt"}

    if tool_name == "imap.fetch":
        return _fetch(config)
    elif tool_name == "imap.search":
        return _search(config, params[0] if params else "")
    elif tool_name == "imap.folders":
        return _folders(config)
    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}


def _connect(config):
    host = config["host"]
    port = int(config.get("port", 993))
    user = config.get("email", "")
    pw = config.get("password", "")
    use_tls = config.get("tls", True)

    if use_tls:
        conn = imaplib.IMAP4_SSL(host, port)
    else:
        conn = imaplib.IMAP4(host, port)
    conn.login(user, pw)
    return conn


def _decode_header_value(raw):
    if raw is None:
        return ""
    parts = decode_header(raw)
    result = []
    for data, charset in parts:
        if isinstance(data, bytes):
            result.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(data))
    return " ".join(result)


def _parse_mail(raw_bytes):
    msg = email.message_from_bytes(raw_bytes)
    from_addr = _decode_header_value(msg.get("From", ""))
    to_addr = _decode_header_value(msg.get("To", ""))
    subject = _decode_header_value(msg.get("Subject", ""))
    date_str = msg.get("Date", "")
    message_id = msg.get("Message-ID", "")

    # Body extrahieren
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")

    # Raw headers fuer Filter
    raw_headers = ""
    for key in ["Authentication-Results", "Received", "ARC-Authentication-Results"]:
        vals = msg.get_all(key, [])
        for v in vals:
            raw_headers += f"{key}: {v}\n"

    return {
        "message_id": message_id,
        "from_addr": from_addr,
        "to_addr": to_addr,
        "subject": subject,
        "body": body,
        "date": date_str,
        "raw_headers": raw_headers,
    }


def _apply_filters(mail_data, config):
    """Wendet Filter an. Returns (pass: bool, reason: str)."""
    headers = mail_data.get("raw_headers", "").lower()
    from_addr = mail_data.get("from_addr", "").lower()
    subject = mail_data.get("subject", "").lower()
    body = mail_data.get("body", "")

    # DKIM
    if config.get("dkim_required"):
        if "dkim=pass" not in headers:
            return False, "DKIM check failed"

    # DMARC
    if config.get("dmarc_required"):
        if "dmarc=pass" not in headers:
            return False, "DMARC check failed"

    # ARC
    if config.get("arc_required"):
        if "arc=pass" not in headers:
            return False, "ARC check failed"

    # Sender Whitelist
    whitelist = config.get("sender_whitelist", [])
    if whitelist:
        if not any(w.lower() in from_addr for w in whitelist):
            return False, f"Absender nicht in Whitelist: {from_addr}"

    # Keyword Filter (mind. 1 muss im Betreff matchen)
    keywords = config.get("keyword_filter", [])
    if keywords:
        if not any(kw.lower() in subject for kw in keywords):
            return False, f"Kein Keyword im Betreff: {mail_data.get('subject', '')}"

    # Secret Phrase
    phrase = config.get("secret_phrase", "")
    if phrase:
        if phrase.lower() not in body.lower():
            return False, "Secret Phrase nicht gefunden"

    # IP Whitelist (best-effort aus Received-Headers)
    ip_list = config.get("ip_whitelist", [])
    if ip_list:
        received_lines = [l for l in headers.split("\n") if l.startswith("received:")]
        found_ip = False
        for ip in ip_list:
            for line in received_lines:
                if ip.lower() in line:
                    found_ip = True
                    break
        if not found_ip:
            return False, f"Keine erlaubte IP in Received-Headers"

    return True, "OK"


def _fetch(config):
    conn = None
    try:
        conn = _connect(config)
        folder = config.get("folder", "INBOX")
        conn.select(folder)

        # Ungelesene Mails holen
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return {"success": False, "data": f"IMAP SEARCH fehlgeschlagen: {status}"}

        msg_ids = data[0].split() if data[0] else []
        if not msg_ids:
            return {"success": True, "data": "Keine neuen Mails"}

        db_path = config.get("db_path", "agent-data/mail.db")
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mailstore"))
        from module import store_mail

        stored = 0
        filtered = 0
        errors = []

        for mid in msg_ids[:50]:
            try:
                status, msg_data = conn.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue
                raw = msg_data[0][1]
                parsed = _parse_mail(raw)

                passed, reason = _apply_filters(parsed, config)
                if not passed:
                    filtered += 1
                    continue

                ok = store_mail(
                    db_path, parsed["message_id"], parsed["from_addr"],
                    parsed["to_addr"], parsed["subject"], parsed["body"],
                    parsed["date"], folder, "imap", parsed["raw_headers"]
                )
                if ok:
                    stored += 1
            except Exception as e:
                errors.append(str(e))

        result = f"{stored} neue Mails gespeichert, {filtered} gefiltert"
        if errors:
            result += f", {len(errors)} Fehler"
        result += f" (von {len(msg_ids)} ungelesen)"
        return {"success": True, "data": result}

    except Exception as e:
        return {"success": False, "data": f"IMAP Fehler: {e}"}
    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass


def _search(config, query):
    if not query.strip():
        return {"success": False, "data": "Keine Suchanfrage"}
    conn = None
    try:
        conn = _connect(config)
        folder = config.get("folder", "INBOX")
        conn.select(folder)

        # Sanitize
        safe_query = query.replace('"', '').replace('\\', '')
        if ':' in safe_query:
            search_cmd = safe_query
        else:
            search_cmd = f'OR SUBJECT "{safe_query}" FROM "{safe_query}"'

        status, data = conn.search(None, search_cmd)
        if status != "OK":
            return {"success": False, "data": f"Suche fehlgeschlagen: {status}"}

        msg_ids = data[0].split() if data[0] else []
        if not msg_ids:
            return {"success": True, "data": f"Keine Mails gefunden fuer: {query}"}

        # Envelopes holen
        results = []
        for mid in msg_ids[-10:]:  # Letzte 10
            status, msg_data = conn.fetch(mid, "(ENVELOPE)")
            if status == "OK" and msg_data[0]:
                # Einfache Darstellung
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                results.append(f"  ID:{mid.decode()} — {str(raw)[:100]}")

        return {"success": True, "data": f"{len(msg_ids)} Treffer:\n" + "\n".join(results)}

    except Exception as e:
        return {"success": False, "data": f"IMAP Suche Fehler: {e}"}
    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass


def _folders(config):
    conn = None
    try:
        conn = _connect(config)
        status, folders = conn.list()
        if status != "OK":
            return {"success": False, "data": f"Ordner auflisten fehlgeschlagen: {status}"}
        lines = ["IMAP Ordner:"]
        for f in folders:
            decoded = f.decode("utf-8", errors="replace") if isinstance(f, bytes) else str(f)
            # Ordnername extrahieren (nach letztem Leerzeichen oder Anführungszeichen)
            parts = decoded.rsplit('"', 2)
            name = parts[-1].strip() if len(parts) > 1 else decoded.rsplit(" ", 1)[-1]
            lines.append(f"  {name}")
        return {"success": True, "data": "\n".join(lines)}
    except Exception as e:
        return {"success": False, "data": f"IMAP Fehler: {e}"}
    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass


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
