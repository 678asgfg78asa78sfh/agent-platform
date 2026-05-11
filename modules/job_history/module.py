"""Central job history module."""

import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import job_history_common as hist  # noqa: E402


MODULE = {
    "name": "job_history",
    "description": "Zentrale Pull-/Job-Historie: welche Module wann mit welchem Query welche Quellen geholt haben.",
    "version": "1.0",
    "settings": {
        "limit": {"type": "number", "label": "Default Limit", "default": 25},
        "max_output_chars": {"type": "number", "label": "Max Ausgabezeichen", "default": 24000},
    },
    "tools": [
        {
            "name": "job_history.list",
            "description": "Listet Jobs. JSON {module?, tool?, status?, query?, since_hours?, limit?}.",
            "params": ["filter_json"],
        },
        {
            "name": "job_history.detail",
            "description": "Zeigt Details zu einem job_id inkl. Quellen.",
            "params": ["job_id"],
        },
        {
            "name": "job_history.sources",
            "description": "Listet Quellen aus der Job-Historie. JSON {query?, module?, source_url?, since_hours?, limit?}.",
            "params": ["filter_json"],
        },
        {
            "name": "job_history.stats",
            "description": "Aggregierte Job-Stats nach Modul/Status. JSON {since_hours?}.",
            "params": ["filter_json"],
        },
        {
            "name": "job_history.ingest_legacy",
            "description": "Importiert vorhandene RSS-Items und DeepDive-Crawl-Manifeste in die zentrale Job-Historie.",
            "params": ["filter_json"],
        },
    ],
}


def handle_tool(tool_name, params, config):
    try:
        if tool_name == "job_history.list":
            return ok(limit_output(_list(parse_payload(params), config), config))
        if tool_name == "job_history.detail":
            job_id = params[0] if params else ""
            return ok(limit_output(_detail(str(job_id).strip(), config), config))
        if tool_name == "job_history.sources":
            return ok(limit_output(_sources(parse_payload(params), config), config))
        if tool_name == "job_history.stats":
            return ok(limit_output(_stats(parse_payload(params), config), config))
        if tool_name == "job_history.ingest_legacy":
            return ok(limit_output(_ingest_legacy(parse_payload(params), config), config))
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"Job-History Fehler: {exc}")


def _list(payload, config):
    limit = cfg_int(payload.get("limit", config.get("limit", 25)), 25, 1, 200)
    wheres, args = job_filters(payload)
    sql = (
        "SELECT job_id,module,tool,query,status,started_at_utc,finished_at_utc,duration_ms,"
        "source_count,summary,error FROM jobs"
    )
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY started_at_utc DESC LIMIT ?"
    args.append(limit)
    conn = hist.connect(config)
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    lines = ["JOB_HISTORY_LIST", f"count: {len(rows)}"]
    if not rows:
        lines.append("Keine Jobs gefunden.")
        return "\n".join(lines)
    for idx, row in enumerate(rows, 1):
        lines.append(
            f"{idx}. {row['started_at_utc']} status={row['status']} module={row['module']} "
            f"tool={row['tool']} sources={row['source_count']} duration_ms={row['duration_ms'] or ''}"
        )
        lines.append(f"   job_id: {row['job_id']}")
        if row["query"]:
            lines.append(f"   query: {row['query']}")
        if row["summary"]:
            lines.append(f"   summary: {truncate(row['summary'], 240)}")
        if row["error"]:
            lines.append(f"   error: {truncate(row['error'], 240)}")
    return "\n".join(lines)


