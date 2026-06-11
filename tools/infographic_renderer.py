#!/usr/bin/env python3
"""Infographic motion renderer — szenenbasierte Erklaervideo-Engine.

Ersetzt die monotone Map-only-Optik des Briefing-Renderers durch ein
Szenensystem im Infografik-Stil: animierte Piktogramm-Figuren, Counter,
Balken, Timelines, Zitate, Vergleiche — die Weltkarte ist nur noch EIN
Szenentyp von vielen.

Eingabe ist das VIDEO_ASSETS_JSON des Normalizers (title, voice_script,
scenes[]). Jede Szene traegt optional "type" + typspezifische Daten;
Szenen ohne type fallen auf map (route vorhanden) bzw. list zurueck —
damit bleiben alte Assets renderbar.

Abhaengigkeiten: nur PIL + numpy + imageio_ffmpeg (kein Blender, kein
matplotlib). Audio wird nach dem Encode gemuxt. Letzte stdout-Zeile ist
der finale MP4-Pfad (Modul-Contract).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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
}
DEFAULT_ACCENT = "gold"


# ─── Easing / Mathe ───────────────────────────────────────────────────────
def clamp01(t: float) -> float:
    return max(0.0, min(1.0, t))


def ease_out_cubic(t: float) -> float:
    t = clamp01(t)
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = clamp01(t)
    return t * t * (3 - 2 * t)


def ease_out_back(t: float) -> float:
    t = clamp01(t)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def stagger(t: float, index: int, total: int, overlap: float = 0.55) -> float:
    """Zeitversatz pro Element: Element i startet bei i/total*(1-overlap)."""
    if total <= 1:
        return clamp01(t)
    window = 1.0 / (total - (total - 1) * overlap) if total else 1.0
    start = index * window * (1 - overlap)
    return clamp01((t - start) / max(window, 1e-6))


def mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(round(a[i] + (b[i] - a[i]) * clamp01(t))) for i in range(3))


def bbox(x0: float, y0: float, x1: float, y1: float) -> list[float]:
    """PIL verlangt x0<=x1, y0<=y1 — normalisiert (z.B. bei gespiegelten Figuren)."""
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


# ─── Fonts ────────────────────────────────────────────────────────────────
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def find_font(name: str) -> str:
    for d in FONT_DIRS:
        p = Path(d) / name
        if p.exists():
            return str(p)
    return ""


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    key = ("b" if bold else "r", size)
    if key not in _FONT_CACHE:
        path = find_font(FONT_BOLD if bold else FONT_REGULAR) or find_font("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
        _FONT_CACHE[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    return _FONT_CACHE[key]


def text_width(d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont) -> float:
    return d.textlength(text, font=f)


def wrap_text(d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_w: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip()
        if text_width(d, cand, f) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


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
        self.start_s = 0.0
        self.duration_s = 0.0

    def resolve_type(self, explicit: str) -> str:
        known = {"hook", "stat", "bars", "people", "figures", "timeline", "quote", "compare", "map", "list", "outro"}
        if explicit in known:
            return explicit
        # Heuristik fuer Alt-Assets ohne type
        if self.raw.get("stat"):
            return "stat"
        if self.raw.get("bars"):
            return "bars"
        if self.raw.get("people"):
            return "people"
        if self.raw.get("timeline"):
            return "timeline"
        if self.raw.get("quote"):
            return "quote"
        if self.raw.get("compare"):
            return "compare"
        if len(self.route) >= 2:
            return "map"
        return "list"


# ─── Welt-Geometrie (fuer map-Szenen) ─────────────────────────────────────
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
        # grobe Aliasse, mehrsprachig
        alias = {
            "usa": "united states of america", "united states": "united states of america",
            "vereinigte staaten": "united states of america", "uk": "united kingdom",
            "grossbritannien": "united kingdom", "deutschland": "germany",
            "frankreich": "france", "russland": "russia", "russian federation": "russia",
            "china": "china", "suedkorea": "south korea", "nordkorea": "north korea",
            "tuerkei": "turkey", "iran": "iran", "japan": "japan", "indien": "india",
            "taiwan": "taiwan", "ukraine": "ukraine", "israel": "israel",
            "saudi arabien": "saudi arabia", "spanien": "spain", "italien": "italy",
            "polen": "poland", "schweiz": "switzerland", "oesterreich": "austria",
        }
        for k, v in alias.items():
            if v in self.centroids:
                self.centroids[k] = self.centroids[v]
        # abstrakte Regionen
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
    def __init__(self, args: argparse.Namespace, assets: dict[str, Any]):
        self.out_dir = Path(args.out)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.preview = bool(args.preview)
        self.width = 854 if self.preview else int(args.width)
        self.height = 480 if self.preview else int(args.height)
        self.fps = 12 if self.preview else int(args.fps)
        self.ss = 1 if self.preview else max(1, int(args.supersample))
        self.title = str(assets.get("title") or args.title or "Briefing")
        self.scenes = [Scene(s, i + 1) for i, s in enumerate(assets.get("scenes") or []) if isinstance(s, dict)]
        if not self.scenes:
            self.scenes = [Scene({"title": self.title, "type": "hook"}, 1)]
        self.voice_script = str(assets.get("voice_script") or "")
        self.audio_path = Path(args.audio).resolve() if args.audio else None
        self.transition_s = 0.55
        self.source_note = str(assets.get("source_line") or "DeepDive-Auswertung | Visualisierung schematisch")
        self.kicker = str(assets.get("kicker") or "BRIEFING")

    # ── Timing ──
    def plan_timing(self, total_s: float) -> None:
        # Outro/Hook bekommen feste Anteile, Rest nach weight
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
        """Voice-Script proportional auf Szenen verteilen (satzweise)."""
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
            chunk = sentences[cursor:cursor + take]
            cursor += take
            s.narration = " ".join(chunk).strip()
        if cursor < n and self.scenes:
            self.scenes[-1].narration = (self.scenes[-1].narration + " " + " ".join(sentences[cursor:])).strip()

    # ── Frame-Aufbau ──
    def render_frame(self, t_global: float) -> Image.Image:
        W, H = self.width * self.ss, self.height * self.ss
        img = self.background(W, H, t_global)
        d = ImageDraw.Draw(img, "RGBA")

        scene, t_local, nxt = self.scene_at(t_global)
        prog = clamp01(t_local / max(scene.duration_s, 1e-6))

        # Szene zeichnen (mit Crossfade zur naechsten)
        self.draw_scene(img, d, scene, t_local, prog)
        if nxt is not None and scene.duration_s - t_local < self.transition_s:
            alpha = 1 - (scene.duration_s - t_local) / self.transition_s
            overlay = self.background(W, H, t_global)
            od = ImageDraw.Draw(overlay, "RGBA")
            self.draw_scene(overlay, od, nxt, 0.0, 0.0)
            img = Image.blend(img, overlay, ease_in_out(alpha))
            d = ImageDraw.Draw(img, "RGBA")

        self.draw_chrome(d, scene, t_global, W, H)
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
        col = np.linspace(0, 1, H, dtype=np.float32)[:, None]
        grad = (np.array(BG_TOP, dtype=np.float32)[None, None, :] * (1 - col[..., None])
                + np.array(BG_BOTTOM, dtype=np.float32)[None, None, :] * col[..., None])
        arr = np.repeat(grad, W, axis=1).astype(np.uint8)
        img = Image.fromarray(arr, "RGB").convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        # dezentes Punkt-Raster, langsam driftend (Parallax-Leben)
        step = int(64 * self.ss)
        drift = int((t * 4 * self.ss) % step)
        r = max(1, self.ss)
        for y in range(-step, H + step, step):
            for x in range(-step, W + step, step):
                d.ellipse([x + drift - r, y - r, x + drift + r, y + r], fill=(255, 255, 255, 10))
        return img

    # ── Chrome (Header, Subtitles, Progress) ──
    def draw_chrome(self, d: ImageDraw.ImageDraw, scene: Scene, t: float, W: int, H: int) -> None:
        s = self.ss
        m = int(54 * s)
        if scene.type not in {"hook", "outro"}:
            # Kicker-Chip + Kapiteltitel
            f_kick = font(int(15 * s))
            kick = f"{self.kicker} {scene.index:02d}"
            kw = text_width(d, kick, f_kick)
            d.rounded_rectangle([m, m, m + kw + 28 * s, m + 30 * s], radius=6 * s, fill=scene.accent)
            d.text((m + 14 * s, m + 7 * s), kick, font=f_kick, fill=(15, 18, 26))
            f_title = font(int(34 * s))
            d.text((m, m + 42 * s), scene.title, font=f_title, fill=INK)
            if scene.subtitle:
                d.text((m, m + 88 * s), scene.subtitle, font=font(int(19 * s), bold=False), fill=INK_DIM)
        # Untertitel-Band (Wort-Reveal)
        self.draw_subtitles(d, scene, t, W, H)
        # Footer + Fortschritt
        d.text((m, H - 34 * s), self.source_note, font=font(int(12 * s), bold=False), fill=(120, 130, 150))
        total = self.scenes[-1].start_s + self.scenes[-1].duration_s
        frac = clamp01((scene.start_s + t) / max(total, 1e-6))
        d.rectangle([0, H - 6 * s, int(W * frac), H], fill=scene.accent + (220,))

    def draw_subtitles(self, d: ImageDraw.ImageDraw, scene: Scene, t: float, W: int, H: int) -> None:
        if not scene.narration:
            return
        s = self.ss
        words = scene.narration.split()
        if not words:
            return
        visible = max(1, int(len(words) * clamp01(t / max(scene.duration_s - 0.4, 0.1)) + 2))
        # rollendes Fenster: max ~14 Woerter
        win = 14
        start = max(0, visible - win)
        text = " ".join(words[start:visible])
        f = font(int(21 * s), bold=False)
        lines = wrap_text(d, text, f, W * 0.62)[-2:]
        y = H - int(96 * s)
        for i, line in enumerate(lines):
            lw = text_width(d, line, f)
            x = (W - lw) / 2
            pad = 12 * s
            ly = y + i * int(30 * s)
            d.rounded_rectangle([x - pad, ly - 5 * s, x + lw + pad, ly + 25 * s], radius=8 * s, fill=(10, 13, 22, 215))
            d.text((x, ly), line, font=f, fill=INK)

    # ── Szenen-Dispatch ──
    def draw_scene(self, img: Image.Image, d: ImageDraw.ImageDraw, scene: Scene, t: float, prog: float) -> None:
        fn = {
            "hook": self.scene_hook, "stat": self.scene_stat, "bars": self.scene_bars,
            "people": self.scene_people, "figures": self.scene_figures,
            "timeline": self.scene_timeline, "quote": self.scene_quote,
            "compare": self.scene_compare, "map": self.scene_map,
            "list": self.scene_list, "outro": self.scene_outro,
        }.get(scene.type, self.scene_list)
        fn(img, d, scene, t, prog)

    # ── Szenentypen ──
    def scene_hook(self, img, d, scene: Scene, t: float, prog: float) -> None:
        W, H, s = img.width, img.height, self.ss
        a = ease_out_cubic(t / 0.9)
        # Akzent-Formen
        d.ellipse([W * 0.78 - 160 * s * a, H * 0.22 - 160 * s * a, W * 0.78 + 160 * s * a, H * 0.22 + 160 * s * a],
                  outline=scene.accent + (90,), width=int(3 * s))
        d.ellipse([W * 0.84, H * 0.70, W * 0.84 + 22 * s, H * 0.70 + 22 * s], fill=scene.accent + (140,))
        f_big = font(int(64 * s))
        lines = wrap_text(d, scene.title, f_big, W * 0.74)
        base_y = H * 0.36 - len(lines) * 38 * s
        for i, line in enumerate(lines):
            la = ease_out_cubic(stagger(t / 1.1, i, max(1, len(lines))))
            y = base_y + i * int(78 * s) + (1 - la) * 36 * s
            d.text((W * 0.13, y), line, font=f_big, fill=INK + (int(255 * la),))
        bar_w = W * 0.30 * ease_out_cubic((t - 0.4) / 0.8)
        d.rectangle([W * 0.13, base_y + len(lines) * 78 * s + 18 * s, W * 0.13 + bar_w, base_y + len(lines) * 78 * s + 28 * s], fill=scene.accent)
        if scene.subtitle:
            sa = ease_out_cubic((t - 0.7) / 0.8)
            d.text((W * 0.13, base_y + len(lines) * 78 * s + 52 * s), scene.subtitle,
                   font=font(int(26 * s), bold=False), fill=INK_DIM + (int(255 * sa),))

    def scene_stat(self, img, d, scene: Scene, t: float, prog: float) -> None:
        W, H, s = img.width, img.height, self.ss
        stat = scene.raw.get("stat") or {}
        value = float(stat.get("value") or 0.0)
        unit = str(stat.get("unit") or "")
        label = str(stat.get("label") or scene.subtitle or scene.title)
        maximum = float(stat.get("max") or (100.0 if value <= 100 else value * 1.25)) or 1.0
        a = ease_out_cubic(t / 1.6)
        shown = value * a
        # Donut
        cx, cy = W * 0.28, H * 0.58
        r = min(150 * s, H * 0.24)
        d.arc([cx - r, cy - r, cx + r, cy + r], 0, 360, fill=PANEL_LINE, width=int(20 * s))
        sweep = 360 * clamp01(value / maximum) * a
        d.arc([cx - r, cy - r, cx + r, cy + r], -90, -90 + sweep, fill=scene.accent, width=int(20 * s))
        num = f"{shown:,.0f}" if value >= 10 else f"{shown:.1f}"
        num = num.replace(",", ".")
        f_num = font(int(64 * s))
        nw = text_width(d, num, f_num)
        d.text((cx - nw / 2, cy - 44 * s), num, font=f_num, fill=INK)
        if unit:
            uw = text_width(d, unit, font(int(26 * s)))
            d.text((cx - uw / 2, cy + 28 * s), unit, font=font(int(26 * s)), fill=scene.accent)
        # Label rechts
        f_lab = font(int(32 * s))
        la = ease_out_cubic((t - 0.4) / 1.0)
        for i, line in enumerate(wrap_text(d, label, f_lab, W * 0.38)):
            d.text((W * 0.52, H * 0.40 + i * 44 * s + (1 - la) * 24 * s), line, font=f_lab, fill=INK + (int(255 * la),))
        self.draw_bullet_block(d, scene, t - 0.8, W * 0.52, H * 0.40 + 120 * s)

    def scene_bars(self, img, d, scene: Scene, t: float, prog: float) -> None:
        W, H, s = img.width, img.height, self.ss
        bars = [b for b in (scene.raw.get("bars") or []) if isinstance(b, dict)][:6]
        if not bars:
            return self.scene_list(img, d, scene, t, prog)
        max_v = max(float(b.get("value") or 0.0) for b in bars) or 1.0
        x0, x1 = W * 0.14, W * 0.86
        y = H * 0.34
        gap = min(86 * s, (H * 0.50) / len(bars))
        f_lab = font(int(20 * s), bold=False)
        f_val = font(int(20 * s))
        for i, b in enumerate(bars):
            a = ease_out_cubic(stagger(t / 1.8, i, len(bars)))
            v = float(b.get("value") or 0.0)
            label = str(b.get("label") or f"#{i + 1}")
            w = (x1 - x0) * (v / max_v) * a
            col = ACCENTS.get(str(b.get("color") or "").lower(), scene.accent)
            d.text((x0, y - 26 * s), label, font=f_lab, fill=INK_DIM)
            d.rounded_rectangle([x0, y, x1, y + 26 * s], radius=8 * s, fill=PANEL)
            if w > 16 * s:
                d.rounded_rectangle([x0, y, x0 + w, y + 26 * s], radius=8 * s, fill=col)
            val_txt = str(b.get("display") or (f"{v:,.0f}".replace(",", ".")))
            d.text((x0 + w + 12 * s, y + 1 * s), val_txt, font=f_val, fill=INK + (int(255 * a),))
            y += gap

    def scene_people(self, img, d, scene: Scene, t: float, prog: float) -> None:
        W, H, s = img.width, img.height, self.ss
        people = scene.raw.get("people") or {}
        count = max(1, min(100, int(people.get("count") or 10)))
        highlight = max(0, min(count, int(people.get("highlight") or 0)))
        label = str(people.get("label") or scene.subtitle or "")
        cols = 10 if count > 20 else max(4, min(10, count))
        rows = math.ceil(count / cols)
        ph = min(64 * s, (H * 0.36) / rows)
        grid_w = cols * ph * 0.62
        x0 = (W - grid_w) / 2
        y0 = H * 0.34
        for i in range(count):
            r_, c_ = divmod(i, cols)
            a = ease_out_back(stagger(t / 1.6, i, count, overlap=0.8))
            if a <= 0.01:
                continue
            x = x0 + c_ * ph * 0.62
            y = y0 + r_ * ph * 1.06 + (1 - a) * 18 * s
            col = scene.accent if i < highlight else (70, 80, 100)
            bob = math.sin(t * 2.2 + i * 0.7) * 1.5 * s
            self.draw_person_glyph(d, x, y + bob, ph * a, col)
        if label:
            f = font(int(28 * s))
            la = ease_out_cubic((t - 0.9) / 0.9)
            lw = text_width(d, label, f)
            ly = min(y0 + rows * ph * 1.06 + 26 * s, H - 150 * s)
            d.text(((W - lw) / 2, ly), label, font=f, fill=INK + (int(255 * la),))
        if highlight:
            ratio = f"{highlight} von {count}"
            f2 = font(int(44 * s))
            rw = text_width(d, ratio, f2)
            d.text((W - rw - 60 * s, 60 * s), ratio, font=f2,
                   fill=scene.accent + (int(255 * ease_out_cubic(t / 0.8)),))

    def scene_figures(self, img, d, scene: Scene, t: float, prog: float) -> None:
        """Zwei Akteurs-Figuren mit Sprechblasen — der 'Infografik-Show'-Moment."""
        W, H, s = img.width, img.height, self.ss
        fig = scene.raw.get("figures") or {}
        left = fig.get("left") or {}
        right = fig.get("right") or {}
        l_label = str(left.get("label") or (scene.route[0] if scene.route else "A"))
        r_label = str(right.get("label") or (scene.route[1] if len(scene.route) > 1 else "B"))
        l_say = str(left.get("say") or "")
        r_say = str(right.get("say") or "")
        l_col = ACCENTS.get(str(left.get("color") or "").lower(), ACCENTS["blue"])
        r_col = ACCENTS.get(str(right.get("color") or "").lower(), ACCENTS["red"])
        ground = H * 0.82
        walk_l = ease_out_cubic(t / 1.1)
        walk_r = ease_out_cubic((t - 0.25) / 1.1)
        lx = W * (-0.1 + 0.34 * walk_l)
        rx = W * (1.1 - 0.34 * walk_r)
        body_h = H * 0.36
        l_speaking = bool(l_say) and t > 1.3
        r_speaking = bool(r_say) and t > 2.1
        d.line([W * 0.08, ground + 2 * s, W * 0.92, ground + 2 * s], fill=PANEL_LINE, width=int(2 * s))
        self.draw_character(d, lx, ground, body_h, l_col, t, walking=walk_l < 0.99, facing=1,
                            gesture=ease_in_out((t - 1.3) / 0.5) if l_speaking else 0.0)
        self.draw_character(d, rx, ground, body_h, r_col, t + 0.4, walking=walk_r < 0.99, facing=-1,
                            gesture=ease_in_out((t - 2.1) / 0.5) if r_speaking else 0.0)
        # Namens-Chips auf der Brust
        f_name = font(int(20 * s))
        for x, label in ((lx, l_label), (rx, r_label)):
            lw = text_width(d, label, f_name)
            cy = ground - body_h * 0.50
            d.rounded_rectangle([x - lw / 2 - 9 * s, cy - 15 * s, x + lw / 2 + 9 * s, cy + 15 * s],
                                radius=7 * s, fill=(10, 13, 22, 215))
            d.text((x - lw / 2, cy - 11 * s), label, font=f_name, fill=INK)
        # Sprechblasen: getrennte Spalten links/rechts, Top-anchored unterhalb
        # des Headers — kollisionsfrei mit Titel, Figuren und untereinander.
        bubble_top = H * 0.30
        if l_speaking:
            self.draw_speech(d, W * 0.085, bubble_top, lx, ground - body_h - 8 * s, l_say,
                             ease_out_back((t - 1.3) / 0.5), W * 0.36)
        if r_speaking:
            self.draw_speech(d, W * 0.555, bubble_top, rx, ground - body_h - 8 * s, r_say,
                             ease_out_back((t - 2.1) / 0.5), W * 0.36)

    def scene_timeline(self, img, d, scene: Scene, t: float, prog: float) -> None:
        W, H, s = img.width, img.height, self.ss
        items = [i for i in (scene.raw.get("timeline") or []) if isinstance(i, dict)][:6]
        if not items:
            return self.scene_list(img, d, scene, t, prog)
        y = H * 0.56
        x0, x1 = W * 0.12, W * 0.88
        line_a = ease_out_cubic(t / 1.0)
        d.line([x0, y, x0 + (x1 - x0) * line_a, y], fill=PANEL_LINE, width=int(4 * s))
        n = len(items)
        f_date = font(int(20 * s))
        f_txt = font(int(17 * s), bold=False)
        for i, item in enumerate(items):
            a = ease_out_back(stagger(t / 2.0, i, n, overlap=0.45))
            if a <= 0.01:
                continue
            x = x0 + (x1 - x0) * (i + 0.5) / n
            r = 11 * s * a
            d.ellipse([x - r, y - r, x + r, y + r], fill=scene.accent)
            date = str(item.get("date") or item.get("year") or "")
            txt = str(item.get("text") or item.get("label") or "")
            up = i % 2 == 0
            ty = y - 130 * s if up else y + 36 * s
            dw = text_width(d, date, f_date)
            d.text((x - dw / 2, ty), date, font=f_date, fill=scene.accent + (int(255 * a),))
            for j, line in enumerate(wrap_text(d, txt, f_txt, 200 * s)[:3]):
                lw2 = text_width(d, line, f_txt)
                d.text((x - lw2 / 2, ty + 28 * s + j * 22 * s), line, font=f_txt, fill=INK + (int(235 * a),))

    def scene_quote(self, img, d, scene: Scene, t: float, prog: float) -> None:
        W, H, s = img.width, img.height, self.ss
        q = scene.raw.get("quote") or {}
        text = str(q.get("text") or scene.subtitle or scene.title)
        by = str(q.get("by") or "")
        a = ease_out_cubic(t / 0.8)
        f_mark = font(int(150 * s))
        d.text((W * 0.12, H * 0.20 - 40 * s), "„", font=f_mark, fill=scene.accent + (int(220 * a),))
        f_q = font(int(34 * s))
        lines = wrap_text(d, text, f_q, W * 0.64)[:4]
        base = H * 0.30
        for i, line in enumerate(lines):
            la = ease_out_cubic(stagger(t / 1.6, i, len(lines)))
            d.text((W * 0.20, base + i * 50 * s + (1 - la) * 20 * s), line, font=f_q, fill=INK + (int(255 * la),))
        if by:
            ba = ease_out_cubic((t - 1.0) / 0.8)
            ay = min(base + len(lines) * 50 * s + 26 * s, H - 140 * s)
            d.line([W * 0.20, ay + 12 * s, W * 0.20 + 70 * s * ba, ay + 12 * s], fill=scene.accent, width=int(4 * s))
            d.text((W * 0.20 + 86 * s, ay), by, font=font(int(22 * s), bold=False), fill=INK_DIM + (int(255 * ba),))

    def scene_compare(self, img, d, scene: Scene, t: float, prog: float) -> None:
        W, H, s = img.width, img.height, self.ss
        comp = scene.raw.get("compare") or {}
        panels = [comp.get("left") or {}, comp.get("right") or {}]
        cols = [ACCENTS["blue"], ACCENTS["red"]]
        for i, (panel, col) in enumerate(zip(panels, cols)):
            a = ease_out_cubic(stagger(t / 1.2, i, 2, overlap=0.3))
            x0 = W * (0.10 + 0.42 * i)
            x1 = x0 + W * 0.36
            y0, y1 = H * 0.30, H * 0.78
            slide = (1 - a) * 30 * s * (-1 if i == 0 else 1)
            d.rounded_rectangle([x0 + slide, y0, x1 + slide, y1], radius=16 * s, fill=PANEL + (int(235 * a),), outline=PANEL_LINE)
            label = str(panel.get("label") or ("A" if i == 0 else "B"))
            value = str(panel.get("value") or "")
            f_l = font(int(26 * s))
            d.text((x0 + 28 * s + slide, y0 + 26 * s), label, font=f_l, fill=col + (int(255 * a),))
            if value:
                f_v = font(int(54 * s))
                d.text((x0 + 28 * s + slide, y0 + 72 * s), value, font=f_v, fill=INK + (int(255 * a),))
            points = [str(p) for p in (panel.get("points") or []) if str(p).strip()][:4]
            f_p = font(int(19 * s), bold=False)
            for j, p in enumerate(points):
                pa = ease_out_cubic(stagger((t - 0.5) / 1.4, j, max(1, len(points))))
                py = y0 + 160 * s + j * 44 * s
                d.ellipse([x0 + 28 * s + slide, py + 6 * s, x0 + 38 * s + slide, py + 16 * s], fill=col + (int(255 * pa),))
                for k, line in enumerate(wrap_text(d, p, f_p, x1 - x0 - 80 * s)[:2]):
                    d.text((x0 + 52 * s + slide, py + k * 24 * s), line, font=f_p, fill=INK + (int(235 * pa),))
        f_vs = font(int(34 * s))
        va = ease_out_back((t - 0.8) / 0.6)
        if va > 0:
            vw = text_width(d, "VS", f_vs)
            cx = W / 2
            d.ellipse([cx - 34 * s, H * 0.50 - 34 * s, cx + 34 * s, H * 0.50 + 34 * s], fill=scene.accent + (int(255 * clamp01(va)),))
            d.text((cx - vw / 2, H * 0.50 - 20 * s), "VS", font=f_vs, fill=(15, 18, 26, int(255 * clamp01(va))))

    def scene_map(self, img, d, scene: Scene, t: float, prog: float) -> None:
        W, H, s = img.width, img.height, self.ss
        WORLD.load()
        pts = [(name, WORLD.lookup(name)) for name in scene.route]
        pts = [(n, p) for n, p in pts if p]
        # Projektion: zoom auf Route-BBox (mit Rand), sonst Welt
        if pts:
            lons = [p[0] for _, p in pts]
            lats = [p[1] for _, p in pts]
            pad_lon = max(18.0, (max(lons) - min(lons)) * 0.45)
            pad_lat = max(12.0, (max(lats) - min(lats)) * 0.45)
            lon0, lon1 = min(lons) - pad_lon, max(lons) + pad_lon
            lat0, lat1 = min(lats) - pad_lat, max(lats) + pad_lat
        else:
            lon0, lon1, lat0, lat1 = -170, 190, -58, 78
        # Aspect angleichen
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
        # Nie ueber die Welt hinaus aufblasen — sonst schrumpfen die Kontinente
        # zu einem unkenntlichen Band (Interkontinental-Routen).
        lat0, lat1 = max(lat0, -62.0), min(lat1, 84.0)
        lon0, lon1 = max(lon0, -185.0), min(lon1, 200.0)

        def proj(lon: float, lat: float) -> tuple[float, float]:
            x = (lon - lon0) / (lon1 - lon0) * W
            y = (1 - (lat - lat0) / (lat1 - lat0)) * H
            return x, y

        for poly in WORLD.polys:
            pp = [proj(x_, y_) for x_, y_ in poly]
            if all(p[0] < -W * 0.2 or p[0] > W * 1.2 or p[1] < -H * 0.2 or p[1] > H * 1.2 for p in pp):
                continue
            d.polygon(pp, fill=(30, 39, 58), outline=(50, 62, 88))
        # Route-Boegen sequentiell animieren
        if len(pts) >= 2:
            seg_t = clamp01(t / max(1.6, min(4.0, scene.duration_s * 0.55))) * (len(pts) - 1)
            for i in range(len(pts) - 1):
                a = clamp01(seg_t - i)
                if a <= 0:
                    break
                x1, y1 = proj(*pts[i][1])
                x2, y2 = proj(*pts[i + 1][1])
                mxp, myp = (x1 + x2) / 2, min(y1, y2) - abs(x2 - x1) * 0.22 - 30 * s
                steps = 42
                path = []
                for k in range(int(steps * a) + 1):
                    u = k / steps
                    bx = (1 - u) ** 2 * x1 + 2 * (1 - u) * u * mxp + u ** 2 * x2
                    by = (1 - u) ** 2 * y1 + 2 * (1 - u) * u * myp + u ** 2 * y2
                    path.append((bx, by))
                if len(path) > 1:
                    d.line(path, fill=scene.accent + (235,), width=int(4 * s))
                    hx, hy = path[-1]
                    d.ellipse([hx - 7 * s, hy - 7 * s, hx + 7 * s, hy + 7 * s], fill=scene.accent)
        # Marker + Labels
        f_m = font(int(19 * s))
        placed: list[tuple[float, float]] = []
        for i, (name, p) in enumerate(pts):
            a = ease_out_back(stagger(t / 1.4, i, max(1, len(pts)), overlap=0.4))
            if a <= 0.01:
                continue
            x, y = proj(*p)
            pulse = 1 + 0.18 * math.sin(t * 3 + i)
            r = 9 * s * a
            d.ellipse([x - r * pulse - 6 * s, y - r * pulse - 6 * s, x + r * pulse + 6 * s, y + r * pulse + 6 * s], outline=scene.accent + (90,), width=int(2 * s))
            d.ellipse([x - r, y - r, x + r, y + r], fill=scene.accent)
            lw = text_width(d, name, f_m)
            ly = y
            # Label-Kollision: nahe Punkte bekommen das Label unterhalb
            for px, py in placed:
                if abs(x - px) < 140 * s and abs(ly - py) < 34 * s:
                    ly = py + 36 * s
            placed.append((x, ly))
            d.rounded_rectangle([x + 14 * s, ly - 14 * s, x + 14 * s + lw + 16 * s, ly + 14 * s], radius=6 * s, fill=(10, 13, 22, 220))
            d.text((x + 22 * s, ly - 9 * s), name, font=f_m, fill=INK)
        self.draw_bullet_block(d, scene, t - 0.8, W * 0.66, H * 0.50, boxed=True)

    def scene_list(self, img, d, scene: Scene, t: float, prog: float) -> None:
        W, H, s = img.width, img.height, self.ss
        self.draw_bullet_block(d, scene, t, W * 0.14, H * 0.38, big=True)
        # dekorative Seitenlinie
        a = ease_out_cubic(t / 0.8)
        d.rectangle([W * 0.10, H * 0.38, W * 0.10 + 6 * s, H * 0.38 + (H * 0.34) * a], fill=scene.accent)

    def scene_outro(self, img, d, scene: Scene, t: float, prog: float) -> None:
        W, H, s = img.width, img.height, self.ss
        a = ease_out_cubic(t / 1.0)
        f_big = font(int(52 * s))
        lines = wrap_text(d, scene.title or self.title, f_big, W * 0.7)
        y = H * 0.38 - len(lines) * 30 * s
        for i, line in enumerate(lines):
            lw = text_width(d, line, f_big)
            d.text(((W - lw) / 2, y + i * 66 * s), line, font=f_big, fill=INK + (int(255 * a),))
        sub = scene.subtitle or "Quellen und Details im Begleittext"
        sa = ease_out_cubic((t - 0.5) / 0.8)
        f_sub = font(int(24 * s), bold=False)
        sw = text_width(d, sub, f_sub)
        d.text(((W - sw) / 2, y + len(lines) * 66 * s + 26 * s), sub, font=f_sub, fill=INK_DIM + (int(255 * sa),))
        bw = 90 * s * ease_out_cubic((t - 0.3) / 0.8)
        d.rectangle([(W - bw) / 2, y - 36 * s, (W + bw) / 2, y - 28 * s], fill=scene.accent)

    # ── Bausteine ──
    def draw_bullet_block(self, d, scene: Scene, t: float, x: float, y: float, boxed: bool = False, big: bool = False) -> None:
        if not scene.bullets or t < 0:
            return
        s = self.ss
        f = font(int((24 if big else 18) * s), bold=False)
        lh = int((46 if big else 34) * s)
        if boxed:
            widest = max(text_width(d, b, f) for b in scene.bullets)
            d.rounded_rectangle([x - 20 * s, y - 16 * s, x + min(widest, 360 * s) + 36 * s, y + len(scene.bullets) * lh + 4 * s],
                                radius=10 * s, fill=(10, 13, 22, 200), outline=PANEL_LINE)
        for i, b in enumerate(scene.bullets):
            a = ease_out_cubic(stagger(t / 1.6, i, len(scene.bullets)))
            if a <= 0.01:
                continue
            by = y + i * lh + (1 - a) * 14 * s
            d.ellipse([x, by + 8 * s, x + 10 * s, by + 18 * s], fill=scene.accent + (int(255 * a),))
            line = wrap_text(d, b, f, 560 * s)[0]
            d.text((x + 24 * s, by), line, font=f, fill=INK + (int(240 * a),))

    def draw_person_glyph(self, d, x: float, y: float, h: float, col: tuple) -> None:
        """ISOTYPE-Piktogramm: Kopf + Rumpf, fuer Mengen-Darstellungen."""
        r = h * 0.16
        d.ellipse([x - r, y, x + r, y + 2 * r], fill=col)
        bw = h * 0.42
        d.rounded_rectangle([x - bw / 2, y + 2.3 * r, x + bw / 2, y + h], radius=bw / 2, fill=col)

    def draw_character(self, d, x: float, ground: float, h: float, col: tuple, t: float,
                       walking: bool, facing: int, gesture: float = 0.0) -> None:
        """Flat-Design-Figur: Kopf, schlanker Rumpf, sichtbare Beine, Arme mit
        Idle-Sway; `gesture` hebt den vorderen Arm zum Zeigen (0..1)."""
        s = self.ss
        head_r = h * 0.11
        top = ground - h
        body_top = top + head_r * 2.3
        body_bot = ground - h * 0.30
        body_w = h * 0.27
        bob = math.sin(t * (7 if walking else 2.0)) * (3 if walking else 1.2) * s
        # Beine
        hip_y = body_bot - h * 0.02 + bob
        phase = math.sin(t * 7) * (0.55 if walking else 0.04)
        for sign in (1, -1):
            # leichte Grundspreizung, damit die Beine im Stand nicht zu EINER
            # Linie verschmelzen (Lollipop-Effekt)
            ang = phase * sign + 0.13 * sign
            kx = x + math.sin(ang) * (ground - hip_y) * 0.55 * facing
            d.line([x + sign * body_w * 0.18, hip_y, kx, ground - 2 * s], fill=col, width=int(h * 0.075))
            d.ellipse(bbox(kx - h * 0.05, ground - h * 0.04, kx + h * 0.085 * facing, ground), fill=col)
        # Rumpf
        d.rounded_rectangle([x - body_w / 2, body_top + bob, x + body_w / 2, body_bot + bob],
                            radius=body_w / 2, fill=col)
        # Arme: 1.35rad = haengend, 0.22rad = nach vorn zeigend
        sh_y = body_top + h * 0.05 + bob
        arm_len = h * 0.30
        sway = math.sin(t * (7 if walking else 1.8)) * (0.5 if walking else 0.08)
        back_ang = 1.35 + sway
        bx2 = x - facing * math.cos(back_ang - 1.35 + 0.35) * arm_len * 0.4 - facing * math.sin(back_ang) * 0 
        bx2 = x - facing * math.sin(back_ang - 1.0) * arm_len
        by2 = sh_y + math.sin(back_ang) * arm_len
        d.line([x, sh_y, bx2, by2], fill=col, width=int(h * 0.055))
        front_idle = 1.35 - sway
        front_ang = front_idle + (0.22 - front_idle) * clamp01(gesture)
        fx = x + facing * math.cos(front_ang) * arm_len
        fy = sh_y + math.sin(front_ang) * arm_len
        d.line([x, sh_y, fx, fy], fill=col, width=int(h * 0.055))
        d.ellipse([fx - h * 0.04, fy - h * 0.04, fx + h * 0.04, fy + h * 0.04], fill=col)
        # Kopf (leichtes Nicken beim Sprechen)
        hy = top + head_r + bob + math.sin(t * 5) * gesture * 1.5 * s
        d.ellipse([x - head_r, hy - head_r, x + head_r, hy + head_r], fill=col)

    def draw_speech(self, d, x0: float, y0: float, head_x: float, head_y: float,
                    text: str, a: float, max_w: float) -> None:
        if a <= 0.01:
            return
        s = self.ss
        a = clamp01(a)
        f = font(int(19 * s))
        lines = wrap_text(d, text, f, max_w - 32 * s)[:3]
        w = max(text_width(d, line, f) for line in lines) + 32 * s
        h = len(lines) * 26 * s + 22 * s
        d.rounded_rectangle([x0, y0, x0 + w * a, y0 + h], radius=12 * s, fill=(245, 246, 250, int(240 * a)))
        # Schwanz zeigt zum Kopf der Figur
        tx = max(x0 + 18 * s, min(head_x, x0 + w * a - 30 * s))
        d.polygon([(tx, y0 + h - 2), (tx + 18 * s, y0 + h - 2), (tx + 4 * s, min(y0 + h + 22 * s, head_y))],
                  fill=(245, 246, 250, int(240 * a)))
        if a > 0.6:
            for i, line in enumerate(lines):
                d.text((x0 + 16 * s, y0 + 11 * s + i * 26 * s), line, font=f, fill=(20, 25, 38))

    # ── Encode ──
    def render(self, total_s: float, audio: Path | None) -> Path:
        import imageio_ffmpeg

        self.plan_timing(total_s)
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        silent = self.out_dir / ("infographic_silent_preview.mp4" if self.preview else "infographic_silent.mp4")
        frames = int(total_s * self.fps)
        writer = imageio_ffmpeg.write_frames(
            str(silent),
            (self.width, self.height),
            fps=self.fps,
            codec="libx264",
            quality=None,
            output_params=["-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
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
        if audio and audio.exists():
            subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(silent), "-i", str(audio),
                 "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", str(final)],
                check=True,
            )
        else:
            silent.replace(final)
        self.write_sidecars(final, total_s)
        return final

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
        # storyboard_mapled.json zusaetzlich schreiben: der semantische
        # Shorts-Planer im video_pipeline-Modul sucht genau diesen Dateinamen
        # als Fallback neben dem Quellvideo.
        for name in ("storyboard_infographic.json", "storyboard_mapled.json"):
            (self.out_dir / name).write_text(json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8")
        chapters = "\n".join(
            f"{int(s.start_s // 60):02d}:{int(s.start_s % 60):02d} {s.title}" for s in self.scenes
        )
        (self.out_dir / "youtube_package_infographic.md").write_text(
            f"# {self.title}\n\n## Kapitel\n{chapters}\n\n## Video\n{final.name}\n", encoding="utf-8"
        )


# ─── Audio-Dauer ──────────────────────────────────────────────────────────
def probe_duration(path: Path) -> float:
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-f", "null", "-"],
                          capture_output=True, text=True, timeout=60, check=False)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", (proc.stderr or "") + (proc.stdout or ""))
    if not m:
        return 0.0
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


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
