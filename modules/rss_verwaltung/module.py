"""RSS-Registry & Index: Verwaltet RSS/Atom-Quellen in SQLite, ruft Feeds ab,
indexiert Items und liefert der KI nur suchbare, limitierte Auszuege.
Dient als Zwischenlayer fuer DeepDive."""

import json
import sys
import os
import uuid
import hashlib
import sqlite3
import threading
import re
import time as _time
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET
from urllib.parse import urlparse, urlunparse

# ── Pfade ──────────────────────────────────────────────────────────────────
MODUL_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(MODUL_DIR, "rss_store.sqlite3")
OLD_JSON = os.path.join(MODUL_DIR, "quellen.json")

# ── Erlaubte Werte ─────────────────────────────────────────────────────────
KATEGORIEN = [
    "Politik", "Wirtschaft", "Recht", "Technologie", "Wissenschaft",
    "Gesundheit", "Kultur", "Sport", "Bildung", "Umwelt", "International", "Lokal"
]
SERIOESITAET = ["sehr_hoch", "hoch", "mittel", "niedrig", "unbekannt"]
AUSRICHTUNG = ["links", "mitte_links", "neutral", "mitte_rechts", "rechts", "unbekannt"]
REICHWEITE = ["international", "national", "regional", "lokal", "unbekannt"]
AKTUALITAET = ["live", "taeglich", "mehrmals_woche", "woechentlich", "unregelmaessig", "unbekannt"]
SPRACHEN = ["de", "en", "fr", "es", "it", "nl", "pl", "andere"]

SERIOESITAET_RANK = {"sehr_hoch": 5, "hoch": 4, "mittel": 3, "niedrig": 2, "unbekannt": 1}

# ── MODULE-Metadaten ───────────────────────────────────────────────────────
MODULE = {
    "name": "rss_verwaltung",
    "description": "RSS-Registry & Index: Quellenverwaltung, Feed-Fetching, Item-Index, Suche fuer DeepDive",
    "version": "3.0",
    "settings": {
        "max_items_per_fetch": {"type": "number", "label": "Max Items pro Feed beim Fetch", "default": 50},
        "default_search_limit": {"type": "number", "label": "Standard-Suchlimit", "default": 20},
        "fetch_timeout_sec": {"type": "number", "label": "HTTP-Timeout (Sekunden)", "default": 15},
    },
    "tools": [
        {"name": "rss_verwaltung.hinzufuegen", "description": "Fuegt eine RSS-Quelle hinzu oder aktualisiert Dublette. JSON: {url, name, kategorie, sprache, serioesitaet, ausrichtung, reichweite, aktualitaet, tags?, notizen?}", "params": ["daten_json"]},
        {"name": "rss_verwaltung.auflisten", "description": "Listet Quellen gefiltert. JSON: {kategorie?, sprache?, ausrichtung?, reichweite?, min_serioesitaet?, sort?, aktiv?}", "params": ["filter_json"]},
        {"name": "rss_verwaltung.entfernen", "description": "Deaktiviert/loescht Quelle per ID. JSON: {id, hard_delete?}", "params": ["daten_json"]},
        {"name": "rss_verwaltung.bewerten", "description": "Metadaten einer Quelle aktualisieren. JSON: {id, serioesitaet?, ausrichtung?, reichweite?, aktualitaet?, notizen?, tags?}", "params": ["daten_json"]},
        {"name": "rss_verwaltung.fetch", "description": "Ruft Feeds ab und indexiert Items. JSON: {source_id?, kategorie?, limit_sources?, max_items_per_source?}", "params": ["daten_json"]},
        {"name": "rss_verwaltung.suche", "description": "Durchsucht indexierte Items. JSON: {query, since_hours?, kategorie?, sprache?, source_id?, tags?, limit?}", "params": ["query_json"]},
        {"name": "rss_verwaltung.fuer_deepdive", "description": "Quellen+Items fuer DeepDive. JSON: {query?, kategorie?, since_hours?, limit_sources?, limit_items?}", "params": ["anfrage_json"]},
        {"name": "rss_verwaltung.item", "description": "Detail zu einem Item per ID.", "params": ["item_id"]},
        {"name": "rss_verwaltung.stats", "description": "Statistiken: Quellen/Items/Verteilung.", "params": []},
        {"name": "rss_verwaltung.kategorien", "description": "Erlaubte Kategorien und Bewertungswerte.", "params": []},
    ],
}

