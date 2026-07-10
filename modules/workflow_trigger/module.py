"""Workflow trigger and dependency gate for multi-step agent jobs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODULE = {
    "name": "workflow_trigger",
    "description": "Dependency-/Trigger-Modul: wartet auf Tasks/DeepDive-Reports und startet danach Normalisierung, Video-Render und Shorts.",
    "version": "1.0",
    "settings": {
        "enabled": {"type": "bool", "label": "Aktiv", "default": True},
        "python_timeout_s": {"type": "number", "label": "Python Timeout Sekunden", "default": 60},
        "default_target_modul_id": {"type": "string", "label": "Ziel-Chatmodul", "default": "chat.deepseekdeepseekv4flash"},
        "default_normalizer_modul_id": {"type": "string", "label": "Video-Normalizer Worker", "default": "llm_worker.video_normalizer"},
        "default_reviewer_modul_id": {"type": "string", "label": "Video-Quality Reviewer", "default": "llm_worker.video_normalizer"},
        "default_factcheck_modul_id": {"type": "string", "label": "Video-Faktencheck Modul", "default": "factcheck.default"},
        "default_tts_modul_id": {"type": "string", "label": "TTS-Modul-Instanz", "default": "tts.default"},
        "default_video_modul_id": {"type": "string", "label": "Video-Modul-Instanz", "default": "video_pipeline.default"},
        "default_output_dir": {"type": "string", "label": "Workflow Output", "default": "agent-data/workflows"},
        "default_render_output_dir": {"type": "string", "label": "Video Output", "default": "agent-data/video_pipeline"},
        "default_synthesis_dir": {"type": "string", "label": "Synthese Output", "default": ""},
        "default_task_timeout_s": {"type": "number", "label": "LLM Task Timeout", "default": 3600},
        "default_render_timeout_s": {"type": "number", "label": "Render Task Timeout", "default": 3600},
        "tick_limit": {"type": "number", "label": "Workflows pro Tick", "default": 12},
        "daily_video_guarantee": {"type": "bool", "label": "Tagesvideo-Garantie: bei Fehlschlag naechstes Thema produzieren", "default": True},
        "daily_guarantee_max_attempts": {"type": "number", "label": "Max Themen-Versuche pro Tag", "default": 3},
        "recover_failed_workflows": {"type": "bool", "label": "Reparierbare fehlgeschlagene Workflows automatisch fortsetzen", "default": True},
        "recover_failed_max_attempts": {"type": "number", "label": "Max Self-Recovery Versuche pro Workflow", "default": 2},
        "recover_failed_max_age_hours": {"type": "number", "label": "Max Alter fuer automatische Failed-Recovery", "default": 12},
        "fallback_target_modul_ids": {"type": "string", "label": "Fallback-Chatmodule bei API-/Quota-Ausfall", "default": ""},
        "fallback_normalizer_modul_ids": {"type": "string", "label": "Fallback-Normalizer bei API-/Quota-Ausfall", "default": ""},
        "fallback_reviewer_modul_ids": {"type": "string", "label": "Fallback-Reviewer/Reparatur bei API-/Quota-Ausfall", "default": ""},
        "production_health_enabled": {"type": "bool", "label": "Produktions-Health-Snapshot im Tick schreiben", "default": True},
        "production_health_window_hours": {"type": "number", "label": "Health-Zeitfenster fuer aktuelle Produktion", "default": 36},
        "production_health_require_upload": {"type": "bool", "label": "Health erwartet YouTube-URL statt nur Render", "default": True},
        "production_health_strict_latest": {"type": "bool", "label": "Neuester Workflow darf nicht von altem Upload verdeckt werden", "default": True},
        "auto_render": {"type": "bool", "label": "Nach Normalisierung rendern", "default": True},
        "auto_upload": {"type": "bool", "label": "Nach Render automatisch auf YouTube hochladen", "default": False},
        "default_upload_privacy": {"type": "select", "label": "YouTube Privacy", "default": "unlisted", "options": ["private", "unlisted", "public"]},
        "video_style": {"type": "select", "label": "Video-Renderer", "default": "infographic", "options": ["infographic", "mapled"]},
        "scene_images": {"type": "bool", "label": "KI-Szenenbilder generieren", "default": True},
        "default_image_modul_id": {"type": "string", "label": "Image-Gen Modul", "default": "image_gen.default"},
        "auto_shorts": {"type": "bool", "label": "Nach Render Shorts schneiden", "default": False},
        "fact_check": {"type": "bool", "label": "Vor TTS Faktencheck ausfuehren", "default": True},
        "fact_check_min_score": {"type": "number", "label": "Faktencheck Mindestscore", "default": 72},
        "fact_check_max_claims": {"type": "number", "label": "Faktencheck Max Claims", "default": 14},
        "factcheck_max_repairs": {"type": "number", "label": "Max Faktencheck-Reparaturen", "default": 3},
        "quality_gate": {"type": "bool", "label": "Vor TTS/Render Quality Gate erzwingen", "default": True},
        "quality_auto_repair": {"type": "bool", "label": "Bei Review-Fehlern einmal reparieren", "default": True},
        "quality_min_score": {"type": "number", "label": "Mindestscore fuer Video-Freigabe", "default": 78},
        "quality_max_repairs": {"type": "number", "label": "Legacy: Max Reparaturen pro Gate", "default": 2},
        "quality_review_max_repairs": {"type": "number", "label": "Max Quality-Review-Reparaturen", "default": 2},
        "quality_proceed_after_repairs": {"type": "bool", "label": "Nach erschoepften Reparaturen best effort rendern", "default": True},
        "quality_proceed_min_score": {"type": "number", "label": "Best-effort Mindestscore", "default": 70},
        "quality_best_effort_upload": {"type": "bool", "label": "Best-effort Videos trotzdem hochladen", "default": False},
        "require_tts": {"type": "bool", "label": "Vor Produktion TTS erzwingen", "default": True},
        "allow_silent_audio": {"type": "bool", "label": "Stummes Audio nur explizit erlauben", "default": False},
        "default_tts_provider": {"type": "select", "label": "TTS Provider", "default": "minimax", "options": ["minimax", "piper", "xai", "qwen"]},
        "default_tts_voice": {"type": "string", "label": "TTS Stimme (leer = Provider-Default)", "default": ""},
        "default_tts_language": {"type": "string", "label": "TTS Sprache", "default": "de"},
        "default_tts_fast": {"type": "bool", "label": "TTS schnell", "default": True},
        "default_shorts_count": {"type": "number", "label": "Shorts Anzahl", "default": 30},
        "default_shorts_duration_s": {"type": "number", "label": "Short Dauer", "default": 45},
        "preview": {"type": "bool", "label": "Preview Render", "default": False},
    },
    "tools": [
        {
            "name": "workflow_trigger.deepdive_video",
            "description": "Startet einen abhängigen Workflow: DeepDive-Report -> Video-Normalisierung -> TTS -> Render -> optional Upload/Shorts. JSON {query,title,target_modul_id,normalizer_modul_id,tts_modul_id,chat_route,audio_path,preview,auto_render,auto_upload,upload_privacy,auto_shorts,shorts_count,target_minutes}.",
            "params": ["query_json"],
        },
        {
            "name": "workflow_trigger.video_from_report",
            "description": "Startet die Video-Pipeline ab vorhandenem DeepDive. JSON {report_task_id|crawl_id|report_path|report_text,query,title,normalizer_modul_id,tts_modul_id,audio_path,preview,auto_render,auto_upload,upload_privacy,auto_shorts,shorts_count,target_minutes}.",
            "params": ["query_json"],
        },
        {
            "name": "workflow_trigger.repair_video",
            "description": "Repariert einen vorhandenen Video-Workflow ab fertigem script.txt/scenes.json: TTS neu erzeugen -> Video neu rendern -> optional Upload/Shorts. JSON {workflow_id|workflow_dir,auto_upload?,upload_privacy?,auto_shorts?,shorts_count?,render_out_dir?}.",
            "params": ["query_json"],
        },
        {
            "name": "workflow_trigger.tick",
            "description": "Prueft offene Workflow-Dependencies und startet faellige Folgeaufgaben. Sollte per Cron laufen.",
            "params": ["query_json"],
        },
        {
            "name": "workflow_trigger.status",
            "description": "Zeigt Workflow-Zustand, Dependencies, Task-IDs und naechste faellige Schritte. JSON {workflow_id?, limit?}.",
            "params": ["query_json"],
        },
        {
            "name": "workflow_trigger.synthesis_get",
            "description": "Liest ein gespeichertes DeepDive-/Video-Syntheseobjekt. JSON {synthesis_id?|workflow_id?|crawl_id?}. Kann alte Workflows backfillen.",
            "params": ["query_json"],
        },
        {
            "name": "workflow_trigger.synthesis_list",
            "description": "Listet gespeicherte Syntheseobjekte. JSON {limit?, crawl_id?, query?}.",
            "params": ["query_json"],
        },
        {
            "name": "workflow_trigger.cancel",
            "description": "Markiert einen Workflow als cancelled. JSON {workflow_id, reason?}.",
            "params": ["query_json"],
        },
        {
            "name": "workflow_trigger.production_health",
            "description": "Prueft den Autopilot-Produktionszustand: Cron/Workflows/Upload im aktuellen Zeitfenster. JSON {window_hours?, require_upload?, write?}.",
            "params": ["query_json"],
        },
        {
            "name": "workflow_trigger.help",
            "description": "Zeigt das Dependency-Konzept und Beispielaufrufe.",
            "params": [],
        },
    ],
}


def handle_tool(tool_name: str, params: Any, config: dict[str, Any]) -> dict[str, Any]:
    try:
        if not cfg_bool(config, "enabled", True):
            return fail("workflow_trigger ist deaktiviert.")
        if tool_name == "workflow_trigger.deepdive_video":
            return deepdive_video(params, config)
        if tool_name == "workflow_trigger.video_from_report":
            return video_from_report(params, config)
        if tool_name == "workflow_trigger.repair_video":
            return repair_video(params, config)
        if tool_name == "workflow_trigger.tick":
            return tick(params, config)
        if tool_name == "workflow_trigger.status":
            return status(params, config)
        if tool_name == "workflow_trigger.synthesis_get":
            return synthesis_get(params, config)
        if tool_name == "workflow_trigger.synthesis_list":
            return synthesis_list(params, config)
        if tool_name == "workflow_trigger.cancel":
            return cancel(params, config)
        if tool_name == "workflow_trigger.production_health":
            return production_health(params, config)
        if tool_name == "workflow_trigger.help":
            return ok(help_text())
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"WORKFLOW_TRIGGER_FAILED: {exc}")


def deepdive_video(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    query = first_text(payload, "query", "topic", "thema", "q")
    if not query:
        return fail('query fehlt. Beispiel: workflow_trigger.deepdive_video({"query":"UFO UAP Disclosure","title":"UAP Lagebild"})')

    title = first_text(payload, "title", "video_title") or query[:90]
    target = first_text(payload, "target_modul_id", "target", "chat_modul_id") or str(config.get("default_target_modul_id") or "chat.deepseekdeepseekv4flash")
    normalizer = first_text(payload, "normalizer_modul_id", "normalizer", "video_normalizer_modul_id") or str(config.get("default_normalizer_modul_id") or target)
    reviewer = first_text(payload, "reviewer_modul_id", "reviewer", "quality_reviewer_modul_id") or str(config.get("default_reviewer_modul_id") or normalizer)
    factchecker = first_text(payload, "factcheck_modul_id", "fact_checker_modul_id", "factchecker_modul_id", "factchecker") or str(config.get("default_factcheck_modul_id") or "factcheck.default")
    tts_modul = first_text(payload, "tts_modul_id", "tts", "tts_target_modul_id") or str(config.get("default_tts_modul_id") or "tts.default")
    video_modul = first_text(payload, "video_modul_id") or str(config.get("default_video_modul_id") or "video_pipeline.default")
    workflow_id = "wf-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    parent_task_id = inherited_parent_task_id(payload, config)
    workflow_dir = workflows_dir(config) / workflow_id
    workflow_dir.mkdir(parents=True, exist_ok=True)

    wf = {
        "id": workflow_id,
        "kind": "deepdive_video",
        "status": "running",
        "stage": "deepdive_report",
        "query": query,
        "title": title,
        "target_modul_id": target,
        "normalizer_modul_id": normalizer,
        "reviewer_modul_id": reviewer,
        "factcheck_modul_id": factchecker,
        "tts_modul_id": tts_modul,
        "video_modul_id": video_modul,
        "parent_task_id": parent_task_id,
        "source_task_id": first_text(config, "task_id"),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "workflow_dir": str(workflow_dir),
        "options": {
            "chat_route": first_text(payload, "chat_route", "zurueck_an"),
            "audio_path": first_text(payload, "audio_path", "audio"),
            "preview": bool_param(payload.get("preview"), cfg_bool(config, "preview", False)),
            "auto_render": bool_param(payload.get("auto_render"), cfg_bool(config, "auto_render", True)),
            "auto_shorts": bool_param(payload.get("auto_shorts"), cfg_bool(config, "auto_shorts", False)),
            "auto_upload": bool_param(payload.get("auto_upload"), cfg_bool(config, "auto_upload", False)),
            "upload_privacy": first_text(payload, "upload_privacy", "privacy") or str(config.get("default_upload_privacy") or "unlisted"),
            "fact_check": bool_param(payload.get("fact_check"), cfg_bool(config, "fact_check", True)),
            "fact_check_min_score": int_param(payload.get("fact_check_min_score"), cfg_int(config, "fact_check_min_score", 72), 0, 100),
            "fact_check_max_claims": int_param(payload.get("fact_check_max_claims"), cfg_int(config, "fact_check_max_claims", 14), 1, 60),
            "factcheck_max_repairs": int_param(payload.get("factcheck_max_repairs"), cfg_int(config, "factcheck_max_repairs", cfg_int(config, "quality_max_repairs", 2)), 0, 5),
            "quality_gate": bool_param(payload.get("quality_gate"), cfg_bool(config, "quality_gate", True)),
            "quality_auto_repair": bool_param(payload.get("quality_auto_repair"), cfg_bool(config, "quality_auto_repair", True)),
            "quality_min_score": int_param(payload.get("quality_min_score"), cfg_int(config, "quality_min_score", 78), 0, 100),
            "quality_max_repairs": int_param(payload.get("quality_max_repairs"), cfg_int(config, "quality_max_repairs", 2), 0, 5),
            "quality_review_max_repairs": int_param(payload.get("quality_review_max_repairs") or payload.get("review_max_repairs"), cfg_int(config, "quality_review_max_repairs", cfg_int(config, "quality_max_repairs", 2)), 0, 5),
            "require_tts": bool_param(payload.get("require_tts"), cfg_bool(config, "require_tts", True)),
            "allow_silent_audio": bool_param(payload.get("allow_silent_audio"), cfg_bool(config, "allow_silent_audio", False)),
            "tts_provider": first_text(payload, "tts_provider", "provider") or str(config.get("default_tts_provider") or "xai"),
            "animate_scenes": None if payload.get("animate_scenes") is None else bool_param(payload.get("animate_scenes"), False),
            "tts_voice": first_text(payload, "tts_voice", "voice", "voice_id") or str(config.get("default_tts_voice") or ""),
            "tts_language": first_text(payload, "tts_language", "language", "lang", "sprache") or str(config.get("default_tts_language") or "de"),
            "tts_fast": bool_param(payload.get("tts_fast"), cfg_bool(config, "default_tts_fast", True)),
            "shorts_count": int_param(payload.get("shorts_count") or payload.get("count"), cfg_int(config, "default_shorts_count", 30), 1, 100),
            "shorts_duration_s": float_param(payload.get("shorts_duration_s") or payload.get("short_duration_s"), cfg_float(config, "default_shorts_duration_s", 45.0), 3.0, 180.0),
            "render_out_dir": first_text(payload, "render_out_dir", "video_out_dir") or str(default_render_output_dir(config) / workflow_id),
            "allow_extra_research": bool_param(payload.get("allow_extra_research"), cfg_bool(config, "allow_extra_research", False)),
            "target_fallback_modul_ids": list_param(payload.get("target_fallback_modul_ids") or payload.get("target_fallbacks")),
            "normalizer_fallback_modul_ids": list_param(payload.get("normalizer_fallback_modul_ids") or payload.get("normalizer_fallbacks")),
            "reviewer_fallback_modul_ids": list_param(payload.get("reviewer_fallback_modul_ids") or payload.get("reviewer_fallbacks")),
            "video_style": first_text(payload, "video_style", "renderer", "style") or str(config.get("video_style") or ""),
            "target_duration_s": float_param(payload.get("target_duration_s") or payload.get("min_duration_s") or (float_param(payload.get("target_minutes"), 0.0, 0.0, 60.0) * 60.0), 0.0, 0.0, 3600.0),
            "language": (first_text(payload, "language", "lang", "sprache") or "de").lower()[:5],
        },
        "tasks": {},
        "artifacts": {},
        "events": [],
    }

    task_id = enqueue_llm_task(
        config,
        target,
        deepdive_report_prompt(query, title),
        created_by="workflow_trigger",
        timeout_s=cfg_int(config, "default_task_timeout_s", 3600, 60, 14400),
        back_route=wf["options"].get("chat_route") or None,
        parent_id=parent_task_id,
        workflow_id=workflow_id,
        workflow_stage="deepdive_report",
    )
    wf["tasks"]["deepdive_report"] = task_id
    wf["events"].append(event("created", f"DeepDive-Report Task gestartet: {task_id}"))
    save_workflow(wf, config)
    return ok(
        {
            "workflow_id": workflow_id,
            "status": wf["status"],
            "stage": wf["stage"],
            "query": query,
            "title": title,
            "deepdive_report_task_id": task_id,
            "workflow_dir": str(workflow_dir),
            "next": "workflow_trigger.tick prueft, ob der Report fertig ist, und startet danach die Video-Normalisierung.",
        }
    )


def video_from_report(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    report_task_id = first_text(payload, "report_task_id", "task_id", "deepdive_task_id")
    crawl_id = first_text(payload, "crawl_id")
    report_path_raw = first_text(payload, "report_path", "path")
    report_text = first_text(payload, "report_text", "report")
    if not any([report_task_id, crawl_id, report_path_raw, report_text]):
        return fail("report_task_id, crawl_id, report_path oder report_text fehlt.")

    query = first_text(payload, "query", "topic", "thema", "q") or crawl_id or "DeepDive Report"
    title = first_text(payload, "title", "video_title") or query[:90]
    target = first_text(payload, "target_modul_id", "target", "chat_modul_id") or str(config.get("default_target_modul_id") or "chat.deepseekdeepseekv4flash")
    normalizer = first_text(payload, "normalizer_modul_id", "normalizer", "video_normalizer_modul_id") or str(config.get("default_normalizer_modul_id") or target)
    reviewer = first_text(payload, "reviewer_modul_id", "reviewer", "quality_reviewer_modul_id") or str(config.get("default_reviewer_modul_id") or normalizer)
    factchecker = first_text(payload, "factcheck_modul_id", "fact_checker_modul_id", "factchecker_modul_id", "factchecker") or str(config.get("default_factcheck_modul_id") or "factcheck.default")
    tts_modul = first_text(payload, "tts_modul_id", "tts", "tts_target_modul_id") or str(config.get("default_tts_modul_id") or "tts.default")
    video_modul = first_text(payload, "video_modul_id") or str(config.get("default_video_modul_id") or "video_pipeline.default")
    workflow_id = "wf-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    parent_task_id = inherited_parent_task_id(payload, config)
    workflow_dir = workflows_dir(config) / workflow_id
    workflow_dir.mkdir(parents=True, exist_ok=True)
    wf = {
        "id": workflow_id,
        "kind": "video_from_report",
        "status": "running",
        "stage": "normalize_report",
        "query": query,
        "title": title,
        "target_modul_id": target,
        "normalizer_modul_id": normalizer,
        "reviewer_modul_id": reviewer,
        "factcheck_modul_id": factchecker,
        "tts_modul_id": tts_modul,
        "video_modul_id": video_modul,
        "parent_task_id": parent_task_id,
        "source_task_id": first_text(config, "task_id"),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "workflow_dir": str(workflow_dir),
        "options": {
            "chat_route": first_text(payload, "chat_route", "zurueck_an"),
            "audio_path": first_text(payload, "audio_path", "audio"),
            "preview": bool_param(payload.get("preview"), cfg_bool(config, "preview", False)),
            "auto_render": bool_param(payload.get("auto_render"), cfg_bool(config, "auto_render", True)),
            "auto_shorts": bool_param(payload.get("auto_shorts"), cfg_bool(config, "auto_shorts", False)),
            "auto_upload": bool_param(payload.get("auto_upload"), cfg_bool(config, "auto_upload", False)),
            "upload_privacy": first_text(payload, "upload_privacy", "privacy") or str(config.get("default_upload_privacy") or "unlisted"),
            "fact_check": bool_param(payload.get("fact_check"), cfg_bool(config, "fact_check", True)),
            "fact_check_min_score": int_param(payload.get("fact_check_min_score"), cfg_int(config, "fact_check_min_score", 72), 0, 100),
            "fact_check_max_claims": int_param(payload.get("fact_check_max_claims"), cfg_int(config, "fact_check_max_claims", 14), 1, 60),
            "factcheck_max_repairs": int_param(payload.get("factcheck_max_repairs"), cfg_int(config, "factcheck_max_repairs", cfg_int(config, "quality_max_repairs", 2)), 0, 5),
            "quality_gate": bool_param(payload.get("quality_gate"), cfg_bool(config, "quality_gate", True)),
            "quality_auto_repair": bool_param(payload.get("quality_auto_repair"), cfg_bool(config, "quality_auto_repair", True)),
            "quality_min_score": int_param(payload.get("quality_min_score"), cfg_int(config, "quality_min_score", 78), 0, 100),
            "quality_max_repairs": int_param(payload.get("quality_max_repairs"), cfg_int(config, "quality_max_repairs", 2), 0, 5),
            "quality_review_max_repairs": int_param(payload.get("quality_review_max_repairs") or payload.get("review_max_repairs"), cfg_int(config, "quality_review_max_repairs", cfg_int(config, "quality_max_repairs", 2)), 0, 5),
            "require_tts": bool_param(payload.get("require_tts"), cfg_bool(config, "require_tts", True)),
            "allow_silent_audio": bool_param(payload.get("allow_silent_audio"), cfg_bool(config, "allow_silent_audio", False)),
            "tts_provider": first_text(payload, "tts_provider", "provider") or str(config.get("default_tts_provider") or "xai"),
            "animate_scenes": None if payload.get("animate_scenes") is None else bool_param(payload.get("animate_scenes"), False),
            "tts_voice": first_text(payload, "tts_voice", "voice", "voice_id") or str(config.get("default_tts_voice") or ""),
            "tts_language": first_text(payload, "tts_language", "language", "lang", "sprache") or str(config.get("default_tts_language") or "de"),
            "tts_fast": bool_param(payload.get("tts_fast"), cfg_bool(config, "default_tts_fast", True)),
            "shorts_count": int_param(payload.get("shorts_count") or payload.get("count"), cfg_int(config, "default_shorts_count", 30), 1, 100),
            "shorts_duration_s": float_param(payload.get("shorts_duration_s") or payload.get("short_duration_s"), cfg_float(config, "default_shorts_duration_s", 45.0), 3.0, 180.0),
            "render_out_dir": first_text(payload, "render_out_dir", "video_out_dir") or str(default_render_output_dir(config) / workflow_id),
            "allow_extra_research": bool_param(payload.get("allow_extra_research"), cfg_bool(config, "allow_extra_research", False)),
            "normalizer_fallback_modul_ids": list_param(payload.get("normalizer_fallback_modul_ids") or payload.get("normalizer_fallbacks")),
            "reviewer_fallback_modul_ids": list_param(payload.get("reviewer_fallback_modul_ids") or payload.get("reviewer_fallbacks")),
            "video_style": first_text(payload, "video_style", "renderer", "style") or str(config.get("video_style") or ""),
            "target_duration_s": float_param(payload.get("target_duration_s") or payload.get("min_duration_s") or (float_param(payload.get("target_minutes"), 0.0, 0.0, 60.0) * 60.0), 0.0, 0.0, 3600.0),
            "language": (first_text(payload, "language", "lang", "sprache") or "de").lower()[:5],
        },
        "tasks": {},
        "artifacts": {},
        "events": [],
    }

    if report_task_id:
        task = load_task(config, report_task_id)
        if not task:
            return fail(f"report_task_id nicht gefunden: {report_task_id}")
        wf["tasks"]["deepdive_report"] = report_task_id
        if task["status"] in {"erstellt", "gestartet"}:
            wf["stage"] = "deepdive_report"
            wf["status"] = "waiting"
            wf["events"].append(event("waiting", f"Warte auf bestehenden DeepDive-Task: {report_task_id}"))
            save_workflow(wf, config)
            return ok(
                {
                    "workflow_id": workflow_id,
                    "status": wf["status"],
                    "stage": wf["stage"],
                    "report_task_id": report_task_id,
                    "next": "workflow_trigger.tick startet die Normalisierung, sobald der Report-Task success ist.",
                }
            )
        if task["status"] != "success":
            return fail(f"report_task_id ist nicht erfolgreich: {report_task_id} status={task['status']}")
        report_text = task.get("result") or ""
    elif report_path_raw:
        report_path = resolve_path(config, report_path_raw)
        if not report_path.exists():
            return fail(f"report_path nicht gefunden: {report_path}")
        report_text = report_path.read_text(encoding="utf-8", errors="replace")
        wf["artifacts"]["source_report_path"] = str(report_path)
    elif crawl_id:
        report_text = (
            f"crawl_id: {crawl_id}\n"
            "Hinweis: Dieser Workflow wurde direkt aus einer crawl_id gestartet. "
            "Die Normalisierung soll deepdive.pack/deepdive.blocks fuer diese crawl_id nutzen."
        )

    report_file = workflow_dir / "deepdive_report.txt"
    write_text(report_file, report_text)
    wf["artifacts"]["deepdive_report"] = str(report_file)
    found_crawl_id = crawl_id or extract_crawl_id(report_text)
    if found_crawl_id:
        wf["artifacts"]["crawl_id"] = found_crawl_id

    normalize_task = enqueue_llm_task(
        config,
        normalizer,
        normalize_prompt(wf, report_text, prepare_deepdive_context(config, wf, found_crawl_id)),
        created_by="workflow_trigger",
        timeout_s=cfg_int(config, "default_task_timeout_s", 3600, 60, 14400),
        back_route=wf.get("options", {}).get("chat_route") or None,
        parent_id=parent_task_id,
        workflow_id=workflow_id,
        workflow_stage="normalize_report",
    )
    wf["tasks"]["normalize_report"] = normalize_task
    wf["events"].append(event("created", f"Normalisierung aus vorhandenem Report gestartet: {normalize_task}"))
    save_workflow(wf, config)
    return ok(
        {
            "workflow_id": workflow_id,
            "status": wf["status"],
            "stage": wf["stage"],
            "query": query,
            "title": title,
            "normalize_task_id": normalize_task,
            "crawl_id": found_crawl_id,
            "workflow_dir": str(workflow_dir),
            "next": "workflow_trigger.tick wartet auf die Normalisierung und startet danach Render/Shorts.",
        }
    )


def repair_video(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    source_id = first_text(payload, "workflow_id", "id")
    source_dir_raw = first_text(payload, "workflow_dir", "dir")
    source_wf = load_workflow(config, source_id) if source_id else None
    source_dir = resolve_path(config, source_dir_raw) if source_dir_raw else None
    if not source_wf and source_dir:
        source_wf = read_json_file(source_dir / "workflow.json", {})
    if not source_wf:
        return fail("workflow_id oder workflow_dir nicht gefunden.")

    source_artifacts = source_wf.get("artifacts") if isinstance(source_wf.get("artifacts"), dict) else {}
    if not source_dir:
        source_dir = Path(str(source_wf.get("workflow_dir") or "")).expanduser()
    if not source_dir or not source_dir.exists():
        source_dir = workflows_dir(config) / str(source_wf.get("id") or source_id)

    script_src = resolve_existing_artifact(config, payload, source_artifacts, source_dir, "script_path", "script", "script.txt")
    scenes_src = resolve_existing_artifact(config, payload, source_artifacts, source_dir, "scenes_json_path", "scenes_json", "scenes.json")
    assets_src = resolve_existing_artifact(config, payload, source_artifacts, source_dir, "video_assets_path", "video_assets", "video_assets.json", required=False)
    if not script_src:
        return fail("Repair nicht moeglich: script.txt fehlt.")
    if not scenes_src:
        return fail("Repair nicht moeglich: scenes.json fehlt.")

    script_text = script_src.read_text(encoding="utf-8", errors="replace")
    scenes_data = read_json_file(scenes_src, {})
    assets = read_json_file(assets_src, {}) if assets_src else {}
    if not isinstance(assets, dict):
        assets = {}
    if not assets.get("voice_script"):
        assets["voice_script"] = script_text
    if not assets.get("title"):
        assets["title"] = first_text(payload, "title", "video_title") or str(source_wf.get("title") or source_wf.get("query") or "Repair Video")
    if not assets.get("duration_s"):
        assets["duration_s"] = estimate_duration(script_text)
    if not assets.get("scenes") and isinstance(scenes_data, dict):
        assets["scenes"] = scenes_data.get("scenes") or []
    assets = normalize_assets(assets, source_wf)

    workflow_id = "wf-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    workflow_dir = workflows_dir(config) / workflow_id
    workflow_dir.mkdir(parents=True, exist_ok=True)
    render_out_dir = first_text(payload, "render_out_dir", "video_out_dir") or str(default_render_output_dir(config) / workflow_id)

    wf = {
        "id": workflow_id,
        "kind": "video_repair",
        "status": "running",
        "stage": "assets_ready",
        "query": first_text(payload, "query", "topic", "thema", "q") or str(source_wf.get("query") or ""),
        "title": first_text(payload, "title", "video_title") or str(source_wf.get("title") or assets.get("title") or "Repair Video"),
        "target_modul_id": first_text(payload, "target_modul_id", "target", "chat_modul_id") or str(source_wf.get("target_modul_id") or config.get("default_target_modul_id") or "chat.deepseekdeepseekv4flash"),
        "normalizer_modul_id": first_text(payload, "normalizer_modul_id", "normalizer") or str(source_wf.get("normalizer_modul_id") or config.get("default_normalizer_modul_id") or "llm_worker.video_normalizer"),
        "reviewer_modul_id": first_text(payload, "reviewer_modul_id", "reviewer", "quality_reviewer_modul_id") or str(source_wf.get("reviewer_modul_id") or config.get("default_reviewer_modul_id") or source_wf.get("normalizer_modul_id") or config.get("default_normalizer_modul_id") or "llm_worker.video_normalizer"),
        "factcheck_modul_id": first_text(payload, "factcheck_modul_id", "fact_checker_modul_id", "factchecker_modul_id", "factchecker") or str(source_wf.get("factcheck_modul_id") or config.get("default_factcheck_modul_id") or "factcheck.default"),
        "tts_modul_id": first_text(payload, "tts_modul_id", "tts") or str(source_wf.get("tts_modul_id") or config.get("default_tts_modul_id") or "tts.default"),
        "video_modul_id": first_text(payload, "video_modul_id") or str(source_wf.get("video_modul_id") or config.get("default_video_modul_id") or "video_pipeline.default"),
        "parent_task_id": inherited_parent_task_id(payload, config),
        "source_task_id": first_text(config, "task_id"),
        "source_workflow_id": source_wf.get("id") or source_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "workflow_dir": str(workflow_dir),
        "options": {
            "chat_route": first_text(payload, "chat_route", "zurueck_an"),
            "audio_path": first_text(payload, "audio_path", "audio"),
            "preview": bool_param(payload.get("preview"), False),
            "auto_render": True,
            "auto_shorts": bool_param(payload.get("auto_shorts"), cfg_bool(config, "auto_shorts", False)),
            "auto_upload": bool_param(payload.get("auto_upload"), cfg_bool(config, "auto_upload", False)),
            "upload_privacy": first_text(payload, "upload_privacy", "privacy") or str(config.get("default_upload_privacy") or "unlisted"),
            "fact_check": bool_param(payload.get("fact_check"), cfg_bool(config, "fact_check", True)),
            "fact_check_min_score": int_param(payload.get("fact_check_min_score"), cfg_int(config, "fact_check_min_score", 72), 0, 100),
            "fact_check_max_claims": int_param(payload.get("fact_check_max_claims"), cfg_int(config, "fact_check_max_claims", 14), 1, 60),
            "factcheck_max_repairs": int_param(payload.get("factcheck_max_repairs"), cfg_int(config, "factcheck_max_repairs", cfg_int(config, "quality_max_repairs", 2)), 0, 5),
            "quality_gate": bool_param(payload.get("quality_gate"), cfg_bool(config, "quality_gate", True)),
            "quality_auto_repair": bool_param(payload.get("quality_auto_repair"), cfg_bool(config, "quality_auto_repair", True)),
            "quality_min_score": int_param(payload.get("quality_min_score"), cfg_int(config, "quality_min_score", 78), 0, 100),
            "quality_max_repairs": int_param(payload.get("quality_max_repairs"), cfg_int(config, "quality_max_repairs", 2), 0, 5),
            "quality_review_max_repairs": int_param(payload.get("quality_review_max_repairs") or payload.get("review_max_repairs"), cfg_int(config, "quality_review_max_repairs", cfg_int(config, "quality_max_repairs", 2)), 0, 5),
            "require_tts": bool_param(payload.get("require_tts"), cfg_bool(config, "require_tts", True)),
            "allow_silent_audio": bool_param(payload.get("allow_silent_audio"), cfg_bool(config, "allow_silent_audio", False)),
            "tts_provider": first_text(payload, "tts_provider", "provider") or str(config.get("default_tts_provider") or "xai"),
            "animate_scenes": None if payload.get("animate_scenes") is None else bool_param(payload.get("animate_scenes"), False),
            "tts_voice": first_text(payload, "tts_voice", "voice", "voice_id") or str(config.get("default_tts_voice") or ""),
            "tts_language": first_text(payload, "tts_language", "language", "lang", "sprache") or str(config.get("default_tts_language") or "de"),
            "tts_fast": bool_param(payload.get("tts_fast"), cfg_bool(config, "default_tts_fast", True)),
            "shorts_count": int_param(payload.get("shorts_count") or payload.get("count"), cfg_int(config, "default_shorts_count", 30), 1, 100),
            "shorts_duration_s": float_param(payload.get("shorts_duration_s") or payload.get("short_duration_s"), cfg_float(config, "default_shorts_duration_s", 45.0), 3.0, 180.0),
            "render_out_dir": render_out_dir,
            "allow_extra_research": False,
            "normalizer_fallback_modul_ids": list_param(payload.get("normalizer_fallback_modul_ids") or payload.get("normalizer_fallbacks")),
            "reviewer_fallback_modul_ids": list_param(payload.get("reviewer_fallback_modul_ids") or payload.get("reviewer_fallbacks")),
        },
        "tasks": {},
        "artifacts": {
            "source_workflow": str(source_dir / "workflow.json"),
        },
        "events": [event("created", f"Repair aus Workflow {source_wf.get('id') or source_id} gestartet.")],
    }
    if source_artifacts.get("crawl_id"):
        wf["artifacts"]["crawl_id"] = source_artifacts.get("crawl_id")

    assets_path = workflow_dir / "video_assets.json"
    scenes_path = workflow_dir / "scenes.json"
    script_path = workflow_dir / "script.txt"
    write_json(assets_path, assets)
    write_json(scenes_path, {"title": assets.get("title"), "scenes": assets.get("scenes") or []})
    write_text(script_path, assets.get("voice_script") or script_text)
    wf["artifacts"].update({"video_assets": str(assets_path), "scenes_json": str(scenes_path), "script": str(script_path)})
    save_synthesis(wf, config, status="assets_ready", assets=assets)

    continue_after_assets_ready(wf, config, assets, reason="repair")
    save_workflow(wf, config)

    return ok(
        {
            "workflow_id": workflow_id,
            "source_workflow_id": source_wf.get("id") or source_id,
            "status": wf["status"],
            "stage": wf["stage"],
            "tts_task_id": wf.get("tasks", {}).get("synthesize_audio", ""),
            "render_out_dir": render_out_dir,
            "workflow_dir": str(workflow_dir),
            "next": "workflow_trigger.tick wartet auf TTS, rendert danach mit echter Audiospur und schneidet neue Shorts.",
        }
    )


def call_module_tool(config: dict[str, Any], module_name: str, tool: str, params_list: list[str], timeout: int = 60) -> dict[str, Any]:
    """Ruft ein anderes Python-Modul direkt via stdio-Protokoll auf (gleiche
    Mechanik wie prepare_deepdive_context). Persona-Settings + home_dir werden
    aus agent-data/config.json uebernommen, damit das Modul dieselben Daten
    sieht wie seine echte Instanz."""
    module_path = project_root(config) / "modules" / module_name / "module.py"
    if not module_path.exists():
        return {"success": False, "data": f"Modul fehlt: {module_name}"}
    mod_config: dict[str, Any] = {"project_root": str(project_root(config)), "python_timeout_s": timeout}
    try:
        all_cfg = json.loads((project_root(config) / "agent-data" / "config.json").read_text(encoding="utf-8"))
        inst = next((m for m in all_cfg.get("module", []) if m.get("typ") == module_name), None)
        if inst:
            mod_config.update({k: v for k, v in (inst.get("settings") or {}).items() if v is not None})
            mod_config["home_dir"] = str(project_root(config) / "agent-data" / "home" / inst["id"])
    except Exception:
        pass
    req = {"action": "handle_tool", "tool": tool, "params": params_list, "config": mod_config}
    try:
        proc = subprocess.run(
            [sys.executable or "python3", str(module_path)],
            input=json.dumps(req, ensure_ascii=False), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
        return json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception as exc:
        return {"success": False, "data": f"{module_name} Aufruf fehlgeschlagen: {exc}"}


def normalized_topic(text: str) -> str:
    return " ".join(str(text or "").lower().split())[:160]


def ensure_daily_video(config: dict[str, Any]) -> dict[str, Any] | None:
    """Tagesvideo-Garantie: ist der heutige Produktionsversuch tot und kein
    Erfolg da, wird das NAECHSTE Planner-Thema automatisch gestartet — bis
    max_attempts erreicht ist. Ein blockiertes Thema darf den Tag nicht mehr
    kosten; es kostet nur noch einen Themenwechsel."""
    if not cfg_bool(config, "daily_video_guarantee", True):
        return None
    max_attempts = cfg_int(config, "daily_guarantee_max_attempts", 3, 1, 6)
    today = time.strftime("%Y%m%d", time.gmtime())
    try:
        wfs = load_workflows(config, None, 60, include_done=True, include_failed=True, failed_max_age_hours=30.0)
    except Exception:
        return None
    todays = [w for w in wfs if str(w.get("id") or "").startswith(f"wf-{today}T")]
    if not todays:
        return None  # Autopilot hat heute noch nicht gefeuert — nichts zu garantieren
    if any(w.get("status") in ("running", "waiting") for w in todays):
        return None
    if any(w.get("status") == "success" for w in todays):
        return None
    if len(todays) >= max_attempts:
        marker = workflows_dir(config) / f".guarantee_exhausted_{today}"
        if not marker.exists():
            try:
                marker.write_text(now_iso(), encoding="utf-8")
            except Exception:
                pass
            return {"action": "exhausted", "attempts": len(todays),
                    "detail": f"Tagesvideo-Garantie: {len(todays)} Themen fehlgeschlagen, Budget erschoepft"}
        return None

    tried = {normalized_topic((w.get("options") or {}).get("query") or w.get("query") or w.get("title")) for w in todays}
    res = call_module_tool(config, "content_planner", "content_planner.proposals", [json.dumps({"limit": 30})])
    proposals = []
    if res.get("success"):
        data = res.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        proposals = (data or {}).get("proposals") or []
    now_ts = int(time.time())
    candidate = None
    for item in proposals:
        if str(item.get("status") or "") not in ("next", "proposed", "queued"):
            continue
        if int_param(item.get("snoozed_until"), 0, 0, 2**62) > now_ts:
            continue
        if normalized_topic(item.get("query") or item.get("title")) in tried:
            continue
        candidate = item
        break
    if not candidate:
        return {"action": "no_candidate", "attempts": len(todays),
                "detail": "Tagesvideo-Garantie: kein unverbrauchtes Planner-Thema verfuegbar"}

    call_module_tool(config, "content_planner", "content_planner.decide",
                     [json.dumps({"id": candidate.get("id"), "action": "now"})])
    last = todays[-1]
    opts = last.get("options") or {}
    payload: dict[str, Any] = {
        "query": candidate.get("query") or candidate.get("title"),
        "title": candidate.get("title") or "",
    }
    for key in ("language", "target_minutes", "auto_upload", "upload_privacy", "auto_shorts",
                "shorts_count", "chat_route", "animate_scenes", "tts_provider", "tts_voice"):
        if opts.get(key) is not None:
            payload[key] = opts[key]
    started = deepdive_video([json.dumps(payload, ensure_ascii=False)], config)
    return {"action": "started_fallback", "attempts": len(todays) + 1,
            "topic": payload["query"],
            "detail": f"Tagesvideo-Garantie: Thema-Versuch {len(todays) + 1}/{max_attempts} gestartet",
            "result_ok": bool(started.get("success"))}


def tick(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    wanted = first_text(payload, "workflow_id", "id")
    limit = int_param(payload.get("limit"), cfg_int(config, "tick_limit", 12), 1, 100)
    include_failed = bool_param(payload.get("recover_failed"), cfg_bool(config, "recover_failed_workflows", True))
    failed_max_age_hours = None if wanted else cfg_float(config, "recover_failed_max_age_hours", 12.0)
    workflows = load_workflows(
        config,
        wanted,
        limit,
        include_failed=include_failed or bool(wanted),
        failed_max_age_hours=failed_max_age_hours,
    )
    changed = 0
    processed = []
    for wf in workflows:
        before = json.dumps(wf, sort_keys=True)
        process_workflow(wf, config)
        if json.dumps(wf, sort_keys=True) != before:
            changed += 1
            save_workflow(wf, config)
        processed.append(workflow_summary(wf, config))
    guarantee = None
    try:
        guarantee = ensure_daily_video(config)
    except Exception as exc:
        guarantee = {"action": "error", "detail": f"Garantie-Check fehlgeschlagen: {exc}"}
    health = None
    if cfg_bool(config, "production_health_enabled", True):
        health = compute_production_health(
            config,
            window_hours=cfg_float(config, "production_health_window_hours", 36.0),
            require_upload=cfg_bool(config, "production_health_require_upload", True),
            write=True,
        )
    return ok({"processed": len(processed), "changed": changed, "health": health,
               "daily_guarantee": guarantee, "workflows": processed})


def process_workflow(wf: dict[str, Any], config: dict[str, Any]) -> None:
    if wf.get("status") == "failed":
        if cfg_bool(config, "recover_failed_workflows", True):
            recover_failed_workflow(wf, config)
        return
    if wf.get("status") not in {"running", "waiting"}:
        return
    stage = wf.get("stage")
    if stage == "deepdive_report":
        advance_deepdive_report(wf, config)
    elif stage == "normalize_report":
        advance_normalize_report(wf, config)
    elif stage == "fact_check_assets":
        advance_fact_check_assets(wf, config)
    elif stage == "review_assets":
        advance_review_assets(wf, config)
    elif stage == "repair_assets":
        advance_repair_assets(wf, config)
    elif stage == "generate_visuals":
        advance_generate_visuals(wf, config)
    elif stage == "synthesize_audio":
        advance_synthesize_audio(wf, config)
    elif stage == "render_video":
        advance_render_video(wf, config)
    elif stage == "upload_video":
        advance_upload_video(wf, config)
    elif stage == "make_shorts":
        advance_make_shorts(wf, config)


def recovery_state(wf: dict[str, Any]) -> dict[str, Any]:
    recovery = wf.get("recovery")
    if not isinstance(recovery, dict):
        recovery = {}
        wf["recovery"] = recovery
    return recovery


def begin_self_recovery(wf: dict[str, Any], config: dict[str, Any], detail: str) -> bool:
    recovery = recovery_state(wf)
    attempts = int_param(recovery.get("failed_recovery_count"), 0, 0, 100)
    max_attempts = cfg_int(config, "recover_failed_max_attempts", 2, 0, 10)
    if attempts >= max_attempts:
        return False
    recovery["failed_recovery_count"] = attempts + 1
    recovery["last_recovery_at"] = now_iso()
    recovery["last_recovery_stage"] = wf.get("stage")
    wf["status"] = "running"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("self_recovery_started", f"{detail} (Versuch {attempts + 1}/{max_attempts})"))
    return True


def is_capacity_error(text: str) -> bool:
    haystack = str(text or "").casefold()
    return any(
        marker in haystack
        for marker in (
            "402 payment required",
            "insufficient balance",
            "quota exceeded",
            "billing",
            "out of credits",
            "credit balance",
        )
    )


def fallback_modules(wf: dict[str, Any], option_key: str, config: dict[str, Any], config_key: str) -> list[str]:
    options = wf.get("options") if isinstance(wf.get("options"), dict) else {}
    values = list_param(options.get(option_key))
    values.extend(list_param(config.get(config_key)))
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def persist_configured_fallbacks(wf: dict[str, Any], config: dict[str, Any]) -> None:
    options = wf.get("options")
    if not isinstance(options, dict):
        options = {}
        wf["options"] = options
    for option_key, config_key in (
        ("target_fallback_modul_ids", "fallback_target_modul_ids"),
        ("normalizer_fallback_modul_ids", "fallback_normalizer_modul_ids"),
        ("reviewer_fallback_modul_ids", "fallback_reviewer_modul_ids"),
    ):
        if not list_param(options.get(option_key)):
            values = list_param(config.get(config_key))
            if values:
                options[option_key] = values


def restart_llm_stage_with_fallback(
    wf: dict[str, Any],
    config: dict[str, Any],
    stage: str,
    task_key: str,
    module_field: str,
    option_key: str,
    config_key: str,
    prompt: str,
    reason: str,
) -> bool:
    if not is_capacity_error(reason):
        return False

    persist_configured_fallbacks(wf, config)
    current = str(wf.get(module_field) or "").strip()
    recovery = recovery_state(wf)
    used_by_stage = recovery.get("llm_fallbacks")
    if not isinstance(used_by_stage, dict):
        used_by_stage = {}
        recovery["llm_fallbacks"] = used_by_stage
    used = list_param(used_by_stage.get(stage))
    fallback = ""
    for candidate in fallback_modules(wf, option_key, config, config_key):
        if candidate and candidate != current and candidate not in used:
            fallback = candidate
            break
    if not fallback:
        recovery["last_capacity_error"] = truncate(reason, 600)
        wf.setdefault("events", []).append(event("llm_fallback_unavailable", f"{stage}: API-/Quota-Fehler, aber kein ungenutzter Fallback konfiguriert."))
        return False

    old_task = str((wf.get("tasks") or {}).get(task_key) or "")
    if old_task:
        wf.setdefault("tasks", {})[f"{task_key}_failed_{len(used) + 1}"] = old_task
    used.append(fallback)
    used_by_stage[stage] = used
    wf[module_field] = fallback
    new_task = enqueue_llm_task(
        config,
        fallback,
        prompt,
        created_by="workflow_trigger:fallback",
        timeout_s=cfg_int(config, "default_task_timeout_s", 3600, 60, 14400),
        back_route=wf.get("options", {}).get("chat_route") or None,
        parent_id=wf.get("parent_task_id") or None,
        workflow_id=wf.get("id"),
        workflow_stage=stage,
    )
    wf.setdefault("tasks", {})[task_key] = new_task
    wf["stage"] = stage
    wf["status"] = "running"
    wf["updated_at"] = now_iso()
    recovery["last_capacity_error"] = truncate(reason, 600)
    wf.setdefault("events", []).append(event(
        "llm_fallback_started",
        f"{stage}: {current or '(leer)'} fiel wegen API-/Quota-Fehler aus; Fallback {fallback} gestartet: {new_task}",
    ))
    return True


def restart_deepdive_with_fallback(wf: dict[str, Any], config: dict[str, Any], reason: str) -> bool:
    return restart_llm_stage_with_fallback(
        wf,
        config,
        "deepdive_report",
        "deepdive_report",
        "target_modul_id",
        "target_fallback_modul_ids",
        "fallback_target_modul_ids",
        deepdive_report_prompt(str(wf.get("query") or ""), str(wf.get("title") or wf.get("query") or "")),
        reason,
    )


def restart_normalize_with_fallback(wf: dict[str, Any], config: dict[str, Any], reason: str) -> bool:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    report = read_text_file(artifacts.get("deepdive_report"))
    if not report:
        return False
    crawl_id = str(artifacts.get("crawl_id") or extract_crawl_id(report) or "")
    return restart_llm_stage_with_fallback(
        wf,
        config,
        "normalize_report",
        "normalize_report",
        "normalizer_modul_id",
        "normalizer_fallback_modul_ids",
        "fallback_normalizer_modul_ids",
        normalize_prompt(wf, report, prepare_deepdive_context(config, wf, crawl_id)),
        reason,
    )


def restart_review_with_fallback(wf: dict[str, Any], config: dict[str, Any], reason: str) -> bool:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    assets = read_json_file(artifacts.get("video_assets"), {})
    if not isinstance(assets, dict) or not assets:
        return False
    return restart_llm_stage_with_fallback(
        wf,
        config,
        "review_assets",
        "review_assets",
        "reviewer_modul_id",
        "reviewer_fallback_modul_ids",
        "fallback_reviewer_modul_ids",
        review_prompt(wf, assets, local_asset_review(wf, assets, config)),
        reason,
    )


def restart_repair_with_fallback(wf: dict[str, Any], config: dict[str, Any], reason: str) -> bool:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    assets = read_json_file(artifacts.get("video_assets"), {})
    review = read_json_file(artifacts.get("quality_review"), {})
    if not isinstance(assets, dict) or not assets or not isinstance(review, dict):
        return False
    return restart_llm_stage_with_fallback(
        wf,
        config,
        "repair_assets",
        "repair_assets",
        "normalizer_modul_id",
        "normalizer_fallback_modul_ids",
        "fallback_normalizer_modul_ids",
        repair_prompt(wf, assets, review),
        reason,
    )


def recover_failed_workflow(wf: dict[str, Any], config: dict[str, Any]) -> None:
    stage = str(wf.get("stage") or "")
    tasks = wf.get("tasks") if isinstance(wf.get("tasks"), dict) else {}

    task_recovery: dict[str, tuple[str, str, Any]] = {
        "deepdive_report": ("deepdive_report", "deepdive_report", advance_deepdive_report),
        "normalize_report": ("normalize_report", "normalize_report", advance_normalize_report),
        "fact_check_assets": ("fact_check_assets", "fact_check_assets", advance_fact_check_assets),
        "fact_check_failed": ("fact_check_assets", "fact_check_assets", advance_fact_check_assets),
        "review_assets": ("review_assets", "review_assets", advance_review_assets),
        "review_failed": ("review_assets", "review_assets", advance_review_assets),
        "repair_assets": ("repair_assets", "repair_assets", advance_repair_assets),
        "generate_visuals": ("generate_visuals", "generate_visuals", advance_generate_visuals),
        "synthesize_audio": ("synthesize_audio", "synthesize_audio", advance_synthesize_audio),
        "render_video": ("render_video", "render_video", advance_render_video),
        "upload_video": ("upload_video", "upload_video", advance_upload_video),
        "make_shorts": ("make_shorts", "make_shorts", advance_make_shorts),
    }

    if stage in task_recovery:
        task_key, live_stage, advancer = task_recovery[stage]
        task = load_task(config, tasks.get(task_key))
        task_status = str(task.get("status") or "") if task else ""
        can_replay = task_status == "success" or (live_stage == "upload_video" and task_status and task_status not in {"erstellt", "gestartet"})
        if can_replay and begin_self_recovery(wf, config, f"Fehlgeschlagenen Workflow ab Stage '{stage}' erneut ausgewertet; Task ist {task_status}"):
            wf["stage"] = live_stage
            advancer(wf, config)
            return
        task_result = str(task.get("result") or "") if task else ""
        if task_status == "failed" and is_capacity_error(task_result):
            if stage == "deepdive_report" and restart_deepdive_with_fallback(wf, config, task_result):
                return
            if stage == "normalize_report" and restart_normalize_with_fallback(wf, config, task_result):
                return
            if stage == "review_assets" and restart_review_with_fallback(wf, config, task_result):
                return
            if stage == "repair_assets" and restart_repair_with_fallback(wf, config, task_result):
                return
        if stage not in {"review_failed", "fact_check_failed"}:
            return

    if stage == "review_failed":
        recover_review_failed_from_artifacts(wf, config)
        return
    if stage == "fact_check_failed":
        recover_factcheck_failed_from_artifacts(wf, config)


def recover_review_failed_from_artifacts(wf: dict[str, Any], config: dict[str, Any]) -> None:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    assets = read_json_file(artifacts.get("video_assets"), {})
    review = read_json_file(artifacts.get("quality_review"), {})
    if not isinstance(assets, dict) or not assets.get("voice_script") or not isinstance(review, dict) or not review:
        return

    if review_passes(wf, config, review):
        if begin_self_recovery(wf, config, "Quality Review war doch freigabefaehig; Workflow wird fortgesetzt"):
            proceed_after_assets_approved(wf, config, assets=assets)
        return

    quality = quality_state(wf)
    repairs_used = int_param(quality.get("review_repair_count"), 0, 0, 100)
    max_repairs = review_repair_budget(wf, config)
    auto_repair = bool_param(wf.get("options", {}).get("quality_auto_repair"), cfg_bool(config, "quality_auto_repair", True))
    if auto_repair and repairs_used < max_repairs:
        if begin_self_recovery(wf, config, "Quality Review ist reparierbar; fehlende Review-Reparatur wird nachgeholt"):
            start_repair_task(wf, config, assets, review, scope="review")
        return

    decision = str(review.get("decision") or "").strip().lower()
    score = int_param(review.get("score"), 0, 0, 100)
    proceed_after = bool_param(wf.get("options", {}).get("quality_proceed_after_repairs"), cfg_bool(config, "quality_proceed_after_repairs", True))
    proceed_min = int_param(wf.get("options", {}).get("quality_proceed_min_score"), cfg_int(config, "quality_proceed_min_score", 70), 0, 100)
    if proceed_after and decision != "reject" and score >= proceed_min:
        if begin_self_recovery(wf, config, f"Review-Budget erschoepft, aber score={score}; best-effort Render ohne ungeprueften Upload"):
            if not cfg_bool(config, "quality_best_effort_upload", False):
                wf.setdefault("options", {})["auto_upload"] = False
            proceed_after_assets_approved(wf, config, assets=assets)


def recover_factcheck_failed_from_artifacts(wf: dict[str, Any], config: dict[str, Any]) -> None:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    assets = read_json_file(artifacts.get("video_assets"), {})
    report = read_json_file(artifacts.get("factcheck_report"), {})
    if not isinstance(assets, dict) or not assets.get("voice_script") or not isinstance(report, dict) or not report:
        return

    min_score = int_param(wf.get("options", {}).get("fact_check_min_score"), cfg_int(config, "fact_check_min_score", 72), 0, 100)
    score = int_param(report.get("score"), 0, 0, 100)
    if bool(report.get("pass")) and score >= min_score:
        if begin_self_recovery(wf, config, "Faktencheck-Report ist freigabefaehig; Workflow wird fortgesetzt"):
            continue_after_factcheck_passed(wf, config, assets=assets, reason="self_recovery")
        return
    proceed_after = bool_param(wf.get("options", {}).get("quality_proceed_after_repairs"), cfg_bool(config, "quality_proceed_after_repairs", True))
    proceed_min = int_param(wf.get("options", {}).get("quality_proceed_min_score"), cfg_int(config, "quality_proceed_min_score", 70), 0, 100)
    if proceed_after and not report.get("blocking_issues") and score >= proceed_min:
        if begin_self_recovery(wf, config, f"Faktencheck best effort (score={score}, 0 Blocker); Workflow wird fortgesetzt"):
            continue_after_factcheck_passed(wf, config, assets=assets, reason="best_effort_recovery")
        return

    quality = quality_state(wf)
    repairs_used = int_param(quality.get("factcheck_repair_count"), 0, 0, 100)
    max_repairs = factcheck_repair_budget(wf, config)
    auto_repair = bool_param(wf.get("options", {}).get("quality_auto_repair"), cfg_bool(config, "quality_auto_repair", True))
    if auto_repair and repairs_used < max_repairs and (report.get("blocking_issues") or report.get("warnings")):
        if begin_self_recovery(wf, config, "Faktencheck ist reparierbar; fehlende Faktencheck-Reparatur wird nachgeholt"):
            start_repair_task(wf, config, assets, report, scope="factcheck")


def advance_deepdive_report(wf: dict[str, Any], config: dict[str, Any]) -> None:
    task_id = wf.get("tasks", {}).get("deepdive_report")
    task = load_task(config, task_id)
    if not task:
        mark_failed(wf, f"DeepDive-Report Task fehlt: {task_id}")
        return
    if task["status"] in {"erstellt", "gestartet"}:
        wf["status"] = "waiting"
        wf["updated_at"] = now_iso()
        return
    if task["status"] != "success":
        if restart_deepdive_with_fallback(wf, config, task.get("result") or ""):
            return
        mark_failed(wf, f"DeepDive-Report Task endete mit {task['status']}: {truncate(task.get('result') or '', 600)}")
        return

    report = task.get("result") or ""
    workflow_dir = Path(wf["workflow_dir"])
    report_path = workflow_dir / "deepdive_report.txt"
    write_text(report_path, report)
    wf.setdefault("artifacts", {})["deepdive_report"] = str(report_path)
    crawl_id = extract_crawl_id(report)
    if crawl_id:
        wf["artifacts"]["crawl_id"] = crawl_id

    normalize_task = enqueue_llm_task(
        config,
        wf.get("normalizer_modul_id") or wf["target_modul_id"],
        normalize_prompt(wf, report, prepare_deepdive_context(config, wf, crawl_id)),
        created_by="workflow_trigger",
        timeout_s=cfg_int(config, "default_task_timeout_s", 3600, 60, 14400),
        back_route=wf.get("options", {}).get("chat_route") or None,
        parent_id=wf.get("parent_task_id") or None,
        workflow_id=wf.get("id"),
        workflow_stage="normalize_report",
    )
    wf.setdefault("tasks", {})["normalize_report"] = normalize_task
    wf["stage"] = "normalize_report"
    wf["status"] = "running"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("dependency_met", f"DeepDive-Report fertig, Normalisierung gestartet: {normalize_task}"))


def advance_normalize_report(wf: dict[str, Any], config: dict[str, Any]) -> None:
    task_id = wf.get("tasks", {}).get("normalize_report")
    task = load_task(config, task_id)
    if not task:
        mark_failed(wf, f"Normalisierungstask fehlt: {task_id}")
        return
    if task["status"] in {"erstellt", "gestartet"}:
        wf["status"] = "waiting"
        wf["updated_at"] = now_iso()
        return
    if task["status"] != "success":
        if restart_normalize_with_fallback(wf, config, task.get("result") or ""):
            return
        mark_failed(wf, f"Normalisierung endete mit {task['status']}: {truncate(task.get('result') or '', 600)}")
        return

    raw = task.get("result") or ""
    workflow_dir = Path(wf["workflow_dir"])
    normalized_raw_path = workflow_dir / "normalized_raw.txt"
    write_text(normalized_raw_path, raw)
    wf.setdefault("artifacts", {})["normalized_raw"] = str(normalized_raw_path)
    assets = parse_assets(raw)
    if not assets:
        mark_failed(wf, "Normalisierung lieferte kein parsebares VIDEO_ASSETS_JSON.")
        return

    assets = normalize_assets(assets, wf)
    assets_path = workflow_dir / "video_assets.json"
    scenes_path = workflow_dir / "scenes.json"
    script_path = workflow_dir / "script.txt"
    write_json(assets_path, assets)
    write_json(scenes_path, {"title": assets["title"], "scenes": assets["scenes"]})
    write_text(script_path, assets["voice_script"])
    wf.setdefault("artifacts", {}).update(
        {
            "video_assets": str(assets_path),
            "scenes_json": str(scenes_path),
            "script": str(script_path),
        }
    )
    save_synthesis(wf, config, status="assets_ready", assets=assets)
    continue_after_assets_ready(wf, config, assets, reason="normalization")


def quality_gate_enabled(wf: dict[str, Any], config: dict[str, Any]) -> bool:
    options = wf.get("options") if isinstance(wf.get("options"), dict) else {}
    return bool_param(options.get("quality_gate"), cfg_bool(config, "quality_gate", True))


def fact_check_enabled(wf: dict[str, Any], config: dict[str, Any]) -> bool:
    options = wf.get("options") if isinstance(wf.get("options"), dict) else {}
    return bool_param(options.get("fact_check"), cfg_bool(config, "fact_check", True))


def quality_state(wf: dict[str, Any]) -> dict[str, Any]:
    quality = wf.get("quality")
    if not isinstance(quality, dict):
        quality = {}
        wf["quality"] = quality
    return quality


def factcheck_repair_budget(wf: dict[str, Any], config: dict[str, Any]) -> int:
    options = wf.get("options") if isinstance(wf.get("options"), dict) else {}
    legacy = int_param(options.get("quality_max_repairs"), cfg_int(config, "quality_max_repairs", 2), 0, 5)
    return int_param(options.get("factcheck_max_repairs"), cfg_int(config, "factcheck_max_repairs", legacy), 0, 5)


def review_repair_budget(wf: dict[str, Any], config: dict[str, Any]) -> int:
    options = wf.get("options") if isinstance(wf.get("options"), dict) else {}
    legacy = int_param(options.get("quality_max_repairs"), cfg_int(config, "quality_max_repairs", 2), 0, 5)
    return int_param(options.get("quality_review_max_repairs"), cfg_int(config, "quality_review_max_repairs", legacy), 0, 5)


def continue_after_assets_ready(wf: dict[str, Any], config: dict[str, Any], assets: dict[str, Any], reason: str = "") -> None:
    if fact_check_enabled(wf, config):
        start_factcheck_task(wf, config, assets, reason=reason)
        return
    continue_after_factcheck_passed(wf, config, assets=assets, reason=reason)


def continue_after_factcheck_passed(wf: dict[str, Any], config: dict[str, Any], assets: dict[str, Any], reason: str = "") -> None:
    if quality_gate_enabled(wf, config):
        start_review_task(wf, config, assets, reason=reason or "factcheck_passed")
        return
    proceed_after_assets_approved(wf, config, assets=assets)


def start_factcheck_task(wf: dict[str, Any], config: dict[str, Any], assets: dict[str, Any], reason: str = "") -> None:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    script_path = str(artifacts.get("script") or "")
    assets_path = str(artifacts.get("video_assets") or "")
    if not script_path or not assets_path:
        mark_failed(wf, "Faktencheck kann nicht starten: script oder video_assets fehlen.")
        return
    params = {
        "title": assets.get("title") or wf.get("title") or wf.get("query") or "",
        "query": wf.get("query") or "",
        "assets_path": assets_path,
        "script_path": script_path,
        "source_notes": assets.get("source_notes") or [],
        "deepdive_report_path": str(artifacts.get("deepdive_report") or ""),
        "deepdive_context_path": str(artifacts.get("deepdive_context") or ""),
        "max_claims": int_param(wf.get("options", {}).get("fact_check_max_claims"), cfg_int(config, "fact_check_max_claims", 14), 1, 60),
        "min_score": int_param(wf.get("options", {}).get("fact_check_min_score"), cfg_int(config, "fact_check_min_score", 72), 0, 100),
    }
    task = enqueue_direct_task(
        config,
        wf.get("factcheck_modul_id") or str(config.get("default_factcheck_modul_id") or "factcheck.default"),
        "factcheck.video_assets",
        [json.dumps(params, ensure_ascii=False)],
        created_by="workflow_trigger",
        timeout_s=cfg_int(config, "default_task_timeout_s", 3600, 60, 14400),
        back_route=wf.get("options", {}).get("chat_route") or None,
        parent_id=wf.get("parent_task_id") or None,
        workflow_id=wf.get("id"),
        workflow_stage="fact_check_assets",
    )
    wf.setdefault("tasks", {})["fact_check_assets"] = task
    wf["stage"] = "fact_check_assets"
    wf["status"] = "running"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("factcheck_started", f"Faktencheck gestartet ({reason or 'assets'}): {task}"))


def advance_fact_check_assets(wf: dict[str, Any], config: dict[str, Any]) -> None:
    task_id = wf.get("tasks", {}).get("fact_check_assets")
    task = load_task(config, task_id)
    if not task:
        mark_failed(wf, f"Faktencheck-Task fehlt: {task_id}")
        return
    if task["status"] in {"erstellt", "gestartet"}:
        wf["status"] = "waiting"
        wf["updated_at"] = now_iso()
        return
    if task["status"] != "success":
        wf["stage"] = "fact_check_failed"
        mark_failed(wf, f"Faktencheck endete mit {task['status']}: {truncate(task.get('result') or '', 900)}")
        return

    report = parse_jsonish_result(task.get("result") or "")
    if not report:
        wf["stage"] = "fact_check_failed"
        mark_failed(wf, "Faktencheck lieferte kein parsebares JSON.")
        return

    workflow_dir = Path(wf["workflow_dir"])
    report_path = workflow_dir / "factcheck_report.json"
    write_json(report_path, report)
    wf.setdefault("artifacts", {})["factcheck_report"] = str(report_path)
    wf["factcheck"] = {
        "pass": bool(report.get("pass")),
        "score": report.get("score"),
        "decision": report.get("decision"),
        "claims_checked": report.get("claims_checked"),
        "verified_claims": report.get("verified_claims"),
        "blocking_count": len(report.get("blocking_issues") or []),
        "warning_count": len(report.get("warnings") or []),
    }

    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    assets = read_json_file(artifacts.get("video_assets"), {})
    if not isinstance(assets, dict) or not assets.get("voice_script"):
        wf["stage"] = "fact_check_failed"
        mark_failed(wf, "Faktencheck kann nicht fortsetzen: video_assets/voice_script fehlen.")
        return

    min_score = int_param(wf.get("options", {}).get("fact_check_min_score"), cfg_int(config, "fact_check_min_score", 72), 0, 100)
    score = int_param(report.get("score"), 0, 0, 100)
    if not bool(report.get("pass")) or score < min_score:
        # Faktencheck war bisher eine Sackgasse, obwohl er suggested_rewrites
        # liefert und die Review-Stage laengst einen Auto-Repair-Loop hat.
        # Jetzt: blockierte Claims gehen (begrenzt durch quality_max_repairs)
        # in dieselbe Reparatur — danach laeuft der Faktencheck erneut.
        quality = quality_state(wf)
        repairs_used = int_param(quality.get("factcheck_repair_count"), 0, 0, 100)
        max_repairs = factcheck_repair_budget(wf, config)
        auto_repair = bool_param(wf.get("options", {}).get("quality_auto_repair"), cfg_bool(config, "quality_auto_repair", True))
        if auto_repair and repairs_used < max_repairs and (report.get("blocking_issues") or report.get("warnings")):
            report_for_repair = dict(report)
            if repairs_used >= max_repairs - 1:
                report_for_repair["repair_directive"] = (
                    "LETZTER Reparaturversuch. Entferne die Saetze ALLER blocking_issues "
                    "ERSATZLOS aus voice_script und aus betroffenen Szenen-Texten "
                    "(bullets/subtitle/say/timeline-Eintraege). Lieber ein kuerzeres "
                    "Video als ein unbelegter Claim. Keine Umformulierungen mehr."
                )
            else:
                report_for_repair["repair_directive"] = (
                    "Formuliere jeden blocking_issue-Satz als klar zugeschriebene, "
                    "UNBESTAETIGTE Aussage um (Muster: 'Ein Bericht von X behauptet — "
                    "unbestaetigt —, dass ...') ODER entferne ihn ersatzlos, wenn der "
                    "DeepDive-Kontext ihn nicht eindeutig stuetzt. Kosmetische "
                    "Wortumstellungen reichen NICHT."
                )
            wf.setdefault("events", []).append(event(
                "factcheck_repair",
                "Faktencheck blockiert (" + factcheck_summary(report) + ") — Auto-Reparatur " + str(repairs_used + 1) + " gestartet",
            ))
            start_repair_task(wf, config, assets, report_for_repair, scope="factcheck")
            return
        # Best-Effort-Ausweg (wie im Quality-Gate): Reparaturen erschoepft,
        # aber NULL harte Blocker und Score >= proceed_min -> weiterlaufen
        # lassen statt das Tagesvideo an 2 Warn-Punkten sterben zu lassen
        # (09.07.: score=70 bei 0 Blockern gegen min_score=72 = kein Video).
        proceed_after = bool_param(wf.get("options", {}).get("quality_proceed_after_repairs"), cfg_bool(config, "quality_proceed_after_repairs", True))
        proceed_min = int_param(wf.get("options", {}).get("quality_proceed_min_score"), cfg_int(config, "quality_proceed_min_score", 70), 0, 100)
        if proceed_after and not report.get("blocking_issues") and score >= proceed_min:
            wf.setdefault("events", []).append(event(
                "factcheck_best_effort",
                f"Faktencheck unter Zielscore ({score} < Ziel), aber 0 Blocker und >= {proceed_min}: best effort weiter",
            ))
            continue_after_factcheck_passed(wf, config, assets=assets, reason="best_effort")
            return
        wf["stage"] = "fact_check_failed"
        save_synthesis(wf, config, status="fact_check_failed", assets=assets)
        mark_failed(wf, "Faktencheck blockiert TTS/Render: " + factcheck_summary(report))
        return

    wf.setdefault("events", []).append(event("factcheck_passed", f"Faktencheck bestanden: score={score} decision={report.get('decision')}"))
    save_synthesis(wf, config, status="fact_checked", assets=assets)
    continue_after_factcheck_passed(wf, config, assets=assets, reason="factcheck_passed")


def factcheck_summary(report: dict[str, Any]) -> str:
    blockers = report.get("blocking_issues") if isinstance(report.get("blocking_issues"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    parts = [
        f"score={report.get('score')}",
        f"decision={report.get('decision')}",
        f"claims={report.get('claims_checked')}",
        f"blocker={len(blockers)}",
        f"warnings={len(warnings)}",
    ]
    first = blockers[0] if blockers else None
    if isinstance(first, dict):
        parts.append(f"erster_blocker={truncate(str(first.get('claim') or ''), 180)}")
        if first.get("reason"):
            parts.append(f"grund={truncate(str(first.get('reason')), 180)}")
    return "; ".join(parts)


def visuals_enabled(wf: dict[str, Any], config: dict[str, Any]) -> bool:
    options = wf.get("options") if isinstance(wf.get("options"), dict) else {}
    return bool_param(options.get("scene_images"), cfg_bool(config, "scene_images", True))


def start_visuals_task(wf: dict[str, Any], config: dict[str, Any]) -> None:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    assets_path = artifacts.get("video_assets") or ""
    modul = str(wf.get("image_modul_id") or config.get("default_image_modul_id") or "image_gen.default")
    params = {
        "assets_json_path": str(assets_path),
        "out_dir": str(Path(wf["workflow_dir"]) / "scene_images"),
    }
    task = enqueue_direct_task(
        config,
        modul,
        "image_gen.for_assets",
        [json.dumps(params, ensure_ascii=False)],
        created_by="workflow_trigger",
        timeout_s=cfg_int(config, "default_task_timeout_s", 3600, 60, 14400),
        back_route=None,
        parent_id=wf.get("parent_task_id") or None,
        workflow_id=wf.get("id"),
        workflow_stage="generate_visuals",
    )
    wf.setdefault("tasks", {})["generate_visuals"] = task
    wf["stage"] = "generate_visuals"
    wf["status"] = "running"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("visuals_started", f"Szenenbilder werden generiert: {task}"))


def advance_generate_visuals(wf: dict[str, Any], config: dict[str, Any]) -> None:
    task_id = wf.get("tasks", {}).get("generate_visuals")
    task = load_task(config, task_id)
    if not task:
        wf.setdefault("events", []).append(event("visuals_skipped", f"Bilder-Task fehlt ({task_id}) — weiter ohne Szenenbilder."))
        proceed_to_audio_or_render(wf, config)
        return
    if task["status"] in {"erstellt", "gestartet"}:
        wf["status"] = "waiting"
        wf["updated_at"] = now_iso()
        return
    # Bilder sind Enhancement, nie Blocker: bei Fehlern einfach weiter.
    if task["status"] == "success":
        result = parse_jsonish_result(task.get("result") or "")
        wf.setdefault("artifacts", {})["scene_images"] = str(Path(wf["workflow_dir"]) / "scene_images")
        wf.setdefault("events", []).append(event(
            "visuals_done",
            f"Szenenbilder: {result.get('generated', '?')} neu, {result.get('cached_hits', 0)} aus Cache, ~${result.get('cost_usd_estimate', 0)}",
        ))
    else:
        wf.setdefault("events", []).append(event("visuals_failed", f"Bilder-Task {task['status']} — weiter ohne Szenenbilder."))
    proceed_to_audio_or_render(wf, config)


def proceed_after_assets_approved(wf: dict[str, Any], config: dict[str, Any], assets: dict[str, Any] | None = None) -> None:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    if assets is None:
        assets = read_json_file(artifacts.get("video_assets"), {})
    if not isinstance(assets, dict) or not assets.get("voice_script"):
        mark_failed(wf, "Freigabe kann nicht fortgesetzt werden: video_assets/voice_script fehlen.")
        return

    # Szenenbilder VOR TTS/Render generieren (Enhancement; Fehler blocken nie).
    if (
        visuals_enabled(wf, config)
        and wf.get("options", {}).get("auto_render", True)
        and not wf.get("tasks", {}).get("generate_visuals")
        and any(isinstance(s, dict) and s.get("image_prompt") for s in assets.get("scenes") or [])
    ):
        start_visuals_task(wf, config)
        return

    proceed_to_audio_or_render(wf, config, assets)


def proceed_to_audio_or_render(wf: dict[str, Any], config: dict[str, Any], assets: dict[str, Any] | None = None) -> None:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    if assets is None:
        assets = read_json_file(artifacts.get("video_assets"), {})
    if not isinstance(assets, dict) or not assets.get("voice_script"):
        mark_failed(wf, "Fortsetzung fehlgeschlagen: video_assets/voice_script fehlen.")
        return

    if not wf.get("options", {}).get("auto_render", True):
        wf["stage"] = "assets_ready"
        wf["status"] = "success"
        wf["updated_at"] = now_iso()
        wf.setdefault("events", []).append(event("completed", "Video-Assets erstellt und freigegeben; auto_render ist aus."))
        save_synthesis(wf, config, status="assets_ready", assets=assets)
        return

    script_path = str(artifacts.get("script") or "")
    if not script_path:
        mark_failed(wf, "TTS kann nicht starten: script.txt fehlt.")
        return

    audio_path = wf.get("options", {}).get("audio_path")
    if audio_path:
        wf.setdefault("artifacts", {})["audio"] = audio_path
        start_render_task(wf, config, assets=assets, audio_path=audio_path)
        return

    if should_generate_tts(wf, config):
        start_tts_task(wf, config, assets=assets, script_path=Path(script_path))
        return

    if can_use_silent_audio(wf, config):
        start_render_task(wf, config, assets=assets, audio_path="", allow_silent=True)
        return

    mark_failed(wf, "Kein audio_path vorhanden und TTS ist nicht konfiguriert/erlaubt. Produktion wird nicht stumm gerendert.")


SENTENCE_END = (".", "!", "?", "\u2026", '"', "\u201c", "\u2019", "'")


def sanitize_shorts(assets: dict[str, Any]) -> list[str]:
    """Repariert/entfernt kaputte Shorts-Metadaten VOR dem Review.

    Der Normalizer liefert gelegentlich mitten im Wort abgeschnittene Hooks
    ('ein neuer Robote', 'so die Anal') — das Review wertete das als Blocker
    und toetete damit das HAUPTVIDEO (09./10.07.). Hooks werden auf den
    letzten vollstaendigen Satz getrimmt; ist nichts Brauchbares uebrig,
    fliegt nur der Short raus, nie das Video."""
    notes: list[str] = []
    shorts = assets.get("shorts")
    if not isinstance(shorts, list):
        return notes
    kept = []
    for sh in shorts:
        if not isinstance(sh, dict):
            continue
        hook = str(sh.get("hook") or "").strip()
        if hook and not hook.endswith(SENTENCE_END):
            cut = max(hook.rfind(p) for p in (".", "!", "?"))
            if cut >= 20:
                sh["hook"] = hook[: cut + 1]
                notes.append("Short-Hook auf letzten ganzen Satz getrimmt")
            else:
                notes.append(f"Short verworfen (Hook abgeschnitten): {hook[:48]}")
                continue
        kept.append(sh)
    if len(kept) != len(shorts) or notes:
        assets["shorts"] = kept
    return notes


def start_review_task(wf: dict[str, Any], config: dict[str, Any], assets: dict[str, Any], reason: str = "") -> None:
    notes = sanitize_shorts(assets)
    if notes:
        wf.setdefault("events", []).append(event("shorts_sanitized", "; ".join(notes)[:280]))
        va = wf.get("artifacts", {}).get("video_assets")
        if va:
            try:
                write_json(Path(va), assets)
            except Exception:
                pass
    local_review = local_asset_review(wf, assets, config)
    workflow_dir = Path(wf["workflow_dir"])
    quality = quality_state(wf)
    review_count = int_param(quality.get("review_count"), 0, 0, 100) + 1
    quality["review_count"] = review_count
    local_path = workflow_dir / f"quality_local_{review_count}.json"
    write_json(local_path, local_review)
    wf.setdefault("artifacts", {})["quality_local"] = str(local_path)

    reviewer = str(wf.get("reviewer_modul_id") or wf.get("normalizer_modul_id") or wf.get("target_modul_id") or config.get("default_reviewer_modul_id") or "").strip()
    if not reviewer:
        merged = merge_local_review({"pass": True, "score": 100, "decision": "pass", "blocking_issues": [], "warnings": []}, local_review)
        if review_passes(wf, config, merged):
            wf.setdefault("events", []).append(event("quality_passed", "Lokaler Quality-Check bestanden; kein Reviewer konfiguriert."))
            proceed_after_assets_approved(wf, config, assets=assets)
        else:
            mark_failed(wf, "Quality Gate blockiert ohne Reviewer: " + review_summary(merged))
            wf["stage"] = "review_failed"
            save_synthesis(wf, config, status="review_failed", assets=assets)
        return

    review_task = enqueue_llm_task(
        config,
        reviewer,
        review_prompt(wf, assets, local_review),
        created_by="workflow_trigger",
        timeout_s=cfg_int(config, "default_task_timeout_s", 3600, 60, 14400),
        back_route=wf.get("options", {}).get("chat_route") or None,
        parent_id=wf.get("parent_task_id") or None,
        workflow_id=wf.get("id"),
        workflow_stage="review_assets",
    )
    wf.setdefault("tasks", {})["review_assets"] = review_task
    wf["stage"] = "review_assets"
    wf["status"] = "running"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("quality_review_started", f"Quality Review gestartet ({reason or 'assets'}): {review_task}"))


def advance_review_assets(wf: dict[str, Any], config: dict[str, Any]) -> None:
    task_id = wf.get("tasks", {}).get("review_assets")
    task = load_task(config, task_id)
    if not task:
        mark_failed(wf, f"Quality-Review-Task fehlt: {task_id}")
        return
    if task["status"] in {"erstellt", "gestartet"}:
        wf["status"] = "waiting"
        wf["updated_at"] = now_iso()
        return
    if task["status"] != "success":
        if restart_review_with_fallback(wf, config, task.get("result") or ""):
            return
        mark_failed(wf, f"Quality Review endete mit {task['status']}: {truncate(task.get('result') or '', 600)}")
        return

    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    assets = read_json_file(artifacts.get("video_assets"), {})
    if not isinstance(assets, dict):
        mark_failed(wf, "Quality Review kann nicht auswerten: video_assets.json fehlt oder ist kaputt.")
        return

    quality = quality_state(wf)
    review_count = int_param(quality.get("review_count"), 1, 1, 100)
    raw = task.get("result") or ""
    workflow_dir = Path(wf["workflow_dir"])
    raw_path = workflow_dir / f"quality_review_raw_{review_count}.txt"
    write_text(raw_path, raw)
    wf.setdefault("artifacts", {})["quality_review_raw"] = str(raw_path)

    review = parse_review(raw)
    if not review:
        mark_failed(wf, "Quality Review lieferte kein parsebares VIDEO_REVIEW_JSON.")
        return

    local_review = local_asset_review(wf, assets, config)
    review = merge_local_review(review, local_review)
    review_path = workflow_dir / "quality_review.json"
    write_json(review_path, review)
    wf.setdefault("artifacts", {})["quality_review"] = str(review_path)
    quality["last_score"] = review.get("score")
    quality["last_decision"] = review.get("decision")
    save_synthesis(wf, config, status="quality_reviewed", assets=assets)

    if review_passes(wf, config, review):
        wf.setdefault("events", []).append(event("quality_passed", f"Quality Gate bestanden: score={review.get('score')} decision={review.get('decision')}"))
        proceed_after_assets_approved(wf, config, assets=assets)
        return

    repairs_used = int_param(quality.get("review_repair_count"), 0, 0, 100)
    max_repairs = review_repair_budget(wf, config)
    auto_repair = bool_param(wf.get("options", {}).get("quality_auto_repair"), cfg_bool(config, "quality_auto_repair", True))
    if auto_repair and repairs_used < max_repairs:
        start_repair_task(wf, config, assets, review, scope="review")
        return

    # Reparatur-Budget erschoepft. Statt GAR KEIN Video zu produzieren (Pipeline lief
    # bisher in review_failed und lieferte nichts): wenn der Reviewer kein hartes
    # "reject" sieht und der Score brauchbar ist, die beste Version best-effort
    # rendern. Aber NICHT automatisch hochladen — ein noch nicht perfektes Video soll
    # nicht ungeprueft public gehen. (Shorts sind opt-in/post-render und duerfen den
    # Haupt-Render ohnehin nie blockieren.)
    decision = str(review.get("decision") or "").strip().lower()
    score = int_param(review.get("score"), 0, 0, 100)
    proceed_after = bool_param(wf.get("options", {}).get("quality_proceed_after_repairs"), cfg_bool(config, "quality_proceed_after_repairs", True))
    proceed_min = int_param(wf.get("options", {}).get("quality_proceed_min_score"), cfg_int(config, "quality_proceed_min_score", 70), 0, 100)
    if proceed_after and decision != "reject" and score >= proceed_min:
        # Guter Score (>= quality_min_score, default 78) laedt auch im
        # Best-Effort hoch — Tagesvideo-Prinzip: ein 85er-Video gehoert auf
        # den Kanal, nicht auf die Festplatte. Nur die 70-77-Grauzone
        # bleibt Upload-manuell (bzw. quality_best_effort_upload=true).
        min_score = int_param(wf.get("options", {}).get("quality_min_score"), cfg_int(config, "quality_min_score", 78), 0, 100)
        if cfg_bool(config, "quality_best_effort_upload", False) or score >= min_score:
            upload_note = "Auto-Upload aktiv"
        else:
            wf.setdefault("options", {})["auto_upload"] = False
            upload_note = "Auto-Upload AUS (manuell pruefen)"
        wf.setdefault("events", []).append(event(
            "quality_proceed_best_effort",
            f"Nach {repairs_used} Reparaturen best-effort gerendert (score={score}, decision={decision}; "
            f"{upload_note}); offen: {review_summary(review)[:200]}",
        ))
        proceed_after_assets_approved(wf, config, assets=assets)
        return

    # Shorts-only-Blocker: wenn JEDER Blocker nur die Shorts betrifft, sind
    # Skript+Szenen freigabefaehig — Shorts entfernen, Hauptvideo weiter.
    issues = review.get("blocking_issues") or []
    if issues and all("short" in json.dumps(i, ensure_ascii=False).lower() for i in issues):
        assets = dict(assets)
        assets["shorts"] = []
        va = wf.get("artifacts", {}).get("video_assets")
        if va:
            try:
                write_json(Path(va), assets)
            except Exception:
                pass
        wf.setdefault("options", {})["auto_shorts"] = False
        wf.setdefault("events", []).append(event(
            "shorts_dropped_proceed",
            f"Alle Review-Blocker betreffen nur Shorts — Shorts entfernt, Hauptvideo laeuft weiter (score={score})",
        ))
        proceed_after_assets_approved(wf, config, assets=assets)
        return

    wf["stage"] = "review_failed"
    mark_failed(wf, "Quality Gate blockiert TTS/Render: " + review_summary(review))
    save_synthesis(wf, config, status="review_failed", assets=assets)


def start_repair_task(wf: dict[str, Any], config: dict[str, Any], assets: dict[str, Any], review: dict[str, Any], scope: str = "review") -> None:
    quality = quality_state(wf)
    scope_key = "factcheck" if scope == "factcheck" else "review"
    repair_count = int_param(quality.get("repair_count"), 0, 0, 100) + 1
    scope_count = int_param(quality.get(f"{scope_key}_repair_count"), 0, 0, 100) + 1
    quality["repair_count"] = repair_count
    quality[f"{scope_key}_repair_count"] = scope_count
    quality["last_repair_scope"] = scope_key
    normalizer = str(wf.get("normalizer_modul_id") or wf.get("target_modul_id") or config.get("default_normalizer_modul_id") or "").strip()
    repair_task = enqueue_llm_task(
        config,
        normalizer,
        repair_prompt(wf, assets, review),
        created_by="workflow_trigger",
        timeout_s=cfg_int(config, "default_task_timeout_s", 3600, 60, 14400),
        back_route=wf.get("options", {}).get("chat_route") or None,
        parent_id=wf.get("parent_task_id") or None,
        workflow_id=wf.get("id"),
        workflow_stage="repair_assets",
    )
    wf.setdefault("tasks", {})["repair_assets"] = repair_task
    wf["stage"] = "repair_assets"
    wf["status"] = "running"
    wf["updated_at"] = now_iso()
    label = "Faktencheck" if scope_key == "factcheck" else "Quality"
    wf.setdefault("events", []).append(event(
        f"{scope_key}_repair_started",
        f"Automatische {label}-Reparatur {scope_count} gestartet: {repair_task} (gesamt {repair_count})",
    ))


def advance_repair_assets(wf: dict[str, Any], config: dict[str, Any]) -> None:
    task_id = wf.get("tasks", {}).get("repair_assets")
    task = load_task(config, task_id)
    if not task:
        mark_failed(wf, f"Repair-Task fehlt: {task_id}")
        return
    if task["status"] in {"erstellt", "gestartet"}:
        wf["status"] = "waiting"
        wf["updated_at"] = now_iso()
        return
    if task["status"] != "success":
        if restart_repair_with_fallback(wf, config, task.get("result") or ""):
            return
        mark_failed(wf, f"Script-Reparatur endete mit {task['status']}: {truncate(task.get('result') or '', 600)}")
        return

    quality = quality_state(wf)
    repair_count = int_param(quality.get("repair_count"), 1, 1, 100)
    raw = task.get("result") or ""
    workflow_dir = Path(wf["workflow_dir"])
    repair_raw_path = workflow_dir / f"normalized_repair_{repair_count}.txt"
    write_text(repair_raw_path, raw)
    wf.setdefault("artifacts", {})["normalized_repair_raw"] = str(repair_raw_path)

    assets = parse_assets(raw)
    if not assets:
        mark_failed(wf, "Script-Reparatur lieferte kein parsebares VIDEO_ASSETS_JSON.")
        return
    assets = normalize_assets(assets, wf)
    assets_path = workflow_dir / "video_assets.json"
    scenes_path = workflow_dir / "scenes.json"
    script_path = workflow_dir / "script.txt"
    write_json(assets_path, assets)
    write_json(scenes_path, {"title": assets["title"], "scenes": assets["scenes"]})
    write_text(script_path, assets["voice_script"])
    wf.setdefault("artifacts", {}).update(
        {
            "video_assets": str(assets_path),
            "scenes_json": str(scenes_path),
            "script": str(script_path),
        }
    )
    save_synthesis(wf, config, status="assets_repaired", assets=assets)
    continue_after_assets_ready(wf, config, assets, reason=f"repair_{repair_count}")


def advance_synthesize_audio(wf: dict[str, Any], config: dict[str, Any]) -> None:
    task_id = wf.get("tasks", {}).get("synthesize_audio")
    task = load_task(config, task_id)
    if not task:
        mark_failed(wf, f"TTS-Task fehlt: {task_id}")
        return
    if task["status"] in {"erstellt", "gestartet"}:
        wf["status"] = "waiting"
        wf["updated_at"] = now_iso()
        return
    if task["status"] != "success":
        if can_use_silent_audio(wf, config):
            wf.setdefault("events", []).append(event("warning", f"TTS endete mit {task['status']}; explizit erlaubter Silent-Fallback wird genutzt."))
            start_render_task(wf, config, audio_path="", allow_silent=True)
            return
        mark_failed(wf, f"TTS endete mit {task['status']}: {truncate(task.get('result') or '', 600)}")
        return

    result = parse_jsonish_result(task.get("result") or "")
    audio_path = str(result.get("audio_path") or extract_path(task.get("result") or "", ".mp3") or "")
    if not audio_path:
        mark_failed(wf, "TTS war erfolgreich, aber kein audio_path im Ergebnis gefunden.")
        return
    path = Path(audio_path)
    if not path.exists() or path.stat().st_size <= 0:
        mark_failed(wf, f"TTS-Audio fehlt oder ist leer: {audio_path}")
        return
    wf.setdefault("artifacts", {})["audio"] = audio_path
    if result.get("duration_s"):
        wf.setdefault("artifacts", {})["audio_duration_s"] = result.get("duration_s")
    save_synthesis(wf, config, status="audio_ready")
    start_render_task(wf, config, audio_path=audio_path)


def should_generate_tts(wf: dict[str, Any], config: dict[str, Any]) -> bool:
    options = wf.get("options") or {}
    provider = str(options.get("tts_provider") or config.get("default_tts_provider") or "xai").strip().lower()
    tts_modul = str(wf.get("tts_modul_id") or config.get("default_tts_modul_id") or "").strip()
    return bool(tts_modul) and provider not in {"", "off", "none", "false"} and bool_param(options.get("require_tts"), cfg_bool(config, "require_tts", True))


def can_use_silent_audio(wf: dict[str, Any], config: dict[str, Any]) -> bool:
    options = wf.get("options") or {}
    return bool(options.get("preview")) or bool_param(options.get("allow_silent_audio"), cfg_bool(config, "allow_silent_audio", False))


def start_tts_task(wf: dict[str, Any], config: dict[str, Any], assets: dict[str, Any], script_path: Path) -> None:
    options = wf.get("options") or {}
    out_dir = Path(options.get("render_out_dir") or default_render_output_dir(config) / wf["id"])
    params = {
        "text": assets["voice_script"],
        "out_dir": str(out_dir),
        "filename": "narration.mp3",
        "provider": options.get("tts_provider") or config.get("default_tts_provider") or "xai",
        # voice NUR wenn explizit konfiguriert: ein Provider-fremder Name
        # (z.B. xai-"ara" an MiniMax) laesst den Provider fehlschlagen und
        # erzwingt den Fallback — genau so lief das Video vom 07.07. mit
        # Piper statt MiniMax raus.
        **({"voice": v} if (v := (options.get("tts_voice") or config.get("default_tts_voice") or "").strip()) else {}),
        "language": options.get("tts_language") or config.get("default_tts_language") or "de",
        "fast": bool_param(options.get("tts_fast"), cfg_bool(config, "default_tts_fast", True)),
        "timeout_s": cfg_int(config, "default_task_timeout_s", 3600, 60, 14400),
    }
    task = enqueue_direct_task(
        config,
        wf.get("tts_modul_id") or str(config.get("default_tts_modul_id") or "tts.default"),
        "tts.speak",
        [json.dumps(params, ensure_ascii=False)],
        created_by="workflow_trigger",
        timeout_s=cfg_int(config, "default_task_timeout_s", 3600, 60, 14400),
        back_route=wf.get("options", {}).get("chat_route") or None,
        parent_id=wf.get("parent_task_id") or None,
        workflow_id=wf.get("id"),
        workflow_stage="synthesize_audio",
    )
    wf.setdefault("tasks", {})["synthesize_audio"] = task
    wf["stage"] = "synthesize_audio"
    wf["status"] = "running"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("dependency_met", f"Video-Assets fertig, TTS gestartet: {task}; script={script_path}"))


def start_render_task(
    wf: dict[str, Any],
    config: dict[str, Any],
    assets: dict[str, Any] | None = None,
    audio_path: str = "",
    allow_silent: bool = False,
) -> None:
    artifacts = wf.get("artifacts") or {}
    if assets is None:
        assets = read_json_file(artifacts.get("video_assets"), {})
    if not isinstance(assets, dict):
        assets = {}
    script_path = artifacts.get("script") or ""
    scenes_path = artifacts.get("scenes_json") or ""
    if not script_path or not scenes_path:
        mark_failed(wf, "Render kann nicht starten: script oder scenes_json fehlt.")
        return
    title = str(assets.get("title") or wf.get("title") or wf.get("query") or "Map-Led Briefing")
    script = str(assets.get("voice_script") or read_text_file(script_path))
    silent_allowed = bool(allow_silent or can_use_silent_audio(wf, config))
    if not audio_path and not silent_allowed:
        mark_failed(wf, "Render gestoppt: kein echtes audio_path vorhanden. Silent-Render ist nur fuer Preview/Tests erlaubt.")
        return
    style = str(wf.get("options", {}).get("video_style") or config.get("video_style") or "infographic").strip().lower()
    render_tool = "video_pipeline.infographic_video" if style != "mapled" else "video_pipeline.briefing_video"
    params = {
        "title": title,
        "script_path": str(script_path),
        "scenes_json_path": str(scenes_path),
        "assets_json_path": str(artifacts.get("video_assets") or ""),
        "out_dir": wf.get("options", {}).get("render_out_dir") or str(default_render_output_dir(config) / wf["id"]),
        "preview": bool(wf.get("options", {}).get("preview")),
        "allow_silent_audio": silent_allowed,
        "duration_s": assets.get("duration_s") or estimate_duration(script),
        "timeout_s": cfg_int(config, "default_render_timeout_s", 3600, 60, 14400),
    }
    if audio_path:
        params["audio_path"] = audio_path
    if wf.get("options", {}).get("animate_scenes") is not None:
        params["animate_scenes"] = wf["options"]["animate_scenes"]

    render_task = enqueue_direct_task(
        config,
        wf.get("video_modul_id") or wf["target_modul_id"],
        render_tool,
        [json.dumps(params, ensure_ascii=False)],
        created_by="workflow_trigger",
        timeout_s=cfg_int(config, "default_render_timeout_s", 3600, 60, 14400),
        back_route=wf.get("options", {}).get("chat_route") or None,
        parent_id=wf.get("parent_task_id") or None,
        workflow_id=wf.get("id"),
        workflow_stage="render_video",
    )
    wf.setdefault("tasks", {})["render_video"] = render_task
    wf["stage"] = "render_video"
    wf["status"] = "running"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("dependency_met", f"Audio fertig, Render gestartet: {render_task}"))


def advance_render_video(wf: dict[str, Any], config: dict[str, Any]) -> None:
    task_id = wf.get("tasks", {}).get("render_video")
    task = load_task(config, task_id)
    if not task:
        mark_failed(wf, f"Render-Task fehlt: {task_id}")
        return
    if task["status"] in {"erstellt", "gestartet"}:
        wf["status"] = "waiting"
        wf["updated_at"] = now_iso()
        return
    if task["status"] != "success":
        mark_failed(wf, f"Render endete mit {task['status']}: {truncate(task.get('result') or '', 600)}")
        return

    render_result = parse_jsonish_result(task.get("result") or "")
    video = str(render_result.get("video") or extract_path(task.get("result") or "", ".mp4") or "")
    artifacts = wf.setdefault("artifacts", {})
    if video:
        artifacts["video"] = video
    for result_key, artifact_key in (
        ("storyboard", "storyboard"),
        ("package", "youtube_package"),
        ("scenes_json", "render_scenes_json"),
        ("script_path", "render_script"),
        ("audio_path", "render_audio"),
        ("output_dir", "render_output_dir"),
    ):
        value = str(render_result.get(result_key) or "").strip()
        if value:
            artifacts[artifact_key] = value
    save_synthesis(wf, config, status="video_rendered")
    # Auto-Upload (autonomer Schluss): nach Render direkt auf YouTube (default unlisted).
    if wf.get("options", {}).get("auto_upload") and video:
        privacy = str(wf.get("options", {}).get("upload_privacy") or "unlisted")
        up_params: dict[str, Any] = {"video_path": video, "privacy": privacy}
        thumb = Path(video).parent / "thumbnail.jpg"
        if thumb.exists():
            up_params["thumbnail_path"] = str(thumb)
        upload_task = enqueue_direct_task(
            config,
            "youtube_upload.default",
            "youtube_upload.video",
            [json.dumps(up_params, ensure_ascii=False)],
            created_by="workflow_trigger",
            timeout_s=cfg_int(config, "default_upload_timeout_s", 600, 60, 3600),
            back_route=wf.get("options", {}).get("chat_route") or None,
            parent_id=wf.get("parent_task_id") or None,
            workflow_id=wf.get("id"),
            workflow_stage="upload_video",
        )
        wf.setdefault("tasks", {})["upload_video"] = upload_task
        wf["stage"] = "upload_video"
        wf["status"] = "running"
        wf["updated_at"] = now_iso()
        wf.setdefault("events", []).append(event("dependency_met", f"Video fertig, Upload ({privacy}) gestartet: {upload_task}"))
        return
    if not wf.get("options", {}).get("auto_shorts"):
        wf["stage"] = "done"
        wf["status"] = "success"
        wf["updated_at"] = now_iso()
        wf.setdefault("events", []).append(event("completed", "Video gerendert."))
        save_synthesis(wf, config, status="done")
        return
    if not video:
        mark_failed(wf, "Render erfolgreich, aber kein video-Pfad im Ergebnis gefunden.")
        return

    params = {
        "source_video": video,
        "count": int(wf.get("options", {}).get("shorts_count") or cfg_int(config, "default_shorts_count", 30)),
        "duration_s": float(wf.get("options", {}).get("shorts_duration_s") or cfg_float(config, "default_shorts_duration_s", 45.0)),
        "out_dir": str(Path(wf.get("options", {}).get("render_out_dir") or default_render_output_dir(config) / wf["id"]) / "shorts"),
        "mode": "blur",
        "require_audio": not can_use_silent_audio(wf, config),
        "semantic": True,
        "semantic_fill": False,
        "video_assets_path": artifacts.get("video_assets") or "",
        "storyboard_path": artifacts.get("storyboard") or "",
        "timeout_s": cfg_int(config, "default_render_timeout_s", 3600, 60, 14400),
    }
    shorts_task = enqueue_direct_task(
        config,
        wf.get("video_modul_id") or wf["target_modul_id"],
        "video_pipeline.shorts_from_video",
        [json.dumps(params, ensure_ascii=False)],
        created_by="workflow_trigger",
        timeout_s=cfg_int(config, "default_render_timeout_s", 3600, 60, 14400),
        back_route=wf.get("options", {}).get("chat_route") or None,
        parent_id=wf.get("parent_task_id") or None,
        workflow_id=wf.get("id"),
        workflow_stage="make_shorts",
    )
    wf.setdefault("tasks", {})["make_shorts"] = shorts_task
    wf["stage"] = "make_shorts"
    wf["status"] = "running"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("dependency_met", f"Video fertig, Shorts gestartet: {shorts_task}"))


def advance_upload_video(wf: dict[str, Any], config: dict[str, Any]) -> None:
    task_id = wf.get("tasks", {}).get("upload_video")
    task = load_task(config, task_id)
    done_event = "Video gerendert."
    if task:
        if task["status"] in {"erstellt", "gestartet"}:
            wf["status"] = "waiting"
            wf["updated_at"] = now_iso()
            return
        if task["status"] == "success":
            res = parse_jsonish_result(task.get("result") or "")
            url = str(res.get("url") or "").strip()
            if url:
                wf.setdefault("artifacts", {})["youtube_url"] = url
            done_event = f"Hochgeladen: {url or 'ok'}"
        else:
            # Upload gescheitert — Video existiert trotzdem, WF nicht failen.
            wf.setdefault("artifacts", {})["upload_error"] = truncate(task.get("result") or "", 300)
            done_event = f"Video gerendert; Upload fehlgeschlagen: {truncate(task.get('result') or '', 120)}"
    else:
        done_event = "Video gerendert (Upload-Task fehlte)."
    wf["stage"] = "done"
    wf["status"] = "success"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("completed", done_event))
    save_synthesis(wf, config, status="done")


def advance_make_shorts(wf: dict[str, Any], config: dict[str, Any]) -> None:
    task_id = wf.get("tasks", {}).get("make_shorts")
    task = load_task(config, task_id)
    if not task:
        mark_failed(wf, f"Shorts-Task fehlt: {task_id}")
        return
    if task["status"] in {"erstellt", "gestartet"}:
        wf["status"] = "waiting"
        wf["updated_at"] = now_iso()
        return
    if task["status"] != "success":
        mark_failed(wf, f"Shorts endeten mit {task['status']}: {truncate(task.get('result') or '', 600)}")
        return
    shorts_result = parse_jsonish_result(task.get("result") or "")
    if shorts_result.get("manifest"):
        wf.setdefault("artifacts", {})["shorts_manifest"] = shorts_result["manifest"]
    if shorts_result.get("output_dir"):
        wf.setdefault("artifacts", {})["shorts_output_dir"] = shorts_result["output_dir"]
    wf["stage"] = "done"
    wf["status"] = "success"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("completed", "Video und Shorts fertig."))
    save_synthesis(wf, config, status="done")


def status(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    wanted = first_text(payload, "workflow_id", "id")
    limit = int_param(payload.get("limit"), 20, 1, 100)
    workflows = load_workflows(config, wanted, limit, include_done=True)
    return ok({"count": len(workflows), "workflows": [workflow_summary(wf, config) for wf in workflows]})


def production_health(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    window_hours = float_param(payload.get("window_hours"), cfg_float(config, "production_health_window_hours", 36.0), 1.0, 240.0)
    require_upload = bool_param(payload.get("require_upload"), cfg_bool(config, "production_health_require_upload", True))
    write = bool_param(payload.get("write"), True)
    return ok(compute_production_health(config, window_hours=window_hours, require_upload=require_upload, write=write))


def compute_production_health(config: dict[str, Any], window_hours: float, require_upload: bool, write: bool = False) -> dict[str, Any]:
    workflows = load_workflows(
        config,
        limit=200,
        include_done=True,
        include_failed=True,
        failed_max_age_hours=window_hours,
    )
    recent = [wf for wf in workflows if workflow_age_hours(wf, workflow_json_path(wf, config)) <= window_hours]
    recent = sorted(recent, key=lambda wf: workflow_age_hours(wf, workflow_json_path(wf, config)))
    active = sorted([wf for wf in recent if wf.get("status") in {"running", "waiting"}], key=lambda wf: workflow_age_hours(wf, workflow_json_path(wf, config)))
    failed = sorted([wf for wf in recent if wf.get("status") == "failed"], key=lambda wf: workflow_age_hours(wf, workflow_json_path(wf, config)))
    uploaded = sorted([wf for wf in recent if str((wf.get("artifacts") or {}).get("youtube_url") or "").strip()], key=lambda wf: workflow_age_hours(wf, workflow_json_path(wf, config)))
    rendered = sorted([wf for wf in recent if str((wf.get("artifacts") or {}).get("video") or "").strip()], key=lambda wf: workflow_age_hours(wf, workflow_json_path(wf, config)))
    latest = recent[0] if recent else None
    latest_artifacts = latest.get("artifacts") if isinstance(latest, dict) and isinstance(latest.get("artifacts"), dict) else {}
    latest_has_upload = bool(str((latest_artifacts or {}).get("youtube_url") or "").strip())
    latest_has_render = bool(str((latest_artifacts or {}).get("video") or "").strip())
    strict_latest = cfg_bool(config, "production_health_strict_latest", True)
    capacity_error = any(is_capacity_error(str(((wf.get("events") or [{}])[-1] if wf.get("events") else {}).get("detail") or "")) for wf in failed)

    if strict_latest and latest:
        if latest.get("status") in {"running", "waiting"}:
            state = "pending"
            message = "Neueste Produktion laeuft noch."
        elif latest.get("status") == "failed":
            state = "failed_recent"
            message = "Neueste Produktion ist fehlgeschlagen und braucht Recovery oder manuelle Pruefung."
        elif latest_has_upload:
            state = "ok"
            message = "Neueste Produktion hat eine YouTube-URL."
        elif latest_has_render and not require_upload:
            state = "ok"
            message = "Neueste Produktion wurde gerendert."
        elif latest_has_render:
            state = "rendered_no_upload"
            message = "Neueste Produktion wurde gerendert, aber nicht hochgeladen."
        else:
            state = "missing_upload" if require_upload else "missing_render"
            message = "Neueste Produktion hat keinen fertigen Upload."
    elif active:
        state = "pending"
        message = "Aktuelle Produktion laeuft noch."
    elif failed:
        state = "failed_recent"
        message = "Aktuelle Produktion ist fehlgeschlagen und braucht Recovery oder manuelle Pruefung."
    elif uploaded:
        state = "ok"
        message = "Aktuelle Produktion hat eine YouTube-URL."
    elif rendered and not require_upload:
        state = "ok"
        message = "Aktuelle Produktion wurde gerendert."
    elif rendered:
        state = "rendered_no_upload"
        message = "Aktuelle Produktion wurde gerendert, aber nicht hochgeladen."
    else:
        state = "missing_upload" if require_upload else "missing_render"
        message = "Im Produktionsfenster wurde kein fertiges Video gefunden."

    health = {
        "state": state,
        "ok": state == "ok",
        "message": message,
        "checked_at": now_iso(),
        "window_hours": window_hours,
        "require_upload": require_upload,
        "strict_latest": strict_latest,
        "capacity_error": capacity_error,
        "counts": {
            "recent": len(recent),
            "active": len(active),
            "failed": len(failed),
            "rendered": len(rendered),
            "uploaded": len(uploaded),
        },
        "latest_workflow": workflow_health_summary(latest, config) if latest else None,
        "latest_uploaded": workflow_health_summary(uploaded[0], config) if uploaded else None,
        "recent_failed": [workflow_health_summary(wf, config) for wf in failed[:5]],
        "cron_state": load_cron_state(config, ["content_planner.default", "workflow_trigger.cron"]),
    }
    if write:
        write_json(production_health_path(config), health)
    return health


def workflow_json_path(wf: dict[str, Any], config: dict[str, Any]) -> Path:
    wf_dir = str(wf.get("workflow_dir") or "").strip()
    if wf_dir:
        return Path(wf_dir) / "workflow.json"
    return workflows_dir(config) / str(wf.get("id") or "") / "workflow.json"


def workflow_health_summary(wf: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    events = wf.get("events") if isinstance(wf.get("events"), list) else []
    return {
        "workflow_id": str(wf.get("id") or Path(str(wf.get("workflow_dir") or "")).name or ""),
        "status": wf.get("status"),
        "stage": wf.get("stage"),
        "title": wf.get("title"),
        "updated_at": wf.get("updated_at"),
        "age_hours": workflow_age_hours(wf, workflow_json_path(wf, config)),
        "youtube_url": artifacts.get("youtube_url"),
        "video": artifacts.get("video"),
        "last_event": events[-1] if events else None,
    }


def production_health_path(config: dict[str, Any]) -> Path:
    return workflows_dir(config).parent / "production_health.json"


def load_cron_state(config: dict[str, Any], moduls: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        with sqlite3.connect(f"file:{tasks_db(config)}?mode=ro", uri=True, timeout=5) as conn:
            for modul in moduls:
                row = conn.execute("SELECT last_fire_minute FROM cron_state WHERE modul=?1", (modul,)).fetchone()
                out[modul] = row[0] if row else None
    except Exception as exc:
        out["error"] = str(exc)[:200]
    return out


def cancel(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    workflow_id = first_text(payload, "workflow_id", "id")
    if not workflow_id:
        return fail("workflow_id fehlt.")
    wf = load_workflow(config, workflow_id)
    if not wf:
        return fail(f"Workflow nicht gefunden: {workflow_id}")
    wf["status"] = "cancelled"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("cancelled", first_text(payload, "reason") or "cancelled"))
    save_workflow(wf, config)
    return ok({"workflow_id": workflow_id, "status": "cancelled"})


def synthesis_list(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    limit = int_param(payload.get("limit"), 20, 1, 200)
    crawl_id = first_text(payload, "crawl_id")
    query = first_text(payload, "query", "q").casefold()
    index = load_synthesis_index(config)
    items = index.get("items") if isinstance(index, dict) else []
    if not isinstance(items, list):
        items = []
    filtered = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if crawl_id and item.get("crawl_id") != crawl_id:
            continue
        if query and query not in str(item.get("query") or "").casefold() and query not in str(item.get("title") or "").casefold():
            continue
        filtered.append(item)
    return ok(
        {
            "count": len(filtered[:limit]),
            "total_indexed": len(items),
            "syntheses_dir": str(syntheses_dir(config)),
            "syntheses": filtered[:limit],
        }
    )


def synthesis_get(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    synthesis_id = first_text(payload, "synthesis_id", "id")
    workflow_id = first_text(payload, "workflow_id")
    crawl_id = first_text(payload, "crawl_id")

    if workflow_id and not synthesis_id:
        wf = load_workflow(config, workflow_id)
        if not wf:
            return fail(f"Workflow nicht gefunden: {workflow_id}")
        synthesis_path = save_synthesis(wf, config, status=str(wf.get("stage") or wf.get("status") or "unknown"))
        save_workflow(wf, config)
        synthesis_id = synthesis_path.parent.name

    if not synthesis_id:
        for item in load_synthesis_index(config).get("items", []):
            if not isinstance(item, dict):
                continue
            if crawl_id and item.get("crawl_id") != crawl_id:
                continue
            workflow_id_item = first_text(payload, "workflow_id")
            if workflow_id_item and item.get("workflow_id") != workflow_id_item:
                continue
            synthesis_id = str(item.get("synthesis_id") or "")
            if synthesis_id:
                break

    if not synthesis_id:
        return fail("synthesis_id, workflow_id oder crawl_id fehlt bzw. nicht gefunden.")

    path = syntheses_dir(config) / safe_filename(synthesis_id) / "synthesis.json"
    if not path.exists():
        return fail(f"Synthese nicht gefunden: {synthesis_id}")
    try:
        return ok(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        return fail(f"Synthese konnte nicht gelesen werden: {exc}")


def help_text() -> dict[str, Any]:
    return {
        "concept": "Ein Workflow ist eine kleine Dependency-Maschine: jeder Tick prueft Bedingungen und startet erst dann den naechsten Task.",
        "deepdive_video": [
            "1. LLM-Task erstellt DeepDive-Report mit deepdive.crawl/pack/blocks und allen relevanten Quellenmodulen.",
            "2. Trigger wartet auf Task success.",
            "3. LLM-Task normalisiert den Report zu VIDEO_ASSETS_JSON: gesprochenes Skript, typisierte Infografik-Szenen (stat/bars/people/figures/timeline/quote/compare/map), Shorts-Hooks.",
            "4. Faktencheck extrahiert harte Zahlen-/Absolut-Claims und sucht schnelle Web-Belege. Blocker stoppen vor TTS.",
            "5. Quality Gate prueft danach Quellenhygiene, Ton und Query-Abdeckung vor jeder teuren Produktion.",
            "6. Faktencheck- und Quality-Review-Probleme haben getrennte Repair-Budgets; echte Review-Blocker koennen auch nach Faktencheck-Reparaturen noch repariert werden.",
            "7. Erst nach Faktencheck/Review-Pass startet tts.speak fuer echte Voice.",
            "8. Erst wenn audio_path existiert, startet der Render (video_style: infographic -> video_pipeline.infographic_video, mapled -> video_pipeline.briefing_video).",
            "9. Optional startet der Trigger danach youtube_upload.video, wenn auto_upload=true gesetzt ist.",
            "10. Optional startet der Trigger danach video_pipeline.shorts_from_video; stumme Shorts werden standardmaessig blockiert.",
            "11. Die Synthese wird als eigenes Objekt unter syntheses/<synthesis_id>/synthesis.json gespeichert.",
        ],
        "video_from_report": [
            "Wenn bereits ein DeepDive/crawl_id/report existiert, direkt video_from_report nutzen.",
            "Bei report_task_id wartet der Trigger, falls der Task noch laeuft.",
            "Bei crawl_id fordert der Normalisierungs-Task gezielt deepdive.pack/deepdive.blocks nach.",
        ],
        "repair_video": [
            "Wenn script.txt und scenes.json bereits existieren, repair_video nutzen.",
            "Der Repair erzeugt einen neuen Workflow/Output-Ordner, startet TTS neu und rendert danach Video/Shorts neu.",
            "Alte stumme Videos werden nicht ueberschrieben.",
        ],
        "example": {
            "query": "UFO UAP Disclosure major countries",
            "title": "UAP DeepDive",
            "target_modul_id": "chat.deepseekdeepseekv4flash",
            "normalizer_modul_id": "llm_worker.video_normalizer",
            "factcheck_modul_id": "factcheck.default",
            "tts_modul_id": "tts.default",
            "preview": True,
            "auto_upload": False,
            "upload_privacy": "unlisted",
            "auto_shorts": True,
            "shorts_count": 30,
            "target_minutes": 8,
        },
        "synthesis_tools": {
            "list": 'workflow_trigger.synthesis_list({"limit":10})',
            "get": 'workflow_trigger.synthesis_get({"workflow_id":"wf-..."})',
        },
        "repair_example": {
            "workflow_id": "wf-20260517T030450Z-6316aba1",
            "auto_shorts": True,
            "shorts_count": 30,
        },
        "existing_deepdive_example": {
            "crawl_id": "dd-20260516T205524Z-cb472a08",
            "title": "Taiwan Briefing",
            "preview": True,
            "auto_render": True,
            "auto_upload": False,
        },
    }


def deepdive_report_prompt(query: str, title: str) -> str:
    return f"""ZIEL: Erstelle einen belastbaren DeepDive-Report als Vorstufe fuer ein spaeteres Video.

