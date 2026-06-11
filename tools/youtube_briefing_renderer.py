#!/usr/bin/env python3
"""Render a cleaner map-led briefing video from an existing narration.

This is the second production prototype after youtube_studio_prototype.py.
It uses the deterministic worldmap renderer as the main visual layer and
keeps long sections stable instead of relying on synthetic zoom filters.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import worldmap_renderer as wm


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIO = ROOT / "agent-data/telegram_bot_media/last_deepdive_normalized_audio_de_clara_v2.mp3"
DEFAULT_SCRIPT = ROOT / "agent-data/telegram_bot_media/last_deepdive_audio_script_de_tts_v2.txt"
DEFAULT_OUT = ROOT / "agent-data/youtube_studio/prototypes/xi-trump-taiwan-mapled"
DEFAULT_TITLE = "Xi, Trump und Taiwan: Map-Led Briefing"

W = wm.OUT_W
H = wm.OUT_H
FPS = 25

TEXT = (238, 241, 244)
MUTED = (170, 181, 190)
INK = (10, 15, 22)
PANEL = (12, 18, 27, 214)
PANEL_SOFT = (12, 18, 27, 165)
LINE = (255, 255, 255, 42)
GOLD = wm.GOLD
RED = wm.RED
BLUE = wm.BLUE
TEAL = wm.TEAL


@dataclass(frozen=True)
class BriefingScene:
    key: str
    title: str
    subtitle: str
    route: list[str]
    bullets: list[str]
    weight: float
    color: tuple[int, int, int]


SCENES = [
    BriefingScene(
        "hook",
        "Xi, Trump und Taiwan",
        "Die Story ist nicht ein Gipfel. Es ist eine Kausalkette.",
        ["USA", "China", "Taiwan"],
        ["Washington sucht Hebel.", "Peking markiert rote Linien.", "Taiwan verbindet Militär, Chips und Glaubwürdigkeit."],
        0.90,
        GOLD,
    ),
    BriefingScene(
        "summit",
        "Peking-Gipfel",
        "Hoher Empfang, aber kein sichtbarer Durchbruch.",
        ["USA", "China", "Taiwan"],
        ["Das Protokoll signalisiert Gesprächsbereitschaft.", "Die Taiwan-Warnung bleibt der harte Kern.", "Der Gipfel wirkt taktisch, nicht gelöst."],
        0.86,
        BLUE,
    ),
    BriefingScene(
        "timeline",
        "Die Sequenz",
        "13., 14. und 15. Mai ergeben erst zusammen Sinn.",
        ["USA", "Iran", "China", "Taiwan"],
        ["Iran öffnet diplomatischen Spielraum.", "Xi nutzt den Moment für Taiwan-Signale.", "Sanktionen gegen US-Rüstungsfirmen schärfen die Linie."],
        1.00,
        TEAL,
    ),
    BriefingScene(
        "pressure",
        "Die Druckkette",
        "Innenpolitik, Kriegskosten und Verhandlungsmasse laufen zusammen.",
        ["USA", "Iran", "China", "Taiwan"],
        ["Trump braucht außenpolitische Resultate.", "China kann als Vermittler oder Blockierer auftreten.", "Taiwan wird dadurch strategisch aufgeladen."],
        1.10,
        RED,
    ),
    BriefingScene(
        "taiwan",
        "Taiwan",
        "Ambivalenz ist hier kein Fehler, sondern Teil der Strategie.",
        ["USA", "Japan", "Taiwan", "China"],
        ["Waffenlieferungen stützen Abschreckung.", "Gleichzeitig bleibt Unabhängigkeit offiziell tabu.", "Das Risiko sitzt in der Doppeldeutigkeit."],
        1.00,
        GOLD,
    ),
    BriefingScene(
        "chips",
        "Der Chip-Layer",
        "Nvidia, TSMC, ASML und Exportkontrollen liegen unter der Diplomatie.",
        ["USA", "Netherlands", "Japan", "Taiwan", "China"],
        ["Nvidia steht für Rechenleistung und Kontrolle.", "TSMC steht für strategische Verwundbarkeit.", "ASML und Japan machen Lieferketten politisch."],
        1.08,
        TEAL,
    ),
    BriefingScene(
        "perspectives",
        "Drei Blickwinkel",
        "Westliche Analyse, chinesische Staatslogik und Märkte lesen dieselbe Lage anders.",
        ["USA", "Germany", "China", "Taiwan"],
        ["Think-Tanks sehen minimale Entspannung.", "Peking zeigt Stärke und Agenda-Kontrolle.", "Märkte beobachten Chips und Taiwan-Risiko."],
        0.95,
        BLUE,
    ),
    BriefingScene(
        "prediction",
        "Prognosen",
        "Prediction-Märkte sind Signale, keine Beweise.",
        ["USA", "China", "Taiwan"],
        ["Die 7-bis-10-Prozent-Spanne ist kein Alarmismus.", "Sie zeigt eine eingepreiste Restgefahr.", "Jiang Xueqin bleibt interessant, aber dünn belegt."],
        0.90,
        GOLD,
    ),
    BriefingScene(
        "uncertainty",
        "Was offen bleibt",
        "Der Deal bleibt taktisch, solange Chips und Taiwan ungelöst sind.",
        ["USA", "Iran", "China", "Taiwan"],
        ["Kein klarer Ausstieg aus Exportkontrollen.", "Keine echte Taiwan-Entspannung.", "Ein Grand Bargain wäre sichtbar größer."],
        1.05,
        RED,
    ),
]

COLOR_LOOKUP = {
    "gold": GOLD,
    "red": RED,
    "blue": BLUE,
    "teal": TEAL,
    "green": wm.GREEN,
    "purple": wm.PURPLE,
}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


F_KICKER = font(20, True)
F_TITLE = font(48, True)
F_SUB = font(25)
F_BODY = font(24)
F_SMALL = font(18)
F_MONO = font(22)


def ffmpeg_bin() -> str:
    return wm.ffmpeg_bin()


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def audio_duration(ffmpeg: str, path: Path) -> float:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stdout)
    if not match:
        raise RuntimeError(f"Could not read duration from {path}")
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def scene_durations(total_duration: float, scenes: list[BriefingScene]) -> list[float]:
    unit = total_duration / sum(max(0.05, scene.weight) for scene in scenes)
    durations = [round(max(0.05, scene.weight) * unit, 2) for scene in scenes]
    durations[-1] += round(total_duration - sum(durations), 2)
    return durations


def sanitize_key(value: str, fallback: str) -> str:
    key = re.sub(r"[^a-z0-9_]+", "_", (value or "").casefold()).strip("_")
    return key or fallback


def parse_color(value: object, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in COLOR_LOOKUP:
            return COLOR_LOOKUP[lowered]
        if re.fullmatch(r"#[0-9a-fA-F]{6}", lowered):
            return tuple(int(lowered[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return tuple(max(0, min(255, int(v))) for v in value[:3])  # type: ignore[return-value]
        except Exception:
            return fallback
    return fallback


def normalize_route(value: object) -> list[str]:
    if isinstance(value, str):
        route = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        route = [str(part).strip() for part in value if str(part).strip()]
    else:
        route = []
    if len(route) == 1:
        route.append("Global")
    if len(route) < 2:
        raise ValueError("Jede Szene braucht mindestens zwei Route-Stationen.")
    return route


def normalize_bullets(value: object) -> list[str]:
    if isinstance(value, str):
        bullets = [part.strip() for part in re.split(r"\n+|;\s*", value) if part.strip()]
    elif isinstance(value, list):
        bullets = [str(part).strip() for part in value if str(part).strip()]
    else:
        bullets = []
    return bullets[:6] or ["Kausalkette einordnen.", "Akteure markieren.", "Unsicherheiten sichtbar halten."]


def load_scenes_json(path: Path) -> tuple[str | None, list[BriefingScene]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    title = None
    entries = raw
    if isinstance(raw, dict):
        title = str(raw.get("title") or raw.get("project_title") or "").strip() or None
        entries = raw.get("scenes") or []
    if not isinstance(entries, list) or not entries:
        raise ValueError("scenes_json muss eine Liste oder {title, scenes:[...]} enthalten.")
    scenes: list[BriefingScene] = []
    colors = [GOLD, BLUE, TEAL, RED, wm.PURPLE, wm.GREEN]
    for idx, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"Szene {idx} ist kein Objekt.")
        title_text = str(entry.get("title") or f"Kapitel {idx}").strip()
        subtitle = str(entry.get("subtitle") or entry.get("summary") or "").strip()
        route = normalize_route(entry.get("route") or entry.get("countries") or entry.get("actors"))
        bullets = normalize_bullets(entry.get("bullets") or entry.get("points") or entry.get("causal_points"))
        color = parse_color(entry.get("color"), colors[(idx - 1) % len(colors)])
        try:
            weight = float(entry.get("weight", 1.0))
        except Exception:
            weight = 1.0
        key = sanitize_key(str(entry.get("key") or title_text), f"scene_{idx:02d}")
        scenes.append(BriefingScene(key, title_text, subtitle, route, bullets, max(0.05, weight), color))
    return title, scenes


def wrap_text(draw: ImageDraw.ImageDraw | None, text: str, font_obj: ImageFont.FreeTypeFont, width: int) -> list[str]:
    avg = max(8, int(font_obj.size * 0.50))
    chars = max(16, width // avg)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines.extend(textwrap.wrap(paragraph, chars) or [""])
    return lines


def text_block_height(lines: list[str], font_obj: ImageFont.FreeTypeFont, spacing: int) -> int:
    if not lines:
        return 0
    return len(lines) * font_obj.size + max(0, len(lines) - 1) * spacing


def fit_text_block(text: str, width: int, max_height: int, sizes: list[int], bold: bool = False, spacing: int = 5) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    fallback_font = font(sizes[-1], bold)
    fallback_lines = wrap_text(None, text, fallback_font, width)
    for size in sizes:
        candidate = font(size, bold)
        lines = wrap_text(None, text, candidate, width)
        if text_block_height(lines, candidate, spacing) <= max_height:
            return candidate, lines
    return fallback_font, fallback_lines


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    width: int,
    font_obj: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int] | tuple[int, int, int, int],
    spacing: int = 7,
) -> int:
    x, y = xy
    for line in wrap_text(draw, text, font_obj, width):
        draw.text((x, y), line, font=font_obj, fill=fill)
        y += font_obj.size + spacing
    return y


def draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    xy: tuple[int, int],
    font_obj: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int] | tuple[int, int, int, int],
    spacing: int = 6,
) -> int:
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font_obj, fill=fill)
        y += font_obj.size + spacing
    return y


def draw_glass(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int = 18) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=PANEL, outline=LINE, width=1)


def title_layout(scene: BriefingScene) -> tuple[ImageFont.FreeTypeFont, list[str], ImageFont.FreeTypeFont, list[str], int]:
    panel_w = 910
    inner_w = panel_w - 56
    max_h = 246
    for title_size, sub_size in [(48, 25), (44, 24), (40, 23), (36, 22)]:
        title_font = font(title_size, True)
        sub_font = font(sub_size)
        title_lines = wrap_text(None, scene.title, title_font, inner_w)
        subtitle_lines = wrap_text(None, scene.subtitle, sub_font, inner_w)
        content_h = 32 + 18 + text_block_height(title_lines, title_font, 4) + 12 + text_block_height(subtitle_lines, sub_font, 5) + 24
        if content_h <= max_h:
            return title_font, title_lines, sub_font, subtitle_lines, max(174, content_h)
    title_font = font(36, True)
    sub_font = font(22)
    title_lines = wrap_text(None, scene.title, title_font, inner_w)
    subtitle_lines = wrap_text(None, scene.subtitle, sub_font, inner_w)
    content_h = 32 + 18 + text_block_height(title_lines, title_font, 4) + 12 + text_block_height(subtitle_lines, sub_font, 5) + 24
    return title_font, title_lines, sub_font, subtitle_lines, min(max_h, max(174, content_h))


def bullet_layout(bullets: list[str], width: int, max_height: int) -> tuple[ImageFont.FreeTypeFont, list[list[str]], int]:
    for size in [25, 24, 23, 22, 21, 20]:
        bullet_font = font(size)
        line_groups = [wrap_text(None, bullet, bullet_font, width) for bullet in bullets]
        content_h = 42
        for lines in line_groups:
            content_h += max(size + 4, text_block_height(lines, bullet_font, 5)) + 15
        if content_h <= max_height:
            return bullet_font, line_groups, content_h
    bullet_font = font(20)
    line_groups = [wrap_text(None, bullet, bullet_font, width) for bullet in bullets]
    content_h = 42 + sum(text_block_height(lines, bullet_font, 4) + 12 for lines in line_groups)
    return bullet_font, line_groups, min(max_height, content_h)


def draw_scene_overlay(frame: Image.Image, scene: BriefingScene, scene_idx: int, scene_total: int, global_progress: float) -> Image.Image:
    img = frame.convert("RGBA")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # Left title stack with measured height.
    title_font, title_lines, sub_font, subtitle_lines, title_h = title_layout(scene)
    title_box = (56, 46, 966, 46 + title_h)
    draw_glass(draw, title_box, 18)
    draw.rounded_rectangle((82, 72, 218, 102), radius=8, fill=(*scene.color, 235))
    draw.text((98, 77), f"KAPITEL {scene_idx:02d}", font=F_KICKER, fill=INK)
    title_y = 118
    title_y = draw_lines(draw, title_lines, (82, title_y), title_font, TEXT, spacing=4)
    draw_lines(draw, subtitle_lines, (84, title_y + 7), sub_font, MUTED, spacing=5)

    # Right notes panel with measured bullet layout. This prevents text from
    # escaping when a chapter has longer German phrasing.
    panel_x, panel_y, panel_w, max_panel_h = 1186, 606, 678, 356
    bullet_font, bullet_lines, content_h = bullet_layout(scene.bullets, panel_w - 92, max_panel_h - 44)
    panel_h = min(max_panel_h, max(230, content_h + 34))
    draw_glass(draw, (panel_x, panel_y, panel_x + panel_w, panel_y + panel_h), 18)
    draw.text((panel_x + 34, panel_y + 28), "Kausalpunkte", font=F_KICKER, fill=scene.color)
    y = panel_y + 74
    for lines in bullet_lines:
        if y + bullet_font.size > panel_y + panel_h - 24:
            break
        draw.ellipse((panel_x + 36, y + 8, panel_x + 50, y + 22), fill=(*scene.color, 235))
        y = draw_lines(draw, lines, (panel_x + 70, y), bullet_font, TEXT, spacing=5)
        y += 14

    # Small footer and progress.
    draw.rounded_rectangle((56, H - 82, 846, H - 44), radius=10, fill=PANEL_SOFT, outline=LINE, width=1)
    draw.text((82, H - 70), "DeepDive/RAG-Auswertung | Karten: Natural Earth | Visualisierung schematisch", font=F_SMALL, fill=MUTED)
    draw.rounded_rectangle((56, H - 18, W - 56, H - 10), radius=4, fill=(255, 255, 255, 40))
    draw.rounded_rectangle((56, H - 18, 56 + int((W - 112) * global_progress), H - 10), radius=4, fill=(*scene.color, 235))

    img.alpha_composite(layer)
    return img.convert("RGB")


def frame_for(
    base: Image.Image,
    data: dict,
    stops: list[wm.Stop],
    scene: BriefingScene,
    scene_idx: int,
    scene_total: int,
    lon: float,
    lat: float,
    zoom: float,
    active: wm.Stop,
    global_progress: float,
) -> Image.Image:
    frame = wm.render_frame(base, data, stops, active, lon, lat, zoom, scene.title, global_progress, show_hud=False)
    return draw_scene_overlay(frame, scene, scene_idx, scene_total, global_progress)


def encode_frames(frames_dir: Path, output: Path, fps: int, crf: str) -> None:
    run(
        [
            ffmpeg_bin(),
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
            crf,
            "-bf",
            "0",
            "-g",
            str(fps),
            "-x264-params",
            f"keyint={fps}:min-keyint={fps}:scenecut=0",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )


def encode_still(still: Path, output: Path, duration: float, fps: int, crf: str) -> None:
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loop",
            "1",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(still),
            "-vf",
            f"fps={fps},format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            crf,
            "-bf",
            "0",
            "-g",
            str(fps),
            "-x264-params",
            f"keyint={fps}:min-keyint={fps}:scenecut=0",
            "-movflags",
            "+faststart",
            "-an",
            str(output),
        ]
    )


def concat_videos(inputs: list[Path], output: Path) -> None:
    concat_file = output.with_suffix(".txt")
    concat_file.write_text("".join(f"file '{path.resolve()}'\n" for path in inputs), encoding="utf-8")
    run([ffmpeg_bin(), "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(output)])


def render_scene_clip(
    base: Image.Image,
    data: dict,
    scene: BriefingScene,
    scene_idx: int,
    scene_total: int,
    duration: float,
    out_dir: Path,
    preview: bool,
) -> Path:
    scene_dir = out_dir / f"{scene_idx:02d}_{scene.key}"
    frames_dir = scene_dir / "frames"
    stills_dir = scene_dir / "stills"
    clips_dir = scene_dir / "clips"
    if scene_dir.exists():
        shutil.rmtree(scene_dir)
    frames_dir.mkdir(parents=True)
    stills_dir.mkdir(parents=True)
    clips_dir.mkdir(parents=True)

    stops = wm.resolve_route(data, scene.route)
    motion_duration = min(18.0, max(10.0, duration * 0.24))
    if preview:
        motion_duration = min(8.0, motion_duration)
    motion_frames = max(2, int(motion_duration * FPS))
    segments = len(stops) - 1

    print(
        f"[scene {scene_idx:02d}/{scene_total}] {scene.title}: "
        f"{duration:.1f}s total, {motion_duration:.1f}s map motion, {motion_frames} frames",
        flush=True,
    )
    for frame_idx in range(motion_frames):
        if frame_idx and frame_idx % max(1, FPS * 5) == 0:
            print(f"[scene {scene_idx:02d}] frame {frame_idx}/{motion_frames}", flush=True)
        t = frame_idx / max(1, motion_frames - 1)
        scaled = t * segments
        seg_idx = min(segments - 1, int(scaled))
        local_t = scaled - seg_idx
        a = stops[seg_idx]
        b = stops[seg_idx + 1]
        lon, lat, zoom = wm.interpolate(a, b, local_t)
        active = a if local_t < 0.48 else b
        global_progress = ((scene_idx - 1) + t * min(0.7, motion_duration / max(duration, 1.0))) / scene_total
        frame = frame_for(base, data, stops, scene, scene_idx, scene_total, lon, lat, zoom, active, global_progress)
        frame.save(frames_dir / f"frame_{frame_idx:05d}.png", optimize=False)

    motion_clip = clips_dir / f"{scene_idx:02d}_{scene.key}_motion.mp4"
    print(f"[scene {scene_idx:02d}] encoding motion", flush=True)
    encode_frames(frames_dir, motion_clip, FPS, "21" if preview else "18")
    shutil.rmtree(frames_dir)

    hold_duration = max(0.05, duration - motion_duration)
    final_stop = stops[-1]
    hold_frame = frame_for(
        base,
        data,
        stops,
        scene,
        scene_idx,
        scene_total,
        final_stop.lon,
        final_stop.lat,
        final_stop.zoom,
        final_stop,
        scene_idx / scene_total,
    )
    hold_image = stills_dir / f"{scene_idx:02d}_{scene.key}_hold.png"
    hold_frame.save(hold_image, optimize=True)
    hold_clip = clips_dir / f"{scene_idx:02d}_{scene.key}_hold.mp4"
    print(f"[scene {scene_idx:02d}] encoding hold {hold_duration:.1f}s", flush=True)
    encode_still(hold_image, hold_clip, hold_duration, FPS, "21" if preview else "18")

    scene_clip = out_dir / f"{scene_idx:02d}_{scene.key}.mp4"
    concat_videos([motion_clip, hold_clip], scene_clip)
    print(f"[scene {scene_idx:02d}] done", flush=True)
    return scene_clip


def write_project_files(
    out_dir: Path,
    audio: Path,
    script: Path,
    durations: list[float],
    scenes: list[BriefingScene],
    project_title: str,
    final: Path | None = None,
) -> None:
    starts: list[float] = []
    elapsed = 0.0
    for duration in durations:
        starts.append(round(elapsed, 2))
        elapsed += duration

    storyboard = {
        "title": project_title,
        "format": "16:9 YouTube briefing, deterministic map-led visuals",
        "audio": str(audio),
        "script": str(script),
        "final_video": str(final) if final else None,
        "scenes": [
            {
                **asdict(scene),
                "color": list(scene.color),
                "duration_s": durations[idx],
                "start_s": starts[idx],
            }
            for idx, scene in enumerate(scenes)
        ],
    }
    (out_dir / "storyboard_mapled.json").write_text(json.dumps(storyboard, indent=2, ensure_ascii=False), encoding="utf-8")

    chapters = []
    for scene, start in zip(scenes, starts):
        chapters.append(f"{int(start // 60):02d}:{int(start % 60):02d} {scene.title}")

    package = "\n".join(
        [
            "# YouTube Upload-Paket",
            "",
            "## Titelideen",
            f"- {project_title}",
            f"- {project_title}: Die Kausalkette auf der Karte",
            f"- {project_title}: Was zusammenhaengt",
            "",
            "## Beschreibung",
            "Map-led DeepDive-Briefing. Die Visualisierung ist schematisch und dient der Einordnung der Kausalketten.",
            "",
            "## Kapitel",
            *chapters,
            "",
            "## Thumbnail-Brief",
            "Dunkle Weltkarte, betroffene Laender/Regionen markiert, kurzer kontrastreicher Titel.",
            "",
        ]
    )
    (out_dir / "youtube_package_mapled.md").write_text(package, encoding="utf-8")


def render_video(
    audio: Path,
    script: Path,
    out_dir: Path,
    preview: bool = False,
    scenes: list[BriefingScene] | None = None,
    project_title: str = DEFAULT_TITLE,
) -> Path:
    scenes = scenes or SCENES
    out_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = out_dir / "scene_clips"
    if clips_dir.exists():
        shutil.rmtree(clips_dir)
    clips_dir.mkdir(parents=True)

    ffmpeg = ffmpeg_bin()
    total_duration = audio_duration(ffmpeg, audio)
    if preview:
        total_duration = min(total_duration, 90.0)
    durations = scene_durations(total_duration, scenes)
    write_project_files(out_dir, audio, script, durations, scenes, project_title)

    data = wm.load_geojson()
    base = wm.build_basemap(data)

    scene_clips: list[Path] = []
    scene_total = len(scenes)
    for idx, (scene, duration) in enumerate(zip(scenes, durations), start=1):
        scene_clips.append(render_scene_clip(base, data, scene, idx, scene_total, duration, clips_dir, preview))

    silent = out_dir / ("mapled_video_silent_preview.mp4" if preview else "mapled_video_silent.mp4")
    print("[final] concatenating scenes", flush=True)
    concat_videos(scene_clips, silent)

    slug = sanitize_key(project_title, "mapled_briefing")
    final = out_dir / (f"{slug}_preview.mp4" if preview else f"{slug}_1080p.mp4")
    print("[final] muxing audio", flush=True)
    run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-i",
            str(silent),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(final),
        ]
    )
    write_project_files(out_dir, audio, script, durations, scenes, project_title, final)
    return final


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a map-led YouTube briefing video.")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--scenes-json", type=Path, default=None)
    parser.add_argument("--preview", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    title = args.title
    scenes = None
    if args.scenes_json:
        loaded_title, scenes = load_scenes_json(args.scenes_json.resolve())
        if loaded_title and title == DEFAULT_TITLE:
            title = loaded_title
    final = render_video(args.audio.resolve(), args.script.resolve(), args.out.resolve(), preview=args.preview, scenes=scenes, project_title=title)
    print(final)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
