"""Shared central job history helpers for Python modules."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def data_dir(config: dict | None = None) -> str:
    config = config or {}
    configured = str(config.get("data_dir") or "").strip()
    if configured:
        return configured
    here = os.path.abspath(os.path.dirname(__file__))
    root = os.path.abspath(os.path.join(here, ".."))
    fallback = os.path.join(root, "agent-data")
    return fallback if os.path.isdir(fallback) else os.path.join(os.getcwd(), "agent-data")


def db_path(config: dict | None = None) -> str:
    path = str((config or {}).get("job_history_db_path") or "").strip()
    if path:
        return path
    return os.path.join(data_dir(config), "job_history.sqlite3")


def connect(config: dict | None = None) -> sqlite3.Connection:
    path = db_path(config)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            module TEXT NOT NULL,
            tool TEXT NOT NULL,
            query TEXT,
            params_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            duration_ms INTEGER,
            source_count INTEGER NOT NULL DEFAULT 0,
            sources_json TEXT NOT NULL DEFAULT '[]',
            rag_ids_json TEXT NOT NULL DEFAULT '[]',
            result_ref TEXT,
            summary TEXT,
            error TEXT,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            task_id TEXT,
            created_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started_at_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_module_started ON jobs(module, started_at_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_tool_started ON jobs(tool, started_at_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_status_started ON jobs(status, started_at_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_query ON jobs(query);

        CREATE TABLE IF NOT EXISTS job_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            source_type TEXT,
            source_id TEXT,
            source_url TEXT,
            source_title TEXT,
            source_name TEXT,
            published_at_utc TEXT,
            captured_at_utc TEXT,
            rag_id TEXT,
            score REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_job_sources_job ON job_sources(job_id);
        CREATE INDEX IF NOT EXISTS idx_job_sources_url ON job_sources(source_url);
        CREATE INDEX IF NOT EXISTS idx_job_sources_rag ON job_sources(rag_id);
        """
    )
    conn.commit()


def start_job(
    module: str,
    tool: str,
    query: str = "",
    params: Any = None,
    config: dict | None = None,
    task_id: str = "",
) -> str:
    job_id = new_job_id(module, tool, query)
    started = now_iso()
    conn = connect(config)
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, module, tool, query, params_json, status,
                started_at_utc, created_at_utc, task_id
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (job_id, module, tool, query, safe_json(params or {}), started, started, task_id),
        )
        conn.commit()
    finally:
        conn.close()
    return job_id


def finish_job(
    job_id: str,
    status: str,
    config: dict | None = None,
    sources: list[dict[str, Any]] | None = None,
    rag_ids: list[str] | None = None,
    summary: str = "",
    error: str = "",
    metrics: dict[str, Any] | None = None,
    result_ref: str = "",
) -> None:
    finished = now_iso()
    sources = normalize_sources(sources or [])
    rag_ids = unique([*(rag_ids or []), *(s.get("rag_id", "") for s in sources)])
    conn = connect(config)
    try:
        row = conn.execute("SELECT started_at_utc FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        duration_ms = None
        if row and row["started_at_utc"]:
            duration_ms = iso_duration_ms(row["started_at_utc"], finished)
        conn.execute("DELETE FROM job_sources WHERE job_id=?", (job_id,))
        for src in sources:
            conn.execute(
                """
                INSERT INTO job_sources (
                    job_id, source_type, source_id, source_url, source_title,
                    source_name, published_at_utc, captured_at_utc, rag_id, score, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    src.get("source_type", ""),
                    src.get("source_id", ""),
                    src.get("source_url", ""),
                    src.get("source_title", ""),
                    src.get("source_name", ""),
                    src.get("published_at_utc", ""),
                    src.get("captured_at_utc", finished),
                    src.get("rag_id", ""),
                    src.get("score"),
                    safe_json(src.get("metadata") or {}),
                ),
            )
        conn.execute(
            """
            UPDATE jobs
               SET status=?, finished_at_utc=?, duration_ms=?, source_count=?,
                   sources_json=?, rag_ids_json=?, result_ref=?, summary=?,
                   error=?, metrics_json=?
             WHERE job_id=?
            """,
            (
                status,
                finished,
                duration_ms,
                len(sources),
                safe_json(sources[:200]),
                safe_json(rag_ids),
                result_ref,
                summary,
                error,
                safe_json(metrics or {}),
                job_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_job(
    module: str,
    tool: str,
    query: str = "",
    params: Any = None,
    status: str = "success",
    config: dict | None = None,
    sources: list[dict[str, Any]] | None = None,
    rag_ids: list[str] | None = None,
    summary: str = "",
    error: str = "",
    metrics: dict[str, Any] | None = None,
    result_ref: str = "",
    task_id: str = "",
) -> str:
    job_id = start_job(module, tool, query, params, config, task_id)
    finish_job(job_id, status, config, sources, rag_ids, summary, error, metrics, result_ref)
    return job_id


def source(
    source_type: str = "",
    source_url: str = "",
    source_title: str = "",
    source_id: str = "",
    source_name: str = "",
    published_at_utc: str = "",
    captured_at_utc: str = "",
    rag_id: str = "",
    score: Any = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source_type": clean(source_type),
        "source_url": clean(source_url),
        "source_title": clean(source_title),
        "source_id": clean(source_id),
        "source_name": clean(source_name),
        "published_at_utc": clean(published_at_utc),
        "captured_at_utc": clean(captured_at_utc) or now_iso(),
        "rag_id": clean(rag_id),
        "score": safe_float(score),
        "metadata": metadata or {},
    }


def normalize_sources(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        src = source(
            source_type=item.get("source_type") or item.get("type") or "",
            source_url=item.get("source_url") or item.get("url") or "",
            source_title=item.get("source_title") or item.get("title") or "",
            source_id=item.get("source_id") or item.get("id") or item.get("item_id") or "",
            source_name=item.get("source_name") or item.get("name") or "",
            published_at_utc=item.get("published_at_utc") or item.get("published_at") or "",
            captured_at_utc=item.get("captured_at_utc") or item.get("captured_at") or "",
            rag_id=item.get("rag_id") or "",
            score=item.get("score"),
            metadata=item.get("metadata") or {},
        )
        key = (src["source_url"], src["source_id"], src["source_title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(src)
    return out


def safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def unique(items: list[Any]) -> list[str]:
    out = []
    seen = set()
    for item in items:
        value = clean(item)
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def new_job_id(module: str, tool: str, query: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha1(f"{module}|{tool}|{query}|{time.time()}|{uuid4()}".encode()).hexdigest()[:8]
    prefix = "".join(ch for ch in module.split(".")[0] if ch.isalnum() or ch == "_")[:18] or "job"
    return f"job-{prefix}-{stamp}-{digest}"


def iso_duration_ms(started: str, finished: str) -> int | None:
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        f = datetime.fromisoformat(finished.replace("Z", "+00:00"))
        return int((f - s).total_seconds() * 1000)
    except Exception:
        return None
