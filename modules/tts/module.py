"""Standalone text-to-speech tools for chat, Telegram and video workflows."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "agent-data" / "tts"


MODULE = {
    "name": "tts",
    "description": "Text-to-Speech als eigenes Tool-Modul fuer Grok/xAI, Qwen lokal/API und MiniMax.",
    "version": "1.0",
    "settings": {
        "enabled": {"type": "bool", "label": "Aktiv", "default": True},
        "python_timeout_s": {"type": "number", "label": "Python Prozess Timeout Sekunden", "default": 900},
        "provider": {"type": "select", "label": "Provider", "default": "xai", "options": ["xai", "piper", "qwen", "minimax", "off"]},
        "piper_tts_url": {"type": "string", "label": "Piper TTS URL", "default": ""},
        "piper_timeout_s": {"type": "number", "label": "Piper Timeout Sek", "default": 120},
        "piper_chunk_chars": {"type": "number", "label": "Piper Chunk-Groesse (0=aus)", "default": 600},
        "qwen_chunk_chars": {"type": "number", "label": "Qwen Chunk-Groesse Satz-fuer-Satz", "default": 220},
        "api_key": {"type": "password", "label": "xAI API Key/Alias", "default": "api.xai"},
        "api_base": {"type": "string", "label": "xAI API Base", "default": "https://api.x.ai"},
        "voice": {"type": "string", "label": "Default Voice", "default": "ara"},
        "language": {"type": "string", "label": "Default Sprache", "default": "de"},
        "fast": {"type": "bool", "label": "Schnell sprechen", "default": True},
        "output_dir": {"type": "string", "label": "Audio Output", "default": "agent-data/tts"},
        "request_timeout_s": {"type": "number", "label": "HTTP Timeout Sekunden", "default": 900},
        "tts_german_orthography": {"type": "bool", "label": "Deutsch-Orthografie normalisieren", "default": True},
        "qwen_tts_url": {"type": "string", "label": "Qwen TTS HTTP URL optional", "default": ""},
        "qwen_tts_api_key": {"type": "password", "label": "Qwen TTS API Key optional", "default": ""},
        "qwen_tts_model": {"type": "string", "label": "Qwen TTS Modell", "default": "qwen-tts"},
        "qwen_timeout_s": {"type": "number", "label": "Qwen TTS Timeout Sek (dann Fallback)", "default": 180},
        "fallback_provider": {"type": "select", "label": "Fallback-Provider bei Fehler", "default": "xai", "options": ["xai", "minimax", "off"]},
        "qwen_tts_command": {
            "type": "string",
            "label": "Qwen lokaler Command optional",
            "default": "",
        },
        "minimax_api_key": {"type": "password", "label": "MiniMax API Key", "default": ""},
        "minimax_api_base": {"type": "string", "label": "MiniMax API Base", "default": "https://api.minimax.io"},
        "minimax_tts_model": {"type": "string", "label": "MiniMax TTS Modell", "default": "speech-2.8-turbo"},
        "minimax_voice_id": {"type": "string", "label": "MiniMax Voice ID", "default": "German_Trustworth_Man"},
    },
    "tools": [
        {
            "name": "tts.speak",
            "description": "Erzeugt Audio aus Text. JSON {text,provider?,voice?,language?,fast?,speed?,out_dir?,filename?}. Gibt audio_path zurueck.",
            "params": ["query_json"],
        },
        {
            "name": "tts.prepare_text",
            "description": "Normalisiert Text fuer TTS ohne Audio zu erzeugen. JSON {text,max_chars?}.",
            "params": ["query_json"],
        },
        {
            "name": "tts.status",
            "description": "Zeigt TTS-Konfiguration und Provider-Verfuegbarkeit ohne Secrets.",
            "params": ["query_json"],
        },
        {
            "name": "tts.help",
            "description": "Zeigt Beispiele fuer Grok/xAI, Qwen und MiniMax TTS.",
            "params": [],
        },
    ],
}


def handle_tool(tool_name: str, params: Any, config: dict[str, Any]) -> dict[str, Any]:
    try:
        if not cfg_bool(config, "enabled", True):
            return fail("tts ist deaktiviert.")
        if tool_name == "tts.speak":
            return speak(params, config)
        if tool_name == "tts.prepare_text":
            payload = parse_payload(params)
            text = prepare_tts_text(first_text(payload, "text", "input", "script", "voice_script"), config)
            max_chars = int_param(payload.get("max_chars"), 0, 0, 200000)
            if max_chars and len(text) > max_chars:
                text = text[:max_chars].rstrip()
            return ok({"text": text, "chars": len(text)})
        if tool_name == "tts.status":
            return ok(status(config))
        if tool_name == "tts.help":
            return ok(help_text())
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"TTS_FAILED: {exc}")


def speak(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    text = first_text(payload, "text", "input", "script", "voice_script")
    if not text:
        return fail("text fehlt.")
    provider = first_text(payload, "provider") or str(config.get("provider") or config.get("tts_provider") or "xai")
    provider = provider.strip().lower()
    if provider == "off":
        return fail("TTS Provider ist off.")

    prepared = prepare_tts_text(text, config)
    out_dir = output_dir(payload, config)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_audio_filename(first_text(payload, "filename", "name") or safe_filename(prepared[:50]))
    out_path = out_dir / filename
    if out_path.suffix.lower() != ".mp3":
        out_path = out_path.with_suffix(".mp3")

    # Fallback-Kette: primaerer Provider, dann (bei Fehler/Timeout) der
    # konfigurierte Fallback. So killt eine langsame/ausgefallene lokale TTS
    # (z.B. Qwen auf schwacher GPU bei langen Skripten) nie den ganzen Workflow.
    fallback = str(config.get("fallback_provider") or payload.get("fallback_provider") or "xai").strip().lower()
    chain = [provider]
    if fallback and fallback not in ("off", provider):
        chain.append(fallback)
    used_provider = provider
    errors = []
    for i, prov in enumerate(chain):
        try:
            _dispatch_tts(prov, prepared, out_path, payload, config)
            if out_path.exists() and out_path.stat().st_size > 0:
                used_provider = prov
                break
            errors.append(f"{prov}: leere Audiodatei")
        except Exception as exc:
            errors.append(f"{prov}: {str(exc)[:160]}")
        if i + 1 < len(chain):
            # Fallback ankuendigen (geht in den Task-Log/Result)
            pass
    if not out_path.exists() or out_path.stat().st_size <= 0:
        return fail("TTS fehlgeschlagen (inkl. Fallback): " + " | ".join(errors))
    provider = used_provider
    duration_s = audio_duration(out_path, config) or estimate_duration(prepared)
    return ok(
        {
            "type": "tts_audio",
            "provider": provider,
            "audio_path": str(out_path),
            "duration_s": duration_s,
            "voice": first_text(payload, "voice", "voice_id") or default_voice(provider, config),
            "language": first_text(payload, "language", "lang") or str(config.get("language") or config.get("xai_tts_language") or "de"),
            "chars": len(prepared),
        }
    )


def _dispatch_tts(provider: str, text: str, out_path: Path, payload: dict[str, Any], config: dict[str, Any]) -> None:
    if provider == "xai":
        synthesize_xai(text, out_path, payload, config)
    elif provider == "qwen":
        synthesize_qwen(text, out_path, payload, config)
    elif provider == "piper":
        synthesize_piper(text, out_path, payload, config)
    elif provider == "minimax":
        synthesize_minimax(text, out_path, payload, config)
    else:
        raise RuntimeError(f"Unbekannter TTS Provider: {provider}")


def synthesize_xai(text: str, out_path: Path, payload: dict[str, Any], config: dict[str, Any]) -> None:
    api_key = first_text(payload, "api_key") or str(config.get("api_key") or config.get("xai_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("xAI API Key fuer TTS fehlt")
    base = (first_text(payload, "api_base") or str(config.get("api_base") or config.get("xai_api_base") or "https://api.x.ai")).rstrip("/")
    body = {
        "text": xai_speech_text(text, payload, config),
        "voice_id": first_text(payload, "voice", "voice_id") or default_voice("xai", config),
        "language": first_text(payload, "language", "lang") or str(config.get("language") or config.get("xai_tts_language") or "de"),
        "output_format": {"codec": "mp3", "sample_rate": 24000, "bit_rate": 64000},
    }
    content = http_post_json_bytes(f"{base}/v1/tts", body, api_key, timeout_s(config, payload))
    out_path.write_bytes(content)


def synthesize_qwen(text: str, out_path: Path, payload: dict[str, Any], config: dict[str, Any]) -> None:
    command = first_text(payload, "qwen_tts_command", "command") or str(config.get("qwen_tts_command") or "").strip()
    if command:
        text_file = out_path.with_suffix(".txt")
        text_file.write_text(text, encoding="utf-8")
        values = {
            "text": text,
            "text_file": str(text_file),
            "out": str(out_path),
            "voice": first_text(payload, "voice", "voice_id") or default_voice("qwen", config),
            "language": first_text(payload, "language", "lang") or str(config.get("language") or "de"),
            "speed": str(payload.get("speed") or ("1.18" if is_fast(payload, config) else "1.0")),
        }
        args = [part.format(**values) for part in shlex.split(command)]
        proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout_s(config, payload), check=False)
        if proc.returncode != 0:
            tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-3000:]
            raise RuntimeError(f"Qwen TTS Command fehlgeschlagen ({proc.returncode}): {tail}")
        return

    url = first_text(payload, "qwen_tts_url", "url") or str(config.get("qwen_tts_url") or "").strip()
    if not url:
        raise RuntimeError("Qwen TTS ist nicht konfiguriert: qwen_tts_command oder qwen_tts_url fehlt")
    api_key = first_text(payload, "qwen_tts_api_key") or str(config.get("qwen_tts_api_key") or "").strip()
    body = {
        "model": first_text(payload, "model") or str(config.get("qwen_tts_model") or "qwen-tts"),
        "input": text,
        "voice": first_text(payload, "voice", "voice_id") or default_voice("qwen", config),
        "response_format": "mp3",
        "speed": float_param(payload.get("speed"), 1.18 if is_fast(payload, config) else 1.0, 0.5, 2.0),
    }
    # Lokale Qwen-TTS auf schwacher GPU ist bei langen Skripten extrem
    # langsam — daher chunked (Satz fuer Satz) und mit harter Obergrenze pro
    # Chunk, statt 900s am Stueck zu haengen.
    qwen_to = int_param(config.get("qwen_timeout_s"), 180, 20, 900)
    chunk_chars = int_param(config.get("qwen_chunk_chars"), 220, 0, 2000)
    if chunk_chars and len(text) > chunk_chars:
        synth_chunked(lambda t, op: write_audio_response(
            http_post_json_bytes(url, {**body, "input": t}, api_key, qwen_to), op),
            text, out_path, chunk_chars, config)
    else:
        write_audio_response(http_post_json_bytes(url, body, api_key, qwen_to), out_path)


def synthesize_piper(text: str, out_path: Path, payload: dict[str, Any], config: dict[str, Any]) -> None:
    url = first_text(payload, "piper_tts_url", "url") or str(config.get("piper_tts_url") or "").strip()
    if not url:
        raise RuntimeError("Piper TTS ist nicht konfiguriert: piper_tts_url fehlt")
    to = int_param(config.get("piper_timeout_s"), 120, 10, 600)
    chunk_chars = int_param(config.get("piper_chunk_chars"), 600, 0, 4000)
    if chunk_chars and len(text) > chunk_chars:
        synth_chunked(lambda t, op: write_audio_response(
            http_post_json_bytes(url, {"input": t, "response_format": "mp3"}, "", to), op),
            text, out_path, chunk_chars, config)
    else:
        write_audio_response(http_post_json_bytes(url, {"input": text, "response_format": "mp3"}, "", to), out_path)


def split_into_chunks(text: str, max_chars: int) -> list[str]:
    """Satzweise zu Chunks <= max_chars buendeln (dein 'Stueck fuer Stueck')."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks: list[str] = []
    cur = ""
    for s in sentences:
        if not s:
            continue
        if cur and len(cur) + len(s) + 1 > max_chars:
            chunks.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        chunks.append(cur)
    return chunks or [text]