# ── DB-Helfer ───────────────────────────────────────────────────────────────
_db_local = threading.local()


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_db() -> sqlite3.Connection:
    if not hasattr(_db_local, "conn") or _db_local.conn is None:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        _db_local.conn = conn
    return _db_local.conn


def _init_db():
    """Erstellt Tabellen und migriert alte JSON-Daten falls vorhanden."""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            category TEXT DEFAULT 'Politik',
            language TEXT DEFAULT 'de',
            reliability TEXT DEFAULT 'unbekannt',
            alignment TEXT DEFAULT 'unbekannt',
            reach TEXT DEFAULT 'unbekannt',
            freshness_hint TEXT DEFAULT 'unbekannt',
            tags_json TEXT DEFAULT '[]',
            notes TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            last_fetch_at_utc TEXT,
            last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            guid TEXT NOT NULL,
            url TEXT,
            title TEXT,
            summary TEXT,
            published_at_utc TEXT,
            fetched_at_utc TEXT NOT NULL,
            content_hash TEXT,
            tags_json TEXT DEFAULT '[]',
            raw_json TEXT DEFAULT '{}',
            FOREIGN KEY (source_id) REFERENCES sources(id)
        );
        CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_id);
        CREATE INDEX IF NOT EXISTS idx_items_guid ON items(guid);
        CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at_utc);
        CREATE INDEX IF NOT EXISTS idx_items_content_hash ON items(content_hash);
        CREATE INDEX IF NOT EXISTS idx_items_fetched ON items(fetched_at_utc);
        CREATE INDEX IF NOT EXISTS idx_sources_active ON sources(active);
        CREATE INDEX IF NOT EXISTS idx_sources_category ON sources(category);
    """)
    # Migration aus alter JSON-Datei
    if os.path.exists(OLD_JSON):
        try:
            with open(OLD_JSON, "r", encoding="utf-8") as f:
                old = json.load(f)
            for q in old if isinstance(old, list) else old.get("quellen", []):
                try:
                    _migrate_old_source(conn, q)
                except Exception:
                    pass
            os.rename(OLD_JSON, OLD_JSON + ".migrated")
        except Exception:
            pass
    return conn


def _migrate_old_source(conn, q):
    sid = str(uuid.uuid4())[:8]
    url = (q.get("url") or "").strip()
    if not url:
        return
    name = q.get("name") or q.get("title") or url
    kat = q.get("kategorie") or q.get("category") or "Politik"
    if kat not in KATEGORIEN:
        kat = "Politik"
    lang = q.get("sprache") or q.get("language") or "de"
    if lang not in SPRACHEN:
        lang = "de"
    s = q.get("serioesitaet") or q.get("reliability") or "unbekannt"
    if s not in SERIOESITAET:
        s = "unbekannt"
    a = q.get("ausrichtung") or q.get("alignment") or "unbekannt"
    if a not in AUSRICHTUNG:
        a = "unbekannt"
    r = q.get("reichweite") or q.get("reach") or "unbekannt"
    if r not in REICHWEITE:
        r = "unbekannt"
    fh = q.get("aktualitaet") or q.get("freshness_hint") or "unbekannt"
    if fh not in AKTUALITAET:
        fh = "unbekannt"
    tags = json.dumps(q.get("tags") or [])
    notes = q.get("notizen") or q.get("notes") or ""
    now = _now_utc()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sources(id,url,name,category,language,reliability,alignment,reach,freshness_hint,tags_json,notes,active,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            [sid, url, name, kat, lang, s, a, r, fh, tags, notes, now, now])
    except Exception:
        pass


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    p = urlparse(url)
    return urlunparse((p.scheme.lower(), p.hostname.lower() if p.hostname else "", p.path or "/", p.params, p.query, ""))


def _parse_feed_xml(xml_text: str) -> list:
    """Parst RSS 2.0 / Atom und liefert Liste von Item-Dicts."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    # Atom (xmlns="http://www.w3.org/2005/Atom")
    ns_atom = "http://www.w3.org/2005/Atom"
    is_atom = root.tag == "{%s}feed" % ns_atom or root.tag == "feed"
    # RSS 2.0
    is_rss = root.tag in ("rss", "RDF") or root.find("channel") is not None

    if is_atom:
        for entry in root.findall("{%s}entry" % ns_atom) or root.findall("entry"):
            item = _parse_atom_entry(entry, ns_atom)
            if item.get("guid") or item.get("url"):
                items.append(item)
        if not items:
            for entry in root.findall(".//entry") or root.findall(".//{%s}entry" % ns_atom):
                item = _parse_atom_entry(entry, ns_atom)
                if item.get("guid") or item.get("url"):
                    items.append(item)
    elif is_rss:
        channel = root.find("channel")
        if channel is None:
            channel = root
        for i in channel.findall("item"):
            item = _parse_rss_item(i)
            if item.get("guid") or item.get("url"):
                items.append(item)
        if not items:
            for i in root.findall(".//item"):
                item = _parse_rss_item(i)
                if item.get("guid") or item.get("url"):
                    items.append(item)
    return items


