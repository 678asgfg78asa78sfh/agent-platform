"""Deepdive crawler and RAG helpers.

DeepDive is not just a prompt checklist. The chat agent may still orchestrate
search/fetch/RAG manually, but broad research tasks should start with
``deepdive.crawl`` so the platform gathers current sources deterministically
before the LLM writes a synthesis.
"""

import html
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from uuid import uuid4


MODULE = {
    "name": "deepdive",
    "description": "Crawler fuer mehrstufige Recherche: Web suchen, Quellen abrufen, Datum/Text/Links extrahieren und RAG-Notizen speichern.",
    "version": "1.7",
    "settings": {
        "max_sources": {"type": "number", "label": "Seed-Quellen pro Crawl", "default": 8},
        "max_search_queries": {"type": "number", "label": "Suchvarianten", "default": 6},
        "max_total_pages": {"type": "number", "label": "Max Seiten inkl. Follow-ups", "default": 20},
        "max_follow_links_per_source": {"type": "number", "label": "Follow-up Links je Quelle", "default": 3},
        "max_depth": {"type": "number", "label": "Crawl-Tiefe", "default": 2},
        "max_derived_queries": {"type": "number", "label": "Abgeleitete Nachsuchen", "default": 4},
        "timeout_s": {"type": "number", "label": "Timeout je Request", "default": 6},
        "python_timeout_s": {"type": "number", "label": "Python Tool Timeout", "default": 120},
        "max_chars_per_source": {"type": "number", "label": "Max Textzeichen je Quelle", "default": 6000},
        "allow_private_networks": {"type": "bool", "label": "Private/LAN URLs erlauben", "default": False},
    },
    "tools": [
        {
            "name": "deepdive.crawl",
            "description": "Fuehrt einen kompakten Web-Crawl fuer ein Thema aus, prueft aktuelle Suchvarianten, oeffnet Quellen und speichert verwertbare Quellen-Notizen direkt im RAG.",
            "params": ["query"],
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
1. Bei breiten Web-/News-/Personen-Recherchen zuerst deepdive.crawl(query) nutzen. Das Tool sucht aktuelle Varianten, oeffnet Seed-Quellen, verfolgt relevante Links daraus und legt alles mit Crawl-ID im RAG ab.
2. Such-Snippets sind nur Wegweiser, keine Belege. Inhalte erst behaupten, nachdem die Quelle geoeffnet oder per deepdive.crawl verarbeitet wurde.
3. Quellen nicht isoliert lesen: aus Titel/Text/Links neue Hinweise ableiten, passende Follow-up-Quellen oeffnen, Datum/Stand und Widersprueche vergleichen.
4. Jede Quelle bewerten: URL, Titel, Datum/Stand, Abrufzeit, Autor/Outlet, primaer/sekundaer, Relevanz, Zuverlaessigkeit, Bias/Risiko, Kernaussagen, offene Unsicherheiten.
5. Jede verwertbare Beobachtung sofort mit deepdive.source_note als einzelne RAG-Notiz speichern, wenn sie nicht schon durch deepdive.crawl gespeichert wurde.
6. Bei aktuellen Personen/Politik/News aktiv nach dem aktuellen Stand suchen. Alte Rollen wie "Kanzlerkandidat" duerfen nicht als aktueller Stand stehen bleiben, wenn spaetere Quellen "Kanzler", "ehemalig" oder andere Rollen zeigen.
7. Nicht nach der ersten Notiz stoppen: mehrere unabhaengige Quellen vergleichen, vor allem bei Zeitbezug oder widerspruechlichen Rollen.
8. Vor der Synthese rag.suchen mit der crawl_id und dem Thema ausfuehren, damit das Lagebild aus frisch gespeicherten Beobachtungen entsteht.
9. Ergebnis liefern: aktueller Stand, Timeline, gesicherte Punkte, Widersprueche, Quellenliste mit Herkunft/Alter, naechste sinnvolle Rechercheschritte.
Regeln: Ein Toolcall pro Antwort. Bei Toolfehlern anders versuchen. Die Finalantwort ist nie ein rohes SUCCESS/Tool-Ergebnis."""


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
        dates = _unique(self.date_candidates + _extract_dates(text))[:14]
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
    max_sources = _clamp_int(config.get("max_sources"), 8, 1, 12)
    max_search_queries = _clamp_int(config.get("max_search_queries"), 6, 1, 10)
    max_total_pages = _clamp_int(config.get("max_total_pages"), 20, max_sources, 30)
    max_follow_links = _clamp_int(config.get("max_follow_links_per_source"), 3, 0, 6)
    max_depth = _clamp_int(config.get("max_depth"), 2, 0, 2)
    max_derived_queries = _clamp_int(config.get("max_derived_queries"), 4, 0, 8)
    timeout_s = _clamp_int(config.get("timeout_s"), 6, 3, 12)
    python_timeout_s = _clamp_int(config.get("python_timeout_s"), 120, 20, 180)
    max_chars = _clamp_int(config.get("max_chars_per_source"), 6000, 1200, 16000)
    allow_private = bool(config.get("allow_private_networks") or False)
    deadline = time.monotonic() + max(10, python_timeout_s - 5)
    crawl_started_at = datetime.now(timezone.utc)
    crawl_started_iso = crawl_started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    crawl_id = "dd-" + crawl_started_at.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    trace = []
    tool_trace = []
    _trace(
        trace,
        "crawl.start",
        {
            "query": query,
            "max_sources": max_sources,
            "max_search_queries": max_search_queries,
            "max_total_pages": max_total_pages,
            "max_follow_links_per_source": max_follow_links,
            "max_depth": max_depth,
            "max_derived_queries": max_derived_queries,
            "timeout_s": timeout_s,
            "python_timeout_s": python_timeout_s,
        },
    )
    _tool_trace(
        tool_trace,
        "deepdive.crawl",
        "START",
        {
            "query": query,
            "max_search_queries": max_search_queries,
            "max_total_pages": max_total_pages,
        },
    )

    search_queries = _build_search_queries(query, max_search_queries)
    executed_searches = []
    search_errors = []
    candidates = []
    candidate_urls = set()

    for search_query in search_queries:
        if time.monotonic() > deadline:
            search_errors.append("Zeitbudget vor Ende der Seed-Suche erreicht")
            _trace(trace, "crawl.deadline", {"stage": "seed_search"})
            break
        executed_searches.append(search_query)
        try:
            _trace(trace, "search.start", {"query": search_query, "engine": "duckduckgo_lite"})
            raw_results = _search_duckduckgo(search_query, 10, timeout_s)
            accepted = 0
            for result in raw_results:
                url = _clean_url(result.get("url") or "")
                if not url or url in candidate_urls:
                    continue
                if not _is_allowed_http_url(url, allow_private):
                    continue
                candidate_urls.add(url)
                result["search_query"] = search_query
                result["depth"] = 0
                result["parent_url"] = ""
                result["discovery_method"] = "search"
                result["discovery_reason"] = f"Suchtreffer fuer: {search_query}"
                candidates.append(result)
                accepted += 1
            _trace(
                trace,
                "search.done",
                {"query": search_query, "results": len(raw_results), "accepted": accepted},
            )
            _tool_trace(
                tool_trace,
                "duckduckgo.search",
                "OK",
                {
                    "phase": "seed",
                    "query": search_query,
                    "results": len(raw_results),
                    "accepted": accepted,
                },
            )
        except Exception as exc:
            search_errors.append(f"{search_query}: {exc}")
            _trace(trace, "search.fail", {"query": search_query, "error": str(exc)})
            _tool_trace(
                tool_trace,
                "duckduckgo.search",
                "FAIL",
                {"phase": "seed", "query": search_query, "error": str(exc)},
            )

    # ── RSS-Integration: RSS-Quellen als zusätzliche Kandidaten ──
    rss_candidates_added = 0
    try:
        from modules.rss_verwaltung.module import _fuer_deepdive
        rss_source_list = _fuer_deepdive(json.dumps({
            "query": query,
            "refresh": True,
            "stale_after_minutes": 30,
            "limit_sources": 8,
            "limit_items": 12,
            "max_items_per_source": 30,
        }))
        # Nur echte Item-URLs extrahieren – nicht den ganzen RSS-Block in den LLM-Kontext kippen.
        if rss_source_list:
            rss_urls = _extract_rss_item_urls(rss_source_list)
            for rss_url in rss_urls[:8]:
                if rss_url not in candidate_urls and _is_allowed_http_url(rss_url, allow_private):
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
    except Exception:
        pass  # RSS-Modul nicht verfügbar oder Fehler – kein Beinbruch
    if rss_candidates_added:
        _trace(trace, "rss.inject", {"added": rss_candidates_added})
        _tool_trace(tool_trace, "rss_verwaltung.fuer_deepdive", "OK", {"added_candidates": rss_candidates_added})
    selected = _select_sources(candidates, max_sources, query)
    _trace(
        trace,
        "sources.select",
        {
            "candidates": len(candidates),
            "selected": len(selected),
            "urls": [item["url"] for item in selected],
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

    while crawl_queue and len(fetched) < max_total_pages:
        if time.monotonic() > deadline:
            failed.append("Zeitbudget erreicht, Crawl begrenzt beendet")
            _trace(trace, "crawl.deadline", {"stage": "fetch_loop", "fetched": len(fetched)})
            break
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
            page_html = _fetch_url(url, timeout_s)
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
            relevance_score = _relevance_score(query, result, page)
            reliability = _reliability_label(url)
            recency = _recency_label(page.get("dates") or [])
            key_passages = _key_passages(query, page.get("text") or "", page.get("dates") or [])
            causality_hints = _causality_hints(query, page.get("text") or "", key_passages)
            page_role = _page_role(url, result, page, query)
            result["_relevance_score"] = relevance_score
            result["_reliability"] = reliability
            result["_recency_label"] = recency
            result["_key_passages"] = key_passages
            result["_causality_hints"] = causality_hints
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
                    "page_role": page_role,
                    "stored": stored,
                    "storage_msg": storage_msg,
                    "chars": len(page["text"]),
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
                    try:
                        _trace(
                            trace,
                            "derived_search.start",
                            {"query": derived_query, "from_url": url},
                        )
                        derived_candidates = []
                        for dres in _search_duckduckgo(derived_query, 5, timeout_s):
                            durl = _clean_url(dres.get("url") or "")
                            if not durl or durl in known_urls:
                                continue
                            if not _is_allowed_http_url(durl, allow_private):
                                continue
                            dres["url"] = durl
                            dres["search_query"] = derived_query
                            dres["depth"] = depth + 1
                            dres["parent_url"] = url
                            dres["discovery_method"] = "derived_search"
                            dres["discovery_reason"] = f"Nachsuche aus Quelle: {page['title'] or url}"
                            derived_candidates.append(dres)
                        selected_derived = _select_sources(derived_candidates, 2, derived_query)
                        _trace(
                            trace,
                            "derived_search.done",
                            {
                                "query": derived_query,
                                "candidates": len(derived_candidates),
                                "selected": len(selected_derived),
                                "urls": [item["url"] for item in selected_derived],
                            },
                        )
                        _tool_trace(
                            tool_trace,
                            "duckduckgo.search",
                            "OK",
                            {
                                "phase": "derived",
                                "query": derived_query,
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
                            "duckduckgo.search",
                            "FAIL",
                            {
                                "phase": "derived",
                                "query": derived_query,
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

    _trace(
        trace,
        "crawl.finish",
        {
            "sources_fetched": len(fetched),
            "seed_sources": len(selected),
            "followup_links_queued": followed_links,
            "derived_queries_run": derived_queries_done,
            "derived_sources_queued": derived_sources_added,
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
        f"searched: {', '.join(executed_searches)}",
        f"candidates: {len(candidates)}",
        f"seed_sources: {len(selected)}",
        f"followup_links_queued: {followed_links}",
        f"derived_queries_run: {derived_queries_done}",
        f"derived_sources_queued: {derived_sources_added}",
        f"sources_fetched: {len(fetched)}/{max_total_pages}",
        f"rag_pool: {str(config.get('rag_pool') or 'DeepDive')}",
        f"manifest: {manifest_msg}",
        "",
        "Quellen (Seeds + Follow-ups):",
    ]
    if fetched:
        for idx, item in enumerate(fetched, 1):
            dates = "; ".join(item["dates"][:5]) if item["dates"] else "kein Datum erkannt"
            lines.append(
                f"{idx}. role={item.get('page_role', 'source')} | depth={item['depth']} {item['discovery_method']} | score={item['relevance_score']} | {item['reliability']} | {item['recency_label']} | {item['title']} | {item['url']} | dates: {dates} | {item['storage_msg']}"
            )
    else:
        lines.append("- keine Quelle erfolgreich verarbeitet")

    if failed:
        lines.extend(["", "Fehler/ausgelassen:"])
        lines.extend(f"- {entry}" for entry in failed[:8])
    if search_errors:
        lines.extend(["", "Suchfehler:"])
        lines.extend(f"- {entry}" for entry in search_errors[:4])

    lines.extend(["", "DEEPDIVE_TOOL_TRACE:"])
    lines.extend(_tool_trace_lines(tool_trace))

    lines.extend(["", "DEEPDIVE_TRACE:"])
    lines.extend(_trace_lines(trace))

    lines.extend(
        [
            "",
            f"NEXT_STEP: Jetzt rag.suchen({crawl_id} {query}) ausfuehren und daraus ein Lagebild bauen. Diese Crawl-ID begrenzt die Synthese auf frisch geholte Quellen.",
            "WICHTIG: Nutze die RAG-Treffer als Arbeitsbasis, aber bewerte sie: Quelle, Alter, Stand, Relevanz, Widersprueche. Alte Rollen aktiv aufloesen; neue Quellen schlagen alte Rollen.",
        ]
    )

    return {"success": bool(fetched), "data": "\n".join(lines)}


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


def _select_sources(candidates, max_sources, query=""):
    selected = []
    host_counts = {}
    preferred = []
    terms = _query_terms(query)
    broad_query = _is_broad_query(query)
    for result in candidates:
        host = urllib.parse.urlparse(result["url"]).netloc.lower().removeprefix("www.")
        score = 0
        text = f"{result.get('title','')} {result.get('snippet','')} {result.get('url','')}".lower()
        term_hits = sum(1 for term in terms if term in text)
        if terms and term_hits == 0 and not broad_query and result.get("discovery_method") != "rss":
            continue
        score += term_hits * 8
        if terms and term_hits >= min(2, len(terms)):
            score += 4
        if result.get("discovery_method") == "rss":
            score += 6
        if any(word in text for word in ("aktuell", "news", "nachrichten", "heute", "live")):
            score += 5
        if any(domain in host for domain in ("tagesschau", "bundestag", "bundesregierung", "faz", "zdf", "spiegel", "zeit", "dw.com", "reuters", "apnews", "britannica", "wikipedia")):
            score += 3
        if _extract_dates(text):
            score += 2
        preferred.append((score, result))
    preferred.sort(key=lambda item: item[0], reverse=True)
    for _, result in preferred:
        host = urllib.parse.urlparse(result["url"]).netloc.lower().removeprefix("www.")
        if host_counts.get(host, 0) >= 2:
            continue
        host_counts[host] = host_counts.get(host, 0) + 1
        selected.append(result)
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
    return [q for q in _unique(variants) if q.lower() != _collapse_ws(query).lower()][:3]


def _relevance_score(query, result, page):
    text = f"{result.get('title','')} {result.get('snippet','')} {page.get('title','')} {page.get('text','')[:4000]}".lower()
    terms = _query_terms(query)
    term_hits = sum(1 for term in terms if term in text)
    if terms and term_hits == 0 and not _is_broad_query(query):
        return 0
    score = term_hits * 3
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
        if len(sentence) < 80 or len(sentence) > 900:
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
    bad_parts = (
        "#", "mailto:", "javascript:", "/login", "/signin", "/abo", "/newsletter",
        "/signup", "/register", "session_redirect=", "/datenschutz", "/privacy",
        "/impressum", "/kontakt", "/shop", "/account",
        ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".zip", ".mp4",
        "facebook.com", "instagram.com", "linkedin.com/share", "whatsapp", "mailto",
    )
    return any(part in low for part in bad_parts)


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
    key_passages = result.get("_key_passages") or []
    causality_hints = result.get("_causality_hints") or []
    text = _prepared_source_text(query, page["text"], key_passages)
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
        f"parent_url: {result.get('parent_url') or ''}",
        f"search_query: {result.get('search_query', '')}",
        f"source_url: {result.get('url', '')}",
        f"source_title: {page['title'] or result.get('title') or ''}",
        f"source_type: {_source_type(result.get('url', ''), meta)}",
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
    if causality_hints:
        lines.append("causality_hints:")
        for hint in causality_hints[:6]:
            lines.append("- " + hint)
    lines.extend(
        [
            "assessment_required: relevance, reliability, date_context, uncertainty, contradictions",
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
        f"sources_fetched: {len(fetched)}",
        f"failed_count: {len(failed)}",
        f"search_error_count: {len(search_errors)}",
        "sources:",
    ]
    for idx, item in enumerate(fetched, 1):
        dates = "; ".join(item.get("dates") or [])
        lines.append(
            f"{idx}. role={item.get('page_role', 'source')} depth={item.get('depth')} method={item.get('discovery_method')} score={item.get('relevance_score')} recency={item.get('recency_label')} title={item.get('title')} url={item.get('url')} parent={item.get('parent_url')} dates={dates}"
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
            "assessment_required: relevance, reliability, date_context, uncertainty",
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
            "discovery_method": _line_value(note, "discovery_method"),
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