Thema: {query}
Arbeitstitel: {title}

Arbeite in dieser Reihenfolge:
1. Nutze fuer breite Recherche zuerst deepdive.crawl mit dem Thema. Wenn der Crawl zu gross waere, nutze deepdive.quick nur als Vorstufe und danach trotzdem gezielte DeepDive-Bausteine.
2. Nutze die verlinkten Quellenmodule sinnvoll, wenn sie zum Thema passen: RSS, Web/DuckDuckGo/Tavily/Grok, Reddit, X/X-Kommentare, YouTube-Transkripte und Job-History. Nicht blind alles aufrufen, aber die Recherche darf nicht eindimensional bleiben.
3. Warte nicht auf eine finale Meinung. Sammle Informationslage, Ereignisse, Akteure, Claims, Kausalitaeten, Widersprueche, Sprach-/Laenderkontraste und offene Leads.
4. Nach dem Crawl zwingend deepdive.pack(crawl_id) und danach deepdive.blocks(crawl_id) ausfuehren.
5. Antworte erst, wenn pack und blocks verarbeitet sind.

Output:
- crawl_id klar als eigene Zeile: crawl_id: dd-...
- kurzer Executive Stand
- Timeline
- Akteure und Laenderperspektiven
- Kausalketten und Missing Links
- Widersprueche/Unsicherheiten
- Quellenlage mit grober Qualitaet
- offene Leads fuer spaetere Subcrawls