def _parse_atom_entry(entry, ns):
    def _t(tag):
        e = entry.find("{%s}%s" % (ns, tag)) or entry.find(tag)
        return (e.text or "").strip() if e is not None and e.text else ""

    def _link():
        for l in entry.findall("{%s}link" % ns) or entry.findall("link"):
            rel = l.get("rel", "alternate")
            href = l.get("href", "")
            if rel == "alternate" and href:
                return href
        for l in entry.findall("{%s}link" % ns) or entry.findall("link"):
            href = l.get("href", "")
            if href:
                return href
        return ""

    guid = _t("id")
    url = _link()
    title = _t("title")
    summary = _t("summary") or _t("content") or ""
    published = _t("published") or _t("updated") or ""
    return {"guid": guid, "url": url, "title": title, "summary": summary[:2000], "published": published}


def _parse_rss_item(item):
    def _t(tag):
        e = item.find(tag)
        return (e.text or "").strip() if e is not None and e.text else ""

    guid = _t("guid") or _t("link")
    url = _t("link")
    title = _t("title")
    summary = _t("description") or ""
    published = _t("pubDate") or _t("dc:date") or ""
    return {"guid": guid, "url": url, "title": title, "summary": summary[:2000], "published": published}


def _parse_date(s: str) -> str:
    """Versuch, diverse Datumsformate in UTC-ISO zu wandeln."""
    if not s:
        return ""
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",   # RFC 2822
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return ""


def _content_hash(title: str, summary: str, url: str) -> str:
    raw = (title or "") + "|" + (summary or "")[:500] + "|" + (url or "")
    return hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()


def _score_item(row, query_tokens: list) -> float:
    """Einfacher Score: Query-Match + Aktualitaet + Serioesitaet."""
    score = 0.0
    title = (row["title"] or "").lower()
    summary = (row["summary"] or "").lower()
    tags = (row["tags_json"] or "[]").lower()
    reliability = row.get("reliability", "unbekannt")
    published = row.get("published_at_utc") or ""

    for tok in query_tokens:
        t = tok.lower()
        if t in title:
            score += 3.0
        elif t in summary:
            score += 1.5
        elif t in tags:
            score += 2.0

    # Serioesitaetsbonus
    score += SERIOESITAET_RANK.get(reliability, 1) * 0.5

    # Aktualitaetsbonus (jünger = höher)
    if published:
        try:
            pub_dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            hours_ago = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
            if hours_ago < 1:
                score += 3.0
            elif hours_ago < 6:
                score += 2.0
            elif hours_ago < 24:
                score += 1.0
            elif hours_ago < 72:
                score += 0.5
        except Exception:
            pass

    return score


# ── Tool-Implementierungen ──────────────────────────────────────────────────

