#!/usr/bin/env python3
"""Einmaliger OAuth-Helfer fuer das youtube_upload-Modul.

Holt den langlebigen **Refresh-Token** fuer deinen Kanal. Auf einem Rechner MIT
Browser ausfuehren (z.B. dein Laptop) — er braucht nur die Client-ID/Secret aus
der Google Cloud Console, keinen Agent-Zugriff. Danach client_id, client_secret
und den ausgegebenen refresh_token in die Modul-Settings von youtube_upload.default
eintragen.

Voraussetzung in der Cloud Console:
  1. Projekt anlegen, "YouTube Data API v3" aktivieren.
  2. OAuth-Zustimmungsbildschirm: External, App veroeffentlichen (Production!),
     Scope youtube.upload — sonst laeuft der Refresh-Token nach 7 Tagen ab.
  3. Anmeldedaten -> OAuth-Client-ID -> Typ "Desktopanwendung" -> JSON laden.

Aufruf:
  python3 youtube_auth.py --secrets client_secret_XXX.json
  python3 youtube_auth.py --client-id ... --client-secret ...
"""

from __future__ import annotations

import argparse
import http.server
import json
import socket
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.force-ssl"


def load_secrets(args) -> tuple[str, str]:
    if args.secrets:
        data = json.load(open(args.secrets, encoding="utf-8"))
        node = data.get("installed") or data.get("web") or data
        return node["client_id"], node["client_secret"]
    if args.client_id and args.client_secret:
        return args.client_id, args.client_secret
    sys.exit("Brauche --secrets <client_secret.json> ODER --client-id und --client-secret.")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CodeHandler(http.server.BaseHTTPRequestHandler):
    code: str | None = None

    def do_GET(self):
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        CodeHandler.code = (params.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Fertig! Du kannst dieses Fenster schliessen und ins Terminal zurueck." if CodeHandler.code \
            else "Kein Code erhalten — bitte erneut versuchen."
        self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *a):  # stumm
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--secrets", help="client_secret_*.json aus der Cloud Console")
    p.add_argument("--client-id")
    p.add_argument("--client-secret")
    args = p.parse_args()
    client_id, client_secret = load_secrets(args)

    port = free_port()
    redirect_uri = f"http://127.0.0.1:{port}"
    auth = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",   # -> refresh_token
        "prompt": "consent",        # erzwingt frischen refresh_token
    })

    server = http.server.HTTPServer(("127.0.0.1", port), CodeHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("\nOeffne diese URL im Browser (falls sie sich nicht automatisch oeffnet):\n")
    print(auth + "\n")
    print('Bei "Google hat diese App nicht verifiziert": Erweitert -> Trotzdem fortfahren.\n')
    try:
        webbrowser.open(auth)
    except Exception:
        pass

    print(f"Warte auf die Anmeldung (Loopback {redirect_uri}) ...")
    # handle_request() im Thread beendet sich nach genau 1 Request; hier warten:
    import time
    for _ in range(300):
        if CodeHandler.code:
            break
        time.sleep(1)
    if not CodeHandler.code:
        sys.exit("Timeout: kein Auth-Code empfangen.")

    data = urllib.parse.urlencode({
        "code": CodeHandler.code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        tok = json.loads(resp.read().decode("utf-8", errors="replace"))

    refresh = tok.get("refresh_token")
    if not refresh:
        sys.exit(f"Kein refresh_token erhalten (App auf 'Production' veroeffentlicht?). Antwort: {tok}")

    print("\n" + "=" * 64)
    print("ERFOLG — trage diese drei Werte in youtube_upload.default ein:\n")
    print(f"  client_id     = {client_id}")
    print(f"  client_secret = {client_secret}")
    print(f"  refresh_token = {refresh}")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    main()
