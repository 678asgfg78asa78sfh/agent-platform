# Agent Platform - Architektur & Implementierung

## Überblick

Eine modulare AI-Agent-Platform in Rust. Ein Binary, kein Docker, kein Node.
Der Agent führt Aufgaben aus, nutzt LLMs für Intelligenz und Module für Aktionen.

## Kernprinzip

**Das LLM ist ein Werkzeug das der Cycle benutzt, nicht umgekehrt.**
Der Cycle ist der Boss, das LLM ist der Arbeiter.
Module sind die Hände - sie TUN Dinge (Mails lesen, Dateien schreiben, Web suchen).

## Architektur

```
┌─────────────┐     ┌───────────┐
│  WATCHDOG   │────►│   CYCLE   │ (Hauptloop, prüft Aufgaben)
│  (checker)  │     └─────┬─────┘
└─────────────┘           │
                          │ prüft: WANN? (datum/uhrzeit)
                          │ wenn JETZT → ausführen
                          ▼
                  ┌───────────────┐
                  │ AUFGABE       │
                  │ wann: cron/dt │
                  │ modul: X      │
                  │ anweisung: Y  │
                  └───────┬───────┘
                          │
                          ▼
                  ┌───────────────┐
                  │ MODUL         │ (z.B. mail.privat, chat.roland)
                  │ hat: LLM     │
                  │ hat: Tools    │
                  │ hat: Rechte   │
                  └───────┬───────┘
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
         ┌────────┐  ┌────────┐  ┌────────┐
         │ TOOL 1 │  │ TOOL 2 │  │  LLM   │
         │ (imap) │  │ (smtp) │  │(denken)│
         └────────┘  └────────┘  └────────┘
```

## Ergebnis-Prinzip

JEDE Operation endet mit genau einem von zwei Ergebnissen:
- **SUCCESS** + Ergebnis-Daten
- **FAILED** + Fehler-Beschreibung

Es gibt nichts dazwischen. Kein "maybe", kein "partial".

## Aufgaben-Pipeline

Aufgaben sind JSON-Dateien auf der HDD in 3 Ordnern:

```
agent-data/
├── erstellt/    ← neue Aufgaben warten hier
├── gestartet/   ← Cycle arbeitet daran
├── erledigt/    ← SUCCESS oder FAILED
├── config.json  ← Gesamtkonfiguration
└── logs/        ← JSONL pro Tag
```

### Aufgaben-Format

```json
{
  "id": "uuid",
  "version": 1,
  "wann": "sofort | cron-expr | ISO-datetime",
  "modul": "mail.privat",
  "anweisung": "Suche Mails von Thomas",
  "timeout_s": 10,
  "retry": 3,
  "retry_count": 0,
  "status": "Erstellt",
  "ergebnis": null,
  "erstellt_von": "chat.roland",
  "history": []
}
```

Erste Zeile ist IMMER `wann` - der Cycle prüft das zuerst.
Bei Crash: Aufgabe liegt in `gestartet/`, Cycle nimmt sie beim Neustart wieder auf.
Bei Update: alte Version wandert in `history`, neue Version überschreibt.

## Module

Module sind benannt als `typ.name` (z.B. `mail.privat`, `chat.roland`, `web.kolobri`).
Jedes Modul hat:

- **Eigenes LLM Backend** (z.B. Gemma4 lokal, Claude API, Grok API)
- **Backup LLM** (Fallback wenn Primary nicht erreichbar)
- **Berechtigungen** (Whitelist: auf welche anderen Module/Tools darf zugegriffen werden)
- **Timeout** (wie lange darf eine Operation dauern)
- **Retry** (wie oft bei Fehler wiederholen)
- **Identity** (Name, Greeting, System Prompt)
- **Eigenes RAG** (optional, shared oder privat)
- **Eigene Tools** (Funktionen die das Modul ausführen kann)

### Modul-Typen und ihre Tools

