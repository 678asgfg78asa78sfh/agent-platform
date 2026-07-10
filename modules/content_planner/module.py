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
    {"theme": "US-Politik & Weltlage", "keywords": "usa, trump, wahl, washington, china, russland, geopolitik, konflikt, sanktionen, deal"},
    {"theme": "Persoenlichkeiten & Macher", "keywords": "elon musk, milliardaere, gruender, forscher, kontroverse person, portrait, aufstieg, skandal"},
    {"theme": "SpaceX & Raumfahrt", "keywords": "spacex, starship, mars, mond, nasa, satelliten, rakete, weltraum, artemis"},
    {"theme": "Antarktis & Extreme Erde", "keywords": "antarktis, arktis, tiefsee, pole, gletscher, geheimnis, expedition, klima-kipppunkt"},
    {"theme": "KI & Robotik", "keywords": "ki, ai, llm, agenten, humanoide roboter, automatisierung, durchbruch, openai, deepmind"},
    {"theme": "Longevity & Biotech", "keywords": "longevity, anti-aging, crispr, gentechnik, zelltherapie, neurotech, medizin durchbruch"},
    {"theme": "Energie & Electronics", "keywords": "fusion, kernfusion, batterie, halbleiter, chips, solar, quantencomputer, netz"},
    {"theme": "Trends & Viral", "keywords": "viral, trending, kontrovers, aufkochend, was alle reden, kurios, durchbruch der woche"},
    {"theme": "Zukunft & Big Ideas", "keywords": "zukunft, prognose, was-waere-wenn, szenario, gesellschaft, wohlstand, kausalitaet"},
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
        "similarity_cooldown_days": {"type": "number", "label": "Aehnlichkeits-Cooldown Tage (0=aus)", "default": 7},
        "similarity_block_strength": {"type": "number", "label": "Cooldown-Staerke Prozent (100=voller Block)", "default": 100},
        "demand_lift_score": {"type": "number", "label": "Ab diesem Score halbiert sich der Cooldown (Demand-Lift)", "default": 88},
        "enabled": {"type": "bool", "label": "Aktiv", "default": True},
        "max_queue": {"type": "number", "label": "Max aktive Vorschlaege", "default": 30},
        "max_per_theme": {"type": "number", "label": "Max aktive Vorschlaege pro Thema (Vielfalt)", "default": 2},
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
    # Stabiler Pfad = content_planner-Modul-Home, EGAL welcher Caller das Tool ruft
    # (Chat-LLM, Cron-Scan, ...). tool_modul_id ist die Settings-Modul-Instanz
    # (content_planner.default); sonst landete die Queue im Caller-Home.
    tmid = str(config.get("tool_modul_id") or "").strip()
    data = str(config.get("data_dir") or "").strip()
    if tmid and data:
        base = Path(data) / "home" / tmid
    else:
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


def covered_raw(config: dict[str, Any]) -> list[Any]:
    """covered.json roh (Strings legacy, Dicts mit ts) — NUR hierueber
    schreiben, sonst gehen die Zeitstempel beim Roundtrip verloren."""
    data = load(config, "covered.json", None)
    if not isinstance(data, list):
        data = []
        save(config, "covered.json", data)
    return data


def covered_list(config: dict[str, Any]) -> list[str]:
    """Kompatibel: nur die Themen-Strings (fuer Dedup/Anzeige)."""
    out = []
    for item in covered_raw(config):
        if isinstance(item, dict):
            out.append(str(item.get("topic") or ""))
        else:
            out.append(str(item))
    return [t for t in out if t]



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


# ─── Normalisierung ─────────────────────────────────────────────────────────
def coerce_sources(value: Any) -> list[str]:
    """sources IMMER als Liste — LLM liefert manchmal 'A; B; C' als String."""
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()][:6]
    if isinstance(value, str) and value.strip():
        return [s.strip() for s in re.split(r"[;\n]+", value) if s.strip()][:6]
    return []