Noch KEIN Video rendern. Dieser Task ist nur der DeepDive-Report."""


def prepare_deepdive_context(config: dict[str, Any], wf: dict[str, Any], crawl_id: str | None) -> str:
    if not crawl_id:
        return ""
    module_path = project_root(config) / "modules" / "deepdive" / "module.py"
    if not module_path.exists():
        return f"DEEPDIVE_CONTEXT_ERROR: deepdive module nicht gefunden: {module_path}"

    parts: list[str] = []
    for tool in ("deepdive.blocks", "deepdive.pack"):
        req = {
            "action": "handle_tool",
            "tool": tool,
            "params": [crawl_id],
            "config": {
                "project_root": str(project_root(config)),
                "data_dir": str(data_dir(config)),
                "pool": "DeepDive",
                "rag_pool": "DeepDive",
                "python_timeout_s": 120,
            },
        }
        try:
            proc = subprocess.run(
                [sys.executable or "python3", str(module_path)],
                input=json.dumps(req, ensure_ascii=False),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
        except Exception as exc:
            parts.append(f"{tool}: FAILED {exc}")
            continue

        output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        try:
            parsed = json.loads(output)
            ok_flag = bool(parsed.get("success"))
            data = parsed.get("data")
            if not isinstance(data, str):
                data = json.dumps(data, ensure_ascii=False, indent=2)
            parts.append(f"{tool}: {'SUCCESS' if ok_flag else 'FAILED'}\n{data}")
        except Exception:
            parts.append(f"{tool}: exit={proc.returncode}\n{truncate(output, 70000)}")

    context = "\n\n---\n\n".join(parts)
    workflow_dir = Path(wf["workflow_dir"])
    context_path = workflow_dir / "deepdive_context.txt"
    write_text(context_path, context)
    wf.setdefault("artifacts", {})["deepdive_context"] = str(context_path)
    wf.setdefault("events", []).append(event("prepared_context", f"DeepDive-Kontext vorbereitet fuer crawl_id {crawl_id}"))
    return context


def normalize_prompt(wf: dict[str, Any], report: str, prepared_context: str = "") -> str:
    crawl_id = wf.get("artifacts", {}).get("crawl_id") or extract_crawl_id(report) or ""
    preview = bool(wf.get("options", {}).get("preview"))
    allow_extra = bool(wf.get("options", {}).get("allow_extra_research"))
    target_s = float_param(wf.get("options", {}).get("target_duration_s"), 0.0, 0.0, 3600.0)
    if target_s > 0:
        # Mindestlaenge vom User: Wortbudget aus Sprechtempo (~2.3 W/s),
        # Range +35% nach oben damit der Normalizer Luft hat.
        duration = int(target_s)
        min_words = int(target_s * 2.3)
        words = f"{min_words}-{int(min_words * 1.35)} Woerter (MINDESTLAENGE {int(target_s)}s gesprochen — nicht kuerzer!)"
    else:
        words = "250-500 Woerter fuer Preview" if preview else "900-1800 Woerter fuer Longform"
        duration = 90 if preview else 360
    language = str(wf.get("options", {}).get("language") or "de").lower()
    lang_names = {"de": "Deutsch", "en": "Englisch", "es": "Spanisch", "fr": "Franzoesisch"}
    lang_rule = (
        f"- SPRACHE: Das gesamte voice_script, alle Szenentitel, subtitles, bullets und Sprechblasen-Texte auf {lang_names.get(language, language)}. Nur image_prompt bleibt englisch.\n"
        if language != "de" else ""
    )
    report_limit = 12000 if preview else 45000
    context_limit = 12000 if preview else 70000
    research_rule = (
        "- Zusatzrecherche ist erlaubt, aber nur gezielt fuer klare Luecken. Keine breite neue Recherche-Schleife.\n"
        if allow_extra
        else "- KEINE Zusatzrecherche und KEINE Toolcalls. Die DeepDive-Bausteine wurden bereits unten vorbereitet. Wenn etwas fehlt, als Unsicherheit in source_notes markieren.\n"
    )
    return f"""ZIEL: Normalisiere den fertigen DeepDive-Report in videogeeignete Produktionsdaten.