def synth_chunked(synth_one, text: str, out_path: Path, max_chars: int, config: dict[str, Any]) -> None:
    """Synthetisiert Chunks einzeln und konkateniert die MP3s per ffmpeg."""
    chunks = split_into_chunks(text, max_chars)
    tmp_dir = out_path.parent
    parts: list[Path] = []
    try:
        for i, chunk in enumerate(chunks):
            part = tmp_dir / f".tts_part_{os.getpid()}_{i}.mp3"
            synth_one(chunk, part)
            if not part.exists() or part.stat().st_size <= 0:
                raise RuntimeError(f"Chunk {i+1}/{len(chunks)} lieferte kein Audio")
            parts.append(part)
        # ffmpeg concat (re-encode fuer saubere Uebergaenge)
        listfile = tmp_dir / f".tts_concat_{os.getpid()}.txt"
        listfile.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        ff = ffmpeg_bin(config)
        proc = subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(listfile), "-codec:a", "libmp3lame", "-b:a", "160k", str(out_path)],
            cwd=str(tmp_dir), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"Audio-concat fehlgeschlagen: {proc.stderr[-300:]}")
    finally:
        for pp in parts:
            try: pp.unlink()
            except Exception: pass
        try: (tmp_dir / f".tts_concat_{os.getpid()}.txt").unlink()
        except Exception: pass


