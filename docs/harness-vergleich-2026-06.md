# Harness-Vergleich: agent vs. Hermes / OpenClaw / Letta / smolagents (Stand 2026-06-12)

Verglichen wurden frische Clones (`~/aistuff/compare/`):
- **Hermes Agent** (NousResearch, Python, MIT, ~140k Stars) — Always-on-Personal-Agent, TUI + Messenger-Gateway
- **OpenClaw** (TypeScript/Node, MIT) — Personal Assistant mit Gateway-Architektur, 23 Messaging-Kanäle, Skill-Ökosystem
- **Letta** (ehem. MemGPT, Python/TS) — Stateful-Agent-Server, Memory-first
- **smolagents** (HuggingFace, Python) — Minimal-Harness, Code-as-Action
- **unser `agent/`** (Rust, ~25k LoC) — Multi-Modul-Plattform, Scheduler, Telegram, Web-UI

## Wo wir vorn sind

| Bereich | Wir | Die anderen |
|---|---|---|
| **Betriebssemantik** | SQLite-WAL-Pipeline, atomare Task-Claims, Idempotency (exactly-once Tools), Watchdog + Heartbeats, Crash-Recovery | Hermes: Checkpoints, aber keine exactly-once-Semantik; Letta: Server-State, aber kein Task-Board |
| **Kostenkontrolle** | Harte per-Backend-Caps (USD/Calls/Zyklus) mit Task-Reschedule statt Fail, Call-Rate-Limits, Token-Budgets pro Modul | Hermes: Usage-*Tracking*, aber kein Enforcement mit Reschedule |
| **Guardrail** | Deterministische Pre-Execution-Validierung jedes Tool-Calls (Schema, Whitelists, Levenshtein), Retry-Leiter, Backend-Fallback | Hermes: Approval nur für gefährliche Shell-Kommandos; keine Schema-Validierung mit Fallback |
| **Security-Tiefe** | SSRF-Schutz pro Redirect-Hop, API-/Credential-Vault mit Audit, Permission-Modell mit Temp-Agent-Nullvergabe | Alle drei deutlich dünner |
| **Footprint** | Ein statisches Rust-Binary (~16 MB) + Python-Module on demand | Hermes-Install zieht uv, Python, Node, ripgrep, ffmpeg |
| **Multi-Agent-Flotte** | Viele Modul-Instanzen mit eigenem LLM-Binding, eigenem Scheduler, eigenen Budgets | Hermes ist EIN Agent (+Subagents); Flottensteuerung gibt es dort nicht |

Das Konzept „Plattform mit vielen unabhängig ge-scheduleten, budgetierten Agenten-Instanzen" hat keiner der drei. Die Einschätzung „konzeptionell überlegen" stimmt für den **Betriebs-/Ops-Kern** — nicht für die Model-Interaktion (s.u.).

## Wo die anderen vorn sind — Adoptionskandidaten

### 1. Programmatic Tool Calling („Code Mode") — Hermes `tools/code_execution_tool.py`, smolagents-Kernthese
Das LLM schreibt ein Python-Skript, das Tools per RPC ruft (UDS lokal, File-RPC remote).
Mehrstufige Pipelines (suchen → 5 URLs fetchen → filtern → aggregieren) kollabieren in
**eine** Inference-Runde; Zwischenergebnisse betreten den Kontext nie, nur stdout kommt zurück.
→ Für uns der größte Hebel: DeepSeek-V4-DeepDive-Flows kosten heute 5–15 LLM-Runden à 30–120 s.
Wir haben PyProcessPool + Modul-Protokoll schon — fehlt: ein `script.exec`-Tool, das whitelisted
Tools über einen RPC-Stub exponiert. **Aufwand: mittel. Nutzen: sehr hoch.**

