"""SMTP Modul — E-Mail Versand."""
import json, sys, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

MODULE = {
    "name": "smtp",
    "description": "E-Mail Versand via SMTP",
    "version": "1.0",
    "settings": {
        "host": {"type": "string", "label": "SMTP Host", "default": ""},
        "port": {"type": "number", "label": "SMTP Port", "default": 587},
        "email": {"type": "string", "label": "E-Mail (Absender)", "default": ""},
        "password": {"type": "password", "label": "Passwort", "default": ""},
        "tls": {"type": "bool", "label": "STARTTLS", "default": True},
        "from_name": {"type": "string", "label": "Absendername", "default": "Agent"},
    },
    "tools": [
        {"name": "smtp.send", "description": "Sendet eine E-Mail", "params": ["to", "subject", "body"]},
    ],
}

def handle_tool(tool_name, params, config):
    if tool_name == "smtp.send":
        to = params[0] if params else ""
        subject = params[1] if len(params) > 1 else ""
        body = params[2] if len(params) > 2 else ""
        # Bei Komma-Split: alles ab param 2 joinen
        if len(params) > 3:
            body = ", ".join(params[2:])
        return _send(config, to, subject, body)
    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}

def _send(config, to, subject, body):
    host = config.get("host", "")
    port = int(config.get("port", 587))
    email_addr = config.get("email", "")
    password = config.get("password", "")
    use_tls = config.get("tls", True)
    from_name = config.get("from_name", "Agent")

    if not host or not email_addr:
        return {"success": False, "data": "SMTP nicht konfiguriert: host und email fehlen"}
    if not to or not subject:
        return {"success": False, "data": "Empfaenger und Betreff sind Pflicht"}

    try:
        msg = MIMEMultipart()
        msg["From"] = f"{from_name} <{email_addr}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        server = smtplib.SMTP(host, port)
        if use_tls:
            server.starttls()
        if password:
            server.login(email_addr, password)
        server.sendmail(email_addr, [to], msg.as_string())
        server.quit()

        return {"success": True, "data": f"Mail gesendet an {to}: {subject}"}
    except Exception as e:
        return {"success": False, "data": f"SMTP Fehler: {e}"}


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
