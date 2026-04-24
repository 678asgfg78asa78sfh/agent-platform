# Conversational Agent Creation Wizard — Design Spec

**Datum:** 2026-04-17
**Status:** Approved (Brainstorming)
**Nächster Schritt:** Writing-Plans (Implementation Plan)

---

## 1. Goal & USP

Ein KI-getriebener Conversational Wizard für die Agent-Erstellung — der User beschreibt in freier Prosa was er will, eine dedizierte Wizard-LLM führt einen strukturierten Dialog auf Deutsch, stellt Rückfragen, baut den Agent Schritt für Schritt, und jedes Feld ist vor Commit editierbar/korrigierbar. Das differenziert die Plattform von allen existierenden Agent-Frameworks, die ausschliesslich Formular-basiert arbeiten.

Primärer Mehrwert: (a) Exploration für unklare Intents ("Bot der meine Mails liest und mir ntfy schickt wenn was wichtig ist"), (b) intelligenter Permissions-/Linking-Vorschlag statt manuellem Zusammenklicken, (c) iterative System-Prompt-Verfeinerung, (d) optional Py-Modul-Generierung wenn ein fehlendes Tool identifiziert wurde.

## 2. Scope-Entscheidungen (User-confirmed)

| # | Entscheidung | Impact |
|---|---|---|
| 1 | **Py-Modul-Generierung erlaubt** via `module_builder` + `editor` Tools — hinter `wizard.allow_code_gen`-Flag (Default `false`) | Code-Gen-Pfad ist Phase 3, Infrastruktur darauf vorbereitet |
| 2 | **Drei Modi:** New / Copy / Edit — alle in derselben State-Machine, unterscheiden sich nur durch Draft-Initialisierung und Commit-Target | Single code path, drei Einstiegspunkte |
| 3 | **Eigener Wizard-LLM-Space** mit `config.wizard.llm = {provider, api_url, api_key, model, timeout_ms}`. Provider: OpenAI, Grok, Claude, OpenRouter. Model-Liste live vom Provider gepullt, freier Texteintrag möglich. Kein Ollama (zu schwach für Tool-Calling + Multi-Round-Dialog) | Wizard-LLM isoliert von Agent-LLMs |
| 4 | **Koexistenz** mit bestehendem Formular-Wizard — "Neuer Agent" öffnet Splash mit zwei Optionen | Kein Regression-Risiko, User-Wahl |
| 5 | **Disk-First-Session-Persistenz:** jeder Turn schreibt sofort auf Disk nach `agent-data/wizard-sessions/<id>.json` | Pipeline-Philosophie, Restart-robust, User-inspectable |
| 6 | **Hybrid State-Machine:** Rust hält Invarianten (welche Felder existieren, welche zwingend, Konsistenzregeln), LLM fährt den Dialog frei | Deterministische Garantien + natürlicher Dialog |
| 7 | **Hybrid Info-Model:** System-Prompt enthält Modul-Namen + Einzeiler, Detail via `wizard.inspect_module`-Tool | Konsistent über alle Provider, vorhersehbare Token-Kosten |

## 3. Non-Goals (explicit)

- **Kein** Editing mehrerer Agents in derselben Session.
- **Kein** Replace des Formular-Wizards (coexist, nicht ersetzen).
- **Kein** Ollama-Support als Wizard-Backend.
- **Kein** Multi-User-Session-Sharing (jede Session ist pro Client).
- **Kein** Natural-Language-Config-Import (z.B. "import from Zapier/n8n"). Out-of-Scope.
- **Kein** persistenter Chat-Verlauf nach Commit (Session wird archiviert, aber nicht als "ongoing chat" weitergeführt).

## 4. Architektur-Überblick

```
Browser (wizard.html)
   │
   │ POST /api/wizard/start    → {session_id, draft}
   │ POST /api/wizard/turn     ← NDJSON stream (assistant_text, tool_call, draft_full, ...)
   │ POST /api/wizard/patch    ← user edits preview field
   │ POST /api/wizard/confirm-code-gen
   │ POST /api/wizard/abort
   │ GET  /api/wizard/sessions
   │ GET  /api/wizard/models?provider=X
   ▼
web.rs (routes)  ←→  wizard.rs (session store, tool handlers, invariants)
                         │
                         ├─ llm.rs::send_chat_with_tools (Wizard-LLM backend instance)
                         ├─ validate_for_commit (hard invariants)
                         ├─ agent-data/wizard-sessions/<id>.json (disk-first)
                         └─ config.json lock (via existing config mutex)
```

