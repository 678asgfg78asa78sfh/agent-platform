# LLM Guardrail & Quality Dashboard — Design Spec

**Datum:** 2026-04-18
**Status:** Approved (Brainstorming)
**Nächster Schritt:** Writing-Plans

---

## 1. Goal

Zwischen jeder LLM-Response und der Tool-Execution sitzt ein deterministischer Validator, der strukturellen Müll (halluzinierte Tool-Namen, gebrochenes JSON, fehlende Required-Params) abfängt und die LLM mit strukturiertem Feedback zu einem Retry bringt. Jeder Check-Event wird persistent geloggt, aggregiert und als Dashboard dargestellt, damit der User erkennt wenn ein Modell für seine Anwendung **nicht tauglich** ist. Ein Built-in-Benchmark-Runner kann eine kuratierte Test-Suite gegen ein Backend feuern und eine Pass-Rate ausweisen.

**USP:** Kontrolle und Observability über LLM-Tool-Call-Qualität — die meisten Frameworks haben keinen Validator und keine Historie.

## 2. Scope-Entscheidungen (User-confirmed)

| # | Entscheidung | Wirkung |
|---|---|---|
| 1 | **Retry-Policy: B** — Max-N-Retries (Default 2) mit strukturiertem Fehler-Feedback an den LLM. Config: `guardrail.max_retries: u32`. Optional per-Backend-Override. | Gibt Cloud-LLMs Selbstheilung ohne Kosten-Explosion; Local-LLMs profitieren stark, Cloud kaum. |
| 2 | **Storage: B** — NDJSON-Event-Log auf Disk (`agent-data/guardrail-events/YYYY-MM-DD.jsonl`) mit Daily-Rotation. Aggregates in-memory, beim Startup aus letzten 7 Tagen rekonstruiert. | Passt zur Pipeline-Philosophie (File-based). Retention via bestehendes `log_retention_days`. |
| 3 | **Check-Scope:** Items 1-6 deterministisch (B) + Item 7 als `strict_mode`-Toggle (C). Keine externe jsonschema-Crate. | Fängt 90%+ Gibberish ohne neue Dependency; strict-mode bleibt out-of-the-box aus. |
| 4 | **UI: A + Mini-Card** — Neues Quality-Tab + kleine Live-Stats-Card neben den Config-Settings. | Prominent für Exploration, Feedback-beim-Konfigurieren. |
| 5 | **Benchmark: A** — Built-in-Suite (`modules/templates/benchmark_prompts.json`) mit ~20 kuratierten Prompts. User-defined als Out-of-Scope Phase 2+. | Sofort nutzbar, keine UI-Write-Flows in Phase 1. |
| 6 | **Global on/off** via `guardrail.enabled` (default true). **Kein per-Modul-Opt-Out** in Phase 1. | Weniger Knobs, klarere Semantik. |

## 3. Non-Goals (explicit)

- **Keine externe JSON-Schema-Library** (`jsonschema`-crate). Manuelle required/type-Checks sind ausreichend für unsere simple Tool-Shape.
- **Kein per-Modul-Opt-Out** (nur global on/off).
- **Keine Charts** in Phase 1. Nur Zahlen + Top-Fehler-Tabelle + Event-Liste. Rolling-Graphen kommen später oder nie.
- **Keine user-definierten Benchmark-Cases** (nur built-in Suite).
- **Kein zweiter LLM-Call** für Validation. Reiner Regel-Check. Ein "LLM-as-judge"-Pattern ist reizvoll, aber Out-of-Scope.
- **Kein SQLite**. NDJSON reicht bei erwarteten 1k-10k Events/Tag.
- **Kein Alerting** (z.B. "Modell X hat 30% fail-rate — sende Push"). Nur Display.

## 4. Architektur-Überblick

```
LLM-Response
     │
     ▼
parse_tool_calls(...)
     │
     ▼  (pre-execute hook, NEW)
guardrail::validate_response(raw, cfg, modul_id)
     │
     ├─ Ok(calls) ─────────► dispatch_tool / exec_tool_unified (existing)
     │
     └─ Err(errors) ─► log_event(fail) ─► retry_attempt < max?
                                       │
                          ┌────────────┴────────────┐
                          │                         │
                     YES: feed back              NO: hard_fail
                     error-message to LLM        emit CommitError /
                     as user-role msg,           Task-Failed with
                     run_turn loop continues     clear reason
```

**Kern-Invariante:** Kein Tool wird ausgeführt ohne `validate_response() == Ok(...)`.

## 5. Files & Änderungen

