"""Community-Manager — Kommentar-Pflege fuer den YouTube-Kanal Fathom Reports.

Zieht regelmaessig neue Kommentare (commentThreads.list), ein Tages-LLM-Task
entwirft on-brand Antworten (community_manager.draft). LEITPLANKE: standardmaessig
NUR Entwuerfe — gepostet wird erst auf Freigabe (community_manager.decide
action=post). So kein Spam/ToS-Risiko durch generische Massen-Antworten.

WICHTIG: Antworten POSTEN braucht den Scope youtube.force-ssl (Re-Auth mit
youtube_auth.py). Kommentare LESEN geht mit dem vorhandenen youtube-Scope.
OAuth-Creds = dieselben wie youtube_upload (gleicher Kanal).

Storage (Modul-Home): seen.json (gesehene Kommentar-IDs), drafts.json (Entwuerfe).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from yt_oauth import access_token  # noqa: E402 — geteilter OAuth-Helfer (Dedup)

THREADS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"
COMMENTS_URL = "https://www.googleapis.com/youtube/v3/comments"
ROOT = Path(__file__).resolve().parents[2]

MODULE = {
    "name": "community_manager",
    "description": "YouTube-Kommentar-Pflege: neue Kommentare ziehen, Antwort-Entwuerfe verwalten, auf Freigabe posten (Leitplanke: kein Auto-Post).",
    "version": "1.0",
    "settings": {
        "enabled": {"type": "bool", "label": "Aktiv", "default": True},
        "client_id": {"type": "password", "label": "OAuth Client ID", "default": ""},
        "client_secret": {"type": "password", "label": "OAuth Client Secret", "default": ""},
        "refresh_token": {"type": "password", "label": "OAuth Refresh Token", "default": ""},
        "channel_id": {"type": "string", "label": "Kanal-ID", "default": ""},
        "tone": {"type": "string", "label": "Antwort-Ton", "default": "freundlich, sachlich, kurz, hilfreich; Deutsch; keine Floskeln"},
        "auto_post": {"type": "bool", "label": "Auto-Posten (Leitplanke: AUS lassen)", "default": False},
        "max_fetch": {"type": "number", "label": "Max Kommentare pro Lauf", "default": 25},
        "request_timeout_s": {"type": "number", "label": "HTTP Timeout", "default": 60},
    },
    "tools": [
        {"name": "community_manager.fetch", "description": "Zieht die neuesten Kanal-Kommentare (ungesehene). Liefert Liste {comment_id, author, text, video_id, likes, published}.", "params": ["query_json"]},
        {"name": "community_manager.draft", "description": "Speichert Antwort-Entwuerfe (vom Tages-LLM). JSON {drafts:[{comment_id, reply, category}]}.", "params": ["query_json"]},
        {"name": "community_manager.drafts", "description": "Listet offene Antwort-Entwuerfe (fuers Panel/Freigabe). JSON {status?,limit?}.", "params": ["query_json"]},
        {"name": "community_manager.decide", "description": "Aktion auf einen Entwurf. JSON {comment_id, action: post|skip|edit, reply?}. post braucht youtube.force-ssl.", "params": ["query_json"]},
        {"name": "community_manager.status", "description": "Credentials/Kanal/Zaehler.", "params": []},
    ],
}


def home(config: dict[str, Any]) -> Path:
    tmid = str(config.get("tool_modul_id") or "").strip()
    data = str(config.get("data_dir") or "").strip()
    base = (Path(data) / "home" / tmid) if (tmid and data) else Path(str(config.get("home_dir") or (ROOT / "agent-data" / "community_manager")))
    base.mkdir(parents=True, exist_ok=True)
    return base


def load(config, name, default):
    p = home(config) / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save(config, name, data):
    p = home(config) / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def handle_tool(tool_name: str, params: Any, config: dict[str, Any]) -> dict[str, Any]:
    try:
        if not bool_param(config.get("enabled"), True):
            return fail("community_manager ist deaktiviert.")
        fn = {
            "community_manager.fetch": fetch,
            "community_manager.draft": draft,
            "community_manager.drafts": list_drafts,
            "community_manager.decide": decide,
            "community_manager.status": status,
        }.get(tool_name)
        if not fn:
            return fail(f"Unbekanntes Tool: {tool_name}")
        return fn(parse_payload(params), config)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        hint = " (Posten braucht youtube.force-ssl — Re-Auth noetig)" if exc.code == 403 else ""
        return fail(f"YOUTUBE_HTTP_{exc.code}: {body}{hint}")
    except Exception as exc:
        return fail(f"COMMUNITY_MANAGER_FAILED: {exc}")


def fetch(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    channel = str(config.get("channel_id") or "").strip()
    if not channel:
        return fail("channel_id fehlt in den Settings.")
    token = access_token(config)
    maxn = int_param(payload.get("max") or config.get("max_fetch"), 25, 1, 100)
    url = THREADS_URL + "?" + urllib.parse.urlencode({
        "part": "snippet", "allThreadsRelatedToChannelId": channel,
        "order": "time", "maxResults": maxn, "textFormat": "plainText",
    })
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=int_param(config.get("request_timeout_s"), 60, 10, 300)) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    seen = set(load(config, "seen.json", []))
    out = []
    for it in data.get("items", []):
        top = (((it.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {})
        cid = ((it.get("snippet") or {}).get("topLevelComment") or {}).get("id") or it.get("id")
        if not cid or cid in seen:
            continue
        out.append({
            "comment_id": cid,
            "author": top.get("authorDisplayName"),
            "text": top.get("textDisplay") or top.get("textOriginal"),
            "video_id": top.get("videoId"),
            "likes": top.get("likeCount"),
            "published": top.get("publishedAt"),
        })
    return ok({"new_comments": len(out), "comments": out})


def draft(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    incoming = payload.get("drafts") or []
    if isinstance(incoming, dict):
        incoming = [incoming]
    drafts = load(config, "drafts.json", [])
    seen = set(load(config, "seen.json", []))
    have = {d.get("comment_id") for d in drafts}
    added = 0
    for d in incoming:
        if not isinstance(d, dict):
            continue
        cid = str(d.get("comment_id") or "").strip()
        reply = str(d.get("reply") or "").strip()
        if not cid or not reply or cid in have:
            continue
        drafts.append({
            "comment_id": cid, "reply": reply[:2000],
            "category": str(d.get("category") or "").strip(),
            "author": d.get("author"), "comment_text": d.get("comment_text") or d.get("text"),
            "status": "draft", "created": int(time.time()),
        })
        have.add(cid)
        seen.add(cid)
        added += 1
    save(config, "drafts.json", drafts)
    save(config, "seen.json", sorted(seen))
    return ok({"added": added, "open_drafts": len([d for d in drafts if d.get("status") == "draft"])})


def list_drafts(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    drafts = load(config, "drafts.json", [])
    st = str(payload.get("status") or "draft").strip().lower()
    items = [d for d in drafts if (st == "all" or d.get("status") == st)]
    return ok({"count": len(items), "drafts": items[: int_param(payload.get("limit"), 40, 1, 200)]})


def decide(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    cid = str(payload.get("comment_id") or "").strip()
    action = str(payload.get("action") or "").strip().lower()
    if action not in ("post", "skip", "edit"):
        return fail("action muss post|skip|edit sein.")
    drafts = load(config, "drafts.json", [])
    d = next((x for x in drafts if x.get("comment_id") == cid), None)
    if not d:
        return fail(f"Entwurf {cid} nicht gefunden.")
    if action == "edit":
        d["reply"] = str(payload.get("reply") or d.get("reply") or "")[:2000]
        save(config, "drafts.json", drafts)
        return ok({"comment_id": cid, "status": "draft", "reply": d["reply"]})
    if action == "skip":
        d["status"] = "skipped"
        save(config, "drafts.json", drafts)
        return ok({"comment_id": cid, "status": "skipped"})
    # post — braucht force-ssl
    reply = str(payload.get("reply") or d.get("reply") or "").strip()
    if not reply:
        return fail("Keine Antwort zum Posten.")
    token = access_token(config)
    body = {"snippet": {"parentId": cid, "textOriginal": reply}}
    req = urllib.request.Request(COMMENTS_URL + "?part=snippet", data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=int_param(config.get("request_timeout_s"), 60, 10, 300)) as resp:
        res = json.loads(resp.read().decode("utf-8", errors="replace"))
    d["status"] = "posted"
    d["posted_id"] = res.get("id")
    save(config, "drafts.json", drafts)
    return ok({"comment_id": cid, "status": "posted", "reply_id": res.get("id")})


def status(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    have = all(str(config.get(k) or "").strip() for k in ("client_id", "client_secret", "refresh_token"))
    drafts = load(config, "drafts.json", [])
    by = {}
    for d in drafts:
        by[d.get("status", "?")] = by.get(d.get("status", "?"), 0) + 1
    return ok({
        "credentials": "ok" if have else "fehlen",
        "channel_id": config.get("channel_id"),
        "auto_post": bool_param(config.get("auto_post"), False),
        "drafts_by_status": by,
        "seen_count": len(load(config, "seen.json", [])),
    })


# ─── Helpers ────────────────────────────────────────────────────────────────
def parse_payload(params: Any) -> dict[str, Any]:
    if isinstance(params, dict):
        return params
    if isinstance(params, list) and params:
        item = params[0]
        if isinstance(item, dict):
            return item
        text = str(item or "").strip()
        if text.startswith("{") or text.startswith("["):
            try:
                d = json.loads(text)
                return d if isinstance(d, dict) else {"drafts": d}
            except Exception:
                pass
    return {}


def bool_param(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on"}


def int_param(value: Any, default: int, min_v=None, max_v=None) -> int:
    try:
        out = int(float(value))
    except Exception:
        out = default
    if min_v is not None:
        out = max(min_v, out)
    if max_v is not None:
        out = min(max_v, out)
    return out


def ok(data: Any) -> dict[str, Any]:
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, indent=2)
    return {"success": True, "data": data}


def fail(data: Any) -> dict[str, Any]:
    return {"success": False, "data": str(data)}


if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            if req.get("action") == "describe":
                print(json.dumps(MODULE), flush=True)
            elif req.get("action") == "handle_tool":
                print(json.dumps(handle_tool(req.get("tool", ""), req.get("params", []), req.get("config", {}))), flush=True)
            else:
                print(json.dumps({"error": f"Unknown action: {req.get('action')}"}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
