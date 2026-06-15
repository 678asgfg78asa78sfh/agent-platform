"""Coding orchestrator: goal-driven code work with snapshots and local memory."""

import difflib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


MODULE = {
    "name": "coding",
    "description": "Zielgefuehrtes Coding: Zielvertrag, Code-Kontext, Coding-Memory, Snapshot, Patch, Verify und Review.",
    "version": "1.0",
    "settings": {
        "workspace_root": {"type": "string", "label": "Workspace Root leer=Home", "default": ""},
        "allow_project_root": {"type": "bool", "label": "Projektroot als Workspace erlauben", "default": False},
        "max_search_results": {"type": "number", "label": "Max Suchtreffer", "default": 20},
        "max_read_lines": {"type": "number", "label": "Max Zeilen pro Read", "default": 220},
        "max_file_size_kb": {"type": "number", "label": "Max Read-Dateigroesse KB", "default": 768},
        "snapshot_max_files": {"type": "number", "label": "Max Dateien pro Snapshot", "default": 700},
        "snapshot_max_file_kb": {"type": "number", "label": "Max Snapshot-Dateigroesse KB", "default": 768},
        "snapshot_before_patch": {"type": "bool", "label": "Vor Patch Snapshot erstellen", "default": True},
        "command_timeout_s": {"type": "number", "label": "Run Timeout Sekunden", "default": 120},
        "allowed_run_commands": {
            "type": "list",
            "label": "Erlaubte Verify-Kommandos",
            "default": [
                "python3 -m py_compile",
                "python3 -m pytest",
                "pytest",
                "cargo test",
                "cargo check",
                "npm test",
                "npm run build",
                "go test",
            ],
        },
        "ignored_dirs": {
            "type": "list",
            "label": "Ignorierte Ordner",
            "default": [
                ".git",
                ".coding",
                "target",
                "node_modules",
                "__pycache__",
                ".pytest_cache",
                ".venv",
                "venv",
                "dist",
                "build",
            ],
        },
    },
    "tools": [
        {
            "name": "coding.start",
            "description": "Startet Coding-Task. Param Suchtext oder JSON: {request, mode, acceptance_criteria, workspace}.",
            "params": ["request_json"],
        },
        {
            "name": "coding.status",
            "description": "Zeigt Task-Status, Snapshots, Plan, Verify-Ergebnisse und Workspace-Status.",
            "params": ["task_id"],
        },
        {
            "name": "coding.plan",
            "description": "Speichert/aktualisiert den Ausfuehrungsplan. JSON: {task_id, steps:[{step,status}]}.",
            "params": ["plan_json"],
        },
        {
            "name": "coding.context",
            "description": "Sucht Code-Kontext plus Coding-Memory. Param JSON: {query, task_id?, path?, max_results?}.",
            "params": ["query_json"],
        },
        {
            "name": "coding.read",
            "description": "Liest gezielte Dateien/Zeilen/Symbole. JSON: {files:[\"path:1-80\"], symbols:[{path,symbol}]}.",
            "params": ["query_json"],
        },
        {
            "name": "coding.symbol_index",
            "description": "Extrahiert Funktionen/Klassen/Symbole mit Zeilenbereichen. JSON: {path, store?}.",
            "params": ["query_json"],
        },
        {
            "name": "coding.memory_search",
            "description": "Sucht im Coding-Memory nach Architektur-, Symbol- und Task-Notizen.",
            "params": ["query"],
        },
        {
            "name": "coding.memory_note",
            "description": "Speichert eine Coding-Notiz. JSON: {type,path,symbol,summary,details,task_id?,scope?}.",
            "params": ["note_json"],
        },
        {
            "name": "coding.memory_gc",
            "description": "Markiert stale Memory-Notizen und entfernt alte temporaere Task-Notizen.",
            "params": [],
        },
        {
            "name": "coding.snapshot",
            "description": "Snapshot create/list/restore. JSON: {action:create|list|restore, task_id?, snapshot_id?}.",
            "params": ["query_json"],
        },
        {
            "name": "coding.patch",
            "description": "Wendet unified diff nach dry-run an. Param ist Diff oder JSON {task_id,diff}.",
            "params": ["diff_or_json"],
        },
        {
            "name": "coding.run",
            "description": "Fuehrt erlaubte Verify-Kommandos/Profile im Workspace aus. JSON: {task_id?, profile?, cmd?, files?}.",
            "params": ["query_json"],
        },
        {
            "name": "coding.review",
            "description": "Heuristischer Reviewer: Diff, Verify, Zielvertrag, Risiken, naechste Schritte.",
            "params": ["task_id"],
        },
        {
            "name": "coding.check_goal",
            "description": "Vergleicht gespeichertes Ziel mit aktuellem Stand und Verify-Ergebnissen.",
            "params": ["task_id"],
        },
        {
            "name": "coding.finish",
            "description": "Erzeugt Abschlussbericht aus Ziel, Aenderungen, Verify und Review.",
            "params": ["task_id"],
        },
    ],
}


TEXT_EXTENSIONS = {
    "",
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".lua",
    ".md",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}

BLOCKED_EXTENSIONS = {
    ".7z",
    ".bin",
    ".dll",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpg",
    ".jpeg",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".tar",
    ".webp",
    ".zip",
}


def handle_tool(tool_name, params, config):
    try:
        if tool_name == "coding.start":
            return _start(params, config)
        if tool_name == "coding.status":
            return _status(_first(params, "task_id"), config)
        if tool_name == "coding.plan":
            return _plan(params, config)
        if tool_name == "coding.context":
            return _context(params, config)
        if tool_name == "coding.read":
            return _read(params, config)
        if tool_name == "coding.symbol_index":
            return _symbol_index_tool(params, config)
        if tool_name == "coding.memory_search":
            return _memory_search_tool(_first(params, "query"), config)
        if tool_name == "coding.memory_note":
            return _memory_note_tool(params, config)
        if tool_name == "coding.memory_gc":
            return _memory_gc_tool(config)
        if tool_name == "coding.snapshot":
            return _snapshot_tool(params, config)
        if tool_name == "coding.patch":
            return _patch(params, config)
        if tool_name == "coding.run":
            return _run(params, config)
        if tool_name == "coding.review":
            return _review(_first(params, "task_id"), config)
        if tool_name == "coding.check_goal":
            return _check_goal(_first(params, "task_id"), config)
        if tool_name == "coding.finish":
            return _finish(_first(params, "task_id"), config)
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"Coding Fehler: {exc}")


