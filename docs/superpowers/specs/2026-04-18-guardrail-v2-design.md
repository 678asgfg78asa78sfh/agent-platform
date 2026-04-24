# Guardrail V2 — Alerts, Backup-Fallback, A/B-Benchmark, Strict-Verify

**Datum:** 2026-04-18
**Status:** Approved
**Baut auf:** `2026-04-18-llm-guardrail-and-quality-design.md` (V1)

---

## 1. Goal

Vier additive Erweiterungen die das Guardrail-System von passiv (schreibt Events) zu **aktiv-reagierend** (alert + fallback), **vergleichend** (A/B-Benchmark) und **konfigurierbar** (Strict-Mode-Triggers) machen.

**Kern-Prinzipien:**
- Keine neuen Crates. Keine Schema-Migration. Keine Kern-Refactors.
- Jedes Feature hat globalen Toggle + sensible Defaults. Default-Verhalten bleibt V1-kompatibel.
- Notify nutzt existierendes `notify.send` Tool (kein eigenes Messaging).
- A/B nutzt existierendes `run_benchmark` zweimal parallel.
- Backup nutzt existierendes `backup_llm` Feld in `ModulConfig`.

## 2. Scope-Entscheidungen (User-confirmed)

| # | Entscheidung |
|---|---|
| 1 | Alle 4 Features in einer Spec, 4 Phasen (V2-A bis V2-D), jede unabhängig mergebar |
| 2 | Alerting ist pull-basiert (Task poll'd Stats alle 5min) — kein Event-Push |
| 3 | Alerting nutzt `notify.send` via einen konfigurierten `notify_backend_id` |
| 4 | A/B parallel via `tokio::join!` — nicht sequentiell |
| 5 | Backup-Fallback nur auf hard_fail, nicht auf einzelne Retries |
| 6 | Strict-Triggers werden config-driven (Vec<String>), bestehende Hardcoded-Liste ist Fallback-Default |

## 3. Non-Goals

- Keine Multi-Channel Alerts (nur der eine `notify.send`, Kanal-Config liegt im Notify-Modul)
- Kein Alert-History-Endpoint (Alert-Events landen im normalen NDJSON-Log, sind dort filterbar)
- Kein persistenter Cooldown (in-memory reicht — Restart = frischer Alert ist erwünscht)
- Keine semantische Vergleichs-Analyse bei A/B (nur Pass/Fail pro Case + Gesamt-Pass-Rate)
- Kein LLM-as-Judge zur Strict-Mode-Evaluation (bleibt rein regel-basiert, User-editierbar)

## 4. Architektur-Überblick

```
V2-A Alerting:
  main.rs spawn ──► alert_loop every 5min
                     │
                     ▼
             check_alert_threshold(cfg, data_root)
                     │
                     ▼
           [for each backend+model breaching threshold]
                     │
                     ▼
           exec_tool_unified(notify.send, ...) via notify_backend_id
                     │
                     ▼
             log_alert_event() ─► NDJSON event log

V2-B A/B-Compare:
  POST /api/quality/benchmark/compare {backend_a, backend_b, modul_id?}
                     │
                     ▼
             tokio::join!(run_benchmark(A), run_benchmark(B))
                     │
                     ▼
             Merge into CompareReport + stream events with side="A"|"B"

V2-C Strict-Triggers config:
  validate_response() reads cfg.guardrail.strict_triggers (fallback to const list)

V2-D Backup-Fallback:
  In wizard/chat/cycle retry-loops:
    on hard_fail & modul.backup_llm set & cfg.fallback_on_hard_fail:
      log_fallback_event()
      switch backend_id → backup_llm
      reset guardrail_retries = 0
      continue loop (one extra fallback attempt)
```

## 5. Files & Änderungen

**Modifiziert:**
- `src/types.rs` — Add `GuardrailAlertConfig`, extend `GuardrailConfig` with `alert`, `strict_triggers`, `fallback_on_hard_fail`. Add `GuardrailAlertEvent` + `GuardrailFallbackEvent` types (both serializable to NDJSON).
- `src/guardrail.rs` — Add `check_alert_threshold`, `log_alert_event`, `log_fallback_event`. Make `STRICT_TRIGGERS` reader use config if present.
- `src/benchmark.rs` — Add `run_compare`, `CompareReport` type.
- `src/web.rs` — Add `POST /api/quality/benchmark/compare` endpoint.
- `src/main.rs` — Spawn alert-loop task (every 5min).
- `src/wizard.rs` / `src/web.rs::chat` / `src/cycle.rs` — Modify retry-loop hard-fail branch: check backup_llm + fallback flag, do one extra attempt with backup backend, log fallback event.
- `src/frontend.html` — Config tab Quality-Alerts subsection, Quality tab A/B-Compare subsection, strict-triggers textarea editor, alert-events filter.

**Neu:**
- Nothing. All feature code fits into existing modules.

## 6. Data Model

```rust
// src/types.rs

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GuardrailAlertConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_alert_threshold")]
    pub threshold_valid_pct: u32,         // e.g. 70 — alert when below
    #[serde(default = "default_alert_min_calls")]
    pub min_calls_window: u64,            // need this many calls before judging
    #[serde(default = "default_alert_window_mins")]
    pub window_minutes: u64,              // rolling window
    #[serde(default = "default_alert_cooldown_mins")]
    pub cooldown_minutes: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub notify_backend_id: Option<String>, // which modul to route notify.send through
}

fn default_alert_threshold() -> u32 { 70 }
fn default_alert_min_calls() -> u64 { 20 }
fn default_alert_window_mins() -> u64 { 30 }
fn default_alert_cooldown_mins() -> u64 { 60 }

// Extend existing GuardrailConfig:
pub struct GuardrailConfig {
    // ... existing fields ...
    #[serde(default)]
    pub alert: GuardrailAlertConfig,
    #[serde(default)]
    pub strict_triggers: Vec<String>,       // empty = use hardcoded fallback
    #[serde(default = "default_true")]
    pub fallback_on_hard_fail: bool,
}

// Alert event (written to same NDJSON log with distinct outcome)
// Reuses GuardrailEvent shape with final_outcome = "alert_fired"
// and tool_name = Some(format!("{}/{}", backend, model))
// and errors[0].code = "quality_threshold_breached" — no new struct needed.

// Fallback event: same NDJSON, final_outcome = "fallback_triggered"
// errors[0].code = "hard_fail_with_backup_used"
```

## 7. V2-A: Alerting

### 7.1 check_alert_threshold

```rust
pub async fn check_alert_threshold(
    cfg: &GuardrailConfig,
    data_root: &Path,
    cooldown_map: &Mutex<HashMap<(String, String), i64>>,
) -> Vec<(String, String, f32, u64)> {
    // Returns (backend, model, valid_pct, sample_size) for pairs that breached.
    if !cfg.alert.enabled { return Vec::new(); }
    let window_hours_approx = (cfg.alert.window_minutes as u32).div_ceil(60).max(1);
    let stats = compute_stats(data_root, window_hours_approx).await;
    // Time-bound filter: compute_stats uses hours; for minutes we load events_since(now - window_minutes*60)
    let since = chrono::Utc::now().timestamp() - (cfg.alert.window_minutes as i64) * 60;
    let events = load_events_since(data_root, since, 100_000, None, false).await;

    let mut per_key: HashMap<(String, String), (u64, u64)> = HashMap::new();
    for e in events {
        let k = (e.backend.clone(), e.model.clone());
        let entry = per_key.entry(k).or_insert((0, 0));
        entry.0 += 1;
        if e.passed { entry.1 += 1; }
    }

    let now = chrono::Utc::now().timestamp();
    let cooldown_secs = (cfg.alert.cooldown_minutes as i64) * 60;
    let mut cd = cooldown_map.lock().await;
    let mut fired = Vec::new();

    for ((backend, model), (total, valid)) in per_key {
        if total < cfg.alert.min_calls_window { continue; }
        let pct = (valid as f32 / total as f32) * 100.0;
        if pct as u32 >= cfg.alert.threshold_valid_pct { continue; }
        let k = (backend.clone(), model.clone());
        let last = cd.get(&k).copied().unwrap_or(0);
        if now - last < cooldown_secs { continue; }
        cd.insert(k, now);
        fired.push((backend, model, pct, total));
    }
    fired
}
```

### 7.2 Alert-Task in main.rs

```rust
// After guardrail init:
let cfg_ref = config.clone();
let data_root_clone = base_dir.clone();
let web_state_clone = web_state.clone();
let cooldown_map: Arc<Mutex<HashMap<(String, String), i64>>> = Arc::new(Mutex::new(HashMap::new()));
tokio::spawn(async move {
    let mut tick = tokio::time::interval(std::time::Duration::from_secs(300));
    loop {
        tick.tick().await;
        let gcfg = cfg_ref.read().await.guardrail.clone().unwrap_or_default();
        if !gcfg.alert.enabled { continue; }
        let breaches = guardrail::check_alert_threshold(&gcfg, &data_root_clone, &cooldown_map).await;
        for (backend, model, pct, n) in breaches {
            let msg = format!("Guardrail Alert: {} / {} quality dropped to {:.1}% (over last {} min, {} calls)",
                              backend, model, pct, gcfg.alert.window_minutes, n);
            let notify_modul = match gcfg.alert.notify_backend_id.as_deref() {
                Some(m) => m,
                None => {
                    tracing::warn!("Guardrail alert: no notify_backend_id configured, skipping send: {}", msg);
                    continue;
                }
            };
            let params = vec![msg.clone()];
            let py_mods = web_state_clone.py_modules.read().await.clone();
            let cfg_snap = cfg_ref.read().await.clone();
            let (ok, detail) = crate::tools::exec_tool_unified(
                "notify.send", &params, notify_modul,
                &web_state_clone.pipeline, &web_state_clone.llm,
                &py_mods, &web_state_clone.py_pool, &cfg_snap,
            ).await;
            tracing::info!("Guardrail alert notify: ok={} {}", ok, detail);
            guardrail::log_alert_event(&data_root_clone, &backend, &model, pct, n).await.ok();
        }
    }
});
```

### 7.3 log_alert_event

```rust
pub async fn log_alert_event(
    data_root: &Path,
    backend: &str,
    model: &str,
    valid_pct: f32,
    sample_size: u64,
) -> std::io::Result<()> {
    let ev = GuardrailEvent {
        ts: chrono::Utc::now().timestamp(),
        modul: "__alert__".into(),
        backend: backend.into(),
        model: model.into(),
        tool_name: None,
        passed: false,
        errors: vec![ValidationError {
            field: format!("{}/{}", backend, model),
            code: "quality_threshold_breached".into(),
            human_message_de: format!("Valid-Rate {:.1}% bei {} Calls unter Threshold", valid_pct, sample_size),
        }],
        retry_attempt: 0,
        final_outcome: "alert_fired".into(),
        similar_suggestion: None,
    };
    log_event(data_root, &ev).await
}
```

## 8. V2-B: A/B-Benchmark

### 8.1 `run_compare` in benchmark.rs

```rust
#[derive(serde::Serialize)]
pub struct CompareReport {
    pub report_a: BenchmarkReport,
    pub report_b: BenchmarkReport,
    pub winner_per_case: Vec<(String, String)>,  // (case_id, "A" | "B" | "tie")
}

#[derive(serde::Serialize, Clone)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum CompareEvent {
    SideStart { side: String, backend: String, model: String },
    SideCaseResult { side: String, result: BenchmarkResult },
    Report { report: CompareReport },
    Error { message: String },
}

pub async fn run_compare(
    backend_a: LlmBackend, backend_b: LlmBackend,
    run_modul_id: String, cfg_snapshot: AgentConfig,
    py_modules: Vec<crate::loader::PyModuleMeta>,
    llm: Arc<crate::llm::LlmRouter>,
    tx: mpsc::Sender<CompareEvent>,
) {
    let (tx_a, mut rx_a) = mpsc::channel::<BenchmarkEvent>(64);
    let (tx_b, mut rx_b) = mpsc::channel::<BenchmarkEvent>(64);

    let a_future = run_benchmark(backend_a.clone(), run_modul_id.clone(), cfg_snapshot.clone(), py_modules.clone(), llm.clone(), tx_a);
    let b_future = run_benchmark(backend_b.clone(), run_modul_id.clone(), cfg_snapshot.clone(), py_modules.clone(), llm.clone(), tx_b);

    let tx_events = tx.clone();
    let collect_a = async {
        let mut report: Option<BenchmarkReport> = None;
        while let Some(ev) = rx_a.recv().await {
            match ev {
                BenchmarkEvent::CaseResult { result } => {
                    let _ = tx_events.send(CompareEvent::SideCaseResult { side: "A".into(), result }).await;
                }
                BenchmarkEvent::Report { report: r } => { report = Some(r); }
                _ => {}
            }
        }
        report
    };
    let tx_events_b = tx.clone();
    let collect_b = async {
        let mut report: Option<BenchmarkReport> = None;
        while let Some(ev) = rx_b.recv().await {
            match ev {
                BenchmarkEvent::CaseResult { result } => {
                    let _ = tx_events_b.send(CompareEvent::SideCaseResult { side: "B".into(), result }).await;
                }
                BenchmarkEvent::Report { report: r } => { report = Some(r); }
                _ => {}
            }
        }
        report
    };

    let _ = tx.send(CompareEvent::SideStart { side: "A".into(), backend: backend_a.id.clone(), model: backend_a.model.clone() }).await;
    let _ = tx.send(CompareEvent::SideStart { side: "B".into(), backend: backend_b.id.clone(), model: backend_b.model.clone() }).await;

    let ((), (), report_a_opt, report_b_opt) = tokio::join!(a_future, b_future, collect_a, collect_b);

    match (report_a_opt, report_b_opt) {
        (Some(a), Some(b)) => {
            let mut winners = Vec::new();
            for ra in &a.results {
                if let Some(rb) = b.results.iter().find(|x| x.case_id == ra.case_id) {
                    let w = match (ra.passed, rb.passed) {
                        (true, false) => "A",
                        (false, true) => "B",
                        _ => "tie",
                    };
                    winners.push((ra.case_id.clone(), w.to_string()));
                }
            }
            let report = CompareReport { report_a: a, report_b: b, winner_per_case: winners };
            let _ = tx.send(CompareEvent::Report { report }).await;
        }
        _ => {
            let _ = tx.send(CompareEvent::Error { message: "one or both benchmark reports missing".into() }).await;
        }
    }
}
```

### 8.2 `POST /api/quality/benchmark/compare`

Analog zu `quality_benchmark_run`, streamt `CompareEvent` als NDJSON.

### 8.3 UI

Im Quality-Tab unter dem Benchmark-Block:
```html
<h4 style="margin-top:16px;">A/B Compare</h4>
<div style="display:flex;gap:8px;">
  <select id="ab-backend-a"></select>
  <select id="ab-backend-b"></select>
  <button onclick="runCompare()">Compare</button>
</div>
<div id="ab-output" style="font-family:monospace;font-size:12px;..."></div>
```

JS streamt NDJSON, rendert zwei Spalten (A | B) mit Pass-Rate-Badges und pro Case den Winner.

## 9. V2-C: Strict-Mode Verify + Triggers

**Code-Änderung minimal:**
- `STRICT_TRIGGERS` wird von Konstante zu Funktion: `fn strict_triggers(cfg: &GuardrailConfig) -> Vec<&str>`. Wenn `cfg.strict_triggers` nicht leer → nutze das, sonst fallback auf Hardcoded-Liste.
- Im `validate_response` statt direktem Zugriff auf die Const, Call zur Helper-Funktion.

**UI:**
- Config-Tab bekommt Textarea (ein Trigger pro Zeile) neben dem strict_mode-Checkbox.
- Wird mit existierendem `populateGuardrailSettings` / `collectGuardrailSettings` verdrahtet (Feld `strict_triggers` als `string.split('\n').filter(Boolean)`).

**Manuelle Verifikation:**
- User hat lokales Ollama-Setup (gemma4). In einer neuen Wizard-Session wird strict_mode aktiviert, dann 10 gezielte Prompts gesendet (5 positive wo Tool erwartet, 5 negative wo Text reicht). False-Positive-Rate dokumentiert.
- Falls FP zu hoch → einzelne Trigger aus der Liste entfernen. **Keine Code-Änderung nötig**, weil Triggers jetzt config-driven sind.

## 10. V2-D: Backup-LLM-Fallback

### 10.1 Mechanik

In den drei Retry-Loops (wizard/chat/cycle) wird die hard-fail-Stelle erweitert:

```rust
// Before (current):
if is_last {
    // emit error + return
}

// After (new):
if is_last {
    // Check if backup_llm available + fallback flag on
    let backup_id = cfg_snap.module.iter()
        .find(|m| m.id == modul_id)
        .and_then(|m| m.backup_llm.clone());
    if gcfg.fallback_on_hard_fail && backup_id.is_some() && !used_fallback {
        let backup_id = backup_id.unwrap();
        let backup_backend = cfg_snap.llm_backends.iter().find(|b| b.id == backup_id).cloned();
        if let Some(bb) = backup_backend {
            // Log fallback event
            guardrail::log_fallback_event(&data_root, &backend_id, &backup_id, &modul_id, &errors.iter().map(|e| e.code.clone()).collect::<Vec<_>>()).await.ok();
            // Switch backend
            backend_id = bb.id.clone();
            model = bb.model.clone();
            used_fallback = true;
            guardrail_retries = 0;
            continue;   // retry with backup
        }
    }
    // Real hard-fail — emit error + return
}
```

`used_fallback: bool` wird vor dem Loop auf `false` initialisiert. So wird genau ein Fallback-Attempt erlaubt; scheitert auch das, ist's ein echter Hard-Fail.

**Wizard-Variante:** statt `backend_id` muss die gesamte `backend` (Arc<dyn WizardBackend>) neu gebaut werden aus der backup-Config. Das geht via `RealWizardBackend::new(router, backup_backend)`. Da `backend: &dyn WizardBackend` im run_turn const-referenziert ist, muss signature-mässig oder mit shadowing gearbeitet werden. Pragmatisch: neue lokale Variable `let mut active_backend: Box<dyn WizardBackend + ...>` die am Loop-Anfang gesetzt wird, am Fallback-Punkt getauscht.

### 10.2 log_fallback_event

```rust
pub async fn log_fallback_event(
    data_root: &Path,
    original: &str, fallback: &str,
    modul: &str, codes: &[String],
) -> std::io::Result<()> {
    let ev = GuardrailEvent {
        ts: chrono::Utc::now().timestamp(),
        modul: modul.into(),
        backend: original.into(),
        model: fallback.into(),   // repurposed: "this is the backup that took over"
        tool_name: None,
        passed: false,
        errors: codes.iter().map(|c| ValidationError {
            field: "backend".into(), code: c.clone(),
            human_message_de: format!("Hard-fail, fallback auf {}", fallback),
        }).collect(),
        retry_attempt: 0,
        final_outcome: "fallback_triggered".into(),
        similar_suggestion: Some(fallback.into()),
    };
    log_event(data_root, &ev).await
}
```

### 10.3 UI-Markierung

Im Quality-Tab Event-Liste: Events mit `final_outcome = "fallback_triggered"` werden mit `🔄` Präfix (tatsächliches UI-Element, nicht dekorativ) oder Text `FALLBACK` gerendert. Events mit `final_outcome = "alert_fired"` mit `ALERT`.

Filter-Dropdown im Quality-Tab: "Alle | Nur Fehler | Nur Alerts | Nur Fallbacks".

## 11. Security

- Alert-Task hat keinen direkten HTTP-Zugriff; läuft als tokio::spawn intern.
- `notify.send` Aufruf geht durch existierende `tools::exec_tool_unified` → vollständige Permission-/Path-/SSRF-Prüfungen inklusive.
- Fallback-Backend muss bereits in `config.llm_backends` registriert sein (same security posture als primary).
- Strict-Triggers kommen aus Admin-UI Config — User mit Write-Access kann sie ändern; kein zusätzliches Attack-Surface.
- A/B-Benchmark respektiert bestehende Rate-Limits auf `/api/quality/*`.

## 12. Testing

**Unit (`src/guardrail.rs`):**
- `check_alert_threshold`: 6 Szenarien (unter Threshold + ok cooldown → alert; unter + cooldown aktiv → kein alert; über Threshold → kein; <min_calls → kein; mehrere Backends → nur betroffene; cooldown-map update korrekt).
- `log_alert_event` + `log_fallback_event`: roundtrip-Test (write + re-load).
- `strict_triggers(cfg)` mit konfigurierten vs leeren Triggern.

**Unit (`src/benchmark.rs`):**
- `run_compare` mit Mock-Backends: A passes 10/20, B passes 15/20 → CompareReport hat 15 winners=B, 5 winners=A/tie.

**Integration:** 
- Backup-Fallback-Pfad: Mock-Backend-A fails validation 3×, backup B returns valid. Verifiziert Fallback-Event gelogged und zweites backend erfolgreich.

**Manuelle Verifikation:** 
- Strict-Mode gegen Ollama gemma4: 20 prompts, FP/FN-Rate dokumentiert (erwartet <15% FP).

## 13. Phased Delivery

| Phase | Inhalt | Schätzung |
|---|---|---|
| **V2-A** | Alerting: Config + check_alert_threshold + main.rs-task + log_alert_event + Config-UI "Quality Alerts" + Filter "Nur Alerts" | 1 Tag |
| **V2-B** | A/B-Benchmark: run_compare + CompareEvent + `/api/quality/benchmark/compare` + Quality-Tab A/B-UI | 1 Tag |
| **V2-C** | Strict-Triggers config-driven + Textarea im Config-Tab + manueller Ollama-Test + Dokumentation der FP-Rate | 0.5 Tag |
| **V2-D** | Backup-Fallback in wizard/chat/cycle + log_fallback_event + UI-Filter "Nur Fallbacks" | 1 Tag |

Jede Phase eigene Commits, kann unabhängig gemergt werden.

## 14. Aufgelöste Detail-Entscheidungen

- **Cooldown-Persistence:** in-memory HashMap (restart = alert-reset). Absichtlich, weil nach Restart der Operator sowieso hinguckt.
- **Min-Sample-Size:** Default 20. Bei kleinerer Nutzung (z.B. 5 calls/tag) würde nie gealertet — ist OK; User kann auf 5 runter setzen.
- **Alert-Nachrichten-Format:** Fixed template in Rust, kein Template-System nötig.
- **A/B-Parallelität vs Sequentiell:** Parallel. Rechtfertigung: Benchmarks sind unabhängig; serialisieren würde Tests 2× so lang machen ohne Genauigkeits-Gewinn.
- **Strict-Triggers-UI:** Textarea mit "ein Trigger pro Zeile" ist UX-am-einfachsten (statt Tag-Chips).
- **Fallback-Loop-Guard:** Ein `used_fallback: bool`. Zwei-Backup-Chain ist Out-of-Scope.