def _detail(job_id, config):
    if not job_id:
        return "job_id fehlt."
    conn = hist.connect(config)
    try:
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job:
            return f"Job nicht gefunden: {job_id}"
        sources = conn.execute(
            "SELECT * FROM job_sources WHERE job_id=? ORDER BY id ASC LIMIT 300", (job_id,)
        ).fetchall()
    finally:
        conn.close()
    lines = [
        "JOB_HISTORY_DETAIL",
        f"job_id: {job['job_id']}",
        f"module: {job['module']}",
        f"tool: {job['tool']}",
        f"status: {job['status']}",
        f"started_at_utc: {job['started_at_utc']}",
        f"finished_at_utc: {job['finished_at_utc'] or ''}",
        f"duration_ms: {job['duration_ms'] or ''}",
        f"query: {job['query'] or ''}",
        f"source_count: {job['source_count']}",
    ]
    if job["summary"]:
        lines.append(f"summary: {job['summary']}")
    if job["error"]:
        lines.append(f"error: {job['error']}")
    lines.append("params_json: " + truncate(job["params_json"], 1800))
    lines.append("metrics_json: " + truncate(job["metrics_json"], 1800))
    lines.append("")
    lines.append("SOURCES")
    if not sources:
        lines.append("Keine Quellen gespeichert.")
        return "\n".join(lines)
    for idx, src in enumerate(sources, 1):
        lines.append(
            f"{idx}. type={src['source_type'] or ''} score={fmt(src['score'])} "
            f"published={src['published_at_utc'] or ''} rag_id={src['rag_id'] or ''}"
        )
        if src["source_title"]:
            lines.append(f"   title: {src['source_title']}")
        if src["source_name"]:
            lines.append(f"   source: {src['source_name']}")
        if src["source_url"]:
            lines.append(f"   url: {src['source_url']}")
    return "\n".join(lines)


def _sources(payload, config):
    limit = cfg_int(payload.get("limit", config.get("limit", 25)), 25, 1, 300)
    wheres, args = source_filters(payload)
    sql = (
        "SELECT s.*, j.module, j.tool, j.query, j.started_at_utc, j.status "
        "FROM job_sources s JOIN jobs j ON j.job_id=s.job_id"
    )
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY j.started_at_utc DESC, s.id ASC LIMIT ?"
    args.append(limit)
    conn = hist.connect(config)
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    lines = ["JOB_HISTORY_SOURCES", f"count: {len(rows)}"]
    for idx, row in enumerate(rows, 1):
        lines.append(
            f"{idx}. {row['started_at_utc']} module={row['module']} tool={row['tool']} "
            f"type={row['source_type'] or ''} score={fmt(row['score'])}"
        )
        lines.append(f"   job_id: {row['job_id']}")
        if row["query"]:
            lines.append(f"   query: {row['query']}")
        if row["source_title"]:
            lines.append(f"   title: {row['source_title']}")
        if row["source_url"]:
            lines.append(f"   url: {row['source_url']}")
        if row["rag_id"]:
            lines.append(f"   rag_id: {row['rag_id']}")
    if not rows:
        lines.append("Keine Quellen gefunden.")
    return "\n".join(lines)


def _stats(payload, config):
    wheres, args = job_filters(payload)
    where_sql = " WHERE " + " AND ".join(wheres) if wheres else ""
    conn = hist.connect(config)
    try:
        by_module = conn.execute(
            "SELECT module, status, COUNT(*) c, SUM(source_count) sources "
            "FROM jobs" + where_sql + " GROUP BY module,status ORDER BY c DESC",
            args,
        ).fetchall()
        totals = conn.execute(
            "SELECT COUNT(*) jobs, SUM(source_count) sources FROM jobs" + where_sql,
            args,
        ).fetchone()
    finally:
        conn.close()
    lines = [
        "JOB_HISTORY_STATS",
        f"jobs: {totals['jobs'] or 0}",
        f"sources: {totals['sources'] or 0}",
        "",
        "BY_MODULE",
    ]
    for row in by_module:
        lines.append(f"- {row['module']} status={row['status']} jobs={row['c']} sources={row['sources'] or 0}")
    return "\n".join(lines)