Wichtig:
- Das ist NACH dem DeepDive. Du bist kein Recherche-Agent mehr, sondern Produktions-Normalizer.
{research_rule}{lang_rule}- Wenn crawl_id vorhanden ist, nutze die vorbereiteten deepdive.pack/deepdive.blocks-Ausgaben im Prompt.
- Mache aus dem Report keinen Markdown-Artikel, sondern ein hoerbares, sachliches deutsches Sprecher-Skript.
- Voice-Skript: keine URL-Listen, keine Tabellen, keine Quellenklammern, keine Markdown-Formatierung, keine englisch/deutsch-Mischung ausser Eigennamen.
- Inhaltlich muss die Kausalitaet erhalten bleiben: Akteure, Ereignisse, Zusammenhaenge, Widersprueche, offene Unsicherheit.
- Harte Fakten duerfen nur aus offiziellen/primaeren Quellen oder klar belastbaren journalistischen Quellen kommen.
- Claims aus Kommentaren, Podcasts, Reddit, X, Substack oder unklaren Archiven duerfen NICHT als Fakt formuliert werden. Formuliere dann "laut der Analyse", "der Bericht behauptet", "unbestaetigt" oder lasse es weg.
- Whistleblower-Aussagen wie "nicht-menschliche Biologika" sind Aussagen/Behauptungen unter Eid, kein bestaetigter Fakt. Genau so sprechen.
- Wenn DeepDive-Bausteine thematisch fremde Subcrawls enthalten, ignoriere sie komplett und vermerke sie nur in source_notes als Artefakt.
- Keine Spekulationsbegriffe als Tatsachen: "NHI", "Alien", "nicht-terrestrisch", "Informationsoperation", "psychologische Akklimatisierung" nur als These/Deutung, wenn die Quelle selbst nur kommentierend ist.
	- source_notes muessen die Qualitaet jeder Hauptaussage abgrenzen: official_primary, reliable_journalism, commentary, social/unverified, stale_background.
	- Das Video ist eine INFOGRAFIK-SHOW, keine Karten-Diashow. Jede Szene bekommt ein "type" und passende Daten. Verfuegbare Typen:
	  * hook    — Einstieg/Titelszene (erste Szene, immer).
	  * stat    — EINE starke Kennzahl: stat:{{value, unit, label, max}}. Nur belegte Zahlen.
	  * bars    — Vergleich von 2-5 Groessen: bars:[{{label, value, display, color}}].
	  * people  — Anteils-Piktogramm: people:{{count, highlight, label}} (z.B. 3 von 10).
	  * figures — zwei Akteure mit Positionen: figures:{{left:{{label, say, color}}, right:{{label, say, color}}}}. say = EIN kurzer Kernsatz pro Seite.
	  * timeline— 3-6 datierte Stationen: timeline:[{{date, text}}].
	  * quote   — EIN praegnantes belegtes Zitat: quote:{{text, by}}.
	  * compare — zwei Strategien/Lager: compare:{{left:{{label, value, points:[]}}, right:{{...}}}}.
	  * map     — geografische Kausalkette: route mit 2-6 Stationen. NUR fuer wirklich raeumliche Zusammenhaenge.
	  * list    — 3-4 Kernpunkte als bullets, wenn nichts anderes passt.
	  * outro   — Schluss-Szene (letzte Szene, immer).
	- Mische die Typen: maximal EINE map-Szene pro Video, nie zwei gleiche Typen direkt hintereinander. Zahlen in stat/bars/people NUR wenn sie im Report belegt sind — sonst anderen Typ waehlen.
	- Jede Szene AUSSER map bekommt zusaetzlich "image_prompt": eine konkrete englische Bildbeschreibung des Szenenmotivs fuer einen Illustrations-Generator (Motiv + Komposition, KEIN Stil, KEIN Text im Bild, keine echten Personennamen — Politiker als "two world leaders" o.ae. umschreiben). Beispiel: "two giant hands pulling a glowing semiconductor chip in opposite directions".
	- route ist NUR bei type=map Pflicht (2+ Stationen). Pro Station genau EIN Name, z.B. "USA", "Russland", "Frankreich"; keine Slash-Kombinationen. Abstrakte Stationen wie "Global", "Europe", "Asia", "Middle East" sind erlaubt.
	- Baue das Sprecher-Skript in klaren, in sich geschlossenen Sinnpassagen auf. Jede Szene soll als eigenstaendige Passage funktionieren, damit daraus sauber geschnitten werden kann.
	- HOOK-REGEL (Retention): Die ersten 1-2 Saetze des voice_script muessen SOFORT die staerkste Zahl, den groessten Widerspruch oder die spannendste offene Frage des Themas bringen. VERBOTEN als Einstieg: "Willkommen bei...", "Heute schauen wir uns an...", "In diesem Video...", Begruessungen jeder Art.
	- TITEL-REGEL (Klickrate): Der "title" nutzt eine Neugier-Luecke nach dem Muster "[Konkretes Bild/Geheimnis]: [Superlativ oder Spannung] - [offene Frage]" (Beispiel-Muster: "Die versiegelte Kammer: Aegyptens groesstes Geheimnis seit Tutanchamun"). 50-80 Zeichen, ein konkretes Detail statt Abstraktion, KEINE Uebertreibung die das Video nicht einloest.
	- "thumbnail_text": ZUSAETZLICH 2-4 plakative Woerter fuer das Thumbnail (z.B. "Die versiegelte Kammer", "8,5 Grad zu heiss") — NICHT der Titel, sondern der kuerzeste Neugier-Trigger.
	- Shorts sind NICHT einfach Ausschnitte. Markiere nur Passagen als Short, die allein verstaendlich sind: starker Hook, ein klarer Gedanke, keine Abhaengigkeit vom vorherigen Absatz, keine langen Quellenlisten.
	- Fuer jeden Short gib eine source_scene und optional start_offset_s relativ zum Szenenanfang sowie duration_s an. Wenn eine Szene nicht short-faehig ist, erzeuge dafuer keinen Short.