def _start(params, config):
    payload = parse_payload(params, "request")
    request = first_text(payload, "request", "goal", "ziel", "query", "text")
    if not request:
        return fail("Kein Ziel angegeben. Beispiel: coding.start({\"request\":\"Bug in RSS-Modul fixen\"})")

    root, err = workspace_root(config, payload)
    if err:
        return fail(err)

    task_id = payload.get("task_id") or f"code-{int(time.time())}-{os.getpid()}"
    mode = (payload.get("mode") or detect_mode(request)).strip().lower()
    if mode not in {"scaffold", "feature", "bughunt"}:
        mode = detect_mode(request)
    criteria = normalize_list(payload.get("acceptance_criteria") or payload.get("kriterien"))
    if not criteria:
        criteria = default_criteria(mode)
    non_goals = normalize_list(payload.get("non_goals") or payload.get("nicht_ziele"))

    task = {
        "id": task_id,
        "mode": mode,
        "goal": request,
        "acceptance_criteria": [{"text": c, "status": "open"} for c in criteria],
        "non_goals": non_goals,
        "workspace": root,
        "status": "planned",
        "plan": [],
        "events": [],
        "snapshots": [],
        "verify": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    add_event(task, "goal_defined", f"mode={mode}; goal={request}")
    save_task(task, config)

    snap_msg = ""
    if cfg_bool(payload.get("snapshot", config.get("snapshot_on_start", True)), True):
        snap = create_snapshot(config, root, task_id, "initial")
        task["snapshots"].append(snap["id"])
        add_event(task, "snapshot", snap["id"])
        save_task(task, config)
        snap_msg = f"\nsnapshot: {snap['id']} files={snap['files']} skipped={snap['skipped']}"

    return ok(
        "\n".join(
            [
                "CODING_TASK_STARTED",
                f"task_id: {task_id}",
                f"mode: {mode}",
                f"workspace: {root}",
                f"goal: {request}",
                "acceptance_criteria:",
                *[f"- [ ] {c['text']}" for c in task["acceptance_criteria"]],
                *(["non_goals:"] + [f"- {x}" for x in non_goals] if non_goals else []),
                snap_msg.strip(),
                "next: coding.context({\"task_id\":\"%s\",\"query\":\"%s\"})" % (task_id, safe_oneline(request)[:120]),
            ]
        ).strip()
    )


def _status(task_id, config):
    task = load_task(task_id, config)
    if not task:
        return fail(f"Task nicht gefunden: {task_id}")
    root = task.get("workspace") or workspace_root(config)[0]
    lines = [
        "CODING_STATUS",
        f"task_id: {task['id']}",
        f"mode: {task.get('mode', '')}",
        f"status: {task.get('status', '')}",
        f"workspace: {root}",
        f"goal: {task.get('goal', '')}",
        "",
        "criteria:",
    ]
    for crit in task.get("acceptance_criteria") or []:
        mark = "x" if crit.get("status") == "done" else " "
        lines.append(f"- [{mark}] {crit.get('text', '')}")
    if task.get("plan"):
        lines.append("")
        lines.append("plan:")
        for idx, item in enumerate(task["plan"], 1):
            lines.append(f"{idx}. [{item.get('status', 'pending')}] {item.get('step', '')}")
    if task.get("snapshots"):
        lines.append("")
        lines.append("snapshots: " + ", ".join(task["snapshots"][-6:]))
    if task.get("verify"):
        lines.append("")
        lines.append("verify:")
        for item in task["verify"][-5:]:
            lines.append(f"- {item.get('status')} exit={item.get('exit_code')} cmd={item.get('cmd')}")
    status = workspace_status(root)
    if status:
        lines.append("")
        lines.append(status)
    return ok("\n".join(lines))


def _plan(params, config):
    payload = parse_payload(params, "plan")
    task = load_task(payload.get("task_id"), config) if payload.get("task_id") else latest_task(config)
    if not task:
        return fail("Task nicht gefunden. Starte zuerst coding.start(...).")

    steps_raw = payload.get("steps") or payload.get("plan") or []
    if isinstance(steps_raw, str):
        steps = [{"step": line.strip(" -\t"), "status": "pending"} for line in steps_raw.splitlines() if line.strip(" -\t")]
    elif isinstance(steps_raw, list):
        steps = []
        for item in steps_raw:
            if isinstance(item, dict):
                step = str(item.get("step") or item.get("text") or "").strip()
                status = str(item.get("status") or "pending").strip()
            else:
                step = str(item).strip()
                status = "pending"
            if step:
                steps.append({"step": step, "status": status})
    else:
        steps = []

    if not steps and not payload.get("status"):
        return fail("Keine Plan-Schritte angegeben. Beispiel: coding.plan({\"task_id\":\"...\",\"steps\":[\"Dateien finden\",\"Patch bauen\",\"Tests laufen\"]})")

    if steps:
        task["plan"] = steps
        add_event(task, "plan", f"steps={len(steps)}")
    if payload.get("status"):
        task["status"] = str(payload.get("status"))
        add_event(task, "status", task["status"])
    save_task(task, config)

    lines = ["CODING_PLAN_SAVED", f"task_id: {task['id']}", f"status: {task.get('status', '')}", "plan:"]
    for idx, item in enumerate(task.get("plan") or [], 1):
        lines.append(f"{idx}. [{item.get('status', 'pending')}] {item.get('step', '')}")
    return ok("\n".join(lines))


def _context(params, config):
    payload = parse_payload(params)
    query = first_text(payload, "query", "q", "goal", "request", "text")
    if not query:
        return fail("Kein Query. Beispiel: coding.context({\"query\":\"tool permission parser\"})")
    task = load_task(payload.get("task_id"), config) if payload.get("task_id") else None
    root, err = workspace_root(config, payload, task)
    if err:
        return fail(err)

    max_results = cfg_int(payload.get("max_results", config.get("max_search_results", 20)), 20, 1, 80)
    path_filter = first_text(payload, "path", "dir")
    search_root = root
    if path_filter:
        search_root, err = resolve_path(path_filter, config, root, must_exist=True)
        if err:
            return fail(err)
        if os.path.isfile(search_root):
            search_root = os.path.dirname(search_root)

    memory = search_memory(config, query, limit=min(8, max_results))
    matches = rg_search(search_root, query, config, max_results)
    files = rank_candidate_files(root, query, config, limit=12)

    lines = [
        "CODING_CONTEXT",
        f"workspace: {root}",
        f"query: {query}",
        "",
        "MEMORY_MATCHES",
    ]
    if memory:
        for idx, row in enumerate(memory, 1):
            stale = " stale" if row.get("stale") else ""
            loc = format_loc(row)
            lines.append(f"{idx}. score={row.get('score', 0)}{stale} {loc}")
            lines.append(f"   {row.get('summary', '')}")
    else:
        lines.append("Keine Memory-Treffer.")

    lines.append("")
    lines.append("CODE_MATCHES")
    if matches:
        for idx, hit in enumerate(matches, 1):
            lines.append(f"{idx}. {relpath(hit['path'], root)}:{hit['line']}: {hit['text']}")
    else:
        lines.append("Keine rg-Treffer.")

    lines.append("")
    lines.append("CANDIDATE_FILES")
    for idx, item in enumerate(files, 1):
        lines.append(f"{idx}. score={item['score']} {relpath(item['path'], root)}")
    if task:
        add_event(task, "context", f"query={query}; matches={len(matches)}; memory={len(memory)}")
        save_task(task, config)
    return ok("\n".join(lines))


def _read(params, config):
    payload = parse_payload(params)
    task = load_task(payload.get("task_id"), config) if payload.get("task_id") else None
    root, err = workspace_root(config, payload, task)
    if err:
        return fail(err)

    requests = []
    for item in payload.get("files") or []:
        if isinstance(item, dict):
            requests.append((str(item.get("path", "")), item.get("start"), item.get("end"), None))
        else:
            path, start, end = parse_path_range(str(item))
            requests.append((path, start, end, None))
    for item in payload.get("symbols") or []:
        if isinstance(item, dict):
            requests.append((str(item.get("path", "")), None, None, str(item.get("symbol", ""))))
    if not requests:
        raw = first_text(payload, "path", "file", "query")
        if not raw and params:
            raw = str(params[0])
        if raw:
            path, start, end = parse_path_range(raw)
            requests.append((path, start, end, None))
    if not requests:
        return fail("Keine Dateien angegeben. Beispiel: coding.read({\"files\":[\"src/tools.rs:1-80\"]})")

    max_lines = cfg_int(payload.get("max_lines", config.get("max_read_lines", 220)), 220, 20, 800)
    blocks = ["CODING_READ", f"workspace: {root}"]
    for path, start, end, symbol in requests:
        abs_path, err = resolve_path(path, config, root, must_exist=True)
        if err:
            blocks.append(f"\nFAILED {path}: {err}")
            continue
        if symbol:
            symbols = extract_symbols(abs_path)
            found = next((s for s in symbols if s["name"] == symbol), None)
            if not found:
                blocks.append(f"\nFAILED {path}#{symbol}: Symbol nicht gefunden.")
                continue
            start, end = found["start"], found["end"]
        block = read_range(abs_path, root, start, end, max_lines, config)
        blocks.append(block)
        maybe_note_symbol(config, abs_path, root, symbol, start, end)
    if task:
        add_event(task, "read", f"requests={len(requests)}")
        save_task(task, config)
    return ok("\n".join(blocks))


def _symbol_index_tool(params, config):
    payload = parse_payload(params)
    path = first_text(payload, "path", "file")
    if not path:
        return fail("Kein Pfad. Beispiel: coding.symbol_index({\"path\":\"src/tools.rs\",\"store\":true})")
    root, err = workspace_root(config, payload)
    if err:
        return fail(err)
    abs_path, err = resolve_path(path, config, root, must_exist=True)
    if err:
        return fail(err)
    symbols = extract_symbols(abs_path)
    store = cfg_bool(payload.get("store"), False)
    if store:
        for sym in symbols:
            upsert_memory(
                config,
                {
                    "type": "symbol_summary",
                    "path": relpath(abs_path, root),
                    "symbol": sym["name"],
                    "kind": sym["kind"],
                    "line_start": sym["start"],
                    "line_end": sym["end"],
                    "summary": sym["summary"],
                    "scope": "project",
                    "file_hash": file_hash(abs_path),
                },
            )
    lines = [
        "CODING_SYMBOL_INDEX",
        f"file: {relpath(abs_path, root)}",
        f"symbols: {len(symbols)}",
    ]
    if store:
        lines.append("stored: true")
    for sym in symbols[:120]:
        lines.append(f"- {sym['kind']} {sym['name']} lines={sym['start']}-{sym['end']} sig={sym['signature']}")
    if len(symbols) > 120:
        lines.append(f"... {len(symbols) - 120} weitere Symbole gekuerzt")
    return ok("\n".join(lines))


def _memory_search_tool(query, config):
    query = (query or "").strip()
    if not query:
        return fail("Kein Query. Beispiel: coding.memory_search(tool permission parser)")
    rows = search_memory(config, query, limit=20)
    lines = ["CODING_MEMORY_SEARCH", f"query: {query}", f"results: {len(rows)}"]
    for idx, row in enumerate(rows, 1):
        stale = " stale" if row.get("stale") else ""
        lines.append(f"{idx}. score={row.get('score', 0)}{stale} {format_loc(row)}")
        lines.append(f"   type={row.get('type')} scope={row.get('scope')} updated={row.get('updated_at')}")
        lines.append(f"   {row.get('summary', '')}")
        details = row.get("details", "")
        if details:
            lines.append(f"   details: {truncate(details, 260)}")
    if not rows:
        lines.append("Keine Treffer.")
    return ok("\n".join(lines))


def _memory_note_tool(params, config):
    payload = parse_payload(params)
    summary = first_text(payload, "summary", "notiz", "note", "text")
    if not summary:
        return fail("summary fehlt. Beispiel: coding.memory_note({\"type\":\"project\",\"summary\":\"...\"})")
    root, _ = workspace_root(config, payload)
    note = dict(payload)
    note["summary"] = summary
    note.setdefault("type", "note")
    note.setdefault("scope", "project")
    path = note.get("path")
    if path:
        abs_path, err = resolve_path(str(path), config, root, must_exist=False)
        if err:
            return fail(err)
        note["path"] = relpath(abs_path, root)
        if os.path.exists(abs_path) and os.path.isfile(abs_path):
            note["file_hash"] = file_hash(abs_path)
    note_id = upsert_memory(config, note)
    return ok(f"CODING_MEMORY_NOTE\nid: {note_id}\nsummary: {summary}")


def _memory_gc_tool(config):
    db = memory_db(config)
    root, _ = workspace_root(config)
    now = int(time.time())
    stale = 0
    deleted = 0
    con = sqlite3.connect(db)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM notes").fetchall()
        for row in rows:
            path = row["path"]
            fh = row["file_hash"]
            if path and fh:
                abs_path = os.path.join(root, path)
                if not os.path.exists(abs_path) or file_hash(abs_path) != fh:
                    con.execute("UPDATE notes SET stale=1, updated_at=? WHERE id=?", (now_iso(), row["id"]))
                    stale += 1
            if row["scope"] == "task" and row["updated_ts"] < now - 14 * 86400:
                con.execute("DELETE FROM notes WHERE id=?", (row["id"],))
                deleted += 1
        con.commit()
    finally:
        con.close()
    return ok(f"CODING_MEMORY_GC\nstale_marked: {stale}\ntask_notes_deleted: {deleted}")


def _snapshot_tool(params, config):
    payload = parse_payload(params)
    action = (payload.get("action") or "create").strip().lower()
    task = load_task(payload.get("task_id"), config) if payload.get("task_id") else None
    root, err = workspace_root(config, payload, task)
    if err:
        return fail(err)
    if action == "create":
        snap = create_snapshot(config, root, payload.get("task_id"), payload.get("label") or "manual")
        if task:
            task["snapshots"].append(snap["id"])
            add_event(task, "snapshot", snap["id"])
            save_task(task, config)
        return ok(format_snapshot(snap))
    if action == "list":
        snaps = list_snapshots(config)
        lines = ["CODING_SNAPSHOTS", f"count: {len(snaps)}"]
        for snap in snaps[-20:]:
            lines.append(f"- {snap['id']} task={snap.get('task_id','')} files={snap.get('files',0)} created={snap.get('created_at','')}")
        return ok("\n".join(lines))
    if action == "restore":
        snap_id = first_text(payload, "snapshot_id", "id")
        if not snap_id:
            return fail("snapshot_id fehlt.")
        restored = restore_snapshot(config, snap_id, root)
        return ok(restored)
    return fail(f"Unbekannte Snapshot-Action: {action}")


def _patch(params, config):
    payload = parse_payload(params, default_key="diff")
    diff_text = payload.get("diff") if isinstance(payload, dict) else ""
    if not diff_text and params:
        diff_text = str(params[0])
    diff_text = strip_code_fence(str(diff_text or ""))
    if not diff_text.strip():
        return fail("Kein Diff. Nutze unified diff mit ---/+++ und @@ Hunks.")
    if not diff_text.endswith("\n"):
        diff_text += "\n"
    if "--- " not in diff_text or "+++ " not in diff_text or "@@" not in diff_text:
        return fail("Diff sieht nicht wie unified diff aus (---/+++/@@ fehlen).")

    task = load_task(payload.get("task_id"), config) if isinstance(payload, dict) and payload.get("task_id") else None
    root, err = workspace_root(config, payload if isinstance(payload, dict) else {}, task)
    if err:
        return fail(err)
    touched, err = touched_paths_from_diff(diff_text, config, root)
    if err:
        return fail(err)
    if not touched:
        return fail("Keine Zielpfade im Diff gefunden.")

    snap_id = ""
    if cfg_bool(config.get("snapshot_before_patch"), True):
        snap = create_snapshot(config, root, task["id"] if task else "", "before_patch")
        snap_id = snap["id"]
        if task:
            task["snapshots"].append(snap_id)
            add_event(task, "snapshot", snap_id)

    tmp_dir = coding_dir(config, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="patch-", suffix=".diff", dir=tmp_dir, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(diff_text)
        check = run_proc(["git", "apply", "--check", "--whitespace=nowarn", tmp_path], root, timeout=30)
        if check["exit_code"] != 0:
            return fail(format_run("CODING_PATCH_DRY_RUN_FAILED", check))
        apply = run_proc(["git", "apply", "--whitespace=nowarn", tmp_path], root, timeout=30)
        if apply["exit_code"] != 0:
            return fail(format_run("CODING_PATCH_FAILED", apply))
    finally:
        try:
            os.remove(tmp_path)
        except Exception as _e:
            sys.stderr.write("[coding] uebersprungener Fehler: %r\n" % (_e,))

    diff_check = git_diff_check(root)
    if task:
        add_event(task, "patch", f"files={', '.join(relpath(p, root) for p in touched)}")
        task["status"] = "editing"
        save_task(task, config)

    lines = [
        "CODING_PATCH_APPLIED",
        f"workspace: {root}",
        f"files: {', '.join(relpath(p, root) for p in touched)}",
    ]
    if snap_id:
        lines.append(f"snapshot_before_patch: {snap_id}")
    lines.append(diff_check)
    return ok("\n".join(lines))


def _run(params, config):
    payload = parse_payload(params)
    task = load_task(payload.get("task_id"), config) if payload.get("task_id") else None
    root, err = workspace_root(config, payload, task)
    if err:
        return fail(err)
    cmd = command_from_payload(payload)
    if not cmd:
        return fail("Kein cmd/profile. Beispiel: coding.run({\"profile\":\"cargo_check\"})")
    allowed, reason = command_allowed(cmd, config)
    if not allowed:
        return fail(reason)
    timeout = cfg_int(payload.get("timeout_s", config.get("command_timeout_s", 120)), 120, 5, 900)
    result = run_proc(shlex.split(cmd), root, timeout=timeout)
    status = "success" if result["exit_code"] == 0 else "failed"
    if task:
        task["verify"].append(
            {
                "cmd": cmd,
                "status": status,
                "exit_code": result["exit_code"],
                "stdout": truncate(result["stdout"], 2000),
                "stderr": truncate(result["stderr"], 2000),
                "ts": now_iso(),
            }
        )
        add_event(task, "verify", f"{status}: {cmd}")
        task["status"] = "verifying"
        save_task(task, config)
    return {"success": result["exit_code"] == 0, "data": format_run("CODING_RUN", result, cmd=cmd)}


def _review(task_id, config):
    task = load_task(task_id, config) if task_id else latest_task(config)
    if not task:
        return fail("Task nicht gefunden.")
    root = task.get("workspace") or workspace_root(config)[0]
    status = workspace_status(root)
    diff_check = git_diff_check(root)
    last_verify = (task.get("verify") or [])[-1] if task.get("verify") else None
    risks = []
    if not task.get("snapshots"):
        risks.append("kein Snapshot im Task")
    if not last_verify:
        risks.append("kein Verify-Lauf dokumentiert")
    elif last_verify.get("status") != "success":
        risks.append("letzter Verify-Lauf ist nicht gruen")
    if "No changes" in status:
        risks.append("keine Aenderungen im Workspace sichtbar")

    lines = [
        "CODING_REVIEW",
        f"task_id: {task['id']}",
        f"goal: {task.get('goal', '')}",
        "",
        "workspace:",
        status,
        "",
        "diff_check:",
        diff_check,
        "",
        "verify:",
    ]
    if last_verify:
        lines.append(f"- {last_verify.get('status')} exit={last_verify.get('exit_code')} cmd={last_verify.get('cmd')}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("risks:")
    if risks:
        lines.extend(f"- {r}" for r in risks)
    else:
        lines.append("- keine offensichtlichen Review-Risiken im Heuristik-Check")
    lines.append("")
    lines.append("recommendation: " + ("needs_work" if risks else "ready_for_goal_check"))
    return ok("\n".join(lines))


def _check_goal(task_id, config):
    task = load_task(task_id, config) if task_id else latest_task(config)
    if not task:
        return fail("Task nicht gefunden.")
    last_verify = (task.get("verify") or [])[-1] if task.get("verify") else None
    root = task.get("workspace") or workspace_root(config)[0]
    changed = workspace_has_changes(root)

    checks = []
    checks.append(("snapshot_exists", bool(task.get("snapshots")), "vor Aenderungen muss ein Snapshot existieren"))
    checks.append(("workspace_changed", changed or task.get("mode") == "scaffold", "es sollten relevante Aenderungen sichtbar sein"))
    checks.append(("verify_success", bool(last_verify and last_verify.get("status") == "success"), "letzter Verify-Lauf muss gruen sein"))
    if task.get("mode") == "bughunt":
        checks.append(("bughunt_evidence", any("repro" in e.get("kind", "") or "bug" in e.get("detail", "").lower() for e in task.get("events", [])), "Bug-Hunting braucht Repro-/Ursachen-Notiz"))

    solved = all(ok_ for _, ok_, _ in checks)
    if solved:
        for crit in task.get("acceptance_criteria") or []:
            if crit.get("status") != "done":
                crit["status"] = "done"
                crit["evidence"] = "coding.check_goal heuristic checks passed"
    task["status"] = "solved" if solved else "needs_work"
    add_event(task, "goal_check", "solved" if solved else "needs_work")
    save_task(task, config)

    lines = [
        "CODING_GOAL_CHECK",
        f"task_id: {task['id']}",
        f"mode: {task.get('mode')}",
        f"goal: {task.get('goal')}",
        f"status: {task['status']}",
        "",
        "checks:",
    ]
    for name, ok_, detail in checks:
        lines.append(f"- [{'x' if ok_ else ' '}] {name}: {detail}")
    lines.append("")
    lines.append("acceptance_criteria:")
    for crit in task.get("acceptance_criteria") or []:
        lines.append(f"- [{crit.get('status', 'open')}] {crit.get('text', '')}")
    return ok("\n".join(lines))


def _finish(task_id, config):
    task = load_task(task_id, config) if task_id else latest_task(config)
    if not task:
        return fail("Task nicht gefunden.")
    review = _review(task["id"], config)["data"]
    goal = _check_goal(task["id"], config)["data"]
    task = load_task(task["id"], config) or task
    root = task.get("workspace") or workspace_root(config)[0]
    lines = [
        "CODING_FINISH",
        f"task_id: {task['id']}",
        f"status: {task.get('status')}",
        f"workspace: {root}",
        f"goal: {task.get('goal')}",
        "",
        "snapshots: " + (", ".join(task.get("snapshots") or []) or "none"),
        "",
        goal,
        "",
        review,
    ]
    return ok("\n".join(lines))


def parse_payload(params, default_key="query"):
    if isinstance(params, dict):
        return dict(params)
    if not params:
        return {}
    raw = params[0]
    if isinstance(raw, dict):
        return dict(raw)
    raw = str(raw or "").strip()
    if raw.startswith("{") and raw.endswith("}"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception as _e:
            sys.stderr.write("[coding] uebersprungener Fehler: %r\n" % (_e,))
    return {default_key: raw}


def _first(params, key):
    if isinstance(params, dict):
        return str(params.get(key) or "").strip()
    if not params:
        return ""
    raw = str(params[0] or "").strip()
    if raw.startswith("{") and raw.endswith("}"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return str(data.get(key) or data.get("id") or "").strip()
        except Exception as _e:
            sys.stderr.write("[coding] uebersprungener Fehler: %r\n" % (_e,))
    return raw


def first_text(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def ok(data):
    return {"success": True, "data": data}


def fail(data):
    return {"success": False, "data": data}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cfg_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on"}


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


def normalize_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value)
    parts = []
    for line in text.replace(";", "\n").splitlines():
        line = line.strip(" -\t")
        if line:
            parts.append(line)
    if len(parts) <= 1 and "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
    return parts


def detect_mode(request):
    text = request.lower()
    if any(x in text for x in ["bug", "fehler", "failed", "fail", "exception", "traceback", "kaputt", "fix"]):
        return "bughunt"
    if any(x in text for x in ["grundgeruest", "grundgerüst", "scaffold", "neues projekt", "aufbauen", "bootstrap"]):
        return "scaffold"
    return "feature"


def default_criteria(mode):
    if mode == "scaffold":
        return [
            "Projektstruktur ist angelegt",
            "Start-/Build-/Smoke-Befehl funktioniert",
            "Minimaler Nutzungs- oder README-Hinweis existiert",
        ]
    if mode == "bughunt":
        return [
            "Fehlerursache ist nachvollziehbar isoliert",
            "Fix ist kontrolliert eingebaut",
            "Regressionstest oder konkreter Verify-Check ist gruen",
        ]
    return [
        "Feature ist im bestehenden Projekt integriert",
        "Relevanter Test/Smoke-Check ist gruen",
        "Keine offensichtliche Regression im Review",
    ]


def home_dir(config):
    home = str(config.get("home_dir") or "").strip()
    if home:
        return os.path.realpath(os.path.abspath(home))
    return os.path.realpath(os.path.abspath(os.path.join(os.getcwd(), "agent-data", "home", "coding")))


def workspace_root(config, payload=None, task=None):
    payload = payload or {}
    if task and task.get("workspace"):
        raw = task["workspace"]
    else:
        raw = payload.get("workspace") or payload.get("workspace_root") or config.get("workspace_root") or ""
    home = home_dir(config)
    project = str(config.get("project_root") or "").strip()
    if raw in {"$PROJECT_ROOT", "{project_root}", "project_root"} and cfg_bool(config.get("allow_project_root"), False):
        raw = project
    if not raw:
        raw = home
    if not os.path.isabs(str(raw)):
        raw = os.path.join(home, str(raw))
    root = os.path.realpath(os.path.abspath(str(raw)))
    allowed = [home]
    if cfg_bool(config.get("allow_project_root"), False) and project:
        allowed.append(os.path.realpath(os.path.abspath(project)))
    if not any(is_within(root, a) for a in allowed):
        return None, f"Workspace nicht erlaubt: {root}. Erlaubt: {allowed}"
    os.makedirs(root, exist_ok=True)
    return root, None


def coding_dir(config, name):
    root = home_dir(config)
    path = os.path.join(root, ".coding", name)
    os.makedirs(path, exist_ok=True)
    return path


def resolve_path(path, config, root, must_exist=False):
    if not path:
        return None, "Pfad fehlt"
    p = str(path).strip().strip('"').strip("'")
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    if os.path.isabs(p):
        abs_path = os.path.realpath(os.path.abspath(p))
    else:
        abs_path = os.path.realpath(os.path.abspath(os.path.join(root, p)))
    if not is_within(abs_path, root):
        return None, f"Pfad ausserhalb Workspace: {path}"
    if os.path.basename(abs_path) == ".coding" or "/.coding/" in abs_path.replace("\\", "/"):
        return None, "Interner .coding Ordner darf nicht bearbeitet werden"
    ext = os.path.splitext(abs_path)[1].lower()
    if ext in BLOCKED_EXTENSIONS:
        return None, f"Blockierte Dateiendung: {ext}"
    if must_exist and not os.path.exists(abs_path):
        return None, f"Pfad existiert nicht: {path}"
    return abs_path, None


def is_within(path, root):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except Exception:
        return False


def relpath(path, root):
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except Exception:
        return str(path)


def safe_oneline(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def truncate(text, limit):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def add_event(task, kind, detail):
    task.setdefault("events", []).append({"ts": now_iso(), "kind": kind, "detail": str(detail)})
    task["updated_at"] = now_iso()


def task_path(config, task_id):
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(task_id or ""))
    return os.path.join(coding_dir(config, "tasks"), safe + ".json")


def save_task(task, config):
    path = task_path(config, task["id"])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(task, fh, indent=2, ensure_ascii=True)


def load_task(task_id, config):
    if not task_id:
        return None
    path = task_path(config, task_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def latest_task(config):
    d = coding_dir(config, "tasks")
    files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")]
    if not files:
        return None
    latest = max(files, key=lambda p: os.path.getmtime(p))
    with open(latest, "r", encoding="utf-8") as fh:
        return json.load(fh)


def ignored_dirs(config):
    vals = config.get("ignored_dirs") or MODULE["settings"]["ignored_dirs"]["default"]
    return set(str(v) for v in vals)


def is_text_file(path):
    ext = os.path.splitext(path)[1].lower()
    return ext in TEXT_EXTENSIONS and ext not in BLOCKED_EXTENSIONS


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_source_files(root, config, max_files=None, max_kb=None):
    ignored = ignored_dirs(config)
    max_files = cfg_int(max_files or config.get("snapshot_max_files", 700), 700, 1, 5000)
    max_kb = cfg_int(max_kb or config.get("snapshot_max_file_kb", 768), 768, 1, 4096)
    count = 0
    skipped = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ignored]
        for name in files:
            path = os.path.join(base, name)
            if not is_text_file(path):
                skipped += 1
                continue
            try:
                size = os.path.getsize(path)
            except Exception:
                skipped += 1
                continue
            if size > max_kb * 1024:
                skipped += 1
                continue
            if count >= max_files:
                skipped += 1
                continue
            count += 1
            yield path, skipped


def create_snapshot(config, root, task_id="", label="manual"):
    snap_id = f"snap-{int(time.time())}-{os.getpid()}"
    snap_dir = os.path.join(coding_dir(config, "snapshots"), snap_id)
    files_dir = os.path.join(snap_dir, "files")
    os.makedirs(files_dir, exist_ok=True)
    manifest = {
        "id": snap_id,
        "task_id": task_id or "",
        "label": label,
        "workspace": root,
        "created_at": now_iso(),
        "files": 0,
        "skipped": 0,
        "entries": [],
    }
    last_skipped = 0
    for path, skipped in iter_source_files(root, config):
        last_skipped = skipped
        rel = relpath(path, root)
        dest = os.path.join(files_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        manifest["entries"].append({"path": rel, "sha256": file_hash(path), "size": os.path.getsize(path)})
        manifest["files"] += 1
    manifest["skipped"] = last_skipped
    with open(os.path.join(snap_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=True)
    return manifest


def list_snapshots(config):
    d = coding_dir(config, "snapshots")
    out = []
    for name in sorted(os.listdir(d)):
        path = os.path.join(d, name, "manifest.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except Exception as _e:
                sys.stderr.write("[coding] uebersprungener Fehler: %r\n" % (_e,))
    return out


def restore_snapshot(config, snap_id, root):
    snap_dir = os.path.join(coding_dir(config, "snapshots"), snap_id)
    manifest_path = os.path.join(snap_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return f"CODING_SNAPSHOT_RESTORE_FAILED\nsnapshot nicht gefunden: {snap_id}"
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    restored = 0
    for entry in manifest.get("entries") or []:
        rel = entry.get("path", "")
        src = os.path.join(snap_dir, "files", rel)
        dest, err = resolve_path(rel, config, root, must_exist=False)
        if err or not os.path.exists(src):
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        restored += 1
    return f"CODING_SNAPSHOT_RESTORED\nsnapshot: {snap_id}\nfiles_restored: {restored}"


def format_snapshot(snap):
    return "\n".join(
        [
            "CODING_SNAPSHOT_CREATED",
            f"snapshot_id: {snap['id']}",
            f"task_id: {snap.get('task_id', '')}",
            f"workspace: {snap.get('workspace', '')}",
            f"files: {snap.get('files', 0)}",
            f"skipped: {snap.get('skipped', 0)}",
        ]
    )


def rg_search(root, query, config, max_results):
    rg = shutil.which("rg")
    hits = []
    if rg:
        cmd = [rg, "-n", "-i", "-F", "--hidden", "--glob", "!.git/*", "--glob", "!.coding/*", query, root]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12, check=False)
            for line in proc.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) == 3:
                    hits.append({"path": parts[0], "line": parts[1], "text": truncate(parts[2].strip(), 260)})
                    if len(hits) >= max_results:
                        break
        except Exception as _e:
            sys.stderr.write("[coding] uebersprungener Fehler: %r\n" % (_e,))
    if hits:
        return hits
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) > 2]
    for path, _ in iter_source_files(root, config, max_files=2000, max_kb=config.get("max_file_size_kb", 768)):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for idx, line in enumerate(fh, 1):
                    lower = line.lower()
                    if terms and all(t in lower for t in terms[:4]):
                        hits.append({"path": path, "line": str(idx), "text": truncate(line.strip(), 260)})
                        if len(hits) >= max_results:
                            return hits
        except Exception:
            continue
    return hits


def rank_candidate_files(root, query, config, limit=12):
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) > 2]
    scored = []
    for path, _ in iter_source_files(root, config, max_files=2500, max_kb=config.get("max_file_size_kb", 768)):
        rel = relpath(path, root).lower()
        score = sum(3 for t in terms if t in rel)
        if score <= 0:
            continue
        scored.append({"path": path, "score": score})
    scored.sort(key=lambda x: (x["score"], x["path"]), reverse=True)
    return scored[:limit]


def parse_path_range(raw):
    raw = str(raw or "").strip()
    m = re.match(r"^(.*):(\d+)(?:-(\d+))?$", raw)
    if not m:
        return raw, None, None
    return m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))


def read_range(path, root, start, end, max_lines, config):
    max_kb = cfg_int(config.get("max_file_size_kb", 768), 768, 1, 8192)
    size = os.path.getsize(path)
    if size > max_kb * 1024:
        return f"\nFILE {relpath(path, root)}\nFAILED: Datei zu gross {size//1024}KB > {max_kb}KB"
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    total = len(lines)
    if start is None:
        start = 1
    if end is None:
        end = min(total, start + max_lines - 1)
    start = max(1, int(start))
    end = min(total, int(end))
    if end - start + 1 > max_lines:
        end = start + max_lines - 1
    out = [f"\nFILE {relpath(path, root)}:{start}-{end} total_lines={total}"]
    for idx in range(start, end + 1):
        out.append(f"{idx:5d}  {lines[idx - 1]}")
    return "\n".join(out)


def extract_symbols(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return []
    patterns = [
        ("python_function", re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")),
        ("python_class", re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b")),
        ("rust_function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*[<(]")),
        ("rust_type", re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)\b")),
        ("js_function", re.compile(r"^\s*(?:export\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")),
        ("js_class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b")),
        ("js_arrow", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?\(?")),
        ("go_function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\(")),
    ]
    found = []
    for idx, line in enumerate(lines, 1):
        for kind, pat in patterns:
            m = pat.search(line)
            if m:
                found.append({"kind": kind, "name": m.group(1), "start": idx, "signature": line.strip()[:220]})
                break
    for i, sym in enumerate(found):
        next_start = found[i + 1]["start"] if i + 1 < len(found) else len(lines) + 1
        sym["end"] = max(sym["start"], next_start - 1)
        sym["summary"] = symbol_summary(lines, sym)
    return found


def symbol_summary(lines, sym):
    start = sym["start"]
    before = []
    for idx in range(max(1, start - 3), start):
        text = lines[idx - 1].strip()
        if text.startswith(("#", "//", "///", "*")) or text.startswith('"""'):
            before.append(text.strip("#/ *"))
    doc = " ".join(x for x in before if x)
    if doc:
        return f"{sym['kind']} {sym['name']}: {doc[:220]}"
    return f"{sym['kind']} {sym['name']} at lines {sym['start']}-{sym['end']}: {sym['signature']}"


def maybe_note_symbol(config, abs_path, root, symbol, start, end):
    if not symbol:
        return
    upsert_memory(
        config,
        {
            "type": "symbol_touch",
            "path": relpath(abs_path, root),
            "symbol": symbol,
            "line_start": start,
            "line_end": end,
            "summary": f"Symbol wurde fuer Coding-Kontext gelesen: {symbol}",
            "scope": "task",
            "file_hash": file_hash(abs_path),
        },
    )


def memory_db(config):
    path = os.path.join(coding_dir(config, "memory"), "coding_memory.sqlite3")
    con = sqlite3.connect(path)
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                type TEXT,
                scope TEXT,
                task_id TEXT,
                path TEXT,
                symbol TEXT,
                kind TEXT,
                line_start INTEGER,
                line_end INTEGER,
                summary TEXT,
                details TEXT,
                data_json TEXT,
                file_hash TEXT,
                stale INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                updated_ts INTEGER
            )
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def upsert_memory(config, note):
    db = memory_db(config)
    note_id = note.get("id") or memory_id(note)
    now = now_iso()
    details = note.get("details")
    if isinstance(details, (dict, list)):
        details = json.dumps(details, ensure_ascii=True)
    con = sqlite3.connect(db)
    try:
        con.execute(
            """
            INSERT INTO notes(id,type,scope,task_id,path,symbol,kind,line_start,line_end,summary,details,data_json,file_hash,stale,created_at,updated_at,updated_ts)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                type=excluded.type, scope=excluded.scope, task_id=excluded.task_id,
                path=excluded.path, symbol=excluded.symbol, kind=excluded.kind,
                line_start=excluded.line_start, line_end=excluded.line_end,
                summary=excluded.summary, details=excluded.details, data_json=excluded.data_json,
                file_hash=excluded.file_hash, stale=excluded.stale, updated_at=excluded.updated_at,
                updated_ts=excluded.updated_ts
            """,
            (
                note_id,
                str(note.get("type") or "note"),
                str(note.get("scope") or "project"),
                str(note.get("task_id") or ""),
                str(note.get("path") or ""),
                str(note.get("symbol") or ""),
                str(note.get("kind") or ""),
                int(note.get("line_start") or 0),
                int(note.get("line_end") or 0),
                str(note.get("summary") or ""),
                str(details or ""),
                json.dumps(note, ensure_ascii=True),
                str(note.get("file_hash") or ""),
                int(note.get("stale") or 0),
                now,
                now,
                int(time.time()),
            ),
        )
        con.commit()
    finally:
        con.close()
    return note_id


def memory_id(note):
    base = "|".join(
        [
            str(note.get("type") or "note"),
            str(note.get("scope") or "project"),
            str(note.get("task_id") or ""),
            str(note.get("path") or ""),
            str(note.get("symbol") or ""),
            str(note.get("summary") or "")[:80],
        ]
    )
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def search_memory(config, query, limit=10):
    db = memory_db(config)
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) > 2]
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM notes ORDER BY updated_ts DESC LIMIT 1000").fetchall()
    finally:
        con.close()
    scored = []
    for row in rows:
        text = " ".join(str(row[k] or "") for k in ("type", "scope", "task_id", "path", "symbol", "summary", "details")).lower()
        score = sum(3 if t in str(row["path"]).lower() or t in str(row["symbol"]).lower() else 1 for t in terms if t in text)
        if score > 0:
            item = dict(row)
            item["score"] = score
            scored.append(item)
    scored.sort(key=lambda x: (x["score"], x["updated_ts"]), reverse=True)
    return scored[:limit]


