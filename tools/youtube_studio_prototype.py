#!/usr/bin/env python3
"""Render a low-cost briefing-style YouTube prototype from an audio narration.

This is intentionally asset-light: it creates editorial motion-graphic scenes
from structured scene metadata, then combines them with an existing narration.
The goal is a repeatable baseline that avoids generic AI-video slop.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass, asdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIO = ROOT / "agent-data/telegram_bot_media/last_deepdive_normalized_audio_de_clara_v2.mp3"
DEFAULT_SCRIPT = ROOT / "agent-data/telegram_bot_media/last_deepdive_audio_script_de_tts_v2.txt"
DEFAULT_OUT = ROOT / "agent-data/youtube_studio/prototypes/xi-trump-taiwan"

W, H = 1920, 1080
FPS = 30

PALETTE = {
    "ink": (20, 24, 30),
    "panel": (33, 39, 49),
    "panel2": (42, 50, 62),
    "text": (236, 239, 242),
    "muted": (161, 171, 184),
    "line": (92, 106, 124),
    "red": (222, 78, 77),
    "blue": (78, 148, 217),
    "teal": (82, 188, 162),
    "gold": (230, 179, 88),
    "green": (115, 196, 123),
    "purple": (150, 125, 218),
    "white": (255, 255, 255),
}


@dataclass
class Scene:
    key: str
    title: str
    subtitle: str
    bullets: list[str]
    visual: str
    weight: float


SCENES = [
    Scene(
        "hook",
        "Xi, Trump und Taiwan",
        "Warum der Mai-Gipfel mehr war als Diplomatie",
        ["Peking, Mai 2026", "Taiwan als neuralgischer Punkt", "Chips, Iran und Machtpolitik im selben System"],
        "map",
        0.9,
    ),
    Scene(
        "summit",
        "Der Gipfel",
        "Volles Protokoll, harte Warnung",
        ["Xi empfängt Trump in Peking", "kein sichtbarer Durchbruch", "Taiwan bleibt der Kernkonflikt"],
        "dossier",
        0.85,
    ),
    Scene(
        "timeline",
        "Die Chronologie",
        "13., 14. und 15. Mai als Sequenz",
        ["Peace Diplomacy Panel", "Gipfel in Peking", "Sanktionen gegen US-Rüstungsfirmen"],
        "timeline",
        1.0,
    ),
    Scene(
        "pressure",
        "Die Druckkette",
        "Warum Iran, Umfragen und Taiwan zusammenlaufen",
        ["Trump sucht außenpolitischen Spielraum", "China kann als Vermittler auftreten", "Taiwan wird strategisch verhandelbar"],
        "chain",
        1.1,
    ),
    Scene(
        "taiwan",
        "Taiwan",
        "Ambivalenz als Strategie",
        ["Washington liefert Waffen", "gleichzeitig keine Unterstützung für Unabhängigkeit", "Peking testet die rote Linie"],
        "map_focus",
        1.0,
    ),
    Scene(
        "chips",
        "Der Chip-Layer",
        "Nvidia, TSMC, ASML und Exportkontrollen",
        ["Exportkontrollen bleiben der harte Hebel", "Nvidia als Markt- und Militärfrage", "TSMC sichert Produktion international ab"],
        "chips",
        1.08,
    ),
    Scene(
        "perspectives",
        "Drei Blickwinkel",
        "Westliche Analyse, chinesische Staatsposition, Märkte",
        ["Think Tanks: minimaler Frieden", "Peking: Stärke und Agenda-Kontrolle", "Märkte: Blick auf Chips und Taiwan-Risiko"],
        "triad",
        0.95,
    ),
    Scene(
        "prediction",
        "Prognosen",
        "Die Märkte bleiben erstaunlich stabil",
        ["Taiwan-Invasionsrisiko: 7 bis 10 Prozent", "Jiang Xueqin als interessanter, aber dünner Track Record", "Signal ja, Beweis nein"],
        "meter",
        0.9,
    ),
    Scene(
        "uncertainty",
        "Was offen bleibt",
        "Der Deal ist taktisch, solange Chips und Taiwan ungelöst bleiben",
        ["keine klare Entspannung beim Chip-Embargo", "Taiwan bleibt doppeldeutig", "Grand Bargain bleibt unvollständig"],
        "matrix",
        1.05,
    ),
]


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


F_TITLE = font(76, True)
F_SUB = font(34)
F_BODY = font(34)
F_SMALL = font(24)
F_MONO = font(26)
F_LABEL = font(28, True)


def ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


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
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def draw_wrapped(draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], width: int, fnt, fill, spacing=8) -> int:
    x, y = xy
    avg = max(8, int(fnt.size * 0.52))
    chars = max(18, width // avg)
    for para in text.split("\n"):
        for line in textwrap.wrap(para, chars):
            draw.text((x, y), line, font=fnt, fill=fill)
            y += fnt.size + spacing
        y += spacing
    return y


def rounded(draw: ImageDraw.ImageDraw, box, fill, outline=None, width=2, radius=18):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def arrow(draw: ImageDraw.ImageDraw, a, b, fill, width=5):
    draw.line([a, b], fill=fill, width=width)
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    size = 18
    p1 = (b[0] - size * math.cos(ang - 0.45), b[1] - size * math.sin(ang - 0.45))
    p2 = (b[0] - size * math.cos(ang + 0.45), b[1] - size * math.sin(ang + 0.45))
    draw.polygon([b, p1, p2], fill=fill)


def base_canvas(scene: Scene, idx: int, total: int) -> Image.Image:
    img = Image.new("RGB", (W, H), PALETTE["ink"])
    draw = ImageDraw.Draw(img)
    for y in range(H):
        r = int(PALETTE["ink"][0] + y / H * 12)
        g = int(PALETTE["ink"][1] + y / H * 11)
        b = int(PALETTE["ink"][2] + y / H * 9)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    for x in range(0, W, 80):
        draw.line([(x, 0), (x, H)], fill=(36, 43, 52), width=1)
    for y in range(0, H, 80):
        draw.line([(0, y), (W, y)], fill=(36, 43, 52), width=1)
    draw.rectangle((0, 0, W, 7), fill=PALETTE["blue"])
    draw.rectangle((0, H - 9, int(W * idx / total), H), fill=PALETTE["gold"])
    draw.text((70, 48), f"{idx:02d}", font=F_MONO, fill=PALETTE["gold"])
    draw.text((140, 44), "DEEPDIVE BRIEFING", font=F_SMALL, fill=PALETTE["muted"])
    draw.text((70, 115), scene.title, font=F_TITLE, fill=PALETTE["text"])
    draw_wrapped(draw, scene.subtitle, (72, 210), 760, F_SUB, PALETTE["muted"])
    return img


def draw_bullets(draw: ImageDraw.ImageDraw, bullets: list[str], x=78, y=740, width=760):
    for bullet in bullets:
        draw.ellipse((x, y + 10, x + 14, y + 24), fill=PALETTE["teal"])
        y = draw_wrapped(draw, bullet, (x + 34, y), width - 34, F_BODY, PALETTE["text"], spacing=5)
        y += 16


def draw_footer(draw: ImageDraw.ImageDraw):
    draw.line((70, 1006, W - 70, 1006), fill=PALETTE["line"], width=2)
    draw.text((70, 1022), "Quelle: DeepDive/RAG-Auswertung | Visualisierung: schematisch, nicht maßstabsgetreu", font=F_SMALL, fill=PALETTE["muted"])


def node(draw, center, label, color, r=58):
    x, y = center
    draw.ellipse((x - r, y - r, x + r, y + r), fill=color, outline=(255, 255, 255), width=3)
    bbox = draw.textbbox((0, 0), label, font=F_LABEL)
    draw.text((x - (bbox[2] - bbox[0]) / 2, y - 16), label, font=F_LABEL, fill=PALETTE["ink"])


def draw_world_panel(draw):
    rounded(draw, (930, 132, 1810, 900), (29, 36, 48), PALETTE["line"], 2, 24)
    draw.text((970, 164), "Schematische Lagekarte", font=F_LABEL, fill=PALETTE["muted"])
    # Abstract land masses.
    draw.rounded_rectangle((1010, 350, 1230, 470), radius=55, fill=(62, 89, 100))
    draw.rounded_rectangle((1260, 300, 1575, 475), radius=70, fill=(68, 93, 96))
    draw.rounded_rectangle((1450, 510, 1650, 650), radius=58, fill=(66, 85, 96))
    points = {
        "USA": (1085, 405),
        "CHINA": (1510, 420),
        "TAIWAN": (1580, 515),
        "IRAN": (1375, 505),
    }
    for a, b, col in [
        ("USA", "CHINA", PALETTE["red"]),
        ("CHINA", "TAIWAN", PALETTE["gold"]),
        ("USA", "IRAN", PALETTE["blue"]),
        ("IRAN", "CHINA", PALETTE["teal"]),
    ]:
        draw.line([points[a], points[b]], fill=col, width=5)
    for label, pt in points.items():
        node(draw, pt, label, PALETTE["gold"] if label == "TAIWAN" else PALETTE["text"], r=42)


def visual_map(draw, scene):
    draw_world_panel(draw)
    draw_bullets(draw, scene.bullets)


def visual_dossier(draw, scene):
    rounded(draw, (940, 140, 1780, 880), PALETTE["panel"], PALETTE["line"], 2, 20)
    draw.text((990, 182), "PEKING / MAI 2026", font=F_LABEL, fill=PALETTE["gold"])
    for i, (label, val, col) in enumerate([
        ("Signal", "militärische Ehren", PALETTE["blue"]),
        ("Konflikt", "Taiwan-Warnung", PALETTE["red"]),
        ("Resultat", "kein Durchbruch", PALETTE["gold"]),
        ("Unterbau", "Chips + Iran + Handel", PALETTE["teal"]),
    ]):
        y = 260 + i * 128
        rounded(draw, (995, y, 1715, y + 88), (43, 51, 63), col, 3, 14)
        draw.text((1025, y + 20), label.upper(), font=F_SMALL, fill=PALETTE["muted"])
        draw.text((1225, y + 16), val, font=F_BODY, fill=PALETTE["text"])
    draw_bullets(draw, scene.bullets)


def visual_timeline(draw, scene):
    x0, y0, x1 = 970, 360, 1760
    draw.line((x0, y0, x1, y0), fill=PALETTE["line"], width=7)
    events = [
        ("13. Mai", "Peace Diplomacy Panel", PALETTE["blue"]),
        ("14. Mai", "Xi warnt Trump zu Taiwan", PALETTE["red"]),
        ("15. Mai", "Sanktionen gegen US-Rüstung", PALETTE["gold"]),
    ]
    for i, (date, label, col) in enumerate(events):
        x = x0 + i * ((x1 - x0) // 2)
        draw.ellipse((x - 24, y0 - 24, x + 24, y0 + 24), fill=col)
        rounded(draw, (x - 145, y0 + 70, x + 145, y0 + 230), PALETTE["panel"], col, 3, 16)
        draw.text((x - 78, y0 + 94), date, font=F_LABEL, fill=col)
        draw_wrapped(draw, label, (x - 108, y0 + 140), 220, F_SMALL, PALETTE["text"], spacing=4)
    draw_bullets(draw, scene.bullets)


def visual_chain(draw, scene):
    nodes = [
        ((1040, 285), "Iran", PALETTE["blue"]),
        ((1300, 285), "Trump", PALETTE["gold"]),
        ((1560, 285), "Xi", PALETTE["red"]),
        ((1300, 560), "Taiwan", PALETTE["teal"]),
    ]
    arrow(draw, (1100, 285), (1230, 285), PALETTE["line"])
    arrow(draw, (1370, 285), (1490, 285), PALETTE["line"])
    arrow(draw, (1560, 345), (1340, 510), PALETTE["line"])
    arrow(draw, (1300, 350), (1300, 500), PALETTE["line"])
    for center, label, col in nodes:
        node(draw, center, label, col, r=66)
    rounded(draw, (980, 710, 1680, 850), PALETTE["panel"], PALETTE["line"], 2, 18)
    draw_wrapped(draw, "Die Kausalkette läuft nicht linear. Sie verbindet innenpolitischen Druck, Kriegskosten, chinesische Verhandlungsmasse und strategische Ambivalenz.", (1015, 742), 640, F_SMALL, PALETTE["text"])
    draw_bullets(draw, scene.bullets)


def visual_map_focus(draw, scene):
    draw_world_panel(draw)
    draw.ellipse((1540, 475, 1620, 555), outline=PALETTE["red"], width=8)
    draw.ellipse((1522, 457, 1638, 573), outline=(222, 78, 77, ), width=3)
    rounded(draw, (1010, 705, 1735, 835), PALETTE["panel"], PALETTE["red"], 3, 16)
    draw_wrapped(draw, "Taiwan ist nicht nur Ort, sondern Hebel: Militär, Halbleiter, Bündnisse und Glaubwürdigkeit laufen hier zusammen.", (1040, 735), 650, F_SMALL, PALETTE["text"])
    draw_bullets(draw, scene.bullets)


def visual_chips(draw, scene):
    x, y = 960, 245
    companies = [("NVIDIA", PALETTE["green"]), ("TSMC", PALETTE["teal"]), ("ASML", PALETTE["blue"]), ("EXPORTKONTROLLEN", PALETTE["red"])]
    for i, (name, col) in enumerate(companies):
        yy = y + i * 130
        rounded(draw, (x, yy, x + 720, yy + 86), PALETTE["panel"], col, 3, 14)
        draw.text((x + 35, yy + 24), name, font=F_LABEL, fill=PALETTE["text"])
        if i < len(companies) - 1:
            arrow(draw, (x + 360, yy + 90), (x + 360, yy + 126), PALETTE["line"], 4)
    draw_bullets(draw, scene.bullets)


def visual_triad(draw, scene):
    centers = [(1110, 350), (1450, 350), (1280, 650)]
    labels = ["westliche\nAnalyse", "chinesische\nStaatsposition", "Markt &\nTechnologie"]
    cols = [PALETTE["blue"], PALETTE["red"], PALETTE["gold"]]
    for c in centers:
        for d in centers:
            if c != d:
                draw.line([c, d], fill=PALETTE["line"], width=3)
    for c, label, col in zip(centers, labels, cols):
        node(draw, c, label, col, r=92)
    draw_bullets(draw, scene.bullets)


def visual_meter(draw, scene):
    rounded(draw, (980, 250, 1740, 720), PALETTE["panel"], PALETTE["line"], 2, 24)
    draw.text((1030, 290), "Prediction-Märkte", font=F_LABEL, fill=PALETTE["muted"])
    draw.arc((1100, 360, 1620, 880), 180, 360, fill=PALETTE["line"], width=30)
    draw.arc((1100, 360, 1620, 880), 180, 207, fill=PALETTE["gold"], width=30)
    draw.text((1260, 548), "7-10%", font=font(86, True), fill=PALETTE["gold"])
    draw.text((1160, 650), "Taiwan-Invasionsrisiko", font=F_BODY, fill=PALETTE["text"])
    draw.text((1090, 710), "Signal, kein Beweis", font=F_SMALL, fill=PALETTE["muted"])
    draw_bullets(draw, scene.bullets)


def visual_matrix(draw, scene):
    items = [
        ("Chip-Embargo", "offen", PALETTE["red"]),
        ("Taiwan-Linie", "ambivalent", PALETTE["gold"]),
        ("Grand Bargain", "unvollständig", PALETTE["blue"]),
        ("Taktische Pause", "möglich", PALETTE["teal"]),
    ]
    for i, (left, right, col) in enumerate(items):
        y = 250 + i * 130
        rounded(draw, (960, y, 1760, y + 92), PALETTE["panel"], col, 3, 14)
        draw.text((1000, y + 25), left, font=F_LABEL, fill=PALETTE["text"])
        draw.text((1480, y + 25), right, font=F_LABEL, fill=col)
    draw_bullets(draw, scene.bullets)


VISUALS = {
    "map": visual_map,
    "dossier": visual_dossier,
    "timeline": visual_timeline,
    "chain": visual_chain,
    "map_focus": visual_map_focus,
    "chips": visual_chips,
    "triad": visual_triad,
    "meter": visual_meter,
    "matrix": visual_matrix,
}


def render_scene(scene: Scene, idx: int, total: int, path: Path) -> None:
    img = base_canvas(scene, idx, total)
    draw = ImageDraw.Draw(img)
    VISUALS[scene.visual](draw, scene)
    draw_footer(draw)
    # Subtle vignette.
    vignette = Image.new("L", (W, H), 0)
    vd = ImageDraw.Draw(vignette)
    vd.ellipse((-350, -220, W + 350, H + 260), fill=220)
    vignette = vignette.filter(ImageFilter.GaussianBlur(95))
    dark = Image.new("RGB", (W, H), (0, 0, 0))
    img = Image.composite(img, dark, vignette.point(lambda p: 255 - int(p * 0.18)))
    img.save(path)


def scene_durations(total_duration: float) -> list[float]:
    weights = [s.weight for s in SCENES]
    unit = total_duration / sum(weights)
    durations = [round(w * unit, 2) for w in weights]
    durations[-1] += round(total_duration - sum(durations), 2)
    return durations


def write_storyboard(out_dir: Path, durations: list[float], audio: Path, script: Path) -> None:
    storyboard = {
        "title": "Xi, Trump und Taiwan: Die Kausalkette hinter dem Mai-Gipfel",
        "format": "16:9 YouTube briefing prototype",
        "audio": str(audio),
        "script": str(script),
        "scenes": [
            {**asdict(scene), "duration_s": durations[idx], "start_s": round(sum(durations[:idx]), 2)}
            for idx, scene in enumerate(SCENES)
        ],
    }
    (out_dir / "storyboard.json").write_text(json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8")

    chapters = []
    elapsed = 0
    for scene, duration in zip(SCENES, durations):
        mm = int(elapsed // 60)
        ss = int(elapsed % 60)
        chapters.append(f"{mm:02d}:{ss:02d} {scene.title}")
        elapsed += duration
    package = "\n".join(
        [
            "# YouTube Upload-Paket",
            "",
            "## Titelideen",
            "- Xi, Trump und Taiwan: Was hinter dem Mai-Gipfel wirklich verhandelt wurde",
            "- Taiwan, Chips und Iran: Die Kausalkette hinter Trumps China-Gipfel",
            "- Der Grand Bargain? Xi, Trump und das Risiko um Taiwan",
            "",
            "## Beschreibung",
            "Ein ruhiges geopolitisches Lagebriefing zu Xi Jinping, Donald Trump, Taiwan, Iran, Nvidia, TSMC, ASML und den Exportkontrollen. Die Visualisierung ist schematisch; Details und Quellen stammen aus der DeepDive/RAG-Auswertung.",
            "",
            "## Kapitel",
            *chapters,
            "",
            "## Thumbnail-Brief",
            "Weltkarte mit USA-China-Taiwan-Dreieck, roter Taiwan-Markierung, Titel: 'TAIWAN ALS DEAL?'",
            "",
        ]
    )
    (out_dir / "youtube_package.md").write_text(package, encoding="utf-8")


def render_video(audio: Path, script: Path, out_dir: Path, preview: bool = False) -> Path:
    ensure_dir(out_dir)
    scenes_dir = out_dir / "scenes"
    clips_dir = out_dir / "clips"
    ensure_dir(scenes_dir)
    ensure_dir(clips_dir)
    ffmpeg = ffmpeg_bin()
    duration = audio_duration(ffmpeg, audio)
    durations = scene_durations(duration)
    write_storyboard(out_dir, durations, audio, script)

    for idx, scene in enumerate(SCENES, start=1):
        render_scene(scene, idx, len(SCENES), scenes_dir / f"{idx:02d}_{scene.key}.png")

    clip_paths = []
    for idx, (scene, dur) in enumerate(zip(SCENES, durations), start=1):
        img = scenes_dir / f"{idx:02d}_{scene.key}.png"
        clip = clips_dir / f"{idx:02d}_{scene.key}.mp4"
        frames = max(1, int(dur * FPS))
        zoom_expr = "min(zoom+0.00010,1.035)"
        vf = (
            f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames}:s={W}x{H}:fps={FPS},"
            f"trim=duration={dur:.2f},setpts=PTS-STARTPTS,"
            f"fade=t=in:st=0:d=0.25,fade=t=out:st={max(0, dur - 0.25):.2f}:d=0.25,"
            "format=yuv420p"
        )
        run(
            [
                ffmpeg,
                "-y",
                "-loop",
                "1",
                "-i",
                str(img),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20" if preview else "18",
                "-an",
                str(clip),
            ]
        )
        clip_paths.append(clip)

    concat_file = out_dir / "clips.txt"
    concat_file.write_text("".join(f"file '{p.resolve()}'\n" for p in clip_paths), encoding="utf-8")
    video_silent = out_dir / "video_silent.mp4"
    run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(video_silent)])

    final = out_dir / ("xi_trump_taiwan_briefing_preview.mp4" if preview else "xi_trump_taiwan_briefing_1080p.mp4")
    run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_silent),
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
    return final


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args(argv)
    final = render_video(args.audio.resolve(), args.script.resolve(), args.out.resolve(), preview=args.preview)
    print(final)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