**Kern-Invariante:** Wizard-LLM kann *nie* direkt Config schreiben. Der einzige Pfad zur Config-Änderung ist `wizard.commit` → `validate_for_commit` → bestehende Config-Write-Infrastruktur. Schlägt `validate_for_commit` fehl, geht der Fehler als Tool-Result zurück an die LLM, die ihn dem User in Prosa erklärt.

## 5. Files & Änderungen

### Neu

- `src/wizard.rs` — Session-Store, Tool-Handler, `validate_for_commit`, State-Machine-Hints. Kein LLM-Code; nutzt `llm::send_chat_with_tools`.
- `src/wizard.html` — Split-View-Frontend (eingebettet, Axum-served).
- `modules/templates/wizard.txt` — System-Prompt (Modul-Summaries, Tool-Regeln, Dialog-Guideline auf Deutsch).
- `tests/wizard_flow.rs` — Integration-Tests mit Mock-LLM.

### Modifiziert

- `src/types.rs` — `DraftAgent`, `DraftIdentity`, `WizardMode`, `WizardSession`, `WizardMessage`, `WizardToolCall`, `WizardCodeGenProposal`.
- `src/web.rs` — 7 neue Routen unter `/api/wizard/*` (s. §7).
- `src/main.rs` — Config-Block `wizard` laden, Session-Verzeichnis initialisieren, Cleanup-Task für expired Sessions starten.
- `src/security.rs` — `WizardRateLimiter` (analog `ChatRateLimiter`), Session-ID-Generator (128-bit crypto-random).
- `src/llm.rs` — neue Methode `chat_with_tools_adhoc(backend: &LlmBackend, messages, tools) -> Result<(String, Value), String>`, die einen Ad-hoc-Backend (aus `config.wizard.llm`) akzeptiert statt Lookup über `backend_id` in `config.llm_backends`. Sonst identische Provider-Logik.
- `src/frontend.html` — "Neuer Agent"-Button öffnet Splash statt direkt Modal; "Mit KI bearbeiten"- und "Kopieren mit KI"-Buttons in der Modul-Liste; Offene-Sessions-Badge; neuer Settings-Tab-Abschnitt *"Wizard-LLM"*.

Keine neuen Crate-Dependencies erwartet (getrandom, tokio, axum, serde sind bereits da).

## 6. Data Model

```rust
// src/types.rs

pub struct DraftAgent {
    pub id: Option<String>,
    pub typ: Option<String>,                          // chat|filesystem|websearch|shell|notify|cron
    pub llm_backend: Option<String>,
    pub persistent: bool,                             // default false
    pub token_budget: Option<u64>,
    pub scheduler_interval_ms: Option<u64>,
    pub max_concurrent_tasks: Option<u32>,
    pub linked_modules: Vec<String>,
    pub berechtigungen: Vec<String>,
    pub identity: DraftIdentity,
    pub settings: serde_json::Value,                  // typ-spezifische Settings (cron, shell, etc.)
}

pub struct DraftIdentity {
    pub bot_name: Option<String>,
    pub language: Option<String>,                     // "de"|"en"|...
    pub personality: Option<String>,                  // "professional"|"friendly"|...
    pub system_prompt: Option<String>,
}

pub enum WizardMode {
    New,
    Copy { source_id: String },
    Edit { target_id: String },
}

pub struct WizardSession {
    pub session_id: String,                           // 128-bit crypto-random, base64url
    pub mode: WizardMode,
    pub draft: DraftAgent,
    pub original: Option<ModulConfig>,                // für Copy/Edit: Ausgangszustand für Diff
    pub transcript: Vec<WizardMessage>,
    pub llm_rounds_used: u32,
    pub created_at: i64,
    pub last_activity: i64,
    pub code_gen_proposal: Option<WizardCodeGenProposal>,
}

pub struct WizardMessage {
    pub role: String,                                 // "user" | "assistant" | "tool"
    pub content: String,
    pub tool_calls: Vec<WizardToolCall>,
    pub tool_result: Option<String>,
    pub timestamp: i64,
}

pub struct WizardToolCall {
    pub id: String,                                   // LLM-generated call ID
    pub tool_name: String,
    pub arguments: serde_json::Value,
}

pub struct WizardCodeGenProposal {
    pub module_name: String,
    pub description: String,
    pub tools: Vec<ProposedTool>,
    pub source_code: String,
    pub user_decision: Option<CodeGenDecision>,       // None = pending, Some = approved/rejected
}

pub enum CodeGenDecision {
    Approved,
    Rejected { reason: String },
}
```