### 2. Tool-Result-Persistenz mit Preview — Hermes `tools/budget_config.py` (3-Layer-Budget)
Große Tool-Ergebnisse (>1.5k Preview / 100k Limit / 200k Turn-Budget) landen vollständig auf
Disk; das LLM bekommt Preview + Handle und kann gezielt Bereiche nachlesen (`read_file` Loop-
geschützt via pinned threshold). Wir truncaten hart auf 4000 Zeichen und der Hinweis „voll-
ständiges Ergebnis im Aufgaben-Board" ist für das LLM eine Sackgasse (kein Read-Back-Tool).
→ `toolresult.lesen(handle, von, bis)` + Persistenz im Task-Store. **Aufwand: klein-mittel. Nutzen: hoch.**

### 3. Skills + Curator (Lernschleife) — Hermes `skills/`, `agent/curator.py`; Letta „sleep-time agents"
Agent erstellt nach komplexen Tasks Skill-Dokumente (Markdown, agentskills.io-Standard),
nutzt sie künftig, ein Hintergrund-Curator konsolidiert/archiviert (nie löschen, nur archivieren;
Idle-getriggert statt Cron). Letta macht dasselbe für Memory (Konsolidierung im Leerlauf).
→ Bei uns fast geschenkt: RAG-Pools existieren. Ein Cron-Modul „memory_curator", das täglich
Task-/Chat-Historie destilliert und in einen RAG-Pool schreibt + Skill-Markdown in den System-
Prompt-Kontext injiziert. **Aufwand: klein (nutzt vorhandene Bausteine). Nutzen: mittel-hoch.**

### 4. MCP — Hermes konsumiert MCP-Server UND exponiert sich selbst als MCP-Server
`mcp_serve.py`: Konversationen/Messaging als MCP-Tools für Claude Code/Cursor/Codex.
Unser Modul-System ist proprietär; ein MCP-Client-Modul würde tausende fertige Tool-Server
erschließen, ein MCP-Server-Endpoint macht unsere Plattform aus jedem MCP-Client steuerbar.
**Aufwand: mittel (stdio-JSON-RPC, passt zu unserem Loader-Muster). Nutzen: hoch (Ökosystem).**

### 5. LLM-Summarization-Compaction — Hermes `agent/context_engine.py` (pluggable)
Unsere neue Kompaktierung (context_window) ist deterministisch (Gruppen droppen). Hermes
fasst die Mitte per Hilfs-LLM zusammen (protect first/last N, Summary ersetzt Mitte) und hat
das als austauschbare Engine abstrahiert. → Als Stufe 2 unserer Kompaktierung: vor dem
Droppen die Gruppe per Billig-LLM (lokales Modell!) zu 2 Sätzen destillieren. **Aufwand: klein
(Turn-Engine hat den Hook schon). Nutzen: mittel.**

