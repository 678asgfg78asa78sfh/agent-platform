"""Deepdive crawler and RAG helpers.

DeepDive is not just a prompt checklist. The chat agent may still orchestrate
search/fetch/RAG manually, but broad research tasks should start with
``deepdive.crawl`` so the platform gathers current sources deterministically
before the LLM writes a synthesis.
"""

import html
import importlib.util
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from html.parser import HTMLParser
from uuid import uuid4


MODUL_DIR = os.path.dirname(os.path.abspath(__file__))
_RSS_MODULE = None
_REDDIT_MODULE = None
_GROK_SEARCH_MODULE = None
_YOUTUBE_TRANSCRIPT_MODULE = None

MODULE = {
    "name": "deepdive",
    "description": "Crawler fuer mehrstufige Recherche: Web suchen, Quellen abrufen, Datum/Text/Links extrahieren und RAG-Notizen speichern.",
    "version": "2.2",
    "settings": {
        "max_sources": {"type": "number", "label": "Seed-Quellen pro Crawl", "default": 8},
        "max_search_queries": {"type": "number", "label": "Suchvarianten", "default": 8},
        "enable_impact_language_plan": {"type": "bool", "label": "Impact-Sprachen im DeepDive", "default": True},
        "enable_branching_causality_plan": {"type": "bool", "label": "Causal Branching im DeepDive", "default": True},
        "max_branch_queries": {"type": "number", "label": "DeepDive Branch-Suchen", "default": 8},
        "min_branch_sources": {"type": "number", "label": "DeepDive Mindest-Branch-Quellen", "default": 6},
        "enable_subcrawls": {"type": "bool", "label": "DeepDive Subcrawls", "default": True},
        "max_subcrawls": {"type": "number", "label": "DeepDive Subcrawls Anzahl", "default": 4},
        "subcrawl_sources_per_topic": {"type": "number", "label": "Quellen je Subcrawl", "default": 2},
        "subcrawl_min_score": {"type": "number", "label": "Subcrawl Mindestscore", "default": 6},
        "deep_full_auto_reddit_sources": {"type": "bool", "label": "DeepDive: Reddit automatisch", "default": True},
        "deep_full_auto_grok_search_sources": {"type": "bool", "label": "DeepDive: Grok Search automatisch", "default": True},
        "enable_youtube_transcripts": {"type": "bool", "label": "YouTube-Transkripte als Quelle", "default": True},
        "parallel_search_workers": {"type": "number", "label": "Parallele Seed-Suchen", "default": 6},
        "parallel_fetch_workers": {"type": "number", "label": "Parallele Seitenabrufe", "default": 4},
        "quick_max_sources": {"type": "number", "label": "Quick: Seed-Quellen", "default": 4},
        "quick_max_search_queries": {"type": "number", "label": "Quick: Suchvarianten", "default": 3},
        "quick_max_total_pages": {"type": "number", "label": "Quick: Max Seiten", "default": 6},
        "quick_python_timeout_s": {"type": "number", "label": "Quick: Python Timeout", "default": 75},
        "max_total_pages": {"type": "number", "label": "Max Seiten inkl. Follow-ups", "default": 20},
        "max_follow_links_per_source": {"type": "number", "label": "Follow-up Links je Quelle", "default": 3},
        "max_depth": {"type": "number", "label": "Crawl-Tiefe", "default": 2},
        "max_derived_queries": {"type": "number", "label": "Abgeleitete Nachsuchen", "default": 4},
        "timeout_s": {"type": "number", "label": "Timeout je Request", "default": 6},
        "python_timeout_s": {"type": "number", "label": "Python Tool Timeout", "default": 300},
        "max_chars_per_source": {"type": "number", "label": "Max Textzeichen je Quelle", "default": 6000},
        "search_provider": {"type": "select", "label": "Websuche", "default": "auto", "options": ["auto", "tavily", "duckduckgo"]},
        "tavily_api_key": {"type": "password", "label": "Tavily API Key", "default": ""},
        "tavily_search_depth": {"type": "select", "label": "Tavily Suchtiefe", "default": "basic", "options": ["basic", "advanced"]},
        "enable_reddit_sources": {"type": "bool", "label": "Reddit als DeepDive-Quelle", "default": False},
        "reddit_max_threads": {"type": "number", "label": "Reddit Threads", "default": 3},
        "enable_grok_search_sources": {"type": "bool", "label": "Grok Web/X Search als DeepDive-Quelle", "default": False},
        "grok_search_api_key": {"type": "password", "label": "xAI API Key fuer DeepDive", "default": ""},
        "grok_search_model": {"type": "string", "label": "Grok Search Model", "default": "grok-4.3"},
        "grok_search_mode": {"type": "select", "label": "Grok Search Modus", "default": "research", "options": ["research", "web", "x"]},
        "grok_search_max_sources": {"type": "number", "label": "Grok Quellen in Crawl", "default": 8},
        "allow_private_networks": {"type": "bool", "label": "Private/LAN URLs erlauben", "default": False},
        "dedupe_source_url_hours": {"type": "number", "label": "RAG-Dedupe fuer gleiche URL (Stunden)", "default": 72},
    },
    "tools": [
        {
            "name": "deepdive.crawl",
            "description": "Fuehrt einen kompakten Web-Crawl fuer ein Thema aus, prueft aktuelle Suchvarianten, oeffnet Quellen und speichert verwertbare Quellen-Notizen direkt im RAG.",
            "params": ["query"],
        },
        {
            "name": "deepdive.quick",
            "description": "Schneller Recherche-Fanout fuer normale/kurze Fragen: weniger Quellen, harte Budgets, RAG-Speicherung mit Crawl-ID.",
            "params": ["query"],
        },
        {
            "name": "deepdive.pack",
            "description": "Liest ein kompaktes Ergebnispaket zu einer DeepDive crawl_id oder Query aus dem DeepDive-RAG, ohne breit im ganzen RAG zu suchen.",
            "params": ["crawl_id_or_query"],
        },
        {
            "name": "deepdive.blocks",
            "description": "Bereitet aus einer crawl_id getrennte Research-Bausteine vor: Quellenblock, Timeline, Claims, Kausalitaeten, Branching-Kontext, Kontraste und offene Leads fuer die finale Synthese.",
            "params": ["crawl_id_or_query"],
        },
        {
            "name": "deepdive.workflow",
            "description": "Gibt den Deepdive-Ablauf fuer ein Thema aus: Suche, Quellen oeffnen, bewerten, in RAG speichern, synthetisieren.",
            "params": ["query"],
        },
        {
            "name": "deepdive.source_note",
            "description": "Speichert eine einzelne Quellenbewertung als RAG-Notiz im verbundenen Deepdive-Pool.",
            "params": ["source"],
        },
    ],
}


WORKFLOW = """Deepdive-Ablauf:
1. Bei normalen kurzen Web-/News-/Preis-/Meinungs-Recherchen zuerst deepdive.quick(query) nutzen. Bei ausdruecklichem "DeepDive", "ausfuehrlich" oder komplexen Widerspruechen deepdive.crawl(query) nutzen.
2. DeepDive ist ein Causal-Investigator: Nicht "die besten Quellen" sammeln, sondern Ereignisse, Akteure, Claims, Leads, Analogien, Kausalitaeten, Widersprueche und offene Fragen herausarbeiten.
3. Branching planen: vom Startthema bewusst zu Nachbarbegriffen, Akteursnetzwerk, Wettbewerbern, betroffenen Laendern, historischen Analogien und moeglichen Missing Links suchen. Beispiel UFO: UAP, Aliens, Disclosure, Militaer/Sensorik, Whistleblower, internationale Akten. Beispiel Ford: GM/Toyota/Tesla/Stellantis/BYD, EV-Markt, Gewerkschaften, Lieferketten.
4. Impact-Sprachen planen: Suche nicht nur in der Nutzersprache. Waehle Sprachen/Regionen nach betroffenen Akteuren und Impact. Beispiel Japan: Japanisch, Chinesisch, Koreanisch, Englisch/Taiwan; Costa Rica nur bei konkretem Bezug.
5. Such-Snippets sind nur Wegweiser, keine Belege. Inhalte erst behaupten, nachdem die Quelle geoeffnet oder per deepdive.crawl verarbeitet wurde.
6. Quellen und Kommentare nicht isoliert lesen: Hinweise wie "vergleichbar mit XY", "laut XX" oder Links als Leads behandeln, nachziehen und als Lead/Claim speichern, nicht als Wahrheit.
7. Jede Quelle bewerten: URL, Titel, Sprache/Land/Perspektive, Datum/Stand, Autor/Outlet, primaer/sekundaer, Relevanz, Zuverlaessigkeit, Bias/Risiko, Kernaussagen, offene Unsicherheiten.
8. Jede verwertbare Beobachtung sofort mit deepdive.source_note als einzelne RAG-Notiz speichern, wenn sie nicht schon durch deepdive.crawl gespeichert wurde.
9. Nicht nach der ersten Notiz stoppen: mehrere unabhaengige Perspektiven vergleichen, vor allem bei Zeitbezug, geopolitischem Framing, sozialer Meinung oder widerspruechlichen Rollen.
10. Vor der Synthese deepdive.pack(crawl_id) ausfuehren, danach zwingend deepdive.blocks(crawl_id), damit Quellen/Timeline/Claims/Kausalitaeten als Bausteine vorbereitet sind. rag.suchen nur nutzen, wenn das Pack fehlt oder eine konkrete alte Notiz gesucht wird.
11. Ergebnis aus den vorbereiteten Blocks liefern: aktueller Stand, Timeline, Akteure, Kausalketten/Mechanismen, Perspektivenkontrast, gesicherte Punkte, Widersprueche, Unsicherheiten, Quellenliste mit Herkunft/Alter, offene Leads.
Regeln: Ein Toolcall pro Antwort. Bei Toolfehlern anders versuchen. Die Finalantwort ist nie ein rohes SUCCESS/Tool-Ergebnis und bildet keine eigene Meinung, sondern legt Informationslage und Kausalitaetsbehauptungen offen."""


class ReadableHtmlParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts = []
        self.text_parts = []
        self.links = []
        self.meta = {}
        self.date_candidates = []
        self._skip_stack = []
        self._in_title = False
        self._in_a = False
        self._a_href = ""
        self._a_text = []
        self._in_time = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr = {k.lower(): v for k, v in attrs if k}
        if tag in {"script", "style", "svg", "canvas", "noscript"}:
            self._skip_stack.append(tag)
            return
        if self._skip_stack:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            self._handle_meta(attr)
        elif tag == "time":
            self._in_time = True
            if attr.get("datetime"):
                self.date_candidates.append(attr["datetime"])
        elif tag == "a":
            href = (attr.get("href") or "").strip()
            if href:
                self._in_a = True
                self._a_href = href
                self._a_text = []
        elif tag in {"article", "section", "p", "li", "br", "h1", "h2", "h3"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_stack:
            if self._skip_stack[-1] == tag:
                self._skip_stack.pop()
            return
        if tag == "title":
            self._in_title = False
        elif tag == "time":
            self._in_time = False
        elif tag == "a" and self._in_a:
            url = urllib.parse.urljoin(self.base_url, self._a_href)
            label = _collapse_ws(" ".join(self._a_text))
            if _is_http_url(url) and label:
                self.links.append({"text": label[:140], "url": url})
            self._in_a = False
            self._a_href = ""
            self._a_text = []

    def handle_data(self, data):
        if self._skip_stack:
            return
        value = data.strip()
        if not value:
            return
        if self._in_title:
            self.title_parts.append(value)
        if self._in_a:
            self._a_text.append(value)
        if self._in_time:
            self.date_candidates.append(value)
        self.text_parts.append(value)

    def _handle_meta(self, attr):
        key = (
            attr.get("name")
            or attr.get("property")
            or attr.get("itemprop")
            or attr.get("http-equiv")
            or ""
        ).lower()
        content = (attr.get("content") or "").strip()
        if not key or not content:
            return
        if key in {
            "description",
            "og:description",
            "twitter:description",
            "author",
            "article:author",
            "publisher",
            "og:site_name",
        }:
            self.meta[key] = content
        if (
            "date" in key
            or "published" in key
            or "modified" in key
            or key in {"article:published_time", "article:modified_time"}
        ):
            self.meta[key] = content
            self.date_candidates.append(content)

    def readable(self):
        title = _collapse_ws(" ".join(self.title_parts))
        text = _normalize_text("\n".join(self.text_parts))
        dates = [value for value in _unique(self.date_candidates + _extract_dates(text)) if not _looks_like_tracking_id(value)][:14]
        links = []
        seen = set()
        for link in self.links:
            url = link["url"]
            if url in seen:
                continue
            seen.add(url)
            links.append(link)
            if len(links) >= 120:
                break
        return {
            "title": title,
            "meta": self.meta,
            "dates": dates,
            "text": text,
            "links": links,
        }


def handle_tool(tool_name, params, config):
    if tool_name == "deepdive.crawl":
        query = _first_param(params, "query")
        if not query:
            return {"success": False, "data": "Kein Thema angegeben."}
        return _crawl(query, config if isinstance(config, dict) else {})

    if tool_name == "deepdive.quick":
        query = _first_param(params, "query")
        if not query:
            return {"success": False, "data": "Kein Thema angegeben."}
        return _crawl(query, _quick_config(config if isinstance(config, dict) else {}))

    if tool_name == "deepdive.pack":
        needle = _first_param(params, "crawl_id_or_query") or _first_param(params, "query")
        if not needle:
            return {"success": False, "data": "Keine crawl_id/Query angegeben."}
        return _pack(needle, config if isinstance(config, dict) else {})

    if tool_name == "deepdive.blocks":
        needle = _first_param(params, "crawl_id_or_query") or _first_param(params, "query")
        if not needle:
            return {"success": False, "data": "Keine crawl_id/Query angegeben."}
        return _blocks(needle, config if isinstance(config, dict) else {})

    if tool_name == "deepdive.workflow":
        query = _first_param(params, "query") or "(kein Thema angegeben)"
        return {"success": True, "data": f"Thema: {query}\n\n{WORKFLOW}"}

    if tool_name == "deepdive.source_note":
        source = _first_param(params, "source")
        if not source:
            return {"success": False, "data": "Keine Quelle/Notiz angegeben."}
        note = _source_note(source)
        stored, storage_msg = _store_rag_note(note, config)
        next_step = (
            "NEXT_STEP: Diese Notiz ist gespeichert. Nicht als finale Antwort mit SUCCESS enden. "
            "Entweder weitere Quellen pruefen oder vor der Synthese rag.suchen nutzen."
        )
        if stored:
            return {"success": True, "data": f"{storage_msg}\n\n{note}\n\n{next_step}"}
        return {"success": True, "data": f"{note}\n\nHinweis: {storage_msg}\n\n{next_step}"}

    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}


