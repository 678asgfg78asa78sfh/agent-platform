#!/usr/bin/env python3
"""Render stable world-map shots and camera moves for briefing videos.

The renderer is intentionally simple and dependency-light: it uses Natural
Earth GeoJSON, PIL and ffmpeg. It generates deterministic map frames instead
of relying on ffmpeg zoom filters, which avoids the flicker/jitter seen in the
first video prototype.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "agent-data/youtube_studio/assets/worldmap"
DEFAULT_OUT = ROOT / "agent-data/youtube_studio/worldmap_demo"
GEOJSON_PATH = ASSET_DIR / "ne_110m_admin_0_countries.geojson"
BASEMAP_PATH = ASSET_DIR / "world_base_4320x2160.png"
GEOJSON_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson"

MAP_W = 4320
MAP_H = 2160
OUT_W = 1920
OUT_H = 1080
FPS = 25

OCEAN = (14, 22, 32)
OCEAN_GRID = (31, 43, 55)
LAND = (48, 65, 68)
LAND_ALT = (55, 74, 76)
BORDER = (88, 107, 111)
TEXT = (236, 239, 242)
MUTED = (170, 181, 190)
PANEL = (18, 26, 36, 210)
GOLD = (232, 183, 86)
RED = (224, 84, 82)
BLUE = (84, 156, 224)
TEAL = (87, 191, 169)
PURPLE = (159, 128, 222)
GREEN = (116, 198, 125)

HIGHLIGHT_COLORS = [BLUE, RED, GOLD, TEAL, PURPLE, GREEN]

COUNTRY_ALIASES = {
    "usa": "United States of America",
    "us": "United States of America",
    "u.s.": "United States of America",
    "u.s.a.": "United States of America",
    "united states": "United States of America",
    "united states congress": "United States of America",
    "us congress": "United States of America",
    "washington": "United States of America",
    "washington dc": "United States of America",
    "pentagon": "United States of America",
    "department of war": "United States of America",
    "department of defense": "United States of America",
    "vereinigte staaten": "United States of America",
    "vereinigte staaten von amerika": "United States of America",
    "america": "United States of America",
    "iran": "Iran",
    "china": "China",
    "prc": "China",
    "taiwan": "Taiwan",
    "roc": "Taiwan",
    "japan": "Japan",
    "germany": "Germany",
    "deutschland": "Germany",
    "russia": "Russia",
    "russland": "Russia",
    "russian federation": "Russia",
    "russische foederation": "Russia",
    "russische föderation": "Russia",
    "ukraine": "Ukraine",
    "israel": "Israel",
    "india": "India",
    "indien": "India",
    "south korea": "South Korea",
    "suedkorea": "South Korea",
    "südkorea": "South Korea",
    "korea": "South Korea",
    "north korea": "North Korea",
    "nordkorea": "North Korea",
    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "grossbritannien": "United Kingdom",
    "großbritannien": "United Kingdom",
    "vereinigtes koenigreich": "United Kingdom",
    "vereinigtes königreich": "United Kingdom",
    "netherlands": "Netherlands",
    "niederlande": "Netherlands",
    "holland": "Netherlands",
    "france": "France",
    "frankreich": "France",
    "french republic": "France",
    "franzoesische republik": "France",
    "französische republik": "France",
    "italy": "Italy",
    "italien": "Italy",
    "italian republic": "Italy",
    "italienische republik": "Italy",
    "brazil": "Brazil",
    "brasilien": "Brazil",
    "colombia": "Colombia",
    "kolumbien": "Colombia",
    "mexico": "Mexico",
    "mexiko": "Mexico",
    "canada": "Canada",
    "kanada": "Canada",
    "australia": "Australia",
    "australien": "Australia",
    "new zealand": "New Zealand",
    "neuseeland": "New Zealand",
    "argentina": "Argentina",
    "argentinien": "Argentina",
    "chile": "Chile",
    "peru": "Peru",
    "spain": "Spain",
    "spanien": "Spain",
    "poland": "Poland",
    "polen": "Poland",
    "turkey": "Turkey",
    "tuerkei": "Turkey",
    "türkei": "Turkey",
    "saudi arabia": "Saudi Arabia",
    "saudi-arabien": "Saudi Arabia",
    "south africa": "South Africa",
    "suedafrika": "South Africa",
    "südafrika": "South Africa",
}

REGION_STOPS = {
    "global": ("Global", 0.0, 18.0, 1.0),
    "world": ("Global", 0.0, 18.0, 1.0),
    "welt": ("Global", 0.0, 18.0, 1.0),
    "international": ("Global", 0.0, 18.0, 1.0),
    "worldwide": ("Global", 0.0, 18.0, 1.0),
    "globally": ("Global", 0.0, 18.0, 1.0),
    "globaler kontext": ("Global", 0.0, 18.0, 1.0),
    "internet": ("Global", 0.0, 18.0, 1.0),
    "web": ("Global", 0.0, 18.0, 1.0),
    "online": ("Global", 0.0, 18.0, 1.0),
    "europe": ("Europe", 10.0, 51.0, 2.2),
    "europa": ("Europe", 10.0, 51.0, 2.2),
    "eu": ("Europe", 10.0, 51.0, 2.2),
    "european union": ("Europe", 10.0, 51.0, 2.2),
    "europaeische union": ("Europe", 10.0, 51.0, 2.2),
    "europäische union": ("Europe", 10.0, 51.0, 2.2),
    "asia": ("Asia", 95.0, 35.0, 1.7),
    "asien": ("Asia", 95.0, 35.0, 1.7),
    "pacific": ("Pacific", -160.0, 8.0, 1.7),
    "pazifik": ("Pacific", -160.0, 8.0, 1.7),
    "middle east": ("Middle East", 45.0, 30.0, 2.6),
    "naher osten": ("Middle East", 45.0, 30.0, 2.6),
    "near east": ("Middle East", 45.0, 30.0, 2.6),
    "latin america": ("Latin America", -63.0, -15.0, 1.9),
    "lateinamerika": ("Latin America", -63.0, -15.0, 1.9),
    "south america": ("South America", -60.0, -17.0, 2.0),
    "suedamerika": ("South America", -60.0, -17.0, 2.0),
    "südamerika": ("South America", -60.0, -17.0, 2.0),
    "north america": ("North America", -98.0, 45.0, 1.9),
    "nordamerika": ("North America", -98.0, 45.0, 1.9),
    "africa": ("Africa", 20.0, 3.0, 1.8),
    "afrika": ("Africa", 20.0, 3.0, 1.8),
    "nato": ("NATO", -20.0, 48.0, 1.65),
    "vatican": ("Vatican", 12.453, 41.902, 5.1),
    "vatican city": ("Vatican", 12.453, 41.902, 5.1),
    "holy see": ("Vatican", 12.453, 41.902, 5.1),
    "vatikan": ("Vatican", 12.453, 41.902, 5.1),
    "vatikanstadt": ("Vatican", 12.453, 41.902, 5.1),
}

REGION_STOP_INDEX = {}

MANUAL_CENTERS = {
    "United States of America": (-98.5, 39.5),
    "Iran": (53.0, 32.0),
    "China": (104.0, 35.0),
    "Taiwan": (121.0, 23.7),
    "Japan": (138.0, 36.0),
    "Germany": (10.4, 51.0),
    "Russia": (96.0, 60.0),
    "Ukraine": (31.0, 49.0),
    "Israel": (35.1, 31.4),
    "India": (78.0, 22.0),
    "South Korea": (127.8, 36.3),
    "North Korea": (127.1, 40.0),
    "United Kingdom": (-2.5, 54.0),
    "Netherlands": (5.3, 52.1),
    "France": (2.2, 46.2),
    "Italy": (12.6, 42.8),
    "Brazil": (-53.2, -10.8),
    "Colombia": (-73.1, 4.6),
}


@dataclass(frozen=True)
class Stop:
    name: str
    lon: float
    lat: float
    zoom: float


_COUNTRY_INDEX_CACHE: dict[int, dict[str, dict[str, Any]]] = {}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


F_TITLE = font(42, True)
F_LABEL = font(28, True)
F_BODY = font(24)
F_SMALL = font(20)


def ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def ensure_assets() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    if GEOJSON_PATH.exists():
        return
    response = requests.get(GEOJSON_URL, timeout=30)
    response.raise_for_status()
    GEOJSON_PATH.write_bytes(response.content)


def load_geojson() -> dict[str, Any]:
    ensure_assets()
    return json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))


def norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def region_stop(value: str) -> tuple[str, float, float, float] | None:
    global REGION_STOP_INDEX
    if not REGION_STOP_INDEX:
        REGION_STOP_INDEX = {norm_name(alias): stop for alias, stop in REGION_STOPS.items()}
    return REGION_STOP_INDEX.get(norm_name(value))


def route_item_candidates(value: str) -> list[str]:
    raw = re.sub(r"\s+", " ", str(value or "").strip())
    if not raw:
        return []
    candidates = [raw]
    candidates.extend(part.strip() for part in re.split(r"\s*[/|]\s*", raw) if part.strip())
    candidates.append(re.sub(r"\([^)]*\)", "", raw).strip())
    low = raw.casefold()
    if any(word in low for word in ("internet", "web", "online")):
        candidates.append("Global")
    if any(word in low for word in ("congress", "washington", "pentagon", "department of war", "department of defense")):
        candidates.append("USA")
    if any(word in low for word in ("pacific", "pazifik")):
        candidates.append("Pacific")
    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = norm_name(candidate)
        if candidate and key and key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def feature_display_name(feature: dict[str, Any]) -> str:
    props = feature.get("properties") or {}
    for key in ("ADMIN", "NAME", "SOVEREIGNT", "NAME_LONG"):
        value = props.get(key)
        if value:
            return str(value)
    return "Unknown"


def primary_feature_names(feature: dict[str, Any]) -> list[str]:
    props = feature.get("properties") or {}
    names = []
    for key in ("ADMIN", "NAME", "NAME_LONG", "BRK_NAME"):
        value = props.get(key)
        if value and value != "-99":
            names.append(str(value))
    return names


def secondary_feature_names(feature: dict[str, Any]) -> list[str]:
    props = feature.get("properties") or {}
    names = []
    for key in ("SOVEREIGNT", "ISO_A3", "ISO_A2"):
        value = props.get(key)
        if value and value != "-99":
            names.append(str(value))
    return names


def country_index(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cache_key = id(data)
    cached = _COUNTRY_INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    index: dict[str, dict[str, Any]] = {}
    for feature in data.get("features", []):
        for name in primary_feature_names(feature):
            index[norm_name(name)] = feature
    for feature in data.get("features", []):
        for name in secondary_feature_names(feature):
            index.setdefault(norm_name(name), feature)
    for alias, target in COUNTRY_ALIASES.items():
        target_feature = index.get(norm_name(target))
        if target_feature:
            index[norm_name(alias)] = target_feature
    _COUNTRY_INDEX_CACHE[cache_key] = index
    return index


def iter_polygons(geometry: dict[str, Any]) -> list[list[list[float]]]:
    if not geometry:
        return []
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return coords
    if kind == "MultiPolygon":
        rings: list[list[list[float]]] = []
        for polygon in coords:
            rings.extend(polygon)
        return rings
    return []


def lonlat_to_map(lon: float, lat: float) -> tuple[float, float]:
    x = (lon + 180.0) / 360.0 * MAP_W
    y = (90.0 - lat) / 180.0 * MAP_H
    return x, y


def map_to_screen(x: float, y: float, left: float, top: float, crop_w: float, crop_h: float) -> tuple[float, float]:
    return (x - left) * OUT_W / crop_w, (y - top) * OUT_H / crop_h


def country_center(feature: dict[str, Any]) -> tuple[float, float]:
    name = feature_display_name(feature)
    if name in MANUAL_CENTERS:
        return MANUAL_CENTERS[name]
    xs: list[float] = []
    ys: list[float] = []
    for ring in iter_polygons(feature.get("geometry") or {}):
        for lon, lat, *_ in ring:
            xs.append(float(lon))
            ys.append(float(lat))
    if not xs or not ys:
        return 0.0, 20.0
    return sum(xs) / len(xs), sum(ys) / len(ys)


def resolve_route(data: dict[str, Any], route: list[str]) -> list[Stop]:
    index = country_index(data)
    stops: list[Stop] = []
    missing: list[str] = []
    for item in route:
        resolved_stop: Stop | None = None
        feature = None
        for candidate in route_item_candidates(item):
            key = norm_name(candidate)
            region = region_stop(candidate)
            if region:
                name, lon, lat, zoom = region
                resolved_stop = Stop(name, lon, lat, zoom)
                break
            feature = index.get(key)
            if feature:
                break
        if not feature:
            if resolved_stop:
                stops.append(resolved_stop)
                continue
            missing.append(item)
            continue
        if resolved_stop:
            stops.append(resolved_stop)
            continue
        name = feature_display_name(feature)
        lon, lat = country_center(feature)
        zoom = 2.0
        if name == "Taiwan":
            zoom = 4.6
        elif name in {"China", "Russia", "United States of America"}:
            zoom = 2.05
        elif name in {"Iran", "Germany", "Japan", "Israel", "United Kingdom"}:
            zoom = 3.1
        stops.append(Stop(short_name(name), lon, lat, zoom))
    if missing:
        raise SystemExit(f"Unknown countries/regions: {', '.join(missing)}")
    if len(stops) < 2:
        raise SystemExit("Route needs at least two countries/regions.")
    return stops


def short_name(name: str) -> str:
    if name == "United States of America":
        return "USA"
    return name


def build_basemap(data: dict[str, Any], force: bool = False) -> Image.Image:
    if BASEMAP_PATH.exists() and not force:
        return Image.open(BASEMAP_PATH).convert("RGB")

    img = Image.new("RGB", (MAP_W, MAP_H), OCEAN)
    draw = ImageDraw.Draw(img)

    for lat in range(-60, 90, 30):
        _, y = lonlat_to_map(0, lat)
        draw.line((0, y, MAP_W, y), fill=OCEAN_GRID, width=2)
    for lon in range(-180, 181, 30):
        x, _ = lonlat_to_map(lon, 0)
        draw.line((x, 0, x, MAP_H), fill=OCEAN_GRID, width=2)

    for idx, feature in enumerate(data.get("features", [])):
        fill = LAND if idx % 2 else LAND_ALT
        for ring in iter_polygons(feature.get("geometry") or {}):
            pts = [lonlat_to_map(float(lon), float(lat)) for lon, lat, *_ in ring]
            if len(pts) >= 3:
                draw.polygon(pts, fill=fill, outline=BORDER)

    # Subtle vignette to keep labels readable without making the map muddy.
    mask = Image.new("L", (MAP_W, MAP_H), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse((-MAP_W * 0.15, -MAP_H * 0.35, MAP_W * 1.15, MAP_H * 1.35), fill=180)
    mask = mask.filter(ImageFilter.GaussianBlur(170))
    dark = Image.new("RGB", (MAP_W, MAP_H), (6, 10, 16))
    img = Image.composite(img, dark, mask.point(lambda p: 255 - int(p * 0.75)))

    BASEMAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    img.save(BASEMAP_PATH, quality=95)
    return img


def camera_box(lon: float, lat: float, zoom: float) -> tuple[float, float, float, float]:
    cx, cy = lonlat_to_map(lon, lat)
    crop_h = MAP_H / zoom
    crop_w = crop_h * OUT_W / OUT_H
    if crop_w > MAP_W:
        crop_w = MAP_W
        crop_h = crop_w * OUT_H / OUT_W
    left = max(0.0, min(MAP_W - crop_w, cx - crop_w / 2))
    top = max(0.0, min(MAP_H - crop_h, cy - crop_h / 2))
    return left, top, crop_w, crop_h


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def interpolate(a: Stop, b: Stop, t: float) -> tuple[float, float, float]:
    s = smoothstep(t)
    lon = a.lon + (b.lon - a.lon) * s
    lat = a.lat + (b.lat - a.lat) * s
    zoom = a.zoom + (b.zoom - a.zoom) * s
    return lon, lat, zoom


def draw_country_overlay(
    frame: Image.Image,
    data: dict[str, Any],
    route_names: list[str],
    active_name: str,
    camera: tuple[float, float, float, float],
) -> None:
    index = country_index(data)
    left, top, crop_w, crop_h = camera
    overlay = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for idx, name in enumerate(route_names):
        feature = index.get(norm_name(name)) or index.get(norm_name(COUNTRY_ALIASES.get(name.casefold(), name)))
        if not feature:
            continue
        color = HIGHLIGHT_COLORS[idx % len(HIGHLIGHT_COLORS)]
        is_active = name == active_name
        fill_alpha = 120 if is_active else 64
        line_alpha = 235 if is_active else 155
        width = 5 if is_active else 3
        for ring in iter_polygons(feature.get("geometry") or {}):
            pts = []
            for lon, lat, *_ in ring:
                mx, my = lonlat_to_map(float(lon), float(lat))
                sx, sy = map_to_screen(mx, my, left, top, crop_w, crop_h)
                pts.append((sx, sy))
            if len(pts) >= 3:
                draw.polygon(pts, fill=(*color, fill_alpha))
                draw.line(pts + [pts[0]], fill=(*color, line_alpha), width=width)

    frame.alpha_composite(overlay)


def draw_route_lines(draw: ImageDraw.ImageDraw, stops: list[Stop], camera: tuple[float, float, float, float]) -> None:
    left, top, crop_w, crop_h = camera
    points = []
    for stop in stops:
        mx, my = lonlat_to_map(stop.lon, stop.lat)
        points.append(map_to_screen(mx, my, left, top, crop_w, crop_h))

    for a, b in zip(points, points[1:]):
        ax, ay = a
        bx, by = b
        if max(ax, bx) < -80 or min(ax, bx) > OUT_W + 80 or max(ay, by) < -80 or min(ay, by) > OUT_H + 80:
            continue
        draw.line((ax, ay, bx, by), fill=(238, 210, 132, 150), width=4)
        angle = math.atan2(by - ay, bx - ax)
        tip = (bx, by)
        size = 17
        p1 = (bx - size * math.cos(angle - 0.45), by - size * math.sin(angle - 0.45))
        p2 = (bx - size * math.cos(angle + 0.45), by - size * math.sin(angle + 0.45))
        draw.polygon([tip, p1, p2], fill=(238, 210, 132, 180))


def draw_markers(draw: ImageDraw.ImageDraw, stops: list[Stop], active: Stop, camera: tuple[float, float, float, float]) -> None:
    left, top, crop_w, crop_h = camera
    for stop in stops:
        mx, my = lonlat_to_map(stop.lon, stop.lat)
        x, y = map_to_screen(mx, my, left, top, crop_w, crop_h)
        if x < -120 or x > OUT_W + 120 or y < -120 or y > OUT_H + 120:
            continue
        current = stop.name == active.name
        color = GOLD if current else TEXT
        radius = 16 if current else 10
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color, outline=(8, 13, 20), width=4)
        label = stop.name
        bbox = draw.textbbox((0, 0), label, font=F_LABEL)
        lw = bbox[2] - bbox[0]
        lh = bbox[3] - bbox[1]
        box = (x + 18, y - 18, x + 34 + lw, y + 18 + lh)
        draw.rounded_rectangle(box, radius=8, fill=(10, 16, 24, 210), outline=(210, 220, 230, 80), width=1)
        draw.text((x + 26, y - 11), label, font=F_LABEL, fill=color)


def draw_hud(frame: Image.Image, title: str, stops: list[Stop], active: Stop, progress: float) -> None:
    layer = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((54, 46, 1010, 166), radius=18, fill=PANEL, outline=(255, 255, 255, 36), width=1)
    draw.text((84, 70), title, font=F_TITLE, fill=TEXT)
    route = "  ->  ".join(stop.name for stop in stops)
    draw.text((86, 124), route, font=F_BODY, fill=MUTED)
    draw.rounded_rectangle((54, OUT_H - 98, 720, OUT_H - 44), radius=14, fill=PANEL, outline=(255, 255, 255, 30), width=1)
    draw.text((84, OUT_H - 82), f"Aktiver Fokus: {active.name}", font=F_BODY, fill=GOLD)
    draw.rounded_rectangle((54, OUT_H - 20, OUT_W - 54, OUT_H - 12), radius=4, fill=(255, 255, 255, 36))
    draw.rounded_rectangle((54, OUT_H - 20, 54 + int((OUT_W - 108) * progress), OUT_H - 12), radius=4, fill=GOLD)
    frame.alpha_composite(layer)


def render_frame(
    base: Image.Image,
    data: dict[str, Any],
    stops: list[Stop],
    active: Stop,
    lon: float,
    lat: float,
    zoom: float,
    title: str,
    progress: float,
    show_hud: bool = True,
) -> Image.Image:
    camera = camera_box(lon, lat, zoom)
    left, top, crop_w, crop_h = camera
    frame = base.transform(
        (OUT_W, OUT_H),
        Image.Transform.EXTENT,
        (left, top, left + crop_w, top + crop_h),
        Image.Resampling.BICUBIC,
    ).convert("RGBA")
    route_names = [stop.name for stop in stops]
    draw_country_overlay(frame, data, route_names, active.name, camera)
    draw = ImageDraw.Draw(frame, "RGBA")
    draw_route_lines(draw, stops, camera)
    draw_markers(draw, stops, active, camera)
    if show_hud:
        draw_hud(frame, title, stops, active, progress)
    return frame.convert("RGB")


def render_snapshots(base: Image.Image, data: dict[str, Any], stops: list[Stop], out_dir: Path, title: str) -> None:
    snapshots = out_dir / "snapshots"
    if snapshots.exists():
        shutil.rmtree(snapshots)
    snapshots.mkdir(parents=True, exist_ok=True)
    for idx, stop in enumerate(stops, 1):
        frame = render_frame(base, data, stops, stop, stop.lon, stop.lat, stop.zoom, title, idx / len(stops))
        safe = re.sub(r"[^a-z0-9]+", "_", stop.name.casefold()).strip("_")
        frame.save(snapshots / f"{idx:02d}_{safe}.png", quality=95)


def render_video(
    base: Image.Image,
    data: dict[str, Any],
    stops: list[Stop],
    out_dir: Path,
    title: str,
    duration: float,
    fps: int,
    keep_frames: bool,
) -> Path:
    frames_dir = out_dir / "frames"
    clips_dir = out_dir / "clips"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)

    total_frames = max(1, int(duration * fps))
    segments = len(stops) - 1
    for frame_idx in range(total_frames):
        global_t = frame_idx / max(1, total_frames - 1)
        scaled = global_t * segments
        seg_idx = min(segments - 1, int(scaled))
        local_t = scaled - seg_idx
        a = stops[seg_idx]
        b = stops[seg_idx + 1]
        lon, lat, zoom = interpolate(a, b, local_t)
        active = a if local_t < 0.45 else b
        frame = render_frame(base, data, stops, active, lon, lat, zoom, title, global_t)
        frame.save(frames_dir / f"frame_{frame_idx:05d}.png", optimize=False)

    output = out_dir / "worldmap_route_1080p.mp4"
    ffmpeg = ffmpeg_bin()
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-framerate",
            str(fps),
            "-i",
            str(frames_dir / "frame_%05d.png"),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )
    if not keep_frames:
        shutil.rmtree(frames_dir)
    return output


def write_manifest(out_dir: Path, title: str, stops: list[Stop], duration: float, fps: int, video: Path | None) -> None:
    manifest = {
        "title": title,
        "duration_seconds": duration,
        "fps": fps,
        "route": [stop.__dict__ for stop in stops],
        "video": str(video) if video else None,
        "snapshots": str(out_dir / "snapshots"),
        "source_geojson": str(GEOJSON_PATH),
        "basemap": str(BASEMAP_PATH),
    }
    (out_dir / "worldmap_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a stable world-map route video and snapshots.")
    parser.add_argument("--route", default="USA,Iran,China,Taiwan", help="Comma-separated countries/regions, e.g. USA,Iran,China,Taiwan")
    parser.add_argument("--title", default="Geopolitische Kausalkette", help="HUD title")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--duration", type=float, default=28.0)
    parser.add_argument("--fps", type=int, default=FPS)
    parser.add_argument("--snapshots-only", action="store_true")
    parser.add_argument("--keep-frames", action="store_true")
    parser.add_argument("--force-basemap", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    data = load_geojson()
    stops = resolve_route(data, [part.strip() for part in args.route.split(",") if part.strip()])
    base = build_basemap(data, force=args.force_basemap)
    render_snapshots(base, data, stops, args.out, args.title)
    video = None
    if not args.snapshots_only:
        video = render_video(base, data, stops, args.out, args.title, args.duration, args.fps, args.keep_frames)
    write_manifest(args.out, args.title, stops, args.duration, args.fps, video)
    if video:
        print(video)
    else:
        print(args.out / "snapshots")


if __name__ == "__main__":
    main()