## 7. Wire Protocol

### Endpoints

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/wizard/start` | `{mode: "new"\|"copy"\|"edit", source_id?}` | `{session_id, draft}` |
| POST | `/api/wizard/turn` | `{session_id, text}` | NDJSON stream (siehe unten) |
| POST | `/api/wizard/patch` | `{session_id, field, value}` | `{ok, draft, missing_for_commit}` |
| POST | `/api/wizard/confirm-code-gen` | `{session_id, approved: bool, reason?}` | NDJSON stream (live scaffold/test output) |
| POST | `/api/wizard/abort` | `{session_id}` | `{ok: true}` |
| GET | `/api/wizard/sessions` | — | `[{session_id, mode, draft_summary, last_activity}]` |
| GET | `/api/wizard/models?provider=X` | — | `{models: [{id, display_name, context_length?}]}` |

Alle Routen: Bearer-Auth (`api_auth_token`), Body-Limit (`max_body_bytes`), Rate-Limit (`rate_limit_per_min` aus Wizard-Config).

### NDJSON-Events (ein JSON-Objekt pro Zeile, bei `/api/wizard/turn` und `/api/wizard/confirm-code-gen`)

| `type` | Payload | Wann |
|---|---|---|
| `session` | `{session_id, mode}` | Erstes Event pro Stream |
| `assistant_text` | `{delta: string}` | LLM streamt Text an User |
| `tool_call` | `{tool, arguments}` | LLM hat Tool aufgerufen (informational) |
| `draft_full` | `{draft, missing_for_commit, next_suggested?}` | Nach jedem State-ändernden Tool-Call |
| `ask` | `{question, options?}` | `wizard.ask` aufgerufen |
| `code_gen_proposal` | `{proposal}` | `wizard.create_py_module` aufgerufen |
| `code_gen_step` | `{step: "scaffold"\|"write"\|"test"\|"activate", status, output?}` | Während Code-Gen-Ausführung |
| `commit_ok` | `{agent_id}` | `wizard.commit` erfolgreich |
| `commit_error` | `{errors: [ValidationError]}` | `wizard.commit` verletzt Invarianten |
| `error` | `{message}` | Fataler Fehler (LLM-Backend down, Rate-Limit, etc.) |
| `done` | `{}` | Turn beendet |

### Tool-Interface (LLM-visible)

Alle Tools folgen dem OpenAI-Function-Calling-Format. Liste:

1. **`wizard.propose(field, value, reasoning)`** — patcht `draft.<field>`. Rust validiert Feld-Type, Rückgabe: `{ok, draft, missing_for_commit, next_suggested}`.
2. **`wizard.ask(question, options?)`** — streamt strukturierte Frage zum Frontend, kein State-Change. Rückgabe: `{ack: true}`.
3. **`wizard.list_modules()`** — returnt `[{id, typ, identity.bot_name, linked_modules}]` aller existierenden Module.
4. **`wizard.inspect_module(id)`** — returnt komplette ModulConfig als JSON.
5. **`wizard.list_py_modules()`** — returnt `[{name, description, tools: [{name, description, params}]}]`.
6. **`wizard.commit()`** — ruft `validate_for_commit`, schreibt Config (unter Config-Mutex), archiviert Session. Rückgabe: `{ok, agent_id}` oder `{ok: false, errors}`.
7. **`wizard.abort(reason)`** — löscht Session, returnt `{ok: true}`.
8. **`wizard.create_py_module(name, description, tools, source_code)`** *(nur wenn `allow_code_gen=true`)* — speichert Proposal, streamt `code_gen_proposal`-Event. Returnt `{awaiting_user_confirmation: true}`. LLM muss auf User-Aktion warten, bevor weitere Tools gerufen werden.

## 8. Invariants (`validate_for_commit`)

Reine Funktion `fn validate_for_commit(draft: &DraftAgent, cfg: &Config, mode: &WizardMode) -> Result<(), Vec<ValidationError>>`. Prüfungen:

1. `draft.id` gesetzt, matches regex `^[a-z][a-z0-9._-]*$`, max 64 Zeichen.
2. Kollisions-Check: `id` darf nicht in `cfg.module` existieren — *ausser* `mode == Edit { target_id }` und `id == target_id`.
3. `draft.typ` ∈ {`chat`, `filesystem`, `websearch`, `shell`, `notify`, `cron`}.
4. Wenn `typ == "chat"`: `llm_backend` muss gesetzt sein und in `cfg.llm_backends` existieren.
5. `token_budget > 0` (wenn gesetzt); `scheduler_interval_ms >= 500` (wenn gesetzt); `max_concurrent_tasks >= 1` (wenn gesetzt).
6. `linked_modules`: jedes Element muss in `cfg.module` existieren (oder in derselben Commit-Transaktion neu erstellt werden — out-of-scope für Phase 1, nur Pre-existing).
7. `berechtigungen`: jedes Element muss aus `linked_modules` + `typ` ableitbar sein ODER in einer expliziten Whitelist (`aufgaben`, `rag.shared`, `rag.private`).
8. `identity.bot_name` nicht leer, max 64 Zeichen.
9. `identity.system_prompt` nicht leer, max 20.000 Zeichen.
10. `settings`-Shape passt zum `typ` (z.B. `typ=cron` verlangt `settings.schedule` + `settings.cron_typ`).

Jede Verletzung → `ValidationError { field: String, code: String, human_message_de: String }`. LLM bekommt Liste aller Fehler, nicht nur den ersten.

## 9. Wizard-LLM-Config (`config.json`)

```json
{
  "wizard": {
    "enabled": true,
    "llm": {
      "provider": "Claude",
      "api_url": "https://api.anthropic.com",
      "api_key": "sk-ant-...",
      "model": "claude-haiku-4-5",
      "timeout_ms": 30000
    },
    "allow_code_gen": false,
    "max_rounds_per_session": 30,
    "max_tool_rounds_per_turn": 5,
    "session_timeout_secs": 600,
    "rate_limit_per_min": 10,
    "max_system_prompt_chars": 20000
  }
}
```

- Falls `wizard.enabled == false` oder `wizard.llm` fehlt: Alle `/api/wizard/*`-Routen returnen 503 mit Message "Wizard-Backend nicht konfiguriert".
- Model-Discovery (`GET /api/wizard/models?provider=X`): Server proxied API-Call (API-Key bleibt server-side). Claude: hardcoded Liste (claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5, …). OpenAI/Grok: `GET /v1/models`. OpenRouter: `GET /api/v1/models`.

## 10. Frontend UX

### Einstiegspunkte

- Dashboard "Neuer Agent"-Button → **Splash-Modal** mit zwei Kacheln:
  - *"Formular (schnell — für wenn du weisst was du willst)"* → öffnet existierendes `modal-agent-wizard`.
  - *"KI-Assistent (empfohlen — führt dich durch)"* → öffnet `/wizard?mode=new`.
- In der Modul-Liste pro Agent: **"Mit KI bearbeiten"**- und **"Kopieren mit KI"**-Buttons → öffnen `/wizard?mode=edit&source=<id>` bzw. `/wizard?mode=copy&source=<id>`.
- Header-Badge **"N offene Wizard-Session(s)"** (aus `GET /api/wizard/sessions`): Klick öffnet Liste mit Fortsetzen / Verwerfen je Session.

### Wizard-Seite (`wizard.html`)

Split-View 45/55, responsive (bei < 900px vertical stack).

**Chat-Pane (links):**
- Nachrichten-Liste (User rechts-bündig, Assistant links-bündig).
- Tool-Calls als collapsible Chips (z.B. *"Vorschlag: `bot_name = Roland` — weil du 'meine Mails' sagtest"*). Klick → expandiert mit vollem Reasoning.
- `wizard.ask` mit `options`: Button-Gruppe, ein Klick sendet die Option als User-Text.
- Streaming-Indikator "Wizard denkt…" während NDJSON-Stream läuft.
- Footer: Multi-line-Textarea, Submit mit Ctrl+Enter; Status-Zeile `Rounds: X/30` + `Timeout: Y:MM`.

**Preview-Pane (rechts):**
- Cards per Section: *Identität*, *LLM-Backend*, *Linking & Berechtigungen*, *System-Prompt*, *Scheduler & Budget*, *Settings*.
- Felder inline editierbar (Enter oder Blur → `POST /api/wizard/patch`). Updates triggern kein LLM-Round.
- Origin-Badge je Feld: Text-Label `Wizard` (blaues Pill) für vom Wizard gesetzte Felder, `Du` (graues Pill) für vom User überschriebene Felder. Unverändert leere Felder haben kein Badge.
- Missing-Pflichtfelder: rot umrandet, Tooltip "Noch fehlt: …".
- In Copy/Edit-Mode: Toggle **"Nur Änderungen anzeigen"** → filtert auf Diff gegenüber `original` (grün=neu, gelb=geändert, rot=entfernt).
- Commit-Button unten: disabled solange `missing_for_commit` nicht leer; Tooltip listet fehlende Felder.
- Commit-Klick → **Diff-Modal** "Das wird geschrieben" mit vorher/nachher (für Edit/Copy vs existierender Config; für New: vollständiger Draft). User muss *"Ja, schreiben"* klicken.

**Code-Gen-Proposal-Modal** (nur wenn `allow_code_gen=true`):
- Titel: *"Der Wizard möchte ein neues Py-Modul erstellen: `<name>`"*.
- Sections: Beschreibung, Tool-Liste mit Params, Source-Code (syntax-highlighted, scrollable, read-only).
- Checkbox **"Ich habe den Code überflogen und verstehe was er tut"** → aktiviert Approve-Button.
- **Reject**-Button mit Begründungs-Textarea (wird als Tool-Result an LLM zurückgegeben).
- Nach Approve: Live-Log-Panel mit Scaffold/Write/Test/Activate-Events, jeder Schritt mit Status-Icon (running/ok/fail).

**Settings-Tab-Abschnitt "Wizard-LLM":**
- Provider-Dropdown (Claude/OpenAI/Grok/OpenRouter).
- API-URL (prefilled je Provider, editierbar).
- API-Key (password-Feld, masked).
- Model-Dropdown (gepullt via `/api/wizard/models?provider=…` bei Provider-Wechsel; freier Texteintrag möglich falls Model neuer als Liste).
- Timeout (ms), `allow_code_gen`-Toggle, Limits (rounds/session, rounds/turn, session-timeout, rate/min).
- "Verbindung testen"-Button → macht Dummy-Call, zeigt OK/Fehler.

## 11. Security-Guardrails

| Ebene | Schutz |
|---|---|
| Transport | Alle Routen hinter `api_auth_token` (gleich wie Rest-API). |
| Rate-Limit | `WizardRateLimiter` pro IP, `rate_limit_per_min` aus Config. |
| Body-Limit | Bestehender `max_body_bytes`-Check greift auf alle Routen. |
| Session-ID | 128-bit crypto-random, URL-safe base64, nicht sequentiell. |
| Caps | 30 LLM-Rounds/Session, 5 Tool-Rounds/User-Turn, 10 Min Idle-Timeout, 20k Zeichen System-Prompt. |
| Config-Write | `wizard.commit` schreibt durch denselben Mutex wie `POST /api/config` — kein Race mit manuellen Edits. |
| Secret-Redaction | Wizard-Transcript geht durch `security::redact_secrets` bevor auf Disk geschrieben. |
| Audit-Log | Jeder `commit`, jeder `code-gen`-Step, jede `abort` → Audit-Log mit `event=wizard.commit\|wizard.codegen\|wizard.abort`. |
| Validation | `validate_for_commit` ist der **einzige** Pfad zur Config-Änderung. LLM-unumgehbar. |

**Code-Gen-Zusatz-Schutz (Phase 3):**
- `module_name` regex `^[a-z][a-z0-9_]*$`, max 32 Zeichen.
- `module_name` darf nicht in `SYSTEM_MODULES` (bereits in `module_builder` enforced) und nicht mit existierendem kollidieren.
- `source_code` max 50 kB.
- Doppelte User-Bestätigung: LLM-Proposal + Checkbox + Approve-Button.
- Test-Phase ist Pflicht; nur bei grünem Test wird aktiviert.
- Rollback verfügbar: `module_builder.delete` im Frontend erreichbar.
- Jeder Code-Gen-Schritt als separater Audit-Log-Entry.

## 12. Code-Gen-Flow (Phase 3)

```
1. LLM ruft wizard.create_py_module(name, description, tools, source_code)
2. Rust validiert Regex/Kollision/Grösse; wenn OK:
   → session.code_gen_proposal = {...}, user_decision = None
   → NDJSON: {type: "code_gen_proposal", proposal}
   → Tool-Result an LLM: {awaiting_user_confirmation: true}
   → LLM muss warten (System-Prompt-Regel)
3. Frontend zeigt Proposal-Modal
4. User Approve:
   → POST /api/wizard/confirm-code-gen {session_id, approved: true}
   → Rust führt sequentiell (jeder Schritt streamt ein code_gen_step-Event):
      a) module_builder.scaffold(name, description, tools) — legt modules/<name>/ an und
         schreibt ein Template-module.py. (Template wird in Schritt b überschrieben;
         scaffold wird genutzt wegen konsistenter Verzeichnisstruktur + README-Registrierung.)
      b) editor.create(modules/<name>/module.py, source_code) — überschreibt Template
         mit dem vom LLM gelieferten Source.
      c) module_builder.test(name) — startet Subprocess, ruft `describe` + probe `handle_tool`.
         Stdout/Stderr gehen live ins code_gen_step-Event.
      d) Bei Test-Erfolg: module_builder.activate(name) — Modul wird im Process-Pool registriert
         und taucht in wizard.list_py_modules sowie im Dashboard auf.
   → Proposal cleared, Tool-Result an LLM:
      - Bei Erfolg: {ok: true, module_name, tools: [...]}
      - Bei Fehler: {ok: false, failed_step, output}
   → LLM reagiert (meist: wizard.propose("linked_modules", [...]))
5. User Reject:
   → POST /api/wizard/confirm-code-gen {approved: false, reason}
   → Proposal cleared, Tool-Result an LLM: {rejected: true, reason}
   → LLM kann Alternative vorschlagen
```

## 13. Testing Strategy

### Unit (`src/wizard.rs`-Tests)
- `validate_for_commit`: min. 20 Cases (valid draft, jedes Einzel-Feld invalid, Kombination-Invarianten, Edit-vs-New-Kollision, Berechtigungs-Ableitung).
- Feld-Patch-Validation: richtiger Type, Range, Length pro Feld.
- Session-JSON-Roundtrip: serialize/deserialize-Equality.
- NDJSON-Event-Serde: alle Event-Typen round-trip.
- Session-ID-Generator: Uniqueness über 10k Aufrufe, Länge, Zeichensatz.
- Linking→Permissions-Derivation: für jeden `typ` und typische `linked_modules`-Kombinationen.

### Integration (`tests/wizard_flow.rs` mit Mock-LLM)
Mock-LLM liest scripted Tool-Call-Sequenzen aus Test-Files, returnt sie deterministisch.

- **Happy New:** Intent→Identity→Linking→Prompt→Commit schreibt `config.json` korrekt, Session in `archived/`.
- **Happy Copy:** source-Agent bleibt unberührt, neuer Agent mit neuer ID.
- **Happy Edit:** Target-Agent überschrieben, andere Agents unberührt.
- **Abort mid-session:** Session-File gelöscht, keine Config-Änderung.
- **Commit mit fehlenden Feldern:** `commit_error` mit allen fehlenden Feldern, Config unverändert.
- **Rate-Limit:** 11. Req/min → 429.
- **Round-Cap:** 31. LLM-Round → Session freezed, Error-Event.
- **Session-Timeout:** nach 10 Min ohne Activity → Cleanup-Task löscht Session-File.
- **Code-Gen deny:** Proposal, User rejected → kein File geschrieben, LLM bekommt Reason.
- **Code-Gen approve:** Proposal, User approved → Py-Modul in `modules/<name>/` existiert, getestet, in Config.
- **Code-Gen test fail:** LLM schreibt broken code → Test failed, kein Activate, LLM kriegt Error-Output.
- **Secret-Redaction:** User pastet API-Key in Chat → Session-File enthält `[REDACTED]`.
- **Concurrent Config-Edit:** Wizard committed während manuelles `/api/config` läuft → beide serialisiert durch Mutex, kein Data-Loss.

### Mock-LLM
Simple Datei-basierte Scripted Responses: `tests/wizard_fixtures/<scenario>.ndjson` enthält erwartete Tool-Call-Sequenz je User-Message. Mock-LLM ersetzt die echte LLM-Implementation nur in `#[cfg(test)]`.

## 14. Phased Delivery

| Phase | Inhalt | Schätzung | Dependencies |
|---|---|---|---|
| 1 — MVP | New/Copy/Edit-Flow, Split-View, Invariants, Commit, NDJSON-Stream, Mock-LLM-Tests. **Ohne** Code-Gen. Settings-Tab-Abschnitt "Wizard-LLM". Model-Discovery. | 2-3 Tage | — |
| 2 — Polish | Diff-Modal beim Commit, Session-Resume via Badge, Offene-Sessions-Liste im Header, "Verbindung testen"-Button, Visual-Polish. | 1-2 Tage | Phase 1 |
| 3 — Code-Gen | `allow_code_gen`-Flag, `wizard.create_py_module`-Tool, Proposal-Modal, Approve-Flow, Live-Scaffold/Test-Output, Rollback-Button, Audit-Log-Integration. | 2-3 Tage | Phase 2 |

Jede Phase ist mergeable in `main` unabhängig. Phase 1 ist voll funktional ohne Phase 2/3. Jede Phase komplett getestet (grüne Tests vor Merge, keine "tests-for-later").

---

## 15. Aufgelöste Detail-Entscheidungen

- **Mobile-Layout:** Split-View nur Desktop/Tablet (≥ 900px). Unter 900px wird die Preview-Pane als Akkordeon über dem Chat kollabiert, mit "Preview zeigen"-Button. Kein eigener Mobile-Flow für Phase 1.
- **`max_rounds_per_session` erreicht:** Session **freezed** → LLM-Aufrufe blockiert, UI zeigt Banner "Max. Runden erreicht — bitte Commit oder Abort". Nur `wizard.commit`, `/api/wizard/patch` und `/api/wizard/abort` bleiben aktiv. Kein Auto-Abort.
- **Config-Snapshots vor Commit:** Nicht implementiert. Audit-Log enthält die geschriebenen Felder. Wer Rollback-Versioning will, nutzt externes Git-Versioning der `config.json` (out-of-scope).
- **Gleichzeitiges Editing (Wizard + Edit-Modal):** Config-Mutex serialisiert beide Writes. Last-write-wins. Kein zusätzlicher Konflikt-Detection-Mechanismus in Phase 1 — identisches Verhalten wie aktuell zwei parallele `POST /api/config`-Calls.
- **Verhalten wenn `wizard.llm` unerreichbar** (z.B. API-Key falsch, Network-Down): `/api/wizard/turn` streamt `{type:"error", message}`-Event und returnt HTTP 200 (Stream gültig, Fehler im Body). Session bleibt intakt, User kann nach Fix retryen. Keine automatische Retry-Logik.
- **Bei Rate-Limit-Treffer auf `/api/wizard/turn`:** HTTP 429 bevor Stream startet (kein NDJSON-Body). Frontend zeigt Cooldown-Timer basierend auf `Retry-After`-Header.
