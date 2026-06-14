#!/usr/bin/env python3
"""Infographic motion renderer v2 — szenenbasierte Erklaervideo-Engine.

v2 nach Feedback-Runde:
- Preview ist jetzt echtes 720p25 (vorher 480p12 — sah "lowend" aus),
  Vollrender 1080p25 mit 2x Supersampling.
- Timing nach Motion-Design-Standard (Material/NNg): Element-Eintritte
  350-550 ms ease-out, Szenen-Transition 400 ms, Counter ~900 ms. Vorher
  0.8-1.8 s — wirkte traege.
- Hartes Layout-System: jede Szene zeichnet in eine Content-Box zwischen
  Header und Untertitel-Band; Textbloecke werden gemessen, umgebrochen und
  notfalls verkleinert (shrink-to-fit). Ueberlappungen sind damit
  konstruktiv ausgeschlossen, nicht nur unwahrscheinlich.
- Figuren-Rig v2: Zwei-Segment-Arme mit Ellbogen und Haenden, runde
  Gelenke, Arme leicht abgedunkelt; Ruhe-/Gesten-/Laufpose.

Eingabe: VIDEO_ASSETS_JSON (title, voice_script, scenes[] mit optionalem
"type" + Daten). Szenen ohne type fallen auf map/list zurueck.
Abhaengigkeiten: PIL + numpy + imageio_ffmpeg. Letzte stdout-Zeile ist der
finale MP4-Pfad (Modul-Contract). Schreibt storyboard_infographic.json,
storyboard_mapled.json (Shorts-Planer-Fallback) und YouTube-Package.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
GEOJSON_PATH = ROOT / "agent-data/youtube_studio/assets/worldmap/ne_110m_admin_0_countries.geojson"

FONT_DIRS = [
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/dejavu",
]
FONT_BOLD = "LiberationSans-Bold.ttf"
FONT_REGULAR = "LiberationSans-Regular.ttf"

# ─── Design-System ────────────────────────────────────────────────────────
BG_TOP = (13, 18, 30)
BG_BOTTOM = (9, 12, 20)
INK = (242, 244, 248)
INK_DIM = (158, 168, 186)
PANEL = (20, 27, 42)
PANEL_LINE = (44, 54, 76)
ACCENTS = {
    "gold": (240, 180, 41),
    "red": (235, 87, 87),
    "blue": (86, 156, 240),
    "green": (76, 199, 133),
    "violet": (167, 122, 240),
    "cyan": (74, 201, 211),
    "teal": (74, 201, 211),
    "purple": (167, 122, 240),
}
DEFAULT_ACCENT = "gold"

# Timing (Sekunden) — Motion-Design-Standard: schnell rein, ruhig stehen.
T_IN = 0.45        # Standard-Element-Eintritt
T_IN_BIG = 0.6     # grosse Flaechen (Panels, Linien)
T_COUNT = 0.9      # Counter/Donut-Aufbau (Inhalt, nicht Transition)
T_SCENE_FADE = 0.4 # Szenen-Crossfade


# ─── Easing / Mathe ───────────────────────────────────────────────────────
def clamp01(t: float) -> float:
    return max(0.0, min(1.0, t))


def ease_out(t: float) -> float:
    t = clamp01(t)
    return 1 - (1 - t) ** 3


def ease_out_quint(t: float) -> float:
    t = clamp01(t)
    return 1 - (1 - t) ** 5


def ease_in_out(t: float) -> float:
    t = clamp01(t)
    return t * t * (3 - 2 * t)


def ease_out_back(t: float) -> float:
    t = clamp01(t)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def stagger(t: float, index: int, total: int, each: float = T_IN, gap: float = 0.12) -> float:
    """Element i startet bei i*gap und animiert `each` Sekunden — absolute
    Zeiten statt szenenrelativ, damit lange Szenen nicht traege wirken."""
    return clamp01((t - index * gap) / max(each, 1e-6))


def mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(round(a[i] + (b[i] - a[i]) * clamp01(t))) for i in range(3))


def bbox(x0: float, y0: float, x1: float, y1: float) -> list[float]:
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


# ─── Fonts / Text-Layout ──────────────────────────────────────────────────
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def find_font(name: str) -> str:
    for d in FONT_DIRS:
        p = Path(d) / name
        if p.exists():
            return str(p)
    return ""


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    size = max(8, int(size))
    key = ("b" if bold else "r", size)
    if key not in _FONT_CACHE:
        path = find_font(FONT_BOLD if bold else FONT_REGULAR) or find_font(
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        )
        _FONT_CACHE[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    return _FONT_CACHE[key]


def text_w(d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont) -> float:
    return d.textlength(text, font=f)


def wrap_text(d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_w: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip()
        if text_w(d, cand, f) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_text(
    d: ImageDraw.ImageDraw,
    text: str,
    size: int,
    max_w: float,
    max_lines: int,
    bold: bool = True,
    min_size: int = 12,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Shrink-to-fit: verkleinert die Schrift, bis der Text in max_w x
    max_lines passt — Texte koennen konstruktiv nicht mehr ueberlaufen."""
    size = int(size)
    while size > min_size:
        f = font(size, bold)
        lines = wrap_text(d, text, f, max_w)
        if len(lines) <= max_lines and all(text_w(d, ln, f) <= max_w for ln in lines):
            return f, lines
        size -= 2
    f = font(min_size, bold)
    lines = wrap_text(d, text, f, max_w)[:max_lines]
    if lines and text_w(d, lines[-1], f) > max_w:
        while lines[-1] and text_w(d, lines[-1] + "…", f) > max_w:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return f, lines


# ─── Szenen-Modell ────────────────────────────────────────────────────────
class Scene:
    def __init__(self, raw: dict[str, Any], index: int):
        self.raw = raw
        self.index = index
        self.title = str(raw.get("title") or f"Kapitel {index}")
        self.subtitle = str(raw.get("subtitle") or "")
        self.bullets = [str(b) for b in (raw.get("bullets") or []) if str(b).strip()][:6]
        self.route = [str(r) for r in (raw.get("route") or []) if str(r).strip()]
        self.weight = float(raw.get("weight") or 1.0)
        self.accent = ACCENTS.get(str(raw.get("color") or "").lower(), ACCENTS[DEFAULT_ACCENT])
        self.type = self.resolve_type(str(raw.get("type") or "").strip().lower())
        self.narration = str(raw.get("narration") or "")
        self.image = str(raw.get("image") or raw.get("image_path") or "")
        self.start_s = 0.0
        self.duration_s = 0.0

    def resolve_type(self, explicit: str) -> str:
        known = {"hook", "stat", "bars", "people", "figures", "timeline", "quote", "compare", "map", "list", "outro"}
        if explicit in known:
            return explicit
        for key in ("stat", "bars", "people", "figures", "timeline", "quote", "compare"):
            if self.raw.get(key):
                return key
        if len(self.route) >= 2:
            return "map"
        return "list"


# ─── Welt-Geometrie ───────────────────────────────────────────────────────
class WorldMap:
    def __init__(self) -> None:
        self.polys: list[list[tuple[float, float]]] = []
        self.centroids: dict[str, tuple[float, float]] = {}
        self.loaded = False

    def load(self) -> None:
        if self.loaded or not GEOJSON_PATH.exists():
            self.loaded = True
            return
        try:
            data = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
        except Exception:
            self.loaded = True
            return
        for feat in data.get("features", []):
            geom = feat.get("geometry") or {}
            props = feat.get("properties") or {}
            name = str(props.get("NAME") or props.get("ADMIN") or "").casefold()
            coords = geom.get("coordinates") or []
            rings: list[list[list[float]]] = []
            if geom.get("type") == "Polygon":
                rings = [coords[0]] if coords else []
            elif geom.get("type") == "MultiPolygon":
                rings = [poly[0] for poly in coords if poly]
            pts_all: list[tuple[float, float]] = []
            for ring in rings:
                pts = [(float(x), float(y)) for x, y in (p[:2] for p in ring)]
                if len(pts) >= 3:
                    self.polys.append(pts)
                    pts_all.extend(pts)
            if name and pts_all:
                xs = [p[0] for p in pts_all]
                ys = [p[1] for p in pts_all]
                self.centroids[name] = (sum(xs) / len(xs), sum(ys) / len(ys))
        alias = {
            "usa": "united states of america", "united states": "united states of america",
            "vereinigte staaten": "united states of america", "uk": "united kingdom",
            "grossbritannien": "united kingdom", "deutschland": "germany",
            "frankreich": "france", "russland": "russia", "russian federation": "russia",
            "suedkorea": "south korea", "nordkorea": "north korea", "tuerkei": "turkey",
            "indien": "india", "spanien": "spain", "italien": "italy", "polen": "poland",
            "schweiz": "switzerland", "oesterreich": "austria", "niederlande": "netherlands",
            "saudi arabien": "saudi arabia",
        }
        for k, v in alias.items():
            if v in self.centroids:
                self.centroids[k] = self.centroids[v]
        regions = {
            "global": (10.0, 25.0), "europe": (12.0, 50.0), "europa": (12.0, 50.0),
            "asia": (95.0, 35.0), "asien": (95.0, 35.0), "africa": (20.0, 5.0),
            "afrika": (20.0, 5.0), "middle east": (45.0, 29.0), "naher osten": (45.0, 29.0),
            "south america": (-60.0, -15.0), "suedamerika": (-60.0, -15.0),
            "north america": (-100.0, 45.0), "nordamerika": (-100.0, 45.0),
            "pacific": (-170.0, 0.0), "pazifik": (-170.0, 0.0),
        }
        for k, v in regions.items():
            self.centroids.setdefault(k, v)
        self.loaded = True

    def lookup(self, name: str) -> tuple[float, float] | None:
        self.load()
        key = re.sub(r"[^a-zäöüß ]+", "", name.casefold()).strip()
        if key in self.centroids:
            return self.centroids[key]
        for cand, pt in self.centroids.items():
            if key and (key in cand or cand in key):
                return pt
        return None