### Neu
- `src/guardrail.rs` — Validator-Funktionen, Event-Logger, Stats-Aggregator. Target ~600 Zeilen.
- `src/benchmark.rs` — Built-in-Suite-Runner (kleiner, Phase C). Target ~200 Zeilen.
- `modules/templates/benchmark_prompts.json` — 20 Standard-Test-Prompts.
- `tests/guardrail_tests.rs` *(optional; inline in guardrail.rs gemäß Projektstil)*.

### Modifiziert
- `src/types.rs` — `GuardrailConfig`, `GuardrailEvent`, `ValidationError` (bereits vorhanden, wiederverwenden), `StatsSummary`, `BackendStats`, `BenchmarkCase`, `BenchmarkResult`, `BenchmarkReport`. Extend `AgentConfig` mit `guardrail: Option<GuardrailConfig>`.
- `src/wizard.rs::run_turn` — nach `parse_tool_calls`, vor `dispatch_tool` → Guardrail-Hook mit Retry-Loop. Fehler gehen als user-role Message in den Transcript zurück zur LLM.
- `src/web.rs::chat_stream_endpoint` — gleiche Integration am Chat-Tool-Loop-Punkt.
- `src/cycle.rs` — LLM-Task-Handler: vor `exec_tool_unified` Hook aufrufen (mit `guardrail::validate_single_call`, da hier typischerweise eine direkt-formulierte Tool-Call-Struktur vorliegt).
- `src/web.rs` — neue Routen: `GET /api/quality/stats`, `GET /api/quality/events?since=...&limit=...&backend=...`, `POST /api/quality/benchmark/run`.
- `src/main.rs` — Init: `guardrail::ensure_dirs`, `guardrail::rebuild_aggregates_from_logs(7)` beim Startup, Cleanup-Task für Event-Log-Rotation (nutzt `log_retention_days`).
- `src/frontend.html` — Neuer Tab "Quality" + Mini-Stats-Card in Config-Tab.
- `src/security.rs` — Keine Änderungen; `redact_secrets` wird von `log_event` auf Events angewandt.

## 6. Data Model

```rust
// src/types.rs

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GuardrailConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_guardrail_retries")]
    pub max_retries: u32,
    #[serde(default)]
    pub strict_mode: bool,
    #[serde(default)]
    pub per_backend_overrides: std::collections::HashMap<String, u32>,
    #[serde(default = "default_guardrail_max_events_per_turn")]
    pub max_events_per_turn: u32,
}
fn default_guardrail_retries() -> u32 { 2 }
fn default_guardrail_max_events_per_turn() -> u32 { 10 }

// ValidationError reused from Wizard (already in types.rs):
// { field, code, human_message_de }
// codes: "bad_json" | "unknown_tool" | "no_permission"
//      | "missing_param" | "bad_param_type" | "gibberish"
//      | "no_tool_call_when_expected"

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GuardrailEvent {
    pub ts: i64,                            // unix seconds
    pub modul: String,                      // calling module id
    pub backend: String,                    // llm backend id
    pub model: String,                      // llm model string
    pub tool_name: Option<String>,          // tool that was attempted
    pub passed: bool,
    #[serde(default)]
    pub errors: Vec<ValidationError>,
    pub retry_attempt: u32,                 // 0 = first try, 1 = first retry, ...
    pub final_outcome: String,              // "ok" | "retried" | "hard_fail"
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub similar_suggestion: Option<String>, // "did you mean foo.bar?"
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct StatsSummary {
    pub total: u64,
    pub valid: u64,
    pub invalid: u64,
    pub retried: u64,
    pub hard_failed: u64,
    pub per_backend: std::collections::HashMap<String, BackendStats>,
    pub top_errors: Vec<(String, u64)>,     // ("unknown_tool", 42)
    pub window_hours: u32,                  // time window of the aggregate
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct BackendStats {
    pub total: u64,
    pub valid: u64,
    pub hard_failed: u64,
    pub per_model: std::collections::HashMap<String, ModelStats>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct ModelStats {
    pub total: u64,
    pub valid: u64,
    pub hard_failed: u64,
    pub last_ts: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BenchmarkCase {
    pub id: String,                         // "list-modules-01"
    pub prompt: String,
    pub expected: BenchmarkExpectation,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum BenchmarkExpectation {
    ToolCalled { tool_name: String },
    NoToolCall,                             // pure-text answer is correct
    Denied,                                 // should be permission-denied
}

#[derive(Debug, Clone, Serialize)]
pub struct BenchmarkResult {
    pub case_id: String,
    pub prompt: String,
    pub passed: bool,
    pub actual_tool: Option<String>,
    pub errors: Vec<ValidationError>,
    pub latency_ms: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct BenchmarkReport {
    pub backend: String,
    pub model: String,
    pub started_at: i64,
    pub total_cases: usize,
    pub passed: usize,
    pub failed: usize,
    pub denied: usize,
    pub total_latency_ms: u64,
    pub results: Vec<BenchmarkResult>,
}
```