def _crawl(query, config):
    profile = str(config.get("_deepdive_profile") or "crawl")
    max_sources = _clamp_int(config.get("max_sources"), 8, 1, 16)
    max_search_queries = _clamp_int(config.get("max_search_queries"), 6, 1, 16)
    parallel_search_workers = _clamp_int(config.get("parallel_search_workers"), 6, 1, 10)
    parallel_fetch_workers = _clamp_int(config.get("parallel_fetch_workers"), 4, 1, 12)
    max_total_pages = _clamp_int(config.get("max_total_pages"), 20, max_sources, 36)
    max_follow_links = _clamp_int(config.get("max_follow_links_per_source"), 3, 0, 6)
    max_depth = _clamp_int(config.get("max_depth"), 2, 0, 2)
    max_derived_queries = _clamp_int(config.get("max_derived_queries"), 4, 0, 8)
    max_branch_queries = _clamp_int(config.get("max_branch_queries"), 8, 0, 12)
    min_branch_sources = _clamp_int(config.get("min_branch_sources"), 6, 0, 10)
    enable_subcrawls = _cfg_bool(config.get("enable_subcrawls"), True)
    max_subcrawls = _clamp_int(config.get("max_subcrawls"), 4, 0, 8)
    subcrawl_sources_per_topic = _clamp_int(config.get("subcrawl_sources_per_topic"), 2, 1, 4)
    subcrawl_min_score = _clamp_int(config.get("subcrawl_min_score"), 6, 0, 50)
    timeout_s = _clamp_int(config.get("timeout_s"), 6, 3, 12)
    python_timeout_s = _clamp_int(config.get("python_timeout_s"), 300, 20, 300)
    max_chars = _clamp_int(config.get("max_chars_per_source"), 6000, 1200, 16000)
    enable_reddit_sources = _cfg_bool(config.get("enable_reddit_sources"), False)
    reddit_max_threads = _clamp_int(config.get("reddit_max_threads"), 3, 0, 6)
    enable_grok_search_sources = _cfg_bool(config.get("enable_grok_search_sources"), False)
    grok_search_max_sources = _clamp_int(config.get("grok_search_max_sources"), 8, 0, 20)
    allow_private = bool(config.get("allow_private_networks") or False)
    full_deepdive = profile != "quick"
    if full_deepdive:
        max_sources = max(max_sources, 12)
        max_search_queries = max(max_search_queries, 16)
        max_total_pages = max(max_total_pages, 36)
        max_follow_links = max(max_follow_links, 4)
        max_derived_queries = max(max_derived_queries, 6)
        max_branch_queries = max(max_branch_queries, 8)
        min_branch_sources = max(min_branch_sources, 6)
        if enable_subcrawls:
            max_subcrawls = max(max_subcrawls, 4)
            subcrawl_sources_per_topic = max(subcrawl_sources_per_topic, 2)
        parallel_search_workers = min(max(parallel_search_workers, 8), max_search_queries)
        parallel_fetch_workers = min(max(parallel_fetch_workers, 6), max_total_pages)
        if _cfg_bool(config.get("deep_full_auto_reddit_sources"), True):
            enable_reddit_sources = True
            reddit_max_threads = max(reddit_max_threads, 3)
        if _cfg_bool(config.get("deep_full_auto_grok_search_sources"), True) and _grok_search_likely_available(config):
            enable_grok_search_sources = True
            grok_search_max_sources = max(grok_search_max_sources, 8)
    original_query = query
    query, query_normalization = _normalize_current_query(query)
    deadline = time.monotonic() + max(10, python_timeout_s - 5)
    subcrawl_reserve_s = 0
    if profile != "quick" and enable_subcrawls and max_subcrawls > 0:
        subcrawl_reserve_s = max(40, min(90, int(python_timeout_s * 0.35)))
    crawl_started_at = datetime.now(timezone.utc)
    crawl_started_iso = crawl_started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    crawl_id = "dd-" + crawl_started_at.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    research_plan = _impact_research_plan(query, max_search_queries, full_deepdive, config, max_branch_queries)
    trace = []
    tool_trace = []
    _trace(
        trace,
        "crawl.start",
        {
            "query": query,
            "profile": profile,
            "max_sources": max_sources,
            "max_search_queries": max_search_queries,
            "parallel_search_workers": parallel_search_workers,
            "parallel_fetch_workers": parallel_fetch_workers,
            "max_total_pages": max_total_pages,
            "max_follow_links_per_source": max_follow_links,
            "max_depth": max_depth,
            "max_derived_queries": max_derived_queries,
            "max_branch_queries": max_branch_queries,
            "min_branch_sources": min_branch_sources,
            "enable_subcrawls": enable_subcrawls,
            "max_subcrawls": max_subcrawls,
            "subcrawl_sources_per_topic": subcrawl_sources_per_topic,
            "subcrawl_min_score": subcrawl_min_score,
            "subcrawl_reserve_s": subcrawl_reserve_s,
            "timeout_s": timeout_s,
            "python_timeout_s": python_timeout_s,
            "enable_reddit_sources": enable_reddit_sources,
            "enable_grok_search_sources": enable_grok_search_sources,
            "enable_branching_causality_plan": _cfg_bool(config.get("enable_branching_causality_plan"), True),
        },
    )
    if query_normalization:
        _trace(
            trace,
            "query.normalized_currentness",
            {
                "original_query": original_query,
                "normalized_query": query,
                **query_normalization,
            },
        )
    _trace(trace, "research.plan", research_plan)
    _tool_trace(
        tool_trace,
        "deepdive.crawl",
        "START",
        {
            "query": query,
            "profile": profile,
            "impact_languages": [item.get("language") for item in research_plan.get("impact_plan", [])],
            "branch_queries": len(research_plan.get("branch_queries", [])),
            "max_search_queries": max_search_queries,
            "parallel_fetch_workers": parallel_fetch_workers,
            "max_total_pages": max_total_pages,
        },
    )

    search_queries = research_plan.get("search_queries") or _build_search_queries(query, max_search_queries)
    branch_lookup = _branch_query_lookup(research_plan)
    impact_lookup = _impact_query_lookup(research_plan)
    executed_searches = []
    search_errors = []
    candidates = []
    candidate_urls = set()

    seed_deadline = deadline
    if full_deepdive and enable_subcrawls and max_subcrawls > 0 and subcrawl_reserve_s > 0:
        seed_deadline = min(deadline, max(time.monotonic() + 10, deadline - subcrawl_reserve_s))
        _trace(
            trace,
            "subcrawl.seed_reserve",
            {
                "seed_budget_s": round(max(0, seed_deadline - time.monotonic()), 1),
                "subcrawl_reserve_s": subcrawl_reserve_s,
            },
        )
    seed_results = _run_seed_searches(search_queries, timeout_s, config, seed_deadline, parallel_search_workers, trace, tool_trace)
    for item in seed_results:
        search_query = item["query"]
        search_tool = item["tool"]
        executed_searches.append(search_query)
        if item.get("error"):
            search_errors.append(f"{search_query}: {item['error']}")
            continue
        raw_results = item.get("results") or []
        branch_item = branch_lookup.get(_query_key(search_query))
        impact_item = impact_lookup.get(_query_key(search_query))
        accepted = 0
        for result in raw_results:
            url = _clean_url(result.get("url") or "")
            if not url or url in candidate_urls:
                continue
            if not _is_allowed_http_url(url, allow_private):
                continue
            if _skip_link(url, f"{result.get('title', '')} {result.get('snippet', '')}"):
                continue
            candidate_urls.add(url)
            result["search_query"] = search_query
            result["depth"] = 0
            result["parent_url"] = ""
            if branch_item:
                result["branch_name"] = branch_item.get("branch") or "branch"
                result["branch_reason"] = branch_item.get("reason") or ""
                result["discovery_method"] = "branch_search"
                result["discovery_reason"] = f"Branch-Suchtreffer fuer {result['branch_name']}: {search_query}"
            else:
                result["discovery_method"] = "search"
                result["discovery_reason"] = f"Suchtreffer fuer: {search_query}"
            if impact_item:
                result["impact_language"] = impact_item.get("language") or ""
                result["impact_country"] = impact_item.get("country") or ""
                result["impact_role"] = impact_item.get("role") or ""
            candidates.append(result)
            accepted += 1
        _trace(
            trace,
            "search.accept",
            {
                "query": search_query,
                "engine": search_tool,
                "results": len(raw_results),
                "accepted": accepted,
            },
        )
        _tool_trace(
            tool_trace,
            search_tool,
            "ACCEPT",
            {
                "phase": "seed",
                "query": search_query,
                "results": len(raw_results),
                "accepted": accepted,
            },
        )

    # ── RSS-Integration: RSS-Quellen als zusätzliche Kandidaten ──
    rss_candidates_added = 0
    rss_packet = ""
    rss_urls = []
    try:
        rss_module = _load_rss_module()
        rss_source_list = rss_module._fuer_deepdive(json.dumps({
            "query": query,
            "refresh": True,
            "rag_ingest": True,
            "stale_after_minutes": 30,
            "limit_sources": 8,
            "limit_items": 12,
            "max_items_per_source": 30,
        }), config)
        # Nur echte Item-URLs extrahieren – nicht den ganzen RSS-Block in den LLM-Kontext kippen.
        if rss_source_list:
            rss_packet = str(rss_source_list or "")
            rss_urls = _extract_rss_item_urls(rss_source_list)
            for rss_url in rss_urls[:8]:
                if rss_url not in candidate_urls and _is_allowed_http_url(rss_url, allow_private):
                    if _skip_link(rss_url, ""):
                        continue
                    candidate_urls.add(rss_url)
                    candidates.append({
                        "url": rss_url,
                        "search_query": query,
                        "depth": 0,
                        "parent_url": "",
                        "discovery_method": "rss",
                        "discovery_reason": f"RSS-Item passend zu: {query}",
                    })
                    rss_candidates_added += 1
    except Exception as exc:
        _trace(trace, "rss.fail", {"error": str(exc)[:220]})
        _tool_trace(tool_trace, "rss_verwaltung.fuer_deepdive", "FAIL", {"error": str(exc)[:220]})
    if rss_candidates_added:
        _trace(trace, "rss.inject", {"added": rss_candidates_added})
        _tool_trace(tool_trace, "rss_verwaltung.fuer_deepdive", "OK", {"added_candidates": rss_candidates_added})

    external_packets = []
    if rss_packet and rss_urls:
        stored, storage_msg = _store_rag_note(
            _external_packet_note(crawl_id, query, "rss_verwaltung.fuer_deepdive", rss_packet),
            config,
        )
        external_packets.append(("rss_verwaltung.fuer_deepdive", len(rss_urls), stored, storage_msg))
        _trace(
            trace,
            "rss.packet.store",
            {"urls": len(rss_urls), "stored": stored, "rag_id": _stored_id(storage_msg), "message": storage_msg},
        )
        _tool_trace(
            tool_trace,
            "rag.speichern",
            "OK" if stored else "FAIL",
            {
                "source_url": "rss_verwaltung.fuer_deepdive",
                "rag_id": _stored_id(storage_msg),
                "message": storage_msg,
            },
        )
    if enable_reddit_sources and reddit_max_threads > 0:
        try:
            packet, reddit_urls = _collect_reddit_sources(query, reddit_max_threads, config)
            stored, storage_msg = _store_rag_note(_external_packet_note(crawl_id, query, "reddit_scraper.pull", packet), config)
            external_packets.append(("reddit_scraper.pull", len(reddit_urls), stored, storage_msg))
            added = 0
            for reddit_url in reddit_urls[:reddit_max_threads]:
                if reddit_url not in candidate_urls and _is_allowed_http_url(reddit_url, allow_private):
                    candidate_urls.add(reddit_url)
                    candidates.append({
                        "title": "Reddit Thread",
                        "url": reddit_url,
                        "search_query": query,
                        "depth": 0,
                        "parent_url": "",
                        "discovery_method": "reddit",
                        "discovery_reason": f"Reddit-Diskussion passend zu: {query}",
                    })
                    added += 1
            _trace(trace, "reddit.inject", {"urls": len(reddit_urls), "added": added, "stored": stored})
            _tool_trace(
                tool_trace,
                "reddit_scraper.pull",
                "OK",
                {"urls": len(reddit_urls), "added_candidates": added, "rag": storage_msg},
            )
        except Exception as exc:
            _trace(trace, "reddit.fail", {"error": str(exc)[:220]})
            _tool_trace(tool_trace, "reddit_scraper.pull", "FAIL", {"error": str(exc)[:220]})

    if enable_grok_search_sources and grok_search_max_sources > 0:
        try:
            packet, grok_urls, grok_tool = _collect_grok_search_sources(query, config)
            stored, storage_msg = _store_rag_note(_external_packet_note(crawl_id, query, grok_tool, packet), config)
            external_packets.append((grok_tool, len(grok_urls), stored, storage_msg))
            added = 0
            for grok_url in grok_urls[:grok_search_max_sources]:
                if grok_url not in candidate_urls and _is_allowed_http_url(grok_url, allow_private):
                    if _skip_link(grok_url, ""):
                        continue
                    candidate_urls.add(grok_url)
                    candidates.append({
                        "title": "Grok Search Source",
                        "url": grok_url,
                        "search_query": query,
                        "depth": 0,
                        "parent_url": "",
                        "discovery_method": "grok_search",
                        "discovery_reason": f"Grok Web/X Search Quelle passend zu: {query}",
                    })
                    added += 1
            _trace(trace, "grok_search.inject", {"tool": grok_tool, "urls": len(grok_urls), "added": added, "stored": stored})
            _tool_trace(
                tool_trace,
                grok_tool,
                "OK",
                {"urls": len(grok_urls), "added_candidates": added, "rag": storage_msg},
            )
        except Exception as exc:
            _trace(trace, "grok_search.fail", {"error": str(exc)[:220]})
            _tool_trace(tool_trace, "grok_search", "FAIL", {"error": str(exc)[:220]})

    selected = _select_sources(candidates, max_sources, query, min_branch_sources=min_branch_sources)
    _trace(
        trace,
        "sources.select",
        {
            "candidates": len(candidates),
            "selected": len(selected),
            "urls": [item["url"] for item in selected],
        },
    )
    subcrawl_plan = []
    if full_deepdive and enable_subcrawls and max_subcrawls > 0:
        planning_context = []
        for item in (selected + candidates[:24]):
            planning_context.append(
                {
                    "title": item.get("title") or "",
                    "branch_name": item.get("branch_name") or "",
                    "analysis_text": _collapse_ws(
                        " ".join(
                            [
                                str(item.get("title") or ""),
                                str(item.get("snippet") or ""),
                                str(item.get("search_query") or ""),
                                str(item.get("branch_name") or ""),
                                str(item.get("branch_reason") or ""),
                                str(item.get("discovery_reason") or ""),
                            ]
                        )
                    )[:3000],
                }
            )
        subcrawl_plan = _plan_subcrawls(
            query,
            planning_context,
            research_plan,
            max_subcrawls=max_subcrawls,
            min_score=subcrawl_min_score,
        )
        _trace(
            trace,
            "subcrawl.plan",
            {
                "phase": "pre_fetch",
                "candidates": len(subcrawl_plan),
                "selected": [f"{item.get('score')}:{item.get('topic')}" for item in subcrawl_plan],
            },
        )
    crawl_queue = list(selected)
    known_urls = {item["url"] for item in crawl_queue}
    visited_urls = set()
    fetched = []
    failed = []
    followed_links = 0
    derived_queries_done = 0
    derived_sources_added = 0
    subcrawl_sources_added = 0

    while crawl_queue and len(fetched) < max_total_pages:
        if time.monotonic() > deadline:
            failed.append("Zeitbudget erreicht, Crawl begrenzt beendet")
            _trace(trace, "crawl.deadline", {"stage": "fetch_loop", "fetched": len(fetched)})
            break
        subcrawl_yield_reason = ""
        if full_deepdive and enable_subcrawls and max_subcrawls > 0 and subcrawl_plan:
            main_before_subcrawl_cap = max(4, min(10, max_sources))
            if len(fetched) >= main_before_subcrawl_cap:
                subcrawl_yield_reason = "main_page_cap"
            elif subcrawl_reserve_s > 0 and len(fetched) >= 1 and time.monotonic() > deadline - subcrawl_reserve_s:
                subcrawl_yield_reason = "time_reserve"
        if subcrawl_yield_reason:
            failed.append("Hauptcrawl zugunsten Subcrawl-Zeitreserve begrenzt")
            _trace(
                trace,
                "subcrawl.reserve",
                {
                    "fetched": len(fetched),
                    "queued": len(crawl_queue),
                    "reserve_s": subcrawl_reserve_s,
                    "reason": subcrawl_yield_reason,
                    "stage": "fetch_loop",
                },
            )
            break
        if parallel_fetch_workers > 1:
            _prefetch_queue(
                crawl_queue,
                visited_urls,
                timeout_s,
                parallel_fetch_workers,
                deadline,
                max_total_pages - len(fetched),
                trace,
                tool_trace,
            )
        result = crawl_queue.pop(0)
        url = result["url"]
        if url in visited_urls:
            _trace(trace, "fetch.skip", {"url": url, "reason": "already_visited"})
            continue
        visited_urls.add(url)
        depth = int(result.get("depth") or 0)
        try:
            _trace(
                trace,
                "fetch.start",
                {
                    "url": url,
                    "depth": int(result.get("depth") or 0),
                    "method": result.get("discovery_method") or "search",
                    "parent_url": result.get("parent_url") or "",
                },
            )
            prefetch_error = result.pop("_prefetch_error", "")
            is_youtube = _is_youtube_url(url)
            page_html = ""
            if prefetch_error and not is_youtube:
                raise RuntimeError(prefetch_error)
            if is_youtube and bool(config.get("enable_youtube_transcripts", True)):
                try:
                    page = _youtube_transcript_page(url, query, config, max_chars)
                    page_html = ""
                    _trace(trace, "youtube_transcript.use", {"url": url, "text_chars": len(page.get("text") or "")})
                    _tool_trace(
                        tool_trace,
                        "youtube_transcript.fetch",
                        "OK",
                        {"url": url, "text_chars": len(page.get("text") or "")},
                    )
                except Exception as exc:
                    _trace(trace, "youtube_transcript.fail", {"url": url, "error": str(exc)[:220]})
                    _tool_trace(tool_trace, "youtube_transcript.fetch", "FAIL", {"url": url, "error": str(exc)[:220]})
                    if prefetch_error:
                        raise RuntimeError(prefetch_error)
                    page = None
            else:
                page = None
            if page is None and "_prefetched_html" in result:
                page_html = result.pop("_prefetched_html") or ""
                _trace(trace, "fetch.prefetch_use", {"url": url, "html_chars": len(page_html)})
            elif page is None:
                page_html = _fetch_url(url, timeout_s)
            if page is None:
                page = _parse_page(url, page_html)
            if not page["text"] and not page["title"]:
                failed.append(f"{url} -> kein lesbarer Text")
                _trace(trace, "fetch.empty", {"url": url, "html_chars": len(page_html)})
                _tool_trace(
                    tool_trace,
                    "http.get",
                    "EMPTY",
                    {"url": url, "depth": depth, "html_chars": len(page_html)},
                )
                continue
            if _low_value_page(url, page):
                failed.append(f"{url} -> low_value_page")
                _trace(trace, "fetch.low_value", {"url": url, "title": page.get("title") or ""})
                continue
            relevance_score = _relevance_score(query, result, page)
            reliability = _reliability_label(url)
            recency = _recency_label(page.get("dates") or [])
            if _stale_for_current_query(query, page.get("dates") or []):
                failed.append(f"{url} -> stale_source_for_current_query ({recency})")
                _trace(
                    trace,
                    "fetch.stale_skip",
                    {
                        "url": url,
                        "title": page["title"] or result.get("title") or "",
                        "recency_label": recency,
                        "query": query,
                    },
                )
                _tool_trace(
                    tool_trace,
                    "http.get",
                    "SKIP",
                    {
                        "url": url,
                        "reason": "stale_source_for_current_query",
                        "recency_label": recency,
                    },
                )
                continue
            key_passages = _key_passages(query, page.get("text") or "", page.get("dates") or [])
            causality_hints = _causality_hints(query, page.get("text") or "", key_passages)
            claim_hints = _claim_hints(query, page.get("text") or "", key_passages)
            event_hints = _event_hints(query, page.get("text") or "", page.get("dates") or [])
            lead_hints = _lead_hints(query, page.get("text") or "", page.get("links") or [])
            contrast_hints = _contrast_hints(query, page.get("text") or "", key_passages)
            perspective = _source_perspective(result, page, research_plan)
            page_role = _page_role(url, result, page, query)
            result["_relevance_score"] = relevance_score
            result["_reliability"] = reliability
            result["_recency_label"] = recency
            result["_key_passages"] = key_passages
            result["_causality_hints"] = causality_hints
            result["_claim_hints"] = claim_hints
            result["_event_hints"] = event_hints
            result["_lead_hints"] = lead_hints
            result["_contrast_hints"] = contrast_hints
            result["_perspective"] = perspective
            result["_page_role"] = page_role
            note = _crawl_note(crawl_id, query, result, page, max_chars)
            stored, storage_msg = _store_rag_note(note, config)
            if page_role == "hub":
                _trace(
                    trace,
                    "hub.detect",
                    {
                        "url": url,
                        "title": page["title"] or result.get("title") or "",
                        "links": len(page.get("links") or []),
                    },
                )
            _trace(
                trace,
                "fetch.done",
                {
                    "url": url,
                    "title": page["title"] or result.get("title") or "",
                    "html_chars": len(page_html),
                    "text_chars": len(page["text"]),
                    "links": len(page.get("links") or []),
                    "date_hints": len(page.get("dates") or []),
                    "relevance_score": relevance_score,
                    "recency_label": recency,
                    "page_role": page_role,
                },
            )
            _tool_trace(
                tool_trace,
                "http.get",
                "OK",
                {
                    "url": url,
                    "depth": depth,
                    "html_chars": len(page_html),
                    "text_chars": len(page["text"]),
                    "links": len(page.get("links") or []),
                    "page_role": page_role,
                },
            )
            _trace(
                trace,
                "rag.store",
                {
                    "url": url,
                    "stored": stored,
                    "rag_id": _stored_id(storage_msg),
                    "message": storage_msg,
                },
            )
            _tool_trace(
                tool_trace,
                "rag.speichern",
                "OK" if stored else "FAIL",
                {
                    "source_url": url,
                    "rag_id": _stored_id(storage_msg),
                    "message": storage_msg,
                },
            )
            fetched.append(
                {
                    "url": url,
                    "title": page["title"] or result.get("title") or "(kein Titel)",
                    "dates": page["dates"],
                    "depth": depth,
                    "discovery_method": result.get("discovery_method") or "search",
                    "parent_url": result.get("parent_url") or "",
                    "relevance_score": relevance_score,
                    "reliability": reliability,
                    "recency_label": recency,
                    "source_language": perspective.get("language", ""),
                    "source_country": perspective.get("country", ""),
                    "perspective_role": perspective.get("role", ""),
                    "branch_name": result.get("branch_name") or "",
                    "branch_reason": result.get("branch_reason") or "",
                    "page_role": page_role,
                    "stored": stored,
                    "storage_msg": storage_msg,
                    "chars": len(page["text"]),
                    "analysis_text": _collapse_ws(" ".join([page["title"] or result.get("title") or ""] + key_passages[:4] + causality_hints[:3] + claim_hints[:3]))[:3000],
                }
            )

            if depth < max_depth and max_follow_links > 0:
                remaining = max_total_pages - len(fetched) - len(crawl_queue)
                if remaining > 0:
                    follow_limit = max_follow_links
                    if page_role == "hub":
                        follow_limit = max(max_follow_links, 4)
                    links = _select_followup_links(
                        page.get("links") or [],
                        query,
                        url,
                        min(follow_limit, remaining),
                        known_urls,
                        allow_private,
                        page_role,
                    )
                    _trace(
                        trace,
                        "followup.select",
                        {
                            "parent_url": url,
                            "available_links": len(page.get("links") or []),
                            "selected": len(links),
                            "urls": [link["url"] for link in links],
                        },
                    )
                    queued_followups = []
                    for link in links:
                        if len(fetched) + len(crawl_queue) >= max_total_pages:
                            break
                        known_urls.add(link["url"])
                        queued_followups.append(
                            {
                                "title": link["text"],
                                "url": link["url"],
                                "snippet": f"Follow-up-Linktext: {link['text']}",
                                "search_query": result.get("search_query") or "",
                                "depth": depth + 1,
                                "parent_url": url,
                                "discovery_method": "source_link",
                                "discovery_reason": link.get("reason") or "relevanter Link in Quelle",
                                "link_score": link.get("score", 0),
                                "branch_name": result.get("branch_name") or "",
                                "branch_reason": result.get("branch_reason") or "",
                            }
                        )
                        followed_links += 1
                        _trace(
                            trace,
                            "followup.queue",
                            {
                                "url": link["url"],
                                "parent_url": url,
                                "score": link.get("score", 0),
                                "reason": link.get("reason") or "",
                                "priority": "front" if page_role == "hub" else "normal",
                            },
                        )
                        _tool_trace(
                            tool_trace,
                            "deepdive.follow_link",
                            "QUEUE",
                            {
                                "url": link["url"],
                                "parent_url": url,
                                "score": link.get("score", 0),
                                "reason": link.get("reason") or "",
                            },
                        )
                    if page_role == "hub":
                        for item in reversed(queued_followups):
                            crawl_queue.insert(0, item)
                    else:
                        crawl_queue.extend(queued_followups)

            pending_seed_sources = any(int(item.get("depth") or 0) == 0 for item in crawl_queue)
            if page_role == "hub":
                _trace(
                    trace,
                    "derived_search.skip",
                    {
                        "from_url": url,
                        "reason": "hub_followups_have_priority",
                    },
                )
            elif pending_seed_sources:
                _trace(
                    trace,
                    "derived_search.skip",
                    {
                        "from_url": url,
                        "reason": "pending_seed_sources_first",
                    },
                )
            elif derived_queries_done < max_derived_queries:
                for derived_query in _derive_search_queries(query, page):
                    if derived_queries_done >= max_derived_queries:
                        break
                    if len(fetched) + len(crawl_queue) >= max_total_pages:
                        break
                    if time.monotonic() > deadline:
                        break
                    if derived_query.lower() in {q.lower() for q in executed_searches}:
                        continue
                    executed_searches.append(derived_query)
                    derived_queries_done += 1
                    search_tool = _configured_search_tool_name(config)
                    try:
                        _trace(
                            trace,
                            "derived_search.start",
                            {"query": derived_query, "from_url": url, "engine": search_tool},
                        )
                        derived_candidates = []
                        search_tool, derived_results, search_note = _search_web(derived_query, 5, timeout_s, config)
                        for dres in derived_results:
                            durl = _clean_url(dres.get("url") or "")
                            if not durl or durl in known_urls:
                                continue
                            if not _is_allowed_http_url(durl, allow_private):
                                continue
                            if _skip_link(durl, f"{dres.get('title', '')} {dres.get('snippet', '')}"):
                                continue
                            dres["url"] = durl
                            dres["search_query"] = derived_query
                            dres["depth"] = depth + 1
                            dres["parent_url"] = url
                            dres["discovery_method"] = "derived_search"
                            dres["discovery_reason"] = f"Nachsuche aus Quelle: {page['title'] or url}"
                            dres["branch_name"] = result.get("branch_name") or ""
                            dres["branch_reason"] = result.get("branch_reason") or ""
                            derived_candidates.append(dres)
                        selected_derived = _select_sources(derived_candidates, 2, derived_query)
                        _trace(
                            trace,
                            "derived_search.done",
                            {
                                "query": derived_query,
                                "engine": search_tool,
                                "candidates": len(derived_candidates),
                                "selected": len(selected_derived),
                                "urls": [item["url"] for item in selected_derived],
                                **({"note": search_note} if search_note else {}),
                            },
                        )
                        _tool_trace(
                            tool_trace,
                            search_tool,
                            "OK",
                            {
                                "phase": "derived",
                                "query": derived_query,
                                "engine": search_tool,
                                "from_url": url,
                                "candidates": len(derived_candidates),
                                "selected": len(selected_derived),
                            },
                        )
                        for dres in selected_derived:
                            if len(fetched) + len(crawl_queue) >= max_total_pages:
                                break
                            known_urls.add(dres["url"])
                            crawl_queue.append(dres)
                            derived_sources_added += 1
                            _trace(
                                trace,
                                "derived_source.queue",
                                {
                                    "query": derived_query,
                                    "url": dres["url"],
                                    "parent_url": url,
                                },
                            )
                    except Exception as exc:
                        search_errors.append(f"{derived_query}: {exc}")
                        _trace(
                            trace,
                            "derived_search.fail",
                            {"query": derived_query, "from_url": url, "error": str(exc)},
                        )
                        _tool_trace(
                            tool_trace,
                            search_tool,
                            "FAIL",
                            {
                                "phase": "derived",
                                "query": derived_query,
                                "engine": search_tool,
                                "from_url": url,
                                "error": str(exc),
                            },
                        )
        except Exception as exc:
            failed.append(f"{url} -> {exc}")
            _trace(trace, "fetch.fail", {"url": url, "error": str(exc)})
            _tool_trace(
                tool_trace,
                "http.get",
                "FAIL",
                {"url": url, "depth": depth, "error": str(exc)},
            )

    if full_deepdive and enable_subcrawls and max_subcrawls > 0 and time.monotonic() < deadline:
        if not subcrawl_plan:
            subcrawl_plan = _plan_subcrawls(
                query,
                fetched,
                research_plan,
                max_subcrawls=max_subcrawls,
                min_score=subcrawl_min_score,
            )
            _trace(
                trace,
                "subcrawl.plan",
                {
                    "phase": "post_fetch",
                    "candidates": len(subcrawl_plan),
                    "selected": [f"{item.get('score')}:{item.get('topic')}" for item in subcrawl_plan],
                },
            )
        if subcrawl_plan:
            subcrawl_added, subcrawl_failures = _run_subcrawls(
                crawl_id,
                query,
                subcrawl_plan,
                fetched,
                known_urls,
                config,
                timeout_s,
                max_chars,
                allow_private,
                research_plan,
                trace,
                tool_trace,
                deadline,
                subcrawl_sources_per_topic,
            )
            subcrawl_sources_added = subcrawl_added
            failed.extend(subcrawl_failures)

    _trace(
        trace,
        "crawl.finish",
        {
            "sources_fetched": len(fetched),
            "seed_sources": len(selected),
            "followup_links_queued": followed_links,
            "derived_queries_run": derived_queries_done,
            "derived_sources_queued": derived_sources_added,
            "subcrawls_planned": len(subcrawl_plan),
            "subcrawl_sources_added": subcrawl_sources_added,
            "failed": len(failed),
            "search_errors": len(search_errors),
        },
    )
    _tool_trace(
        tool_trace,
        "deepdive.crawl",
        "DONE",
        {
            "sources_fetched": len(fetched),
            "seed_sources": len(selected),
            "followup_links_queued": followed_links,
            "derived_queries_run": derived_queries_done,
            "subcrawls_planned": len(subcrawl_plan),
            "subcrawl_sources_added": subcrawl_sources_added,
            "failed": len(failed),
        },
    )
    manifest_note = _crawl_manifest_note(
        crawl_id,
        query,
        crawl_started_iso,
        fetched,
        failed,
        search_errors,
        research_plan,
        subcrawl_plan,
        trace,
        tool_trace,
    )
    manifest_stored, manifest_msg = _store_rag_note(manifest_note, config)
    _trace(
        trace,
        "manifest.store",
        {"stored": manifest_stored, "rag_id": _stored_id(manifest_msg), "message": manifest_msg},
    )
    _tool_trace(
        tool_trace,
        "rag.speichern",
        "OK" if manifest_stored else "FAIL",
        {
            "source_url": "DEEPDIVE_CRAWL_MANIFEST",
            "rag_id": _stored_id(manifest_msg),
            "message": manifest_msg,
        },
    )

    lines = [
        "DEEPDIVE_CRAWL",
        f"crawl_id: {crawl_id}",
        f"crawl_started_at_utc: {crawl_started_iso}",
        f"query: {query}",
        f"profile: {profile}",
        f"impact_languages: {', '.join(item.get('language', '') for item in research_plan.get('impact_plan', [])) or '-'}",
        f"impact_regions: {', '.join(_unique([item.get('country', '') for item in research_plan.get('impact_plan', []) if item.get('country')])) or '-'}",
        f"branch_queries: {', '.join(research_plan.get('branch_queries', [])[:8]) or '-'}",
        f"searched: {', '.join(executed_searches)}",
        f"candidates: {len(candidates)}",
        f"seed_sources: {len(selected)}",
        f"followup_links_queued: {followed_links}",
        f"derived_queries_run: {derived_queries_done}",
        f"derived_sources_queued: {derived_sources_added}",
        f"subcrawls_planned: {len(subcrawl_plan)}",
        f"subcrawl_sources_added: {subcrawl_sources_added}",
        f"sources_fetched: {len(fetched)}/{max_total_pages}",
        f"rag_pool: {str(config.get('rag_pool') or 'DeepDive')}",
        f"manifest: {manifest_msg}",
        f"external_packets: {len(external_packets)}",
        "",
        "Quellen (Seeds + Follow-ups):",
    ]
    if fetched:
        for idx, item in enumerate(fetched, 1):
            dates = "; ".join(item["dates"][:5]) if item["dates"] else "kein Datum erkannt"
            lines.append(
                f"{idx}. role={item.get('page_role', 'source')} | branch={item.get('branch_name') or '-'} | subcrawl={item.get('subcrawl_id') or '-'} {item.get('subcrawl_topic') or ''} | perspective={item.get('perspective_role') or '-'} {item.get('source_country') or ''}/{item.get('source_language') or ''} | depth={item['depth']} {item['discovery_method']} | score={item['relevance_score']} | {item['reliability']} | {item['recency_label']} | {item['title']} | {item['url']} | dates: {dates} | {item['storage_msg']}"
            )
    else:
        lines.append("- keine Quelle erfolgreich verarbeitet")

    if failed:
        lines.extend(["", "Fehler/ausgelassen:"])
        lines.extend(f"- {entry}" for entry in failed[:8])
    if search_errors:
        lines.extend(["", "Suchfehler:"])
        lines.extend(f"- {entry}" for entry in search_errors[:4])
    if external_packets:
        lines.extend(["", "Externe DeepDive-Pakete:"])
        for tool, url_count, stored, storage_msg in external_packets[:6]:
            lines.append(f"- {tool}: urls={url_count} rag_stored={stored} {storage_msg}")

    lines.extend(["", "DEEPDIVE_TOOL_TRACE:"])
    lines.extend(_tool_trace_lines(tool_trace))

    lines.extend(["", "DEEPDIVE_TRACE:"])
    lines.extend(_trace_lines(trace))

    lines.extend(
        [
            "",
            f"NEXT_STEP: Jetzt deepdive.pack({crawl_id}) ausfuehren, danach deepdive.blocks({crawl_id}). Erst aus den vorbereiteten Bausteinen das Lagebild bauen.",
            "WICHTIG: Nutze die RAG-Treffer als Arbeitsbasis, aber bewerte sie: Ereignisse, Akteure, Claims, Leads, Kausalketten, Perspektiven/Sprache/Land, Alter, Relevanz und Widersprueche. Social/Kommentar-Signale sind Leads, keine Belege.",
        ]
    )

    return {"success": bool(fetched), "data": "\n".join(lines)}


