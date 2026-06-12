# GPU-Box 192.168.2.102 — Setup (Stand 2026-06-12)

CachyOS (Arch), 2× GTX 1080 (8 GB, Pascal/GP104), 2 Cores, 6.7 GB RAM (knapp!),
235 GB Disk (~34 GB frei). **Statische IP gesetzt** (NetworkManager manual,
192.168.2.102/24, GW .1, DNS .1+1.1.1.1).

## Laufende Dienste (alle systemd, survive reboot)
- **llama-server** :8080 — bestehend, models-preset (GPU0)
- **Qwen3-TTS** :8002 — `/opt/qwen3-tts`, OpenAI-kompatibel (`/v1/audio/speech`),
  GPU0. 15 Stimmen (ryan/eric deutsch-tauglich getestet), **Voice-Cloning**
  unter `/v1/audio/voice-clone`. = "Alibaba TTS", war schon installiert.
- **whisper-sync** :8003 — NEU, `/opt/whisper-sync`, faster-whisper medium
  **int8** (Pascal kann kein float16!), GPU1, cuBLAS/cuDNN-12 via pip-wheels +
  LD_LIBRARY_PATH im Unit. POST /transcribe (audio body) → Wort-Timestamps JSON.
  Lazy-Load, MemoryMax=3G. systemd: `whisper-sync.service`.

## Agent-Anbindung
- tts.default + workflow_trigger.default: provider=qwen,
  qwen_tts_url=http://192.168.2.102:8002/v1/audio/speech, voice=ryan, de.
  → Video-TTS ist jetzt lokal + kostenlos (war xAI).
- Whisper-Untertitel-Sync: Renderer-Anbindung TODO (Service steht).

## Pascal-Lektion
GTX 1080 (compute 6.1): float16/int8_float16 NICHT unterstuetzt für CT2.
Nur `int8` (DP4A) oder `float32`. Gilt für alle ML auf diesen Karten.