# Voting-Gewichtung: woraus sich der Score zusammensetzt (transparent + anpassbar).
VOTE_WEIGHTS = {"trend": 0.35, "hook": 0.30, "gap": 0.25, "watchtime": 0.10}
SIM_PENALTY_MAX = 40  # max. Minus-Gewicht bei hoher Aehnlichkeit zu schon Behandeltem


def weighted_score(breakdown: dict[str, Any]) -> float | None:
    """Gewichteter Gesamt-Score aus den Faktoren trend/hook/gap/watchtime."""
    total = 0.0
    wsum = 0.0
    for k, w in VOTE_WEIGHTS.items():
        v = breakdown.get(k)
        if isinstance(v, (int, float)):
            total += float(v) * w
            wsum += w
    return (total / wsum) if wsum else None


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
    covered = covered_list(config)  # schon behandelte Themen — fuer Strafe + Cancel
    added, skipped = [], []
    # Vielfalt: max N aktive Vorschlaege pro Thema (sonst klumpt alles in ein Thema).
    cap_theme = int_param(config.get("max_per_theme"), 2, 1, 10)
    active_states = ("proposed", "queued", "next", "now", "approved")
    theme_counts: dict[str, int] = {}
    for p in q:
        if p.get("status") in active_states:
            th = str(p.get("theme") or "").strip().lower()
            theme_counts[th] = theme_counts.get(th, 0) + 1
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
        theme_key = str(raw.get("theme") or "").strip().lower()
        if theme_key and theme_counts.get(theme_key, 0) >= cap_theme:
            skipped.append({"title": title, "theme_voll": raw.get("theme")})
            continue
        theme_counts[theme_key] = theme_counts.get(theme_key, 0) + 1
        # Transparentes Voting: Faktoren (trend/hook/gap/watchtime) -> gewichteter
        # Basis-Score; falls LLM nur einen Gesamtscore liefert, den nehmen.
        raw_scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
        breakdown = {k: int_param(raw_scores.get(k), 0, 0, 100) for k in VOTE_WEIGHTS} if raw_scores else {}
        base = weighted_score(breakdown)
        if base is None:
            base = float(int_param(raw.get("score"), 50, 0, 100))
        # Aehnlichkeits-Strafe (Minus-Gewicht): je aehnlicher zu schon Behandeltem
        # oder bereits in der Queue, desto mehr Abzug.
        sims = [similarity(topic, prev) for prev in covered] + [similarity(topic, e) for e in existing_topics]
        maxsim = max(sims) if sims else 0.0
        penalty = round(maxsim * SIM_PENALTY_MAX)
        final = max(0, min(100, round(base - penalty)))
        item = {
            "id": uuid.uuid4().hex[:10],
            "title": title or query[:80],
            "query": query,
            "theme": str(raw.get("theme") or "").strip(),
            "rationale": str(raw.get("rationale") or "").strip(),
            "sources": coerce_sources(raw.get("sources")),
            "scores": breakdown,
            "similarity_penalty": penalty,
            "score": final,
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


_SIM_STOP = {"der","die","das","und","oder","von","mit","fuer","für","the","and","for","von","aus","wie","was","wer","2026","2025","2027","neue","neuer","neues","jahr","test","grosse","große"}


def _sim_tokens(text: str) -> set[str]:
    """Leichte Normalisierung fuer Themen-Vergleich: Umlaute, lowercase,
    Suffixe (-en/-er/-e/-s) kappen, Stopwoerter raus. 'Fabriken'/'Fabrikhalle'
    und 'Roboter'/'humanoide Robots' sollen sich treffen."""
    import re as _re
    text = str(text or "").lower()
    for a, b in (("ä","a"),("ö","o"),("ü","u"),("ß","ss")):
        text = text.replace(a, b)
    out = set()
    for w in _re.findall(r"[a-z0-9]+", text):
        if len(w) < 3 or w in _SIM_STOP:
            continue
        if len(w) > 5:
            for suf in ("en","er","es","e","s","n"):
                if w.endswith(suf):
                    w = w[: -len(suf)]
                    break
        out.add(w)
    return out


def _topic_similarity(a: str, b: str) -> float:
    """Overlap-Koeffizient (Schnitt / kleinere Menge), 0..1."""
    ta, tb = _sim_tokens(a), _sim_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb))


