"""Content-Planner — autonome Redaktions-Pipeline fuer den YouTube-Kanal.

Idee: 1x/Tag scannt ein LLM-Task (Cron) die Interessen-Themen, sucht/crawlt
News, verbindet sie kausal zu Video-Vorschlaegen, bewertet sie selbst nach
YouTube-Kriterien (Nachfrage/Luecke/Watchtime-Hook/Aktualitaet/Themen-Fit) und
ruft `content_planner.save_proposals`. Dieses Modul haelt Interessen + Queue +
bereits-behandelte Themen (Dedup) und liefert die Daten fuer das Chat-Panel.

Transparenz: alles liegt als JSON im Modul-Home, der Scan laeuft als sichtbarer
Scheduler-Task. Dedup-Regel: ein Thema, das schon behandelt wurde, wird NIE
erneut vorgeschlagen — ausser der User markiert es explizit als wiederholbar.

Storage (im home_dir):
  interests.json  – Themen/Interessen (vom User pflegbar)
  queue.json      – aktive Vorschlaege (proposed|approved|queued|now|rejected)
  covered.json    – behandelte Themen (Dedup-Basis)
"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_INTERESTS = [
    {"theme": "Geopolitik", "keywords": "geopolitik, konflikte, handel, macht, energie, taiwan, brics"},
    {"theme": "Human Enhancement / Biologie", "keywords": "human enhancement, gene editing, crispr, neurotech, brain computer interface"},
    {"theme": "Anti-Aging / Longevity", "keywords": "longevity, anti-aging, zellbiologie, senolytics, reprogramming, lebensspanne"},
    {"theme": "Medizin-Durchbrueche", "keywords": "medizin durchbruch, therapie, krebs, mrna, klinische studie, zelltherapie"},
    {"theme": "Electronics", "keywords": "halbleiter, chips, batterie, photonik, quantencomputer, hardware"},
    {"theme": "KI / AI", "keywords": "ki, ai, llm, agent, robotik, automatisierung, durchbruch"},
    {"theme": "Emerging Tech", "keywords": "aufkommende technologie, fusion, energie, raumfahrt, materialwissenschaft"},
]

# Bereits behandelte Themen (aus frueheren Videos) — Dedup-Startwerte.
DEFAULT_COVERED = [
    "vimacs vim emacs entwickler", "scamiv person investigation",
    "angela merkel 2026", "smegma", "zukunft deutschlands 2100",
]

MODULE = {
    "name": "content_planner",
    "description": "Autonome Redaktions-Pipeline: Interessen-Themen, Video-Vorschlaege (Queue), Dedup gegen behandelte Themen. Fuettert die Video-Pipeline.",
    "version": "1.0",
    "settings": {
        "enabled": {"type": "bool", "label": "Aktiv", "default": True},
        "max_queue": {"type": "number", "label": "Max aktive Vorschlaege", "default": 30},
        "dedup_threshold": {"type": "number", "label": "Dedup-Aehnlichkeit (0-1)", "default": 0.5},
    },
    "tools": [
        {"name": "content_planner.save_proposals", "description": "Speichert neue Video-Vorschlaege (vom Tages-Scan). JSON {proposals:[{title,query,theme,rationale,sources?,score}]}. Dedupt gegen behandelte + vorhandene.", "params": ["query_json"]},
        {"name": "content_planner.proposals", "description": "Listet die aktive Vorschlags-Queue (nach Score sortiert) fuers Chat-Panel. JSON {status?,limit?}.", "params": ["query_json"]},
        {"name": "content_planner.decide", "description": "Aktion auf einen Vorschlag. JSON {id, action: now|next|approve|reject|snooze}. now/approve markiert behandelt + liefert query zum Triggern.", "params": ["query_json"]},
        {"name": "content_planner.interests", "description": "Interessen verwalten. JSON {action: get|add|remove|set, theme?, keywords?, items?}.", "params": ["query_json"]},
        {"name": "content_planner.covered", "description": "Behandelte Themen lesen/ergaenzen (Dedup-Basis). JSON {action: get|add, topic?}.", "params": ["query_json"]},
        {"name": "content_planner.status", "description": "Uebersicht: Interessen, Queue-Zahlen, behandelte Themen.", "params": []},
    ],
}


def home(config: dict[str, Any]) -> Path:
    h = str(config.get("home_dir") or "").strip()
    base = Path(h) if h else (ROOT / "agent-data" / "content_planner")
    base.mkdir(parents=True, exist_ok=True)
    return base


def load(config: dict[str, Any], name: str, default: Any) -> Any:
    p = home(config) / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save(config: dict[str, Any], name: str, data: Any) -> None:
    p = home(config) / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def interests_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    data = load(config, "interests.json", None)
    if data is None:
        data = list(DEFAULT_INTERESTS)
        save(config, "interests.json", data)
    return data


def covered_list(config: dict[str, Any]) -> list[str]:
    data = load(config, "covered.json", None)
    if data is None:
        data = list(DEFAULT_COVERED)
        save(config, "covered.json", data)
    return data


def queue_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    return load(config, "queue.json", [])


def handle_tool(tool_name: str, params: Any, config: dict[str, Any]) -> dict[str, Any]:
    try:
        if not bool_param(config.get("enabled"), True):
            return fail("content_planner ist deaktiviert.")
        fn = {
            "content_planner.save_proposals": save_proposals,
            "content_planner.proposals": list_proposals,
            "content_planner.decide": decide,
            "content_planner.interests": interests,
            "content_planner.covered": covered,
            "content_planner.status": status,
        }.get(tool_name)
        if not fn:
            return fail(f"Unbekanntes Tool: {tool_name}")
        return fn(parse_payload(params), config)
    except Exception as exc:
        return fail(f"CONTENT_PLANNER_FAILED: {exc}")


# ─── Tokenizer / Dedup ──────────────────────────────────────────────────────
_STOP = {"und", "oder", "der", "die", "das", "im", "in", "ein", "eine", "den", "von",
         "zu", "mit", "auf", "wie", "was", "des", "the", "and", "for", "a", "of"}


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zäöüß0-9]{3,}", str(text).lower()) if w not in _STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_duplicate(topic: str, config: dict[str, Any], extra: list[str]) -> str | None:
    thr = float_param(config.get("dedup_threshold"), 0.5)
    for prev in covered_list(config) + extra:
        if similarity(topic, prev) >= thr:
            return prev
    return None


# ─── Tools ──────────────────────────────────────────────────────────────────
def save_proposals(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    props = payload.get("proposals") or []
    if isinstance(props, dict):
        props = [props]
    q = queue_list(config)
    existing_topics = [str(p.get("title") or p.get("query") or "") for p in q]
    added, skipped = [], []
    for raw in props:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        query = str(raw.get("query") or raw.get("topic") or title).strip()
        if not (title or query):
            continue
        topic = title + " " + query
        dup = is_duplicate(topic, config, existing_topics)
        if dup:
            skipped.append({"title": title, "duplicate_of": dup})
            continue
        item = {
            "id": uuid.uuid4().hex[:10],
            "title": title or query[:80],
            "query": query,
            "theme": str(raw.get("theme") or "").strip(),
            "rationale": str(raw.get("rationale") or "").strip(),
            "sources": raw.get("sources") or [],
            "score": int_param(raw.get("score"), 50, 0, 100),
            "status": "proposed",
            "created": int(time.time()),
        }
        q.append(item)
        existing_topics.append(topic)
        added.append(item["title"])
    # Kappen auf max_queue (proposed niedrigster Score zuerst raus)
    cap = int_param(config.get("max_queue"), 30, 5, 200)
    actives = [p for p in q if p.get("status") in ("proposed", "queued", "next", "now", "approved")]
    if len(actives) > cap:
        actives.sort(key=lambda p: (p.get("status") != "proposed", p.get("score", 0)))
        drop = {id(p) for p in actives[: len(actives) - cap] if p.get("status") == "proposed"}
        q = [p for p in q if id(p) not in drop]
    save(config, "queue.json", q)
    return ok({"added": len(added), "skipped_duplicates": len(skipped),
               "titles": added, "skipped": skipped[:6]})


def list_proposals(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    q = queue_list(config)
    status_f = str(payload.get("status") or "").strip().lower()
    items = [p for p in q if (not status_f or p.get("status") == status_f)]
    # Reihenfolge: now -> next -> approved -> proposed (queued), je Score
    rank = {"now": 0, "next": 1, "approved": 2, "queued": 3, "proposed": 4}
    items.sort(key=lambda p: (rank.get(p.get("status"), 9), -int(p.get("score", 0))))
    limit = int_param(payload.get("limit"), 30, 1, 200)
    return ok({"count": len(items), "proposals": items[:limit]})


def decide(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    pid = str(payload.get("id") or "").strip()
    action = str(payload.get("action") or "").strip().lower()
    if action not in ("now", "next", "approve", "reject", "snooze"):
        return fail("action muss now|next|approve|reject|snooze sein.")
    q = queue_list(config)
    item = next((p for p in q if p.get("id") == pid), None)
    if not item:
        return fail(f"Vorschlag {pid} nicht gefunden.")
    if action == "reject":
        item["status"] = "rejected"
    elif action == "snooze":
        item["status"] = "proposed"
        item["snoozed_until"] = int(time.time()) + 7 * 86400
    elif action == "next":
        item["status"] = "next"
    else:  # now | approve -> wird produziert, gilt als behandelt
        item["status"] = "now" if action == "now" else "approved"
        cov = covered_list(config)
        topic = (item.get("title") or "") + " " + (item.get("query") or "")
        cov.append(topic.strip())
        save(config, "covered.json", cov)
    save(config, "queue.json", q)
    return ok({"id": pid, "status": item["status"], "query": item.get("query"),
               "title": item.get("title"),
               "trigger_video": action in ("now", "approve")})


def interests(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "get").strip().lower()
    data = interests_list(config)
    if action == "get":
        return ok({"interests": data})
    if action == "add":
        theme = str(payload.get("theme") or "").strip()
        if not theme:
            return fail("theme fehlt.")
        data.append({"theme": theme, "keywords": str(payload.get("keywords") or "").strip()})
    elif action == "remove":
        theme = str(payload.get("theme") or "").strip().lower()
        data = [d for d in data if str(d.get("theme", "")).lower() != theme]
    elif action == "set":
        items = payload.get("items")
        if isinstance(items, list):
            data = items
    else:
        return fail("action muss get|add|remove|set sein.")
    save(config, "interests.json", data)
    return ok({"interests": data})


def covered(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "get").strip().lower()
    data = covered_list(config)
    if action == "add":
        topic = str(payload.get("topic") or "").strip()
        if topic:
            data.append(topic)
            save(config, "covered.json", data)
        return ok({"covered": data, "added": topic})
    return ok({"covered": data})


def status(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    q = queue_list(config)
    by_status: dict[str, int] = {}
    for p in q:
        by_status[p.get("status", "?")] = by_status.get(p.get("status", "?"), 0) + 1
    return ok({
        "interests": [d.get("theme") for d in interests_list(config)],
        "queue_total": len(q),
        "by_status": by_status,
        "covered_count": len(covered_list(config)),
    })


# ─── Helpers ──────────────────────────────────────────────────────────────
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
                return d if isinstance(d, dict) else {"proposals": d}
            except Exception:
                pass
        return {"topic": text} if text else {}
    return {}


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


def float_param(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


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
