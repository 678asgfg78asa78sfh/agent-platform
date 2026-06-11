"""Video pipeline module for world-map briefings and Shorts exports."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = ROOT / "tools"
DEFAULT_DATA_DIR = ROOT / "agent-data" / "video_pipeline"
WORLDMAP_RENDERER = TOOLS_DIR / "worldmap_renderer.py"
BRIEFING_RENDERER = TOOLS_DIR / "youtube_briefing_renderer.py"


MODULE = {
    "name": "video_pipeline",
    "description": "Rendert Worldmap-Clips, map-led YouTube-Briefings und vertikale Shorts aus fertigen Videos.",
    "version": "1.0",
    "settings": {
        "enabled": {"type": "bool", "label": "Aktiv", "default": True},
        "python_timeout_s": {"type": "number", "label": "Python Timeout Sekunden", "default": 1800},
        "default_render_timeout_s": {"type": "number", "label": "Render Timeout Sekunden", "default": 1800},
        "python_bin": {"type": "string", "label": "Python Binary optional", "default": ""},
        "ffmpeg_bin": {"type": "string", "label": "ffmpeg Binary optional", "default": ""},
        "ffprobe_bin": {"type": "string", "label": "ffprobe Binary optional", "default": ""},
        "default_output_dir": {"type": "string", "label": "Output-Verzeichnis", "default": "agent-data/video_pipeline"},
        "default_worldmap_fps": {"type": "number", "label": "Worldmap FPS", "default": 25},
        "default_worldmap_duration_s": {"type": "number", "label": "Worldmap Dauer Sekunden", "default": 28},
        "default_shorts_count": {"type": "number", "label": "Shorts Anzahl", "default": 30},
        "default_shorts_duration_s": {"type": "number", "label": "Short Dauer Sekunden", "default": 45},
        "max_shorts_count": {"type": "number", "label": "Max Shorts pro Lauf", "default": 30},
        "shorts_mode": {"type": "select", "label": "Shorts Bildmodus", "default": "blur", "options": ["blur", "crop", "letterbox"]},
        "semantic_shorts": {"type": "bool", "label": "Shorts aus Hooks/Szenen planen", "default": True},
        "semantic_shorts_fill_count": {"type": "bool", "label": "Semantische Shorts bis count auffuellen", "default": False},
        "semantic_shorts_min_duration_s": {"type": "number", "label": "Min semantische Short-Dauer", "default": 18},
        "allow_silent_audio": {"type": "bool", "label": "Stummes Platzhalter-Audio erlauben", "default": False},
        "require_audio_for_shorts": {"type": "bool", "label": "Shorts nur mit hoerbarer Audiospur", "default": True},
        "speech_words_per_second": {"type": "number", "label": "Skript Sekunden pro Wort Schaetzung", "default": 2.35},
    },
    "tools": [
        {
            "name": "video_pipeline.worldmap_clip",
            "description": "Rendert einen Worldmap-Routenclip und Snapshots. JSON {route:['USA','Iran','China'], title, duration, fps, out_dir, snapshots_only}.",
            "params": ["query_json"],
        },
        {
            "name": "video_pipeline.briefing_video",
            "description": "Rendert ein map-led YouTube-Briefing aus Audio/Skript/Szenen. JSON {title,audio_path,script_path,script_text,scenes:[{title,subtitle,route,bullets,weight,color}],scenes_json_path,out_dir,preview,allow_silent_audio,duration_s}.",
            "params": ["query_json"],
        },
        {
            "name": "video_pipeline.shorts_from_video",
            "description": "Schneidet vertikale Shorts aus einem Video. Mit storyboard_path/video_assets_path werden Shorts thematisch nach Hooks/Szenen geplant. JSON {source_video,count,duration_s,storyboard_path,video_assets_path,semantic:true,out_dir,mode}.",
            "params": ["query_json"],
        },
        {
            "name": "video_pipeline.status",
            "description": "Prueft Renderer, ffmpeg und aktuelle Konfiguration.",
            "params": ["query_json"],
        },
        {
            "name": "video_pipeline.help",
            "description": "Zeigt Beispiele fuer Worldmap, Briefing-Video und Shorts-Pipeline.",
            "params": [],
        },
    ],
}


def handle_tool(tool_name: str, params: Any, config: dict[str, Any]) -> dict[str, Any]:
    try:
        if not cfg_bool(config, "enabled", True):
            return fail("video_pipeline ist deaktiviert.")
        if tool_name == "video_pipeline.worldmap_clip":
            return worldmap_clip(params, config)
        if tool_name == "video_pipeline.briefing_video":
            return briefing_video(params, config)
        if tool_name == "video_pipeline.shorts_from_video":
            return shorts_from_video(params, config)
        if tool_name == "video_pipeline.status":
            return status(config)
        if tool_name == "video_pipeline.help":
            return ok(help_text())
        return fail(f"Unbekanntes Tool: {tool_name}")
    except subprocess.TimeoutExpired as exc:
        return fail(f"VIDEO_PIPELINE_TIMEOUT nach {exc.timeout}s: {' '.join(exc.cmd) if isinstance(exc.cmd, list) else exc.cmd}")
    except Exception as exc:
        return fail(f"VIDEO_PIPELINE_FAILED: {exc}")


def worldmap_clip(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    require_file(WORLDMAP_RENDERER, "Worldmap Renderer")

    route = normalize_route(payload.get("route") or payload.get("countries") or payload.get("actors") or payload.get("route_csv"))
    title = text_value(payload.get("title"), "Geopolitische Kausalkette")
    duration = float_param(payload.get("duration") or payload.get("duration_s"), cfg_float(config, "default_worldmap_duration_s", 28.0), 2.0, 900.0)
    fps = int_param(payload.get("fps"), cfg_int(config, "default_worldmap_fps", 25, 1, 60), 1, 60)
    snapshots_only = bool_param(payload.get("snapshots_only") or payload.get("snapshots"), False)
    keep_frames = bool_param(payload.get("keep_frames"), False)

    out_dir = output_dir(payload, config, "worldmap_" + slugify(title))
    timeout = timeout_param(payload, config)
    cmd = [
        python_bin(config),
        str(WORLDMAP_RENDERER),
        "--route",
        ",".join(route),
        "--title",
        title,
        "--out",
        str(out_dir),
        "--duration",
        str(duration),
        "--fps",
        str(fps),
    ]
    if snapshots_only:
        cmd.append("--snapshots-only")
    if keep_frames:
        cmd.append("--keep-frames")

    started = time.time()
    proc = run_cmd(cmd, timeout)
    manifest = read_json(out_dir / "worldmap_manifest.json")
    result_path = last_stdout_path(proc.stdout) or manifest.get("video") or str(out_dir / "snapshots")
    return ok(
        {
            "type": "worldmap_clip",
            "output_dir": str(out_dir),
            "result": str(result_path),
            "video": manifest.get("video"),
            "snapshots": manifest.get("snapshots") or str(out_dir / "snapshots"),
            "manifest": str(out_dir / "worldmap_manifest.json"),
            "route": route,
            "title": title,
            "duration_s": duration,
            "fps": fps,
            "elapsed_s": round(time.time() - started, 2),
        }
    )


def briefing_video(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    require_file(BRIEFING_RENDERER, "Briefing Renderer")

    title = text_value(payload.get("title") or payload.get("project_title"), "Map-Led Briefing")
    out_dir = output_dir(payload, config, "briefing_" + slugify(title))
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [python_bin(config), str(BRIEFING_RENDERER), "--out", str(out_dir), "--title", title]
    preview = bool_param(payload.get("preview"), False)
    if preview:
        cmd.append("--preview")

    audio_path = path_value(payload.get("audio_path") or payload.get("audio"))
    script_path = path_value(payload.get("script_path") or payload.get("script"))
    script_text = text_value(payload.get("script_text") or payload.get("voice_script"), "")
    if not script_path and script_text:
        script_path = out_dir / "script_input.txt"
        script_path.write_text(script_text, encoding="utf-8")
    if audio_path:
        ensure_input_file(audio_path, "audio_path")
        cmd.extend(["--audio", str(audio_path)])
    if script_path:
        ensure_input_file(script_path, "script_path")
        cmd.extend(["--script", str(script_path)])
    if not audio_path:
        allow_silent = bool_param(payload.get("allow_silent_audio"), cfg_bool(config, "allow_silent_audio", False) or preview)
        if not allow_silent:
            return fail("audio_path fehlt und allow_silent_audio ist deaktiviert.")
        duration_s = optional_float(payload.get("duration_s") or payload.get("duration"))
        if duration_s is None:
            source_text = script_text
            if not source_text and script_path and script_path.exists():
                source_text = script_path.read_text(encoding="utf-8", errors="replace")
            duration_s = estimate_audio_duration(source_text, config)
        audio_path = out_dir / "silent_placeholder.mp3"
        create_silent_audio(audio_path, duration_s, config)
        cmd.extend(["--audio", str(audio_path)])

    scenes_json = None
    if payload.get("scenes"):
        scenes_json = out_dir / "scenes_input.json"
        scenes_payload = {
            "title": title,
            "scenes": normalize_scenes(payload.get("scenes")),
        }
        write_json(scenes_json, scenes_payload)
    elif payload.get("scenes_json_path") or payload.get("scenes_json"):
        scenes_json = path_value(payload.get("scenes_json_path") or payload.get("scenes_json"))
        ensure_input_file(scenes_json, "scenes_json_path")
    if scenes_json:
        cmd.extend(["--scenes-json", str(scenes_json)])

    timeout = timeout_param(payload, config)
    started = time.time()
    proc = run_cmd(cmd, timeout)
    final_path = last_stdout_path(proc.stdout) or newest_video(out_dir)
    storyboard = out_dir / "storyboard_mapled.json"
    package = out_dir / "youtube_package_mapled.md"
    return ok(
        {
            "type": "briefing_video",
            "output_dir": str(out_dir),
            "video": str(final_path) if final_path else "",
            "storyboard": str(storyboard) if storyboard.exists() else "",
            "package": str(package) if package.exists() else "",
            "scenes_json": str(scenes_json) if scenes_json else "",
            "script_path": str(script_path) if script_path else "",
            "audio_path": str(audio_path) if audio_path else "",
            "preview": preview,
            "title": title,
            "elapsed_s": round(time.time() - started, 2),
        }
    )


def shorts_from_video(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    source = path_value(payload.get("source_video") or payload.get("video") or payload.get("input"))
    if not source:
        return fail('source_video fehlt. Beispiel: {"source_video":"agent-data/.../video.mp4","count":30}')
    ensure_input_file(source, "source_video")
    require_audio = bool_param(payload.get("require_audio"), cfg_bool(config, "require_audio_for_shorts", True))
    if require_audio:
        audio = audio_profile(source, config)
        if not audio.get("audible"):
            return fail(f"source_video hat keine hoerbare Audiospur: {audio.get('reason') or 'unknown'}")

    max_count = cfg_int(config, "max_shorts_count", 30, 1, 200)
    count = int_param(payload.get("count"), cfg_int(config, "default_shorts_count", 30, 1, max_count), 1, max_count)
    duration_s = float_param(payload.get("duration_s") or payload.get("duration"), cfg_float(config, "default_shorts_duration_s", 45.0), 3.0, 180.0)
    start_s = float_param(payload.get("start_s") or payload.get("start"), 0.0, 0.0, 24 * 3600.0)
    gap_s = optional_float(payload.get("gap_s") or payload.get("gap"))
    mode = text_value(payload.get("mode"), str(config.get("shorts_mode") or "blur")).strip().lower()
    if mode not in {"blur", "crop", "letterbox"}:
        return fail("mode muss blur, crop oder letterbox sein.")
    prefix = slugify(text_value(payload.get("prefix"), source.stem), "short")
    out_dir = output_dir(payload, config, "shorts_" + prefix)
    out_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = ffmpeg_bin(config)
    total_duration = video_duration(source, config)
    semantic = bool_param(payload.get("semantic"), cfg_bool(config, "semantic_shorts", True))
    clips_plan = semantic_short_plan(source, total_duration, count, duration_s, start_s, gap_s, payload, config) if semantic else []
    if not clips_plan:
        starts = clip_starts(total_duration, count, duration_s, start_s, gap_s)
        clips_plan = [
            {
                "index": idx,
                "start_s": clip_start,
                "duration_s": duration_s,
                "semantic": False,
            }
            for idx, clip_start in enumerate(starts, start=1)
        ]
    clips = []
    timeout = timeout_param(payload, config)
    started = time.time()
    for idx, clip in enumerate(clips_plan, start=1):
        clip_start = float(clip.get("start_s") or 0.0)
        clip_duration = float(clip.get("duration_s") or duration_s)
        clip_slug = slugify(text_value(clip.get("hook") or clip.get("title") or clip.get("source_scene"), ""), f"{idx:02d}")
        out_path = out_dir / f"{prefix}_{idx:02d}_{int(round(clip_start)):05d}s_{clip_slug}.mp4"
        run_cmd(shorts_cmd(ffmpeg, source, out_path, clip_start, clip_duration, mode), timeout)
        meta = {
            "index": idx,
            "path": str(out_path),
            "start_s": round(clip_start, 2),
            "duration_s": round(clip_duration, 2),
            "mode": mode,
            "semantic": bool(clip.get("semantic")),
            "hook": text_value(clip.get("hook"), ""),
            "angle": text_value(clip.get("angle"), ""),
            "source_scene": text_value(clip.get("source_scene") or clip.get("title"), ""),
        }
        if clip.get("subtitle"):
            meta["subtitle"] = text_value(clip.get("subtitle"), "")
        if clip.get("why"):
            meta["why"] = text_value(clip.get("why"), "")
        write_json(out_path.with_suffix(".json"), meta)
        clips.append(meta)

    manifest = {
        "type": "shorts_from_video",
        "source_video": str(source),
        "source_duration_s": total_duration,
        "requested_count": count,
        "count": len(clips),
        "duration_s": duration_s,
        "mode": mode,
        "semantic": bool(clips and clips[0].get("semantic")),
        "clips": clips,
        "created_at": int(time.time()),
        "elapsed_s": round(time.time() - started, 2),
    }
    manifest_path = out_dir / "shorts_manifest.json"
    write_json(manifest_path, manifest)
    return ok(
        {
            "type": "shorts_from_video",
            "output_dir": str(out_dir),
            "manifest": str(manifest_path),
            "source_duration_s": total_duration,
            "count": len(clips),
            "requested_count": count,
            "clips": clips,
            "elapsed_s": manifest["elapsed_s"],
        }
    )


def status(config: dict[str, Any]) -> dict[str, Any]:
    ffmpeg = ffmpeg_bin(config)
    ffprobe = ffprobe_bin(config)
    ffprobe_available = bool(shutil.which(ffprobe) or Path(ffprobe).exists())
    checks = {
        "worldmap_renderer": WORLDMAP_RENDERER.exists(),
        "briefing_renderer": BRIEFING_RENDERER.exists(),
        "ffmpeg": shutil.which(ffmpeg) or Path(ffmpeg).exists(),
        "ffprobe": ffprobe_available,
        "duration_probe": "ffprobe" if ffprobe_available else "ffmpeg_fallback",
        "default_output_dir": str(default_output_dir(config)),
        "python_bin": python_bin(config),
        "python_timeout_s": cfg_int(config, "python_timeout_s", 1800, 1, 86400),
        "render_timeout_s": cfg_int(config, "default_render_timeout_s", 1800, 1, 86400),
    }
    return ok(checks)


def help_text() -> dict[str, Any]:
    return {
        "pipeline": [
            "1. video_pipeline.worldmap_clip fuer schnelle Kartenbewegungen und Snapshots.",
            "2. video_pipeline.briefing_video fuer Longform-Video mit Audio, Skript und Szenen.",
            "3. video_pipeline.shorts_from_video fuer bis zu 30 vertikale Clips aus dem Longform-Video.",
        ],
        "examples": {
            "worldmap_clip": {
                "route": ["USA", "Iran", "China", "Taiwan"],
                "title": "UAP, USA und geopolitischer Kontext",
                "duration": 20,
                "fps": 25,
            },
            "briefing_video": {
                "title": "UAP DeepDive",
                "audio_path": "agent-data/telegram_bot_media/last_deepdive_normalized_audio_de_clara_v2.mp3",
                "script_path": "agent-data/telegram_bot_media/last_deepdive_audio_script_de_tts_v2.txt",
                "preview": True,
                "scenes": [
                    {
                        "title": "USA",
                        "subtitle": "Politik, Militaer und Disclosure",
                        "route": ["USA", "United Kingdom", "China"],
                        "bullets": ["Kongressdruck", "Militaerberichte", "internationale Reaktionen"],
                        "weight": 1.0,
                        "color": "gold",
                    }
                ],
            },
            "shorts_from_video": {
                "source_video": "agent-data/video_pipeline/briefing_uap/uap_deepdive_1080p.mp4",
                "count": 30,
                "duration_s": 45,
                "mode": "blur",
            },
        },
    }


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
        result: dict[str, Any] = {}
        for idx, item in enumerate(params):
            if isinstance(item, dict):
                result.update(item)
            elif isinstance(item, str) and ":" in item:
                key, value = item.split(":", 1)
                result[key.strip()] = value.strip()
            else:
                result[f"arg{idx + 1}"] = item
        return result
    return {}


def parse_jsonish(value: str) -> dict[str, Any]:
    raw = (value or "").strip()
    if not raw:
        return {}
    for prefix in ("query_json:", "json:", "params:", "payload:"):
        if raw.lower().startswith(prefix):
            raw = raw.split(":", 1)[1].strip()
            break
    if raw.startswith("{") or raw.startswith("["):
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"items": data}
    return {"query": raw}


def normalize_route(value: Any) -> list[str]:
    if value is None:
        return ["USA", "Iran", "China", "Taiwan"]
    if isinstance(value, str):
        route = [part.strip() for part in re.split(r"[,>\n;]+", value) if part.strip()]
    elif isinstance(value, list):
        route = [str(part).strip() for part in value if str(part).strip()]
    else:
        route = []
    if len(route) == 1:
        route.append("Global")
    if len(route) < 2:
        raise ValueError("route braucht mindestens zwei Laender/Regionen.")
    return route


def normalize_scenes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not value:
        raise ValueError("scenes muss eine nicht-leere Liste sein.")
    scenes = []
    for idx, scene in enumerate(value, start=1):
        if not isinstance(scene, dict):
            raise ValueError(f"Szene {idx} ist kein Objekt.")
        route = normalize_route(scene.get("route") or scene.get("countries") or scene.get("actors"))
        bullets = scene.get("bullets") or scene.get("points") or []
        if isinstance(bullets, str):
            bullets = [part.strip() for part in re.split(r"\n+|;\s*", bullets) if part.strip()]
        if not isinstance(bullets, list):
            bullets = []
        scenes.append(
            {
                "title": text_value(scene.get("title"), f"Kapitel {idx}"),
                "subtitle": text_value(scene.get("subtitle") or scene.get("summary"), ""),
                "route": route,
                "bullets": [str(item).strip() for item in bullets if str(item).strip()][:6],
                "weight": float_param(scene.get("weight"), 1.0, 0.05, 10.0),
                "color": scene.get("color") or "gold",
            }
        )
    return scenes


def shorts_cmd(ffmpeg: str, source: Path, out_path: Path, start_s: float, duration_s: float, mode: str) -> list[str]:
    common = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-ss",
        f"{start_s:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration_s:.3f}",
    ]
    if mode == "blur":
        return common + [
            "-filter_complex",
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=18:1[bg];"
            "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]",
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
    if mode == "crop":
        vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,format=yuv420p"
    else:
        vf = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p"
    return common + [
        "-vf",
        vf,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(out_path),
    ]


def semantic_short_plan(
    source: Path,
    total_duration: float,
    count: int,
    default_duration_s: float,
    start_s: float,
    gap_s: float | None,
    payload: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    storyboard = load_json_from_payload(payload, source, "storyboard_path", "storyboard", "storyboard_json", fallback_name="storyboard_mapled.json")
    assets = load_json_from_payload(payload, source, "video_assets_path", "assets_path", "video_assets", "shorts_json_path", fallback_name="video_assets.json")
    scenes = normalize_storyboard_scenes(storyboard.get("scenes") or assets.get("scenes") or [], total_duration)
    if not scenes:
        return []
    shorts = [item for item in assets.get("shorts", []) if isinstance(item, dict)]
    shorts = [item for item in shorts if bool_param(item.get("shortable"), True)]
    fill_count = bool_param(payload.get("semantic_fill") or payload.get("fill_count"), cfg_bool(config, "semantic_shorts_fill_count", False))
    min_duration = float_param(
        payload.get("semantic_min_duration_s") or payload.get("min_duration_s"),
        cfg_float(config, "semantic_shorts_min_duration_s", 18.0),
        3.0,
        max(3.0, default_duration_s),
    )

    plan: list[dict[str, Any]] = []
    if shorts:
        matched = [(short, match_scene(short.get("source_scene") or short.get("scene") or short.get("title") or short.get("angle"), scenes)) for short in shorts[:count]]
        per_scene_total: dict[int, int] = {}
        for _short, scene in matched:
            per_scene_total[scene["index"]] = per_scene_total.get(scene["index"], 0) + 1
        per_scene_seen: dict[int, int] = {}
        for short, scene in matched:
            scene_key = scene["index"]
            slot = per_scene_seen.get(scene_key, 0)
            per_scene_seen[scene_key] = slot + 1
            desired_duration = optional_float(short.get("duration_s") or short.get("duration")) or default_duration_s
            offset = optional_float(short.get("start_offset_s") or short.get("offset_s"))
            plan.append(plan_clip_for_scene(scene, desired_duration, min_duration, total_duration, slot, per_scene_total[scene_key], offset, short))

    if not plan:
        for scene in scenes[:count]:
            plan.append(plan_clip_for_scene(scene, default_duration_s, min_duration, total_duration, 0, 1, None, {}))

    if fill_count and len(plan) < count:
        idx = 0
        while len(plan) < count and scenes:
            scene = scenes[idx % len(scenes)]
            slot = idx // len(scenes) + 1
            plan.append(plan_clip_for_scene(scene, default_duration_s, min_duration, total_duration, slot, 3, None, {}))
            idx += 1

    return plan[:count]


def load_json_from_payload(payload: dict[str, Any], source: Path, *keys: str, fallback_name: str = "") -> dict[str, Any]:
    candidates: list[Path] = []
    for key in keys:
        raw = payload.get(key)
        if raw:
            path = path_value(raw)
            if path:
                candidates.append(path)
    if fallback_name:
        candidates.append(source.parent / fallback_name)
    for path in candidates:
        if path.exists() and path.is_file():
            data = read_json(path)
            if isinstance(data, dict):
                return data
    return {}


def normalize_storyboard_scenes(raw_scenes: Any, total_duration: float) -> list[dict[str, Any]]:
    if not isinstance(raw_scenes, list):
        return []
    scenes = []
    elapsed = 0.0
    weights = []
    has_timing = any(isinstance(scene, dict) and scene.get("duration_s") for scene in raw_scenes)
    if not has_timing:
        for scene in raw_scenes:
            weights.append(float_param((scene or {}).get("weight") if isinstance(scene, dict) else None, 1.0, 0.05, 10.0))
        unit = total_duration / max(sum(weights), 0.001) if total_duration > 0 else 0.0
    for idx, scene in enumerate(raw_scenes, start=1):
        if not isinstance(scene, dict):
            continue
        duration = optional_float(scene.get("duration_s") or scene.get("duration"))
        if duration is None:
            duration = max(0.0, weights[idx - 1] * unit) if idx - 1 < len(weights) else 0.0
        start = optional_float(scene.get("start_s") or scene.get("start"))
        if start is None:
            start = elapsed
        elapsed = max(elapsed, start + duration)
        scenes.append(
            {
                "index": idx,
                "title": text_value(scene.get("title"), f"Kapitel {idx}"),
                "subtitle": text_value(scene.get("subtitle") or scene.get("summary"), ""),
                "start_s": max(0.0, start),
                "duration_s": max(0.0, duration),
            }
        )
    return scenes


def match_scene(value: Any, scenes: list[dict[str, Any]]) -> dict[str, Any]:
    needle = normalize_match_text(str(value or ""))
    if not needle:
        return scenes[0]
    best = scenes[0]
    best_score = -1
    for scene in scenes:
        hay = normalize_match_text(scene.get("title", "") + " " + scene.get("subtitle", ""))
        score = token_overlap_score(needle, hay)
        if needle and needle in hay:
            score += 10
        if score > best_score:
            best = scene
            best_score = score
    return best


def plan_clip_for_scene(
    scene: dict[str, Any],
    desired_duration: float,
    min_duration: float,
    total_duration: float,
    slot: int,
    slot_count: int,
    explicit_offset: float | None,
    short: dict[str, Any],
) -> dict[str, Any]:
    scene_start = max(0.0, float(scene.get("start_s") or 0.0))
    scene_duration = max(0.0, float(scene.get("duration_s") or 0.0))
    if scene_duration <= 0 and total_duration > 0:
        scene_duration = max(0.0, total_duration - scene_start)
    clip_duration = min(max(min_duration, desired_duration), max(min_duration, scene_duration or desired_duration))
    if scene_duration and scene_duration < clip_duration:
        clip_duration = max(3.0, scene_duration)
    max_offset = max(0.0, scene_duration - clip_duration)
    if explicit_offset is not None:
        offset = max(0.0, min(float(explicit_offset), max_offset))
    elif slot_count > 1:
        offset = max_offset * slot / max(1, slot_count - 1)
    else:
        offset = 0.0
    clip_start = scene_start + offset
    if total_duration > 0:
        clip_start = min(clip_start, max(0.0, total_duration - clip_duration))
        clip_duration = min(clip_duration, max(3.0, total_duration - clip_start))
    return {
        "semantic": True,
        "start_s": round(clip_start, 3),
        "duration_s": round(clip_duration, 3),
        "title": scene.get("title") or "",
        "subtitle": scene.get("subtitle") or "",
        "source_scene": text_value(short.get("source_scene") or scene.get("title"), ""),
        "hook": text_value(short.get("hook"), ""),
        "angle": text_value(short.get("angle"), ""),
        "why": text_value(short.get("why"), ""),
    }


def normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", " ", value.casefold()).strip()


def token_overlap_score(a: str, b: str) -> int:
    left = {token for token in a.split() if len(token) > 2}
    right = {token for token in b.split() if len(token) > 2}
    return len(left & right)


def clip_starts(total_duration: float, count: int, duration_s: float, start_s: float, gap_s: float | None) -> list[float]:
    if total_duration <= 0:
        return [start_s + idx * (duration_s + (gap_s or 0.0)) for idx in range(count)]
    max_start = max(0.0, total_duration - duration_s)
    start_s = min(start_s, max_start)
    if gap_s is not None:
        starts = [start_s + idx * (duration_s + max(0.0, gap_s)) for idx in range(count)]
        return [min(value, max_start) for value in starts]
    if count <= 1 or max_start <= start_s:
        return [start_s for _ in range(count)]
    step = (max_start - start_s) / max(1, count - 1)
    return [start_s + idx * step for idx in range(count)]


def video_duration(path: Path, config: dict[str, Any]) -> float:
    ffprobe = ffprobe_bin(config)
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if proc.returncode == 0:
            return round(float(proc.stdout.strip()), 3)
    except Exception:
        pass

    ffmpeg = ffmpeg_bin(config)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path), "-f", "null", "-"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        return 0.0
    h, m, s = match.groups()
    return round(int(h) * 3600 + int(m) * 60 + float(s), 3)


def audio_profile(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [ffmpeg_bin(config), "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except Exception as exc:
        return {"audible": False, "reason": f"audio_probe_failed: {exc}"}
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if "Audio:" not in text:
        return {"audible": False, "reason": "no_audio_stream"}
    match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)
    if not match:
        return {"audible": False, "reason": "volume_probe_failed"}
    max_volume = float(match.group(1))
    if max_volume <= -80.0:
        return {"audible": False, "reason": f"silent_audio max_volume={max_volume}dB", "max_volume_db": max_volume}
    return {"audible": True, "max_volume_db": max_volume}


def estimate_audio_duration(text: str, config: dict[str, Any]) -> float:
    words = len(re.findall(r"\w+", text or ""))
    words_per_second = float_param(config.get("speech_words_per_second"), 2.35, 0.8, 5.0)
    return max(12.0, min(1800.0, round(words / words_per_second + 6.0, 2)))


def create_silent_audio(path: Path, duration_s: float, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            ffmpeg_bin(config),
            "-y",
            "-hide_banner",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            f"{max(1.0, duration_s):.3f}",
            "-q:a",
            "9",
            "-acodec",
            "libmp3lame",
            str(path),
        ],
        120,
    )


def run_cmd(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-4000:]
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{tail}")
    return proc


def output_dir(payload: dict[str, Any], config: dict[str, Any], default_name: str) -> Path:
    raw = payload.get("out_dir") or payload.get("output_dir") or payload.get("out")
    if raw:
        resolved = path_value(raw)
        if resolved:
            return resolved
    return default_output_dir(config) / unique_dir_name(default_name)


def default_output_dir(config: dict[str, Any]) -> Path:
    raw = str(config.get("default_output_dir") or "").strip()
    if not raw:
        return DEFAULT_DATA_DIR
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def unique_dir_name(base: str) -> str:
    clean = slugify(base, "render")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return f"{clean}_{stamp}"


def path_value(value: Any) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def ensure_input_file(path: Path, label: str) -> None:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"{label} nicht gefunden: {path}")


def require_file(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} fehlt: {path}")


def newest_video(out_dir: Path) -> Path | None:
    videos = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return videos[0] if videos else None


def last_stdout_path(stdout: str) -> str:
    for line in reversed((stdout or "").splitlines()):
        candidate = line.strip()
        if candidate and (candidate.startswith("/") or candidate.startswith("agent-data") or candidate.startswith("tools")):
            return candidate
    return ""


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def python_bin(config: dict[str, Any]) -> str:
    return str(config.get("python_bin") or sys.executable or "python3")


def ffmpeg_bin(config: dict[str, Any]) -> str:
    configured = str(config.get("ffmpeg_bin") or "").strip()
    if configured:
        return configured
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def ffprobe_bin(config: dict[str, Any]) -> str:
    configured = str(config.get("ffprobe_bin") or "").strip()
    if configured:
        return configured
    return "ffprobe"


def timeout_param(payload: dict[str, Any], config: dict[str, Any]) -> int:
    return int_param(payload.get("timeout_s") or payload.get("timeout"), cfg_int(config, "default_render_timeout_s", 1800, 1, 86400), 1, 86400)


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


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float_param(value, 0.0, 0.0, 24 * 3600.0)


def text_value(value: Any, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text or default


def slugify(value: str, fallback: str = "video") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").casefold()).strip("_")
    return slug[:90] or fallback


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