def covered_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    """covered.json normalisiert: Legacy-Strings (ohne Zeit) -> ts=0."""
    out = []
    for item in covered_raw(config):
        if isinstance(item, dict):
            out.append({"topic": str(item.get("topic") or ""), "ts": int_param(item.get("ts"), 0, 0, 2**62)})
        else:
            out.append({"topic": str(item), "ts": 0})
    return out


def similarity_penalty(topic: str, score: int, config: dict[str, Any]) -> tuple[float, str]:
    """Einstellbarer Aehnlichkeits-Cooldown: startet bei (strength * similarity)
    und faellt LINEAR ueber similarity_cooldown_days auf 0. Themen mit sehr
    hohem Demand (score >= demand_lift_score) zahlen nur die halbe Strafe."""
    days = int_param(config.get("similarity_cooldown_days"), 7, 0, 90)
    if days <= 0:
        return 0.0, ""
    strength = int_param(config.get("similarity_block_strength"), 100, 0, 100) / 100.0
    lift_score = int_param(config.get("demand_lift_score"), 88, 0, 100)
    now = int(time.time())
    worst = 0.0
    worst_ref = ""
    for entry in covered_entries(config):
        ts = entry["ts"]
        if ts <= 0:
            continue
        age_days = max(0.0, (now - ts) / 86400.0)
        if age_days >= days:
            continue
        sim = _topic_similarity(topic, entry["topic"])
        if sim < 0.3:
            continue
        decay = 1.0 - (age_days / days)  # linear: frisch=1.0 -> nach `days`=0
        pen = sim * decay * strength
        if pen > worst:
            worst = pen
            worst_ref = entry["topic"][:60]
    if worst > 0 and score >= lift_score:
        worst *= 0.5  # Demand-Lift: grosses Thema darf frueher wieder ran
    return min(worst, 1.0), worst_ref


def list_proposals(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    q = queue_list(config)
    status_f = str(payload.get("status") or "").strip().lower()
    items = [p for p in q if (not status_f or p.get("status") == status_f)]
    # Aktives Voting ZUERST (Kandidaten, nach Score), DANN schon produzierte
    # (now/approved) als erledigt, zuletzt verworfene. Schon gemachte Themen
    # konkurrieren NICHT mehr im Vote — sie stehen unten als ✓ produziert.
    group = {"next": 0, "proposed": 0, "queued": 0, "now": 1, "approved": 1, "rejected": 2}
    for p in items:
        if p.get("status") in ("next", "proposed", "queued"):
            base = int_param(p.get("score"), 0, 0, 100)
            pen, ref = similarity_penalty((p.get("title") or "") + " " + (p.get("query") or ""), base, config)
            p["effective_score"] = int(round(base * (1.0 - pen)))
            p["cooldown_pct"] = int(round(pen * 100))
            if ref:
                p["cooldown_wegen"] = ref
        else:
            p["effective_score"] = int_param(p.get("score"), 0, 0, 100)
            p["cooldown_pct"] = 0
    items.sort(key=lambda p: (group.get(p.get("status"), 1), -int(p.get("effective_score", 0))))
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
        cov = covered_raw(config)
        topic = (item.get("title") or "") + " " + (item.get("query") or "")
        cov.append({"topic": topic.strip(), "ts": int(time.time())})
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
    if action == "add":
        topic = str(payload.get("topic") or "").strip()
        raw = covered_raw(config)
        if topic:
            raw.append({"topic": topic, "ts": int(time.time())})
            save(config, "covered.json", raw)
        return ok({"covered": covered_list(config), "added": topic})
    return ok({"covered": covered_list(config)})


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
