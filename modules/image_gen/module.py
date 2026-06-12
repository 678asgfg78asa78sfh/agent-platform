"""Image-Generation-Modul — szenen-passende KI-Bilder fuer die Video-Pipeline.

Provider: xAI Grok Imagine (grok-imagine-image, $0.02/Bild laut Preisliste).
Kernidee: der Normalizer plant pro Szene einen "image_prompt"; dieses Modul
generiert die Bilder mit festem Style-Suffix (Marken-Look), cached per
SHA256(prompt+model) in agent-data/image_cache (Re-Renders und Repairs
kosten nichts erneut) und schreibt die Pfade als scene.image zurueck in
die video_assets.json — der Infografik-Renderer legt sie als Ken-Burns-
Hintergrund unter die Szenen.

Budget-Schutz: max_images_per_call kappt jeden Lauf; ohne api_key oder bei
Fehlern faellt die Pipeline einfach auf den bildlosen Look zurueck (Bilder
sind Enhancement, nie Blocker).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "agent-data" / "image_cache"

DEFAULT_STYLE = (
    "Flat vector illustration, dark navy blue background, golden accent "
    "lighting, minimalist geometric style, subtle glow, high contrast, "
    "no text, no letters, no words, 16:9 wide composition"
)

# Frei-Stil fuer generische Bild-Requests (image_gen.request) — kein Marken-Look,
# nur Qualitaets-Hinweise; der User-Prompt bestimmt den Look.
FREE_STYLE = (
    "high quality, sharp focus, detailed, professional lighting, "
    "no text, no watermark, no letters"
)

# Komposition/Seitenverhaeltnis als Prompt-Hinweis (xAI Imagine hat keinen
# size-Parameter — Orientierung wird ueber den Prompt gesteuert).
ASPECTS = {
    "16:9": "wide 16:9 horizontal cinematic composition",
    "1:1": "square 1:1 composition",
    "9:16": "tall vertical 9:16 portrait composition",
    "4:3": "4:3 composition",
    "3:2": "3:2 photographic composition",
}

MODULE = {
    "name": "image_gen",
    "description": "Generiert szenen-passende KI-Bilder (xAI Grok Imagine) fuer Video-Szenen, mit Cache und Budget-Kappe.",
    "version": "1.0",
    "settings": {
        "enabled": {"type": "bool", "label": "Aktiv", "default": True},
        "api_key": {"type": "password", "label": "xAI API Key (api.xai Alias ok)", "default": "api.xai"},
        "api_base": {"type": "string", "label": "API Base", "default": "https://api.x.ai"},
        "model": {"type": "string", "label": "Bildmodell", "default": "grok-imagine-image"},
        "style_suffix": {"type": "string", "label": "Style-Suffix (Marken-Look)", "default": DEFAULT_STYLE},
        "max_images_per_call": {"type": "number", "label": "Max Bilder pro Lauf (Budget-Kappe)", "default": 10},
        "request_timeout_s": {"type": "number", "label": "HTTP Timeout Sekunden", "default": 120},
        "python_timeout_s": {"type": "number", "label": "Python Timeout Sekunden", "default": 900},
        "price_per_image_usd": {"type": "number", "label": "Preis pro Bild (Reporting)", "default": 0.02},
    },
    "tools": [
        {
            "name": "image_gen.request",
            "description": (
                "Erzeugt 1-4 KI-Bilder zu einem freien Prompt und legt sie in die Medien-Galerie. "
                "JSON {prompt, count?=1, aspect?=16:9|1:1|9:16|4:3, no_style?, brand?, out_dir?}. "
                "Default freier Look (brand=true erzwingt den Marken-Stil). Liefert Galerie-Pfade + Kosten."
            ),
            "params": ["query_json"],
        },
        {
            "name": "image_gen.create",
            "description": "Erzeugt EIN Bild (Marken-Stil, Szenen-Helper). JSON {prompt, out_path?, model?, no_style?}. Liefert Pfad (Cache-Hit kostenlos).",
            "params": ["query_json"],
        },
        {
            "name": "image_gen.for_assets",
            "description": "Generiert Bilder fuer alle Szenen mit image_prompt in einer video_assets.json und schreibt scene.image-Pfade zurueck. JSON {assets_json_path, out_dir?, max_images?}.",
            "params": ["query_json"],
        },
        {
            "name": "image_gen.status",
            "description": "Zeigt Konfiguration, Cache-Statistik und Preis-Schaetzung.",
            "params": [],
        },
    ],
}


def handle_tool(tool_name: str, params: Any, config: dict[str, Any]) -> dict[str, Any]:
    try:
        if not bool_param(config.get("enabled"), True):
            return fail("image_gen ist deaktiviert.")
        if tool_name == "image_gen.request":
            return request(params, config)
        if tool_name == "image_gen.create":
            return create_one(params, config)
        if tool_name == "image_gen.for_assets":
            return for_assets(params, config)
        if tool_name == "image_gen.status":
            return status(config)
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"IMAGE_GEN_FAILED: {exc}")


def create_one(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    prompt = str(payload.get("prompt") or payload.get("query") or "").strip()
    if not prompt:
        return fail('prompt fehlt. Beispiel: {"prompt":"data center server racks"}')
    no_style = bool_param(payload.get("no_style"), False)
    model = str(payload.get("model") or config.get("model") or "grok-imagine-image")
    path, cached, err = generate_cached(prompt, model, config, no_style)
    if err:
        return fail(err)
    out_path = str(payload.get("out_path") or "").strip()
    if out_path:
        dest = resolve_path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(path).read_bytes())
        path = str(dest)
    return ok({"image": path, "cached": cached, "model": model,
               "cost_usd": 0.0 if cached else float_param(config.get("price_per_image_usd"), 0.02)})


def request(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Freier Bild-Request: 1-4 Bilder zu einem Prompt in die Galerie.

    Zielverzeichnis: explizit `out_dir` (vom UI-Task = Chat-Galerie) ODER
    `<home_dir>/images` (Modul-Home, das die Medien-Galerie scannt). Wird
    direkt vom Chat-LLM ODER ueber POST /api/image/start aufgerufen.
    """
    payload = parse_payload(params)
    prompt = str(payload.get("prompt") or payload.get("query") or "").strip()
    if not prompt:
        return fail('prompt fehlt. Beispiel: {"prompt":"a cozy reading nook at golden hour"}')

    cap = int_param(config.get("max_images_per_call"), 10, 1, 50)
    count = int_param(payload.get("count"), 1, 1, min(4, cap))
    no_style = bool_param(payload.get("no_style"), False)
    brand = bool_param(payload.get("brand"), False)
    aspect = str(payload.get("aspect") or "16:9").strip()
    comp = ASPECTS.get(aspect, ASPECTS["16:9"])
    model = str(payload.get("model") or config.get("model") or "grok-imagine-image")

    out_dir = resolve_path(str(payload.get("out_dir") or ""))
    if out_dir is None:
        home = str(config.get("home_dir") or "").strip()
        base = Path(home) if home else (ROOT / "agent-data")
        out_dir = base / "images"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Stil: brand=Marken-Look, sonst freier Look; in beiden Faellen die
    # gewuenschte Komposition. no_style schaltet jeden Stil ab.
    if no_style:
        style: str | None = ""
    else:
        base_style = str(config.get("style_suffix") or DEFAULT_STYLE) if brand else FREE_STYLE
        style = build_style(base_style, comp)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = slugify(prompt) or "bild"
    price = float_param(config.get("price_per_image_usd"), 0.02)
    images: list[str] = []
    errors: list[str] = []
    cost = 0.0
    for i in range(1, count + 1):
        # Bei count>1 leichte Varianz, damit Cache-Keys UND Bilder differieren.
        vseed = "" if count == 1 else f" — variation {i}"
        path, cached, err = generate_cached(prompt + vseed, model, config, no_style, style_override=style)
        if err:
            errors.append(f"Bild {i}: {err}")
            continue
        name = f"{slug}-{stamp}.png" if count == 1 else f"{slug}-{stamp}-{i}.png"
        dest = out_dir / name
        dest.write_bytes(Path(path).read_bytes())
        images.append(str(dest))
        if not cached:
            cost += price

    if not images:
        detail = (" " + " | ".join(errors[:3])) if errors else ""
        return fail("Kein Bild erzeugt." + detail)

    meta = {
        "prompt": prompt, "model": model, "aspect": aspect, "count": len(images),
        "brand": brand, "no_style": no_style, "created": stamp,
        "cost_usd": round(cost, 3), "images": [Path(p).name for p in images],
    }
    try:
        (out_dir / f"{slug}-{stamp}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    gallery = [r for r in (gallery_rel(p, config) for p in images) if r]
    return ok({
        "type": "image_gen.request",
        "prompt": prompt,
        "aspect": aspect,
        "count": len(images),
        "images": images,
        "gallery": gallery,
        "out_dir": str(out_dir),
        "cost_usd": round(cost, 3),
        "errors": errors[:3],
    })


def for_assets(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    assets_path = resolve_path(str(payload.get("assets_json_path") or payload.get("assets_path") or ""))
    if not assets_path or not assets_path.exists():
        return fail(f"assets_json_path nicht gefunden: {assets_path}")
    assets = json.loads(assets_path.read_text(encoding="utf-8"))
    scenes = assets.get("scenes") or []
    max_images = int_param(payload.get("max_images"), int_param(config.get("max_images_per_call"), 10, 0, 50), 0, 50)
    model = str(config.get("model") or "grok-imagine-image")
    out_dir = resolve_path(str(payload.get("out_dir") or "")) or assets_path.parent / "scene_images"
    out_dir.mkdir(parents=True, exist_ok=True)

    generated = 0
    cached_hits = 0
    skipped = 0
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    for idx, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        prompt = str(scene.get("image_prompt") or "").strip()
        # Karte braucht kein Bild (sie IST das Visual); ohne Prompt kein Bild.
        if not prompt or str(scene.get("type") or "").lower() == "map":
            skipped += 1
            continue
        if generated >= max_images:
            skipped += 1
            errors.append(f"Szene {idx}: Budget-Kappe max_images={max_images} erreicht.")
            continue
        path, was_cached, err = generate_cached(prompt, model, config, no_style=False)
        if err:
            errors.append(f"Szene {idx}: {err}")
            continue
        # Stabile Kopie im Workflow-Verzeichnis (Cache kann rotieren)
        dest = out_dir / f"scene_{idx:02d}.png"
        dest.write_bytes(Path(path).read_bytes())
        scene["image"] = str(dest)
        results.append({"scene": idx, "title": scene.get("title"), "image": str(dest), "cached": was_cached})
        if was_cached:
            cached_hits += 1
        else:
            generated += 1
    assets["scenes"] = scenes
    tmp = assets_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(assets, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(assets_path)
    price = float_param(config.get("price_per_image_usd"), 0.02)
    return ok({
        "type": "image_gen.for_assets",
        "assets_json_path": str(assets_path),
        "generated": generated,
        "cached_hits": cached_hits,
        "skipped": skipped,
        "cost_usd_estimate": round(generated * price, 3),
        "images": results,
        "errors": errors[:6],
    })


def generate_cached(prompt: str, model: str, config: dict[str, Any], no_style: bool,
                    style_override: str | None = None) -> tuple[str, bool, str]:
    if no_style:
        style = ""
    elif style_override is not None:
        style = str(style_override).strip()
    else:
        style = str(config.get("style_suffix") or DEFAULT_STYLE).strip()
    full_prompt = (prompt.rstrip(". ") + ". " + style).strip() if style else prompt
    key = hashlib.sha256(f"{model}|{full_prompt}".encode("utf-8")).hexdigest()[:32]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{key}.png"
    if cache_path.exists() and cache_path.stat().st_size > 1000:
        return str(cache_path), True, ""
    api_key = str(config.get("api_key") or "").strip()
    if not api_key or api_key.startswith("api."):
        return "", False, "api_key fehlt/unaufgeloest — Bilder uebersprungen."
    api_base = str(config.get("api_base") or "https://api.x.ai").rstrip("/")
    timeout = int_param(config.get("request_timeout_s"), 120, 10, 600)
    body = {"model": model, "prompt": full_prompt, "n": 1, "response_format": "b64_json"}
    data = None
    last_err = ""
    # 5xx/Transport sind bei Bildgeneratoren oft transient — ein Retry genuegt meist.
    for attempt in (1, 2):
        req = urllib.request.Request(
            api_base + "/v1/images/generations",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            break
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}"
            if exc.code < 500 or attempt == 2:
                return "", False, last_err
        except Exception as exc:
            last_err = f"request failed: {exc}"
            if attempt == 2:
                return "", False, last_err
        time.sleep(2.5)
    if data is None:
        return "", False, last_err or "keine Antwort"
    item = (data.get("data") or [{}])[0]
    raw = b""
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        try:
            with urllib.request.urlopen(item["url"], timeout=timeout) as r2:
                raw = r2.read()
        except Exception as exc:
            return "", False, f"image download failed: {exc}"
    if len(raw) < 1000:
        return "", False, "Antwort enthielt kein Bild."
    cache_path.write_bytes(raw)
    return str(cache_path), False, ""


def status(config: dict[str, Any]) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    files = list(CACHE_DIR.glob("*.png"))
    return ok({
        "model": config.get("model") or "grok-imagine-image",
        "api_base": config.get("api_base") or "https://api.x.ai",
        "api_key_set": bool(str(config.get("api_key") or "").strip()) and not str(config.get("api_key") or "").startswith("api."),
        "cache_images": len(files),
        "cache_mb": round(sum(f.stat().st_size for f in files) / 1e6, 1),
        "price_per_image_usd": float_param(config.get("price_per_image_usd"), 0.02),
        "max_images_per_call": int_param(config.get("max_images_per_call"), 10, 0, 50),
    })


# ─── Helpers ──────────────────────────────────────────────────────────────
def slugify(text: str, max_len: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:max_len].rstrip("-")


def build_style(base_style: str, comp: str) -> str:
    """Komposition robust setzen: bestehende 16:9-Phrase aus dem Basis-Stil
    entfernen und die gewuenschte anhaengen (sonst widersprechen sie sich)."""
    base = str(base_style or "").strip()
    base = re.sub(r",?\s*16:9[^,]*composition\.?", "", base, flags=re.IGNORECASE).strip(" ,.")
    return f"{base}, {comp}" if base else comp


def gallery_rel(abs_path: str, config: dict[str, Any]) -> str | None:
    """Pfad relativ zum Modul-Home (fuer /api/home/<modul>/<rel>-Anzeige).
    None, wenn das Bild ausserhalb des Home liegt (UI-Task schreibt in fremdes
    Chat-Home — dann uebernimmt die server-seitige Medien-Galerie die Anzeige)."""
    home = str(config.get("home_dir") or "").strip()
    if not home:
        return None
    try:
        return str(Path(abs_path).relative_to(Path(home)))
    except Exception:
        return None


def parse_payload(params: Any) -> dict[str, Any]:
    if isinstance(params, dict):
        return params
    if isinstance(params, list) and params:
        item = params[0]
        if isinstance(item, dict):
            return item
        text = str(item or "").strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {"prompt": text} if text else {}
    return {}


def resolve_path(value: str) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    return p


def bool_param(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on"}


def int_param(value: Any, default: int, min_v: int | None = None, max_v: int | None = None) -> int:
    try:
        out = int(float(value))
    except Exception:
        out = default
    if min_v is not None:
        out = max(min_v, out)
    if max_v is not None:
        out = min(max_v, out)
    return out


def float_param(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def ok(data: Any) -> dict[str, Any]:
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, indent=2)
    return {"success": True, "data": data}


def fail(data: Any) -> dict[str, Any]:
    return {"success": False, "data": str(data)}


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
