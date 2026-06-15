"""Geteilter YouTube-OAuth-Helfer (von youtube_upload + community_manager genutzt).

Refresh-Token -> kurzlebiges Access-Token. Vorher war diese Funktion in beiden
Modulen dupliziert (DRY-Verstoss). Einbindung wie x_api_common:
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from yt_oauth import access_token
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

TOKEN_URL = "https://oauth2.googleapis.com/token"


def access_token(config: dict[str, Any]) -> str:
    cid = str(config.get("client_id") or "").strip()
    secret = str(config.get("client_secret") or "").strip()
    refresh = str(config.get("refresh_token") or "").strip()
    if not (cid and secret and refresh):
        raise RuntimeError("OAuth-Credentials fehlen (client_id/client_secret/refresh_token).")
    data = urllib.parse.urlencode({
        "client_id": cid, "client_secret": secret,
        "refresh_token": refresh, "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        tok = json.loads(resp.read().decode("utf-8", errors="replace"))
    at = tok.get("access_token")
    if not at:
        raise RuntimeError(f"Kein access_token: {json.dumps(tok)[:160]}")
    return at