Workflow:
1. Lies den DeepDive-Report und die vorbereiteten DeepDive-Bausteine unten.
2. Baue daraus ein kompaktes Video-Briefing.
3. Liefere am Ende EXAKT ein JSON zwischen VIDEO_ASSETS_JSON und END_VIDEO_ASSETS_JSON.

Erwartetes JSON:
{{
  "title": "Videotitel mit Neugier-Luecke (50-80 Zeichen)",
  "thumbnail_text": "2-4 plakative Woerter",
  "voice_script": "Hoerbares deutsches Sprecher-Skript als Fliesstext, ca. {words}.",
  "duration_s": {duration},
  "scenes": [
    {{"type": "hook", "title": "Videotitel", "subtitle": "Unterzeile", "weight": 0.7, "color": "gold"}},
    {{"type": "stat", "title": "Kapitel", "subtitle": "Einordnung", "stat": {{"value": 92, "unit": "Prozent", "label": "wofuer die Zahl steht", "max": 100}}, "bullets": ["Kontext"], "image_prompt": "a single glowing microchip on a pedestal, spotlight from above", "weight": 1.0, "color": "gold"}},
    {{"type": "figures", "title": "Akteure", "figures": {{"left": {{"label": "USA", "say": "Kernposition", "color": "blue"}}, "right": {{"label": "China", "say": "Kernposition", "color": "red"}}}}, "weight": 1.1, "color": "gold"}},
    {{"type": "map", "title": "Kausalkette", "route": ["USA", "China", "Japan"], "bullets": ["kurzer Punkt"], "weight": 1.0, "color": "gold"}},
    {{"type": "outro", "title": "Videotitel", "subtitle": "Quellen im Begleittext", "weight": 0.5, "color": "gold"}}
  ],
	  "shorts": [
	    {{
	      "hook": "kurzer Hook als erster Satz fuer den Clip",
	      "angle": "ein klarer Clip-Winkel",
	      "source_scene": "exakter Szenentitel",
	      "start_offset_s": 0,
	      "duration_s": 35,
	      "shortable": true,
	      "why": "warum diese Passage allein funktioniert"
	    }}
	  ],
  "source_notes": ["knappe Hinweise auf starke/unsichere Quellenlage"]
}}

