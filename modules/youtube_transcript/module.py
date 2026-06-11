"""YouTube transcript module.

Primary path is keyless: use yt-dlp to read public YouTube metadata and
caption/auto-caption tracks. Optional STT fallback can use an already configured
xAI key, but YouTube OAuth/API keys are not required.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import job_history_common as job_history
except Exception:  # pragma: no cover
    job_history = None


MODULE = {
    "name": "youtube_transcript",
    "description": (
        "YouTube-Videos ohne YouTube-API-Key transkribieren: erst Creator-/Auto-Captions "
        "via yt-dlp, optional Audio-STT via vorhandenen xAI-Key, plus RAG-Speicherung."
    ),
    "version": "1.0",
    "settings": {
        "yt_dlp_bin": {"type": "string", "label": "yt-dlp Pfad optional", "default": ""},
        "preferred_languages": {"type": "string", "label": "Bevorzugte Untertitelsprachen", "default": "de,en,auto"},
        "request_timeout_s": {"type": "number", "label": "Request Timeout Sekunden", "default": 25},
        "max_output_chars": {"type": "number", "label": "Max Ausgabezeichen", "default": 50000},
        "max_rag_chars": {"type": "number", "label": "Max RAG Transcript Zeichen", "default": 90000},
        "enable_stt_fallback": {"type": "bool", "label": "Audio-STT Fallback erlauben", "default": False},
        "xai_api_key": {"type": "password", "label": "xAI API Key/Alias fuer STT optional", "default": "api.xai"},
        "xai_stt_url": {"type": "string", "label": "xAI STT URL", "default": "https://api.x.ai/v1/stt"},
        "stt_language": {"type": "string", "label": "STT Sprache oder auto", "default": "auto"},
        "max_audio_duration_s": {"type": "number", "label": "Max Audio-Dauer fuer STT", "default": 3600},
        "keep_audio_files": {"type": "bool", "label": "Audio-Dateien behalten", "default": False},
    },
    "tools": [
        {
            "name": "youtube_transcript.fetch",
            "description": (
                "Holt ein YouTube-Transkript bevorzugt aus Captions/Auto-Captions. "
                "Parameter: URL oder JSON {url, languages?, fallback_stt?, store_rag?}."
            ),
            "params": ["query_json_or_url"],
        },
        {
            "name": "youtube_transcript.captions",
            "description": "Listet verfuegbare Creator- und Auto-Caption-Sprachen/Formate fuer eine YouTube-URL.",
            "params": ["query_json_or_url"],
        },
        {
            "name": "youtube_transcript.transcribe",
            "description": (
                "Erzwingt Audio-Download und STT ueber vorhandenen xAI-Key. "
                "Parameter: JSON {url, language?, store_rag?}. Keine YouTube-OAuth/API noetig."
            ),
            "params": ["query_json_or_url"],
        },
        {
            "name": "youtube_transcript.to_rag",
            "description": "Holt Transkript und speichert es als strukturierte YouTube-Notiz im verbundenen RAG-Pool.",
            "params": ["query_json_or_url"],
        },
        {
            "name": "youtube_transcript.help",
            "description": "Zeigt Beispiele, Voraussetzungen und Grenzen.",
            "params": [],
        },
    ],
}


def handle_tool(tool_name: str, params: list[str], config: dict[str, Any]) -> dict[str, Any]:
    try:
        if tool_name == "youtube_transcript.fetch":
            payload = parse_payload(params)
            return fetch_tool(payload, config, force_stt=False, force_rag=False)
        if tool_name == "youtube_transcript.captions":
            return captions_tool(parse_payload(params), config)
        if tool_name == "youtube_transcript.transcribe":
            payload = parse_payload(params)
            return fetch_tool(payload, config, force_stt=True, force_rag=bool_param(payload.get("store_rag", False)))
        if tool_name == "youtube_transcript.to_rag":
            payload = parse_payload(params)
            return fetch_tool(payload, config, force_stt=False, force_rag=True)
        if tool_name == "youtube_transcript.help":
            return ok(help_text(config))
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"YOUTUBE_TRANSCRIPT_ERROR: {exc}")


def captions_tool(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    url = normalize_youtube_url(first_text(payload, "url", "video", "query", "q"))
    if not url:
        return fail("YouTube-URL oder Video-ID fehlt.")
    started = start_job("youtube_transcript.captions", url, payload, config)
    try:
        info = probe_video(url, config)
        lines = [
            "YOUTUBE_CAPTIONS",
            f"video_id: {info.get('id', '')}",
            f"title: {clean_line(info.get('title', ''))}",
            f"channel: {clean_line(info.get('channel') or info.get('uploader') or '')}",
            f"duration_s: {info.get('duration') or ''}",
            f"source_url: {canonical_video_url(info, url)}",
            "",
        ]
        manual = summarize_caption_map(info.get("subtitles") or {}, limit=80)
        auto = summarize_caption_map(info.get("automatic_captions") or {}, limit=40)
        lines.append("creator_captions:")
        lines.extend(manual or ["- none"])
        lines.append("")
        lines.append("auto_captions:")
        lines.extend(auto or ["- none"])
        sources = [video_source(info, url)]
        finish_job(started, "success", config, sources, [], "caption languages listed", "", {"manual": len(manual), "auto": len(auto)})
        return ok(limit_output("\n".join(lines), config))
    except Exception as exc:
        finish_job(started, "failed", config, [], [], "", str(exc), {})
        return fail(f"YOUTUBE_CAPTIONS_FAILED\nurl: {url}\nerror: {exc}")


def fetch_tool(
    payload: dict[str, Any],
    config: dict[str, Any],
    force_stt: bool = False,
    force_rag: bool = False,
) -> dict[str, Any]:
    url = normalize_youtube_url(first_text(payload, "url", "video", "query", "q"))
    if not url:
        return fail("YouTube-URL oder Video-ID fehlt.")
    tool = "youtube_transcript.transcribe" if force_stt else ("youtube_transcript.to_rag" if force_rag else "youtube_transcript.fetch")
    started = start_job(tool, url, payload, config)
    try:
        if not force_stt and not bool_param(payload.get("refresh", False)):
            cached = load_cached_transcript(url, config)
            if cached is not None:
                result, transcript, segments = cached
                store_rag = force_rag or bool_param(payload.get("store_rag", False))
                rag_id = ""
                if store_rag:
                    rag_id, rag_msg = store_rag_note(result, transcript, config)
                    result["rag_id"] = rag_id
                    result["rag_message"] = rag_msg
                sources = [video_source(info_from_result(result), url, rag_id=rag_id)]
                finish_job(
                    started,
                    "success",
                    config,
                    sources,
                    [rag_id] if rag_id else [],
                    f"cache transcript chars={len(transcript)}",
                    "",
                    {"method": "cache", "segments": len(segments), "chars": len(transcript), "rag": bool(rag_id)},
                )
                return ok(limit_output(format_result(result, transcript, segments), config))

        info = probe_video(url, config)
        lang_prefs = payload_languages(payload, config)
        result: dict[str, Any] | None = None
        transcript = ""
        segments: list[dict[str, Any]] = []
        method = "captions"
        error_note = ""

        if not force_stt:
            selected = select_caption_track(info, lang_prefs)
            if selected:
                raw = download_caption(selected, info, config)
                segments = parse_caption(raw["text"], raw["ext"])
                transcript = segments_to_text(segments)
                result = build_result(info, url, selected, segments, transcript, method="captions")
            else:
                error_note = "Keine passenden Creator-/Auto-Captions gefunden."

        fallback_allowed = force_stt or bool_param(payload.get("fallback_stt", config.get("enable_stt_fallback", False)))
        if result is None and fallback_allowed:
            method = "stt"
            result = transcribe_audio(info, url, payload, config)
            transcript = str(result.get("transcript") or "")
            segments = result.get("segments") or []

        if result is None:
            sources = [video_source(info, url)]
            finish_job(started, "failed", config, sources, [], "", error_note, {"captions": False, "stt": False})
            return fail(no_transcript_message(info, url, lang_prefs, error_note))

        stored_paths = store_transcript_files(result, transcript, segments, config)
        result.update(stored_paths)

        store_rag = force_rag or bool_param(payload.get("store_rag", False))
        rag_id = ""
        rag_msg = ""
        if store_rag:
            rag_id, rag_msg = store_rag_note(result, transcript, config)
            result["rag_id"] = rag_id
            result["rag_message"] = rag_msg

        sources = [video_source(info, url, rag_id=rag_id)]
        status = "success" if transcript.strip() else "failed"
        finish_job(
            started,
            status,
            config,
            sources,
            [rag_id] if rag_id else [],
            f"{method} transcript chars={len(transcript)}",
            "" if status == "success" else "empty transcript",
            {"method": method, "segments": len(segments), "chars": len(transcript), "rag": bool(rag_id)},
        )
        return ok(limit_output(format_result(result, transcript, segments), config))
    except Exception as exc:
        finish_job(started, "failed", config, [], [], "", str(exc), {})
        return fail(f"YOUTUBE_TRANSCRIPT_FAILED\nurl: {url}\nerror: {exc}")


def probe_video(url: str, config: dict[str, Any]) -> dict[str, Any]:
    yt_dlp = yt_dlp_bin(config)
    if not yt_dlp:
        raise RuntimeError(
            "yt-dlp nicht gefunden. Installiere yt-dlp oder setze youtube_transcript.yt_dlp_bin. "
            "Lokal z.B.: python3 -m venv .venv-ytdlp && .venv-ytdlp/bin/pip install yt-dlp"
        )
    timeout = int_param(config.get("request_timeout_s"), 25, 5, 180)
    cmd = [
        yt_dlp,
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout",
        str(timeout),
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 20)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"yt-dlp metadata failed ({proc.returncode}): {safe_truncate(err, 900)}")
    try:
        return json.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError(f"yt-dlp JSON konnte nicht gelesen werden: {exc}")


def yt_dlp_bin(config: dict[str, Any]) -> str:
    candidates = [
        str(config.get("yt_dlp_bin") or "").strip(),
        os.environ.get("YTDLP_BIN", ""),
        shutil.which("yt-dlp") or "",
        os.path.expanduser("~/.local/bin/yt-dlp"),
    ]
    project_root = str(config.get("project_root") or "").strip()
    if project_root:
        candidates.extend(
            [
                os.path.join(project_root, ".venv-ytdlp", "bin", "yt-dlp"),
                os.path.join(project_root, ".venv", "bin", "yt-dlp"),
            ]
        )
    here = os.path.abspath(os.path.dirname(__file__))
    candidates.extend(
        [
            os.path.abspath(os.path.join(here, "..", "..", ".venv-ytdlp", "bin", "yt-dlp")),
            os.path.abspath(os.path.join(here, "..", "..", ".venv", "bin", "yt-dlp")),
        ]
    )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        if candidate and os.path.basename(candidate) == candidate:
            found = shutil.which(candidate)
            if found:
                return found
    return ""


def select_caption_track(info: dict[str, Any], lang_prefs: list[str]) -> dict[str, Any] | None:
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for source_name, tracks in (("creator_caption", manual), ("auto_caption", auto)):
        for lang in language_order(tracks, lang_prefs):
            formats = tracks.get(lang) or []
            selected_format = select_caption_format(formats)
            if selected_format:
                out = dict(selected_format)
                out["language"] = lang
                out["caption_source"] = source_name
                return out
    return None


def language_order(tracks: dict[str, Any], prefs: list[str]) -> list[str]:
    keys = list(tracks.keys())
    out: list[str] = []
    for pref in prefs:
        p = pref.strip()
        if not p:
            continue
        if p.lower() in ("auto", "any", "*"):
            for key in keys:
                append_unique(out, key)
            continue
        for key in keys:
            if key.lower() == p.lower():
                append_unique(out, key)
        for key in keys:
            kl = key.lower()
            pl = p.lower()
            if kl.startswith(pl + "-") or kl.startswith(pl + "."):
                append_unique(out, key)
    for key in keys:
        append_unique(out, key)
    return out


def select_caption_format(formats: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not formats:
        return None
    priorities = ["json3", "vtt", "srv3", "srv2", "srv1", "ttml", "srt"]
    for ext in priorities:
        for item in formats:
            if str(item.get("ext") or "").lower() == ext and (item.get("url") or item.get("data")):
                return item
    for item in formats:
        if item.get("url") or item.get("data"):
            return item
    return None


def download_caption(track: dict[str, Any], info: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    if track.get("data"):
        return {"text": str(track.get("data") or ""), "ext": str(track.get("ext") or "vtt").lower()}
    url = str(track.get("url") or "")
    if not url:
        raise RuntimeError("Caption Track hat keine URL.")
    timeout = int_param(config.get("request_timeout_s"), 25, 5, 180)
    headers = dict(info.get("http_headers") or {})
    headers.setdefault("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(20_000_000)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Caption Download HTTP {exc.code}")
    text = raw.decode("utf-8", errors="replace")
    return {"text": text, "ext": str(track.get("ext") or guess_caption_ext(url) or "vtt").lower()}


def parse_caption(text: str, ext: str) -> list[dict[str, Any]]:
    ext = (ext or "").lower()
    if ext == "json3" or text.lstrip().startswith("{"):
        return parse_json3(text)
    if ext in ("srv1", "srv2", "srv3", "xml") or text.lstrip().startswith("<"):
        parsed = parse_xml_caption(text)
        if parsed:
            return parsed
    if ext == "srt" or re.search(r"\n\d+\s*\n\d\d:\d\d:\d\d,\d{3}\s+-->", text[:1000]):
        return parse_srt(text)
    return parse_vtt(text)


def parse_json3(text: str) -> list[dict[str, Any]]:
    data = json.loads(text)
    out: list[dict[str, Any]] = []
    for event in data.get("events") or []:
        segs = event.get("segs") or []
        content = "".join(seg.get("utf8") or "" for seg in segs)
        content = clean_caption_text(content)
        if not content:
            continue
        start = float(event.get("tStartMs") or 0) / 1000.0
        dur = float(event.get("dDurationMs") or 0) / 1000.0
        out.append({"start": round(start, 3), "end": round(start + dur, 3) if dur else None, "text": content})
    return merge_caption_segments(out)


def parse_xml_caption(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except Exception:
        return out
    for elem in root.iter():
        tag = elem.tag.split("}", 1)[-1].lower()
        if tag not in ("text", "p"):
            continue
        content = clean_caption_text("".join(elem.itertext()))
        if not content:
            continue
        start = parse_float(elem.attrib.get("start"))
        dur = parse_float(elem.attrib.get("dur"))
        begin = elem.attrib.get("begin")
        end_attr = elem.attrib.get("end")
        if start is None and begin:
            start = parse_timestamp(begin)
        end = None
        if end_attr:
            end = parse_timestamp(end_attr)
        elif start is not None and dur is not None:
            end = start + dur
        out.append({"start": round(start or 0.0, 3), "end": round(end, 3) if end is not None else None, "text": content})
    return merge_caption_segments(out)


def parse_vtt(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    time_re = re.compile(r"(?P<start>(?:\d+:)?\d\d:\d\d[.,]\d{3})\s+-->\s+(?P<end>(?:\d+:)?\d\d:\d\d[.,]\d{3})")
    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if idx < 0:
            continue
        match = time_re.search(lines[idx])
        if not match:
            continue
        content = clean_caption_text(" ".join(lines[idx + 1 :]))
        if not content:
            continue
        out.append(
            {
                "start": round(parse_timestamp(match.group("start")) or 0.0, 3),
                "end": round(parse_timestamp(match.group("end")) or 0.0, 3),
                "text": content,
            }
        )
    return merge_caption_segments(out)


def parse_srt(text: str) -> list[dict[str, Any]]:
    return parse_vtt(text.replace(",", "."))


def merge_caption_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    prev_text = ""
    for seg in segments:
        text = clean_caption_text(seg.get("text") or "")
        if not text:
            continue
        # YouTube auto captions can repeat the same phrase in overlapping windows.
        if text == prev_text:
            continue
        if prev_text and text.startswith(prev_text + " "):
            text = text[len(prev_text) :].strip()
        if not text:
            continue
        item = {"start": float(seg.get("start") or 0), "text": text}
        if seg.get("end") is not None:
            item["end"] = float(seg.get("end") or 0)
        merged.append(item)
        prev_text = text
    return merged


def transcribe_audio(info: dict[str, Any], url: str, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    api_key = str(config.get("xai_api_key") or config.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("xAI STT Key fehlt. Setze youtube_transcript.xai_api_key auf api.xai oder einen Key.")
    duration = int(info.get("duration") or 0)
    max_duration = int_param(payload.get("max_audio_duration_s", config.get("max_audio_duration_s")), 3600, 60, 24 * 3600)
    if duration and duration > max_duration:
        raise RuntimeError(f"Video ist {duration}s lang, Limit fuer STT ist {max_duration}s.")
    audio_path = download_audio(url, info, config)
    try:
        result = call_xai_stt(audio_path, payload, config, api_key)
    finally:
        if not bool_param(config.get("keep_audio_files", False)):
            try:
                os.remove(audio_path)
            except OSError:
                pass
    text = str(result.get("text") or "").strip()
    words = result.get("words") if isinstance(result.get("words"), list) else []
    segments = words_to_segments(words) if words else [{"start": 0.0, "text": text}] if text else []
    selected = {
        "language": result.get("language") or payload.get("language") or config.get("stt_language") or "auto",
        "caption_source": "xai_stt",
        "ext": Path(audio_path).suffix.lstrip("."),
    }
    built = build_result(info, url, selected, segments, text, method="xai_stt")
    built["stt_duration_s"] = result.get("duration")
    return built


def download_audio(url: str, info: dict[str, Any], config: dict[str, Any]) -> str:
    yt_dlp = yt_dlp_bin(config)
    if not yt_dlp:
        raise RuntimeError("yt-dlp nicht gefunden.")
    timeout = int_param(config.get("request_timeout_s"), 25, 5, 180)
    audio_dir = module_home(config) / "youtube_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    video_id = safe_id(str(info.get("id") or "video")) or "video"
    outtmpl = str(audio_dir / f"{video_id}.%(ext)s")
    cmd = [
        yt_dlp,
        "-f",
        "bestaudio/best",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout",
        str(timeout),
        "-o",
        outtmpl,
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=max(timeout + 120, 240))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Audio-Download fehlgeschlagen ({proc.returncode}): {safe_truncate(err, 900)}")
    matches = sorted(audio_dir.glob(f"{video_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise RuntimeError("Audio-Download ergab keine Datei.")
    return str(matches[0])


def call_xai_stt(path: str, payload: dict[str, Any], config: dict[str, Any], api_key: str) -> dict[str, Any]:
    try:
        import requests
    except Exception as exc:
        raise RuntimeError(f"Python requests fehlt fuer xAI STT: {exc}")
    language = str(payload.get("language") or config.get("stt_language") or "auto").strip() or "auto"
    data = {"format": "true"}
    if language.lower() != "auto":
        data["language"] = language
    url = str(config.get("xai_stt_url") or "https://api.x.ai/v1/stt")
    mime = guess_audio_mime(path)
    with open(path, "rb") as fh:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (os.path.basename(path), fh, mime)},
            data=data,
            timeout=max(int_param(config.get("request_timeout_s"), 25, 5, 180), 120),
        )
    if response.status_code >= 400:
        raise RuntimeError(f"xAI STT HTTP {response.status_code}: {safe_truncate(response.text, 900)}")
    return response.json()


def words_to_segments(words: list[dict[str, Any]], window_s: float = 20.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    bucket: list[str] = []
    bucket_start: float | None = None
    bucket_end: float | None = None
    for word in words:
        text = str(word.get("word") or word.get("text") or "").strip()
        if not text:
            continue
        start = parse_float(word.get("start")) or parse_float(word.get("start_time")) or 0.0
        end = parse_float(word.get("end")) or parse_float(word.get("end_time")) or start
        if bucket_start is None:
            bucket_start = start
        if bucket and start - bucket_start >= window_s:
            out.append({"start": round(bucket_start, 3), "end": round(bucket_end or start, 3), "text": " ".join(bucket)})
            bucket = []
            bucket_start = start
        bucket.append(text)
        bucket_end = end
    if bucket:
        out.append({"start": round(bucket_start or 0.0, 3), "end": round(bucket_end or 0.0, 3), "text": " ".join(bucket)})
    return out


def build_result(
    info: dict[str, Any],
    url: str,
    selected: dict[str, Any],
    segments: list[dict[str, Any]],
    transcript: str,
    method: str,
) -> dict[str, Any]:
    return {
        "video_id": str(info.get("id") or ""),
        "url": canonical_video_url(info, url),
        "title": str(info.get("title") or ""),
        "channel": str(info.get("channel") or info.get("uploader") or ""),
        "channel_url": str(info.get("channel_url") or info.get("uploader_url") or ""),
        "duration_s": info.get("duration"),
        "upload_date": upload_date_iso(info.get("upload_date")),
        "language": normalize_caption_language(selected.get("language") or ""),
        "raw_caption_language": str(selected.get("language") or ""),
        "caption_source": str(selected.get("caption_source") or method),
        "caption_format": str(selected.get("ext") or ""),
        "method": method,
        "segment_count": len(segments),
        "transcript_chars": len(transcript),
        "transcript": transcript,
    }


def store_transcript_files(result: dict[str, Any], transcript: str, segments: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, str]:
    base = module_home(config) / "youtube_transcripts"
    base.mkdir(parents=True, exist_ok=True)
    video_id = safe_id(result.get("video_id") or "") or safe_id(hash_text(result.get("url") or "")) or "youtube"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{video_id}-{stamp}"
    json_path = base / f"{stem}.json"
    txt_path = base / f"{stem}.txt"
    payload = dict(result)
    payload["segments"] = segments
    payload["stored_at_utc"] = now_iso()
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(transcript.strip() + "\n")
    return {"stored_json": str(json_path), "stored_txt": str(txt_path)}


def load_cached_transcript(url: str, config: dict[str, Any]) -> tuple[dict[str, Any], str, list[dict[str, Any]]] | None:
    video_id = video_id_from_url(url)
    if not video_id:
        return None
    base = module_home(config) / "youtube_transcripts"
    if not base.exists():
        return None
    candidates = sorted(base.glob(f"{safe_id(video_id)}-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        transcript = str(data.get("transcript") or "").strip()
        txt_path = path.with_suffix(".txt")
        if not transcript and txt_path.exists():
            transcript = txt_path.read_text(encoding="utf-8", errors="replace").strip()
        if not transcript:
            continue
        segments = data.get("segments") if isinstance(data.get("segments"), list) else []
        result = {
            "video_id": str(data.get("video_id") or data.get("id") or video_id),
            "url": str(data.get("url") or f"https://www.youtube.com/watch?v={video_id}"),
            "title": str(data.get("title") or ""),
            "channel": str(data.get("channel") or data.get("uploader") or ""),
            "channel_url": str(data.get("channel_url") or data.get("uploader_url") or ""),
            "duration_s": data.get("duration_s") or data.get("duration"),
            "upload_date": data.get("upload_date") or "",
            "language": data.get("language") or "",
            "raw_caption_language": data.get("raw_caption_language") or data.get("language") or "",
            "caption_source": data.get("caption_source") or "cached_transcript",
            "caption_format": data.get("caption_format") or "",
            "method": data.get("method") or "captions",
            "segment_count": len(segments),
            "transcript_chars": len(transcript),
            "transcript": transcript,
            "stored_json": str(path),
            "stored_txt": str(txt_path) if txt_path.exists() else str(data.get("stored_txt") or ""),
            "cache_hit": True,
        }
        return result, transcript, segments
    return None


def info_from_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": result.get("video_id") or "",
        "title": result.get("title") or "",
        "channel": result.get("channel") or "",
        "uploader": result.get("channel") or "",
        "channel_url": result.get("channel_url") or "",
        "uploader_url": result.get("channel_url") or "",
        "duration": result.get("duration_s"),
        "upload_date": str(result.get("upload_date") or "").replace("-", ""),
    }


def store_rag_note(result: dict[str, Any], transcript: str, config: dict[str, Any]) -> tuple[str, str]:
    data_dir = str(config.get("data_dir") or "").strip()
    pool = safe_id(str(config.get("rag_pool") or "DeepDive").strip()) or "DeepDive"
    if not data_dir:
        return "", "RAG nicht gespeichert: data_dir fehlt."
    rag_dir = Path(data_dir) / "rag" / pool
    rag_dir.mkdir(parents=True, exist_ok=True)
    max_chars = int_param(config.get("max_rag_chars"), 90000, 1000, 500000)
    note = build_rag_note(result, safe_truncate(transcript, max_chars))
    entry_id = str(uuid4())
    entry = {
        "id": entry_id,
        "text": note,
        "timestamp": now_iso(),
        "keywords": keywords(note),
        "source_url": result.get("url") or "",
        "source_title": result.get("title") or "",
        "captured_at_utc": now_iso(),
        "source_language": result.get("language") or "",
        "source_country": "",
        "perspective_role": "youtube_video_transcript",
    }
    path = rag_dir / f"{entry_id}.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return entry_id[:8], f"Im RAG Pool '{pool}' gespeichert (id: {entry_id[:8]})"


def build_rag_note(result: dict[str, Any], transcript: str) -> str:
    lines = [
        "YOUTUBE_TRANSCRIPT_NOTE",
        f"captured_at_utc: {now_iso()}",
        f"source_url: {result.get('url') or ''}",
        f"source_title: {clean_line(result.get('title') or '')}",
        f"youtube_video_id: {result.get('video_id') or ''}",
        f"channel: {clean_line(result.get('channel') or '')}",
        f"channel_url: {result.get('channel_url') or ''}",
        f"upload_date: {result.get('upload_date') or ''}",
        f"duration_s: {result.get('duration_s') or ''}",
        f"language: {result.get('language') or ''}",
        f"transcript_method: {result.get('method') or ''}",
        f"caption_source: {result.get('caption_source') or ''}",
        f"caption_format: {result.get('caption_format') or ''}",
        f"segment_count: {result.get('segment_count') or 0}",
        f"transcript_chars: {result.get('transcript_chars') or len(transcript)}",
        f"stored_json: {result.get('stored_json') or ''}",
        f"stored_txt: {result.get('stored_txt') or ''}",
        "assessment_required: claims, timeline, speakers_if_inferred, source_leads, uncertainty; transcript may be auto-generated and must be treated as imperfect source material",
        "transcript:",
        transcript.strip(),
    ]
    return "\n".join(lines)


def format_result(result: dict[str, Any], transcript: str, segments: list[dict[str, Any]]) -> str:
    lines = [
        "YOUTUBE_TRANSCRIPT_SUCCESS",
        f"video_id: {result.get('video_id') or ''}",
        f"title: {clean_line(result.get('title') or '')}",
        f"channel: {clean_line(result.get('channel') or '')}",
        f"duration_s: {result.get('duration_s') or ''}",
        f"upload_date: {result.get('upload_date') or ''}",
        f"source_url: {result.get('url') or ''}",
        f"method: {result.get('method') or ''}",
        f"caption_source: {result.get('caption_source') or ''}",
        f"language: {result.get('language') or ''}",
        f"segments: {len(segments)}",
        f"transcript_chars: {len(transcript)}",
    ]
    if result.get("stored_json"):
        lines.append(f"stored_json: {result.get('stored_json')}")
    if result.get("stored_txt"):
        lines.append(f"stored_txt: {result.get('stored_txt')}")
    if result.get("rag_message"):
        lines.append(f"rag: {result.get('rag_message')}")
    if result.get("cache_hit"):
        lines.append("cache_hit: true")
    lines.extend(["", "transcript:"])
    lines.append(format_transcript(segments, transcript))
    return "\n".join(lines)


def format_transcript(segments: list[dict[str, Any]], transcript: str) -> str:
    if not segments:
        return transcript.strip()
    out = []
    for seg in segments:
        start = float(seg.get("start") or 0.0)
        text = clean_caption_text(seg.get("text") or "")
        if text:
            out.append(f"[{fmt_time(start)}] {text}")
    return "\n".join(out).strip() or transcript.strip()


def no_transcript_message(info: dict[str, Any], url: str, lang_prefs: list[str], error_note: str) -> str:
    manual = ", ".join((info.get("subtitles") or {}).keys()) or "none"
    auto = ", ".join(list((info.get("automatic_captions") or {}).keys())[:80]) or "none"
    return "\n".join(
        [
            "YOUTUBE_TRANSCRIPT_NO_CAPTIONS",
            f"video_id: {info.get('id', '')}",
            f"title: {clean_line(info.get('title', ''))}",
            f"source_url: {canonical_video_url(info, url)}",
            f"wanted_languages: {', '.join(lang_prefs)}",
            f"reason: {error_note}",
            f"creator_caption_languages: {manual}",
            f"auto_caption_languages: {auto}",
            "hint: Nutze youtube_transcript.captions(url) zum Pruefen oder youtube_transcript.transcribe({\"url\":\"...\"}) fuer xAI-STT, falls der vorhandene xAI-Key genutzt werden soll.",
        ]
    )


def summarize_caption_map(tracks: dict[str, Any], limit: int = 80) -> list[str]:
    rows = []
    keys = sorted(tracks.keys())
    for lang in keys[:limit]:
        formats = []
        for item in tracks.get(lang) or []:
            ext = str(item.get("ext") or "").lower()
            if ext and ext not in formats:
                formats.append(ext)
        rows.append(f"- {lang}: {', '.join(formats[:8]) or 'unknown'}")
    if len(keys) > limit:
        rows.append(f"- ... {len(keys) - limit} weitere Sprachen ausgeblendet")
    return rows


def parse_payload(params: list[str]) -> dict[str, Any]:
    if not params:
        return {}
    if len(params) == 1:
        raw = str(params[0] or "").strip()
        if not raw:
            return {}
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {"url": raw}
            except Exception:
                return {"url": raw}
        return {"url": raw}
    return {"url": params[0], "languages": params[1:]}


def payload_languages(payload: dict[str, Any], config: dict[str, Any]) -> list[str]:
    raw = payload.get("languages", payload.get("langs", payload.get("lang", payload.get("language"))))
    if raw is None:
        raw = config.get("preferred_languages") or "de,en,auto"
    if isinstance(raw, list):
        langs = [str(x).strip() for x in raw if str(x).strip()]
    else:
        langs = [x.strip() for x in re.split(r"[,;\s]+", str(raw)) if x.strip()]
    return langs or ["de", "en", "auto"]


def normalize_youtube_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return f"https://www.youtube.com/watch?v={raw}"
    if not re.match(r"^https?://", raw, re.I):
        return ""
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
    allowed = host == "youtu.be" or host.endswith(".youtube.com") or host == "youtube.com" or host.endswith(".youtube-nocookie.com")
    if not allowed:
        return ""
    if host == "youtu.be":
        vid = parsed.path.strip("/").split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or ""):
            return f"https://www.youtube.com/watch?v={vid}"
    if "/shorts/" in parsed.path:
        vid = parsed.path.split("/shorts/", 1)[1].split("/", 1)[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or ""):
            return f"https://www.youtube.com/watch?v={vid}"
    query = urllib.parse.parse_qs(parsed.query)
    vid = (query.get("v") or [""])[0]
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or ""):
        return f"https://www.youtube.com/watch?v={vid}"
    return raw


def video_id_from_url(value: Any) -> str:
    url = normalize_youtube_url(value)
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    vid = (query.get("v") or [""])[0]
    return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or "") else ""


def canonical_video_url(info: dict[str, Any], fallback: str) -> str:
    video_id = str(info.get("id") or "")
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return f"https://www.youtube.com/watch?v={video_id}"
    return fallback


def clean_caption_text(text: Any) -> str:
    value = html.unescape(str(text or ""))
    value = re.sub(r"<\d\d:\d\d:\d\d\.\d{3}>", " ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("\u200b", " ").replace("\ufeff", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def segments_to_text(segments: list[dict[str, Any]]) -> str:
    return "\n".join(clean_caption_text(seg.get("text") or "") for seg in segments if clean_caption_text(seg.get("text") or "")).strip()


def parse_timestamp(value: Any) -> float | None:
    raw = str(value or "").strip().replace(",", ".")
    if not raw:
        return None
    if raw.endswith("s") and re.fullmatch(r"\d+(?:\.\d+)?s", raw):
        return float(raw[:-1])
    parts = raw.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(raw)
    except Exception:
        return None


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def bool_param(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "ja", "on", "y")


def int_param(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        num = int(float(value))
    except Exception:
        num = default
    return max(minimum, min(maximum, num))


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def upload_date_iso(value: Any) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def normalize_caption_language(value: Any) -> str:
    raw = str(value or "").strip()
    match = re.match(r"^([a-z]{2,3})-[A-Za-z0-9_-]{8,}$", raw)
    if match:
        return match.group(1)
    return raw


def guess_caption_ext(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    for key in ("fmt", "format"):
        if qs.get(key):
            return str(qs[key][0]).lower()
    return Path(parsed.path).suffix.lstrip(".").lower()


def guess_audio_mime(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".wav": "audio/wav",
    }.get(ext, "application/octet-stream")


def module_home(config: dict[str, Any]) -> Path:
    home = str(config.get("home_dir") or "").strip()
    if home:
        return Path(home)
    data_dir = str(config.get("data_dir") or "").strip()
    if data_dir:
        return Path(data_dir) / "module-home" / "youtube_transcript"
    return Path.cwd() / "agent-data" / "module-home" / "youtube_transcript"


def video_source(info: dict[str, Any], url: str, rag_id: str = "") -> dict[str, Any]:
    if job_history is None:
        return {
            "source_type": "youtube_video",
            "source_url": canonical_video_url(info, url),
            "source_title": str(info.get("title") or ""),
            "source_id": str(info.get("id") or ""),
            "source_name": str(info.get("channel") or info.get("uploader") or ""),
            "rag_id": rag_id,
        }
    return job_history.source(
        source_type="youtube_video",
        source_url=canonical_video_url(info, url),
        source_title=str(info.get("title") or ""),
        source_id=str(info.get("id") or ""),
        source_name=str(info.get("channel") or info.get("uploader") or ""),
        published_at_utc=upload_date_iso(info.get("upload_date")),
        rag_id=rag_id,
        metadata={"duration_s": info.get("duration"), "channel_url": info.get("channel_url") or info.get("uploader_url") or ""},
    )


def start_job(tool: str, query: str, payload: dict[str, Any], config: dict[str, Any]) -> str:
    if job_history is None:
        return ""
    try:
        return job_history.start_job("youtube_transcript", tool, query=query, params=payload, config=config, task_id=str(config.get("task_id") or ""))
    except Exception:
        return ""


def finish_job(
    job_id: str,
    status: str,
    config: dict[str, Any],
    sources: list[dict[str, Any]],
    rag_ids: list[str],
    summary: str,
    error: str,
    metrics: dict[str, Any],
) -> None:
    if not job_id or job_history is None:
        return
    try:
        job_history.finish_job(job_id, status, config=config, sources=sources, rag_ids=rag_ids, summary=summary, error=error, metrics=metrics)
    except Exception:
        pass


def clean_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def safe_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._-")[:120]


def safe_truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 80)] + f"\n...[truncated {len(text) - limit} chars]..."


def limit_output(text: str, config: dict[str, Any]) -> str:
    limit = int_param(config.get("max_output_chars"), 50000, 1000, 300000)
    return safe_truncate(text, limit)


def append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_text(value: str) -> str:
    import hashlib

    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def keywords(text: str) -> list[str]:
    words = re.findall(r"[\wÄÖÜäöüß-]{3,}", text.lower())
    stop = {
        "der",
        "die",
        "das",
        "und",
        "oder",
        "mit",
        "von",
        "the",
        "and",
        "for",
        "you",
        "youtube",
        "transcript",
    }
    out: list[str] = []
    seen = set()
    for word in words:
        if word in stop or word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= 80:
            break
    return out


def help_text(config: dict[str, Any]) -> str:
    yt = yt_dlp_bin(config)
    return "\n".join(
        [
            "youtube_transcript Modul",
            "",
            "Tools:",
            '- youtube_transcript.fetch("https://www.youtube.com/watch?v=...")',
            '- youtube_transcript.fetch({"url":"...","languages":["de","en"],"store_rag":true})',
            '- youtube_transcript.captions("https://youtu.be/...")',
            '- youtube_transcript.transcribe({"url":"...","language":"de"})  # optional xAI-STT',
            '- youtube_transcript.to_rag({"url":"...","languages":"de,en,auto"})',
            "",
            "Strategie:",
            "- Kein YouTube API/OAuth Key.",
            "- Zuerst Creator-Untertitel, dann YouTube Auto-Captions.",
            "- Audio-STT nur explizit/optional und nur mit vorhandenem xAI-Key.",
            "",
            f"yt-dlp: {'OK: ' + yt if yt else 'FEHLT'}",
        ]
    )


def ok(data: str) -> dict[str, Any]:
    return {"success": True, "data": data}


def fail(data: str) -> dict[str, Any]:
    return {"success": False, "data": data}


if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            if req.get("action") == "describe":
                print(json.dumps(MODULE, ensure_ascii=False), flush=True)
            elif req.get("action") == "handle_tool":
                result = handle_tool(req.get("tool", ""), req.get("params", []), req.get("config", {}))
                print(json.dumps(result, ensure_ascii=False), flush=True)
            else:
                print(json.dumps({"error": f"Unknown action: {req.get('action')}"}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