def format_loc(row):
    path = row.get("path") or "(project)"
    symbol = row.get("symbol") or ""
    line_start = row.get("line_start") or 0
    line_end = row.get("line_end") or 0
    suffix = f"#{symbol}" if symbol else ""
    lines = f":{line_start}-{line_end}" if line_start else ""
    return f"{path}{suffix}{lines}"


def touched_paths_from_diff(diff_text, config, root):
    paths = []
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            raw = line[4:].strip()
            if raw == "/dev/null":
                continue
            raw = raw.split("\t", 1)[0]
            if raw.startswith("a/") or raw.startswith("b/"):
                raw = raw[2:]
            abs_path, err = resolve_path(raw, config, root, must_exist=False)
            if err:
                return [], err
            if abs_path not in paths:
                paths.append(abs_path)
    return paths, ""


def strip_code_fence(text):
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines)
    return text


def run_proc(args, cwd, timeout=120):
    start = time.time()
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        return {
            "cmd": " ".join(shlex.quote(a) for a in args),
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "duration_s": round(time.time() - start, 2),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": " ".join(shlex.quote(a) for a in args),
            "exit_code": 124,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + f"\nTIMEOUT after {timeout}s",
            "duration_s": round(time.time() - start, 2),
        }
    except FileNotFoundError as exc:
        return {"cmd": " ".join(args), "exit_code": 127, "stdout": "", "stderr": str(exc), "duration_s": 0}


