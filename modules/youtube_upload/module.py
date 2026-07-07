"""YouTube-Upload-Modul — laedt fertige Pipeline-Videos auf den Kanal.

Provider: YouTube Data API v3 (OAuth2 Refresh-Token-Flow). Die KI ruft
`youtube_upload.video` auf, wenn der User es sagt; der Upload erscheint als
sichtbarer Scheduler-Task (Transparenz-Prinzip). Setzt Titel/Beschreibung/Tags,
optional Thumbnail und Playlist.

Secrets (einmalig in der Google Cloud Console + tools/youtube_auth.py):
  client_id, client_secret, refresh_token  -> in die Modul-Settings.

WICHTIG (Google-Beschraenkung): Solange das Cloud-Projekt nicht den
YouTube-API-Audit bestanden hat, werden per API hochgeladene Videos auf
'private' gesperrt — der Kanaleigentuemer schaltet sie in YouTube Studio
manuell frei. default_privacy hier ist trotzdem durchgereicht (post-Audit
greift dann 'public'/'unlisted' direkt).
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

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels?part=snippet,statistics&mine=true"
PLAYLISTITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"

MODULE = {
    "name": "youtube_upload",
    "description": "Laedt ein fertiges Video auf den YouTube-Kanal (Data API v3, OAuth). Setzt Titel/Beschreibung/Tags, optional Thumbnail+Playlist.",
    "version": "1.0",
    "settings": {
        "enabled": {"type": "bool", "label": "Aktiv", "default": True},
        "client_id": {"type": "password", "label": "OAuth Client ID", "default": ""},
        "client_secret": {"type": "password", "label": "OAuth Client Secret", "default": ""},
        "refresh_token": {"type": "password", "label": "OAuth Refresh Token (aus youtube_auth.py)", "default": ""},
        "default_privacy": {"type": "string", "label": "Default-Sichtbarkeit (private/unlisted/public)", "default": "private"},
        "default_category_id": {"type": "string", "label": "Kategorie-ID (27=Bildung, 25=News, 24=Entertainment)", "default": "27"},
        "default_tags": {"type": "string", "label": "Default-Tags (kommagetrennt)", "default": ""},
        "description_suffix": {"type": "string", "label": "Beschreibungs-Zusatz (Footer)", "default": "\n\nErstellt mit einer KI-Recherche-Pipeline."},
        "made_for_kids": {"type": "bool", "label": "Made for Kids", "default": False},
        "request_timeout_s": {"type": "number", "label": "HTTP Timeout Sekunden", "default": 300},
    },
    "tools": [
        {
            "name": "youtube_upload.video",
            "description": (
                "Laedt EIN Video auf den Kanal. JSON {video_path, title, description?, tags?, "
                "privacy?=private|unlisted|public, category_id?, playlist_id?, thumbnail_path?}. "
                "Liefert video_id + URL. Hinweis: vor dem API-Audit landet das Video 'private'."
            ),
            "params": ["query_json"],
        },
        {
            "name": "youtube_upload.status",
            "description": "Prueft die OAuth-Credentials und zeigt den verbundenen Kanal (Name, Abos, Videos).",
            "params": [],
        },
    ],
}


def handle_tool(tool_name: str, params: Any, config: dict[str, Any]) -> dict[str, Any]:
    try:
        if not bool_param(config.get("enabled"), True):
            return fail("youtube_upload ist deaktiviert.")
        if tool_name == "youtube_upload.video":
            return upload_video(params, config)
        if tool_name == "youtube_upload.status":
            return status(config)
        return fail(f"Unbekanntes Tool: {tool_name}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        return fail(f"YOUTUBE_HTTP_{exc.code}: {body}")
    except Exception as exc:
        return fail(f"YOUTUBE_UPLOAD_FAILED: {exc}")


def upload_video(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    video_path = resolve_path(str(payload.get("video_path") or payload.get("path") or ""))
    if not video_path or not video_path.exists():
        return fail(f"video_path nicht gefunden: {video_path}")
    # Auto-Metadaten aus den Video-Assets (Titel/Beschreibung/Tags) — der Agent
    # laedt ohne Handarbeit sauber hoch. Explizite payload-Werte ueberschreiben.
    auto: dict[str, Any] = {}
    if bool_param(payload.get("auto_meta"), True):
        assets, query = find_workflow_assets(video_path)
        if assets:
            auto = build_auto_meta(assets, query)
    title = (str(payload.get("title") or "").strip() or auto.get("title") or video_path.stem)[:100]
    desc = str(payload.get("description") or "").strip() or auto.get("description") or ""
    chapters = build_chapters(video_path)
    if chapters and "0:00" not in desc:
        desc = (desc + "\n\n" + chapters).strip()
    suffix = str(config.get("description_suffix") or "")
    description = (desc + suffix)[:4900]
    tags = norm_tags(payload.get("tags"), None) or auto.get("tags") or norm_tags(config.get("default_tags"), None)
    privacy = str(payload.get("privacy") or config.get("default_privacy") or "private").lower()
    if privacy not in ("private", "unlisted", "public"):
        privacy = "private"
    category_id = str(payload.get("category_id") or config.get("default_category_id") or "27")
    timeout = int_param(config.get("request_timeout_s"), 300, 30, 1800)

    token = access_token(config)

    body = {
        "snippet": {"title": title, "description": description, "tags": tags, "categoryId": category_id},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool_param(config.get("made_for_kids"), False)},
    }
    data = json.dumps(body).encode("utf-8")
    size = video_path.stat().st_size
    init = urllib.request.Request(UPLOAD_URL, data=data, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/*",
        "X-Upload-Content-Length": str(size),
    })
    with urllib.request.urlopen(init, timeout=60) as resp:
        session_uri = resp.headers.get("Location")
    if not session_uri:
        return fail("Keine resumable Upload-Session erhalten (Location-Header fehlt).")

    # Bytes hochladen (ein PUT — Videos sind ~20 MB, das reicht hier).
    raw = video_path.read_bytes()
    put = urllib.request.Request(session_uri, data=raw, method="PUT", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "video/*",
        "Content-Length": str(len(raw)),
    })
    with urllib.request.urlopen(put, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8", errors="replace"))
    video_id = result.get("id")
    if not video_id:
        return fail(f"Upload ohne video_id: {json.dumps(result)[:200]}")

    notes: list[str] = []
    # Thumbnail (optional; braucht verifizierten Kanal)
    thumb = resolve_path(str(payload.get("thumbnail_path") or ""))
    if thumb and thumb.exists():
        try:
            set_thumbnail(token, video_id, thumb, timeout)
            notes.append("Thumbnail gesetzt")
        except Exception as exc:
            notes.append(f"Thumbnail fehlgeschlagen: {exc}")

    # Playlist (optional)
    playlist_id = str(payload.get("playlist_id") or "").strip()
    if playlist_id:
        try:
            add_to_playlist(token, video_id, playlist_id, timeout)
            notes.append(f"zu Playlist {playlist_id} hinzugefuegt")
        except Exception as exc:
            notes.append(f"Playlist fehlgeschlagen: {exc}")

    actual_privacy = (result.get("status") or {}).get("privacyStatus", privacy)
    if actual_privacy == "private" and privacy != "private":
        notes.append("Hinweis: auf 'private' gesperrt (App-Audit ausstehend) — in YouTube Studio freischalten.")

    return ok({
        "type": "youtube_upload.video",
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
        "title": title,
        "privacy": actual_privacy,
        "requested_privacy": privacy,
        "notes": notes,
    })


def set_thumbnail(token: str, video_id: str, thumb: Path, timeout: int) -> None:
    url = f"{THUMB_URL}?videoId={urllib.parse.quote(video_id)}"
    ctype = "image/png" if thumb.suffix.lower() == ".png" else "image/jpeg"
    req = urllib.request.Request(url, data=thumb.read_bytes(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": ctype,
    })
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def add_to_playlist(token: str, video_id: str, playlist_id: str, timeout: int) -> None:
    body = {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    req = urllib.request.Request(PLAYLISTITEMS_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def status(config: dict[str, Any]) -> dict[str, Any]:
    have = {k: bool(str(config.get(k) or "").strip()) for k in ("client_id", "client_secret", "refresh_token")}
    if not all(have.values()):
        missing = [k for k, v in have.items() if not v]
        return fail(f"OAuth-Credentials fehlen: {', '.join(missing)}. Siehe tools/youtube_auth.py.")
    token = access_token(config)
    req = urllib.request.Request(CHANNELS_URL, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    items = data.get("items") or []
    if not items:
        return fail("Token gueltig, aber kein Kanal gefunden (richtiger Google-Account/Brand-Account autorisiert?).")
    ch = items[0]
    snip = ch.get("snippet") or {}
    stat = ch.get("statistics") or {}
    return ok({
        "channel": snip.get("title"),
        "channel_id": ch.get("id"),
        "subscribers": stat.get("subscriberCount"),
        "videos": stat.get("videoCount"),
        "default_privacy": config.get("default_privacy"),
        "credentials": "ok",
    })


# ─── Auto-Metadaten ─────────────────────────────────────────────────────────
import re as _re

_STOP = {"und", "oder", "der", "die", "das", "im", "in", "ein", "eine", "den", "dem",
         "von", "zu", "fuer", "für", "mit", "auf", "wie", "was", "des", "bis", "nach",
         "vergleich", "internationalen", "the", "and", "for", "wird", "sind", "eines",
         "jahr", "neue", "neuer", "zwei", "mehr", "weg", "doch", "aber", "auch", "schon"}


def find_workflow_assets(video_path: Path) -> tuple[dict | None, str]:
    """Findet die video_assets.json + workflow-query zum Video.
    Layout: home/<modul>/videos/<wf>/video.mp4  <->  home/<modul>/workflows/<wf>/."""
    wf = video_path.parent.name
    candidates = [
        video_path.parent / "video_assets.json",
        video_path.parent.parent.parent / "workflows" / wf / "video_assets.json",
    ]
    for c in candidates:
        if c.exists():
            try:
                assets = json.loads(c.read_text(encoding="utf-8"))
            except Exception:
                continue
            query = ""
            wfj = c.parent / "workflow.json"
            if wfj.exists():
                try:
                    query = str(json.loads(wfj.read_text(encoding="utf-8")).get("query") or "")
                except Exception:
                    pass
            return assets, query
    return None, ""


def build_chapters(video_path: Path) -> str:
    """Kapitel-Timestamps aus dem Renderer-Storyboard (scene start_s + title).

    YouTube blendet Kapitel nur ein, wenn: erstes Kapitel bei 0:00, mindestens
    3 Kapitel, jedes >= 10s. Szenen unter 10s werden uebersprungen (ihr Inhalt
    laeuft im vorherigen Kapitel mit).
    """
    for cand in (video_path.parent / "storyboard_infographic.json",
                 video_path.parent / "storyboard.json"):
        if not cand.exists():
            continue
        try:
            scenes = (json.loads(cand.read_text(encoding="utf-8")) or {}).get("scenes") or []
        except Exception:
            continue
        lines: list[str] = []
        for sc in scenes:
            title = str(sc.get("title") or "").strip()
            try:
                start = float(sc.get("start_s") or 0.0)
                dur = float(sc.get("duration_s") or 0.0)
            except (TypeError, ValueError):
                continue
            if not title or (dur and dur < 10.0):
                continue
            if not lines:
                start = 0.0  # YouTube-Regel: erstes Kapitel exakt bei 0:00
            m, sec = divmod(int(start), 60)
            stamp = f"{m // 60}:{m % 60:02d}:{sec:02d}" if m >= 60 else f"{m}:{sec:02d}"
            lines.append(f"{stamp} {title}")
        if len(lines) >= 3:
            return "\u23f1 Kapitel:\n" + "\n".join(lines)
    return ""


def build_auto_meta(assets: dict, query: str) -> dict[str, Any]:
    title = str(assets.get("title") or "").strip()
    scenes = assets.get("scenes") or []
    opener = first_sentences(str(assets.get("voice_script") or ""), 320)
    topics = [str(s.get("title") or "").strip() for s in scenes
              if str(s.get("type") or "").lower() not in ("hook", "outro") and str(s.get("title") or "").strip()]
    tags = auto_tags(query, title, topics)
    parts: list[str] = []
    if opener:
        parts.append(opener)
    if topics:
        parts.append("\U0001F4CC Themen in diesem Video:\n" + "\n".join("• " + t for t in topics[:8]))
    hashtags = [f"#{_re.sub(r'[^A-Za-z0-9]', '', t)}" for t in tags[:4] if _re.sub(r'[^A-Za-z0-9]', '', t)]
    if hashtags:
        parts.append(" ".join(hashtags))
    return {"title": title, "description": "\n\n".join(parts), "tags": tags}


def first_sentences(text: str, max_chars: int = 320) -> str:
    text = _re.sub(r"\s+", " ", str(text)).strip()
    if not text:
        return ""
    out = ""
    for sent in _re.split(r"(?<=[.!?]) ", text):
        if out and len(out) + len(sent) > max_chars:
            break
        out = (out + " " + sent).strip()
    return out


def auto_tags(query: str, title: str, topics: list[str]) -> list[str]:
    words = _re.findall(r"[A-Za-zÄÖÜäöüß0-9]{4,}", f"{query} {title} {' '.join(topics)}")
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        wl = w.lower()
        if wl in _STOP or wl in seen:
            continue
        seen.add(wl)
        out.append(w)
    return out[:15]


# ─── Helpers ──────────────────────────────────────────────────────────────
def norm_tags(value: Any, default: Any) -> list[str]:
    def split(v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return [t.strip() for t in str(v or "").split(",") if t.strip()]
    tags = split(value) or split(default)
    return tags[:30]


def parse_payload(params: Any) -> dict[str, Any]:
    if isinstance(params, dict):
        return params
    if isinstance(params, list) and params:
        item = params[0]
        if isinstance(item, dict):
            return item
        text = str(item or "").strip()
        if text.startswith("{"):
            try:
                d = json.loads(text)
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
        return {"video_path": text} if text else {}
    return {}


def resolve_path(value: str) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p


def bool_param(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on"}


def int_param(value: Any, default: int, min_v: int | None = None, max_v: int | None = None) -> int:
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
                result = handle_tool(req.get("tool", ""), req.get("params", []), req.get("config", {}))
                print(json.dumps(result), flush=True)
            else:
                print(json.dumps({"error": f"Unknown action: {req.get('action')}"}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