WORLD = WorldMap()


# ─── Renderer ─────────────────────────────────────────────────────────────
class Renderer:
    _bg_cache: Image.Image | None = None
    _bg_overlay: Image.Image | None = None

    def __init__(self, args: argparse.Namespace, assets: dict[str, Any]):
        self.out_dir = Path(args.out)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.preview = bool(args.preview)
        # Preview = echtes 720p25; Vollrender = 1080p25 mit Supersampling.
        self.width = 1280 if self.preview else int(args.width)
        self.height = 720 if self.preview else int(args.height)
        self.fps = int(args.fps)
        self.ss = 1 if self.preview else max(1, int(args.supersample))
        self.title = str(assets.get("title") or args.title or "Briefing")
        self.scenes = [Scene(s, i + 1) for i, s in enumerate(assets.get("scenes") or []) if isinstance(s, dict)]
        if not self.scenes:
            self.scenes = [Scene({"title": self.title, "type": "hook"}, 1)]
        self.voice_script = str(assets.get("voice_script") or "")
        self.source_note = str(assets.get("source_line") or "DeepDive-Auswertung | Visualisierung schematisch")
        self.kicker = str(assets.get("kicker") or "BRIEFING")
        self.music = not bool(getattr(args, "no_music", False)) and bool(assets.get("music", True))

    # ── Layout-System: Content-Box zwischen Header und Untertitel-Band ──
    def margin(self) -> int:
        return int(54 * self.ss)

    def header_bottom(self, scene: Scene) -> int:
        if scene.type in {"hook", "outro"}:
            return int(40 * self.ss)
        base = self.margin() + int(34 * self.ss) + int(44 * self.ss)
        if scene.subtitle:
            base += int(30 * self.ss)
        return base + int(14 * self.ss)

    def subtitle_top(self) -> int:
        H = self.height * self.ss
        return int(H - 118 * self.ss)

    def content_box(self, scene: Scene) -> tuple[int, int, int, int]:
        W = self.width * self.ss
        top = self.header_bottom(scene)
        bottom = self.subtitle_top() - int(16 * self.ss)
        return self.margin(), top, W - self.margin(), bottom

    # ── Timing ──
    def plan_timing(self, total_s: float) -> None:
        weights = [max(0.05, s.weight) for s in self.scenes]
        unit = total_s / sum(weights)
        t = 0.0
        for s, w in zip(self.scenes, weights):
            s.start_s = t
            s.duration_s = max(2.5, w * unit)
            t += s.duration_s
        scale = total_s / t if t > 0 else 1.0
        t = 0.0
        for s in self.scenes:
            s.duration_s *= scale
            s.start_s = t
            t += s.duration_s
        self.assign_narration()

    def assign_narration(self) -> None:
        if not self.voice_script.strip():
            return
        sentences = re.split(r"(?<=[.!?])\s+", self.voice_script.strip())
        total = sum(s.duration_s for s in self.scenes) or 1.0
        n = len(sentences)
        cursor = 0
        for s in self.scenes:
            if s.narration:
                continue
            share = s.duration_s / total
            take = max(1, int(round(share * n))) if cursor < n else 0
            s.narration = " ".join(sentences[cursor:cursor + take]).strip()
            cursor += take
        if cursor < n and self.scenes:
            self.scenes[-1].narration = (self.scenes[-1].narration + " " + " ".join(sentences[cursor:])).strip()

    # ── Frame ──
    def render_frame(self, t_global: float) -> Image.Image:
        W, H = self.width * self.ss, self.height * self.ss
        img = self.background(W, H, t_global)
        d = ImageDraw.Draw(img, "RGBA")
        scene, t_local, nxt = self.scene_at(t_global)
        self.draw_scene(img, d, scene, t_local)
        if nxt is not None and scene.duration_s - t_local < T_SCENE_FADE:
            alpha = 1 - (scene.duration_s - t_local) / T_SCENE_FADE
            overlay = self.background(W, H, t_global)
            od = ImageDraw.Draw(overlay, "RGBA")
            self.draw_scene(overlay, od, nxt, 0.0)
            self.draw_chrome(od, nxt, 0.0, W, H, scene.start_s + t_local)
            img = Image.blend(img, overlay, ease_in_out(alpha))
            d = ImageDraw.Draw(img, "RGBA")
            if alpha < 0.5:
                self.draw_chrome(d, scene, t_local, W, H, scene.start_s + t_local)
        else:
            self.draw_chrome(d, scene, t_local, W, H, scene.start_s + t_local)
        if self.ss > 1:
            img = img.resize((self.width, self.height), Image.LANCZOS)
        return img

    def scene_at(self, t: float) -> tuple[Scene, float, Scene | None]:
        for i, s in enumerate(self.scenes):
            if t < s.start_s + s.duration_s or i == len(self.scenes) - 1:
                nxt = self.scenes[i + 1] if i + 1 < len(self.scenes) else None
                return s, max(0.0, t - s.start_s), nxt
        last = self.scenes[-1]
        return last, last.duration_s, None

    def background(self, W: int, H: int, t: float) -> Image.Image:
        if self._bg_cache is None or self._bg_cache.size != (W, H):
            col = np.linspace(0, 1, H, dtype=np.float32)[:, None]
            grad = (np.array(BG_TOP, dtype=np.float32)[None, None, :] * (1 - col[..., None])
                    + np.array(BG_BOTTOM, dtype=np.float32)[None, None, :] * col[..., None])
            arr = np.repeat(grad, W, axis=1).astype(np.uint8)
            self._bg_cache = Image.fromarray(arr, "RGB").convert("RGBA")
        img = self._bg_cache.copy()
        d = ImageDraw.Draw(img, "RGBA")
        step = int(72 * self.ss)
        drift = (t * 5 * self.ss) % step
        r = max(1, int(self.ss))
        for y in range(0, H + step, step):
            for x in range(-step, W + step, step):
                d.ellipse([x + drift - r, y - r, x + drift + r, y + r], fill=(255, 255, 255, 9))
        return img

    # ── Chrome ──
    def draw_chrome(self, d, scene: Scene, t: float, W: int, H: int, t_abs: float) -> None:
        s = self.ss
        m = self.margin()
        if scene.type not in {"hook", "outro"}:
            a = ease_out_quint(t / T_IN)
            f_kick = font(int(15 * s))
            kick = f"{self.kicker} {scene.index:02d}"
            kw = text_w(d, kick, f_kick)
            d.rounded_rectangle([m, m, m + kw + 28 * s, m + 30 * s], radius=6 * s, fill=scene.accent + (int(255 * a),))
            d.text((m + 14 * s, m + 7 * s), kick, font=f_kick, fill=(15, 18, 26, int(255 * a)))
            f_title, t_lines = fit_text(d, scene.title, int(33 * s), W - 2 * m, 1)
            d.text((m + (1 - a) * 20 * s, m + 40 * s), t_lines[0], font=f_title, fill=INK + (int(255 * a),))
            if scene.subtitle:
                f_sub, s_lines = fit_text(d, scene.subtitle, int(18 * s), W - 2 * m, 1, bold=False)
                d.text((m, m + 84 * s), s_lines[0], font=f_sub, fill=INK_DIM + (int(235 * a),))
        self.draw_subtitles(d, scene, t, W, H)
        d.text((m, H - 30 * s), self.source_note, font=font(int(11 * s), bold=False), fill=(118, 128, 148))
        total = self.scenes[-1].start_s + self.scenes[-1].duration_s
        frac = clamp01(t_abs / max(total, 1e-6))
        d.rectangle([0, H - 5 * s, int(W * frac), H], fill=scene.accent + (220,))

    def draw_subtitles(self, d, scene: Scene, t: float, W: int, H: int) -> None:
        if not scene.narration:
            return
        s = self.ss
        words = scene.narration.split()
        if not words:
            return
        visible = max(1, int(len(words) * clamp01(t / max(scene.duration_s - 0.4, 0.1)) + 2))
        win = 16
        start = max(0, visible - win)
        text = " ".join(words[start:visible])
        f = font(int(20 * s), bold=False)
        lines = wrap_text(d, text, f, W * 0.66)[-2:]
        band_top = self.subtitle_top()
        for i, line in enumerate(lines):
            lw = text_w(d, line, f)
            x = (W - lw) / 2
            pad = 12 * s
            ly = band_top + i * int(30 * s)
            d.rounded_rectangle([x - pad, ly - 4 * s, x + lw + pad, ly + 26 * s], radius=8 * s, fill=(8, 11, 19, 225))
            d.text((x, ly), line, font=f, fill=INK)

    _image_cache: dict[str, Image.Image] = {}

    def scene_background_image(self, img: Image.Image, scene: Scene, t: float) -> None:
        """Themen-Bild als Hintergrund: Cover-Fit, langsamer Ken-Burns-Zoom
        mit alternierender Drift, dunkles Overlay fuer Textkontrast. Die
        Karte ignoriert Bilder (sie IST das Visual)."""
        if not scene.image or scene.type == "map":
            return
        path = Path(scene.image)
        if not path.is_absolute():
            path = ROOT / path
        key = str(path)
        if key not in self._image_cache:
            try:
                self._image_cache[key] = Image.open(path).convert("RGB")
            except Exception:
                self._image_cache[key] = None  # type: ignore[assignment]
        src_img = self._image_cache.get(key)
        if src_img is None:
            return
        W, H = img.width, img.height
        # Ken Burns: 1.05 -> 1.13 ueber die Szene, Drift-Richtung alterniert
        prog = clamp01(t / max(scene.duration_s, 1e-6))
        zoom = 1.05 + 0.08 * ease_in_out(prog)
        iw, ih = src_img.size
        scale = max(W / iw, H / ih) * zoom
        sw, sh = max(W, int(iw * scale)), max(H, int(ih * scale))
        drift_dir = 1 if scene.index % 2 == 0 else -1
        max_dx = max(0, sw - W)
        max_dy = max(0, sh - H)
        dx = int(max_dx / 2 + drift_dir * max_dx * 0.18 * (prog - 0.5))
        dy = int(max_dy / 2)
        # Nur das benoetigte WxH-Fenster aus der Quelle skalieren (box-resize),
        # statt das ganze Bild auf sw x sh aufzublasen und dann zu croppen —
        # pro Frame quadratisch billiger (sw*sh war bei ss=2 ~9MP).
        box = (dx / scale, dy / scale, (dx + W) / scale, (dy + H) / scale)
        frame = src_img.resize((W, H), Image.BILINEAR, box=box)
        img.paste(frame, (0, 0))
        # Kontrast-Overlay (oben moderat, unten kraeftig) — haengt NICHT von t/scene
        # ab, also einmal pro WxH bauen und cachen statt pro Frame neu (der alte
        # putpixel-Loop + Resize war die Haupt-Renderbremse).
        overlay = self._bg_overlay
        if overlay is None or overlay.size != (W, H):
            ramp = (120 + 95 * np.linspace(0.0, 1.0, H, dtype=np.float32)).astype(np.uint8)
            alpha = Image.fromarray(np.repeat(ramp[:, None], W, axis=1), "L")
            overlay = Image.new("RGBA", (W, H), (8, 11, 19, 255))
            overlay.putalpha(alpha)
            type(self)._bg_overlay = overlay
        img.alpha_composite(overlay)

    # ── Szenen-Dispatch ──
    def draw_scene(self, img, d, scene: Scene, t: float) -> None:
        self.scene_background_image(img, scene, t)
        fn = {
            "hook": self.scene_hook, "stat": self.scene_stat, "bars": self.scene_bars,
            "people": self.scene_people, "figures": self.scene_figures,
            "timeline": self.scene_timeline, "quote": self.scene_quote,
            "compare": self.scene_compare, "map": self.scene_map,
            "list": self.scene_list, "outro": self.scene_outro,
        }.get(scene.type, self.scene_list)
        fn(img, d, scene, t)

    # ── Szenen ──
    def scene_hook(self, img, d, scene: Scene, t: float) -> None:
        W, H, s = img.width, img.height, self.ss
        x0, y0, x1, y1 = self.content_box(scene)
        a = ease_out(t / T_IN_BIG)
        d.ellipse([W * 0.80 - 150 * s * a, H * 0.18 - 150 * s * a, W * 0.80 + 150 * s * a, H * 0.18 + 150 * s * a],
                  outline=scene.accent + (90,), width=int(3 * s))
        f_big, lines = fit_text(d, scene.title, int(62 * s), (x1 - x0) * 0.92, 3)
        lh = int(f_big.size * 1.22)
        base_y = max(y0 + 20 * s, H * 0.40 - len(lines) * lh / 2)
        for i, line in enumerate(lines):
            la = ease_out_quint(stagger(t, i, len(lines), each=T_IN, gap=0.09))
            d.text((x0 + 12 * s + (1 - la) * 30 * s, base_y + i * lh), line, font=f_big, fill=INK + (int(255 * la),))
        bar_w = (x1 - x0) * 0.28 * ease_out((t - 0.25) / T_IN)
        bar_y = base_y + len(lines) * lh + 16 * s
        d.rectangle([x0 + 12 * s, bar_y, x0 + 12 * s + bar_w, bar_y + 9 * s], fill=scene.accent)
        if scene.subtitle:
            sa = ease_out((t - 0.4) / T_IN)
            f_sub, sub_lines = fit_text(d, scene.subtitle, int(25 * s), (x1 - x0) * 0.85, 2, bold=False)
            for i, ln in enumerate(sub_lines):
                d.text((x0 + 12 * s, bar_y + 26 * s + i * int(f_sub.size * 1.25)), ln,
                       font=f_sub, fill=INK_DIM + (int(255 * sa),))

    def scene_stat(self, img, d, scene: Scene, t: float) -> None:
        W, H, s = img.width, img.height, self.ss
        x0, y0, x1, y1 = self.content_box(scene)
        stat = scene.raw.get("stat") or {}
        value = float(stat.get("value") or 0.0)
        unit = str(stat.get("unit") or "")
        label = str(stat.get("label") or scene.subtitle or scene.title)
        maximum = float(stat.get("max") or (100.0 if value <= 100 else value * 1.25)) or 1.0
        a = ease_out(t / T_COUNT)
        r = min((y1 - y0) * 0.36, (x1 - x0) * 0.16)
        cx, cy = x0 + r + 30 * s, (y0 + y1) / 2
        ring = int(max(10 * s, r * 0.14))
        d.arc([cx - r, cy - r, cx + r, cy + r], 0, 360, fill=PANEL_LINE, width=ring)
        sweep = 360 * clamp01(value / maximum) * a
        d.arc([cx - r, cy - r, cx + r, cy + r], -90, -90 + sweep, fill=scene.accent, width=ring)
        shown = value * a
        num = f"{shown:,.0f}".replace(",", ".") if value >= 10 else f"{shown:.1f}"
        f_num, _ = fit_text(d, num, int(r * 0.55), r * 1.5, 1)
        nw = text_w(d, num, f_num)
        d.text((cx - nw / 2, cy - f_num.size * 0.72), num, font=f_num, fill=INK)
        if unit:
            f_unit, _ = fit_text(d, unit, int(r * 0.22), r * 1.6, 1)
            uw = text_w(d, unit, f_unit)
            d.text((cx - uw / 2, cy + f_num.size * 0.28), unit, font=f_unit, fill=scene.accent)
        # Label-Spalte rechts — gemessen, geschrumpft, geklemmt
        col_x = cx + r + 44 * s
        col_w = x1 - col_x
        la = ease_out((t - 0.2) / T_IN)
        f_lab, lab_lines = fit_text(d, label, int(30 * s), col_w, 3)
        ly = cy - (len(lab_lines) * f_lab.size * 1.25) / 2 - 20 * s
        ly = max(y0 + 8 * s, ly)
        for i, ln in enumerate(lab_lines):
            d.text((col_x, ly + i * int(f_lab.size * 1.25) + (1 - la) * 16 * s), ln,
                   font=f_lab, fill=INK + (int(255 * la),))
        self.bullet_block(d, scene, t - 0.35, col_x, ly + len(lab_lines) * int(f_lab.size * 1.25) + 18 * s,
                          col_w, y1)

    def scene_bars(self, img, d, scene: Scene, t: float) -> None:
        W, H, s = img.width, img.height, self.ss
        x0, y0, x1, y1 = self.content_box(scene)
        bars = [b for b in (scene.raw.get("bars") or []) if isinstance(b, dict)][:6]
        if not bars:
            return self.scene_list(img, d, scene, t)
        max_v = max(float(b.get("value") or 0.0) for b in bars) or 1.0
        slot = min(int(92 * s), int((y1 - y0) / len(bars)))
        f_val = font(int(20 * s))
        bar_x1 = x1 - 130 * s  # Platz fuer Wert-Label rechts reservieren
        y = y0 + 10 * s
        for i, b in enumerate(bars):
            a = ease_out_quint(stagger(t, i, len(bars), each=T_IN_BIG, gap=0.10))
            v = float(b.get("value") or 0.0)
            label = str(b.get("label") or f"#{i + 1}")
            col = ACCENTS.get(str(b.get("color") or "").lower(), scene.accent)
            f_l, l_lines = fit_text(d, label, int(19 * s), bar_x1 - x0, 1, bold=False)
            d.text((x0, y), l_lines[0], font=f_l, fill=INK_DIM)
            by = y + 26 * s
            d.rounded_rectangle([x0, by, bar_x1, by + 26 * s], radius=8 * s, fill=PANEL)
            w = (bar_x1 - x0) * (v / max_v) * a
            if w > 16 * s:
                d.rounded_rectangle([x0, by, x0 + w, by + 26 * s], radius=8 * s, fill=col)
            val_txt = str(b.get("display") or f"{v:,.0f}".replace(",", "."))
            d.text((bar_x1 + 14 * s, by + 2 * s), val_txt, font=f_val, fill=INK + (int(255 * a),))
            y += slot

    def scene_people(self, img, d, scene: Scene, t: float) -> None:
        W, H, s = img.width, img.height, self.ss
        x0, y0, x1, y1 = self.content_box(scene)
        people = scene.raw.get("people") or {}
        count = max(1, min(100, int(people.get("count") or 10)))
        highlight = max(0, min(count, int(people.get("highlight") or 0)))
        label = str(people.get("label") or scene.subtitle or "")
        if highlight:
            ratio = f"{highlight} von {count}"
            f_r, _ = fit_text(d, ratio, int(40 * s), W * 0.3, 1)
            rw = text_w(d, ratio, f_r)
            d.text((W - self.margin() - rw, self.margin() + 4 * s), ratio, font=f_r,
                   fill=scene.accent + (int(255 * ease_out(t / T_IN)),))
        label_h = int(46 * s) if label else 0
        grid_bottom = y1 - label_h
        cols = 10 if count > 20 else max(4, min(10, count))
        rows = math.ceil(count / cols)
        ph = min(int(70 * s), int((grid_bottom - y0) / max(rows, 1) / 1.08))
        grid_w = cols * ph * 0.62
        gx = (W - grid_w) / 2
        gy = y0 + (grid_bottom - y0 - rows * ph * 1.08) / 2
        for i in range(count):
            r_, c_ = divmod(i, cols)
            a = ease_out_back(stagger(t, i, count, each=0.3, gap=0.025))
            if a <= 0.01:
                continue
            x = gx + c_ * ph * 0.62
            y = gy + r_ * ph * 1.08 + (1 - a) * 12 * s
            col = scene.accent if i < highlight else (70, 80, 100)
            bob = math.sin(t * 2.2 + i * 0.7) * 1.2 * s
            head_r = ph * 0.16
            d.ellipse([x - head_r, y + bob, x + head_r, y + 2 * head_r + bob], fill=col)
            bw = ph * 0.42
            d.rounded_rectangle([x - bw / 2, y + 2.3 * head_r + bob, x + bw / 2, y + ph + bob], radius=bw / 2, fill=col)
        if label:
            la = ease_out((t - 0.5) / T_IN)
            f_l, l_lines = fit_text(d, label, int(26 * s), x1 - x0, 1)
            lw = text_w(d, l_lines[0], f_l)
            d.text(((W - lw) / 2, grid_bottom + 10 * s), l_lines[0], font=f_l, fill=INK + (int(255 * la),))

    def scene_figures(self, img, d, scene: Scene, t: float) -> None:
        W, H, s = img.width, img.height, self.ss
        x0, y0, x1, y1 = self.content_box(scene)
        fig = scene.raw.get("figures") or {}
        left = fig.get("left") or {}
        right = fig.get("right") or {}
        l_label = str(left.get("label") or (scene.route[0] if scene.route else "A"))
        r_label = str(right.get("label") or (scene.route[1] if len(scene.route) > 1 else "B"))
        l_say = str(left.get("say") or "")
        r_say = str(right.get("say") or "")
        l_col = ACCENTS.get(str(left.get("color") or "").lower(), ACCENTS["blue"])
        r_col = ACCENTS.get(str(right.get("color") or "").lower(), ACCENTS["red"])

        # Mit Szenenbild: das KI-Bild IST die Buehne (zeigt die Akteure) —
        # nur Sprechblasen + Namens-Chips drueber, keine PIL-Figuren.
        if scene.image:
            col_w = (x1 - x0) * 0.44
            l_speak = bool(l_say) and t > 0.7
            r_speak = bool(r_say) and t > 1.1
            for x, label, align_right in ((x0, l_label, False), (x1, r_label, True)):
                f_n, n_lines = fit_text(d, label, int(21 * s), (x1 - x0) * 0.3, 1)
                lw = text_w(d, n_lines[0], f_n)
                cx0 = (x1 - lw - 22 * s) if align_right else x0
                cy = y1 - int(30 * s)
                d.rounded_rectangle([cx0, cy - 15 * s, cx0 + lw + 22 * s, cy + 15 * s],
                                    radius=8 * s, fill=(10, 13, 22, 225))
                d.text((cx0 + 11 * s, cy - 11 * s), n_lines[0], font=f_n, fill=INK)
            if l_speak:
                self.draw_speech(d, x0, y0, x0 + col_w * 0.4, y1, l_say,
                                 ease_out_back((t - 0.7) / 0.4), col_w)
            if r_speak:
                self.draw_speech(d, x1 - col_w, y0, x1 - col_w * 0.4, y1, r_say,
                                 ease_out_back((t - 1.1) / 0.4), col_w)
            return

        # Sprechblasen in festen, getrennten Spalten oben in der Content-Box;
        # Hoehen werden GEMESSEN, die Buehne beginnt erst darunter.
        col_w = (x1 - x0) * 0.44
        bubble_h = 0
        l_speak = bool(l_say) and t > 0.7
        r_speak = bool(r_say) and t > 1.1
        if l_say:
            bubble_h = max(bubble_h, self.speech_height(d, l_say, col_w))
        if r_say:
            bubble_h = max(bubble_h, self.speech_height(d, r_say, col_w))
        stage_top = y0 + (bubble_h + int(26 * s) if (l_say or r_say) else 0)
        ground = y1 - int(34 * s)  # Platz fuer Namens-Chips unter der Linie
        body_h = max(80 * s, (ground - stage_top) * 0.92)

        walk_l = ease_out(t / 0.7)
        walk_r = ease_out((t - 0.15) / 0.7)
        lx = x0 + (W * 0.30 - x0) * walk_l
        rx = x1 - (x1 - W * 0.70) * walk_r
        d.line([x0, ground + 2 * s, x1, ground + 2 * s], fill=PANEL_LINE, width=int(2 * s))
        self.draw_character(d, lx, ground, body_h, l_col, t, walking=walk_l < 0.99, facing=1,
                            gesture=ease_in_out((t - 0.7) / 0.4) if l_speak else 0.0)
        self.draw_character(d, rx, ground, body_h, r_col, t + 0.4, walking=walk_r < 0.99, facing=-1,
                            gesture=ease_in_out((t - 1.1) / 0.4) if r_speak else 0.0)
        # Namens-Chips UNTER der Bodenlinie — kollisionsfreier Reservebereich
        for x, label in ((lx, l_label), (rx, r_label)):
            f_n, n_lines = fit_text(d, label, int(19 * s), (x1 - x0) * 0.3, 1)
            lw = text_w(d, n_lines[0], f_n)
            cy = ground + int(18 * s)
            d.rounded_rectangle([x - lw / 2 - 9 * s, cy - 13 * s, x + lw / 2 + 9 * s, cy + 13 * s],
                                radius=7 * s, fill=(10, 13, 22, 225))
            d.text((x - lw / 2, cy - 10 * s), n_lines[0], font=f_n, fill=INK)
        if l_speak:
            self.draw_speech(d, x0, y0, lx, stage_top, l_say,
                             ease_out_back((t - 0.7) / 0.4), col_w)
        if r_speak:
            self.draw_speech(d, x1 - col_w, y0, rx, stage_top, r_say,
                             ease_out_back((t - 1.1) / 0.4), col_w)

    def scene_timeline(self, img, d, scene: Scene, t: float) -> None:
        W, H, s = img.width, img.height, self.ss
        x0, y0, x1, y1 = self.content_box(scene)
        items = [i for i in (scene.raw.get("timeline") or []) if isinstance(i, dict)][:6]
        if not items:
            return self.scene_list(img, d, scene, t)
        y = (y0 + y1) / 2
        line_a = ease_out(t / T_IN_BIG)
        d.line([x0, y, x0 + (x1 - x0) * line_a, y], fill=PANEL_LINE, width=int(4 * s))
        n = len(items)
        slot_w = (x1 - x0) / n
        f_date = font(int(20 * s))
        for i, item in enumerate(items):
            a = ease_out_back(stagger(t, i, n, each=T_IN, gap=0.14))
            if a <= 0.01:
                continue
            x = x0 + slot_w * (i + 0.5)
            r = 10 * s * a
            d.ellipse([x - r, y - r, x + r, y + r], fill=scene.accent)
            date = str(item.get("date") or item.get("year") or "")
            txt = str(item.get("text") or item.get("label") or "")
            up = i % 2 == 0
            f_txt, t_lines = fit_text(d, txt, int(17 * s), slot_w * 0.94, 3, bold=False)
            block_h = int(26 * s) + len(t_lines) * int(f_txt.size * 1.3)
            ty = (y - 24 * s - block_h) if up else (y + 26 * s)
            ty = max(y0, min(ty, y1 - block_h))
            dw = text_w(d, date, f_date)
            d.text((x - dw / 2, ty), date, font=f_date, fill=scene.accent + (int(255 * a),))
            for j, line in enumerate(t_lines):
                lw2 = text_w(d, line, f_txt)
                d.text((x - lw2 / 2, ty + 26 * s + j * int(f_txt.size * 1.3)), line,
                       font=f_txt, fill=INK + (int(235 * a),))

    def scene_quote(self, img, d, scene: Scene, t: float) -> None:
        W, H, s = img.width, img.height, self.ss
        x0, y0, x1, y1 = self.content_box(scene)
        q = scene.raw.get("quote") or {}
        text = str(q.get("text") or scene.subtitle or scene.title)
        by = str(q.get("by") or "")
        a = ease_out(t / T_IN)
        f_mark = font(int(120 * s))
        d.text((x0, y0 - 26 * s), "„", font=f_mark, fill=scene.accent + (int(220 * a),))
        block_w = (x1 - x0) * 0.82
        f_q, lines = fit_text(d, text, int(34 * s), block_w, 4)
        lh = int(f_q.size * 1.4)
        attr_h = int(40 * s) if by else 0
        base = y0 + max(30 * s, (y1 - y0 - len(lines) * lh - attr_h) / 2)
        qx = x0 + (x1 - x0) * 0.09
        for i, line in enumerate(lines):
            la = ease_out_quint(stagger(t, i, len(lines), each=T_IN, gap=0.10))
            d.text((qx, base + i * lh + (1 - la) * 16 * s), line, font=f_q, fill=INK + (int(255 * la),))
        if by:
            ba = ease_out((t - 0.5) / T_IN)
            ay = min(base + len(lines) * lh + 18 * s, y1 - 28 * s)
            d.line([qx, ay + 11 * s, qx + 64 * s * ba, ay + 11 * s], fill=scene.accent, width=int(4 * s))
            f_by, by_lines = fit_text(d, by, int(20 * s), block_w - 90 * s, 1, bold=False)
            d.text((qx + 80 * s, ay), by_lines[0], font=f_by, fill=INK_DIM + (int(255 * ba),))

    def scene_compare(self, img, d, scene: Scene, t: float) -> None:
        W, H, s = img.width, img.height, self.ss
        x0, y0, x1, y1 = self.content_box(scene)
        comp = scene.raw.get("compare") or {}
        panels = [comp.get("left") or {}, comp.get("right") or {}]
        cols = [ACCENTS["blue"], ACCENTS["red"]]
        panel_w = (x1 - x0) * 0.45
        for i, (panel, col) in enumerate(zip(panels, cols)):
            a = ease_out_quint(stagger(t, i, 2, each=T_IN_BIG, gap=0.12))
            px0 = x0 if i == 0 else x1 - panel_w
            px1 = px0 + panel_w
            slide = (1 - a) * 24 * s * (-1 if i == 0 else 1)
            d.rounded_rectangle([px0 + slide, y0, px1 + slide, y1], radius=14 * s,
                                fill=PANEL + (int(235 * a),), outline=PANEL_LINE)
            inner = px0 + 26 * s + slide
            inner_w = panel_w - 52 * s
            f_l, l_lines = fit_text(d, str(panel.get("label") or ("A" if i == 0 else "B")), int(24 * s), inner_w, 1)
            d.text((inner, y0 + 22 * s), l_lines[0], font=f_l, fill=col + (int(255 * a),))
            cy = y0 + 60 * s
            value = str(panel.get("value") or "")
            if value:
                f_v, v_lines = fit_text(d, value, int(44 * s), inner_w, 1)
                d.text((inner, cy), v_lines[0], font=f_v, fill=INK + (int(255 * a),))
                cy += int(f_v.size * 1.4)
            points = [str(p) for p in (panel.get("points") or []) if str(p).strip()][:4]
            for j, p in enumerate(points):
                pa = ease_out(stagger(t - 0.3, j, max(1, len(points)), each=T_IN, gap=0.1))
                f_pp, p_lines = fit_text(d, p, int(18 * s), inner_w - 26 * s, 2, bold=False)
                ph = len(p_lines) * int(f_pp.size * 1.3)
                if cy + ph > y1 - 14 * s:
                    break
                d.ellipse([inner, cy + 7 * s, inner + 9 * s, cy + 16 * s], fill=col + (int(255 * pa),))
                for k, line in enumerate(p_lines):
                    d.text((inner + 22 * s, cy + k * int(f_pp.size * 1.3)), line,
                           font=f_pp, fill=INK + (int(235 * pa),))
                cy += ph + 12 * s
        va = ease_out_back((t - 0.4) / T_IN)
        if va > 0:
            f_vs = font(int(30 * s))
            vw = text_w(d, "VS", f_vs)
            cx, cyv = (x0 + x1) / 2, (y0 + y1) / 2
            rr = 30 * s * clamp01(va)
            d.ellipse([cx - rr, cyv - rr, cx + rr, cyv + rr], fill=scene.accent + (int(255 * clamp01(va)),))
            d.text((cx - vw / 2, cyv - f_vs.size * 0.58), "VS", font=f_vs, fill=(15, 18, 26, int(255 * clamp01(va))))

    def scene_map(self, img, d, scene: Scene, t: float) -> None:
        W, H, s = img.width, img.height, self.ss
        x0, y0, x1, y1 = self.content_box(scene)
        WORLD.load()
        pts = [(name, WORLD.lookup(name)) for name in scene.route]
        pts = [(n, p) for n, p in pts if p]
        if pts:
            lons = [p[0] for _, p in pts]
            lats = [p[1] for _, p in pts]
            pad_lon = max(18.0, (max(lons) - min(lons)) * 0.45)
            pad_lat = max(12.0, (max(lats) - min(lats)) * 0.45)
            lon0, lon1 = min(lons) - pad_lon, max(lons) + pad_lon
            lat0, lat1 = min(lats) - pad_lat, max(lats) + pad_lat
        else:
            lon0, lon1, lat0, lat1 = -170, 190, -58, 78
        span_lon, span_lat = lon1 - lon0, lat1 - lat0
        target = (W / H) * 0.92
        if span_lon / span_lat < target:
            extra = span_lat * target - span_lon
            lon0 -= extra / 2
            lon1 += extra / 2
        else:
            extra = span_lon / target - span_lat
            lat0 -= extra / 2
            lat1 += extra / 2
        lat0, lat1 = max(lat0, -62.0), min(lat1, 84.0)
        lon0, lon1 = max(lon0, -185.0), min(lon1, 200.0)

        def proj(lon: float, lat: float) -> tuple[float, float]:
            return ((lon - lon0) / (lon1 - lon0) * W, (1 - (lat - lat0) / (lat1 - lat0)) * H)

        for poly in WORLD.polys:
            pp = [proj(px, py) for px, py in poly]
            if all(p[0] < -W * 0.2 or p[0] > W * 1.2 or p[1] < -H * 0.2 or p[1] > H * 1.2 for p in pp):
                continue
            d.polygon(pp, fill=(30, 39, 58), outline=(50, 62, 88))
        if len(pts) >= 2:
            seg_t = clamp01(t / max(1.0, 0.55 * (len(pts) - 1))) * (len(pts) - 1)
            for i in range(len(pts) - 1):
                a = clamp01(seg_t - i)
                if a <= 0:
                    break
                xa, ya = proj(*pts[i][1])
                xb, yb = proj(*pts[i + 1][1])
                mx, my = (xa + xb) / 2, min(ya, yb) - abs(xb - xa) * 0.22 - 26 * s
                steps = 40
                path = []
                for k in range(int(steps * a) + 1):
                    u = k / steps
                    bx = (1 - u) ** 2 * xa + 2 * (1 - u) * u * mx + u ** 2 * xb
                    by2 = (1 - u) ** 2 * ya + 2 * (1 - u) * u * my + u ** 2 * yb
                    path.append((bx, by2))
                if len(path) > 1:
                    d.line(path, fill=scene.accent + (235,), width=int(4 * s), joint="curve")
                    hx, hy = path[-1]
                    d.ellipse([hx - 6 * s, hy - 6 * s, hx + 6 * s, hy + 6 * s], fill=scene.accent)
        f_m = font(int(18 * s))
        placed: list[tuple[float, float]] = []
        # Bereich der Bullet-Box (oben rechts) — Marker-Labels weichen aus
        box_w = min(360 * s, (x1 - self.margin()) * 0.42) if scene.bullets else 0
        box_x0 = x1 - box_w if scene.bullets else x1 + 1
        box_y1 = y0 + (y1 - y0) * 0.8 if scene.bullets else y0
        for i, (name, p) in enumerate(pts):
            a = ease_out_back(stagger(t, i, max(1, len(pts)), each=T_IN, gap=0.12))
            if a <= 0.01:
                continue
            x, y = proj(*p)
            pulse = 1 + 0.15 * math.sin(t * 3 + i)
            r = 8 * s * a
            d.ellipse([x - r * pulse - 6 * s, y - r * pulse - 6 * s, x + r * pulse + 6 * s, y + r * pulse + 6 * s],
                      outline=scene.accent + (90,), width=int(2 * s))
            d.ellipse([x - r, y - r, x + r, y + r], fill=scene.accent)
            ly = y
            for px2, py2 in placed:
                if abs(x - px2) < 150 * s and abs(ly - py2) < 32 * s:
                    ly = py2 + 34 * s
            lw = text_w(d, name, f_m)
            lx0 = x + 13 * s
            # Kollision mit der Bullet-Box? Label links vom Punkt platzieren.
            if lx0 + lw + 16 * s > box_x0 and ly - 13 * s < box_y1:
                lx0 = x - 13 * s - lw - 16 * s
            placed.append((x, ly))
            d.rounded_rectangle([lx0, ly - 13 * s, lx0 + lw + 16 * s, ly + 13 * s],
                                radius=6 * s, fill=(10, 13, 22, 220))
            d.text((lx0 + 8 * s, ly - 9 * s), name, font=f_m, fill=INK)
        if scene.bullets:
            self.bullet_box(d, scene, t - 0.4, x1, y0, y1)

    def scene_list(self, img, d, scene: Scene, t: float) -> None:
        W, H, s = img.width, img.height, self.ss
        x0, y0, x1, y1 = self.content_box(scene)
        a = ease_out(t / T_IN)
        d.rectangle([x0 - 14 * s, y0 + 8 * s, x0 - 8 * s, y0 + 8 * s + (y1 - y0 - 16 * s) * a], fill=scene.accent)
        self.bullet_block(d, scene, t, x0 + 14 * s, y0 + 14 * s, (x1 - x0) * 0.86, y1, big=True)

    def scene_outro(self, img, d, scene: Scene, t: float) -> None:
        W, H, s = img.width, img.height, self.ss
        a = ease_out(t / T_IN_BIG)
        f_big, lines = fit_text(d, scene.title or self.title, int(50 * s), W * 0.74, 3)
        lh = int(f_big.size * 1.3)
        y = H * 0.42 - len(lines) * lh / 2
        bw = 84 * s * ease_out((t - 0.1) / T_IN)
        d.rectangle([(W - bw) / 2, y - 30 * s, (W + bw) / 2, y - 23 * s], fill=scene.accent)
        for i, line in enumerate(lines):
            lw = text_w(d, line, f_big)
            d.text(((W - lw) / 2, y + i * lh), line, font=f_big, fill=INK + (int(255 * a),))
        sub = scene.subtitle or "Quellen und Details im Begleittext"
        sa = ease_out((t - 0.3) / T_IN)
        f_sub, s_lines = fit_text(d, sub, int(23 * s), W * 0.7, 1, bold=False)
        sw = text_w(d, s_lines[0], f_sub)
        d.text(((W - sw) / 2, y + len(lines) * lh + 20 * s), s_lines[0], font=f_sub, fill=INK_DIM + (int(255 * sa),))

    # ── Bausteine ──
    def bullet_block(self, d, scene: Scene, t: float, x: float, y: float, max_w: float,
                     y_limit: float, big: bool = False) -> None:
        if not scene.bullets or t < 0:
            return
        s = self.ss
        size = int((23 if big else 18) * s)
        for i, b in enumerate(scene.bullets):
            a = ease_out(stagger(t, i, len(scene.bullets), each=T_IN, gap=0.10))
            f_b, b_lines = fit_text(d, b, size, max_w - 26 * s, 2, bold=False)
            lh = int(f_b.size * 1.3)
            block_h = len(b_lines) * lh
            if y + block_h > y_limit:
                break
            if a > 0.01:
                by = y + (1 - a) * 12 * s
                d.ellipse([x, by + lh * 0.32, x + 9 * s, by + lh * 0.32 + 9 * s], fill=scene.accent + (int(255 * a),))
                for j, line in enumerate(b_lines):
                    d.text((x + 22 * s, by + j * lh), line, font=f_b, fill=INK + (int(240 * a),))
            y += block_h + int((16 if big else 10) * s)

    def bullet_box(self, d, scene: Scene, t: float, x1: float, y0: float, y1: float) -> None:
        """Karten-Bullets als Panel oben rechts — Hoehe gemessen + geklemmt."""
        if not scene.bullets or t < 0:
            return
        s = self.ss
        box_w = min(360 * s, (x1 - self.margin()) * 0.42)
        inner_w = box_w - 44 * s
        total_h = 14 * s
        shown = []
        for b in scene.bullets:
            f_b, b_lines = fit_text(d, b, int(17 * s), inner_w, 2, bold=False)
            bh = len(b_lines) * int(f_b.size * 1.3) + 10 * s
            if total_h + bh > (y1 - y0) * 0.8:
                break
            shown.append((f_b, b_lines))
            total_h += bh
        if not shown:
            return
        bx0 = x1 - box_w
        a = ease_out(t / T_IN)
        d.rounded_rectangle([bx0, y0, x1, y0 + total_h + 8 * s], radius=10 * s,
                            fill=(10, 13, 22, int(205 * a)), outline=PANEL_LINE)
        y = y0 + 12 * s
        for i, (f_b, b_lines) in enumerate(shown):
            ia = ease_out(stagger(t, i, len(shown), each=T_IN, gap=0.10))
            d.ellipse([bx0 + 16 * s, y + 6 * s, bx0 + 25 * s, y + 15 * s], fill=scene.accent + (int(255 * ia),))
            for j, line in enumerate(b_lines):
                d.text((bx0 + 36 * s, y + j * int(f_b.size * 1.3)), line, font=f_b, fill=INK + (int(235 * ia),))
            y += len(b_lines) * int(f_b.size * 1.3) + 10 * s

    # ── Figuren-Rig v2 ──
    def limb(self, d, p0, p1, p2, col, width):
        """Zwei-Segment-Gliedmasse mit runden Gelenken und Endpunkt."""
        d.line([p0, p1], fill=col, width=int(width), joint="curve")
        d.line([p1, p2], fill=col, width=int(width), joint="curve")
        r = width * 0.5
        for px, py in (p1, p2):
            d.ellipse([px - r, py - r, px + r, py + r], fill=col)

    def draw_character(self, d, x: float, ground: float, h: float, col: tuple, t: float,
                       walking: bool, facing: int, gesture: float = 0.0) -> None:
        s = self.ss
        arm_col = mix(col, (0, 0, 0), 0.25)
        head_r = h * 0.115
        top = ground - h
        body_top = top + head_r * 2.25
        hip_y = ground - h * 0.36
        body_w = h * 0.26
        bob = math.sin(t * (7 if walking else 2.0)) * (2.5 if walking else 1.0) * s

        # Beine: Huefte→Knie→Fuss
        leg_w = h * 0.062
        leg_len = ground - hip_y
        phase = math.sin(t * 7) * (0.5 if walking else 0.0)
        for sign in (1, -1):
            hip = (x + sign * body_w * 0.18, hip_y + bob)
            ang = phase * sign + 0.10 * sign
            knee = (hip[0] + math.sin(ang) * leg_len * 0.45 * facing, hip[1] + leg_len * 0.5)
            foot = (hip[0] + math.sin(ang) * leg_len * 0.8 * facing, ground - 2 * s)
            self.limb(d, hip, knee, foot, col, leg_w)
        # Rumpf
        d.rounded_rectangle([x - body_w / 2, body_top + bob, x + body_w / 2, hip_y + h * 0.04 + bob],
                            radius=body_w / 2, fill=col)
        # Arme: Schulter→Ellbogen→Hand. Ruhe = haengend mit leichtem Knick,
        # Geste = Oberarm hebt, Unterarm zeigt nach vorn.
        sh = (x, body_top + h * 0.06 + bob)
        upper = h * 0.17
        lower = h * 0.16
        arm_w = h * 0.052
        sway = math.sin(t * (7 if walking else 1.8)) * (0.45 if walking else 0.06)
        ua = 1.45 + sway
        elb = (sh[0] - facing * math.sin(ua - 1.1) * upper, sh[1] + math.cos(ua - 1.1) * upper)
        hand = (elb[0] - facing * math.sin(ua - 1.35) * lower, elb[1] + math.cos(ua - 1.35) * lower)
        self.limb(d, sh, elb, hand, arm_col, arm_w)
        g = clamp01(gesture)
        ua2 = (1.45 - sway) * (1 - g) + 0.55 * g
        fa2 = (1.55 - sway * 0.5) * (1 - g) + (-0.15) * g
        elb2 = (sh[0] + facing * math.sin(ua2) * upper * (0.35 + 0.65 * g) - facing * (1 - g) * upper * 0.1,
                sh[1] + math.cos(ua2) * upper)
        hand2 = (elb2[0] + facing * math.cos(fa2) * lower, elb2[1] + math.sin(fa2) * lower)
        self.limb(d, sh, elb2, hand2, arm_col, arm_w)
        hy = top + head_r + bob + math.sin(t * 5) * g * 1.2 * s
        d.ellipse([x - head_r, hy - head_r, x + head_r, hy + head_r], fill=col)
        # Gesicht: Augen schauen zum Gegenueber, blinzeln; Mund bewegt sich
        # beim Sprechen (gesture>0). Haar als dunklere Kappe fuer Charakter.
        hair = mix(col, (0, 0, 0), 0.38)
        # Haar als flache Kappe NUR auf dem Oberkopf (nicht ueber die Augen)
        if facing == 1:
            d.pieslice([x - head_r, hy - head_r, x + head_r, hy + head_r], 195, 330, fill=hair)
        else:
            d.pieslice([x - head_r, hy - head_r, x + head_r, hy + head_r], 210, 345, fill=hair)
        eye_dx = head_r * 0.34 * facing
        eye_y = hy + head_r * 0.10
        blink = (t % 3.7) < 0.13
        er = head_r * 0.15
        for off in (eye_dx * 0.4, eye_dx * 1.5):
            ex = x + off
            if blink:
                d.line([ex - er, eye_y, ex + er, eye_y], fill=(15, 18, 26), width=max(1, int(er * 0.7)))
            else:
                d.ellipse([ex - er, eye_y - er, ex + er, eye_y + er], fill=(248, 249, 252))
                pr = er * 0.55
                px = ex + facing * er * 0.3
                d.ellipse([px - pr, eye_y - pr, px + pr, eye_y + pr], fill=(15, 18, 26))
        mouth_x = x + head_r * 0.5 * facing
        mouth_y = hy + head_r * 0.52
        if g > 0.05:
            mh = head_r * (0.07 + 0.09 * abs(math.sin(t * 9)))
            d.ellipse([mouth_x - head_r * 0.14, mouth_y - mh, mouth_x + head_r * 0.14, mouth_y + mh],
                      fill=(15, 18, 26))
        else:
            d.arc([mouth_x - head_r * 0.2, mouth_y - head_r * 0.18, mouth_x + head_r * 0.2, mouth_y + head_r * 0.14],
                  20, 160, fill=(15, 18, 26), width=max(1, int(head_r * 0.08)))

    def speech_height(self, d, text: str, max_w: float) -> int:
        s = self.ss
        f = font(int(19 * s))
        lines = wrap_text(d, text, f, max_w - 32 * s)[:3]
        return len(lines) * int(26 * s) + int(22 * s)

    def draw_speech(self, d, x0: float, y0: float, head_x: float, head_top: float,
                    text: str, a: float, max_w: float) -> None:
        if a <= 0.01:
            return
        s = self.ss
        a = clamp01(a)
        f = font(int(19 * s))
        lines = wrap_text(d, text, f, max_w - 32 * s)[:3]
        w = min(max_w, max(text_w(d, line, f) for line in lines) + 32 * s)
        h = len(lines) * int(26 * s) + int(22 * s)
        d.rounded_rectangle([x0, y0, x0 + w * a, y0 + h], radius=12 * s, fill=(245, 246, 250, int(242 * a)))
        tx = max(x0 + 18 * s, min(head_x, x0 + w * a - 36 * s))
        tip_y = min(y0 + h + 18 * s, head_top - 2 * s)
        if tip_y > y0 + h:
            d.polygon([(tx, y0 + h - 2), (tx + 18 * s, y0 + h - 2), (tx + 5 * s, tip_y)],
                      fill=(245, 246, 250, int(242 * a)))
        if a > 0.55:
            for i, line in enumerate(lines):
                d.text((x0 + 16 * s, y0 + 11 * s + i * 26 * s), line, font=f, fill=(20, 25, 38))

    # ── Encode ──
    def render(self, total_s: float, audio: Path | None) -> Path:
        import imageio_ffmpeg

        self.plan_timing(total_s)
        # Adaptive Supersampling: ss=2 vervierfacht die Pixel + LANCZOS-Downsample
        # pro Frame (~8x Kosten, ~271 ms/Frame). Bei langen Videos waere der Render
        # unzumutbar (6-Min-Video @ ss2 ~40 min). Ab ~90s Laufzeit auf ss=1 — volles
        # 1080p, nur ohne 4x-Antialiasing (~35 ms/Frame), haelt den Render kurz.
        if self.ss > 1 and total_s > 90:
            print(f"adaptive: supersample {self.ss}->1 (Laufzeit {total_s:.0f}s)", file=sys.stderr, flush=True)
            self.ss = 1
        silent = self.out_dir / ("infographic_silent_preview.mp4" if self.preview else "infographic_silent.mp4")
        frames = int(total_s * self.fps)
        writer = imageio_ffmpeg.write_frames(
            str(silent),
            (self.width, self.height),
            fps=self.fps,
            codec="libx264",
            quality=None,
            macro_block_size=1,
            output_params=["-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        )
        writer.send(None)
        t0 = time.time()
        for i in range(frames):
            frame = self.render_frame(i / self.fps)
            writer.send(np.asarray(frame.convert("RGB"), dtype=np.uint8).tobytes())
            if i and i % (self.fps * 20) == 0:
                done = i / frames
                eta = (time.time() - t0) / done * (1 - done)
                print(f"render {done * 100:5.1f}%  eta {eta:5.0f}s", file=sys.stderr, flush=True)
        writer.close()

        final = self.out_dir / ("infographic_video_preview.mp4" if self.preview else "infographic_video_1080p.mp4")
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        bed_path: Path | None = None
        if self.music:
            try:
                bed_path = synth_music_bed(total_s, [s.start_s for s in self.scenes],
                                           self.out_dir / "music_bed.wav")
            except Exception as exc:
                print(f"music bed failed: {exc}", file=sys.stderr)
        if audio and audio.exists() and bed_path:
            # Stimme + Bett: Bett wird per Sidechain unter der Stimme geduckt.
            subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(silent), "-i", str(audio), "-i", str(bed_path),
                 "-filter_complex",
                 "[2:a][1:a]sidechaincompress=threshold=0.02:ratio=10:attack=40:release=500[duck];"
                 "[1:a][duck]amix=inputs=2:duration=first:dropout_transition=2,alimiter=limit=0.9[a]",
                 "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                 "-shortest", str(final)],
                check=True,
            )
        elif audio and audio.exists():
            subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(silent), "-i", str(audio),
                 "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                 "-shortest", str(final)],
                check=True,
            )
        elif bed_path:
            # Kein Voiceover (Preview): Bett als Tonspur — wirkt sofort fertiger.
            subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(silent), "-i", str(bed_path),
                 "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                 "-shortest", str(final)],
                check=True,
            )
        else:
            silent.replace(final)
        self.write_sidecars(final, total_s)
        return final

    def make_thumbnail(self) -> Path | None:
        """YouTube-Thumbnail (1280x720): erstes Szenenbild als Hintergrund,
        Titel als grosse Headline (shrink-to-fit), Akzent + Brand-Chip."""
        W, H = 1280, 720
        img = None
        for s in self.scenes:
            if s.image:
                path = Path(s.image)
                if not path.is_absolute():
                    path = ROOT / path
                try:
                    src_img = Image.open(path).convert("RGB")
                    scale = max(W / src_img.width, H / src_img.height)
                    sw, sh = int(src_img.width * scale), int(src_img.height * scale)
                    src_img = src_img.resize((sw, sh), Image.LANCZOS)
                    img = src_img.crop(((sw - W) // 2, (sh - H) // 2, (sw - W) // 2 + W, (sh - H) // 2 + H)).convert("RGBA")
                    break
                except Exception:
                    continue
        if img is None:
            col = np.linspace(0, 1, H, dtype=np.float32)[:, None]
            grad = (np.array(BG_TOP, dtype=np.float32)[None, None, :] * (1 - col[..., None])
                    + np.array((24, 32, 52), dtype=np.float32)[None, None, :] * col[..., None])
            img = Image.fromarray(np.repeat(grad, W, axis=1).astype(np.uint8), "RGB").convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        # Links-Gradient fuer Textkontrast
        ov = Image.new("L", (W, 1))
        for x in range(W):
            f = 1 - x / W
            ov.putpixel((x, 0), int(70 + 150 * max(0.0, f - 0.15)))
        dark = Image.new("RGBA", (W, H), (6, 9, 16, 255))
        dark.putalpha(ov.resize((W, H)))
        img.alpha_composite(dark)
        d = ImageDraw.Draw(img, "RGBA")
        accent = self.scenes[0].accent if self.scenes else ACCENTS[DEFAULT_ACCENT]
        # Brand-Chip
        f_kick = font(34)
        kw = text_w(d, self.kicker, f_kick)
        d.rounded_rectangle([54, 48, 54 + kw + 44, 110], radius=12, fill=accent)
        d.text((76, 60), self.kicker, font=f_kick, fill=(15, 18, 26))
        # Headline: Titel ggf. am Doppelpunkt/Gedankenstrich kuerzen fuer Punch
        head = self.title
        for sep in (" — ", " – ", ": "):
            if sep in head and len(head) > 38:
                head = head.split(sep)[0]
        f_head, lines = fit_text(d, head, 108, W * 0.62, 3, min_size=56)
        lh = int(f_head.size * 1.12)
        y = H - 140 - len(lines) * lh
        for line in lines:
            # Soft-Shadow fuer Lesbarkeit
            d.text((58, y + 3), line, font=f_head, fill=(0, 0, 0, 180))
            d.text((54, y), line, font=f_head, fill=INK)
            y += lh
        d.rectangle([54, y + 10, 54 + 220, y + 24], fill=accent)
        out = self.out_dir / "thumbnail.jpg"
        img.convert("RGB").save(out, quality=91)
        return out

    def write_sidecars(self, final: Path, total_s: float) -> None:
        storyboard = {
            "title": self.title,
            "renderer": "infographic",
            "duration_s": round(total_s, 2),
            "scenes": [
                {
                    "title": s.title,
                    "subtitle": s.subtitle,
                    "type": s.type,
                    "start_s": round(s.start_s, 2),
                    "duration_s": round(s.duration_s, 2),
                    "weight": s.weight,
                }
                for s in self.scenes
            ],
        }
        for name in ("storyboard_infographic.json", "storyboard_mapled.json"):
            (self.out_dir / name).write_text(json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8")
        thumb = None
        try:
            thumb = self.make_thumbnail()
        except Exception as exc:
            print(f"thumbnail failed: {exc}", file=sys.stderr)
        chapters = "\n".join(
            f"{int(s.start_s // 60):02d}:{int(s.start_s % 60):02d} {s.title}" for s in self.scenes
        )
        thumb_line = f"\n## Thumbnail\n{thumb.name}\n" if thumb else ""
        (self.out_dir / "youtube_package_infographic.md").write_text(
            f"# {self.title}\n\n## Kapitel\n{chapters}\n\n## Video\n{final.name}\n{thumb_line}", encoding="utf-8"
        )


def synth_music_bed(total_s: float, scene_starts: list[float], out_path: Path, sr: int = 44100) -> Path:
    """Prozedurales Ambient-Bett (dunkler Drone in A) + kurze Whooshes an
    Szenengrenzen. Kein Asset, keine Lizenzfrage, deterministisch."""
    import wave

    n = int(total_s * sr)
    t = np.arange(n, dtype=np.float32) / sr
    rng = np.random.default_rng(7)
    # Drone: Grundton + Quinte + Oktave, leicht verstimmt, langsame LFOs
    bed = (
        0.40 * np.sin(2 * np.pi * 55.0 * t)
        + 0.28 * np.sin(2 * np.pi * 82.41 * t * 1.001)
        + 0.18 * np.sin(2 * np.pi * 110.0 * t * 0.999)
        + 0.10 * np.sin(2 * np.pi * 164.81 * t)
    )
    lfo = 0.6 + 0.4 * np.sin(2 * np.pi * 0.05 * t + 1.3)
    shimmer = 0.5 + 0.5 * np.sin(2 * np.pi * 0.013 * t)
    bed = bed * lfo * (0.7 + 0.3 * shimmer)
    # "Luft": tiefpass-gefiltertes Rauschen (gleitender Mittelwert)
    noise = rng.standard_normal(n).astype(np.float32)
    kernel = np.ones(220, dtype=np.float32) / 220.0
    air = np.convolve(noise, kernel, mode="same") * 0.5
    mix = bed * 0.10 + air * 0.05
    # Whooshes an Szenenstarts (ausser t=0): Rausch-Sweep mit Hann-Huellkurve
    wn = int(0.55 * sr)
    env = np.hanning(wn * 2)[:wn].astype(np.float32) ** 1.5
    for start in scene_starts:
        if start < 0.3 or start > total_s - 0.5:
            continue
        i0 = int((start - 0.25) * sr)
        if i0 < 0 or i0 + wn > n:
            continue
        burst = np.convolve(rng.standard_normal(wn).astype(np.float32),
                            np.ones(60, dtype=np.float32) / 60.0, mode="same")
        sweep = np.sin(2 * np.pi * (180 + 320 * np.linspace(0, 1, wn) ** 2) * np.linspace(0, 0.55, wn))
        mix[i0:i0 + wn] += (burst * 0.5 + sweep.astype(np.float32) * 0.25) * env * 0.16
    # Fade-In/Out + Klipp-Schutz
    fade = int(1.2 * sr)
    if n > 2 * fade:
        mix[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
        mix[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    mix = np.clip(mix, -0.6, 0.6)
    pcm = (mix * 32767 * 0.5).astype(np.int16)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return out_path


def probe_duration(path: Path) -> float:
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-f", "null", "-"],
                          capture_output=True, text=True, timeout=60, check=False)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", (proc.stderr or "") + (proc.stdout or ""))
    if not m:
        return 0.0
    h, mn, sec = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(sec)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Render an infographic-style briefing video.")
    p.add_argument("--assets", help="video_assets.json (title, voice_script, scenes)")
    p.add_argument("--scenes-json", help="scenes.json Kompat-Format {title, scenes}")
    p.add_argument("--script", help="Sprecher-Skript (txt) fuer Untertitel/Timing")
    p.add_argument("--audio", help="Audiospur (bestimmt Gesamtdauer)")
    p.add_argument("--title", default="Briefing")
    p.add_argument("--out", required=True)
    p.add_argument("--duration", type=float, default=0.0)
    p.add_argument("--fps", type=int, default=25)
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--supersample", type=int, default=2)
    p.add_argument("--preview", action="store_true")
    p.add_argument("--no-music", action="store_true", help="Musikbett/Whooshes deaktivieren")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    assets: dict[str, Any] = {}
    if args.assets and Path(args.assets).exists():
        assets = json.loads(Path(args.assets).read_text(encoding="utf-8"))
    elif args.scenes_json and Path(args.scenes_json).exists():
        assets = json.loads(Path(args.scenes_json).read_text(encoding="utf-8"))
    if args.title and not assets.get("title"):
        assets["title"] = args.title
    if args.script and Path(args.script).exists() and not assets.get("voice_script"):
        assets["voice_script"] = Path(args.script).read_text(encoding="utf-8", errors="replace")

    audio = Path(args.audio) if args.audio else None
    total = args.duration or 0.0
    if audio and audio.exists():
        total = probe_duration(audio) or total
    if total <= 0:
        words = len(re.findall(r"\w+", str(assets.get("voice_script") or "")))
        total = max(20.0, words / 2.35 + 6.0)

    r = Renderer(args, assets)
    final = r.render(total, audio if audio and audio.exists() else None)
    print(str(final))


if __name__ == "__main__":
    main()