## 7. Validator Checks (Detail)

```rust
pub fn validate_response(
    raw: &serde_json::Value,
    cfg: &AgentConfig,
    modul_id: &str,
    py_modules: &[crate::loader::PyModuleMeta],
    last_user_msg: Option<&str>,
    strict: bool,
) -> Result<Vec<ParsedCall>, Vec<ValidationError>>
```

1. **JSON wohlgeformt** — `arguments` parse-bar als `Object(Map)`. Code: `bad_json`.
2. **Tool existiert** — `tool_name` ∈ bekannte Tools (built-in + linked-modules + py-module-tools).
3. **Tool erlaubt** — nutzt existierende `has_permission_with_py(modul, tool_name, py_modules)`.
4. **Required-Parameter** — pro Tool-Name lookup in registrierter Parameter-Spec (OpenAI-Function-Call-Schema aus Tool-Registrierung); falls `required: [...]` gesetzt, alle vorhanden.
5. **Parameter-Types** — String, Number, Array-of-String: via `serde_json::Value::is_*()` prüfen gegen das `type` aus dem Schema. Keine tiefe Validation (nested objects beyond 1 Level).
6. **Anti-Gibberish:**
   - `tool_name.matches(^[a-z][a-z0-9._]*$)`
   - `tool_name` nicht leer, nicht >64 Zeichen
   - Keine offensichtlichen Prosa-Muster (`"call"`, `"please"`, Leerzeichen im Namen)
7. **Strict-Mode-Only (Item 7):** Wenn `last_user_msg` bestimmte imperative Trigger-Wörter enthält (`"ruf"`, `"schicke"`, `"list"`, `"show"`, `"search"`, `"create"`) aber die Response gar keinen Tool-Call hat → Code `no_tool_call_when_expected`. Heuristik, nur aktiv wenn `strict_mode=true`.

**Bei jedem Fehler:** `similar_suggestion` computed via Levenshtein-Distance gegen bekannte Tool-Namen wenn Code = `unknown_tool`.

## 8. Retry-Loop (Pseudo-Code)

```rust
let max_retries = cfg.guardrail.per_backend_overrides
    .get(&backend_id)
    .copied()
    .unwrap_or(cfg.guardrail.max_retries);

for attempt in 0..=max_retries {
    let raw = backend.chat(messages, tools).await?;
    match guardrail::validate_response(&raw, cfg, modul_id, py_modules, last_user_msg, strict) {
        Ok(calls) => {
            guardrail::log_event(event_ok(backend_id, model, modul_id, attempt, calls.first()));
            return Ok(calls);
        }
        Err(errors) => {
            let is_last = attempt == max_retries;
            guardrail::log_event(event_fail(backend_id, model, modul_id, attempt, &errors, is_last));
            if is_last {
                return Err(format!("Guardrail hard-fail nach {} Retries: {:?}", max_retries, errors));
            }
            // Feed back as user-role message + continue
            messages.push(synth_feedback_user_message(&errors));
        }
    }
}
```

`synth_feedback_user_message` fasst die Fehler strukturiert zusammen, inkl. Levenshtein-Suggestions:

> SYSTEM-FEEDBACK: Dein letzter Tool-Call war ungültig. Fehler: unknown_tool="sehel.exec" (did you mean "shell.exec"?). Bitte korrigieren und erneut senden.

## 9. Wire-Protokoll (neue Endpoints)

| Method | Path | Returns |
|---|---|---|
| GET | `/api/quality/stats?hours=24` | `StatsSummary` |
| GET | `/api/quality/events?since=<ts>&limit=100&backend=<id>&only_failed=true` | `{events: Vec<GuardrailEvent>, has_more: bool}` |
| POST | `/api/quality/benchmark/run` body: `{backend_id, model?}` | NDJSON-Stream: `{type:"case_start"}`, `{type:"case_result", ...}`, `{type:"report", report}` |
| GET | `/api/quality/benchmark/cases` | aktuelle `Vec<BenchmarkCase>` aus template JSON |

Alle hinter `auth_middleware` + `rate_limit` (bestehende Infra).

## 10. Frontend UX

### Mini-Card im Config-Tab (Phase A)
Neben den Guardrail-Settings ein kleiner Status-Block:
```
Guardrail (letzte 24h)
▪ 127 Calls
▪ 119 valid (94%)    ← grün wenn ≥90%, gelb 70-89%, rot <70%
▪ 8 Retries  |  2 hard fails
▪ Top-Fehler: unknown_tool (5), bad_json (3)
[Details → Quality Tab]
```

