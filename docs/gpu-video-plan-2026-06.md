# GPU-Plan: 2× GTX 1080 (8 GB) für die Video-Pipeline (2026-06-12)

Die Karten stecken nicht in diesem Host (hier nur AMD iGPU) — vermutlich in den
llama.cpp-Boxen (192.168.2.108 / 10.0.0.53). Empfehlung nach Impact:

## 1. CosyVoice 3 als lokale TTS (= "Alibaba TTS") — JA, machen
- Alibabas offenes TTS, 0.5B Parameter, **Deutsch nativ unterstuetzt** (9 Sprachen),
  Qualitaet schlaegt 3x groessere Modelle (0.81% CER), vLLM/TensorRT-Runtimes.
- Passt locker auf eine GTX 1080 (Pascal: fp32, ~2-3 GB); 3-Min-Skript in <1 Min.
- Gewinn: TTS-Kosten = 0, KEINE Cloud-Abhaengigkeit, und Voice-Cloning =
  eigene konsistente Markenstimme fuer den Kanal.
- Anbindung: OpenAI-speech-kompatibler Server auf der GPU-Box (Port z.B. 8060),
  tts-Modul bekommt provider "openai_compat" + base_url. Client-Seite ist trivial.

## 2. faster-whisper fuer Untertitel-Wort-Timing — JA, machen
- medium/int8 laeuft problemlos auf der 1080 (~2 GB), transkribiert die fertige
  narration.mp3 mit Wort-Timestamps.
- Gewinn: Untertitel exakt synchron zur Stimme (heute: Schaetzung), spaeter
  Karaoke-Highlighting. Groesster verbleibender Untertitel-Hebel.
- Anbindung: kleiner HTTP-Service auf der Box; Renderer bekommt --word-timings.

## 3. Lokale Bildgeneration (SDXL) — NEIN (vorerst)
- Geht auf 8 GB (20-40 s/Bild), aber xAI kostet $0.02/Bild — Strom+Wartung der
  1080er sind teurer als die API, solange das Volumen klein ist.

## 4. Upscaling/Frame-Interpolation — NEIN
- Wir rendern nativ 1080p25; nichts zu interpolieren.

## Naechster Schritt
SSH-Zugang/Hostname der GPU-Box(en) bereitstellen, dann: Setup-Skripte fuer
CosyVoice-Server + Whisper-Service (Docker oder venv), tts-Modul-Provider und
Renderer-Wort-Timing in einem Rutsch.
