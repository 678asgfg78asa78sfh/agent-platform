"""POP3 Modul — E-Mail Empfang via POP3 mit Filtern. Speichert in SQLite via mailstore."""
import json, sys, os, poplib, email
from email.header import decode_header

MODULE = {
    "name": "pop3",
    "description": "E-Mail Empfang via POP3 mit Filtern",
    "version": "1.0",
    "settings": {
        "host": {"type": "string", "label": "POP3 Host", "default": ""},
        "port": {"type": "number", "label": "POP3 Port", "default": 995},
        "email": {"type": "string", "label": "E-Mail", "default": ""},
        "password": {"type": "password", "label": "Passwort", "default": ""},
        "tls": {"type": "bool", "label": "TLS/SSL", "default": True},
        "auto_download": {"type": "bool", "label": "Auto-Download (Cycle)", "default": False},
        "db_path": {"type": "string", "label": "DB Pfad", "default": "agent-data/mail.db"},
        "dkim_required": {"type": "bool", "label": "DKIM erforderlich", "default": False},
        "dmarc_required": {"type": "bool", "label": "DMARC erforderlich", "default": False},
        "sender_whitelist": {"type": "list", "label": "Absender-Whitelist", "default": []},
        "keyword_filter": {"type": "list", "label": "Betreff-Keywords", "default": []},
        "secret_phrase": {"type": "string", "label": "Secret Phrase (Body)", "default": ""},
    },
    "tools": [
        {"name": "pop3.fetch", "description": "Laedt Mails via POP3 herunter, wendet Filter an, speichert in DB", "params": []},
        {"name": "pop3.count", "description": "Zeigt Anzahl und Groesse der Mails auf dem Server", "params": []},
    ],
}

def handle_tool(tool_name, params, config):
    host = config.get("host", "")
    if not host:
        return {"success": False, "data": "POP3 nicht konfiguriert: host fehlt"}
    if tool_name == "pop3.fetch":
        return _fetch(config)
    elif tool_name == "pop3.count":
        return _count(config)
    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}


def _connect(config):
    host = config["host"]
    port = int(config.get("port", 995))
    use_tls = config.get("tls", True)
    if use_tls:
        conn = poplib.POP3_SSL(host, port)
    else:
        conn = poplib.POP3(host, port)
    conn.user(config.get("email", ""))
    conn.pass_(config.get("password", ""))
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

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

    raw_headers = ""
    for key in ["Authentication-Results", "Received"]:
        for v in msg.get_all(key, []):
            raw_headers += f"{key}: {v}\n"

    return {
        "message_id": message_id, "from_addr": from_addr, "to_addr": to_addr,
        "subject": subject, "body": body, "date": date_str, "raw_headers": raw_headers,
    }


def _apply_filters(mail_data, config):
    headers = mail_data.get("raw_headers", "").lower()
    from_addr = mail_data.get("from_addr", "").lower()
    subject = mail_data.get("subject", "").lower()
    body = mail_data.get("body", "")

    if config.get("dkim_required") and "dkim=pass" not in headers:
        return False, "DKIM failed"
    if config.get("dmarc_required") and "dmarc=pass" not in headers:
        return False, "DMARC failed"

    whitelist = config.get("sender_whitelist", [])
    if whitelist and not any(w.lower() in from_addr for w in whitelist):
        return False, "Absender nicht in Whitelist"

    keywords = config.get("keyword_filter", [])
    if keywords and not any(kw.lower() in subject for kw in keywords):
        return False, "Kein Keyword im Betreff"

    phrase = config.get("secret_phrase", "")
    if phrase and phrase.lower() not in body.lower():
        return False, "Secret Phrase nicht gefunden"

    return True, "OK"


def _fetch(config):
    conn = None
    try:
        conn = _connect(config)
        count, size = conn.stat()
        if count == 0:
            return {"success": True, "data": "Keine Mails auf dem Server"}

        db_path = config.get("db_path", "agent-data/mail.db")
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mailstore"))
        from module import store_mail

        stored = 0
        filtered = 0
        limit = min(count, 50)

        for i in range(1, limit + 1):
            try:
                status, lines, octets = conn.retr(i)
                raw = b"\r\n".join(lines)
                parsed = _parse_mail(raw)

                passed, reason = _apply_filters(parsed, config)
                if not passed:
                    filtered += 1
                    continue

                ok = store_mail(
                    db_path, parsed["message_id"], parsed["from_addr"],
                    parsed["to_addr"], parsed["subject"], parsed["body"],
                    parsed["date"], "INBOX", "pop3", parsed["raw_headers"]
                )
                if ok:
                    stored += 1
            except Exception:
                continue

        return {"success": True, "data": f"{stored} neue Mails gespeichert, {filtered} gefiltert (von {count} auf Server)"}
    except Exception as e:
        return {"success": False, "data": f"POP3 Fehler: {e}"}
    finally:
        if conn:
            try:
                conn.quit()
            except Exception:
                pass


def _count(config):
    conn = None
    try:
        conn = _connect(config)
        count, size = conn.stat()
        return {"success": True, "data": f"{count} Mails auf dem Server ({size} Bytes)"}
    except Exception as e:
        return {"success": False, "data": f"POP3 Fehler: {e}"}
    finally:
        if conn:
            try:
                conn.quit()
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