DeepDive-Report:
<<<REPORT
{truncate(report, report_limit)}
REPORT

Vorbereitete DeepDive-Bausteine:
<<<DEEPDIVE_CONTEXT
{truncate(prepared_context, context_limit)}
DEEPDIVE_CONTEXT"""


def review_prompt(wf: dict[str, Any], assets: dict[str, Any], local_review: dict[str, Any]) -> str:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    context = read_text_file(artifacts.get("deepdive_context"))
    report = read_text_file(artifacts.get("deepdive_report"))
    min_score = int_param(wf.get("options", {}).get("quality_min_score"), 78, 0, 100)
    return f"""ZIEL: Pruefe ein fertiges Video-Skript VOR TTS/Render. Du bist die Produktionsfreigabe, nicht der Autor.

Kostenregel:
- TTS/Video kosten Geld. Blockiere lieber, wenn ein offensichtlicher Fehler, unsaubere Quellenlage oder zu harter unbelegter Ton im Skript steckt.
- Kleine Stilfragen sind Warnungen, keine Blocker.

Pruefe hart:
1. Faktische Kernfehler: Namen, Aemter, Amtszahlen, Daten, Wahlergebnisse, Orte, Reihenfolge.
2. Query-Abdeckung: Wurde das User-Ziel wirklich beantwortet oder nur ein Nebenthema?
3. Quellenhygiene: Primaerquellen/etablierte Medien duerfen Fakten tragen; Kommentar, Social, Reddit, X, Substack, schwache Agenturen nur als Meinung/Analyse.
4. Ton: Kein eigener politischer Kommentar als Fakt. Wertende Woerter wie "kopiert", "gelogen", "versagt", "gekauft" nur wenn Quelle und Einordnung klar sind.
5. TTS/YouTube-Tauglichkeit: klar hoerbar, keine Tabellen/URLs/Markdown, keine wilde Deutsch/Englisch-Mischung.
6. Shorts: Nur eigenstaendige Passagen, kein zufaelliges Abschneiden.