def synthesize_minimax(text: str, out_path: Path, payload: dict[str, Any], config: dict[str, Any]) -> None:
    api_key = first_text(payload, "minimax_api_key", "api_key") or str(config.get("minimax_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("MiniMax API Key fuer TTS fehlt")
    base = (first_text(payload, "minimax_api_base", "api_base") or str(config.get("minimax_api_base") or "https://api.minimax.io")).rstrip("/")
    body = {
        "model": first_text(payload, "model") or str(config.get("minimax_tts_model") or "speech-2.8-turbo"),
        "text": text,
        "stream": False,
        "language_boost": "auto",
        "output_format": "hex",
        "voice_setting": {
            "voice_id": first_text(payload, "voice", "voice_id") or str(config.get("minimax_voice_id") or "German_Trustworth_Man"),
            "speed": float_param(payload.get("speed"), 1.18 if is_fast(payload, config) else 1.0, 0.5, 2.0),
            "vol": 1,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }
    raw = http_post_json_bytes(f"{base}/v1/t2a_v2", body, api_key, timeout_s(config, payload))
    data = json.loads(raw.decode("utf-8", errors="replace"))
    audio_hex = ((data.get("data") or {}).get("audio") or "").strip()
    if not audio_hex:
        raise RuntimeError("MiniMax TTS lieferte kein Audio")
    out_path.write_bytes(bytes.fromhex(audio_hex))


def http_post_json_bytes(url: str, body: dict[str, Any], api_key: str = "", timeout: int = 120) -> bytes:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"HTTP {exc.code} von {url}: {detail}") from exc


def write_audio_response(content: bytes, out_path: Path) -> None:
    stripped = content.strip()
    if stripped.startswith(b"{"):
        data = json.loads(stripped.decode("utf-8", errors="replace"))
        audio = ((data.get("data") or {}).get("audio") or data.get("audio") or data.get("audio_base64") or "").strip()
        if not audio:
            raise RuntimeError("TTS API lieferte JSON ohne Audio")
        try:
            out_path.write_bytes(base64.b64decode(audio))
        except Exception:
            out_path.write_bytes(bytes.fromhex(audio))
        return
    out_path.write_bytes(content)


def xai_speech_text(text: str, payload: dict[str, Any], config: dict[str, Any]) -> str:
    if is_fast(payload, config):
        return f"<fast>{text}</fast>"
    return text


def prepare_tts_text(text: str, config: dict[str, Any]) -> str:
    text = re.sub(r"\s+", " ", str(text or "").strip())
    if cfg_bool(config, "tts_german_orthography", True):
        text = normalize_german_orthography_for_tts(text)
    return text


def normalize_german_orthography_for_tts(text: str) -> str:
    replacements = {
        "Aeusserst": "Äußerst",
        "aeusserst": "äußerst",
        "ausserst": "äußerst",
        "ausser": "außer",
        "Ausser": "Außer",
        "Aussen": "Außen",
        "aussen": "außen",
        "Aussenministerium": "Außenministerium",
        "Aussenpolitik": "Außenpolitik",
        "aussenpolitisch": "außenpolitisch",
        "Praesident": "Präsident",
        "Praesidenten": "Präsidenten",
        "Noetig": "Nötig",
        "Oel": "Öl",
        "Oekonomie": "Ökonomie",
    }
    for src, dst in replacements.items():
        text = re.sub(rf"\b{re.escape(src)}\b", dst, text)
    return text


def audio_duration(path: Path, config: dict[str, Any]) -> float:
    try:
        proc = subprocess.run(
            [ffmpeg_bin(config), "-hide_banner", "-i", str(path), "-f", "null", "-"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        text = (proc.stderr or "") + "\n" + (proc.stdout or "")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if match:
            h, m, s = match.groups()
            return round(int(h) * 3600 + int(m) * 60 + float(s), 3)
    except Exception:
        pass
    return 0.0


def ffmpeg_bin(config: dict[str, Any]) -> str:
    configured = str(config.get("ffmpeg_bin") or "").strip()
    if configured:
        return configured
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or "ffmpeg"


def estimate_duration(text: str) -> float:
    words = len(re.findall(r"\w+", text or ""))
    return max(3.0, min(7200.0, round(words / 2.35 + 1.0, 2)))


def output_dir(payload: dict[str, Any], config: dict[str, Any]) -> Path:
    raw = first_text(payload, "out_dir", "output_dir", "out") or str(config.get("output_dir") or "").strip()
    if not raw:
        return DEFAULT_OUT
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def status(config: dict[str, Any]) -> dict[str, Any]:
    provider = str(config.get("provider") or config.get("tts_provider") or "xai").lower()
    return {
        "provider": provider,
        "xai_key_configured": bool(str(config.get("api_key") or config.get("xai_api_key") or "").strip()),
        "qwen_command_configured": bool(str(config.get("qwen_tts_command") or "").strip()),
        "qwen_url_configured": bool(str(config.get("qwen_tts_url") or "").strip()),
        "minimax_key_configured": bool(str(config.get("minimax_api_key") or "").strip()),
        "default_voice": default_voice(provider, config),
        "output_dir": str(output_dir({}, config)),
        "ffmpeg": ffmpeg_bin(config),
    }


def help_text() -> dict[str, Any]:
    return {
        "speak": 'tts.speak({"text":"Hallo Welt","provider":"xai","voice":"ara","language":"de","fast":true})',
        "qwen_command": "Setze qwen_tts_command z.B. auf: qwen-tts --text-file {text_file} --voice {voice} --out {out}",
        "qwen_http": "Setze qwen_tts_url auf einen OpenAI-kompatiblen /v1/audio/speech Endpoint.",
        "video_workflow": "workflow_trigger nutzt tts.speak vor video_pipeline.briefing_video, damit keine stummen Produktionsvideos entstehen.",
    }


def default_voice(provider: str, config: dict[str, Any]) -> str:
    if provider == "minimax":
        return str(config.get("minimax_voice_id") or "German_Trustworth_Man")
    if provider == "qwen":
        return str(config.get("qwen_voice") or config.get("voice") or "Cherry")
    return str(config.get("voice") or config.get("xai_tts_voice_id") or "ara")


def is_fast(payload: dict[str, Any], config: dict[str, Any]) -> bool:
    if "fast" in payload:
        return bool_param(payload.get("fast"), True)
    if "xai_tts_fast" in payload:
        return bool_param(payload.get("xai_tts_fast"), True)
    return cfg_bool(config, "fast", cfg_bool(config, "xai_tts_fast", True))


def timeout_s(config: dict[str, Any], payload: dict[str, Any]) -> int:
    return int_param(payload.get("timeout_s") or payload.get("timeout"), cfg_int(config, "request_timeout_s", 120), 5, 1800)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    if not cleaned:
        digest = hashlib.sha256(str(time.time()).encode()).hexdigest()[:10]
        cleaned = f"tts_{digest}"
    return cleaned[:90]


def safe_audio_filename(value: str) -> str:
    value = safe_filename(value)
    if not value.endswith(".mp3"):
        value += ".mp3"
    return value


def parse_payload(params: Any) -> dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, dict):
        return dict(params)
    if isinstance(params, str):
        return parse_jsonish(params)
    if isinstance(params, list):
        if not params:
            return {}
        if len(params) == 1:
            item = params[0]
            if isinstance(item, dict):
                return dict(item)
            if isinstance(item, str):
                return parse_jsonish(item)
        out: dict[str, Any] = {}
        for idx, item in enumerate(params):
            if isinstance(item, dict):
                out.update(item)
            elif isinstance(item, str) and ":" in item:
                key, value = item.split(":", 1)
                out[key.strip()] = value.strip()
            else:
                out[f"arg{idx + 1}"] = item
        return out
    return {}


def parse_jsonish(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    for prefix in ("query_json:", "json:", "params:", "payload:"):
        if text.lower().startswith(prefix):
            text = text.split(":", 1)[1].strip()
            break
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        return data if isinstance(data, dict) else {"items": data}
    return {"text": text}


def first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def cfg_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    return bool_param(config.get(key), default)


def cfg_int(config: dict[str, Any], key: str, default: int) -> int:
    return int_param(config.get(key), default)


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