def _ingest_legacy(payload, config):
    rss_limit = cfg_int(payload.get("rss_limit", 500), 500, 0, 5000)
    dd_limit = cfg_int(payload.get("deepdive_limit", 500), 500, 0, 5000)
    rss_jobs, rss_sources = ingest_rss(config, rss_limit)
    dd_jobs, dd_sources = ingest_deepdive(config, dd_limit)
    return "\n".join(
        [
            "JOB_HISTORY_INGEST_LEGACY",
            f"rss_jobs: {rss_jobs}",
            f"rss_sources: {rss_sources}",
            f"deepdive_jobs: {dd_jobs}",
            f"deepdive_sources: {dd_sources}",
        ]
    )


def ingest_rss(config, limit):
    if limit <= 0:
        return 0, 0
    path = os.path.join(hist.data_dir(config), "rss", "rss_store.sqlite3")
    if not os.path.exists(path):
        return 0, 0
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT i.*, s.name source_name, s.url source_feed_url, s.category, s.reliability, s.alignment
              FROM items i JOIN sources s ON s.id=i.source_id
             ORDER BY COALESCE(NULLIF(i.fetched_at_utc,''), i.published_at_utc) DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    grouped = {}
    for row in rows:
        day = (row["fetched_at_utc"] or row["published_at_utc"] or "unknown")[:10]
        grouped.setdefault(day, []).append(row)
    jobs = 0
    sources = 0
    for day, items in grouped.items():
        result_ref = f"rss:{day}"
        if legacy_exists(config, "rss_verwaltung", "legacy.rss_items", result_ref):
            continue
        job_sources = [
            hist.source(
                source_type="rss_item",
                source_url=row["url"] or "",
                source_title=row["title"] or "",
                source_id=row["id"],
                source_name=row["source_name"] or "",
                published_at_utc=row["published_at_utc"] or "",
                captured_at_utc=row["fetched_at_utc"] or "",
                rag_id=row["rag_id"] or "",
                metadata={
                    "source_id": row["source_id"],
                    "source_feed_url": row["source_feed_url"],
                    "category": row["category"],
                    "reliability": row["reliability"],
                    "alignment": row["alignment"],
                },
            )
            for row in items
        ]
        hist.record_job(
            "rss_verwaltung",
            "legacy.rss_items",
            f"rss legacy import {day}",
            {"day": day, "limit": limit},
            "success",
            config,
            job_sources,
            summary=f"Legacy RSS items for {day}",
            metrics={"items": len(job_sources)},
            result_ref=result_ref,
        )
        jobs += 1
        sources += len(job_sources)
    return jobs, sources


def ingest_deepdive(config, limit):
    if limit <= 0:
        return 0, 0
    rag_dir = os.path.join(hist.data_dir(config), "rag", "DeepDive")
    paths = sorted(glob.glob(os.path.join(rag_dir, "*.json")), key=os.path.getmtime, reverse=True)
    grouped = {}
    seen_files = 0
    for path in paths:
        if seen_files >= limit:
            break
        try:
            data = json.load(open(path, "r", encoding="utf-8"))
        except Exception:
            continue
        crawl_id = data.get("crawl_id") or line_value(data.get("text", ""), "crawl_id")
        if not crawl_id:
            continue
        grouped.setdefault(crawl_id, []).append(data)
        seen_files += 1
    jobs = 0
    sources = 0
    for crawl_id, entries in grouped.items():
        if legacy_exists(config, "deepdive", "legacy.crawl", crawl_id):
            continue
        manifest = next((x for x in entries if (x.get("source_title") == "Crawl Manifest" or "DEEPDIVE_CRAWL_MANIFEST" in x.get("text", ""))), None)
        text = (manifest or entries[0]).get("text", "")
        topic = line_value(text, "topic")
        started = line_value(text, "crawl_started_at_utc")
        job_sources = []
        for entry in entries:
            if entry.get("source_title") == "Crawl Manifest":
                continue
            job_sources.append(
                hist.source(
                    source_type="deepdive_source",
                    source_url=entry.get("source_url", ""),
                    source_title=entry.get("source_title", ""),
                    source_id=entry.get("id", ""),
                    captured_at_utc=entry.get("captured_at_utc", ""),
                    rag_id=entry.get("id", ""),
                    score=entry.get("relevance_score"),
                    metadata={
                        "crawl_id": crawl_id,
                        "source_depth": entry.get("source_depth", ""),
                        "page_role": entry.get("page_role", ""),
                        "discovery_method": entry.get("discovery_method", ""),
                        "parent_url": entry.get("parent_url", ""),
                        "recency_label": entry.get("recency_label", ""),
                    },
                )
            )
        hist.record_job(
            "deepdive",
            "legacy.crawl",
            topic or crawl_id,
            {"crawl_id": crawl_id, "crawl_started_at_utc": started},
            "success",
            config,
            job_sources,
            rag_ids=[x.get("id", "") for x in entries if x.get("id")],
            summary=f"Legacy DeepDive crawl {crawl_id}",
            metrics={"entries": len(entries), "sources": len(job_sources)},
            result_ref=crawl_id,
        )
        jobs += 1
        sources += len(job_sources)
    return jobs, sources