Lokale Vorpruefung:
<<<LOCAL_REVIEW
{json.dumps(local_review, ensure_ascii=False, indent=2)}
LOCAL_REVIEW

Video-Assets:
<<<VIDEO_ASSETS
{truncate(json.dumps(assets, ensure_ascii=False, indent=2), 50000)}
VIDEO_ASSETS

DeepDive-Report:
<<<REPORT
{truncate(report, 16000)}
REPORT

DeepDive-Bausteine / Quellen:
<<<DEEPDIVE_CONTEXT
{truncate(context, 70000)}
DEEPDIVE_CONTEXT

Antworte EXAKT mit JSON zwischen VIDEO_REVIEW_JSON und END_VIDEO_REVIEW_JSON.
Mindestscore fuer Freigabe: {min_score}.

Schema:
VIDEO_REVIEW_JSON
{{
  "pass": false,
  "score": 0,
  "decision": "pass | repair | fail",
  "summary": "knappe Produktionsbewertung",
  "blocking_issues": [
    {{"severity":"critical|high", "issue":"konkretes Problem", "evidence":"Skriptstelle/Quelle", "fix":"konkrete Reparatur"}}
  ],
  "warnings": [
    {{"severity":"medium|low", "issue":"nicht blockierend", "fix":"optional"}}
  ],
  "must_fix": ["konkrete Pflichtkorrektur"],
  "approved_if_fixed": true,
  "recommended_title": "optional besserer Titel"
}}
END_VIDEO_REVIEW_JSON"""


def repair_prompt(wf: dict[str, Any], assets: dict[str, Any], review: dict[str, Any]) -> str:
    artifacts = wf.get("artifacts") if isinstance(wf.get("artifacts"), dict) else {}
    context = read_text_file(artifacts.get("deepdive_context"))
    report = read_text_file(artifacts.get("deepdive_report"))
    directive = str(review.get("repair_directive") or "").strip()
    directive_block = f"\nWICHTIGSTE ANWEISUNG (hat Vorrang):\n- {directive}\n" if directive else ""
    return f"""ZIEL: Repariere VIDEO_ASSETS_JSON nach einem Quality-Review, damit es vor TTS/Render erneut geprueft werden kann.
{directive_block}
Regeln:
- Behebe ALLE blocking_issues und must_fix. Nutze suggested_rewrite wo vorhanden.
- Die Szenen-Struktur bleibt UNVERAENDERT: type, stat, bars, people, figures, timeline, quote, compare, route und Reihenfolge 1:1 uebernehmen. Nur voice_script-Saetze und direkt betroffene Szenen-Texte (bullets/subtitle/say/quote.text) anpassen, falls dort der beanstandete Claim steht.
- Keine neue Recherche und keine Toolcalls.
- Harte Fakten nur aus DeepDive-Kontext/Quellen; wenn unsicher, abschwaechen oder als Unsicherheit formulieren.
- Wertende politische Aussagen als Analyse/Quelle kennzeichnen oder neutralisieren.
- Falsche leicht pruefbare Fakten korrigieren.
- Hoerbares deutsches Voice-Skript, keine Tabellen/URLs/Markdown.
- Baue klare Szenen und nur wirklich short-faehige Shorts.
- Antworte am Ende EXAKT mit VIDEO_ASSETS_JSON ... END_VIDEO_ASSETS_JSON.

Quality Review:
<<<REVIEW
{truncate(json.dumps(review, ensure_ascii=False, indent=2), 22000)}
REVIEW

Aktuelle Video-Assets:
<<<VIDEO_ASSETS
{truncate(json.dumps(assets, ensure_ascii=False, indent=2), 50000)}
VIDEO_ASSETS

DeepDive-Report:
<<<REPORT
{truncate(report, 16000)}
REPORT

DeepDive-Bausteine / Quellen:
<<<DEEPDIVE_CONTEXT
{truncate(context, 70000)}
DEEPDIVE_CONTEXT