def _quick_config(config):
    cfg = dict(config or {})
    quick_sources = _clamp_int(cfg.get("quick_max_sources"), 4, 1, 8)
    quick_queries = _clamp_int(cfg.get("quick_max_search_queries"), 3, 1, 6)
    quick_pages = _clamp_int(cfg.get("quick_max_total_pages"), 6, quick_sources, 12)
    cfg["max_sources"] = quick_sources
    cfg["max_search_queries"] = quick_queries
    cfg["parallel_search_workers"] = min(_clamp_int(cfg.get("parallel_search_workers"), 6, 1, 10), quick_queries)
    cfg["parallel_fetch_workers"] = min(_clamp_int(cfg.get("parallel_fetch_workers"), 4, 1, 12), quick_pages)
    cfg["max_total_pages"] = quick_pages
    cfg["max_follow_links_per_source"] = min(_clamp_int(cfg.get("max_follow_links_per_source"), 3, 0, 6), 1)
    cfg["max_depth"] = min(_clamp_int(cfg.get("max_depth"), 2, 0, 2), 1)
    cfg["max_derived_queries"] = min(_clamp_int(cfg.get("max_derived_queries"), 4, 0, 8), 1)
    cfg["python_timeout_s"] = _clamp_int(cfg.get("quick_python_timeout_s"), 75, 20, 120)
    cfg["reddit_max_threads"] = min(_clamp_int(cfg.get("reddit_max_threads"), 3, 0, 6), 2)
    cfg["grok_search_max_sources"] = min(_clamp_int(cfg.get("grok_search_max_sources"), 8, 0, 20), 4)
    cfg["_deepdive_profile"] = "quick"
    return cfg


