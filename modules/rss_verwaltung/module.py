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
import html
import email.utils
import shutil
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
QUERY_STOPWORDS = {
    "aktuell", "aktuelle", "aktuelles", "news", "nachricht", "nachrichten",
    "info", "infos", "information", "informationen", "suche", "such", "finde",
    "alles", "alle", "was", "ueber", "über", "zum", "zur", "der", "die", "das",
}
QUERY_SYNONYMS = {
    "energiepolitik": ["energie", "energiewende", "strom", "gas", "netze", "energiesteuer", "klima", "bmwk", "wirtschaftsministerium"],
    "energie": ["energiepolitik", "strom", "gas", "netze", "energiewende"],
    "spritpreise": ["sprit", "benzin", "diesel", "kraftstoff", "tanken", "energiesteuer"],
    "ki": ["ai", "kuenstliche", "künstliche", "intelligenz", "openai", "anthropic", "google", "gemini", "chatgpt"],
    "ai": ["ki", "artificial", "intelligence", "openai", "anthropic", "google", "gemini"],
    "openai": ["chatgpt", "gpt", "sam", "altman"],
    "anthropic": ["claude"],
    "google": ["gemini", "deepmind"],
    "deutschland": ["regierung", "bundesregierung", "bundestag", "berlin", "deutsche", "deutscher", "deutschen"],
    "usa": ["trump", "washington", "united", "states"],
    "eu": ["europa", "european", "bruessel", "brüssel", "kommission", "parlament"],
}

# ── MODULE-Metadaten ───────────────────────────────────────────────────────
MODULE = {
    "name": "rss_verwaltung",
    "description": "RSS-Registry & Index: Quellenverwaltung, Feed-Fetching, Item-Index, Suche fuer DeepDive",
    "version": "3.2",
    "settings": {
        "max_items_per_fetch": {"type": "number", "label": "Max Items pro Feed beim Fetch", "default": 50},
        "default_search_limit": {"type": "number", "label": "Standard-Suchlimit", "default": 20},
        "fetch_timeout_sec": {"type": "number", "label": "HTTP-Timeout (Sekunden)", "default": 15},
        "auto_rag_ingest": {"type": "bool", "label": "Neue RSS-Items automatisch in RAG speichern", "default": True},
        "rag_ingest_limit": {"type": "number", "label": "Max RSS-News pro Fetch in RAG", "default": 120},
        "default_rag_pool": {"type": "string", "label": "Fallback RAG-Pool", "default": "DeepDive"},
    },
    "tools": [
        {"name": "rss_verwaltung.hinzufuegen", "description": "Fuegt eine RSS-Quelle hinzu oder aktualisiert Dublette. JSON: {url, name, kategorie, sprache, serioesitaet, ausrichtung, reichweite, aktualitaet, tags?, notizen?}", "params": ["daten_json"]},
        {"name": "rss_verwaltung.auflisten", "description": "Listet Quellen gefiltert. JSON: {kategorie?, sprache?, ausrichtung?, reichweite?, min_serioesitaet?, sort?, aktiv?}", "params": ["filter_json"]},
        {"name": "rss_verwaltung.entfernen", "description": "Deaktiviert/loescht Quelle per ID. JSON: {id, hard_delete?}", "params": ["daten_json"]},
        {"name": "rss_verwaltung.bewerten", "description": "Metadaten einer Quelle aktualisieren. JSON: {id, serioesitaet?, ausrichtung?, reichweite?, aktualitaet?, notizen?, tags?}", "params": ["daten_json"]},
        {"name": "rss_verwaltung.fetch", "description": "Ruft Feeds ab und indexiert Items. JSON: {source_id?, kategorie?, limit_sources?, max_items_per_source?}", "params": ["daten_json"]},
        {"name": "rss_verwaltung.suche", "description": "Durchsucht indexierte Items. JSON: {query, since_hours?, kategorie?, sprache?, source_id?, tags?, limit?}", "params": ["query_json"]},
        {"name": "rss_verwaltung.fuer_deepdive", "description": "Quellen+Items fuer DeepDive. JSON: {query?, kategorie?, since_hours?, limit_sources?, limit_items?}", "params": ["anfrage_json"]},
        {"name": "rss_verwaltung.ingest_rag", "description": "Schreibt sortierte RSS-News als bewertete Notizen in den RAG-Pool. JSON: {query?, since_hours?, kategorie?, source_id?, limit?, force?}", "params": ["daten_json"]},
        {"name": "rss_verwaltung.item", "description": "Detail zu einem Item per ID.", "params": ["item_id"]},
        {"name": "rss_verwaltung.stats", "description": "Statistiken: Quellen/Items/Verteilung.", "params": []},
        {"name": "rss_verwaltung.kategorien", "description": "Erlaubte Kategorien und Bewertungswerte.", "params": []},
    ],
}

# ── DB-Helfer ───────────────────────────────────────────────────────────────
_db_local = threading.local()


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db_file(config: dict | None = None) -> str:
    config = config or {}
    explicit = str(config.get("rss_db_path") or "").strip()
    if explicit:
        return explicit
    data_dir = str(config.get("data_dir") or "").strip()
    if data_dir:
        return os.path.join(data_dir, "rss", "rss_store.sqlite3")
    return DB_FILE