def git_diff_check(root):
    if not is_git_repo(root):
        return "git_diff_check: skipped (kein Git-Repo)"
    res = run_proc(["git", "diff", "--check"], root, timeout=30)
    if res["exit_code"] == 0:
        return "git_diff_check: OK"
    return "git_diff_check: FAILED\n" + truncate(res["stdout"] + res["stderr"], 1200)


def is_git_repo(root):
    res = run_proc(["git", "rev-parse", "--is-inside-work-tree"], root, timeout=10)
    return res["exit_code"] == 0 and "true" in res["stdout"]


def workspace_status(root):
    if is_git_repo(root):
        status = run_proc(["git", "status", "--short"], root, timeout=20)
        stat = run_proc(["git", "diff", "--stat"], root, timeout=20)
        body = (status["stdout"].strip() or "No changes") + ("\n" + stat["stdout"].strip() if stat["stdout"].strip() else "")
        return "WORKSPACE_STATUS\n" + body
    return "WORKSPACE_STATUS\n(no git repo)"


def workspace_has_changes(root):
    if not is_git_repo(root):
        return True
    status = run_proc(["git", "status", "--short"], root, timeout=20)
    return bool(status["stdout"].strip())


def command_from_payload(payload):
    cmd = first_text(payload, "cmd", "command")
    if cmd:
        return cmd
    profile = first_text(payload, "profile")
    files = payload.get("files") or []
    if isinstance(files, str):
        files = [files]
    files = [str(f) for f in files if str(f).strip()]
    profiles = {
        "py_compile": "python3 -m py_compile " + " ".join(shlex.quote(f) for f in files),
        "pytest": "python3 -m pytest",
        "cargo_test": "cargo test",
        "cargo_check": "cargo check",
        "npm_test": "npm test",
        "npm_build": "npm run build",
        "go_test": "go test ./...",
    }
    return profiles.get(profile, "")


def command_allowed(cmd, config):
    try:
        parts = shlex.split(cmd)
    except Exception as exc:
        return False, f"Command parse failed: {exc}"
    if not parts:
        return False, "Leerer Command"
    dangerous_chars = set(";|&><`")
    if any(any(ch in token for ch in dangerous_chars) or "$(" in token or "${" in token for token in parts):
        return False, "Shell-Metazeichen sind nicht erlaubt"
    allowed = config.get("allowed_run_commands") or MODULE["settings"]["allowed_run_commands"]["default"]
    for prefix in allowed:
        prefix_parts = shlex.split(str(prefix))
        if parts[: len(prefix_parts)] == prefix_parts:
            return True, ""
    return False, f"DENIED: Command nicht erlaubt: {cmd}. Erlaubte Prefixe: {allowed}"


def format_run(title, result, cmd=None):
    return "\n".join(
        [
            title,
            f"cmd: {cmd or result.get('cmd', '')}",
            f"exit_code: {result.get('exit_code')}",
            f"duration_s: {result.get('duration_s')}",
            "stdout:",
            truncate(result.get("stdout", ""), 4000),
            "stderr:",
            truncate(result.get("stderr", ""), 4000),
        ]
    )


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