def legacy_exists(config, module, tool, result_ref):
    conn = hist.connect(config)
    try:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE module=? AND tool=? AND result_ref=? LIMIT 1",
            (module, tool, result_ref),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def job_filters(payload):
    wheres, args = [], []
    if payload.get("module"):
        wheres.append("module LIKE ?")
        args.append(f"%{payload['module']}%")
    if payload.get("tool"):
        wheres.append("tool LIKE ?")
        args.append(f"%{payload['tool']}%")
    if payload.get("status"):
        wheres.append("status=?")
        args.append(str(payload["status"]))
    if payload.get("query"):
        wheres.append("query LIKE ?")
        args.append(f"%{payload['query']}%")
    since = since_iso(payload)
    if since:
        wheres.append("started_at_utc >= ?")
        args.append(since)
    return wheres, args


def source_filters(payload):
    wheres, args = [], []
    if payload.get("module"):
        wheres.append("j.module LIKE ?")
        args.append(f"%{payload['module']}%")
    if payload.get("query"):
        wheres.append("(j.query LIKE ? OR s.source_title LIKE ? OR s.source_url LIKE ?)")
        like = f"%{payload['query']}%"
        args.extend([like, like, like])
    if payload.get("source_url"):
        wheres.append("s.source_url LIKE ?")
        args.append(f"%{payload['source_url']}%")
    since = since_iso(payload)
    if since:
        wheres.append("j.started_at_utc >= ?")
        args.append(since)
    return wheres, args


def since_iso(payload):
    if not payload.get("since_hours"):
        return ""
    hours = cfg_int(payload.get("since_hours"), 0, 0, 24 * 365 * 10)
    if not hours:
        return ""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def line_value(text, key):
    match = re.search(rf"^{re.escape(key)}:\s*(.*)$", text or "", flags=re.M)
    return match.group(1).strip() if match else ""


def parse_payload(params, default_key="query"):
    if isinstance(params, dict):
        return dict(params)
    if not params:
        return {}
    raw = params[0]
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {default_key: text} if text else {}


def cfg_int(value, default, min_value=None, max_value=None):
    try:
        num = int(float(str(value)))
    except Exception:
        num = default
    if min_value is not None:
        num = max(min_value, num)
    if max_value is not None:
        num = min(max_value, num)
    return num


def truncate(text, limit):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def fmt(value):
    return "n/a" if value in (None, "") else str(value)


def limit_output(text, config):
    limit = cfg_int(config.get("max_output_chars", 24000), 24000, 2000, 80000)
    return truncate(text, limit)


def ok(data):
    return {"success": True, "data": data}


def fail(data):
    return {"success": False, "data": data}


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
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