def _migrate_legacy_db(target: str):
    legacy = os.path.abspath(DB_FILE)
    target = os.path.abspath(target)
    if target == legacy or not os.path.exists(legacy) or os.path.exists(target):
        return
    target_dir = os.path.dirname(target)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)
    try:
        src = sqlite3.connect(legacy)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except Exception:
        shutil.copy2(legacy, target)


def _get_db(config: dict | None = None) -> sqlite3.Connection:
    path = _db_file(config)
    if not hasattr(_db_local, "conn") or _db_local.conn is None or getattr(_db_local, "path", None) != path:
        if getattr(_db_local, "conn", None) is not None:
            try:
                _db_local.conn.close()
            except Exception as _e:
                sys.stderr.write("[rss_verwaltung] uebersprungener Fehler: %r\n" % (_e,))
        path_dir = os.path.dirname(path)
        if path_dir:
            os.makedirs(path_dir, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        _db_local.conn = conn
        _db_local.path = path
    return _db_local.conn


def _init_db(config: dict | None = None):
    """Erstellt Tabellen und migriert alte JSON-Daten falls vorhanden."""
    target = _db_file(config)
    _migrate_legacy_db(target)
    conn = _get_db(config)
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
            rag_id TEXT,
            rag_pool TEXT,
            rag_stored_at_utc TEXT,
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
    _ensure_column(conn, "items", "rag_id", "TEXT")
    _ensure_column(conn, "items", "rag_pool", "TEXT")
    _ensure_column(conn, "items", "rag_stored_at_utc", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_rag ON items(rag_pool, rag_id)")
    # Migration aus alter JSON-Datei
    if os.path.exists(OLD_JSON):
        try:
            with open(OLD_JSON, "r", encoding="utf-8") as f:
                old = json.load(f)
            for q in old if isinstance(old, list) else old.get("quellen", []):
                try:
                    _migrate_old_source(conn, q)
                except Exception as _e:
                    sys.stderr.write("[rss_verwaltung] uebersprungener Fehler: %r\n" % (_e,))
            os.rename(OLD_JSON, OLD_JSON + ".migrated")
        except Exception as _e:
            sys.stderr.write("[rss_verwaltung] uebersprungener Fehler: %r\n" % (_e,))
    return conn


def _ensure_column(conn, table: str, column: str, definition: str):
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
    except Exception as _e:
        sys.stderr.write("[rss_verwaltung] uebersprungener Fehler: %r\n" % (_e,))


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
    title = _clean_text(_t("title"), 1000)
    summary = _clean_text(_t("summary") or _t("content") or "", 2000)
    published = _t("published") or _t("updated") or ""
    return {"guid": guid, "url": url, "title": title, "summary": summary[:2000], "published": published}


def _parse_rss_item(item):
    def _t(tag):
        e = item.find(tag)
        return (e.text or "").strip() if e is not None and e.text else ""

    def _date():
        direct = _t("pubDate") or _t("date")
        if direct:
            return direct
        for child in list(item):
            if child.tag.lower().endswith("date") and child.text:
                return child.text.strip()
        return ""

    guid = _t("guid") or _t("link")
    url = _t("link")
    title = _clean_text(_t("title"), 1000)
    summary = _clean_text(_t("description") or "", 2000)
    published = _date()
    return {"guid": guid, "url": url, "title": title, "summary": summary[:2000], "published": published}


def _clean_text(value: str, limit: int = 2000) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _parse_date(s: str) -> str:
    """Versuch, diverse Datumsformate in UTC-ISO zu wandeln."""
    if not s:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(s.strip())
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception as _e:
        sys.stderr.write("[rss_verwaltung] uebersprungener Fehler: %r\n" % (_e,))
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
    try:
        reliability = row["reliability"] or "unbekannt"
    except (KeyError, IndexError):
        reliability = "unbekannt"
    try:
        published = row["published_at_utc"] or ""
    except (KeyError, IndexError):
        published = ""

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
        except Exception as _e:
            sys.stderr.write("[rss_verwaltung] uebersprungener Fehler: %r\n" % (_e,))

    return score


def _query_tokens(query: str) -> list:
    tokens = []
    for tok in re.split(r"[^A-Za-zÄÖÜäöüß0-9_.-]+", query or ""):
        tok = tok.strip().lower()
        if len(tok) < 3 or tok in QUERY_STOPWORDS:
            continue
        tokens.append(tok)
    return tokens[:8]


def _token_groups(query_tokens: list) -> list:
    groups = []
    for tok in query_tokens:
        group = [tok]
        group.extend(QUERY_SYNONYMS.get(tok, []))
        if tok.endswith("politik") and len(tok) > 8:
            group.append(tok[:-7])
        seen = set()
        unique = []
        for value in group:
            if value not in seen:
                seen.add(value)
                unique.append(value)
        groups.append(unique)
    return groups


def _expanded_query_tokens(query_tokens: list) -> list:
    seen = set()
    out = []
    for group in _token_groups(query_tokens):
        for tok in group:
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out[:24]


def _row_search_text(row) -> str:
    text = " ".join([
        str(row["title"] or ""),
        str(row["summary"] or ""),
        str(row["tags_json"] or ""),
        str(row["source_name"] if "source_name" in row.keys() else ""),
        str(row["category"] if "category" in row.keys() else ""),
        str(row["reliability"] if "reliability" in row.keys() else ""),
        str(row["alignment"] if "alignment" in row.keys() else ""),
        str(row["reach"] if "reach" in row.keys() else ""),
    ]).lower()
    return text


def _token_match_count(row, query_tokens: list) -> int:
    text = _row_search_text(row)
    return sum(1 for tok in query_tokens if tok in text)


def _token_group_match_count(row, token_groups: list) -> int:
    text = _row_search_text(row)
    return sum(1 for group in token_groups if any(tok in text for tok in group))


def _required_match_count(query_tokens: list) -> int:
    if not query_tokens:
        return 0
    if len(query_tokens) == 1:
        return 1
    if len(query_tokens) == 2:
        return 2
    return min(3, max(2, (len(query_tokens) + 1) // 2))


def _effective_item_time(row) -> str:
    try:
        return row["published_at_utc"] or row["fetched_at_utc"] or ""
    except Exception:
        return ""


def _age_label(iso_value: str) -> str:
    if not iso_value:
        return "age:unknown"
    try:
        dt = datetime.strptime(iso_value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        if hours < 1:
            return f"{int(hours * 60)}m ago"
        if hours < 48:
            return f"{hours:.0f}h ago"
        return f"{hours / 24:.0f}d ago"
    except Exception:
        return "age:unknown"


def _bool_param(d: dict, key: str, default: bool) -> bool:
    if key not in d:
        return default
    value = d.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on"}


def _safe_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return value.strip("._-")[:80]


def _rag_pool(config: dict, params: dict | None = None) -> str:
    params = params or {}
    pool = (
        params.get("rag_pool")
        or config.get("rag_pool")
        or config.get("default_rag_pool")
        or "DeepDive"
    )
    return _safe_id(pool) or "DeepDive"


def _rag_dir(config: dict, params: dict | None = None) -> tuple[str, str] | tuple[None, str]:
    data_dir = str(config.get("data_dir") or "").strip()
    pool = _rag_pool(config, params)
    if not data_dir:
        return None, pool
    rag_dir = os.path.join(data_dir, "rag", pool)
    os.makedirs(rag_dir, exist_ok=True)
    return rag_dir, pool


def _keywords(text: str) -> list:
    words = []
    for tok in re.split(r"[^A-Za-zÄÖÜäöüß0-9_.-]+", text or ""):
        tok = tok.strip().lower()
        if len(tok) < 3 or tok in QUERY_STOPWORDS:
            continue
        words.append(tok)
    # Reihenfolge behalten, Dubletten entfernen.
    seen = set()
    out = []
    for word in words:
        if word not in seen:
            seen.add(word)
            out.append(word)
    return out[:80]


def _row_value(row, key: str, default=""):
    try:
        if key in row.keys():
            value = row[key]
            return default if value is None else value
    except Exception as _e:
        sys.stderr.write("[rss_verwaltung] uebersprungener Fehler: %r\n" % (_e,))
    return default


def _source_score(row) -> float:
    reliability = _row_value(row, "reliability", "unbekannt")
    reach = _row_value(row, "reach", "unbekannt")
    alignment = _row_value(row, "alignment", "unbekannt")
    score = SERIOESITAET_RANK.get(reliability, 1) * 1.2
    if reach == "international":
        score += 1.0
    elif reach == "national":
        score += 0.8
    elif reach == "regional":
        score += 0.4
    if alignment == "neutral":
        score += 0.4
    return score


def _source_query_score(row, query_tokens: list) -> float:
    if not query_tokens:
        return _source_score(row)
    groups = _token_groups(query_tokens)
    text = " ".join([
        str(_row_value(row, "name")),
        str(_row_value(row, "category")),
        str(_row_value(row, "tags_json")),
        str(_row_value(row, "notes")),
        str(_row_value(row, "url")),
    ]).lower()
    group_hits = sum(1 for group in groups if any(tok in text for tok in group))
    return _source_score(row) + group_hits * 3.0


def _rss_rag_score(row, query_tokens: list) -> float:
    return _score_item(row, query_tokens) + _source_score(row)


def _rss_rag_note(row, query: str, score: float) -> str:
    captured = _now_utc()
    item_time = _effective_item_time(row)
    source_url = _row_value(row, "url")
    source_feed = _row_value(row, "source_url")
    source_name = _row_value(row, "source_name")
    summary = _clean_text(_row_value(row, "summary"), 1800)
    title = _clean_text(_row_value(row, "title"), 1000)
    lines = [
        "RSS_NEWS_NOTE",
        f"captured_at_utc: {captured}",
        f"source_last_seen_utc: {_row_value(row, 'fetched_at_utc') or captured}",
        f"topic: {query or 'rss-pull'}",
        f"rss_item_id: {_row_value(row, 'id')}",
        f"rss_source_id: {_row_value(row, 'source_id')}",
        f"source_url: {source_url}",
        f"source_title: {title}",
        f"source_feed_url: {source_feed}",
        f"source_name: {source_name}",
        f"source_type: rss_item",
        f"source_category: {_row_value(row, 'category')}",
        f"source_language: {_row_value(row, 'language')}",
        f"source_reliability: {_row_value(row, 'reliability', 'unbekannt')}",
        f"source_alignment: {_row_value(row, 'alignment', 'unbekannt')}",
        f"source_reach: {_row_value(row, 'reach', 'unbekannt')}",
        f"published_at_utc: {_row_value(row, 'published_at_utc') or '?'}",
        f"fetched_at_utc: {_row_value(row, 'fetched_at_utc') or '?'}",
        f"recency_label: {_age_label(item_time)}",
        f"rss_score: {score:.2f}",
        "deepdive_next_step: Bei Bedarf source_url mit deepdive.crawl oder browser.fetch oeffnen und vollstaendige Quellen-Notiz speichern.",
        "assessment_required: source_reliability, source_alignment, date_context, relevance, uncertainty, contradictions",
        "source_summary:",
        summary,
    ]
    return "\n".join(lines).strip()


def _write_rag_note(note: str, config: dict, params: dict | None = None) -> tuple[bool, str, str]:
    rag_dir, pool = _rag_dir(config, params)
    if not rag_dir:
        return False, "", f"RAG nicht gespeichert: data_dir fehlt (pool: {pool})."
    entry_id = str(uuid.uuid4())
    entry = {
        "id": entry_id,
        "text": note,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "keywords": _keywords(note),
        "source_url": _line_value(note, "source_url"),
        "source_title": _line_value(note, "source_title"),
        "source_type": _line_value(note, "source_type"),
        "source_reliability": _line_value(note, "source_reliability"),
        "source_alignment": _line_value(note, "source_alignment"),
        "published_at_utc": _line_value(note, "published_at_utc"),
        "recency_label": _line_value(note, "recency_label"),
    }
    path = os.path.join(rag_dir, f"{entry_id}.json")
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)
    return True, entry_id, f"Im RAG Pool '{pool}' gespeichert (id: {entry_id[:8]})"


def _line_value(text: str, key: str) -> str:
    prefix = f"{key}:"
    for line in (text or "").splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


# ── Tool-Implementierungen ──────────────────────────────────────────────────

def _hinzufuegen(daten_json: str, config: dict | None = None) -> str:
    _init_db(config)
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
    conn = _get_db(config)
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


def _auflisten(filter_json: str, config: dict | None = None) -> str:
    _init_db(config)
    try:
        f = json.loads(filter_json) if filter_json else {}
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    conn = _get_db(config)
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


def _entfernen(daten_json: str, config: dict | None = None) -> str:
    _init_db(config)
    try:
        d = json.loads(daten_json)
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    sid = d.get("id", "")
    if not sid:
        return "FEHLER: id erforderlich."
    conn = _get_db(config)
    if d.get("hard_delete"):
        conn.execute("DELETE FROM items WHERE source_id=?", [sid])
        conn.execute("DELETE FROM sources WHERE id=?", [sid])
        conn.commit()
        return f"OK: Quelle [{sid}] hart geloescht (inkl. Items)."
    else:
        conn.execute("UPDATE sources SET active=0, updated_at_utc=? WHERE id=?", [_now_utc(), sid])
        conn.commit()
        return f"OK: Quelle [{sid}] deaktiviert."


def _bewerten(daten_json: str, config: dict | None = None) -> str:
    _init_db(config)
    try:
        d = json.loads(daten_json)
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    sid = d.get("id", "")
    if not sid:
        return "FEHLER: id erforderlich."
    conn = _get_db(config)
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


def _fetch(daten_json: str, config: dict | None = None) -> str:
    config = config or {}
    _init_db(config)
    try:
        d = json.loads(daten_json) if daten_json else {}
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    conn = _get_db(config)
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
    new_item_ids = []

    for src in sources:
        try:
            items = _fetch_single_feed(src["url"], config)
            new_for_src = 0
            now = _now_utc()
            for item in items[:max_items]:
                guid = (item.get("guid") or item.get("url") or "").strip()
                if not guid:
                    continue
                url = (item.get("url") or "").strip()
                title = _clean_text((item.get("title") or "").strip(), 1000)
                summary = _clean_text((item.get("summary") or "").strip(), 3000)
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
                    new_item_ids.append(iid)
                except sqlite3.IntegrityError as _e:
                    sys.stderr.write("[rss_verwaltung] uebersprungener Fehler: %r\n" % (_e,))
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
    auto_rag = _bool_param(config, "auto_rag_ingest", True)
    auto_rag = _bool_param(d, "rag_ingest", auto_rag)
    if auto_rag and new_item_ids:
        limit = max(1, min(int(d.get("rag_ingest_limit") or config.get("rag_ingest_limit") or 120), 500))
        ingest_msg = _ingest_rag(json.dumps({
            "item_ids": new_item_ids,
            "query": d.get("query") or "",
            "limit": limit,
            "rag_pool": d.get("rag_pool"),
        }), config)
        output_lines.append("\nRAG-Ingest:")
        output_lines.extend("  " + line for line in ingest_msg.splitlines()[:12])
    return "\n".join(output_lines)


def _fetch_single_feed(feed_url: str, config: dict | None = None) -> list:
    """Holt einen Feed per HTTP und parst ihn."""
    from urllib.request import Request, urlopen
    from urllib.error import URLError
    import ssl
    ctx = ssl.create_default_context()
    timeout = int((config or {}).get("fetch_timeout_sec") or 15)
    req = Request(feed_url, headers={"User-Agent": "RSS-Index/1.0", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    try:
        resp = urlopen(req, timeout=timeout, context=ctx)
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


def _suche(query_json: str, config: dict | None = None) -> str:
    config = config or {}
    _init_db(config)
    try:
        d = json.loads(query_json)
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    q = (d.get("query") or "").strip()
    if not q:
        return "FEHLER: query ist erforderlich."
    tokens = _query_tokens(q)
    token_groups = _token_groups(tokens)
    match_tokens = _expanded_query_tokens(tokens)
    limit = int(d.get("limit", 20))
    since_h = d.get("since_hours") or d.get("since")
    kat = d.get("kategorie") or d.get("category")
    lang = d.get("sprache") or d.get("language")
    sid = d.get("source_id")
    tags = d.get("tags")

    conn = _get_db(config)
    wheres = ["s.active=1"]
    params = []
    if since_h is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(since_h))).strftime("%Y-%m-%dT%H:%M:%SZ")
        wheres.append("COALESCE(NULLIF(i.published_at_utc,''), i.fetched_at_utc) >= ?")
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
    for tok in match_tokens[:12]:
        like_clauses.append("(i.title LIKE ? OR i.summary LIKE ?)")
        params.extend([f"%{tok}%", f"%{tok}%"])
    if like_clauses:
        wheres.append("(" + " OR ".join(like_clauses) + ")")

    sql = """
        SELECT i.*, s.name as source_name, s.url as source_url, s.category, s.reliability, s.language
        FROM items i JOIN sources s ON i.source_id = s.id
        WHERE """ + " AND ".join(wheres) + """
        ORDER BY COALESCE(NULLIF(i.published_at_utc,''), i.fetched_at_utc) DESC
        LIMIT 500
    """
    rows = conn.execute(sql, params).fetchall()
    required_matches = _required_match_count(tokens)
    if required_matches:
        strict_rows = [r for r in rows if _token_group_match_count(r, token_groups) >= required_matches]
        rows = strict_rows

    if not rows:
        return f"RSS_SEARCH\nquery: {q}\nresults: 0\n(Keine Treffer)"

    # Score + sortieren
    scored = [(r, _score_item(r, match_tokens)) for r in rows]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:limit]

    lines = [f"RSS_SEARCH", f"query: {q}", f"results: {len(top)}"]
    for r, score in top:
        pub = r["published_at_utc"] or "?"
        fetched = r["fetched_at_utc"] or "?"
        age = _age_label(_effective_item_time(r))
        summary = (r["summary"] or "")[:200]
        lines.append(
            f"[{r['id']}] {r['source_name']} | {r['category']} | reliab:{r['reliability']} | {age} | score:{score:.1f}\n"
            f"    title: {r['title']}\n"
            f"    item_url: {r['url']}\n"
            f"    source_feed_url: {r['source_url']}\n"
            f"    published_at_utc: {pub}\n"
            f"    fetched_at_utc: {fetched}\n"
            f"    snippet: {summary}"
        )
    return "\n".join(lines)


def _fuer_deepdive(anfrage_json: str, config: dict | None = None) -> str:
    config = config or {}
    _init_db(config)
    try:
        d = json.loads(anfrage_json)
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."
    q = (d.get("query") or "").strip()
    kat = d.get("kategorie") or d.get("category")
    since_h = d.get("since_hours") or d.get("since")
    limit_src = max(1, min(int(d.get("limit_sources", 8)), 20))
    limit_items = max(1, min(int(d.get("limit_items", 20)), 80))
    refresh = _bool_param(d, "refresh", True)
    force_refresh = _bool_param(d, "force_refresh", False)
    stale_after_minutes = max(5, min(int(d.get("stale_after_minutes", 30)), 1440))
    max_items_per_source = max(5, min(int(d.get("max_items_per_source", 30)), 80))

    conn = _get_db(config)
    wheres = ["active=1"]
    params = []
    if kat:
        wheres.append("category=?")
        params.append(kat)
    source_scan_limit = min(max(limit_src * 4, limit_src), 100)
    sources = conn.execute(
        "SELECT * FROM sources WHERE " + " AND ".join(wheres) + " ORDER BY "
        "CASE reliability WHEN 'sehr_hoch' THEN 0 WHEN 'hoch' THEN 1 WHEN 'mittel' THEN 2 ELSE 3 END LIMIT ?",
        params + [source_scan_limit]
    ).fetchall()
    base_tokens = _query_tokens(q) if q else []
    sources = sorted(sources, key=lambda s: _source_query_score(s, base_tokens), reverse=True)[:limit_src]

    item_count = conn.execute("SELECT COUNT(*) as cnt FROM items").fetchone()["cnt"]
    last_fetch = conn.execute("SELECT MAX(last_fetch_at_utc) as lf FROM sources WHERE active=1").fetchone()["lf"]
    index_stale = (item_count == 0) or (last_fetch is None)
    if last_fetch:
        try:
            lf_dt = datetime.strptime(last_fetch, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - lf_dt).total_seconds() > stale_after_minutes * 60:
                index_stale = True
        except Exception as _e:
            sys.stderr.write("[rss_verwaltung] uebersprungener Fehler: %r\n" % (_e,))

    refresh_log = []
    if sources and refresh and (force_refresh or index_stale):
        for s in sources:
            try:
                res = _fetch(json.dumps({
                    "source_id": s["id"],
                    "limit_sources": 1,
                    "max_items_per_source": max_items_per_source,
                }), config)
                tail = res.splitlines()[-1] if res else "fetch done"
                refresh_log.append(f"[{s['id']}] {s['name']}: {tail}")
            except Exception as exc:
                refresh_log.append(f"[{s['id']}] {s['name']}: FEHLER {str(exc)[:160]}")
        sources = conn.execute(
            "SELECT * FROM sources WHERE " + " AND ".join(wheres) + " ORDER BY "
            "CASE reliability WHEN 'sehr_hoch' THEN 0 WHEN 'hoch' THEN 1 WHEN 'mittel' THEN 2 ELSE 3 END LIMIT ?",
            params + [source_scan_limit]
        ).fetchall()
        sources = sorted(sources, key=lambda s: _source_query_score(s, base_tokens), reverse=True)[:limit_src]
        item_count = conn.execute("SELECT COUNT(*) as cnt FROM items").fetchone()["cnt"]
        last_fetch = conn.execute("SELECT MAX(last_fetch_at_utc) as lf FROM sources WHERE active=1").fetchone()["lf"]
        index_stale = False

    out = []
    out.append("RSS_DEEPDIVE_PACKET")
    out.append(f"generated_at_utc: {_now_utc()}")
    out.append(f"query: {q or '(keine)'}")
    out.append(f"category: {kat or '(alle)'}")
    out.append(f"since_hours: {since_h if since_h is not None else '(none)'}")
    out.append(f"index_items_total: {item_count}")
    out.append(f"index_last_fetch_at_utc: {last_fetch or 'nie'}")
    out.append(f"index_stale: {str(index_stale).lower()}")
    out.append(f"refresh_performed: {str(bool(refresh_log)).lower()}")
    if refresh_log:
        out.append("refresh_log:")
        out.extend("  " + line for line in refresh_log[:limit_src])

    if sources:
        out.append(f"\n<quellen count=\"{len(sources)}\">")
        for s in sources:
            out.append(
                f"<quelle id=\"{s['id']}\" feed_url=\"{s['url']}\" category=\"{s['category']}\" "
                f"language=\"{s['language']}\" reliability=\"{s['reliability']}\" alignment=\"{s['alignment']}\" "
                f"reach=\"{s['reach']}\" last_fetch_at_utc=\"{s['last_fetch_at_utc'] or ''}\" "
                f"last_error=\"{(s['last_error'] or '')[:120]}\">{s['name']}</quelle>"
            )
        out.append("</quellen>")

    if q or kat:
        tokens = base_tokens
        token_groups = _token_groups(tokens)
        match_tokens = _expanded_query_tokens(tokens)
        i_wheres = ["s.active=1"]
        i_params = []
        if since_h is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(since_h))).strftime("%Y-%m-%dT%H:%M:%SZ")
            i_wheres.append("COALESCE(NULLIF(i.published_at_utc,''), i.fetched_at_utc) >= ?")
            i_params.append(cutoff)
        if kat:
            i_wheres.append("s.category = ?")
            i_params.append(kat)
        like_clauses = []
        for tok in match_tokens[:12]:
            like_clauses.append("(i.title LIKE ? OR i.summary LIKE ?)")
            i_params.extend([f"%{tok}%", f"%{tok}%"])
        if like_clauses:
            i_wheres.append("(" + " OR ".join(like_clauses) + ")")

        sql = """
            SELECT i.*, s.name as source_name, s.url as source_url, s.category, s.reliability, s.language, s.alignment, s.reach
            FROM items i JOIN sources s ON i.source_id = s.id
            WHERE """ + " AND ".join(i_wheres) + """
            ORDER BY COALESCE(NULLIF(i.published_at_utc,''), i.fetched_at_utc) DESC LIMIT 500
        """
        rows = conn.execute(sql, i_params).fetchall()
        required_matches = _required_match_count(tokens)
        if required_matches:
            strict_rows = [r for r in rows if _token_group_match_count(r, token_groups) >= required_matches]
            rows = strict_rows
        if rows:
            scored = [(r, _score_item(r, match_tokens)) for r in rows]
            scored.sort(key=lambda x: x[1], reverse=True)
            top = scored[:limit_items]
            if _bool_param(d, "rag_ingest", _bool_param(config, "auto_rag_ingest", True)):
                ingest_log = _ingest_rag(json.dumps({
                    "item_ids": [r["id"] for r, _ in top],
                    "query": q,
                    "limit": len(top),
                    "rag_pool": d.get("rag_pool"),
                }), config)
                out.append("\n<rag_ingest>")
                out.extend(ingest_log.splitlines()[:16])
                out.append("</rag_ingest>")
            out.append(f"\n<items count=\"{len(top)}\">")
            for r, score in top:
                pub = r["published_at_utc"] or "?"
                fetched = r["fetched_at_utc"] or "?"
                age = _age_label(_effective_item_time(r))
                summary = (r["summary"] or "")[:450]
                out.append(
                    f"<item id=\"{r['id']}\" source_id=\"{r['source_id']}\" source=\"{r['source_name']}\" "
                    f"category=\"{r['category']}\" reliability=\"{r['reliability']}\" age=\"{age}\" score=\"{score:.1f}\">\n"
                    f"title: {r['title']}\n"
                    f"item_url: {r['url']}\n"
                    f"source_feed_url: {r['source_url']}\n"
                    f"published_at_utc: {pub}\n"
                    f"fetched_at_utc: {fetched}\n"
                    f"summary: {summary}\n"
                    f"</item>"
                )
            out.append("</items>")
            out.append("NEXT: Fuer belastbare DeepDive-Synthese die relevanten item_url-Werte mit browser.fetch/deepdive.crawl oeffnen und danach Beobachtungen ins RAG schreiben.")
        else:
            out.append("\n<items count=\"0\">Keine passenden Items gefunden.</items>")
            out.append("NEXT: rss_verwaltung.fetch mit passenden Quellen ausfuehren oder Websuche als Fallback nutzen.")

    return "\n".join(out)


def _ingested_duplicate(conn, row, pool: str):
    url = _row_value(row, "url")
    chash = _row_value(row, "content_hash")
    if not url and not chash:
        return None
    wheres = ["rag_pool=?", "rag_id IS NOT NULL", "id<>?"]
    params = [pool, _row_value(row, "id")]
    dupe = []
    if url:
        dupe.append("url=?")
        params.append(url)
    if chash:
        dupe.append("content_hash=?")
        params.append(chash)
    if not dupe:
        return None
    sql = "SELECT rag_id, rag_stored_at_utc FROM items WHERE " + " AND ".join(wheres) + " AND (" + " OR ".join(dupe) + ") LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _select_ingest_rows(conn, d: dict, pool: str):
    q = (d.get("query") or "").strip()
    tokens = _query_tokens(q)
    token_groups = _token_groups(tokens)
    match_tokens = _expanded_query_tokens(tokens)
    limit = max(1, min(int(d.get("limit", 120)), 500))
    force = _bool_param(d, "force", False)
    item_ids = d.get("item_ids") or []
    if isinstance(item_ids, str):
        item_ids = [x.strip() for x in item_ids.split(",") if x.strip()]

    wheres = ["s.active=1"]
    params = []
    if item_ids:
        item_ids = [str(x) for x in item_ids[:500]]
        wheres.append("i.id IN (%s)" % ",".join("?" * len(item_ids)))
        params.extend(item_ids)
    elif not force:
        wheres.append("(i.rag_id IS NULL OR i.rag_pool IS NULL OR i.rag_pool<>?)")
        params.append(pool)

    since_h = d.get("since_hours") or d.get("since")
    if since_h is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(since_h))).strftime("%Y-%m-%dT%H:%M:%SZ")
        wheres.append("COALESCE(NULLIF(i.published_at_utc,''), i.fetched_at_utc) >= ?")
        params.append(cutoff)
    kat = d.get("kategorie") or d.get("category")
    if kat:
        wheres.append("s.category = ?")
        params.append(kat)
    lang = d.get("sprache") or d.get("language")
    if lang:
        wheres.append("s.language = ?")
        params.append(lang)
    sid = d.get("source_id")
    if sid:
        wheres.append("i.source_id = ?")
        params.append(sid)

    like_clauses = []
    for tok in match_tokens[:12]:
        like_clauses.append(
            "(i.title LIKE ? OR i.summary LIKE ? OR s.name LIKE ? OR s.tags_json LIKE ? "
            "OR s.category LIKE ? OR s.reliability LIKE ? OR s.alignment LIKE ? OR s.reach LIKE ?)"
        )
        params.extend([f"%{tok}%"] * 8)
    if like_clauses:
        wheres.append("(" + " OR ".join(like_clauses) + ")")

    sql = """
        SELECT i.*, s.name as source_name, s.url as source_url, s.category, s.reliability,
               s.language, s.alignment, s.reach, s.freshness_hint
        FROM items i JOIN sources s ON i.source_id = s.id
        WHERE """ + " AND ".join(wheres) + """
        ORDER BY COALESCE(NULLIF(i.published_at_utc,''), i.fetched_at_utc) DESC
        LIMIT 1000
    """
    rows = conn.execute(sql, params).fetchall()
    required_matches = _required_match_count(tokens)
    if required_matches:
        rows = [r for r in rows if _token_group_match_count(r, token_groups) >= required_matches]
    scored = [(r, _rss_rag_score(r, match_tokens)) for r in rows]
    scored.sort(
        key=lambda item: (
            item[1],
            _effective_item_time(item[0]),
            SERIOESITAET_RANK.get(_row_value(item[0], "reliability", "unbekannt"), 1),
        ),
        reverse=True,
    )
    return scored[:limit], q, force