def _plan_subcrawls(query, fetched, research_plan, max_subcrawls=4, min_score=6):
    """Return scored side-topic candidates; top items with status=run are fetched."""
    max_subcrawls = max(0, int(max_subcrawls or 0))
    if max_subcrawls <= 0:
        return []
    corpus = _collapse_ws(
        " ".join(
            [query]
            + [str(item.get("title") or "") for item in fetched or []]
            + [str(item.get("analysis_text") or "") for item in fetched or []]
            + [str(item.get("branch_name") or "") for item in fetched or []]
        )
    ).lower()
    low = (query + " " + corpus[:5000]).lower()
    base_terms = set(_important_words(query, 14))
    candidates = []

    def add(topic, search_query, reason, branch_name="", base_score=0):
        search_query = _collapse_ws(search_query)
        if not search_query:
            return
        terms = _important_words(search_query, 14)
        side_terms = [term for term in terms if term not in base_terms]
        score = int(base_score or 0)
        score += sum(3 for term in side_terms if term in corpus)
        score += sum(1 for term in terms if term in corpus)
        if branch_name and branch_name in corpus:
            score += 4
        if any(word in search_query.lower() for word in ("nvidia", "tsmc", "huawei", "asml", "semiconductor", "chip", "export control")):
            if any(word in low for word in ("china", "taiwan", "trump", "handelskrieg", "trade war", "tariff", "zoll")):
                score += 20
        if any(word in search_query.lower() for word in ("rubio", "vance", "bessent", "lutnick", "greer", "xi jinping")):
            if any(word in low for word in ("china", "taiwan", "trump")):
                score += 16
        if any(word in search_query.lower() for word in ("japan", "south korea", "philippines", "alliance", "arms package")):
            if "taiwan" in low or "china" in low:
                score += 12
        candidates.append(
            {
                "topic": topic,
                "query": search_query,
                "reason": reason,
                "branch": branch_name or topic,
                "score": score,
            }
        )

    trade_or_chip_context = (
        any(word in low for word in ("handelskrieg", "trade war", "tariff", "zoll", "export control", "export controls", "sanction", "sanktion"))
        or any(word in low for word in ("nvidia", "tsmc", "huawei", "asml", "semiconductor", "chip", "ai compute", "rare earth", "supply chain", "lieferkette"))
    )
    if trade_or_chip_context and any(word in low for word in ("china", "taiwan", "xi", "trump")):
        add(
            "Chipkrieg / AI-Compute",
            "Trump China Nvidia TSMC Huawei ASML export controls AI chips 2026",
            "Strukturell hoher Zusammenhang: China/Taiwan/Trump laeuft ueber Chips, KI-Compute und Exportkontrollen.",
            "subcrawl_chip_war",
            32,
        )
        add(
            "Politiker- und Verhandlergraph",
            "Trump Xi Jinping Marco Rubio JD Vance Scott Bessent Howard Lutnick Jamieson Greer China Taiwan 2026",
            "Politische Hebel und Aussagen koennen die kausale Linie hinter Handel, Taiwan und Sanktionen erklaeren.",
            "subcrawl_policy_actors",
            28,
        )
        add(
            "Taiwan und Allianzen",
            "Trump Xi Taiwan arms package Japan South Korea Philippines US China security 2026",
            "Taiwan ist nicht nur bilaterales Thema, sondern haengt an Waffenpaketen, Allianzen und regionaler Abschreckung.",
            "subcrawl_taiwan_alliances",
            22,
        )
        add(
            "Zoelle, Lieferketten und Seltene Erden",
            "Trump China tariffs rare earths supply chains semiconductors export controls 2026",
            "Handelskrieg wirkt kausal ueber Lieferketten, Seltene Erden, Zoelle und Tech-Exportkontrollen.",
            "subcrawl_supply_chain",
            20,
        )

    uap_context = any(word in low for word in ("uap", "ufo", "unidentified anomalous", "unidentified aerial", "alien", "aliens", "disclosure", "aaro", "pursue"))
    if uap_context:
        add(
            "Offizielle UAP-Release-Quellen",
            "war.gov UFO PURSUE AARO UAP release records May 2026 official",
            "Primaerquellen und offizielle Release-Daten muessen vor Meta-Kommentaren geprueft werden.",
            "subcrawl_uap_official_records",
            30,
        )
        add(
            "Internationale UAP-Behoerden und Archive",
            "France GEIPAN UK UFO files Japan UAP committee China UAP Russia Soviet UFO archives",
            "Internationale Kontraste sollen ueber Laender/Behoerden statt ueber US-only Narrative laufen.",
            "subcrawl_uap_international_archives",
            24,
        )
        add(
            "Hearings, Whistleblower und Meldewege",
            "David Grusch Ryan Graves AARO UAP reporting Congress hearing non-human biologics source",
            "Whistleblower-Claims muessen als Aussagen/Behauptungen markiert und gegen belastbare Berichte abgegrenzt werden.",
            "subcrawl_uap_whistleblower_reporting",
            22,
        )
        add(
            "Wissenschaftliche Kritik und Datenqualitaet",
            "NASA AARO SCU UAP scientific analysis transparency data quality release 2026",
            "Kritische Einordnung der Datenqualitaet verhindert, dass Disclosure-PR als Beweis missverstanden wird.",
            "subcrawl_uap_science_skepticism",
            20,
        )

    for item in (research_plan or {}).get("branch_plan", []) or []:
        branch = item.get("branch") or "branch"
        add(
            branch,
            item.get("query") or "",
            item.get("reason") or "Branch-Kandidat aus Research-Plan",
            branch,
            6 if branch.startswith(("chip_", "ai_", "china_", "taiwan_", "sanctions_")) else 0,
        )

    deduped = []
    seen = set()
    for item in sorted(candidates, key=lambda entry: entry.get("score", 0), reverse=True):
        key = _query_key(item.get("query") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    max_candidates = max(max_subcrawls + 4, 8)
    out = deduped[:max_candidates]
    run_count = 0
    family_runs = {}
    for item in out:
        family = _subcrawl_family(item.get("query") or "", item.get("branch") or "")
        family_limit = 2 if family == "chips" else 1 if family in {"policy_actors", "alliances", "supply_chain"} else max_subcrawls
        worth_followup = int(item.get("score") or 0) >= int(min_score or 0)
        can_run = (
            run_count < max_subcrawls
            and worth_followup
            and family_runs.get(family, 0) < family_limit
        )
        item["family"] = family
        item["worth_followup"] = worth_followup
        item["status"] = "run" if can_run else "suggested"
        if can_run:
            item["recommendation"] = "run_small_subcrawl"
            item["next_step"] = "Wurde als Side-Crawl ausgefuehrt und soll in die Synthese einfliessen."
        elif worth_followup:
            item["recommendation"] = "suggest_full_followup_crawl"
            item["next_step"] = "Kausal wertvoll, aber nicht im aktuellen Side-Crawl-Budget; als eigener Anschluss-Crawl vorschlagen."
        else:
            item["recommendation"] = "monitor_only"
            item["next_step"] = "Nur als Lead markieren; aktuell zu schwach fuer eigenen Crawl."
        if can_run:
            run_count += 1
            family_runs[family] = family_runs.get(family, 0) + 1
    return out


def _subcrawl_family(query, branch):
    low = f"{query} {branch}".lower()
    if any(word in low for word in ("tariff", "tariffs", "zoll", "rare earth", "supply chain", "liefer")):
        return "supply_chain"
    if any(word in low for word in ("nvidia", "tsmc", "huawei", "asml", "chip", "semiconductor", "compute", "export control")):
        return "chips"
    if any(word in low for word in ("rubio", "vance", "bessent", "lutnick", "greer", "cabinet", "actor", "policy")):
        return "policy_actors"
    if any(word in low for word in ("taiwan", "arms package", "japan", "south korea", "philippines", "alliance")):
        return "alliances"
    return "general"


def _run_subcrawls(
    crawl_id,
    query,
    subcrawl_plan,
    fetched,
    known_urls,
    config,
    timeout_s,
    max_chars,
    allow_private,
    research_plan,
    trace,
    tool_trace,
    deadline,
    sources_per_topic,
):
    added = 0
    failures = []
    run_items = [item for item in subcrawl_plan or [] if item.get("status") == "run"]
    for idx, plan in enumerate(run_items, 1):
        if time.monotonic() > deadline:
            failures.append("Subcrawl-Zeitbudget erreicht")
            break
        subcrawl_id = f"sc{idx}"
        subquery = plan.get("query") or plan.get("topic") or query
        search_tool = _configured_search_tool_name(config)
        try:
            _trace(trace, "subcrawl.search.start", {"subcrawl_id": subcrawl_id, "topic": plan.get("topic"), "query": subquery, "score": plan.get("score")})
            search_tool, raw_results, search_note = _search_web(subquery, 6, timeout_s, config)
            candidates = []
            for result in raw_results:
                url = _clean_url(result.get("url") or "")
                if not url or url in known_urls:
                    continue
                if not _is_allowed_http_url(url, allow_private):
                    continue
                if _skip_link(url, f"{result.get('title', '')} {result.get('snippet', '')}"):
                    continue
                result["url"] = url
                result["search_query"] = subquery
                result["depth"] = 0
                result["parent_url"] = ""
                result["discovery_method"] = "subcrawl"
                result["discovery_reason"] = f"Subcrawl {subcrawl_id}: {plan.get('reason') or plan.get('topic') or subquery}"
                result["branch_name"] = plan.get("branch") or plan.get("topic") or "subcrawl"
                result["branch_reason"] = plan.get("reason") or ""
                result["subcrawl_id"] = subcrawl_id
                result["subcrawl_topic"] = plan.get("topic") or subquery
                result["subcrawl_reason"] = plan.get("reason") or ""
                result["_analysis_query"] = subquery
                candidates.append(result)
            selected = _select_sources(candidates, sources_per_topic, subquery, min_branch_sources=0)
            _trace(
                trace,
                "subcrawl.search.done",
                {
                    "subcrawl_id": subcrawl_id,
                    "query": subquery,
                    "engine": search_tool,
                    "results": len(raw_results),
                    "selected": len(selected),
                    "urls": [item.get("url") for item in selected],
                    **({"note": search_note} if search_note else {}),
                },
            )
            _tool_trace(
                tool_trace,
                search_tool,
                "OK",
                {"phase": "subcrawl", "subcrawl_id": subcrawl_id, "query": subquery, "selected": len(selected)},
            )
        except Exception as exc:
            failures.append(f"Subcrawl {subcrawl_id} Suche fehlgeschlagen: {exc}")
            _trace(trace, "subcrawl.search.fail", {"subcrawl_id": subcrawl_id, "query": subquery, "error": str(exc)})
            _tool_trace(tool_trace, search_tool, "FAIL", {"phase": "subcrawl", "subcrawl_id": subcrawl_id, "query": subquery, "error": str(exc)})
            continue

        for result in selected:
            if time.monotonic() > deadline:
                failures.append("Subcrawl-Zeitbudget erreicht")
                return added, failures
            source, error = _fetch_store_subcrawl_source(
                crawl_id,
                query,
                result,
                config,
                timeout_s,
                max_chars,
                research_plan,
                trace,
                tool_trace,
            )
            if error:
                failures.append(error)
                continue
            if source:
                known_urls.add(source["url"])
                fetched.append(source)
                added += 1
    return added, failures


def _fetch_store_subcrawl_source(crawl_id, query, result, config, timeout_s, max_chars, research_plan, trace, tool_trace):
    url = result.get("url") or ""
    subcrawl_id = result.get("subcrawl_id") or "subcrawl"
    analysis_query = result.get("_analysis_query") or result.get("search_query") or query
    try:
        _trace(trace, "subcrawl.fetch.start", {"subcrawl_id": subcrawl_id, "url": url, "query": analysis_query})
        is_youtube = _is_youtube_url(url)
        page_html = ""
        if is_youtube and bool(config.get("enable_youtube_transcripts", True)):
            page = _youtube_transcript_page(url, analysis_query, config, max_chars)
        else:
            page_html = _fetch_url(url, timeout_s)
            page = _parse_page(url, page_html)
        if not page["text"] and not page["title"]:
            return None, f"Subcrawl {subcrawl_id}: {url} -> kein lesbarer Text"
        if _low_value_page(url, page):
            return None, f"Subcrawl {subcrawl_id}: {url} -> low_value_page"
        relevance_score = _relevance_score(analysis_query, result, page)
        if relevance_score <= 0:
            return None, f"Subcrawl {subcrawl_id}: {url} -> relevance_score=0"
        reliability = _reliability_label(url)
        recency = _recency_label(page.get("dates") or [])
        key_passages = _key_passages(analysis_query, page.get("text") or "", page.get("dates") or [])
        causality_hints = _causality_hints(analysis_query, page.get("text") or "", key_passages)
        claim_hints = _claim_hints(analysis_query, page.get("text") or "", key_passages)
        event_hints = _event_hints(analysis_query, page.get("text") or "", page.get("dates") or [])
        lead_hints = _lead_hints(analysis_query, page.get("text") or "", page.get("links") or [])
        contrast_hints = _contrast_hints(analysis_query, page.get("text") or "", key_passages)
        perspective = _source_perspective(result, page, research_plan)
        page_role = _page_role(url, result, page, analysis_query)
        result["_relevance_score"] = relevance_score
        result["_reliability"] = reliability
        result["_recency_label"] = recency
        result["_key_passages"] = key_passages
        result["_causality_hints"] = causality_hints
        result["_claim_hints"] = claim_hints
        result["_event_hints"] = event_hints
        result["_lead_hints"] = lead_hints
        result["_contrast_hints"] = contrast_hints
        result["_perspective"] = perspective
        result["_page_role"] = page_role
        note = _crawl_note(crawl_id, query, result, page, max_chars)
        stored, storage_msg = _store_rag_note(note, config)
        _trace(
            trace,
            "subcrawl.fetch.done",
            {
                "subcrawl_id": subcrawl_id,
                "url": url,
                "title": page["title"] or result.get("title") or "",
                "html_chars": len(page_html),
                "text_chars": len(page["text"]),
                "relevance_score": relevance_score,
                "stored": stored,
                "rag_id": _stored_id(storage_msg),
            },
        )
        _tool_trace(tool_trace, "http.get", "OK", {"phase": "subcrawl", "subcrawl_id": subcrawl_id, "url": url, "text_chars": len(page["text"])})
        _tool_trace(tool_trace, "rag.speichern", "OK" if stored else "FAIL", {"phase": "subcrawl", "source_url": url, "rag_id": _stored_id(storage_msg), "message": storage_msg})
        return (
            {
                "url": url,
                "title": page["title"] or result.get("title") or "(kein Titel)",
                "dates": page["dates"],
                "depth": 0,
                "discovery_method": "subcrawl",
                "parent_url": "",
                "relevance_score": relevance_score,
                "reliability": reliability,
                "recency_label": recency,
                "source_language": perspective.get("language", ""),
                "source_country": perspective.get("country", ""),
                "perspective_role": perspective.get("role", ""),
                "branch_name": result.get("branch_name") or "",
                "branch_reason": result.get("branch_reason") or "",
                "subcrawl_id": subcrawl_id,
                "subcrawl_topic": result.get("subcrawl_topic") or analysis_query,
                "subcrawl_reason": result.get("subcrawl_reason") or "",
                "page_role": page_role,
                "stored": stored,
                "storage_msg": storage_msg,
                "chars": len(page["text"]),
                "analysis_text": _collapse_ws(" ".join([page["title"] or result.get("title") or ""] + key_passages[:4] + causality_hints[:3] + claim_hints[:3]))[:3000],
            },
            None,
        )
    except Exception as exc:
        _trace(trace, "subcrawl.fetch.fail", {"subcrawl_id": subcrawl_id, "url": url, "error": str(exc)})
        _tool_trace(tool_trace, "http.get", "FAIL", {"phase": "subcrawl", "subcrawl_id": subcrawl_id, "url": url, "error": str(exc)})
        return None, f"Subcrawl {subcrawl_id}: {url} -> {exc}"


def _load_deepdive_entries(needle, config):
    if not isinstance(config, dict):
        return {"ok": False, "error": "Tool-Konfig fehlt."}
    data_dir = str(config.get("data_dir") or "").strip()
    pool = str(config.get("rag_pool") or "DeepDive").strip() or "DeepDive"
    safe_pool = _safe_id(pool) or "DeepDive"
    if not data_dir:
        return {"ok": False, "error": "data_dir fehlt."}
    rag_dir = os.path.join(data_dir, "rag", safe_pool)
    if not os.path.isdir(rag_dir):
        return {"ok": False, "error": f"RAG Pool '{safe_pool}' nicht gefunden."}

    wanted_crawl_id = _extract_crawl_id(needle)
    terms = _query_terms(needle)
    max_scan = _clamp_int(config.get("pack_max_scan"), 2000, 50, 10000)
    matches = []
    try:
        files = sorted(
            (p for p in os.scandir(rag_dir) if p.is_file() and p.name.endswith(".json")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:max_scan]
    except Exception as exc:
        return {"ok": False, "error": f"RAG lesen fehlgeschlagen: {exc}"}

    for path in files:
        try:
            with open(path.path, "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except Exception:
            continue
        text = str(entry.get("text") or "")
        crawl_id = str(entry.get("crawl_id") or _line_value(text, "crawl_id") or "")
        score = 0
        if wanted_crawl_id:
            if crawl_id != wanted_crawl_id:
                continue
            score = 1000
        elif terms:
            haystack = " ".join(
                [
                    text[:12000],
                    str(entry.get("source_title") or ""),
                    str(entry.get("source_url") or ""),
                    str(entry.get("keywords") or ""),
                ]
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if score <= 0:
                continue
        else:
            continue
        matches.append((score, path.stat().st_mtime, entry))

    if not matches:
        target = wanted_crawl_id or needle
        return {"ok": False, "error": f"Keine DeepDive-Notizen fuer '{target}' im Pool '{safe_pool}' gefunden."}

    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    entries = [entry for _, _, entry in matches]
    if not wanted_crawl_id:
        wanted_crawl_id = _dominant_crawl_id(entries)
        if wanted_crawl_id:
            entries = [
                entry
                for entry in entries
                if str(entry.get("crawl_id") or _line_value(str(entry.get("text") or ""), "crawl_id")) == wanted_crawl_id
            ]
    return {"ok": True, "pool": safe_pool, "crawl_id": wanted_crawl_id, "entries": entries}


def _blocks(needle, config):
    loaded = _load_deepdive_entries(needle, config)
    if not loaded.get("ok"):
        return {"success": False, "data": loaded.get("error") or "DeepDive-Blocks konnten nicht geladen werden."}

    entries = list(loaded.get("entries") or [])
    safe_pool = loaded.get("pool") or "DeepDive"
    crawl_id = loaded.get("crawl_id") or _extract_crawl_id(needle) or "(query-match)"
    max_sources = _clamp_int(config.get("blocks_max_sources"), 8, 3, 16)
    manifest = next((entry for entry in entries if str(entry.get("text") or "").startswith("DEEPDIVE_CRAWL_MANIFEST")), None)
    all_source_entries = [entry for entry in entries if str(entry.get("text") or "").startswith("DEEPDIVE_CRAWL_NOTE")]
    external_entries = [entry for entry in entries if str(entry.get("text") or "").startswith("DEEPDIVE_EXTERNAL_PACKET")]
    all_source_entries.sort(key=_pack_entry_sort_key, reverse=True)
    subcrawl_entries = [
        entry
        for entry in all_source_entries
        if (entry.get("subcrawl_id") or _line_value(str(entry.get("text") or ""), "subcrawl_id"))
    ]
    source_entries = []
    seen_ids = set()
    for entry in subcrawl_entries[: min(6, max_sources)] + all_source_entries:
        entry_id = entry.get("id") or _line_value(str(entry.get("text") or ""), "source_url")
        if entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)
        source_entries.append(entry)
        if len(source_entries) >= max_sources:
            break
    manifest_text = str(manifest.get("text") or "") if manifest else ""
    topic = _line_value(manifest_text, "topic") if manifest else ""
    topic = topic or str(needle)
    branch_plan_lines = _numbered_section_lines(manifest_text, "branch_plan", 10)
    subcrawl_plan_lines = _numbered_section_lines(manifest_text, "subcrawl_plan", 12)
    branch_queries = [item.strip() for item in (_line_value(manifest_text, "branch_queries") or "").split("|") if item.strip()]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def add_unique(target, value, limit):
        value = _collapse_ws(str(value or ""))
        if not value or _is_boilerplate_line(value):
            return
        key = value.lower()[:220]
        if key in {item.lower()[:220] for item in target}:
            return
        if len(target) < limit:
            target.append(value)

    source_lines = ["<quellen>"]
    timeline = []
    claims = []
    causal = []
    contrasts = []
    leads = []
    branch_sources = []
    subcrawl_sources = []
    seen_source_urls = set()

    for idx, entry in enumerate(source_entries, 1):
        text = str(entry.get("text") or "")
        title = entry.get("source_title") or _line_value(text, "source_title") or "(kein Titel)"
        url = entry.get("source_url") or _line_value(text, "source_url") or ""
        if url:
            seen_source_urls.add(url)
        reliability = _line_value(text, "source_reliability") or "unknown_check_needed"
        recency = entry.get("recency_label") or _line_value(text, "recency_label") or "unknown"
        relevance = entry.get("relevance_score") or _line_value(text, "relevance_score") or "?"
        source_type = _line_value(text, "source_type") or "source"
        language = entry.get("source_language") or _line_value(text, "source_language") or "?"
        country = entry.get("source_country") or _line_value(text, "source_country") or "?"
        perspective = entry.get("perspective_role") or _line_value(text, "perspective_role") or "?"
        branch_name = entry.get("branch_name") or _line_value(text, "branch_name") or ""
        subcrawl_id = entry.get("subcrawl_id") or _line_value(text, "subcrawl_id") or ""
        subcrawl_topic = entry.get("subcrawl_topic") or _line_value(text, "subcrawl_topic") or ""
        captured = entry.get("captured_at_utc") or _line_value(text, "captured_at_utc") or entry.get("timestamp") or "?"
        date_hints = _line_value(text, "date_hints")
        passages = _section_bullets(text, "key_passages", 2) or _source_text_lines(text, 2)
        used_for = passages[0] if passages else title
        source_lines.append(
            "- q{idx} | rag_id: {rag_id} | fundort: {url} | titel: {title} | typ: {source_type} | branch: {branch_name} | subcrawl: {subcrawl_id} {subcrawl_topic} | "
            "stand: {stand} | abgerufen_utc: {captured} | relevanz: {relevance} | reliability: {reliability} | "
            "perspektive: {perspective} {country}/{language} | genutzt_fuer: {used_for}".format(
                idx=idx,
                rag_id=str(entry.get("id") or "")[:8],
                url=url,
                title=_collapse_ws(title)[:180],
                source_type=source_type,
                branch_name=_collapse_ws(branch_name or "-")[:80],
                subcrawl_id=_collapse_ws(subcrawl_id or "-")[:20],
                subcrawl_topic=_collapse_ws(subcrawl_topic or "")[:100],
                stand=_collapse_ws(date_hints or recency)[:180],
                captured=str(captured)[:40],
                relevance=relevance,
                reliability=_collapse_ws(reliability)[:80],
                perspective=_collapse_ws(perspective)[:60],
                country=_collapse_ws(country)[:30],
                language=_collapse_ws(language)[:20],
                used_for=_collapse_ws(used_for)[:220],
            )
        )
        if branch_name:
            add_unique(branch_sources, f"{branch_name}: q{idx} | {title} | {url} | {used_for}", 16)
        if subcrawl_id or subcrawl_topic:
            add_unique(subcrawl_sources, f"{subcrawl_id or 'subcrawl'} | {subcrawl_topic or branch_name}: q{idx} | {title} | {url} | {used_for}", 16)
        for item in _section_bullets(text, "event_hints", 3):
            add_unique(timeline, f"q{idx}: {item}", 18)
        if date_hints:
            add_unique(timeline, f"q{idx}: Datumshinweise: {date_hints}", 18)
        for item in _section_bullets(text, "claim_hints", 3):
            add_unique(claims, f"q{idx}: {item}", 24)
        if not _section_bullets(text, "claim_hints", 1):
            for item in passages[:2]:
                add_unique(claims, f"q{idx}: {item}", 24)
        for item in _section_bullets(text, "causality_hints", 3):
            add_unique(causal, f"q{idx}: {item}", 18)
        for item in _section_bullets(text, "contrast_hints", 2):
            add_unique(contrasts, f"q{idx}: {item}", 16)
        if perspective != "?" or language != "?" or country != "?":
            add_unique(contrasts, f"q{idx}: Perspektive/Region/Sprache: {perspective} {country}/{language}", 16)
        for item in _section_bullets(text, "lead_hints", 3):
            add_unique(leads, f"q{idx}: {item}", 20)

    external_urls_added = 0
    for entry in external_entries[:4]:
        if external_urls_added >= 4:
            break
        text = str(entry.get("text") or "")
        tool = _line_value(text, "tool") or entry.get("source_title") or "external"
        for url in _extract_urls(text)[:8]:
            if external_urls_added >= 4:
                break
            if url in seen_source_urls:
                continue
            seen_source_urls.add(url)
            external_urls_added += 1
            source_lines.extend(
                [
                    "- ext{idx} | rag_id: {rag_id} | fundort: {url} | titel: {tool} | typ: external_packet_url | "
                    "stand: {stand} | reliability: lead_check_needed | genutzt_fuer: Lead/Kommentar-/Suchsignal; vor starken Behauptungen gegen Primaerquellen pruefen".format(
                        idx=external_urls_added,
                        rag_id=str(entry.get("id") or "")[:8],
                        url=url,
                        tool=_collapse_ws(tool)[:160],
                        stand=str(_line_value(text, "captured_at_utc") or entry.get("timestamp") or "?")[:40],
                    ),
                ]
            )
        excerpt = _after_marker(text, "packet_text:", 900)
        if excerpt:
            add_unique(leads, f"{tool}: {_collapse_ws(excerpt)[:600]}", 20)

    source_lines.append("</quellen>")
    if not timeline:
        timeline.append("Keine belastbare Timeline aus den Quellen extrahiert; nur vorsichtig nach Datumshinweisen berichten.")
    if not claims:
        claims.append("Keine expliziten Claims extrahiert; aus key_passages vorsichtig zusammenfassen.")
    if not causal:
        causal.append("Kausalitaeten/Mechanismen sind nicht belastbar extrahiert; nur als behauptete Zusammenhaenge markieren.")
    if not contrasts:
        contrasts.append("Kein klarer Perspektivenkontrast extrahiert; fehlende Laender-/Sprachsicht als Luecke markieren.")
    if not leads:
        leads.append("Keine offenen Leads extrahiert.")

    lines = [
        "DEEPDIVE_BLOCKS",
        f"pool: {safe_pool}",
        f"crawl_id: {crawl_id}",
        f"topic: {topic}",
        f"prepared_at_utc: {now}",
        f"sources_prepared: {len(source_entries)}",
        f"external_urls_added: {external_urls_added}",
        "",
        "PLACEHOLDER_MAP:",
        "{{quellen}} -> QUELLEN_BLOCK",
        "{{timeline}} -> TIMELINE_BLOCK",
        "{{claims}} -> CLAIMS_BLOCK",
        "{{kausalitaeten}} -> CAUSALITY_BLOCK",
        "{{kontraste}} -> CONTRAST_BLOCK",
        "{{branching}} -> BRANCHING_CONTEXT_BLOCK",
        "{{subcrawls}} -> SUBCRAWL_PLAN_BLOCK + SUBCRAWL_RESULTS_BLOCK",
        "{{leads}} -> LEADS_BLOCK",
        "",
        "QUELLEN_BLOCK:",
        "\n".join(source_lines),
        "",
        "TIMELINE_BLOCK:",
    ]
    lines.extend("- " + item[:260] for item in timeline[:8])
    lines.extend(["", "CLAIMS_BLOCK:"])
    lines.extend("- " + item[:260] for item in claims[:10])
    lines.extend(["", "CAUSALITY_BLOCK:"])
    lines.extend("- " + item[:260] for item in causal[:8])
    lines.extend(["", "CONTRAST_BLOCK:"])
    lines.extend("- " + item[:260] for item in contrasts[:8])
    lines.extend(["", "BRANCHING_CONTEXT_BLOCK:"])
    if branch_plan_lines:
        lines.append("- Branch-Suchplan:")
        lines.extend("- " + item[:320] for item in branch_plan_lines[:8])
    elif branch_queries:
        lines.extend("- branch_query: " + item[:280] for item in branch_queries[:8])
    else:
        lines.append("- Kein separater Branch-Plan im Manifest; fehlende Nachbarbegriffe/Akteursnetzwerke als Luecke markieren.")
    if branch_sources:
        lines.append("- Branch-Quellen, die tatsaechlich gecrawlt wurden:")
        lines.extend("- " + item[:340] for item in branch_sources[:10])
    else:
        lines.append("- Keine explizit markierten Branch-Quellen im Crawl; das ist eine Recherche-Luecke und muss als solche benannt werden.")
    lines.extend(["", "SUBCRAWL_PLAN_BLOCK:"])
    lines.append("- Bedeutung: status=run wurde als Side-Crawl geholt; recommendation=suggest_full_followup_crawl ist ein Vorschlag fuer einen eigenen Anschluss-Crawl.")
    if subcrawl_plan_lines:
        lines.extend("- " + item[:420] for item in subcrawl_plan_lines[:10])
    else:
        lines.append("- Keine Subcrawl-Kandidaten im Manifest.")
    lines.extend(["", "SUBCRAWL_RESULTS_BLOCK:"])
    if subcrawl_sources:
        lines.extend("- " + item[:420] for item in subcrawl_sources[:12])
    else:
        lines.append("- Keine ausgefuehrten Subcrawl-Quellen in den vorbereiteten Quellen.")
    lines.extend(["", "LEADS_BLOCK:"])
    lines.extend("- " + item[:260] for item in leads[:8])
    lines.extend(
        [
            "",
            "SYNTHESIS_INSTRUCTION:",
            "Nutze diese Blocks als harte Bausteine. Ersetze Platzhalter wie {{quellen}} nicht mit frei erfundenen Quellen, sondern mit dem QUELLEN_BLOCK. Baue daraus Lagebild, Chronologie, Akteure, Claims, Kausalitaeten, Subcrawl-Plan/Resultate, empfohlene Anschluss-Crawls, Branching-Kontext, Kontraste, Unsicherheiten und offene Leads. Keine neue Quelle behaupten, die nicht im Quellenblock oder in der Tool-Evidenz steht.",
        ]
    )
    result = "\n".join(lines)
    if _cfg_bool(config.get("store_blocks_in_rag"), True):
        note = "\n".join(
            [
                "DEEPDIVE_PREPARED_BLOCKS",
                f"crawl_id: {crawl_id}",
                f"captured_at_utc: {now}",
                f"topic: {topic}",
                "source_title: DeepDive prepared blocks",
                "source_type: prepared_research_blocks",
                "source_text:",
                result,
            ]
        )
        stored, storage_msg = _store_rag_note(note, config)
        result += f"\n\nRAG_STORE: {storage_msg if stored else 'nicht gespeichert: ' + storage_msg}"
    return {"success": True, "data": result}


def _pack(needle, config):
    if not isinstance(config, dict):
        return {"success": False, "data": "Tool-Konfig fehlt."}
    data_dir = str(config.get("data_dir") or "").strip()
    pool = str(config.get("rag_pool") or "DeepDive").strip() or "DeepDive"
    safe_pool = _safe_id(pool) or "DeepDive"
    if not data_dir:
        return {"success": False, "data": "data_dir fehlt."}
    rag_dir = os.path.join(data_dir, "rag", safe_pool)
    if not os.path.isdir(rag_dir):
        return {"success": False, "data": f"RAG Pool '{safe_pool}' nicht gefunden."}

    wanted_crawl_id = _extract_crawl_id(needle)
    terms = _query_terms(needle)
    max_entries = _clamp_int(config.get("pack_max_entries"), 10, 1, 20)
    max_scan = _clamp_int(config.get("pack_max_scan"), 2000, 50, 10000)
    matches = []
    try:
        files = sorted(
            (p for p in os.scandir(rag_dir) if p.is_file() and p.name.endswith(".json")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:max_scan]
    except Exception as exc:
        return {"success": False, "data": f"RAG lesen fehlgeschlagen: {exc}"}

    for path in files:
        try:
            with open(path.path, "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except Exception:
            continue
        text = str(entry.get("text") or "")
        crawl_id = str(entry.get("crawl_id") or _line_value(text, "crawl_id") or "")
        score = 0
        if wanted_crawl_id:
            if crawl_id != wanted_crawl_id:
                continue
            score = 1000
        elif terms:
            haystack = " ".join(
                [
                    text[:12000],
                    str(entry.get("source_title") or ""),
                    str(entry.get("source_url") or ""),
                    str(entry.get("keywords") or ""),
                ]
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if score <= 0:
                continue
        matches.append((score, path.stat().st_mtime, entry))

    if not matches:
        target = wanted_crawl_id or needle
        return {"success": False, "data": f"Keine DeepDive-Notizen fuer '{target}' im Pool '{safe_pool}' gefunden."}

    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    entries = [entry for _, _, entry in matches]
    if not wanted_crawl_id:
        wanted_crawl_id = _dominant_crawl_id(entries)
        if wanted_crawl_id:
            entries = [entry for entry in entries if str(entry.get("crawl_id") or _line_value(str(entry.get("text") or ""), "crawl_id")) == wanted_crawl_id]

    manifest = next((entry for entry in entries if str(entry.get("text") or "").startswith("DEEPDIVE_CRAWL_MANIFEST")), None)
    source_entries = [entry for entry in entries if str(entry.get("text") or "").startswith("DEEPDIVE_CRAWL_NOTE")]
    external_entries = [entry for entry in entries if str(entry.get("text") or "").startswith("DEEPDIVE_EXTERNAL_PACKET")]
    source_entries.sort(key=_pack_entry_sort_key, reverse=True)
    source_entries = source_entries[:max_entries]
    external_entries = external_entries[: min(4, max_entries)]

    lines = [
        "DEEPDIVE_PACK",
        f"pool: {safe_pool}",
        f"input: {needle}",
        f"crawl_id: {wanted_crawl_id or '(query-match)'}",
        f"matched_entries: {len(entries)}",
        f"sources_in_pack: {len(source_entries)}",
        f"external_packets: {len(external_entries)}",
    ]
    if manifest:
        text = str(manifest.get("text") or "")
        lines.extend(
            [
                "",
                "Manifest:",
                f"- topic: {_line_value(text, 'topic')}",
                f"- started: {_line_value(text, 'crawl_started_at_utc')}",
                f"- impact_languages: {_line_value(text, 'impact_languages')}",
                f"- impact_regions: {_line_value(text, 'impact_regions')}",
                f"- branch_queries: {_line_value(text, 'branch_queries')}",
                f"- subcrawl_candidates: {_line_value(text, 'subcrawl_candidates')}",
                f"- subcrawls_run: {_line_value(text, 'subcrawls_run')}",
                f"- sources_fetched: {_line_value(text, 'sources_fetched')}",
                f"- failed_count: {_line_value(text, 'failed_count')}",
                f"- search_error_count: {_line_value(text, 'search_error_count')}",
            ]
        )
        subcrawl_plan_lines = _numbered_section_lines(text, "subcrawl_plan", 8)
        if subcrawl_plan_lines:
            lines.append("- subcrawl_plan:")
            for item in subcrawl_plan_lines:
                lines.append("  - " + item[:700])

    lines.extend(["", "Quellenpaket:"])
    if source_entries:
        for idx, entry in enumerate(source_entries, 1):
            text = str(entry.get("text") or "")
            title = entry.get("source_title") or _line_value(text, "source_title") or "(kein Titel)"
            url = entry.get("source_url") or _line_value(text, "source_url") or ""
            reliability = _line_value(text, "source_reliability")
            recency = entry.get("recency_label") or _line_value(text, "recency_label")
            relevance = entry.get("relevance_score") or _line_value(text, "relevance_score")
            source_type = _line_value(text, "source_type")
            language = entry.get("source_language") or _line_value(text, "source_language")
            country = entry.get("source_country") or _line_value(text, "source_country")
            perspective = entry.get("perspective_role") or _line_value(text, "perspective_role")
            branch_name = entry.get("branch_name") or _line_value(text, "branch_name")
            subcrawl_id = entry.get("subcrawl_id") or _line_value(text, "subcrawl_id")
            subcrawl_topic = entry.get("subcrawl_topic") or _line_value(text, "subcrawl_topic")
            date_hints = _line_value(text, "date_hints")
            lines.append(f"{idx}. score={relevance or '?'} | {source_type or 'source'} | branch={branch_name or '-'} | subcrawl={subcrawl_id or '-'} {subcrawl_topic or ''} | perspective={perspective or '-'} {country or ''}/{language or ''} | {reliability or 'reliability: unknown'} | {recency or 'recency: unknown'} | {title} | {url}")
            if date_hints:
                lines.append("   dates: " + date_hints[:500])
            passages = _section_bullets(text, "key_passages", 4)
            if not passages:
                passages = _source_text_lines(text, 3)
            if passages:
                lines.append("   key_points:")
                for passage in passages:
                    lines.append("   - " + passage[:600])
            for section, label, max_items in (
                ("event_hints", "events", 3),
                ("claim_hints", "claims", 3),
                ("causality_hints", "causal_links", 3),
                ("contrast_hints", "contrasts", 2),
                ("lead_hints", "leads_to_follow", 3),
            ):
                bullets = _section_bullets(text, section, max_items)
                if bullets:
                    lines.append(f"   {label}:")
                    for bullet in bullets:
                        lines.append("   - " + bullet[:600])
    else:
        lines.append("- keine einzelnen Crawl-Quellen gefunden")

    if external_entries:
        lines.extend(["", "Externe Pakete:"])
        for entry in external_entries:
            text = str(entry.get("text") or "")
            tool = _line_value(text, "tool") or entry.get("source_title") or "external"
            lines.append(f"- {tool}: urls_found={_line_value(text, 'urls_found')}")
            for url in _extract_urls(text)[:8]:
                lines.append(f"  - {url}")
            excerpt = _after_marker(text, "packet_text:", 1200)
            if excerpt:
                lines.append("  excerpt: " + _collapse_ws(excerpt)[:1200])

    lines.extend(
        [
            "",
            f"NEXT_STEP: Jetzt deepdive.blocks({wanted_crawl_id or needle}) ausfuehren. Erst danach synthetisieren, damit Quellen, Timeline, Claims, Kausalitaeten, Kontraste und Leads als vorbereitete Bausteine vorliegen.",
        ]
    )
    return {"success": True, "data": "\n".join(lines)}


def _prefetch_queue(crawl_queue, visited_urls, timeout_s, workers, deadline, remaining_pages, trace, tool_trace):
    if remaining_pages <= 1:
        return
    selected = []
    for item in crawl_queue:
        if len(selected) >= min(workers, remaining_pages):
            break
        url = item.get("url") or ""
        if not url or url in visited_urls:
            continue
        if "_prefetched_html" in item or "_prefetch_error" in item:
            continue
        selected.append(item)
    if len(selected) <= 1:
        return

    worker_count = max(1, min(int(workers or 1), len(selected)))
    _trace(trace, "fetch.prefetch_start", {"items": len(selected), "workers": worker_count, "urls": [item.get("url") for item in selected]})
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {executor.submit(_fetch_url, item.get("url") or "", timeout_s): item for item in selected}
        remaining_timeout = max(1, min(timeout_s + 3, deadline - time.monotonic()))
        try:
            for future in as_completed(future_map, timeout=remaining_timeout):
                item = future_map[future]
                url = item.get("url") or ""
                depth = int(item.get("depth") or 0)
                try:
                    html_text = future.result()
                    item["_prefetched_html"] = html_text
                    _trace(trace, "fetch.prefetch_done", {"url": url, "html_chars": len(html_text)})
                    _tool_trace(tool_trace, "http.prefetch", "OK", {"url": url, "depth": depth, "html_chars": len(html_text)})
                except Exception as exc:
                    item["_prefetch_error"] = str(exc)
                    _trace(trace, "fetch.prefetch_fail", {"url": url, "error": str(exc)})
                    _tool_trace(tool_trace, "http.prefetch", "FAIL", {"url": url, "depth": depth, "error": str(exc)})
        except FuturesTimeout:
            for future, item in future_map.items():
                if future.done():
                    continue
                future.cancel()
                url = item.get("url") or ""
                item["_prefetch_error"] = "Prefetch Zeitbudget erreicht"
                _trace(trace, "fetch.prefetch_timeout", {"url": url})
                _tool_trace(tool_trace, "http.prefetch", "TIMEOUT", {"url": url, "depth": int(item.get("depth") or 0)})


def _run_seed_searches(search_queries, timeout_s, config, deadline, workers, trace, tool_trace):
    queries = list(search_queries or [])
    if not queries:
        return []
    workers = max(1, min(int(workers or 1), len(queries)))
    default_tool = _configured_search_tool_name(config)
    results = []

    if workers <= 1:
        for query in queries:
            if time.monotonic() > deadline:
                results.append({"query": query, "tool": default_tool, "error": "Zeitbudget vor Ende der Seed-Suche erreicht"})
                _trace(trace, "crawl.deadline", {"stage": "seed_search"})
                break
            results.append(_run_one_seed_search(query, timeout_s, config, trace, tool_trace))
        return results

    _trace(trace, "search.parallel.start", {"queries": len(queries), "workers": workers, "engine": default_tool})
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {}
        for query in queries:
            if time.monotonic() > deadline:
                results.append({"query": query, "tool": default_tool, "error": "Zeitbudget vor Seed-Submit erreicht"})
                break
            _trace(trace, "search.start", {"query": query, "engine": default_tool, "parallel": True})
            future = executor.submit(_search_web, query, 10, timeout_s, config)
            future_map[future] = query
        remaining_timeout = max(1, deadline - time.monotonic())
        try:
            for future in as_completed(future_map, timeout=remaining_timeout):
                query = future_map[future]
                try:
                    tool, raw_results, search_note = future.result()
                    item = {"query": query, "tool": tool, "results": raw_results, "note": search_note}
                    results.append(item)
                    _trace(
                        trace,
                        "search.done",
                        {
                            "query": query,
                            "engine": tool,
                            "results": len(raw_results),
                            **({"note": search_note} if search_note else {}),
                        },
                    )
                    _tool_trace(
                        tool_trace,
                        tool,
                        "OK",
                        {"phase": "seed", "query": query, "engine": tool, "results": len(raw_results)},
                    )
                except Exception as exc:
                    results.append({"query": query, "tool": default_tool, "error": str(exc)})
                    _trace(trace, "search.fail", {"query": query, "error": str(exc)})
                    _tool_trace(
                        tool_trace,
                        default_tool,
                        "FAIL",
                        {"phase": "seed", "query": query, "engine": default_tool, "error": str(exc)},
                    )
        except FuturesTimeout:
            pending = [future for future in future_map if not future.done()]
            for future in pending:
                future.cancel()
                query = future_map[future]
                results.append({"query": query, "tool": default_tool, "error": "Seed-Suche nach Zeitbudget abgebrochen"})
                _trace(trace, "search.timeout", {"query": query})
                _tool_trace(tool_trace, default_tool, "TIMEOUT", {"phase": "seed", "query": query})
    order = {query: idx for idx, query in enumerate(queries)}
    results.sort(key=lambda item: order.get(item.get("query"), 999))
    _trace(trace, "search.parallel.done", {"results": len(results)})
    return results


def _run_one_seed_search(query, timeout_s, config, trace, tool_trace):
    search_tool = _configured_search_tool_name(config)
    try:
        _trace(trace, "search.start", {"query": query, "engine": search_tool})
        search_tool, raw_results, search_note = _search_web(query, 10, timeout_s, config)
        _trace(
            trace,
            "search.done",
            {
                "query": query,
                "engine": search_tool,
                "results": len(raw_results),
                **({"note": search_note} if search_note else {}),
            },
        )
        _tool_trace(
            tool_trace,
            search_tool,
            "OK",
            {"phase": "seed", "query": query, "engine": search_tool, "results": len(raw_results)},
        )
        return {"query": query, "tool": search_tool, "results": raw_results, "note": search_note}
    except Exception as exc:
        _trace(trace, "search.fail", {"query": query, "error": str(exc)})
        _tool_trace(
            tool_trace,
            search_tool,
            "FAIL",
            {"phase": "seed", "query": query, "engine": search_tool, "error": str(exc)},
        )
        return {"query": query, "tool": search_tool, "error": str(exc)}


def _build_search_queries(query, max_queries=6):
    base = _collapse_ws(query)
    year = datetime.now(timezone.utc).strftime("%Y")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    variants = [
        f"{base} aktuelle Nachrichten {year}",
        f"{base} heute {today}",
        f"{base} letzte Stunde aktuelle Entwicklung",
        f"{base} Ursache Folgen Reaktionen Analyse",
        f"{base} Hintergrund Chronologie Timeline",
        f"{base} neueste Meldungen Kommentare Analyse",
    ]
    # Keep the original too, but not first: broad current queries should drive
    # the crawl so stale encyclopedic labels do not dominate.
    variants.append(base)
    return _unique(variants)[:max_queries]


def _query_wants_current(query):
    text = _collapse_ws(query).lower()
    current_terms = (
        "aktuell", "aktuelle", "aktueller", "nachrichten", "news", "heute",
        "jetzt", "latest", "current", "current events", "neueste",
        "stand heute", "letzte stunde", "live",
    )
    return any(term in text for term in current_terms)


def _query_has_historical_scope(query):
    text = _collapse_ws(query).lower()
    historical_terms = (
        "archiv", "archive", "historisch", "history", "rueckblick",
        "ruckblick", "retrospective", "year in review", "was war", "damals",
    )
    return any(term in text for term in historical_terms)


def _query_years(query):
    years = []
    seen = set()
    for raw in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", query or ""):
        year = int(raw)
        if year not in seen:
            seen.add(year)
            years.append(year)
    return years


def _normalize_current_query(query):
    base = _collapse_ws(query)
    if not base or not _query_wants_current(base) or _query_has_historical_scope(base):
        return base, None
    current_year = datetime.now(timezone.utc).year
    stale_years = [year for year in _query_years(base) if year < current_year]
    if not stale_years:
        return base, None
    stale_pattern = r"(?<!\d)(?:" + "|".join(re.escape(str(year)) for year in stale_years) + r")(?!\d)"
    normalized = re.sub(stale_pattern, " ", base)
    normalized = _collapse_ws(re.sub(r"\s+([,;:])", r"\1", normalized))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if current_year not in _query_years(normalized):
        normalized = _collapse_ws(f"{normalized} {current_year} {today} aktuelle News")
    return normalized, {
        "reason": "current_query_with_stale_year",
        "removed_years": stale_years,
        "current_year": current_year,
        "current_date": today,
    }


def _recency_age_days(dates):
    parsed = [_parse_date_hint(value) for value in dates or []]
    parsed = [value for value in parsed if value]
    if not parsed:
        return None
    age = datetime.now(timezone.utc) - max(parsed)
    return age.days


def _stale_for_current_query(query, dates, max_age_days=90):
    if not _query_wants_current(query) or _query_has_historical_scope(query):
        return False
    age_days = _recency_age_days(dates)
    return age_days is not None and age_days > max_age_days


def _impact_research_plan(query, max_queries, full_deepdive, config, max_branch_queries=None):
    base = _collapse_ws(query)
    cfg = config or {}
    if not full_deepdive:
        return {
            "objective": "quick_current_research",
            "impact_plan": [],
            "branch_plan": [],
            "branch_queries": [],
            "search_queries": _build_search_queries(query, max_queries),
        }

    year = datetime.now(timezone.utc).strftime("%Y")
    impact_plan = _impact_language_plan(base) if _cfg_bool(cfg.get("enable_impact_language_plan"), True) else []
    branch_limit = max_branch_queries
    if branch_limit is None:
        branch_limit = _clamp_int(cfg.get("max_branch_queries"), 6, 0, 10)
    branch_plan = _branching_research_plan(base, branch_limit, cfg)
    branch_queries = [item.get("query", "") for item in branch_plan if item.get("query")]
    core_queries = [
        f"{base} latest developments timeline causes consequences reactions {year}",
        f"{base} background causal chain why implications contradictions analysis",
        f"{base} official statement primary source policy document {year}",
        f"{base} criticism disputed claims fact check comparison public opinion",
    ]
    perspective_queries = []
    for item in impact_plan:
        terms = item.get("terms") or ""
        if not terms:
            continue
        perspective_queries.append(terms)
        if item.get("extra"):
            perspective_queries.append(item["extra"])
    lead_queries = [
        f"{base} compared with similar case analogy what does it resemble",
        f"{base} who benefits who is affected mechanism incentives",
    ]
    search_queries = _unique(
        core_queries[:2]
        + branch_queries[:8]
        + perspective_queries[:4]
        + core_queries[2:]
        + branch_queries[8:]
        + perspective_queries[4:]
        + lead_queries
        + [base]
    )[:max_queries]
    return {
        "objective": "causal_branching_multilingual_contrast_research",
        "impact_plan": impact_plan,
        "branch_plan": branch_plan,
        "branch_queries": branch_queries,
        "search_queries": search_queries,
    }


def _query_key(query):
    return _collapse_ws(query).lower()


def _branch_query_lookup(research_plan):
    out = {}
    for item in (research_plan or {}).get("branch_plan", []) or []:
        key = _query_key(item.get("query") or "")
        if key:
            out[key] = item
    return out


def _impact_query_lookup(research_plan):
    out = {}
    for item in (research_plan or {}).get("impact_plan", []) or []:
        for query in (item.get("terms") or "", item.get("extra") or ""):
            key = _query_key(query)
            if key:
                out[key] = item
    return out


def _branch_core_topic(query):
    words = _important_words(query, 7)
    return " ".join(words) or _collapse_ws(query)


def _branching_research_plan(query, max_branch_queries, config):
    if max_branch_queries <= 0 or not _cfg_bool((config or {}).get("enable_branching_causality_plan"), True):
        return []

    base = _collapse_ws(query)
    low = base.lower()
    core = _branch_core_topic(base)
    branches = []

    def add(branch, search_query, reason):
        search_query = _collapse_ws(search_query)
        if not search_query:
            return
        key = search_query.lower()
        if any(item.get("query", "").lower() == key for item in branches):
            return
        branches.append({"branch": branch, "query": search_query, "reason": reason})

    if any(word in low for word in ("ufo", "ufos", "uap", "alien", "aliens", "extraterrestrial", "ausserird")):
        add("adjacent_concept_aliens", f"{base} aliens extraterrestrial life disclosure evidence skeptics", "UFO/UAP kann mit Alien-/Disclosure-Claims verknuepft sein, muss aber getrennt bewertet werden")
        add("military_sensor_chain", f"{base} military pilots radar sensors drones balloons AARO NASA", "UAP-Berichte haengen oft an Militaer, Sensorik, Fehlidentifikation und Behoerden")
        add("whistleblower_congress", f"{base} David Grusch whistleblower Congress testimony reverse engineering claims", "Whistleblower und Kongressanhoerungen sind zentrale Claim-Treiber")
        add("international_files", f"{base} China Russia UK France government UFO UAP files disclosure", "Internationale Akten/Perspektiven koennen Missing Links oder Gegenframing liefern")

    if any(word in low for word in ("japan", "tokyo", "nippon", "japanese", "japanisch")):
        add("regional_security_network", f"{base} China Taiwan United States South Korea security economy reaction", "Japan-Kontext braucht regionale Gegen- und Allianzperspektiven")
        add("domestic_external_causality", f"{base} Japan domestic politics demographics economy defense China Taiwan causes", "Innenpolitik, Wirtschaft und regionale Sicherheit koennen kausal zusammenhaengen")

    if any(word in low for word in ("china", "taiwan", "xi", "jinping", "handelskrieg", "trade war", "tariff", "tariffs", "zoll", "zoelle", "zölle", "export control", "exportkontrolle")):
        add("chip_war_semiconductors", "Trump Xi China Taiwan semiconductors Nvidia TSMC Huawei ASML export controls 2026", "China/Taiwan/Handelskrieg haengt oft am Chipkrieg, KI-Compute und Exportkontrollen")
        add("ai_compute_supply_chain", "Nvidia Huawei TSMC ASML US China AI chips export controls data centers 2026", "Nvidia/TSMC/Huawei/ASML koennen der fehlende Wirtschafts- und Sicherheitslink sein")
        add("china_policy_actor_graph", "Trump China policy Xi Jinping Marco Rubio JD Vance Scott Bessent Howard Lutnick Jamieson Greer 2026", "Politiker, Kabinett und Verhandler erklaeren Absichten und Hebel")
        add("taiwan_alliance_network", "Trump Xi Taiwan arms package Japan South Korea Philippines US China security 2026", "Taiwan wirkt ueber Allianzen, Waffenpakete und regionale Abschreckung")
        add("sanctions_leverage_theatres", "Trump China Iran North Korea Russia sanctions leverage trade Taiwan 2026", "Iran/Nordkorea/Russland koennen als Druck- und Verhandlungshebel in China-Politik auftauchen")

    if any(word in low for word in ("ford", "ford motors", "automotive", "autohersteller", "electric vehicle", "ev")):
        add("competitor_market_map", f"{core} competitors GM Toyota Tesla Stellantis Hyundai BYD market share strategy", "Bei Autoherstellern erklaeren Wettbewerber und Marktanteile oft die Dynamik")
        add("supply_chain_labor_policy", f"{core} supply chain batteries UAW labor tariffs China EV incentives pricing", "Lieferketten, Gewerkschaften, Zoelle und EV-Foerderung sind moegliche Ursachen")

    if any(word in low for word in ("openai", "google", "microsoft", "anthropic", "deepseek", "xai", "nvidia", "semiconductor", "chip", "tsmc", "huawei", "asml")) or re.search(r"\b(ai|ki|llm|model)\b", low):
        add("tech_competitor_ecosystem", f"{core} competitors ecosystem regulation open source chips cloud pricing", "Tech-Themen brauchen Wettbewerber, Regulierung, Infrastruktur und Kostenstruktur")
        add("supply_compute_policy", f"{core} compute supply chain export controls GPUs data centers policy impact", "Compute, Chips und Regulierung sind haeufige Kausalfaktoren")

    if any(word in low for word in ("bitcoin", "crypto", "krypto", "ethereum", "stock", "aktie", "market", "price", "preis")):
        add("market_drivers", f"{core} macro liquidity regulation ETF flows derivatives sentiment drivers", "Marktbewegungen brauchen Treiber statt nur Preisquellen")
        add("market_counterparties", f"{core} competitors alternatives institutional retail miners exchanges risks", "Gegenparteien und Alternativen helfen Kausalitaet einzuordnen")

    org_like = any(
        word in low
        for word in (
            "motors", "motor", "corp", "inc", "ltd", "company", "gmbh", "ag",
            "ford", "tesla", "nvidia", "openai", "google", "microsoft", "apple", "amazon", "meta", "byd",
        )
    )
    person_like = any(word in low for word in ("trump", "biden", "putin", "merkel", "merz", "musk", "altman")) or bool(
        re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b", base)
    )
    if person_like and not org_like:
        add("person_inner_circle", f"{core} inner circle advisers donors cabinet family business legal network", "Bei Personen ist das Umfeld oft kausal wichtiger als die Person alleine")
        add("person_opposition_institutions", f"{core} opponents courts agencies congress party media influence network", "Gegenakteure und Institutionen zeigen Druck, Grenzen und Interessen")

    add("actor_network", f"{core} key actors stakeholders allies opponents institutions network", "Akteursnetzwerk und Interessenlage suchen")
    add("missing_links", f"{core} adjacent topics related phenomena missing links context", "Moegliche fehlende Verbindungen und Nachbarbegriffe suchen")
    add("causal_mechanism", f"{core} causal mechanism incentives who benefits who loses why", "Mechanismen, Anreize und Gewinner/Verlierer herausarbeiten")
    add("historical_analogy", f"{core} historical parallels similar cases analogy comparison", "Historische Parallelen und Vergleichsfaelle als Leads pruefen")

    return branches[:max_branch_queries]


def _impact_language_plan(query):
    low = query.lower()
    entries = []

    def add(language, country, role, terms, reason, extra=""):
        key = (language, country)
        if any((item["language"], item["country"]) == key for item in entries):
            return
        entries.append(
            {
                "language": language,
                "country": country,
                "role": role,
                "terms": terms,
                "extra": extra,
                "reason": reason,
            }
        )

    if any(word in low for word in ("ufo", "ufos", "uap", "alien", "aliens", "extraterrestrial", "ausserird")):
        add("en", "United States", "government_disclosure_primary", f"{query} UAP AARO NASA Pentagon Congress disclosure whistleblower", "US-Behoerden und Kongress sind beim UAP-Thema zentrale Primaerakteure")
        add("fr", "France", "legacy_government_files", f"{query} OVNI PAN GEIPAN CNES rapports temoignages analyse", "Frankreich/GEIPAN ist eine wichtige staatliche UFO/PAN-Quelle")
        add("zh", "China", "strategic_or_state_framing", f"{query} 不明飞行物 UFO UAP 外星人 军方 反应 分析", "Chinesische Perspektive kann strategisches Gegenframing und Sicherheitsdebatte zeigen")
        add("ru", "Russia", "strategic_or_state_framing", f"{query} НЛО UAP инопланетяне военные документы реакция анализ", "Russische Perspektive kann Sicherheits-/Militaerframing zeigen")
        add("es", "Spain/Latin America", "public_reports_region", f"{query} ovnis UAP extraterrestres gobierno militares testimonios analisis", "Spanischsprachige Quellen liefern weitere Meldungs- und Meinungsraeume")
    if any(word in low for word in ("japan", "tokyo", "nippon", "japanese", "japanisch")):
        add("ja", "Japan", "domestic_primary", "日本 防衛費 軍事力 増強 原因 影響 反応", "Japan ist direkter Akteur")
        add("zh", "China", "rival_or_affected", "日本 军事扩张 中国 反应 台湾 安全 原因 影响", "China ist bei Japan/Taiwan/Sicherheit zentral betroffen")
        add("zh-TW", "Taiwan", "affected_region", "日本 軍事 擴張 台灣 安全 影響 中國 反應", "Taiwan ist betroffene Region")
        add("ko", "South Korea", "regional_ally_affected", "일본 군사력 증강 한국 반응 안보 영향", "Korea ist regionaler Sicherheitsakteur")
        add("en", "United States", "ally_security_architecture", "Japan military buildup causes implications Taiwan China US response", "USA ist Sicherheitsgarant/Allianzakteur")
    if any(word in low for word in ("china", "chinese", "beijing", "peking", "taiwan", "taiwanese")):
        add("zh", "China", "domestic_or_state_framing", "中国 台湾 日本 美国 安全 反应 原因 影响", "Chinesische Perspektive ist direkt relevant")
        add("zh-TW", "Taiwan", "directly_affected", "台灣 中國 美國 日本 安全 影響 反應", "Taiwan-Perspektive ist direkt betroffen")
        add("en", "United States", "global_power_or_ally", "China Taiwan Japan US security implications analysis", "US/englische Analyse fuer internationale Einordnung")
        add("ja", "Japan", "regional_affected", "中国 台湾 日本 安全保障 影響 反応", "Japan ist regional betroffen")
    if any(word in low for word in ("ukraine", "russia", "russland", "moscow", "moskau", "kyiv", "kiew")):
        add("uk", "Ukraine", "directly_affected", "Україна Росія війна причини наслідки реакція", "Ukraine ist direkter Akteur")
        add("ru", "Russia", "adversary_or_state_framing", "Россия Украина война причины последствия реакция", "Russische Perspektive zeigt Gegenframing")
        add("en", "United States/NATO", "ally_or_security_architecture", "Ukraine Russia war causes consequences NATO reactions analysis", "NATO/US-Kontext")
        add("pl", "Poland/Eastern Europe", "regional_affected", "Ukraina Rosja wojna konsekwencje reakcje Polska", "Osteuropa ist besonders betroffen")
    if any(word in low for word in ("israel", "gaza", "hamas", "palestine", "palästina", "iran", "tehran", "teheran")):
        add("he", "Israel", "domestic_primary", "ישראל עזה איראן סיבות השלכות תגובות", "Israelische Perspektive ist direkter Akteur")
        add("ar", "Palestine/Arab region", "affected_region", "غزة إسرائيل فلسطين أسباب تداعيات ردود فعل", "Arabische/palaestinensische Perspektive ist direkt betroffen")
        add("fa", "Iran", "regional_actor", "ایران اسرائیل غزه علت پیامد واکنش", "Iranische Perspektive bei regionaler Dynamik")
        add("en", "International", "international_analysis", "Israel Gaza Iran causes consequences reactions analysis", "Internationale Analyse")
    if any(word in low for word in ("germany", "deutschland", "bundesregierung", "berlin", "merz", "afd", "spd", "cdu")):
        add("de", "Germany", "domestic_primary", f"{query} Deutschland Ursache Folgen Reaktionen Chronologie", "Deutsche Innenperspektive ist primaer")
        add("en", "International", "outside_analysis", f"{query} Germany international reaction analysis", "Internationale Einordnung")
        add("fr", "France/EU", "neighbor_or_eu", f"{query} Allemagne Europe France réaction analyse", "EU-Nachbarperspektive")
    if any(word in low for word in ("bitcoin", "crypto", "krypto", "ethereum", "openai", "nvidia", "semiconductor", "chip")) or re.search(r"\b(ai|ki)\b", low):
        add("en", "United States/Global", "market_or_tech_primary", f"{query} latest market technical analysis causes impact", "Englisch dominiert Tech-/Marktdebatten")
        add("zh", "China/Asia", "market_or_policy_contrast", f"{query} 中国 市场 政策 影响 分析", "China/Asien ist bei Tech/Markets oft relevant")
        add("ja", "Japan", "market_regional", f"{query} 日本 市場 影響 分析", "Japanische Markt-/Tech-Perspektive")
        add("ko", "South Korea", "market_regional", f"{query} 한국 시장 영향 분석", "Koreanische Tech-/Marktperspektive")

    if not entries:
        add("en", "International", "global_analysis", f"{query} latest developments causes consequences reactions analysis", "Englisch als breiter internationaler Quellenraum")
        add("de", "German-speaking", "user_language_context", f"{query} Ursache Folgen Reaktionen Analyse", "Nutzersprache fuer lokalen Kontext")
    return entries[:8]


def _grok_search_likely_available(config):
    try:
        settings = _grok_search_config(config or {})
        return bool(str(settings.get("api_key") or "").strip())
    except Exception:
        return False


def _load_rss_module():
    global _RSS_MODULE
    if _RSS_MODULE is not None:
        return _RSS_MODULE

    _RSS_MODULE = _load_sibling_module("rss_verwaltung", "deepdive_rss_verwaltung")
    return _RSS_MODULE


def _load_reddit_module():
    global _REDDIT_MODULE
    if _REDDIT_MODULE is not None:
        return _REDDIT_MODULE
    _REDDIT_MODULE = _load_sibling_module("reddit_scraper", "deepdive_reddit_scraper")
    return _REDDIT_MODULE


def _load_grok_search_module():
    global _GROK_SEARCH_MODULE
    if _GROK_SEARCH_MODULE is not None:
        return _GROK_SEARCH_MODULE
    _GROK_SEARCH_MODULE = _load_sibling_module("grok_search", "deepdive_grok_search")
    return _GROK_SEARCH_MODULE


def _load_youtube_transcript_module():
    global _YOUTUBE_TRANSCRIPT_MODULE
    if _YOUTUBE_TRANSCRIPT_MODULE is not None:
        return _YOUTUBE_TRANSCRIPT_MODULE
    _YOUTUBE_TRANSCRIPT_MODULE = _load_sibling_module("youtube_transcript", "deepdive_youtube_transcript")
    return _YOUTUBE_TRANSCRIPT_MODULE


def _load_sibling_module(module_name, import_name):
    module_path = os.path.abspath(os.path.join(MODUL_DIR, "..", module_name, "module.py"))
    if not os.path.exists(module_path):
        raise RuntimeError(f"Modul nicht gefunden: {module_path}")
    spec = importlib.util.spec_from_file_location(import_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Modul kann nicht geladen werden: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_youtube_url(url):
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
        host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
        return (
            host == "youtu.be"
            or host == "youtube.com"
            or host.endswith(".youtube.com")
            or host.endswith(".youtube-nocookie.com")
        )
    except Exception:
        return False


def _youtube_transcript_page(url, query, config, max_chars):
    youtube_module = _load_youtube_transcript_module()
    youtube_config = _module_settings(config, "youtube_transcript")
    youtube_config.update(
        {
            "data_dir": config.get("data_dir", ""),
            "rag_pool": config.get("rag_pool", "DeepDive"),
            "home_dir": config.get("home_dir", ""),
            "project_root": config.get("project_root", ""),
            "modules_dir": config.get("modules_dir", ""),
            "max_output_chars": max(12000, min(max_chars + 8000, 80000)),
        }
    )
    if youtube_config.get("xai_api_key"):
        youtube_config["xai_api_key"] = _resolve_api_key_alias(youtube_config.get("xai_api_key"), config)
    payload = {
        "url": url,
        "languages": str(config.get("youtube_transcript_languages") or youtube_config.get("preferred_languages") or "de,en,auto"),
        "fallback_stt": bool(config.get("youtube_transcript_fallback_stt", False)),
    }
    result = youtube_module.handle_tool("youtube_transcript.fetch", [json.dumps(payload)], youtube_config)
    if not result.get("success"):
        raise RuntimeError(str(result.get("data") or "youtube_transcript.fetch failed")[:1200])
    packet = str(result.get("data") or "")
    title = _line_value(packet, "title") or "YouTube transcript"
    upload_date = _line_value(packet, "upload_date")
    source_url = _line_value(packet, "source_url") or url
    links = [{"text": "YouTube video", "url": source_url}]
    text = packet
    if query and "transcript:" in packet:
        # Keep metadata plus transcript; DeepDive hint extraction can work directly on this.
        text = packet
    return {
        "title": title,
        "text": text,
        "links": links,
        "dates": [upload_date] if upload_date else [],
        "meta": {
            "description": "YouTube transcript via youtube_transcript.fetch",
            "og:title": title,
            "source_type": "youtube_transcript",
        },
    }


def _collect_reddit_sources(query, max_threads, config):
    reddit_module = _load_reddit_module()
    reddit_config = _module_settings(config, "reddit_scraper")
    reddit_config.update(
        {
            "data_dir": config.get("data_dir", ""),
            "rag_pool": config.get("rag_pool", "DeepDive"),
            "max_output_chars": max(16000, _clamp_int(reddit_config.get("max_output_chars"), 24000, 2000, 80000)),
        }
    )
    payload = {
        "query": query,
        "threads": max_threads,
        "comments_per_thread": _clamp_int(config.get("reddit_comments_per_thread"), 12, 0, 80),
        "sort": str(config.get("reddit_sort") or "relevance"),
        "time": str(config.get("reddit_time") or "month"),
    }
    result = reddit_module.handle_tool("reddit_scraper.pull", [json.dumps(payload)], reddit_config)
    if not result.get("success"):
        raise RuntimeError(result.get("data") or "reddit_scraper.pull failed")
    packet = str(result.get("data") or "")
    urls = _extract_urls(packet, allowed_hosts=("reddit.com", "old.reddit.com"))
    thread_urls = [url for url in urls if "/comments/" in urllib.parse.urlparse(url).path]
    if thread_urls:
        urls = thread_urls
    else:
        urls = [url for url in urls if "/search/" not in urllib.parse.urlparse(url).path]
    return packet, urls


def _collect_grok_search_sources(query, config):
    grok_module = _load_grok_search_module()
    grok_config = _grok_search_config(config)
    mode = str(config.get("grok_search_mode") or "research").strip().lower()
    tool = {
        "web": "grok_search.web",
        "x": "grok_search.x",
        "research": "grok_search.research",
        "both": "grok_search.research",
    }.get(mode, "grok_search.research")
    payload = {
        "query": query,
        "max_output_tokens": _clamp_int(config.get("grok_search_max_output_tokens"), 900, 200, 6000),
    }
    if config.get("grok_search_allowed_domains"):
        payload["allowed_domains"] = _csv_list(config.get("grok_search_allowed_domains"), 5)
    if config.get("grok_search_allowed_x_handles"):
        payload["allowed_x_handles"] = _csv_list(config.get("grok_search_allowed_x_handles"), 10)
    result = grok_module.handle_tool(tool, [json.dumps(payload)], grok_config)
    if not result.get("success"):
        raise RuntimeError(result.get("data") or f"{tool} failed")
    packet = str(result.get("data") or "")
    return packet, _extract_urls(packet), tool


def _external_packet_note(crawl_id, query, tool, packet):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    urls = _extract_urls(packet)
    lines = [
        "DEEPDIVE_EXTERNAL_PACKET",
        f"crawl_id: {crawl_id}",
        f"captured_at_utc: {now}",
        f"source_last_seen_utc: {now}",
        f"topic: {query}",
        f"source_title: {tool} packet",
        f"tool: {tool}",
        f"urls_found: {len(urls)}",
        "source_urls:",
    ]
    for url in urls[:30]:
        lines.append("- " + url)
    lines.extend(
        [
            "assessment_required: extract claims, opinions, analogies and source leads; compare with fetched sources; treat social/X/Reddit as signal or lead, not proof",
            "packet_text:",
            str(packet or "")[:20000],
        ]
    )
    return "\n".join(lines)


def _module_settings(config, module_typ):
    runtime = _runtime_config(config)
    preferred_id = str((config or {}).get(f"{module_typ}_module_id") or "").strip()
    candidates = [
        module
        for module in runtime.get("module", [])
        if isinstance(module, dict)
        and (module.get("typ") == module_typ or str(module.get("id", "")).startswith(f"{module_typ}."))
    ]
    selected = None
    if preferred_id:
        selected = next((module for module in candidates if module.get("id") == preferred_id or module.get("name") == preferred_id), None)
    if selected is None:
        selected = next((module for module in candidates if _settings_has_key(module.get("settings") or {})), None)
    if selected is None and candidates:
        selected = candidates[0]
    settings = dict((selected or {}).get("settings") or {})
    for key in ("data_dir", "rag_pool", "home_dir", "project_root", "modules_dir"):
        if (config or {}).get(key) and not settings.get(key):
            settings[key] = config.get(key)
    return settings


def _grok_search_config(config):
    settings = _module_settings(config, "grok_search")
    explicit_key = str((config or {}).get("grok_search_api_key") or "").strip()
    if explicit_key:
        settings["api_key"] = _resolve_api_key_alias(explicit_key, config)
    elif settings.get("api_key"):
        settings["api_key"] = _resolve_api_key_alias(settings.get("api_key"), config)
    if not settings.get("api_key"):
        backend_key = _xai_api_key_from_runtime(config)
        if backend_key:
            settings["api_key"] = _resolve_api_key_alias(backend_key, config)
    explicit_model = str((config or {}).get("grok_search_model") or "").strip()
    if explicit_model:
        settings["model"] = explicit_model
    settings.setdefault("api_base", "https://api.x.ai")
    settings.setdefault("model", "grok-4.3")
    settings.setdefault("request_timeout_s", _clamp_int((config or {}).get("grok_search_timeout_s"), 60, 5, 300))
    settings.setdefault("max_output_chars", 24000)
    return settings


def _runtime_config(config):
    path = os.path.join(_data_dir(config), "config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _data_dir(config):
    configured = str((config or {}).get("data_dir") or "").strip()
    if configured:
        return configured
    return os.path.abspath(os.path.join(MODUL_DIR, "..", "..", "agent-data"))


def _settings_has_key(settings):
    if not isinstance(settings, dict):
        return False
    return bool(
        settings.get("api_key")
        or settings.get("bearer_token")
        or settings.get("grok_api_key")
        or settings.get("xai_api_key")
    )


def _xai_api_key_from_runtime(config):
    runtime = _runtime_config(config)
    for backend in runtime.get("llm_backends", []):
        if not isinstance(backend, dict):
            continue
        if backend.get("typ") == "Grok" or "grok" in str(backend.get("id", "")).lower():
            key = str(backend.get("api_key") or "").strip()
            if key:
                return _resolve_api_key_alias(key, config)
    return os.environ.get("XAI_API_KEY", "").strip() or os.environ.get("GROK_API_KEY", "").strip()


def _resolve_api_key_alias(value, config):
    text = str(value or "").strip()
    if not text.startswith("api."):
        return text
    key_id = text.split(".", 1)[1].strip()
    if not key_id:
        return text
    runtime = _runtime_config(config)
    for item in runtime.get("api_key_vault", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "").strip() == key_id:
            secret = str(item.get("secret") or "").strip()
            if secret:
                return secret
    return text


def _search_web(query, max_results, timeout_s, config):
    provider = _search_provider(config)
    fallback_note = ""
    api_key = _tavily_api_key(config)

    if provider in {"auto", "tavily"}:
        if api_key:
            try:
                return "tavily.search", _search_tavily(query, max_results, timeout_s, config, api_key), ""
            except Exception as exc:
                if provider == "tavily":
                    raise
                fallback_note = f"tavily_fallback: {_short_error(exc)}"
        elif provider == "tavily":
            raise RuntimeError("Tavily API Key nicht konfiguriert. Bitte DeepDive-Setting oder modules/tavily/config.json setzen.")

    return "duckduckgo.search", _search_duckduckgo(query, max_results, timeout_s), fallback_note


def _search_provider(config):
    provider = str((config or {}).get("search_provider") or "auto").strip().lower()
    aliases = {
        "ddg": "duckduckgo",
        "duckduckgo_lite": "duckduckgo",
        "duckduckgo-lite": "duckduckgo",
    }
    provider = aliases.get(provider, provider)
    if provider not in {"auto", "tavily", "duckduckgo"}:
        return "auto"
    return provider


def _configured_search_tool_name(config):
    provider = _search_provider(config)
    if provider == "tavily":
        return "tavily.search"
    if provider == "duckduckgo":
        return "duckduckgo.search"
    return "tavily.search" if _tavily_api_key(config) else "duckduckgo.search"


def _short_error(value, limit=180):
    text = _collapse_ws(str(value or ""))
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _tavily_api_key(config):
    cfg = config or {}
    explicit = str(cfg.get("tavily_api_key") or "").strip()
    if explicit:
        return _resolve_api_key_alias(explicit, config)

    env_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if env_key:
        return env_key

    tavily_cfg_path = os.path.abspath(os.path.join(MODUL_DIR, "..", "tavily", "config.json"))
    try:
        with open(tavily_cfg_path, "r", encoding="utf-8") as fh:
            tavily_cfg = json.load(fh)
        for key in ("api_key", "tavily_api_key"):
            value = str(tavily_cfg.get(key) or "").strip()
            if value:
                return _resolve_api_key_alias(value, config)
    except Exception:
        pass
    return ""


def _search_tavily(query, max_results, timeout_s, config, api_key):
    search_depth = str((config or {}).get("tavily_search_depth") or (config or {}).get("search_depth") or "basic").strip().lower()
    if search_depth not in {"basic", "advanced"}:
        search_depth = "basic"
    body = json.dumps(
        {
            "query": query,
            "max_results": int(max(1, min(20, max_results))),
            "search_depth": search_depth,
            "include_answer": False,
            "include_raw_content": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(8, timeout_s)) as resp:
            payload = resp.read(1_000_000)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(500).decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Tavily API HTTP {exc.code}{(': ' + _short_error(detail)) if detail else ''}") from exc

    data = json.loads(payload.decode("utf-8", errors="replace"))
    results = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = _clean_url(item.get("url") or "")
        if not url:
            continue
        title = _collapse_ws(item.get("title") or url)
        snippet = _collapse_ws(item.get("content") or item.get("snippet") or "")
        results.append({"title": title[:220], "url": url, "snippet": snippet[:700]})
        if len(results) >= max_results:
            break
    return results


def _search_duckduckgo(query, max_results, timeout_s):
    encoded = urllib.parse.quote_plus(query)
    url = f"https://lite.duckduckgo.com/lite/?q={encoded}"
    page = _fetch_url(url, timeout_s, allow_search_page=True)
    anchors = re.finditer(r"<a\b[^>]*class=['\"]result-link['\"][^>]*>.*?</a>", page, re.I | re.S)
    snippets = [
        _strip_html(m.group(1))
        for m in re.finditer(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", page, re.I | re.S)
    ]
    out = []
    for idx, match in enumerate(anchors):
        if len(out) >= max_results:
            break
        anchor = match.group(0)
        href_m = re.search(r"href=['\"]([^'\"]+)['\"]", anchor, re.I)
        if not href_m:
            continue
        raw_url = html.unescape(href_m.group(1))
        real_url = _unwrap_duckduckgo_url(raw_url)
        if not real_url or "duckduckgo.com" in real_url or "duck.co" in real_url:
            continue
        title = _strip_html(anchor)
        if not title:
            continue
        out.append(
            {
                "title": title[:220],
                "url": real_url,
                "snippet": snippets[idx] if idx < len(snippets) else "",
            }
        )
    return out


def _select_sources(candidates, max_sources, query="", min_branch_sources=0):
    selected = []
    selected_urls = set()
    host_counts = {}
    preferred = []
    terms = _query_terms(query)
    broad_query = _is_broad_query(query)
    current_query = _query_wants_current(query) and not _query_has_historical_scope(query)
    current_year = datetime.now(timezone.utc).year
    for result in candidates:
        host = urllib.parse.urlparse(result["url"]).netloc.lower().removeprefix("www.")
        score = 0
        text = f"{result.get('title','')} {result.get('snippet','')} {result.get('url','')}".lower()
        term_hits = sum(1 for term in terms if term in text)
        source_terms = _query_terms(result.get("search_query") or "")
        source_term_hits = sum(1 for term in source_terms if term in text)
        is_branch = bool(result.get("branch_name"))
        if terms and term_hits == 0 and source_term_hits == 0 and not broad_query and result.get("discovery_method") != "rss" and not is_branch:
            continue
        score += term_hits * 8
        score += source_term_hits * 5
        if is_branch:
            score += 14
        if (terms and term_hits >= min(2, len(terms))) or source_term_hits >= 2:
            score += 4
        if result.get("discovery_method") == "rss":
            score += 6
        if any(word in text for word in ("aktuell", "news", "nachrichten", "heute", "live")):
            score += 5
        if any(domain in host for domain in ("tagesschau", "bundestag", "bundesregierung", "faz", "zdf", "spiegel", "zeit", "dw.com", "reuters", "apnews", "britannica", "wikipedia")):
            score += 3
        if _extract_dates(text):
            score += 2
        if current_query:
            candidate_years = _query_years(text)
            if current_year in candidate_years:
                score += 4
            if any(year < current_year for year in candidate_years):
                score -= 12
        preferred.append((score, result))
    preferred.sort(key=lambda item: item[0], reverse=True)

    branch_limit = min(max(0, int(min_branch_sources or 0)), max_sources)
    branch_seen = set()
    if branch_limit > 0:
        for _, result in preferred:
            branch = str(result.get("branch_name") or "").strip()
            if not branch or branch in branch_seen:
                continue
            host = urllib.parse.urlparse(result["url"]).netloc.lower().removeprefix("www.")
            if host_counts.get(host, 0) >= 2:
                continue
            selected.append(result)
            selected_urls.add(result["url"])
            branch_seen.add(branch)
            host_counts[host] = host_counts.get(host, 0) + 1
            if len(selected) >= branch_limit:
                break

    for _, result in preferred:
        if result["url"] in selected_urls:
            continue
        host = urllib.parse.urlparse(result["url"]).netloc.lower().removeprefix("www.")
        if host_counts.get(host, 0) >= 2:
            continue
        host_counts[host] = host_counts.get(host, 0) + 1
        selected.append(result)
        selected_urls.add(result["url"])
        if len(selected) >= max_sources:
            break
    return selected


def _select_followup_links(links, query, parent_url, limit, known_urls, allow_private, parent_page_role="source"):
    scored = []
    parent_host = _host(parent_url)
    for link in links:
        url = _clean_url(link.get("url") or "")
        if not url or url in known_urls:
            continue
        if not _is_allowed_http_url(url, allow_private):
            continue
        if _skip_link(url, link.get("text") or ""):
            continue
        score, reason = _link_score(query, link, parent_host, parent_page_role)
        if score <= 0:
            continue
        scored.append((score, {**link, "url": url, "score": score, "reason": reason}))
    scored.sort(key=lambda item: item[0], reverse=True)
    out = []
    host_counts = {}
    for _, link in scored:
        host = _host(link["url"])
        if host_counts.get(host, 0) >= 2:
            continue
        host_counts[host] = host_counts.get(host, 0) + 1
        out.append(link)
        if len(out) >= limit:
            break
    return out


def _link_score(query, link, parent_host, parent_page_role="source"):
    text = f"{link.get('text','')} {link.get('url','')}".lower()
    terms = _query_terms(query)
    term_hits = [term for term in terms if term in text]
    date_hits = _extract_dates(text)
    article_score = _article_link_score(link.get("url") or "", link.get("text") or "")
    score = len(term_hits) * 2
    reasons = []
    if score:
        reasons.append("Query-Bezug")
    if terms and not term_hits and not _is_broad_query(query):
        return 0, ""
    current_words = (
        "aktuell", "news", "nachrichten", "heute", "live", "ticker", "entwicklung",
        "analyse", "kommentar", "hintergrund", "fakten", "timeline", "chronologie",
        "interview", "presse", "regierung", "bundestag", "europa", "international",
    )
    hits = [word for word in current_words if word in text]
    # Bei Hub-Seiten sind die eigentlichen Artikel oft nur ueber Teaser-Links
    # erreichbar, deren Text nicht alle Query-Terme wiederholt.
    if parent_page_role == "hub" and article_score >= 3:
        score += article_score + 4
        reasons.append("konkreter Artikel aus Hub")
    elif not term_hits and not date_hits:
        return 0, ""
    if hits:
        score += min(6, len(hits) * 2)
        reasons.append("aktueller/hintergruendiger Link")
    if date_hits:
        score += 3
        reasons.append("Datumsbezug")
    host = _host(link.get("url") or "")
    if host and host == parent_host:
        score += 1
        reasons.append("gleiche Quelle")
    if _is_preferred_host(host):
        score += 2
        reasons.append("etablierte Quelle")
    return score, ", ".join(reasons) or "relevant"


def _article_link_score(url, label):
    parsed = urllib.parse.urlparse(url or "")
    path = parsed.path.lower()
    label_low = (label or "").lower()
    last = path.rstrip("/").split("/")[-1]
    score = 0
    if re.search(r"/(?:19|20)\d{2}(?:/|-)", path) or re.search(r"\b(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b", path):
        score += 3
    if re.search(r"[-_/](?:\d{5,}|[a-z0-9]{8,})(?:\.html?)?$", path):
        score += 2
    if len(last) >= 24 and ("-" in last or "_" in last):
        score += 2
    if any(part in path for part in ("/artikel/", "/article/", "/news/", "/nachricht", "/meldung", "/story/", "/analyse/", "/politik/")):
        score += 1
    if len(_important_words(label_low, 12)) >= 5:
        score += 2
    if any(word in label_low for word in ("live", "analyse", "kommentar", "hintergrund", "reaktion", "warum", "chronologie")):
        score += 2
    if _looks_like_navigation(label_low, path):
        score -= 5
    return max(0, score)


def _derive_search_queries(query, page):
    title = _collapse_ws(page.get("title") or "")
    description = _collapse_ws(
        page.get("meta", {}).get("description")
        or page.get("meta", {}).get("og:description")
        or ""
    )
    dates = page.get("dates") or []
    snippets = _key_passages(query, page.get("text") or "", dates)[:3]
    leads = _lead_hints(query, page.get("text") or "", page.get("links") or [])[:3]
    causal = _causality_hints(query, page.get("text") or "", snippets)[:2]
    variants = []
    if title:
        title_terms = " ".join(_important_words(title, 6))
        if title_terms:
            variants.append(f"{query} {title_terms} aktuell")
            variants.append(f"{query} {title_terms} Hintergrund")
            variants.append(f"{query} {title_terms} Ursache Folgen Reaktionen")
    if description:
        desc_terms = " ".join(_important_words(description, 5))
        if desc_terms:
            variants.append(f"{query} {desc_terms} Entwicklung")
            variants.append(f"{query} {desc_terms} Warum Kontext")
    for date in dates[:2]:
        variants.append(f"{query} {date} Entwicklung Kontext")
    for passage in snippets:
        passage_terms = " ".join(_important_words(passage, 5))
        if passage_terms:
            variants.append(f"{query} {passage_terms} Reaktion Analyse")
    for lead in leads:
        lead_terms = " ".join(_important_words(lead, 6))
        if lead_terms:
            variants.append(f"{query} {lead_terms} pruefen Quelle Vergleich")
    for hint in causal:
        hint_terms = " ".join(_important_words(hint, 5))
        if hint_terms:
            variants.append(f"{query} {hint_terms} Ursache Mechanismus")
    return [q for q in _unique(variants) if q.lower() != _collapse_ws(query).lower()][:4]


def _relevance_score(query, result, page):
    text = f"{result.get('title','')} {result.get('snippet','')} {page.get('title','')} {page.get('text','')[:4000]}".lower()
    terms = _query_terms(query)
    term_hits = sum(1 for term in terms if term in text)
    source_terms = _query_terms(result.get("search_query") or "")
    source_term_hits = sum(1 for term in source_terms if term in text)
    if terms and term_hits == 0 and source_term_hits == 0 and not _is_broad_query(query):
        return 0
    score = term_hits * 3 + source_term_hits * 2
    if any(word in text for word in ("aktuell", "news", "nachrichten", "heute", "live", "entwicklung")):
        score += 8
    if page.get("dates"):
        score += 5
    if _is_preferred_host(_host(result.get("url") or "")):
        score += 4
    if result.get("discovery_method") == "source_link":
        score += 2
    if result.get("discovery_method") == "derived_search":
        score += 3
    return min(100, score)


def _key_passages(query, text, dates):
    terms = _query_terms(query)
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    scored = []
    for raw in parts:
        sentence = _collapse_ws(raw)
        if len(sentence) < 80 or len(sentence) > 900:
            continue
        low = sentence.lower()
        score = sum(3 for term in terms if term in low)
        if any(date.lower() in low for date in dates[:8]):
            score += 4
        if any(word in low for word in ("aktuell", "heute", "sagte", "erklaerte", "kritik", "forderung", "beschloss", "bericht", "kommentar", "analyse", "stand")):
            score += 2
        if score > 0:
            scored.append((score, sentence))
    scored.sort(key=lambda item: item[0], reverse=True)
    out = []
    seen = set()
    for _, sentence in scored:
        key = sentence[:120].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(sentence)
        if len(out) >= 8:
            break
    return out


def _causality_hints(query, text, key_passages):
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    query_terms = _query_terms(query)
    cause_words = (
        "weil", "wegen", "deshalb", "daher", "darum", "grund", "ursache",
        "ausloeser", "auslöser", "folge", "folgen", "reaktion", "kritik",
        "nachdem", "zuvor", "hintergrund", "kontext", "fuehrte", "führte",
        "loeste", "löste", "erklaerte", "erklärte", "warum",
    )
    candidates = list(key_passages or [])
    candidates.extend(parts)
    scored = []
    for raw in candidates:
        sentence = _collapse_ws(raw)
        if len(sentence) < 55 or len(sentence) > 900:
            continue
        low = sentence.lower()
        score = sum(3 for term in query_terms if term in low)
        score += sum(2 for word in cause_words if word in low)
        if score >= 4:
            scored.append((score, sentence))
    scored.sort(key=lambda item: item[0], reverse=True)
    out = []
    seen = set()
    for _, sentence in scored:
        key = sentence[:160].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(sentence)
        if len(out) >= 6:
            break
    return out


def _claim_hints(query, text, key_passages):
    parts = list(key_passages or [])
    parts.extend(re.split(r"(?<=[.!?])\s+|\n+", text or ""))
    terms = _query_terms(query)
    claim_words = (
        "sagt", "sagte", "erklaerte", "erklärte", "behauptet", "laut",
        "according to", "said", "claimed", "reported", "warned", "announced",
        "fordert", "kritisiert", "bestreitet", "denies", "accuses", "argues",
        "认为", "表示", "称", "警告", "主張", "述べ", "발표", "주장",
    )
    return _scored_sentences(parts, terms, claim_words, 6, min_score=3)


def _event_hints(query, text, dates):
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    terms = _query_terms(query)
    event_words = (
        "am ", "seit", "zuvor", "danach", "heute", "gestern", "angekuendigt",
        "angekündigt", "beschlossen", "veroeffentlicht", "veröffentlicht",
        "announced", "launched", "published", "reported", "after", "before",
        "timeline", "chronologie", "年", "月", "日", "発表", "宣布", "发布",
    )
    candidates = []
    for raw in parts:
        sentence = _collapse_ws(raw)
        if any(date and date in sentence for date in dates[:8]) or _extract_dates(sentence):
            candidates.append(sentence)
        elif any(word in sentence.lower() for word in event_words):
            candidates.append(sentence)
    return _scored_sentences(candidates, terms, event_words, 6, min_score=2)


def _lead_hints(query, text, links):
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    terms = _query_terms(query)
    lead_words = (
        "vergleich", "vergleichbar", "wie ", "ähnlich", "aehnlich", "siehe",
        "steht auch", "laut", "quelle", "link", "thread", "comment", "kommentar",
        "compare", "similar to", "like ", "also see", "source", "on x", "on reddit",
        "analog", "parallele", "precedent", "例", "类似", "参照", "비슷", "출처",
    )
    candidates = [sentence for sentence in parts if any(word in sentence.lower() for word in lead_words)]
    for link in links or []:
        label = _collapse_ws(link.get("text") or "")
        url = link.get("url") or ""
        if label and url:
            candidates.append(f"{label} -> {url}")
    return _scored_sentences(candidates, terms, lead_words, 8, min_score=1)


def _contrast_hints(query, text, key_passages):
    parts = list(key_passages or [])
    parts.extend(re.split(r"(?<=[.!?])\s+|\n+", text or ""))
    terms = _query_terms(query)
    contrast_words = (
        "aber", "jedoch", "widerspruch", "dagegen", "andererseits", "kritik",
        "bestreitet", "unlike", "however", "but", "contrary", "disputed",
        "critics", "supporters", "whereas", "反对", "但是", "然而", "批评",
        "一方", "しかし", "반면", "하지만",
    )
    return _scored_sentences(parts, terms, contrast_words, 6, min_score=3)


def _scored_sentences(parts, terms, marker_words, limit, min_score=1):
    scored = []
    for raw in parts:
        sentence = _collapse_ws(raw)
        if len(sentence) < 55 or len(sentence) > 1000:
            continue
        low = sentence.lower()
        score = sum(2 for term in terms if term in low)
        score += sum(2 for word in marker_words if word in low)
        if _extract_urls(sentence):
            score += 2
        if _extract_dates(sentence):
            score += 2
        if score >= min_score:
            scored.append((score, sentence))
    scored.sort(key=lambda item: item[0], reverse=True)
    out = []
    seen = set()
    for _, sentence in scored:
        key = sentence[:180].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(sentence)
        if len(out) >= limit:
            break
    return out


def _source_perspective(result, page, research_plan):
    if result.get("impact_language") or result.get("impact_country") or result.get("impact_role"):
        return {
            "language": result.get("impact_language") or "",
            "country": result.get("impact_country") or "",
            "role": result.get("impact_role") or "",
        }
    url = result.get("url") or ""
    host = _host(url)
    haystack = " ".join(
        [
            str(result.get("title") or ""),
            str(page.get("title") or ""),
            host,
            urllib.parse.urlparse(url).path,
        ]
    ).lower()
    for item in research_plan.get("impact_plan", []) or []:
        language = str(item.get("language") or "").lower()
        country = str(item.get("country") or "")
        role = str(item.get("role") or "")
        if language and (f"language={language}" in haystack or language in haystack.split()):
            return {"language": language, "country": country, "role": role}
        country_token = country.lower().split("/")[0]
        if country_token and country_token in haystack:
            return {"language": language, "country": country, "role": role}
    tld_language = _language_from_host(host)
    if tld_language:
        return tld_language
    return {"language": "", "country": "", "role": "unknown_or_international"}


def _language_from_host(host):
    checks = [
        (".jp", "ja", "Japan"),
        (".cn", "zh", "China"),
        (".tw", "zh-TW", "Taiwan"),
        (".kr", "ko", "South Korea"),
        (".ua", "uk", "Ukraine"),
        (".ru", "ru", "Russia"),
        (".de", "de", "Germany"),
        (".fr", "fr", "France"),
        (".ir", "fa", "Iran"),
        (".il", "he", "Israel"),
    ]
    for suffix, language, country in checks:
        if host.endswith(suffix):
            return {"language": language, "country": country, "role": "host_tld_inferred"}
    return None


def _page_role(url, result, page, query):
    links = page.get("links") or []
    title = _collapse_ws(page.get("title") or result.get("title") or "")
    title_low = title.lower()
    parsed = urllib.parse.urlparse(url or "")
    path = parsed.path.lower().strip("/")
    meta = page.get("meta") or {}
    text_head = (page.get("text") or "")[:3000].lower()
    terms = _query_terms(query)
    term_hits = sum(1 for term in terms if term in title_low or term in text_head)

    article_signals = 0
    if meta.get("article:published_time") or meta.get("article:modified_time") or meta.get("author") or meta.get("article:author"):
        article_signals += 3
    if page.get("dates"):
        article_signals += 1
    article_signals += min(4, _article_link_score(url, title))
    if term_hits >= 2:
        article_signals += 1

    generic_titles = (
        "politik", "nachrichten", "news", "newsticker", "ticker", "archiv",
        "themen", "aktuell", "homepage", "index", "7-tage-ueberblick",
        "7-tage-überblick", "schlagzeilen", "meldungen",
    )
    hub_signals = 0
    if len(links) >= 35:
        hub_signals += 2
    elif len(links) >= 20:
        hub_signals += 1
    if any(word in title_low for word in generic_titles):
        hub_signals += 2
    if not path or path in {"politik", "news", "nachrichten", "index", "thema", "archiv"}:
        hub_signals += 2
    if any(part in path for part in ("newsticker", "ticker", "archiv", "thema/", "topics/")):
        hub_signals += 2

    if article_signals >= 5 and not _looks_like_navigation(title_low, path):
        return "article"
    if hub_signals >= 3 and article_signals < 6:
        return "hub"
    if len(links) >= 70 and article_signals < 5:
        return "hub"
    if article_signals >= 4:
        return "article"
    return "source"


def _reliability_label(url):
    host = _host(url)
    if _is_preferred_host(host):
        return "reliability: established_or_primary"
    if any(part in host for part in ("blog", "substack", "medium", "reddit", "x.com", "twitter", "facebook", "youtube")):
        return "reliability: commentary_or_social_check_needed"
    if host:
        return "reliability: unknown_check_needed"
    return "reliability: unknown"


def _recency_label(dates):
    parsed = [_parse_date_hint(value) for value in dates or []]
    parsed = [value for value in parsed if value]
    if not parsed:
        return "recency: no_date_detected"
    newest = max(parsed)
    age = datetime.now(timezone.utc) - newest
    if age.days < 0:
        return "recency: future_or_timezone_check"
    if age.days == 0:
        return "recency: today"
    if age.days <= 2:
        return f"recency: {age.days}d"
    if age.days <= 14:
        return f"recency: {age.days}d_check_currentness"
    if age.days <= 90:
        return f"recency: {age.days}d_old"
    return f"recency: {age.days}d_stale_unless_background"


def _source_type(url, meta):
    host = _host(url)
    if any(part in host for part in ("bundesregierung", "bundestag", "europa.eu", ".gov", ".gob", ".gouv")):
        return "primary_or_official"
    if any(part in host for part in ("reuters", "apnews", "dpa", "tagesschau", "zdf", "dw.com")):
        return "news_wire_or_public_media"
    if any(part in host for part in ("spiegel", "zeit", "faz", "sueddeutsche", "handelsblatt", "welt")):
        return "news_or_analysis"
    if meta.get("author") or meta.get("article:author"):
        return "article_with_author"
    return "web_source"


def _skip_link(url, label):
    low = f"{url} {label}".lower()
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
        host = (parsed.netloc or "").lower().removeprefix("www.")
        path = (parsed.path or "").lower()
        query = (parsed.query or "").lower()
    except Exception:
        host = ""
        path = ""
        query = ""
    if host.endswith("biggo.com") or host.endswith("linkedin.com"):
        return True
    if _is_youtube_url(url) and any(part in path for part in ("/results", "/playlist", "/shorts")):
        return True
    if re.search(r"/(search|suche)(/|$)", path) or path.rstrip("/").endswith(("/search", "/suche")):
        return True
    if "/users/" in path or "forgot" in path or "referer_url=" in query or "hot_keyword=" in query:
        return True
    bad_parts = (
        "#", "mailto:", "javascript:", "/login", "/signin", "/abo", "/newsletter",
        "/signup", "/register", "session_redirect=", "/datenschutz", "/privacy",
        "/impressum", "/kontakt", "/shop", "/account", "/mye/", "myebay",
        "bidsoffers", "security measure", "sicherheitsmaßnahme", "sicherheitsmassnahme",
        "signin.ebay.", "auth.ebay.", "ebay.de/help", "ebay.com/help",
        ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".zip", ".mp4",
        "facebook.com", "instagram.com", "whatsapp", "mailto",
        "twitter.com/intent", "x.com/intent", "twitter.com/share", "x.com/share",
        "flipboard.com", "pinterest.com", "reddit.com/submit", "/print", "print=true",
        "kommentarbereich", "comment-", "#comments", "sharearticle", "sharing",
        "trk=article-ssr",
    )
    if any(part in low for part in bad_parts):
        return True
    label_low = _collapse_ws(label or "").lower()
    bad_labels = {
        "x", "twitter", "facebook", "linkedin", "flipboard", "whatsapp",
        "teilen", "share", "vorlesen", "drucken", "print", "sign in", "login",
        "subscribe", "newsletter", "blätterkatalog", "blaetterkatalog",
        "re-use guardian content", "re-use content", "reuse content",
    }
    if label_low in bad_labels:
        return True
    return False


def _low_value_page(url, page):
    title = _collapse_ws((page or {}).get("title") or "").lower()
    text = _collapse_ws((page or {}).get("text") or "").lower()
    meta = (page or {}).get("meta") or {}
    host = _host(url)
    if (
        host in {"x.com", "twitter.com", "flipboard.com"}
        or host.endswith(".x.com")
        or host.endswith(".twitter.com")
        or host.endswith(".biggo.com")
        or host.endswith(".linkedin.com")
        or host in {"biggo.com", "linkedin.com"}
    ):
        return True
    if _is_youtube_url(url) and meta.get("source_type") != "youtube_transcript":
        return True
    if title in {"x.com", "twitter", "flipboard", "anmelden", "sign in"}:
        return True
    if any(marker in title for marker in ("blätterkatalog", "blaetterkatalog", "re-use guardian content", "search", "suche", "forgot password")):
        return True
    parsed = urllib.parse.urlparse(str(url or ""))
    path = (parsed.path or "").lower()
    if re.search(r"/(search|suche)(/|$)", path) or "/users/" in path or "forgot" in path:
        return True
    if len(text) < 160 and any(marker in text for marker in ("sign in", "enable javascript", "log in", "cookies")):
        return True
    return False


def _looks_like_tracking_id(value):
    text = _collapse_ws(value or "")
    return bool(re.fullmatch(r"[a-fA-F0-9]{16,}", text))


def _looks_like_navigation(label, path):
    label = _collapse_ws(label or "").lower()
    path = (path or "").lower().strip("/")
    nav_labels = {
        "politik", "wirtschaft", "sport", "kultur", "wissen", "panorama",
        "inland", "ausland", "news", "nachrichten", "startseite", "archiv",
        "themen", "suche", "videos", "audios", "podcasts", "live", "ticker",
        "newsticker", "schlagzeilen",
    }
    if label in nav_labels:
        return True
    if path in nav_labels:
        return True
    if len(label) <= 28 and not _extract_dates(label):
        words = set(label.split())
        if words and words <= nav_labels:
            return True
    if path.count("/") <= 1 and any(path == item or path.endswith("/" + item) for item in nav_labels):
        return True
    return False


def _is_preferred_host(host):
    return any(
        domain in host
        for domain in (
            "tagesschau", "bundestag", "bundesregierung", "faz", "zdf", "spiegel",
            "zeit", "dw.com", "reuters", "apnews", "dpa", "sueddeutsche",
            "handelsblatt", "welt.de", "bbc.", "guardian", "politico",
        )
    )


def _host(url):
    return urllib.parse.urlparse(url or "").netloc.lower().removeprefix("www.")


def _query_terms(query):
    return _important_words(query, 10)


def _is_broad_query(query):
    terms = _query_terms(query)
    if not terms:
        return True
    broad_terms = {
        "deutschland", "politik", "wirtschaft", "welt", "international",
        "europa", "usa", "news", "nachrichten", "ereignisse", "lage",
        "ticker", "liveblog", "ueberblick", "überblick",
    }
    return len(terms) <= 2 and any(term in broad_terms for term in terms)


def _important_words(text, limit):
    stop = {
        "aktuelle", "aktuell", "nachrichten", "news", "heute", "suche", "such",
        "alles", "ueber", "über", "was", "gibt", "neues", "finde", "bitte",
        "der", "die", "das", "und", "oder", "mit", "von", "fuer", "für",
        "the", "and", "for", "about", "latest",
    }
    words = []
    seen = set()
    for raw in re.findall(r"[\wÄÖÜäöüß-]{3,}", text or "", flags=re.U):
        word = raw.lower()
        if word in stop or word in seen:
            continue
        seen.add(word)
        words.append(word)
        if len(words) >= limit:
            break
    return words


def _parse_date_hint(value):
    text = _collapse_ws(value)
    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%d.%m.%Y",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    m = re.search(r"\b((?:19|20)\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])\b", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except Exception:
            return None
    m = re.search(r"\b(0?[1-9]|[12]\d|3[01])[.](0?[1-9]|1[0-2])[.]((?:19|20)\d{2})\b", text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _fetch_url(url, timeout_s, allow_search_page=False):
    if not allow_search_page and not _is_http_url(url):
        raise ValueError("Nur http/https URLs sind erlaubt.")
    req = urllib.request.Request(
        _iri_to_uri(url),
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 AgentDeepDive/1.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        data = resp.read(2_000_000)
    return data.decode(charset, errors="replace")


def _iri_to_uri(url):
    parsed = urllib.parse.urlsplit(url)
    netloc = parsed.netloc.encode("idna").decode("ascii")
    path = urllib.parse.quote(parsed.path, safe="/%:@")
    query = urllib.parse.quote(parsed.query, safe="=&?/%:@+;,")
    fragment = urllib.parse.quote(parsed.fragment, safe="=&?/%:@+;,")
    return urllib.parse.urlunsplit((parsed.scheme, netloc, path, query, fragment))


def _parse_page(url, page_html):
    parser = ReadableHtmlParser(url)
    parser.feed(page_html)
    return parser.readable()


def _crawl_note(crawl_id, query, result, page, max_chars):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    analysis_query = result.get("_analysis_query") or result.get("search_query") or query
    key_passages = result.get("_key_passages") or []
    causality_hints = result.get("_causality_hints") or []
    claim_hints = result.get("_claim_hints") or []
    event_hints = result.get("_event_hints") or []
    lead_hints = result.get("_lead_hints") or []
    contrast_hints = result.get("_contrast_hints") or []
    perspective = result.get("_perspective") or {}
    text = _prepared_source_text(analysis_query, page["text"], key_passages)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[Text gekuerzt]"
    meta = page["meta"]
    lines = [
        "DEEPDIVE_CRAWL_NOTE",
        f"crawl_id: {crawl_id}",
        f"captured_at_utc: {now}",
        f"source_last_seen_utc: {now}",
        f"topic: {query}",
        f"source_depth: {int(result.get('depth') or 0)}",
        f"page_role: {result.get('_page_role') or 'source'}",
        f"discovery_method: {result.get('discovery_method') or 'search'}",
        f"discovery_reason: {result.get('discovery_reason') or ''}",
        f"branch_name: {result.get('branch_name') or ''}",
        f"branch_reason: {result.get('branch_reason') or ''}",
        f"subcrawl_id: {result.get('subcrawl_id') or ''}",
        f"subcrawl_topic: {result.get('subcrawl_topic') or ''}",
        f"subcrawl_reason: {result.get('subcrawl_reason') or ''}",
        f"parent_url: {result.get('parent_url') or ''}",
        f"search_query: {result.get('search_query', '')}",
        f"analysis_query: {analysis_query}",
        f"source_url: {result.get('url', '')}",
        f"source_title: {page['title'] or result.get('title') or ''}",
        f"source_type: {_source_type(result.get('url', ''), meta)}",
        f"source_language: {perspective.get('language', '')}",
        f"source_country: {perspective.get('country', '')}",
        f"perspective_role: {perspective.get('role', '')}",
        f"source_reliability: {result.get('_reliability') or _reliability_label(result.get('url', ''))}",
        f"relevance_score: {result.get('_relevance_score', 0)}",
        f"recency_label: {result.get('_recency_label') or _recency_label(page.get('dates') or [])}",
    ]
    if meta.get("author") or meta.get("article:author"):
        lines.append(f"author: {meta.get('author') or meta.get('article:author')}")
    if meta.get("publisher") or meta.get("og:site_name"):
        lines.append(f"publisher: {meta.get('publisher') or meta.get('og:site_name')}")
    dates = _unique((page.get("dates") or []) + _extract_dates(result.get("snippet", "")))
    if dates:
        lines.append("date_hints: " + "; ".join(dates[:12]))
    if result.get("snippet"):
        lines.append("search_snippet: " + result["snippet"])
    if key_passages:
        lines.append("key_passages:")
        for passage in key_passages[:8]:
            lines.append("- " + passage)
    if event_hints:
        lines.append("event_hints:")
        for hint in event_hints[:6]:
            lines.append("- " + hint)
    if claim_hints:
        lines.append("claim_hints:")
        for hint in claim_hints[:6]:
            lines.append("- " + hint)
    if causality_hints:
        lines.append("causality_hints:")
        for hint in causality_hints[:6]:
            lines.append("- " + hint)
    if contrast_hints:
        lines.append("contrast_hints:")
        for hint in contrast_hints[:6]:
            lines.append("- " + hint)
    if lead_hints:
        lines.append("lead_hints:")
        for hint in lead_hints[:8]:
            lines.append("- " + hint)
    lines.extend(
        [
            "assessment_required: relevance, reliability, date_context, uncertainty, contradictions, causal_links, perspective_contrast, leads_to_follow",
            "source_text:",
            text,
        ]
    )
    links = page.get("links") or []
    note_links = _links_for_note(links, query)
    if note_links:
        lines.append("source_links:")
        for link in note_links[:12]:
            lines.append(f"- {link['text']} | {link['url']}")
    return "\n".join(lines)


def _crawl_manifest_note(
    crawl_id,
    query,
    crawl_started_iso,
    fetched,
    failed,
    search_errors,
    research_plan,
    subcrawl_plan,
    trace,
    tool_trace,
):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "DEEPDIVE_CRAWL_MANIFEST",
        f"crawl_id: {crawl_id}",
        f"captured_at_utc: {now}",
        f"crawl_started_at_utc: {crawl_started_iso}",
        f"source_last_seen_utc: {now}",
        f"topic: {query}",
        "source_title: Crawl Manifest",
        f"impact_languages: {', '.join(item.get('language', '') for item in research_plan.get('impact_plan', []))}",
        f"impact_regions: {', '.join(_unique([item.get('country', '') for item in research_plan.get('impact_plan', []) if item.get('country')]))}",
        f"branch_queries: {' | '.join(research_plan.get('branch_queries', [])[:12])}",
        f"subcrawl_candidates: {len(subcrawl_plan or [])}",
        f"subcrawls_run: {sum(1 for item in (subcrawl_plan or []) if item.get('status') == 'run')}",
        "research_objective: reconstruct_events_claims_leads_causal_links_perspectives_without_own_opinion",
        f"sources_fetched: {len(fetched)}",
        f"failed_count: {len(failed)}",
        f"search_error_count: {len(search_errors)}",
        "search_plan:",
    ]
    for idx, item in enumerate(research_plan.get("impact_plan", [])[:12], 1):
        lines.append(
            f"{idx}. language={item.get('language','')} country={item.get('country','')} role={item.get('role','')} reason={item.get('reason','')}"
        )
    if research_plan.get("branch_plan"):
        lines.append("branch_plan:")
        for idx, item in enumerate(research_plan.get("branch_plan", [])[:12], 1):
            lines.append(
                f"{idx}. branch={item.get('branch','')} reason={item.get('reason','')} query={item.get('query','')}"
            )
    if subcrawl_plan:
        lines.append("subcrawl_plan:")
        for idx, item in enumerate((subcrawl_plan or [])[:12], 1):
            lines.append(
                f"{idx}. status={item.get('status','')} score={item.get('score','')} family={item.get('family','')} recommendation={item.get('recommendation','')} worth_followup={item.get('worth_followup','')} branch={item.get('branch','')} topic={item.get('topic','')} reason={item.get('reason','')} next_step={item.get('next_step','')} query={item.get('query','')}"
            )
    lines.extend([
        "sources:",
    ])
    for idx, item in enumerate(fetched, 1):
        dates = "; ".join(item.get("dates") or [])
        lines.append(
            f"{idx}. role={item.get('page_role', 'source')} branch={item.get('branch_name','')} subcrawl={item.get('subcrawl_id','')} subcrawl_topic={item.get('subcrawl_topic','')} perspective={item.get('perspective_role','')} country={item.get('source_country','')} language={item.get('source_language','')} depth={item.get('depth')} method={item.get('discovery_method')} score={item.get('relevance_score')} recency={item.get('recency_label')} title={item.get('title')} url={item.get('url')} parent={item.get('parent_url')} dates={dates}"
        )
    if failed:
        lines.append("failures:")
        lines.extend(f"- {entry}" for entry in failed[:20])
    if search_errors:
        lines.append("search_errors:")
        lines.extend(f"- {entry}" for entry in search_errors[:20])
    lines.append("tool_trace:")
    lines.extend(_tool_trace_lines(tool_trace))
    lines.append("trace:")
    lines.extend(_trace_lines(trace))
    return "\n".join(lines)


def _trace(trace, event, details=None):
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
    }
    if isinstance(details, dict):
        for key, value in details.items():
            entry[key] = value
    trace.append(entry)


def _tool_trace(trace, tool, status, details=None):
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": tool,
        "status": status,
    }
    if isinstance(details, dict):
        for key, value in details.items():
            entry[key] = value
    trace.append(entry)


def _tool_trace_lines(trace):
    lines = []
    for idx, entry in enumerate(trace, 1):
        tool = entry.get("tool", "tool")
        status = entry.get("status", "INFO")
        parts = []
        for key, value in entry.items():
            if key in {"tool", "status", "ts"}:
                continue
            parts.append(f"{key}={_trace_value(value)}")
        suffix = " | " + " | ".join(parts) if parts else ""
        lines.append(f"{idx:03d}. {entry.get('ts', '')} {tool} {status}{suffix}")
    return lines


def _trace_lines(trace):
    lines = []
    for idx, entry in enumerate(trace, 1):
        event = entry.get("event", "event")
        parts = []
        for key, value in entry.items():
            if key in {"event", "ts"}:
                continue
            parts.append(f"{key}={_trace_value(value)}")
        suffix = " | " + " | ".join(parts) if parts else ""
        lines.append(f"{idx:03d}. {entry.get('ts', '')} {event}{suffix}")
    return lines


def _trace_value(value):
    if isinstance(value, list):
        if not value:
            return "[]"
        compact = ", ".join(_collapse_ws(str(v)) for v in value[:8])
        if len(value) > 8:
            compact += f", ...(+{len(value) - 8})"
        return "[" + compact + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return _collapse_ws(str(value))[:500]


def _stored_id(storage_msg):
    m = re.search(r"id:\s*([A-Za-z0-9_-]+)", storage_msg or "")
    return m.group(1) if m else ""


def _extract_crawl_id(text):
    m = re.search(r"\bdd-\d{8}T\d{6}Z-[a-fA-F0-9]{8}\b", str(text or ""))
    return m.group(0) if m else ""


def _dominant_crawl_id(entries):
    counts = {}
    order = []
    for entry in entries:
        text = str(entry.get("text") or "")
        crawl_id = str(entry.get("crawl_id") or _line_value(text, "crawl_id") or "")
        if not crawl_id:
            continue
        if crawl_id not in counts:
            order.append(crawl_id)
            counts[crawl_id] = 0
        counts[crawl_id] += 1
    if not counts:
        return ""
    return sorted(order, key=lambda value: counts[value], reverse=True)[0]


def _pack_entry_sort_key(entry):
    text = str(entry.get("text") or "")
    relevance = entry.get("relevance_score") or _line_value(text, "relevance_score")
    try:
        relevance_value = int(float(str(relevance).strip()))
    except Exception:
        relevance_value = 0
    depth = entry.get("source_depth") or _line_value(text, "source_depth")
    try:
        depth_value = int(float(str(depth).strip()))
    except Exception:
        depth_value = 9
    timestamp = str(entry.get("timestamp") or entry.get("captured_at_utc") or _line_value(text, "captured_at_utc") or "")
    return (relevance_value, -depth_value, timestamp)


def _section_bullets(text, section, max_items):
    wanted = section.rstrip(":") + ":"
    lines = str(text or "").splitlines()
    out = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if not in_section:
            if stripped == wanted:
                in_section = True
            continue
        if stripped.startswith("- "):
            out.append(_collapse_ws(stripped[2:]))
            if len(out) >= max_items:
                break
            continue
        if stripped and not line.startswith((" ", "\t")):
            break
    return [item for item in out if item]


def _numbered_section_lines(text, section, max_items):
    wanted = section.rstrip(":") + ":"
    lines = str(text or "").splitlines()
    out = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if not in_section:
            if stripped == wanted:
                in_section = True
            continue
        if re.match(r"^\d+\.\s+", stripped):
            out.append(_collapse_ws(re.sub(r"^\d+\.\s+", "", stripped)))
            if len(out) >= max_items:
                break
            continue
        if stripped and stripped.endswith(":") and not line.startswith((" ", "\t")):
            break
    return [item for item in out if item]


def _source_text_lines(text, max_items):
    payload = _after_marker(text, "source_text:", 2400)
    out = []
    for line in payload.splitlines():
        line = _collapse_ws(line)
        if not line or _is_boilerplate_line(line):
            continue
        out.append(line)
        if len(out) >= max_items:
            break
    return out


def _after_marker(text, marker, max_chars):
    raw = str(text or "")
    idx = raw.find(marker)
    if idx < 0:
        return ""
    return raw[idx + len(marker):].strip()[:max_chars]


def _prepared_source_text(query, raw_text, key_passages):
    terms = _query_terms(query)
    prepared = []
    seen = set()

    def add_line(line):
        line = _collapse_ws(line)
        if not line:
            return
        key = line[:180].lower()
        if key in seen:
            return
        seen.add(key)
        prepared.append(line)

    for passage in key_passages:
        add_line(passage)

    for raw in (raw_text or "").splitlines():
        line = _collapse_ws(raw)
        if not line or _is_boilerplate_line(line):
            continue
        low = line.lower()
        term_hit = any(term in low for term in terms)
        date_hit = bool(_extract_dates(line))
        if len(line) < 55 and not term_hit and not date_hit:
            continue
        if term_hit or date_hit or len(line) >= 110:
            add_line(line)
        if len(prepared) >= 90:
            break

    return "\n".join(prepared)


def _is_boilerplate_line(line):
    low = line.strip().lower()
    if not low:
        return True
    exact = {
        "menu", "menü", "suche", "startseite", "newsletter", "impressum",
        "datenschutz", "kontakt", "anmelden", "registrieren", "mein konto",
        "dark-mode", "cookie-einstellungen", "agb", "hilfe", "archiv",
        "videos", "audios", "podcast", "podcasts", "bilder", "shop",
        "anzeige", "mehr", "teilen", "rss", "facebook", "x", "whatsapp",
    }
    if low in exact:
        return True
    boilerplate_bits = (
        "untermenü", "copyright", "©", "cookie", "datenschutzerklärung",
        "nutzungsrechte", "werbemöglichkeiten", "abo kündigen", "push-mitteilungen",
        "newsletter abonnieren", "auf facebook", "bei x folgen", "google news",
        "app installieren", "jetzt kostenlos", "gutscheincode", "gewinnspiel",
    )
    if any(bit in low for bit in boilerplate_bits):
        return True
    if re.fullmatch(r"[a-zäöüß -]{2,28}", low) and not _extract_dates(low):
        generic_words = {
            "politik", "wirtschaft", "sport", "lokales", "kultur", "wetter",
            "panorama", "ausland", "inland", "wissen", "themen", "service",
            "über uns", "unternehmen", "karriere", "redaktion",
        }
        parts = set(low.split())
        if parts & generic_words and len(parts) <= 3:
            return True
    return False


def _links_for_note(links, query):
    scored = []
    for link in links:
        url = _clean_url(link.get("url") or "")
        if not url or _skip_link(url, link.get("text") or ""):
            continue
        score, reason = _link_score(query, {**link, "url": url}, "")
        if score <= 0:
            continue
        scored.append((score, {**link, "url": url, "reason": reason}))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [link for _, link in scored]


def _source_note(source):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = _extract_url(source) or "(unbekannte URL)"
    dates = _extract_dates(source)
    lines = [
        "DEEPDIVE_RAG_NOTE",
        f"captured_at_utc: {now}",
        f"source_url: {url}",
    ]
    if dates:
        lines.append("date_hints: " + "; ".join(dates[:10]))
    lines.extend(
        [
            "assessment_required: relevance, reliability, date_context, uncertainty, claims, leads, causal_links, perspective_contrast",
            "source_material:",
            source.strip(),
        ]
    )
    return "\n".join(lines)


def _store_rag_note(note, config):
    if not isinstance(config, dict):
        return False, "RAG nicht gespeichert: Tool-Konfig fehlt."
    data_dir = str(config.get("data_dir") or "").strip()
    pool = str(config.get("rag_pool") or "DeepDive").strip() or "DeepDive"
    safe_pool = _safe_id(pool) or "DeepDive"
    if not data_dir:
        return False, "RAG nicht gespeichert: data_dir fehlt."
    rag_dir = os.path.join(data_dir, "rag", safe_pool)
    try:
        os.makedirs(rag_dir, exist_ok=True)
        source_url = _line_value(note, "source_url")
        dedupe_hours = int(config.get("dedupe_source_url_hours") or 72)
        if source_url and dedupe_hours > 0:
            existing = _existing_rag_for_source_url(rag_dir, source_url, dedupe_hours)
            if existing:
                existing_id, existing_age = existing
                return True, (
                    f"Im RAG Pool '{safe_pool}' bereits vorhanden "
                    f"(id: {existing_id[:8]}, source_url_dedupe, age_h: {existing_age:.1f})"
                )
        entry_id = str(uuid4())
        entry = {
            "id": entry_id,
            "text": note,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "keywords": _keywords(note),
            "crawl_id": _line_value(note, "crawl_id"),
            "source_url": _line_value(note, "source_url"),
            "source_title": _line_value(note, "source_title"),
            "captured_at_utc": _line_value(note, "captured_at_utc"),
            "source_depth": _line_value(note, "source_depth"),
            "page_role": _line_value(note, "page_role"),
            "source_language": _line_value(note, "source_language"),
            "source_country": _line_value(note, "source_country"),
            "perspective_role": _line_value(note, "perspective_role"),
            "discovery_method": _line_value(note, "discovery_method"),
            "branch_name": _line_value(note, "branch_name"),
            "branch_reason": _line_value(note, "branch_reason"),
            "subcrawl_id": _line_value(note, "subcrawl_id"),
            "subcrawl_topic": _line_value(note, "subcrawl_topic"),
            "subcrawl_reason": _line_value(note, "subcrawl_reason"),
            "parent_url": _line_value(note, "parent_url"),
            "recency_label": _line_value(note, "recency_label"),
            "relevance_score": _line_value(note, "relevance_score"),
        }
        path = os.path.join(rag_dir, f"{entry_id}.json")
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)
        return True, f"Im RAG Pool '{safe_pool}' gespeichert (id: {entry_id[:8]})"
    except Exception as exc:
        return False, f"RAG speichern fehlgeschlagen: {exc}"


def _existing_rag_for_source_url(rag_dir, source_url, max_age_hours):
    wanted = _canonical_source_url(source_url)
    if not wanted:
        return None
    now = datetime.now(timezone.utc)
    try:
        files = sorted(
            (p for p in os.scandir(rag_dir) if p.is_file() and p.name.endswith(".json")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except FileNotFoundError:
        return None
    for entry in files[:3000]:
        try:
            with open(entry.path, encoding="utf-8") as fh:
                data = json.load(fh)
            existing_url = data.get("source_url") or _line_value(data.get("text", ""), "source_url")
            if _canonical_source_url(existing_url) != wanted:
                continue
            ts = _parse_rag_timestamp(data.get("timestamp") or data.get("captured_at_utc"))
            age_h = (now - ts).total_seconds() / 3600 if ts else 0.0
            if age_h <= max_age_hours:
                return data.get("id") or os.path.splitext(entry.name)[0], age_h
        except Exception:
            continue
    return None


def _canonical_source_url(url):
    parsed = urllib.parse.urlparse(_clean_url(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    drop_prefixes = ("utm_",)
    drop_names = {"fbclid", "gclid", "mc_cid", "mc_eid", "cmpid", "xtor"}
    query_pairs = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        low = key.lower()
        if low in drop_names or any(low.startswith(prefix) for prefix in drop_prefixes):
            continue
        query_pairs.append((key, value))
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urllib.parse.urlencode(query_pairs, doseq=True)
    return urllib.parse.urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def _parse_rag_timestamp(value):
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _first_param(params, key):
    if isinstance(params, dict):
        return str(params.get(key) or params.get("0") or "").strip()
    if not params:
        return ""
    raw = str(params[0]).strip()
    m = re.match(rf"^\s*{re.escape(key)}\s*[:=]\s*(.+)$", raw, flags=re.I | re.S)
    return (m.group(1) if m else raw).strip().strip("\"'")


def _line_value(text, key):
    prefix = key + ":"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _clean_url(url):
    url = html.unescape((url or "").strip())
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def _extract_rss_item_urls(packet):
    urls = []
    seen = set()
    for line in (packet or "").splitlines():
        value = ""
        stripped = line.strip()
        if stripped.startswith("item_url:"):
            value = stripped.split("item_url:", 1)[1].strip()
        elif stripped.startswith("url:"):
            value = stripped.split("url:", 1)[1].strip()
        if not value:
            continue
        value = _clean_url(value)
        if value and value not in seen:
            seen.add(value)
            urls.append(value)
    return urls


def _unwrap_duckduckgo_url(raw_url):
    url = html.unescape(raw_url)
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):
        url = "https://duckduckgo.com" + url
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    if "uddg" in qs and qs["uddg"]:
        return urllib.parse.unquote(qs["uddg"][0])
    if parsed.scheme in {"http", "https"}:
        return url
    return ""


def _is_http_url(url):
    return urllib.parse.urlparse(url).scheme in {"http", "https"}


def _is_allowed_http_url(url, allow_private):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if allow_private:
        return True
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return False
    except Exception:
        return False
    return True


def _collapse_ws(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _normalize_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [_collapse_ws(line) for line in text.splitlines()]
    lines = [line for line in lines if line and len(line) > 1]
    return "\n".join(lines)


def _strip_html(text):
    return _collapse_ws(re.sub(r"<[^>]+>", " ", text or ""))


def _extract_url(text):
    m = re.search(r"https?://[^\s<>\"]+", text)
    if not m:
        return ""
    return m.group(0).rstrip(".,);]")


def _extract_urls(text, allowed_hosts=None):
    urls = []
    seen = set()
    for raw in re.findall(r"https?://[^\s<>\"]+", text or ""):
        raw = raw.rstrip(".,);]}'\"")
        url = _clean_url(raw)
        if not url:
            continue
        if allowed_hosts:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            allowed = any(host == allowed_host or host.endswith("." + allowed_host) for allowed_host in allowed_hosts)
            if not allowed:
                continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _extract_dates(text):
    patterns = [
        r"\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b",
        r"\b(?:0?[1-9]|[12]\d|3[01])[.](?:0?[1-9]|1[0-2])[.](?:19|20)\d{2}\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, (?:19|20)\d{2}\b",
        r"\b\d{1,2}\. (?:Januar|Februar|Maerz|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember) (?:19|20)\d{2}\b",
    ]
    found = []
    seen = set()
    for pattern in patterns:
        for value in re.findall(pattern, text or "", flags=re.I):
            value = re.sub(r"\s+", " ", value).strip()
            if value and value not in seen:
                seen.add(value)
                found.append(value)
    return found


def _unique(values):
    result = []
    seen = set()
    for value in values:
        value = _collapse_ws(str(value))
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _csv_list(value, limit=10):
    if isinstance(value, (list, tuple, set)):
        parts = [str(item).strip() for item in value]
    else:
        parts = [part.strip() for part in re.split(r"[,\n;]+", str(value or ""))]
    result = []
    seen = set()
    for part in parts:
        if not part:
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(part)
        if len(result) >= limit:
            break
    return result


def _safe_id(value):
    if not value or len(value) > 128:
        return ""
    if ".." in value or "/" in value or "\\" in value or "\x00" in value:
        return ""
    if not re.match(r"^[A-Za-z0-9._-]+$", value):
        return ""
    return value


def _clamp_int(value, default, low, high):
    try:
        n = int(value)
    except Exception:
        n = default
    return max(low, min(high, n))


def _cfg_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "ja", "on", "y"}:
        return True
    if text in {"0", "false", "no", "nein", "off", "n"}:
        return False
    return default


def _keywords(text):
    stopwords = {
        "der", "die", "das", "und", "oder", "aber", "ist", "war", "sind", "waren",
        "ein", "eine", "einer", "eines", "einem", "einen", "den", "dem", "des",
        "mit", "von", "bei", "nach", "vor", "ueber", "über", "nicht", "kein",
        "keine", "the", "and", "or", "but", "is", "was", "are", "were", "for",
        "with", "from", "source", "text", "https", "http",
    }
    words = []
    seen = set()
    for raw in text.split():
        word = re.sub(r"^\W+|\W+$", "", raw.lower())
        if len(word) > 2 and word not in stopwords and word not in seen:
            seen.add(word)
            words.append(word)
            if len(words) >= 80:
                break
    return words


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