#### chat (z.B. chat.roland)
- INPUT-Modul: empfängt Nachrichten vom User
- Tools: `aufgaben.erstellen`, `rag.suchen`, `rag.speichern`
- KEIN Zugriff auf: mail, filesystem, web (außer explizit erlaubt)
- Funktion: Versteht User-Anweisungen, erstellt Aufgaben für andere Module, chattet

#### mail (z.B. mail.privat, mail.arbeit)
- Tools: `imap.search`, `imap.read`, `smtp.send`
- Settings: imap_host, imap_port, smtp_host, smtp_port, email, password
- Funktion: Mails lesen, durchsuchen, senden, zusammenfassen

#### filesystem (z.B. files.lokal)
- Tools: `files.read`, `files.write`, `files.list`
- Settings: allowed_paths (Whitelist!)
- Funktion: Dateien lesen/schreiben NUR in erlaubten Pfaden

#### websearch (z.B. web.kolobri)
- Tools: `http.get`, `http.post`
- Settings: allowed_domains, max_results
- Funktion: Webseiten abrufen, Inhalte extrahieren

#### aufgaben (System-Modul)
- KEIN LLM nötig
- Reines Datei-Management der Pipeline
- Wird vom Cycle direkt gesteuert

#### rag (System-Modul)
- Vektor-Suche / Smart Context
- Kann shared oder privat pro Modul sein
- Tools: `rag.suchen(query)`, `rag.speichern(text, metadata)`

## Tool Calling

So funktioniert Tool Calling in der Platform:

### 1. LLM bekommt verfügbare Tools im System Prompt

```
Du bist Roland. Du hast folgende Tools:

[TOOL:aufgaben.erstellen(modul, anweisung, wann)]
  Erstellt eine neue Aufgabe für ein anderes Modul.

[TOOL:rag.suchen(query)]
  Durchsucht das Wissens-Archiv nach relevanten Informationen.

[TOOL:rag.speichern(text)]
  Speichert eine Information im Wissens-Archiv.

Wenn du ein Tool nutzen willst, antworte mit:
<tool>name(param1, param2)</tool>

Du bekommst dann das Ergebnis und kannst weiter antworten.
```

### 2. Cycle erkennt Tool-Call in LLM-Antwort

```
LLM antwortet: "Ich suche mal im Archiv. <tool>rag.suchen(Thomas Projekt)</tool>"

Cycle:
  1. Erkennt <tool>...</tool> Tag
  2. Parsed: tool=rag.suchen, params=["Thomas Projekt"]
  3. Prüft Berechtigung: hat chat.roland Zugriff auf rag? → JA
  4. Führt rag.suchen("Thomas Projekt") aus
  5. Ergebnis: SUCCESS + "Thomas arbeitet an Projekt X seit März..."
  6. Gibt Ergebnis zurück ans LLM
  7. LLM antwortet final mit dem Wissen
```

### 3. Berechtigungs-Check

BEVOR ein Tool ausgeführt wird, prüft der Cycle:
- Hat das Modul die Berechtigung für dieses Tool?
- Ist der Pfad/die Domain in der Whitelist? (für files/web)
- Ist das Timeout eingehalten?

Wenn NEIN → Tool wird nicht ausgeführt, LLM bekommt "DENIED: keine Berechtigung"

## Cycle-Ablauf

```rust
loop {
    // 1. Heartbeat setzen
    heartbeat.update(now);
    
    // 2. Aufgaben in gestartet/ prüfen (Crash Recovery)
    for aufgabe in pipeline.gestartet() {
        if aufgabe.status == Failed { continue; }
        ausfuehren(aufgabe);
    }
    
    // 3. Aufgaben in erstellt/ prüfen
    for aufgabe in pipeline.erstellt() {
        if ist_faellig(aufgabe.wann) {
            aufgabe → gestartet/
            ausfuehren(aufgabe);
        }
    }
    
    // 4. Sleep
    sleep(cycle_interval);
}
```

### ausfuehren(aufgabe):