def _ingest_rag(daten_json: str, config: dict | None = None) -> str:
    config = config or {}
    _init_db(config)
    try:
        d = json.loads(daten_json) if daten_json else {}
    except json.JSONDecodeError:
        return "FEHLER: Ungueltiges JSON."

    rag_dir, pool = _rag_dir(config, d)
    if not rag_dir:
        return f"RAG_INGEST\npool: {pool}\nstored: 0\nFEHLER: data_dir fehlt."

    conn = _get_db(config)
    scored, query, force = _select_ingest_rows(conn, d, pool)
    if not scored:
        return f"RAG_INGEST\npool: {pool}\nstored: 0\nskipped: 0\n(Keine passenden RSS-Items)"

    stored = 0
    skipped = 0
    linked_duplicates = 0
    errors = []
    seen_urls = set()
    now = _now_utc()
    lines = [
        "RAG_INGEST",
        f"pool: {pool}",
        f"query: {query or '(rss-pull)'}",
    ]

    for row, score in scored:
        item_id = _row_value(row, "id")
        if not force and _row_value(row, "rag_id") and _row_value(row, "rag_pool") == pool:
            skipped += 1
            continue
        url = _row_value(row, "url")
        if url and url in seen_urls:
            skipped += 1
            continue
        if url:
            seen_urls.add(url)
        dupe = None if force else _ingested_duplicate(conn, row, pool)
        if dupe and dupe["rag_id"]:
            conn.execute(
                "UPDATE items SET rag_id=?, rag_pool=?, rag_stored_at_utc=? WHERE id=?",
                [dupe["rag_id"], pool, dupe["rag_stored_at_utc"] or now, item_id],
            )
            linked_duplicates += 1
            continue
        try:
            note = _rss_rag_note(row, query, score)
            ok, rag_id, msg = _write_rag_note(note, config, d)
            if not ok:
                errors.append(msg)
                continue
            conn.execute(
                "UPDATE items SET rag_id=?, rag_pool=?, rag_stored_at_utc=? WHERE id=?",
                [rag_id, pool, now, item_id],
            )
            stored += 1
            lines.append(f"- {item_id} -> {rag_id[:8]} | score:{score:.1f} | {(_row_value(row, 'title') or '')[:110]}")
        except Exception as exc:
            errors.append(f"{item_id}: {str(exc)[:180]}")

    conn.commit()
    lines.insert(3, f"stored: {stored}")
    lines.insert(4, f"skipped: {skipped}")
    lines.insert(5, f"linked_duplicates: {linked_duplicates}")
    if errors:
        lines.append("errors:")
        lines.extend("- " + e for e in errors[:8])
    return "\n".join(lines)