### 6. Smart Approval — Hermes `tools/approval.py`
Gefährliche Shell-Kommandos: Pattern-Detection + Risiko-Einschätzung durch Hilfs-LLM +
persistente Allowlist + asynchrone Approval über den Messenger. Wir haben eine statische
Whitelist. → Telegram-Approval-Flow („Agent will X ausführen — erlauben?") wäre für
unbeaufsichtigte Cron-Agents ein echter Sicherheitsgewinn. **Aufwand: mittel. Nutzen: mittel.**

### 7. Kleinigkeiten
- **Streaming von Tool-Output** in laufenden Runden (Hermes TUI) — unsere Chat-UI zeigt nur Status-Zeilen.
- **FTS5-Volltextsuche** über Sessions (wir: chat_history-Modul existiert, aber ohne Summarization-Schicht).
- **Trajectory-Datagen** (Hermes batch_runner/trajectory_compressor) — nur relevant, falls ihr je eigene Modelle tunen wollt.
- **Terminal-Backends** (Docker/SSH/Modal als austauschbare Ausführungsumgebung) — unser shell.exec läuft immer lokal.

## OpenClaw im Detail (Nachtrag)

OpenClaw ist konzeptionell der nächste Verwandte von Hermes (dort gibt es sogar `hermes claw migrate`),
aber mit anderem Schwerpunkt: Kanal-Breite und Ökosystem statt Lernschleife.

**Was OpenClaw besonders macht:**
1. **Commitments-Engine** (`src/commitments/`): extrahiert automatisch Zusagen/Versprechen aus
   Konversationen ("ich schicke dir morgen X") und ein Heartbeat-Runner fasst selbststaendig nach,
   wenn sie faellig sind. Das ist echtes proaktives Verhalten statt nur Cron — fuer einen
   Telegram-Assistenten wie unseren ein sehr passendes Konzept: Task-Board + Heartbeat haben wir,
   die Extraktions-Schicht fehlt. **Adoptionskandidat, Aufwand mittel.**
2. **Gateway als Control-Plane**: 23 Kanaele (WhatsApp, Signal, iMessage, Teams, Matrix, …) ueber
   eine Steuer-Ebene, Geraete als "Nodes" (macOS/iOS/Android mit Voice), dazu ein live
   renderbares **Canvas**. Fuer uns relevant, falls je mehr als Telegram gewuenscht ist —
   Architektur-Muster, nicht 1:1-Code.
3. **Skill-Oekosystem mit Registry**: 58 Bundled-Skills + ClawHub (Community-Registry) +
   SOUL.md-Persona-Templates (162 Community-Vorlagen). Bestaetigt die Skills-Empfehlung aus dem
   Hermes-Teil — das Format setzt sich quer durch die Szene durch (agentskills.io-kompatibel).
4. **clawbench**: Benchmark, der den GESAMTEN Stack bewertet (Harness + Config + Modell, trace-
   basiert, Reliability-Metriken) statt nur das LLM. Genau die Luecke unseres `benchmark.rs`
   (misst nur Backend-Antworten). Trace-basierte Harness-Scores waeren der richtige Ausbau.
5. **ACP-Spawning**: externe Coding-Agents (Claude Code, Codex) werden als Subagents mit
   gebuendelter MCP/LSP-Runtime gestartet — der Personal-Agent delegiert Coding an Spezialisten.
6. **Test-Disziplin**: auffaellig viele colocated e2e-/integration-/guardrail-Tests fuer den
   Harness selbst — dieselbe Richtung, die wir mit den Mock-Server-E2E-Tests eingeschlagen haben.

**Wo wir gegen OpenClaw vorn bleiben:** gleiche Punkte wie gegen Hermes — exactly-once-Tools,
Kosten-Enforcement, deterministischer Guardrail, Security-Tiefe, Flotten-Konzept, Single-Binary
(OpenClaw ist ein grosser Node-Monolith mit pnpm-Workspace).

## Nicht übernehmen
- Hermes' Installations-/Dependency-Modell (uv+Node+ffmpeg-Bootstrap) — unser Single-Binary ist der Vorteil.
- Letta's Memory-Block-Schema 1:1 — unsere RAG-Pools + chat_history decken das pragmatischer ab.
- smolagents als Library einbetten — falsche Sprache, falsches Abstraktionsniveau; nur das CodeAgent-*Konzept* zählt (= Punkt 1).

## Empfohlene Reihenfolge
1. Tool-Result-Persistenz mit Read-Back (klein, sofortiger Qualitätsgewinn)
2. PTC/Code-Mode (größter Latenz-/Kostenhebel für DeepDive & DeepSeek V4)
3. Memory-Curator-Cron auf RAG-Pools (Lernschleife mit Bordmitteln)
4. MCP-Client-Modul (Ökosystem), danach MCP-Server-Endpoint
5. Commitments-Extraktion + Heartbeat-Nachfassen (OpenClaw-Konzept, passt perfekt zu Telegram)
6. Summarization-Stufe in der Kompaktierung (lokales LLM als Summarizer)
7. Telegram-Approval-Flow
8. clawbench-artiger Trace-Benchmark als Ausbau von benchmark.rs