```
1. Finde Modul-Config für aufgabe.modul
2. Baue System-Prompt mit verfügbaren Tools (basierend auf Berechtigungen)
3. Sende an LLM: System-Prompt + Anweisung
4. Parse LLM-Antwort:
   a. Enthält <tool>...</tool>?
      → Parse Tool-Name und Parameter
      → Berechtigungs-Check
      → Tool ausführen (mit Timeout)
      → Ergebnis (SUCCESS/FAILED) zurück ans LLM
      → Wiederhole ab Schritt 3 mit Ergebnis als Context
   b. Keine Tool-Calls mehr?
      → Finale Antwort = Ergebnis der Aufgabe
5. aufgabe.ergebnis = finale Antwort
6. aufgabe → erledigt/ mit SUCCESS
   ODER bei Fehler:
   retry_count < retry? → aufgabe → erstellt/ (nochmal versuchen)
   sonst → erledigt/ mit FAILED
```

## Chat-Modul (Webchat)

Das Chat-Modul ist ein INPUT-Modul. Der User redet mit dem Agent.
Der Chat-Agent versteht die Anweisung und kann:

1. **Direkt antworten** (wenn er die Antwort weiß)
2. **Im RAG suchen** (wenn er Kontext braucht)
3. **Aufgabe erstellen** (wenn ein anderes Modul etwas tun soll)
4. **Ins RAG speichern** (wenn der User sagt "merk dir das")

Im Chat-UI sieht der User was der Agent tut:
- "🔍 Suche im RAG..."
- "📝 Speichere im RAG..."
- "📋 Erstelle Aufgabe für mail.privat..."

## Web-UI

### Tab: Chat
- Modul-Auswahl (welcher Chat-Agent)
- Clear Context Button
- Streaming mit tok/s Stats
- Zeigt Tool-Nutzung an

### Tab: Aufgaben (Kanban)
- Erstellt → Gestartet → Erledigt
- Task-Cards mit Status-Glow

### Tab: Config
- LLM Gems oben (Slot-System)
- Module Cards in der Mitte mit SVG-Linien zu LLMs
- RAG Pools unten
- Socket-Klick zum Verbinden
- Jedes Modul zeigt seine verfügbaren Tools

### Tab: Monitor
- Heartbeat vertikal (neueste oben)
- Zeigt: Cycle-Status, Modul-Aktivität, SUCCESS/FAILED

## Tech Stack

- **Backend**: Rust (axum, tokio, reqwest, serde)
- **Frontend**: Single HTML file, pure CSS/JS, kein Framework
- **LLM**: Ollama, OpenAI-kompatibel, Anthropic (konfigurierbar)
- **Storage**: JSON-Dateien auf HDD
- **RAG**: Einfache Keyword-Suche in JSON-Dateien (später Vektor-DB)

## Dateien

```
agent/
├── src/
│   ├── main.rs          ← Startet Cycle + Watchdog + Webserver
│   ├── types.rs         ← Alle Datenstrukturen
│   ├── cycle.rs         ← Hauptloop mit Tool-Calling-Engine
│   ├── watchdog.rs      ← Heartbeat-Checker
│   ├── llm.rs           ← LLM Router (Ollama/OpenAI/Anthropic)
│   ├── pipeline.rs      ← Erstellt/Gestartet/Erledigt Ordner-Logik
│   ├── tools.rs         ← Tool-Registry und Ausführung
│   ├── modules/
│   │   ├── mod.rs
│   │   ├── mail.rs      ← IMAP/SMTP Funktionen
│   │   ├── files.rs     ← Dateizugriff (Whitelist-basiert)
│   │   ├── web.rs       ← HTTP GET/POST
│   │   └── rag.rs       ← RAG Suche/Speicherung
│   ├── web.rs           ← API Endpoints
│   └── frontend.html    ← Web-UI
├── agent-data/
│   ├── config.json
│   ├── erstellt/
│   ├── gestartet/
│   ├── erledigt/
│   ├── logs/
│   └── rag/             ← RAG Daten pro Pool
└── Cargo.toml
```