def _hinzufuegen(daten_json: str) -> str:
    try:
        d = json.loads(daten_json)
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    url = _normalize_url(d.get("url", ""))
    if not url or not url.startswith("http"):
        return "FEHLER: url ist Pflichtfeld."
    name = (d.get("name") or "").strip()
    if not name:
        return "FEHLER: name ist Pflichtfeld."
    kat = d.get("kategorie") or d.get("category") or "Politik"
    if kat not in KATEGORIEN:
        return f"FEHLER: Unbekannte Kategorie '{kat}'. Erlaubt: {KATEGORIEN}"
    lang = d.get("sprache") or d.get("language") or "de"
    if lang not in SPRACHEN:
        return f"FEHLER: Unbekannte Sprache '{lang}'. Erlaubt: {SPRACHEN}"
    s = d.get("serioesitaet") or d.get("reliability") or "unbekannt"
    if s not in SERIOESITAET:
        return f"FEHLER: Unbekannte Serioesitaet '{s}'. Erlaubt: {SERIOESITAET}"
    a = d.get("ausrichtung") or d.get("alignment") or "unbekannt"
    if a not in AUSRICHTUNG:
        return f"FEHLER: Unbekannte Ausrichtung '{a}'. Erlaubt: {AUSRICHTUNG}"
    r = d.get("reichweite") or d.get("reach") or "unbekannt"
    if r not in REICHWEITE:
        return f"FEHLER: Unbekannte Reichweite '{r}'. Erlaubt: {REICHWEITE}"
    fh = d.get("aktualitaet") or d.get("freshness_hint") or "unbekannt"
    if fh not in AKTUALITAET:
        return f"FEHLER: Unbekannte Aktualitaet '{fh}'. Erlaubt: {AKTUALITAET}"
    tags = json.dumps(list(set(d.get("tags") or [])))
    notes = d.get("notizen") or d.get("notes") or ""
    conn = _get_db()
    now = _now_utc()
    # Prüfen ob URL schon existiert
    cur = conn.execute("SELECT id FROM sources WHERE url=?", [url])
    existing = cur.fetchone()
    if existing:
        sid = existing["id"]
        conn.execute(
            "UPDATE sources SET name=?,category=?,language=?,reliability=?,alignment=?,reach=?,freshness_hint=?,tags_json=?,notes=?,updated_at_utc=? WHERE id=?",
            [name, kat, lang, s, a, r, fh, tags, notes, now, sid])
        conn.commit()
        return f"OK: Quelle aktualisiert: [{sid}] {name} ({url})"
    sid = str(uuid.uuid4())[:8]
    conn.execute(
        "INSERT INTO sources(id,url,name,category,language,reliability,alignment,reach,freshness_hint,tags_json,notes,active,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        [sid, url, name, kat, lang, s, a, r, fh, tags, notes, now, now])
    conn.commit()
    return f"OK: Quelle hinzugefuegt: [{sid}] {name} ({url})"


def _auflisten(filter_json: str) -> str:
    try:
        f = json.loads(filter_json) if filter_json else {}
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    conn = _get_db()
    wheres = []
    params = []
    if f.get("kategorie") or f.get("category"):
        wheres.append("category=?")
        params.append(f.get("kategorie") or f.get("category"))
    if f.get("sprache") or f.get("language"):
        wheres.append("language=?")
        params.append(f.get("sprache") or f.get("language"))
    if f.get("ausrichtung") or f.get("alignment"):
        wheres.append("alignment=?")
        params.append(f.get("ausrichtung") or f.get("alignment"))
    if f.get("reichweite") or f.get("reach"):
        wheres.append("reach=?")
        params.append(f.get("reichweite") or f.get("reach"))
    if f.get("min_serioesitaet"):
        min_s = f["min_serioesitaet"]
        allowed = SERIOESITAET[SERIOESITAET.index(min_s):] if min_s in SERIOESITAET else []
        if allowed:
            wheres.append("reliability IN (%s)" % ",".join("?" * len(allowed)))
            params.extend(allowed)
    if "aktiv" in f or "active" in f:
        wheres.append("active=?")
        params.append(1 if f.get("aktiv", f.get("active", 1)) else 0)
    else:
        wheres.append("active=1")
    sort = f.get("sort", "name")
    order = "name ASC"
    if sort == "serioesitaet":
        order = "CASE reliability " + " ".join(
            f"WHEN '{v}' THEN {i}" for i, v in enumerate(reversed(SERIOESITAET))) + " END DESC"
    elif sort == "aktualisiert":
        order = "updated_at_utc DESC"
    elif sort == "kategorie":
        order = "category ASC, name ASC"
    sql = "SELECT * FROM sources WHERE " + (" AND ".join(wheres) if wheres else "1=1") + " ORDER BY " + order + " LIMIT 50"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return "Keine Quellen gefunden."
    lines = [f"{len(rows)} Quelle(n):"]
    for r in rows:
        lines.append(f"[{r['id']}] {r['name']} | Kat: {r['category']} | {r['language']} | Serioes: {r['reliability']} | Ausr: {r['alignment']} | Reichw: {r['reach']} | Aktiv: {r['freshness_hint']} | {'AKTIV' if r['active'] else 'INAKTIV'}")
        if r["notes"]:
            lines.append(f"    Notiz: {r['notes'][:120]}")
        lines.append(f"    URL: {r['url']}")
    return "\n".join(lines)


def _entfernen(daten_json: str) -> str:
    try:
        d = json.loads(daten_json)
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    sid = d.get("id", "")
    if not sid:
        return "FEHLER: id erforderlich."
    conn = _get_db()
    if d.get("hard_delete"):
        conn.execute("DELETE FROM items WHERE source_id=?", [sid])
        conn.execute("DELETE FROM sources WHERE id=?", [sid])
        conn.commit()
        return f"OK: Quelle [{sid}] hart geloescht (inkl. Items)."
    else:
        conn.execute("UPDATE sources SET active=0, updated_at_utc=? WHERE id=?", [_now_utc(), sid])
        conn.commit()
        return f"OK: Quelle [{sid}] deaktiviert."


def _bewerten(daten_json: str) -> str:
    try:
        d = json.loads(daten_json)
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    sid = d.get("id", "")
    if not sid:
        return "FEHLER: id erforderlich."
    conn = _get_db()
    cur = conn.execute("SELECT * FROM sources WHERE id=?", [sid])
    src = cur.fetchone()
    if not src:
        return f"FEHLER: Keine Quelle mit ID [{sid}]."
    updates = {}
    for field, allowed in [("serioesitaet", SERIOESITAET), ("reliability", SERIOESITAET),
                            ("ausrichtung", AUSRICHTUNG), ("alignment", AUSRICHTUNG),
                            ("reichweite", REICHWEITE), ("reach", REICHWEITE),
                            ("aktualitaet", AKTUALITAET), ("freshness_hint", AKTUALITAET)]:
        if field in d and d[field] in allowed:
            col = "reliability" if field in ("serioesitaet", "reliability") else \
                  "alignment" if field in ("ausrichtung", "alignment") else \
                  "reach" if field in ("reichweite", "reach") else "freshness_hint"
            updates[col] = d[field]
    if "tags" in d:
        updates["tags_json"] = json.dumps(list(set(d["tags"])))
    if "notizen" in d or "notes" in d:
        updates["notes"] = d.get("notizen") or d.get("notes") or ""
    if not updates:
        return "FEHLER: Keine gueltigen Bewertungsfelder angegeben."
    updates["updated_at_utc"] = _now_utc()
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [sid]
    conn.execute(f"UPDATE sources SET {sets} WHERE id=?", vals)
    conn.commit()
    return f"OK: Quelle [{sid}] ({src['name']}) bewertet."


def _fetch(daten_json: str) -> str:
    try:
        d = json.loads(daten_json) if daten_json else {}
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    conn = _get_db()
    sid = d.get("source_id")
    kat = d.get("kategorie") or d.get("category")
    limit_src = int(d.get("limit_sources", 10))
    max_items = int(d.get("max_items_per_source", 50))

    wheres = ["active=1"]
    params = []
    if sid:
        wheres.append("id=?")
        params.append(sid)
    if kat:
        wheres.append("category=?")
        params.append(kat)
    sql = "SELECT * FROM sources WHERE " + " AND ".join(wheres) + " LIMIT ?"
    params.append(limit_src)
    sources = conn.execute(sql, params).fetchall()

    if not sources:
        return "Keine passenden aktiven Quellen zum Abrufen gefunden."

    total_new = 0
    total_errors = 0
    output_lines = [f"Fetch: {len(sources)} Quelle(n) werden abgerufen..."]

    for src in sources:
        try:
            items = _fetch_single_feed(src["url"])
            new_for_src = 0
            now = _now_utc()
            for item in items[:max_items]:
                guid = (item.get("guid") or item.get("url") or "").strip()
                if not guid:
                    continue
                url = (item.get("url") or "").strip()
                title = (item.get("title") or "").strip()
                summary = (item.get("summary") or "").strip()
                pub = _parse_date(item.get("published") or "")
                chash = _content_hash(title, summary, url)
                # Dedup via guid
                cur = conn.execute("SELECT id FROM items WHERE guid=? AND source_id=?", [guid[:500], src["id"]])
                if cur.fetchone():
                    # Update if newer content?
                    continue
                # Dedup via content_hash
                cur = conn.execute("SELECT id FROM items WHERE content_hash=? AND source_id=?", [chash, src["id"]])
                if cur.fetchone():
                    continue
                iid = str(uuid.uuid4())[:8]
                try:
                    conn.execute(
                        "INSERT INTO items(id,source_id,guid,url,title,summary,published_at_utc,fetched_at_utc,content_hash,tags_json,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        [iid, src["id"], guid[:500], url[:2000], title[:1000], summary[:3000], pub, now, chash, src["tags_json"] or "[]", json.dumps(item)]
                    )
                    new_for_src += 1
                except sqlite3.IntegrityError:
                    pass
            conn.execute("UPDATE sources SET last_fetch_at_utc=?, last_error=NULL, updated_at_utc=? WHERE id=?", [now, now, src["id"]])
            total_new += new_for_src
            output_lines.append(f"  [{src['id']}] {src['name']}: {new_for_src} neue Items")
        except Exception as e:
            total_errors += 1
            err = str(e)[:200]
            conn.execute("UPDATE sources SET last_error=?, updated_at_utc=? WHERE id=?", [err, _now_utc(), src["id"]])
            output_lines.append(f"  [{src['id']}] {src['name']}: FEHLER - {err}")

    conn.commit()
    output_lines.append(f"\nGesamt: {total_new} neue Items, {total_errors} Fehler")
    return "\n".join(output_lines)


def _fetch_single_feed(feed_url: str) -> list:
    """Holt einen Feed per HTTP und parst ihn."""
    from urllib.request import Request, urlopen
    from urllib.error import URLError
    import ssl
    ctx = ssl.create_default_context()
    req = Request(feed_url, headers={"User-Agent": "RSS-Index/1.0", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    try:
        resp = urlopen(req, timeout=15, context=ctx)
        content = resp.read()
        # Encoding
        content_type = resp.headers.get("Content-Type", "")
        encoding = "utf-8"
        if "charset=" in content_type:
            encoding = content_type.split("charset=")[-1].split(";")[0].strip()
        try:
            text = content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            text = content.decode("utf-8", errors="replace")
        return _parse_feed_xml(text)
    except URLError as e:
        raise RuntimeError(f"HTTP-Fehler: {e}")
    except Exception:
        raise


def _suche(query_json: str) -> str:
    try:
        d = json.loads(query_json)
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    q = (d.get("query") or "").strip()
    if not q:
        return "FEHLER: query ist erforderlich."
    tokens = [t for t in re.split(r"\s+", q) if t]
    limit = int(d.get("limit", 20))
    since_h = d.get("since_hours") or d.get("since")
    kat = d.get("kategorie") or d.get("category")
    lang = d.get("sprache") or d.get("language")
    sid = d.get("source_id")
    tags = d.get("tags")

    conn = _get_db()
    wheres = ["s.active=1"]
    params = []
    if since_h is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(since_h))).strftime("%Y-%m-%dT%H:%M:%SZ")
        wheres.append("i.published_at_utc >= ?")
        params.append(cutoff)
    if kat:
        wheres.append("s.category = ?")
        params.append(kat)
    if lang:
        wheres.append("s.language = ?")
        params.append(lang)
    if sid:
        wheres.append("i.source_id = ?")
        params.append(sid)
    # Suche: LIKE auf title/summary für jedes Token
    like_clauses = []
    for tok in tokens[:5]:
        like_clauses.append("(i.title LIKE ? OR i.summary LIKE ?)")
        params.extend([f"%{tok}%", f"%{tok}%"])
    if like_clauses:
        wheres.append("(" + " OR ".join(like_clauses) + ")")

    sql = """
        SELECT i.*, s.name as source_name, s.url as source_url, s.category, s.reliability, s.language
        FROM items i JOIN sources s ON i.source_id = s.id
        WHERE """ + " AND ".join(wheres) + """
        ORDER BY i.published_at_utc DESC
        LIMIT 500
    """
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        return f"RSS_SEARCH\nquery: {q}\nresults: 0\n(Keine Treffer)"

    # Score + sortieren
    scored = [(r, _score_item(r, tokens)) for r in rows]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:limit]

    lines = [f"RSS_SEARCH", f"query: {q}", f"results: {len(top)}"]
    for r, score in top:
        pub = r["published_at_utc"] or "?"
        age = ""
        if pub != "?":
            try:
                dt = datetime.strptime(pub, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                age = f"{h:.0f}h ago"
            except Exception:
                age = "?"
        summary = (r["summary"] or "")[:200]
        lines.append(
            f"[{r['id']}] {r['source_name']} | {r['category']} | reliab:{r['reliability']} | {age}\n"
            f"    title: {r['title']}\n"
            f"    url: {r['url']}\n"
            f"    snippet: {summary}"
        )
    return "\n".join(lines)


def _fuer_deepdive(anfrage_json: str) -> str:
    try:
        d = json.loads(anfrage_json)
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    q = (d.get("query") or "").strip()
    kat = d.get("kategorie") or d.get("category")
    since_h = d.get("since_hours") or d.get("since")
    limit_src = int(d.get("limit_sources", 5))
    limit_items = int(d.get("limit_items", 10))

    conn = _get_db()
    # Passende Quellen
    wheres = ["active=1"]
    params = []
    if kat:
        wheres.append("category=?")
        params.append(kat)
    sources = conn.execute(
        "SELECT * FROM sources WHERE " + " AND ".join(wheres) + " ORDER BY "
        "CASE reliability WHEN 'sehr_hoch' THEN 0 WHEN 'hoch' THEN 1 WHEN 'mittel' THEN 2 ELSE 3 END LIMIT ?",
        params + [limit_src]
    ).fetchall()

    # Prüfen ob Index frisch ist
    item_count = conn.execute("SELECT COUNT(*) as cnt FROM items").fetchone()["cnt"]
    last_fetch = conn.execute("SELECT MAX(last_fetch_at_utc) as lf FROM sources WHERE active=1").fetchone()["lf"]
    index_stale = (item_count == 0) or (last_fetch is None)
    if last_fetch:
        try:
            lf_dt = datetime.strptime(last_fetch, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - lf_dt).total_seconds() > 86400:
                index_stale = True
        except Exception:
            pass

    out = []
    out.append("RSS_DEEPDIVE")
    out.append(f"query: {q or '(keine)'}")
    out.append(f"category: {kat or '(alle)'}")
    out.append(f"index_stale: {str(index_stale).lower()}")
    if index_stale:
        out.append("HINWEIS: Index ist leer oder veraltet. Empfehle rss_verwaltung.fetch auszufuehren.")

    if sources:
        out.append(f"\nPassende Quellen ({len(sources)}):")
        for s in sources:
            out.append(f"  [{s['id']}] {s['name']} | {s['category']} | reliab:{s['reliability']} | align:{s['alignment']} | {s['url']}")

    # Items suchen wenn query oder kategorie
    if q or kat:
        tokens = [t for t in re.split(r"\s+", q) if t] if q else []
        i_wheres = ["s.active=1"]
        i_params = []
        if since_h is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(since_h))).strftime("%Y-%m-%dT%H:%M:%SZ")
            i_wheres.append("i.published_at_utc >= ?")
            i_params.append(cutoff)
        if kat:
            i_wheres.append("s.category = ?")
            i_params.append(kat)
        # Source IDs einschränken
        if sources:
            sids = [s["id"] for s in sources]
            i_wheres.append("i.source_id IN (%s)" % ",".join("?" * len(sids)))
            i_params.extend(sids)
        like_clauses = []
        for tok in tokens[:5]:
            like_clauses.append("(i.title LIKE ? OR i.summary LIKE ?)")
            i_params.extend([f"%{tok}%", f"%{tok}%"])
        if like_clauses:
            i_wheres.append("(" + " OR ".join(like_clauses) + ")")

        sql = """
            SELECT i.*, s.name as source_name, s.url as source_url, s.category, s.reliability
            FROM items i JOIN sources s ON i.source_id = s.id
            WHERE """ + " AND ".join(i_wheres) + """
            ORDER BY i.published_at_utc DESC LIMIT 200
        """
        rows = conn.execute(sql, i_params).fetchall()
        if rows:
            scored = [(r, _score_item(r, tokens)) for r in rows]
            scored.sort(key=lambda x: x[1], reverse=True)
            top = scored[:limit_items]
            out.append(f"\nTop Items ({len(top)}):")
            for r, score in top:
                pub = r["published_at_utc"] or "?"
                age = ""
                if pub != "?":
                    try:
                        dt = datetime.strptime(pub, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        age = f"{(datetime.now(timezone.utc)-dt).total_seconds()/3600:.0f}h ago"
                    except Exception:
                        age = "?"
                out.append(
                    f"[{r['id']}] {r['source_name']} | {r['category']} | reliab:{r['reliability']} | {age}\n"
                    f"    title: {r['title']}\n"
                    f"    url: {r['url']}"
                )
        else:
            out.append("\nTop Items: Keine passenden Items gefunden.")

    return "\n".join(out)


def _item(item_id: str) -> str:
    conn = _get_db()
    row = conn.execute("""
        SELECT i.*, s.name as source_name, s.url as source_url, s.category, s.reliability, s.alignment
        FROM items i JOIN sources s ON i.source_id = s.id
        WHERE i.id=?
    """, [item_id]).fetchone()
    if not row:
        return f"Kein Item mit ID [{item_id}]."
    pub = row["published_at_utc"] or "?"
    return (
        f"ITEM [{row['id']}]\n"
        f"source: {row['source_name']} [{row['source_id']}]\n"
        f"category: {row['category']} | reliability: {row['reliability']} | alignment: {row['alignment']}\n"
        f"title: {row['title']}\n"
        f"url: {row['url']}\n"
        f"published: {pub}\n"
        f"fetched: {row['fetched_at_utc']}\n"
        f"summary: {(row['summary'] or '')[:500]}"
    )


def _stats() -> str:
    conn = _get_db()
    total_src = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    active_src = conn.execute("SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0]
    total_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    last_fetch = conn.execute("SELECT MAX(last_fetch_at_utc) FROM sources WHERE active=1").fetchone()[0] or "nie"
    error_src = conn.execute("SELECT COUNT(*) FROM sources WHERE last_error IS NOT NULL AND active=1").fetchone()[0]

    lines = [
        f"=== RSS-Verwaltung Stats ===",
        f"Quellen gesamt: {total_src}",
        f"Quellen aktiv: {active_src}",
        f"Quellen mit Fehlern: {error_src}",
        f"Indexierte Items: {total_items}",
        f"Letzter Fetch: {last_fetch}",
        "",
        "--- Nach Kategorie ---",
    ]
    for row in conn.execute("SELECT category, COUNT(*) as cnt FROM sources WHERE active=1 GROUP BY category ORDER BY cnt DESC"):
        lines.append(f"  {row['category']}: {row['cnt']}")
    lines.append("")
    lines.append("--- Nach Serioesitaet ---")
    for row in conn.execute("SELECT reliability, COUNT(*) as cnt FROM sources WHERE active=1 GROUP BY reliability ORDER BY cnt DESC"):
        lines.append(f"  {row['reliability']}: {row['cnt']}")
    lines.append("")
    lines.append("--- Nach Sprache ---")
    for row in conn.execute("SELECT language, COUNT(*) as cnt FROM sources WHERE active=1 GROUP BY language ORDER BY cnt DESC"):
        lines.append(f"  {row['language']}: {row['cnt']}")
    return "\n".join(lines)


def _kategorien() -> str:
    return (
        "=== Erlaubte Werte ===\n\n"
        f"Kategorien: {', '.join(KATEGORIEN)}\n"
        f"Serioesitaet: {', '.join(SERIOESITAET)} (Ranking: sehr_hoch=5 ... unbekannt=1)\n"
        f"Ausrichtung: {', '.join(AUSRICHTUNG)}\n"
        f"Reichweite: {', '.join(REICHWEITE)}\n"
        f"Aktualitaet: {', '.join(AKTUALITAET)}\n"
        f"Sprachen: {', '.join(SPRACHEN)}\n"
    )


# ── Tool-Dispatcher ─────────────────────────────────────────────────────────

def handle_tool(tool_name: str, params: list, config: dict) -> dict:
    """Dispatcher für alle Tools. params ist immer eine Liste."""
    _init_db()
    try:
        if tool_name == "rss_verwaltung.hinzufuegen":
            data = _hinzufuegen(params[0] if params else "")
        elif tool_name == "rss_verwaltung.auflisten":
            data = _auflisten(params[0] if params else "{}")
        elif tool_name == "rss_verwaltung.entfernen":
            data = _entfernen(params[0] if params else "")
        elif tool_name == "rss_verwaltung.bewerten":
            data = _bewerten(params[0] if params else "")
        elif tool_name == "rss_verwaltung.fetch":
            data = _fetch(params[0] if params else "{}")
        elif tool_name == "rss_verwaltung.suche":
            data = _suche(params[0] if params else "")
        elif tool_name == "rss_verwaltung.fuer_deepdive":
            data = _fuer_deepdive(params[0] if params else "{}")
        elif tool_name == "rss_verwaltung.item":
            data = _item(params[0] if params else "")
        elif tool_name == "rss_verwaltung.stats":
            data = _stats()
        elif tool_name == "rss_verwaltung.kategorien":
            data = _kategorien()
        else:
            return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "data": f"Fehler: {e}"}


# ── stdin/stdout Interface ──────────────────────────────────────────────────
if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if req.get("action") == "describe":
            print(json.dumps(MODULE), flush=True)
        elif req.get("action") == "handle_tool":
            result = handle_tool(req["tool"], req.get("params", []), req.get("config", {}))
            print(json.dumps(result), flush=True)