def _item(item_id: str, config: dict | None = None) -> str:
    config = config or {}
    _init_db(config)
    conn = _get_db(config)
    row = conn.execute("""
        SELECT i.*, s.name as source_name, s.url as source_url, s.category, s.reliability, s.alignment
        FROM items i JOIN sources s ON i.source_id = s.id
        WHERE i.id=?
    """, [item_id]).fetchone()
    if not row:
        return f"Kein Item mit ID [{item_id}]."
    pub = row["published_at_utc"] or "?"
    fetched = row["fetched_at_utc"] or "?"
    return (
        f"ITEM [{row['id']}]\n"
        f"source: {row['source_name']} [{row['source_id']}]\n"
        f"category: {row['category']} | reliability: {row['reliability']} | alignment: {row['alignment']}\n"
        f"title: {row['title']}\n"
        f"item_url: {row['url']}\n"
        f"source_feed_url: {row['source_url']}\n"
        f"published_at_utc: {pub}\n"
        f"fetched_at_utc: {fetched}\n"
        f"age: {_age_label(_effective_item_time(row))}\n"
        f"summary: {(row['summary'] or '')[:500]}"
    )


def _stats(config: dict | None = None) -> str:
    config = config or {}
    _init_db(config)
    conn = _get_db(config)
    total_src = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    active_src = conn.execute("SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0]
    total_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    rag_items = conn.execute("SELECT COUNT(*) FROM items WHERE rag_id IS NOT NULL").fetchone()[0]
    last_fetch = conn.execute("SELECT MAX(last_fetch_at_utc) FROM sources WHERE active=1").fetchone()[0] or "nie"
    error_src = conn.execute("SELECT COUNT(*) FROM sources WHERE last_error IS NOT NULL AND active=1").fetchone()[0]

    lines = [
        f"=== RSS-Verwaltung Stats ===",
        f"Quellen gesamt: {total_src}",
        f"Quellen aktiv: {active_src}",
        f"Quellen mit Fehlern: {error_src}",
        f"Indexierte Items: {total_items}",
        f"Items im RAG markiert: {rag_items}",
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
    _init_db(config)
    try:
        if tool_name == "rss_verwaltung.hinzufuegen":
            data = _hinzufuegen(params[0] if params else "", config)
        elif tool_name == "rss_verwaltung.auflisten":
            data = _auflisten(params[0] if params else "{}", config)
        elif tool_name == "rss_verwaltung.entfernen":
            data = _entfernen(params[0] if params else "", config)
        elif tool_name == "rss_verwaltung.bewerten":
            data = _bewerten(params[0] if params else "", config)
        elif tool_name == "rss_verwaltung.fetch":
            data = _fetch(params[0] if params else "{}", config)
        elif tool_name == "rss_verwaltung.suche":
            data = _suche(params[0] if params else "", config)
        elif tool_name == "rss_verwaltung.fuer_deepdive":
            data = _fuer_deepdive(params[0] if params else "{}", config)
        elif tool_name == "rss_verwaltung.ingest_rag":
            data = _ingest_rag(params[0] if params else "{}", config)
        elif tool_name == "rss_verwaltung.item":
            data = _item(params[0] if params else "", config)
        elif tool_name == "rss_verwaltung.stats":
            data = _stats(config)
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