Erwartetes Format:
VIDEO_ASSETS_JSON
{{
  "title": "Videotitel mit Neugier-Luecke (50-80 Zeichen)",
  "thumbnail_text": "2-4 plakative Woerter",
  "voice_script": "korrigiertes hoerbares Skript",
  "duration_s": 360,
  "scenes": [{{"title":"Kapitel", "subtitle":"Einordnung", "route":["Germany","Europe"], "bullets":["Punkt"], "weight":1.0, "color":"gold"}}],
  "shorts": [{{"hook":"eigenstaendiger Hook", "angle":"Clip-Winkel", "source_scene":"exakter Szenentitel", "start_offset_s":0, "duration_s":35, "shortable":true, "why":"warum allein verstaendlich"}}],
  "source_notes": ["Quellen-/Unsicherheitsnotiz"]
}}
END_VIDEO_ASSETS_JSON"""


def enqueue_llm_task(
    config: dict[str, Any],
    modul: str,
    instruction: str,
    created_by: str,
    timeout_s: int,
    back_route: str | None = None,
    parent_id: str | None = None,
    workflow_id: str | None = None,
    workflow_stage: str | None = None,
) -> str:
    return insert_task(
        config,
        {
            "typ": "llm_call",
            "tool": None,
            "params": [],
            "modul": modul,
            "anweisung": instruction,
            "braucht_ki": True,
            "timeout_s": timeout_s,
            "retry": 2,
            "erstellt_von": created_by,
            "zurueck_an": back_route,
            "parent_id": parent_id,
            "workflow_id": workflow_id,
            "workflow_stage": workflow_stage,
        },
    )


def enqueue_direct_task(
    config: dict[str, Any],
    modul: str,
    tool: str,
    params: list[str],
    created_by: str,
    timeout_s: int,
    back_route: str | None = None,
    parent_id: str | None = None,
    workflow_id: str | None = None,
    workflow_stage: str | None = None,
) -> str:
    return insert_task(
        config,
        {
            "typ": "direct",
            "tool": tool,
            "params": params,
            "modul": modul,
            "anweisung": f"Tool: {tool}",
            "braucht_ki": False,
            "timeout_s": timeout_s,
            "erstellt_von": created_by,
            "zurueck_an": back_route,
            "parent_id": parent_id,
            "workflow_id": workflow_id,
            "workflow_stage": workflow_stage,
        },
    )


def insert_task(config: dict[str, Any], spec: dict[str, Any]) -> str:
    db = tasks_db(config)
    db.parent.mkdir(parents=True, exist_ok=True)
    task_id = str(uuid.uuid4())
    now = int(time.time())
    created = now_iso()
    payload = {
        "id": task_id,
        "version": 1,
        "wann": "sofort",
        "typ": spec["typ"],
        "tool": spec.get("tool"),
        "params": spec.get("params") or [],
        "modul": spec["modul"],
        "anweisung": spec.get("anweisung") or "",
        "antwort_template": None,
        "zurueck_an": spec.get("zurueck_an"),
        "braucht_ki": bool(spec.get("braucht_ki")),
        "timeout_s": int(spec.get("timeout_s") or 300),
        "retry": int(spec.get("retry") or 0),
        "retry_count": 0,
        "status": "Erstellt",
        "ergebnis": None,
        "erstellt_von": spec.get("erstellt_von") or "workflow_trigger",
        "erstellt": created,
        "gestartet": None,
        "erledigt": None,
        "history": [],
        "parent_id": spec.get("parent_id") or None,
        "cap_override": False,
    }
    if spec.get("workflow_id"):
        payload["workflow_id"] = spec.get("workflow_id")
    if spec.get("workflow_stage"):
        payload["workflow_stage"] = spec.get("workflow_stage")
    conn = sqlite3.connect(db, timeout=10)
    try:
        conn.execute(
            """
            INSERT INTO tasks (id,status,modul,payload_json,erstellt_ts,faellig_ab_ts)
            VALUES (?,?,?,?,?,?)
            """,
            (task_id, "erstellt", payload["modul"], json.dumps(payload, ensure_ascii=False), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id


def load_task(config: dict[str, Any], task_id: str | None) -> dict[str, Any] | None:
    if not task_id:
        return None
    db = tasks_db(config)
    if not db.exists():
        return None
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT id,status,modul,payload_json,erstellt_ts,gestartet_ts,erledigt_ts FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    payload = {}
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        payload = {}
    return {
        "id": row["id"],
        "status": str(row["status"] or "").lower(),
        "modul": row["modul"],
        "tool": payload.get("tool"),
        "result": payload.get("ergebnis") or "",
        "anweisung": payload.get("anweisung") or "",
        "erledigt_ts": row["erledigt_ts"],
    }


def workflow_summary(wf: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    tasks = {}
    for name, task_id in (wf.get("tasks") or {}).items():
        task = load_task(config, task_id)
        tasks[name] = {
            "task_id": task_id,
            "status": task["status"] if task else "missing",
            "tool": task.get("tool") if task else "",
        }
    return {
        "workflow_id": wf.get("id"),
        "kind": wf.get("kind"),
        "status": wf.get("status"),
        "stage": wf.get("stage"),
        "query": wf.get("query"),
        "title": wf.get("title"),
        "target_modul_id": wf.get("target_modul_id"),
        "normalizer_modul_id": wf.get("normalizer_modul_id") or wf.get("target_modul_id"),
        "reviewer_modul_id": wf.get("reviewer_modul_id") or wf.get("normalizer_modul_id") or wf.get("target_modul_id"),
        "factcheck_modul_id": wf.get("factcheck_modul_id") or "factcheck.default",
        "tts_modul_id": wf.get("tts_modul_id"),
        "video_modul_id": wf.get("video_modul_id"),
        "factcheck": wf.get("factcheck") or {},
        "quality": wf.get("quality") or {},
        "parent_task_id": wf.get("parent_task_id") or "",
        "synthesis_id": wf.get("artifacts", {}).get("synthesis_id"),
        "synthesis": wf.get("artifacts", {}).get("synthesis"),
        "tasks": tasks,
        "artifacts": wf.get("artifacts") or {},
        "last_event": (wf.get("events") or [{}])[-1],
        "updated_at": wf.get("updated_at"),
    }


def save_synthesis(wf: dict[str, Any], config: dict[str, Any], status: str = "", assets: dict[str, Any] | None = None) -> Path:
    synthesis = build_synthesis(wf, config, status=status, assets=assets)
    synthesis_id = synthesis["synthesis_id"]
    canonical_dir = syntheses_dir(config) / synthesis_id
    canonical_path = canonical_dir / "synthesis.json"
    write_json(canonical_path, synthesis)

    workflow_dir = Path(wf["workflow_dir"])
    local_path = workflow_dir / "synthesis.json"
    write_json(local_path, synthesis)

    artifacts = wf.setdefault("artifacts", {})
    artifacts["synthesis_id"] = synthesis_id
    artifacts["synthesis"] = str(canonical_path)
    artifacts["synthesis_local"] = str(local_path)
    update_synthesis_index(config, synthesis_summary(synthesis, canonical_path))
    return canonical_path


def build_synthesis(wf: dict[str, Any], config: dict[str, Any], status: str = "", assets: dict[str, Any] | None = None) -> dict[str, Any]:
    artifacts = wf.get("artifacts") or {}
    if assets is None:
        assets = read_json_file(artifacts.get("video_assets"), {})
    scenes_data = read_json_file(artifacts.get("scenes_json"), {})
    script = read_text_file(artifacts.get("script"))
    report = read_text_file(artifacts.get("deepdive_report"))
    context = read_text_file(artifacts.get("deepdive_context"))
    normalized_raw = read_text_file(artifacts.get("normalized_raw"))
    factcheck_report = read_json_file(artifacts.get("factcheck_report"), {})
    quality_review = read_json_file(artifacts.get("quality_review"), {})
    if not script and isinstance(assets, dict):
        script = str(assets.get("voice_script") or "")
    if not scenes_data and isinstance(assets, dict):
        scenes_data = {"title": assets.get("title") or wf.get("title"), "scenes": assets.get("scenes") or []}

    synthesis_id = str(artifacts.get("synthesis_id") or ("syn-" + str(wf.get("id") or uuid.uuid4().hex).removeprefix("wf-")))
    title = str((assets or {}).get("title") or wf.get("title") or wf.get("query") or "DeepDive Synthese")
    production = {
        "title": title,
        "script": script,
        "script_path": artifacts.get("script") or "",
        "voice_script": script,
        "duration_s": (assets or {}).get("duration_s"),
        "scenes": scenes_data.get("scenes") if isinstance(scenes_data, dict) else [],
        "scenes_json_path": artifacts.get("scenes_json") or "",
        "video_assets": assets if isinstance(assets, dict) else {},
        "video_assets_path": artifacts.get("video_assets") or "",
        "shorts": (assets or {}).get("shorts") if isinstance(assets, dict) else [],
        "source_notes": (assets or {}).get("source_notes") if isinstance(assets, dict) else [],
    }
    outputs = {
        "audio": artifacts.get("audio") or "",
        "audio_duration_s": artifacts.get("audio_duration_s") or "",
        "video": artifacts.get("video") or "",
        "shorts_manifest": artifacts.get("shorts_manifest") or "",
    }
    return {
        "synthesis_id": synthesis_id,
        "kind": "deepdive_video_synthesis",
        "workflow_id": wf.get("id"),
        "workflow_kind": wf.get("kind"),
        "status": status or wf.get("status") or "",
        "stage": wf.get("stage") or "",
        "query": wf.get("query") or "",
        "title": title,
        "crawl_id": artifacts.get("crawl_id") or "",
        "created_at": wf.get("created_at"),
        "updated_at": now_iso(),
        "source": {
            "deepdive_report_path": artifacts.get("deepdive_report") or "",
            "deepdive_context_path": artifacts.get("deepdive_context") or "",
            "deepdive_report_excerpt": truncate(report, 12000),
            "deepdive_context_excerpt": truncate(context, 16000),
            "normalized_raw_path": artifacts.get("normalized_raw") or "",
            "normalized_raw_excerpt": truncate(normalized_raw, 8000),
        },
        "production": production,
        "outputs": outputs,
        "factcheck": {
            "state": wf.get("factcheck") or {},
            "report": factcheck_report if isinstance(factcheck_report, dict) else {},
            "report_path": artifacts.get("factcheck_report") or "",
        },
        "quality": {
            "state": wf.get("quality") or {},
            "review": quality_review if isinstance(quality_review, dict) else {},
            "review_path": artifacts.get("quality_review") or "",
            "local_review_path": artifacts.get("quality_local") or "",
        },
        "artifacts": artifacts,
        "tasks": wf.get("tasks") or {},
        "events_tail": (wf.get("events") or [])[-30:],
    }


def synthesis_summary(synthesis: dict[str, Any], path: Path) -> dict[str, Any]:
    production = synthesis.get("production") if isinstance(synthesis.get("production"), dict) else {}
    outputs = synthesis.get("outputs") if isinstance(synthesis.get("outputs"), dict) else {}
    return {
        "synthesis_id": synthesis.get("synthesis_id"),
        "workflow_id": synthesis.get("workflow_id"),
        "crawl_id": synthesis.get("crawl_id"),
        "query": synthesis.get("query"),
        "title": synthesis.get("title"),
        "status": synthesis.get("status"),
        "stage": synthesis.get("stage"),
        "updated_at": synthesis.get("updated_at"),
        "script_path": production.get("script_path") or "",
        "video": outputs.get("video") or "",
        "synthesis_path": str(path),
    }


def update_synthesis_index(config: dict[str, Any], summary: dict[str, Any]) -> None:
    index_path = syntheses_dir(config) / "index.json"
    index = load_synthesis_index(config)
    items = index.get("items") if isinstance(index, dict) else []
    if not isinstance(items, list):
        items = []
    synthesis_id = summary.get("synthesis_id")
    items = [item for item in items if isinstance(item, dict) and item.get("synthesis_id") != synthesis_id]
    items.insert(0, summary)
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    write_json(index_path, {"updated_at": now_iso(), "items": items[:1000]})


def load_synthesis_index(config: dict[str, Any]) -> dict[str, Any]:
    path = syntheses_dir(config) / "index.json"
    if not path.exists():
        return {"updated_at": "", "items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"updated_at": "", "items": []}
    except Exception:
        return {"updated_at": "", "items": []}


def read_text_file(raw_path: Any) -> str:
    if not raw_path:
        return ""
    try:
        path = Path(str(raw_path))
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return ""


def read_json_file(raw_path: Any, default: Any) -> Any:
    text = read_text_file(raw_path)
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def resolve_existing_artifact(
    config: dict[str, Any],
    payload: dict[str, Any],
    artifacts: dict[str, Any],
    source_dir: Path,
    payload_key: str,
    artifact_key: str,
    fallback_name: str,
    required: bool = True,
) -> Path | None:
    candidates = [
        first_text(payload, payload_key, artifact_key),
        str(artifacts.get(artifact_key) or ""),
        str(source_dir / fallback_name),
    ]
    for raw in candidates:
        if not raw:
            continue
        path = resolve_path(config, raw)
        if path.exists() and path.is_file():
            return path
    return None if required else None


def parse_assets(raw: str) -> dict[str, Any] | None:
    text = raw or ""
    match = re.search(r"VIDEO_ASSETS_JSON\s*(\{.*?\})\s*END_VIDEO_ASSETS_JSON", text, re.S)
    candidates = []
    if match:
        candidates.append(match.group(1))
    candidates.append(text.strip())
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    brace = extract_balanced_json(text)
    if brace:
        try:
            data = json.loads(brace)
            if isinstance(data, dict):
                return data
        except Exception:
            return None
    return None


def parse_review(raw: str) -> dict[str, Any] | None:
    text = raw or ""
    match = re.search(r"VIDEO_REVIEW_JSON\s*(\{.*?\})\s*END_VIDEO_REVIEW_JSON", text, re.S)
    candidates = []
    if match:
        candidates.append(match.group(1))
    candidates.append(text.strip())
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return normalize_review(data)
        except Exception:
            pass
    brace = extract_balanced_json(text)
    if brace:
        try:
            data = json.loads(brace)
            if isinstance(data, dict):
                return normalize_review(data)
        except Exception:
            return None
    return None


def normalize_review(data: dict[str, Any]) -> dict[str, Any]:
    blocking = data.get("blocking_issues")
    warnings = data.get("warnings")
    must_fix = data.get("must_fix")
    if not isinstance(blocking, list):
        blocking = []
    if not isinstance(warnings, list):
        warnings = []
    if not isinstance(must_fix, list):
        must_fix = []
    decision = str(data.get("decision") or ("pass" if bool_param(data.get("pass"), False) else "repair")).strip().lower()
    if decision not in {"pass", "repair", "fail"}:
        decision = "repair"
    return {
        "pass": bool_param(data.get("pass"), False),
        "score": int_param(data.get("score"), 0, 0, 100),
        "decision": decision,
        "summary": str(data.get("summary") or "").strip()[:1200],
        "blocking_issues": [normalize_issue(item, "high") for item in blocking if isinstance(item, dict) or str(item).strip()],
        "warnings": [normalize_issue(item, "medium") for item in warnings if isinstance(item, dict) or str(item).strip()],
        "must_fix": [str(item).strip()[:500] for item in must_fix if str(item).strip()],
        "approved_if_fixed": bool_param(data.get("approved_if_fixed"), False),
        "recommended_title": str(data.get("recommended_title") or "").strip()[:160],
    }


def normalize_issue(item: Any, default_severity: str = "medium") -> dict[str, str]:
    if isinstance(item, dict):
        return {
            "severity": str(item.get("severity") or default_severity).strip().lower()[:30],
            "issue": str(item.get("issue") or item.get("problem") or item.get("text") or "").strip()[:700],
            "evidence": str(item.get("evidence") or item.get("quote") or "").strip()[:700],
            "fix": str(item.get("fix") or item.get("recommendation") or "").strip()[:700],
        }
    return {"severity": default_severity, "issue": str(item).strip()[:700], "evidence": "", "fix": ""}


def local_asset_review(wf: dict[str, Any], assets: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    _ = config
    title = str(assets.get("title") or wf.get("title") or "")
    script = str(assets.get("voice_script") or "")
    scenes = assets.get("scenes") if isinstance(assets.get("scenes"), list) else []
    shorts = assets.get("shorts") if isinstance(assets.get("shorts"), list) else []
    source_notes = assets.get("source_notes") if isinstance(assets.get("source_notes"), list) else []
    script_words = len(re.findall(r"\w+", script))
    joined = "\n".join([title, script, json.dumps(shorts, ensure_ascii=False)])
    low = joined.casefold()
    blocking: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if not script.strip():
        blocking.append({"severity": "critical", "issue": "voice_script fehlt", "evidence": "", "fix": "Skript erzeugen."})
    if script_words < 550 and not bool(wf.get("options", {}).get("preview")):
        warnings.append({"severity": "medium", "issue": "Longform-Skript ist sehr kurz fuer einen DeepDive", "evidence": f"{script_words} Woerter", "fix": "Bei breiten Themen eher 900-1600 Woerter nutzen oder als Kurzbriefing markieren."})
    if len(scenes) < 3:
        blocking.append({"severity": "high", "issue": "Zu wenige Szenen fuer ein erklaerendes Video", "evidence": str(len(scenes)), "fix": "Mindestens 3-5 sinnvolle Szenen erzeugen."})
    if not source_notes:
        blocking.append({"severity": "high", "issue": "source_notes fehlen", "evidence": "", "fix": "Quellenqualitaet und Unsicherheiten pro Hauptaussage notieren."})
    if re.search(r"https?://|\|[-: ]*\|", script):
        blocking.append({"severity": "high", "issue": "Skript enthaelt URL-/Tabellenformat", "evidence": "URL oder Markdown-Tabelle erkannt", "fix": "Fuer TTS in gesprochenen Fliesstext umschreiben."})

    if "friedrich merz" in low and "neunte bundeskanzler" in low:
        blocking.append(
            {
                "severity": "critical",
                "issue": "Leicht pruefbarer Faktenfehler: Friedrich Merz wird als neunter Bundeskanzler bezeichnet",
                "evidence": "Skript/Titel enthaelt 'neunte Bundeskanzler'",
                "fix": "Auf 'zehnter Bundeskanzler' korrigieren oder die Ordinalzahl weglassen.",
            }
        )
    if "friedrich merz" in low and re.search(r"dreimal\s+[^.]{0,80}scheiter", low):
        blocking.append(
            {
                "severity": "high",
                "issue": "Merz-CDU-Vorsitz-Karriere wirkt faktisch unsauber formuliert",
                "evidence": "Formulierung wie 'dreimal ... scheiterte'",
                "fix": "Korrekt formulieren: Er verlor 2018 und 2021, setzte sich danach 2021/2022 beim CDU-Vorsitz durch.",
            }
        )
    if "afd" in low and "kopiert" in low:
        blocking.append(
            {
                "severity": "high",
                "issue": "Wertende These als Fakt formuliert",
                "evidence": "AfD + 'kopiert'",
                "fix": "Als Analyse/Zitat formulieren oder neutralisieren: 'uebernimmt Teile einer haerteren migrationspolitischen Rhetorik, so die Analyse ...'.",
            }
        )
    if "lüg" in low or "lueg" in low or "gelogen" in low:
        warnings.append({"severity": "medium", "issue": "Luegen-Claim erkannt", "evidence": "lueg/gelogen", "fix": "Nur mit belegtem konkretem Claim verwenden; sonst 'Widerspruch', 'unbelegt' oder 'kritisiert'."})

    source_text = "\n".join(str(item) for item in source_notes).casefold()
    if "unknown_check_needed" in source_text and "official_primary" not in source_text and "reliable_journalism" not in source_text:
        warnings.append({"severity": "medium", "issue": "Quellenlage wirkt ueberwiegend unsicher", "evidence": "source_notes enthalten unknown_check_needed ohne starke Gegengewichtung", "fix": "Primaerquellen/etablierte Medien nachziehen oder Aussagen abschwaechen."})
    if not shorts:
        warnings.append({"severity": "low", "issue": "Keine Shorts-Kandidaten", "evidence": "", "fix": "Nur relevant, wenn auto_shorts aktiv ist."})

    critical_count = len([item for item in blocking if item.get("severity") == "critical"])
    high_count = len(blocking) - critical_count
    score = max(0, 100 - 25 * critical_count - 14 * high_count - 4 * len(warnings))
    return {
        "pass": not blocking and score >= 78,
        "score": score,
        "decision": "pass" if not blocking and score >= 78 else "repair",
        "summary": "Lokale Struktur-/Faktenvorpruefung",
        "blocking_issues": blocking,
        "warnings": warnings,
    }


def merge_local_review(review: dict[str, Any], local_review: dict[str, Any]) -> dict[str, Any]:
    merged = normalize_review(review)
    local = normalize_review(local_review)
    merged["blocking_issues"] = list(merged.get("blocking_issues") or []) + list(local.get("blocking_issues") or [])
    merged["warnings"] = list(merged.get("warnings") or []) + list(local.get("warnings") or [])
    merged["score"] = min(int_param(merged.get("score"), 0, 0, 100), int_param(local.get("score"), 0, 0, 100))
    if merged["blocking_issues"] or merged["score"] < 78:
        merged["pass"] = False
        if merged.get("decision") == "pass":
            merged["decision"] = "repair"
    return merged


def review_blocking_issues(review: dict[str, Any]) -> list[dict[str, str]]:
    issues = review.get("blocking_issues") if isinstance(review.get("blocking_issues"), list) else []
    blocking = []
    for item in issues:
        issue = normalize_issue(item, default_severity="high")
        if issue.get("severity") in {"critical", "high", "blocker", "fatal"}:
            blocking.append(issue)
    return blocking


def review_passes(wf: dict[str, Any], config: dict[str, Any], review: dict[str, Any]) -> bool:
    min_score = int_param(wf.get("options", {}).get("quality_min_score"), cfg_int(config, "quality_min_score", 78), 0, 100)
    score = int_param(review.get("score"), 0, 0, 100)
    decision = str(review.get("decision") or "").strip().lower()
    return bool_param(review.get("pass"), False) and score >= min_score and decision == "pass" and not review_blocking_issues(review)


def review_summary(review: dict[str, Any]) -> str:
    parts = []
    if review.get("summary"):
        parts.append(str(review.get("summary")))
    if review.get("score") is not None:
        parts.append(f"score={review.get('score')}")
    issues = review_blocking_issues(review)
    if issues:
        parts.append("blocking=" + "; ".join((item.get("issue") or "")[:160] for item in issues[:4]))
    fixes = review.get("must_fix") if isinstance(review.get("must_fix"), list) else []
    if fixes:
        parts.append("must_fix=" + "; ".join(str(item)[:160] for item in fixes[:4]))
    return truncate(" | ".join(part for part in parts if part), 900) or "Quality Review nicht bestanden."


def normalize_assets(assets: dict[str, Any], wf: dict[str, Any]) -> dict[str, Any]:
    title = str(assets.get("title") or wf.get("title") or wf.get("query") or "DeepDive Briefing").strip()
    script = str(assets.get("voice_script") or assets.get("script") or "").strip()
    if not script:
        report_path = wf.get("artifacts", {}).get("deepdive_report")
        report = Path(report_path).read_text(encoding="utf-8", errors="replace") if report_path and Path(report_path).exists() else ""
        script = spoken_fallback(report, title)
    scenes = assets.get("scenes")
    if not isinstance(scenes, list):
        scenes = []
    normalized_scenes = []
    colors = ["gold", "blue", "teal", "red", "purple", "green"]
    typed_keys = ("type", "stat", "bars", "people", "figures", "timeline", "quote", "compare", "narration", "image_prompt", "image")
    for idx, scene in enumerate(scenes[:18], start=1):
        if not isinstance(scene, dict):
            continue
        scene_type = str(scene.get("type") or "").strip().lower()
        has_typed_data = any(scene.get(k) for k in typed_keys[1:])
        route = scene.get("route") or scene.get("countries") or []
        if isinstance(route, str):
            route = [part.strip() for part in re.split(r"[,;>]+", route) if part.strip()]
        if not isinstance(route, list):
            route = []
        # Route-Zwang NUR fuer Karten-Szenen. Vorher bekam jede Szene eine
        # fallback_route verpasst — dadurch wurde ALLES zur Weltkarte und die
        # Infografik-Typfelder gingen verloren.
        needs_route = scene_type in {"map", "figures"} or (not scene_type and not has_typed_data)
        if needs_route and len(route) < 2:
            route = fallback_route(wf.get("query") or "")
        if route:
            route = normalize_route_items(route, wf.get("query") or "")
        bullets = scene.get("bullets") or scene.get("points") or []
        if isinstance(bullets, str):
            bullets = [part.strip() for part in re.split(r"\n+|;\s*", bullets) if part.strip()]
        if not isinstance(bullets, list):
            bullets = []
        normalized: dict[str, Any] = {
            "title": str(scene.get("title") or f"Kapitel {idx}")[:80],
            "subtitle": str(scene.get("subtitle") or scene.get("summary") or "")[:160],
            "route": route[:8],
            "bullets": [str(item).strip()[:150] for item in bullets if str(item).strip()][:4],
            "weight": float_param(scene.get("weight"), 1.0, 0.05, 5.0),
            "color": str(scene.get("color") or colors[(idx - 1) % len(colors)]),
        }
        for key in typed_keys:
            if scene.get(key) is not None:
                normalized[key] = scene.get(key)
        normalized_scenes.append(normalized)
    if not normalized_scenes:
        normalized_scenes = [
            {
                "title": title[:80],
                "subtitle": "Akteure, Kausalketten und Unsicherheiten",
                "route": fallback_route(wf.get("query") or ""),
                "bullets": ["Informationslage einordnen", "Akteure verbinden", "Unsicherheiten sichtbar halten"],
                "weight": 1.0,
                "color": "gold",
            }
        ]
    return {
        "title": title,
        "voice_script": script,
        "duration_s": float_param(assets.get("duration_s"), estimate_duration(script), 12.0, 1800.0),
        "scenes": normalized_scenes,
        "shorts": normalize_short_specs(assets.get("shorts"), normalized_scenes),
        "source_notes": assets.get("source_notes") if isinstance(assets.get("source_notes"), list) else [],
    }


def normalize_short_specs(raw_shorts: Any, scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(raw_shorts, list):
        return []
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_shorts[:60]):
        if not isinstance(item, dict):
            continue
        if not bool_param(item.get("shortable"), True):
            continue
        hook = str(item.get("hook") or item.get("title") or "").strip()
        angle = str(item.get("angle") or item.get("summary") or "").strip()
        source_scene = str(item.get("source_scene") or item.get("scene") or "").strip()
        if not source_scene and idx < len(scenes):
            source_scene = str(scenes[idx].get("title") or "").strip()
        if not hook and not angle and not source_scene:
            continue
        spec: dict[str, Any] = {
            "hook": hook[:180],
            "angle": angle[:220],
            "source_scene": source_scene[:100],
            "start_offset_s": optional_number(item.get("start_offset_s") or item.get("offset_s"), 0.0, 0.0, 900.0),
            "shortable": True,
        }
        duration = optional_number(item.get("duration_s") or item.get("duration"), None, 18.0, 60.0)
        if duration is not None:
            spec["duration_s"] = duration
        why = str(item.get("why") or "").strip()
        if why:
            spec["why"] = why[:260]
        normalized.append(spec)
        if len(normalized) >= 30:
            break
    return normalized


def optional_number(value: Any, default: float | None = None, min_value: float | None = None, max_value: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        out = float(value)
    except Exception:
        return default
    if min_value is not None:
        out = max(min_value, out)
    if max_value is not None:
        out = min(max_value, out)
    return round(out, 3)


def fallback_route(query: str) -> list[str]:
    low = (query or "").casefold()
    route = []
    mapping = [
        ("usa", "USA"),
        ("uap", "USA"),
        ("ufo", "USA"),
        ("china", "China"),
        ("japan", "Japan"),
        ("taiwan", "Taiwan"),
        ("iran", "Iran"),
        ("russia", "Russia"),
        ("ukraine", "Ukraine"),
        ("germany", "Germany"),
        ("europe", "Germany"),
        ("eu", "Germany"),
        ("israel", "Israel"),
        ("india", "India"),
    ]
    for needle, country in mapping:
        if needle in low and country not in route:
            route.append(country)
    if len(route) < 2:
        route.extend(country for country in ["USA", "China", "Germany"] if country not in route)
    return route[:4]


def normalize_route_items(route: list[Any], query: str = "") -> list[str]:
    normalized: list[str] = []
    for item in route:
        value = canonical_route_item(str(item or "").strip())
        if value and value not in normalized:
            normalized.append(value)
    if len(normalized) < 2:
        for item in fallback_route(query):
            if item not in normalized:
                normalized.append(item)
            if len(normalized) >= 2:
                break
    return normalized


def canonical_route_item(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if not text:
        return ""
    low = text.casefold()
    special = [
        (("internet", "web", "online", "world wide web"), "Global"),
        (("congress", "washington", "pentagon", "department of war", "department of defense"), "USA"),
        (("pazifik", "pacific"), "Pacific"),
        (("naher osten", "middle east"), "Middle East"),
        (("europa", "europe", "eu "), "Europe"),
        (("asien", "asia"), "Asia"),
        (("lateinamerika", "latin america"), "Latin America"),
    ]
    for needles, replacement in special:
        if any(needle in low for needle in needles):
            return replacement
    if "/" in text or "|" in text:
        parts = [part.strip() for part in re.split(r"[/|]+", text) if part.strip()]
        for part in parts:
            candidate = canonical_route_item(part)
            if candidate:
                return candidate
    return text


def spoken_fallback(report: str, title: str) -> str:
    cleaned = re.sub(r"https?://\S+", "", report or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return f"{title}. {cleaned[:6000]}"


def estimate_duration(text: str) -> float:
    words = len(re.findall(r"\w+", text or ""))
    return max(20.0, min(1800.0, round(words / 2.35 + 6.0, 2)))


def extract_balanced_json(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return ""


def parse_jsonish_result(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_path(text: str, suffix: str) -> str:
    match = re.search(r"(/[^ \n\r\t\"']+" + re.escape(suffix) + r")", text or "")
    return match.group(1) if match else ""


def extract_crawl_id(text: str) -> str:
    match = re.search(r"\bdd-\d{8}T\d{6}Z-[a-f0-9]{8}\b", text or "")
    return match.group(0) if match else ""


def mark_failed(wf: dict[str, Any], reason: str) -> None:
    wf["status"] = "failed"
    wf["updated_at"] = now_iso()
    wf.setdefault("events", []).append(event("failed", reason))


def workflow_age_hours(wf: dict[str, Any], path: Path) -> float:
    raw = str(wf.get("updated_at") or wf.get("created_at") or "").strip()
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)
        except Exception:
            pass
    try:
        return max(0.0, (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 3600.0)
    except Exception:
        return 999999.0


def load_workflows(
    config: dict[str, Any],
    wanted: str = "",
    limit: int = 20,
    include_done: bool = False,
    include_failed: bool = False,
    failed_max_age_hours: float | None = None,
) -> list[dict[str, Any]]:
    if wanted:
        wf = load_workflow(config, wanted)
        return [wf] if wf else []
    roots = [workflows_dir(config)]
    legacy = resolve_path(config, "agent-data/workflows")
    if legacy not in roots:
        roots.append(legacy)
    paths: list[Path] = []
    for root in roots:
        if root.exists():
            paths.extend(root.glob("wf-*/workflow.json"))
    active_items = []
    failed_items = []
    done_items = []
    seen_ids: set[str] = set()
    for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            wf = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        wf_id = str(wf.get("id") or path.parent.name)
        if wf_id in seen_ids:
            continue
        seen_ids.add(wf_id)
        status = wf.get("status")
        if status in {"running", "waiting"}:
            active_items.append(wf)
        elif include_failed and status == "failed" and (
            failed_max_age_hours is None or workflow_age_hours(wf, path) <= failed_max_age_hours
        ):
            failed_items.append(wf)
        elif include_done:
            done_items.append(wf)
    if include_done:
        return (active_items + failed_items + done_items)[:limit]
    return (active_items + failed_items)[:limit]


def load_workflow(config: dict[str, Any], workflow_id: str) -> dict[str, Any] | None:
    path = workflows_dir(config) / workflow_id / "workflow.json"
    if not path.exists():
        legacy = resolve_path(config, "agent-data/workflows") / workflow_id / "workflow.json"
        if legacy.exists():
            path = legacy
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_workflow(wf: dict[str, Any], config: dict[str, Any]) -> None:
    wf["updated_at"] = now_iso()
    # Zurueck in das Verzeichnis schreiben, in dem der Workflow lebt — sonst
    # wandert er beim ersten Save in workflows_dir(config) und die alte Kopie
    # bleibt als running-Leiche liegen.
    wf_dir = str(wf.get("workflow_dir") or "").strip()
    base = Path(wf_dir) if wf_dir else (workflows_dir(config) / wf["id"])
    write_json(base / "workflow.json", wf)


def event(kind: str, detail: str) -> dict[str, Any]:
    return {"ts": now_iso(), "kind": kind, "detail": detail}


def workflows_dir(config: dict[str, Any]) -> Path:
    raw = str(config.get("default_output_dir") or "agent-data/workflows")
    return resolve_path(config, raw)


def default_render_output_dir(config: dict[str, Any]) -> Path:
    raw = str(config.get("default_render_output_dir") or "agent-data/video_pipeline")
    return resolve_path(config, raw)


def syntheses_dir(config: dict[str, Any]) -> Path:
    raw = str(config.get("default_synthesis_dir") or "").strip()
    if raw:
        return resolve_path(config, raw)
    workflow_root = workflows_dir(config)
    if workflow_root.name == "workflows":
        return workflow_root.parent / "syntheses"
    return data_dir(config) / "syntheses"


def data_dir(config: dict[str, Any]) -> Path:
    raw = str(config.get("data_dir") or "agent-data")
    return resolve_path(config, raw)


def project_root(config: dict[str, Any]) -> Path:
    return Path(str(config.get("project_root") or ".")).expanduser().resolve()


def tasks_db(config: dict[str, Any]) -> Path:
    return data_dir(config) / "tasks.db"


def resolve_path(config: dict[str, Any], raw: str) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    project_root = Path(str(config.get("project_root") or ".")).resolve()
    return (project_root / path).resolve()


def parse_payload(params: Any) -> dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    if isinstance(params, str):
        return parse_jsonish(params)
    if isinstance(params, list):
        if not params:
            return {}
        if len(params) == 1:
            item = params[0]
            if isinstance(item, dict):
                return item
            if isinstance(item, str):
                return parse_jsonish(item)
        out = {}
        for idx, item in enumerate(params):
            if isinstance(item, dict):
                out.update(item)
            elif isinstance(item, str) and ":" in item:
                key, val = item.split(":", 1)
                out[key.strip()] = val.strip()
            else:
                out[f"arg{idx + 1}"] = item
        return out
    return {}


def parse_jsonish(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    for prefix in ("query_json:", "json:", "payload:", "params:"):
        if text.lower().startswith(prefix):
            text = text.split(":", 1)[1].strip()
            break
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        return data if isinstance(data, dict) else {"items": data}
    return {"query": text}


def first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def list_param(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, tuple):
        raw_items = list(value)
    else:
        raw_items = re.split(r"[,;\s]+", str(value))
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def inherited_parent_task_id(payload: dict[str, Any], config: dict[str, Any]) -> str:
    raw = (
        first_text(payload, "parent_task_id", "workflow_parent_task_id")
        or first_text(config, "task_root_id", "task_id")
    )
    raw = raw.split("#", 1)[0].strip()
    if re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", raw):
        return raw
    return ""


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return cleaned[:160] or "synthesis"


def cfg_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    return bool_param(config.get(key), default)


def cfg_int(config: dict[str, Any], key: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    return int_param(config.get(key), default, min_value, max_value)


def cfg_float(config: dict[str, Any], key: str, default: float) -> float:
    return float_param(config.get(key), default)


def bool_param(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on", "y"}


def int_param(value: Any, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        out = int(float(value))
    except Exception:
        out = int(default)
    if min_value is not None:
        out = max(min_value, out)
    if max_value is not None:
        out = min(max_value, out)
    return out


def float_param(value: Any, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        out = float(value)
    except Exception:
        out = float(default)
    if min_value is not None:
        out = max(min_value, out)
    if max_value is not None:
        out = min(max_value, out)
    return out


def truncate(text: str, limit: int) -> str:
    if len(text or "") <= limit:
        return text or ""
    return (text or "")[: max(0, limit - 20)] + "\n...[gekuerzt]"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def ok(data: Any) -> dict[str, Any]:
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, indent=2)
    return {"success": True, "data": data}


def fail(data: Any) -> dict[str, Any]:
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, indent=2)
    return {"success": False, "data": data}


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