### Quality-Tab (Phase B)
Kopfzeile: Zeitfenster-Selector (24h / 7d / alles), Backend-Filter-Dropdown.
Drei Cards nebeneinander:
1. **Aggregate** — Zahlen wie oben, aber für gewähltes Fenster.
2. **Pro Backend/Modell** — Tabelle `Backend | Model | Total | Valid-Rate | Last-Seen`.
3. **Top-Fehler** — Tabelle `Code | Count | Beispiel-Message`.

Darunter **Event-Liste** — scrollbare Tabelle mit `Zeit | Modul | Backend | Tool | Pass/Fail | Retry | Fehler`. Klick auf Row → Expand mit JSON-Details. Pagination 50/Page.

### Benchmark-UI (Phase C)
Im Quality-Tab Button "Benchmark gegen Backend laufen"; Dialog: Backend-Dropdown, Model-Override (optional), Start-Button. Danach Live-Progress-Panel:
```
Running benchmark: claude-haiku-4-5
[████████░░░░░░░] 12 / 20

✓ list-modules-01  (142ms)
✓ notify-send-01   (89ms)
✗ web-search-01    (errored: unknown_tool="web.find")
...
```
Bei Done: Report-Card mit Pass-Rate + Download-Button `report.json`.

## 11. Security / Safety

- `log_event` ruft `security::redact_secrets` auf Events → keine API-Keys, keine passwords in Event-Log.
- Event-Log-Pfade nutzen `safe_id` für Filenames (`YYYY-MM-DD.jsonl`).
- Benchmark-Runner validiert `validate_external_url` für das Backend, respektiert Rate-Limit.
- `max_events_per_turn: 10` blockt Retry-Loop-Explosion; bei Überschreitung wird geloggt + abgeschnitten.
- Audit-Log-Integration: `commit_ok` im Wizard und `hard_fail` im Guardrail ge-audit-loggt (bestehendes Audit-Pattern).

## 12. Testing

### Unit (`src/guardrail.rs`-Tests)
- `validate_response` mit ~15 scripted Szenarien: bad JSON, unknown tool, denied tool, missing required param, wrong type, gibberish patterns, strict-mode prose-detection (positive + negative).
- `suggest_similar_tool` Levenshtein-Korrektheit: `sehel.exec` → `shell.exec`, `foo.ba` → `foo.bar`, unrelated name → `None`.
- `log_event` Roundtrip (tempfile): write → re-load → assert.
- `load_recent_stats` mit synthetischen Events: aggregate correctness.

### Integration (`src/guardrail.rs` flow-tests mit MockBackend)
- 3 Szenarien:
  1. First call valid → no retry, event logged as `ok`, result returned.
  2. First call bad_json, second call valid → one retry event + one ok event, result returned.
  3. Both calls bad → hard_fail event, returns Err.
- Assert event file contains expected entries after each.

### Benchmark (`src/benchmark.rs`-Tests)
- `run_benchmark` mit MockBackend das pro Case eine scripted Response liefert, assert `BenchmarkReport` zählt korrekt.

## 13. Phased Delivery

| Phase | Inhalt | Schätzung |
|---|---|---|
| **A** | `guardrail.rs` mit Items 1-6, Retry-Loop, NDJSON-Event-Log, Mini-Card im Config-Tab, Integration in Wizard + Chat-Stream + Cycle | 2 Tage |
| **B** | Quality-Tab mit Aggregate-Card + Backend-Tabelle + Top-Fehler + Event-Liste (keine Charts) | 1 Tag |
| **C** | Benchmark-Runner mit Standard-Suite, Benchmark-UI, `strict_mode`-Toggle für Item 7 | 1-2 Tage |

Jede Phase unabhängig mergebar, Phase A hat sofortigen Value ohne B/C.

## 14. Offene Mikro-Entscheidungen (bereits aufgelöst)

- **Aggregate-Rekonstruktion beim Startup:** Lese letzte 7 Tage Logs (= 7 Files), fold in-memory. Kostet beim Start 1-2 Sek bei 10k Events.
- **Event-Log-Dateigröße:** ~500 bytes/Event × 10k/Tag = 5 MB/Tag. `log_retention_days=30` → max 150 MB. Akzeptabel.
- **Levenshtein-Library:** Hand-gerollt 30 LOC statt externer Crate (wie base64_url_encode in wizard.rs).
- **Event-ID:** kein UUID; `ts + modul + retry_attempt` ist für Debug-Log ausreichend.
