import json
import tempfile
from pathlib import Path

import module


def test_parse_json3_segments():
    raw = json.dumps(
        {
            "events": [
                {"tStartMs": 1000, "dDurationMs": 1200, "segs": [{"utf8": "Hallo "}, {"utf8": "Welt"}]},
                {"tStartMs": 2400, "dDurationMs": 900, "segs": [{"utf8": "Hallo Welt"}]},
                {"tStartMs": 3500, "dDurationMs": 1000, "segs": [{"utf8": "zweite Zeile"}]},
            ]
        }
    )
    segments = module.parse_caption(raw, "json3")
    assert segments[0]["start"] == 1.0
    assert segments[0]["text"] == "Hallo Welt"
    assert segments[1]["text"] == "zweite Zeile"


def test_parse_vtt_segments():
    raw = """WEBVTT

00:00:01.000 --> 00:00:02.500
<c>First line</c>

00:00:03.000 --> 00:00:04.000
Second&nbsp;line
"""
    segments = module.parse_caption(raw, "vtt")
    assert segments == [
        {"start": 1.0, "end": 2.5, "text": "First line"},
        {"start": 3.0, "end": 4.0, "text": "Second line"},
    ]


def test_normalize_youtube_url():
    assert module.normalize_youtube_url("UF8uR6Z6KLc") == "https://www.youtube.com/watch?v=UF8uR6Z6KLc"
    assert module.normalize_youtube_url("https://youtu.be/UF8uR6Z6KLc?t=3") == "https://www.youtube.com/watch?v=UF8uR6Z6KLc"
    assert module.normalize_youtube_url("https://example.com/watch?v=UF8uR6Z6KLc") == ""


def test_load_cached_transcript():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "youtube_transcripts"
        base.mkdir()
        payload = {
            "video_id": "UF8uR6Z6KLc",
            "url": "https://www.youtube.com/watch?v=UF8uR6Z6KLc",
            "title": "Cached Video",
            "transcript": "cached transcript",
            "segments": [{"start": 0.0, "text": "cached transcript"}],
        }
        (base / "UF8uR6Z6KLc-20260518T000000Z.json").write_text(json.dumps(payload), encoding="utf-8")
        cached = module.load_cached_transcript("https://youtu.be/UF8uR6Z6KLc", {"home_dir": tmp})
        assert cached is not None
        result, transcript, segments = cached
        assert result["cache_hit"] is True
        assert transcript == "cached transcript"
        assert segments[0]["text"] == "cached transcript"


if __name__ == "__main__":
    test_parse_json3_segments()
    test_parse_vtt_segments()
    test_normalize_youtube_url()
    test_load_cached_transcript()
    print("youtube_transcript parser tests ok")
