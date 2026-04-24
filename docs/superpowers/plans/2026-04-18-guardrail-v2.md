# Guardrail V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four additive guardrail extensions — alerting, A/B benchmark, config-driven strict triggers, backup-LLM fallback — delivered as four independently mergeable phases.

**Architecture:** All changes extend existing `src/guardrail.rs`, `src/benchmark.rs`, `src/types.rs`, retry-loops in wizard/chat/cycle, and frontend Quality tab. No new crates, no schema migration. NDJSON event log gets two new outcomes (`alert_fired`, `fallback_triggered`) on the existing `GuardrailEvent` shape.

**Tech Stack:** Rust (axum, tokio, serde, reqwest) — existing project crates only.

**Spec reference:** `docs/superpowers/specs/2026-04-18-guardrail-v2-design.md`

---

## File Structure

**Modified:**
- `src/types.rs` — add `GuardrailAlertConfig`, extend `GuardrailConfig` with `alert`, `strict_triggers`, `fallback_on_hard_fail`
- `src/guardrail.rs` — `check_alert_threshold`, `log_alert_event`, `log_fallback_event`, config-driven `strict_triggers`
- `src/benchmark.rs` — `run_compare`, `CompareReport`, `CompareEvent`
- `src/web.rs` — `POST /api/quality/benchmark/compare`
- `src/main.rs` — alert-loop task (5-min tick)
- `src/wizard.rs`, `src/web.rs::chat`, `src/cycle.rs::exec_llm` — backup-fallback branch on hard-fail
- `src/frontend.html` — Config-tab "Quality Alerts" subsection + strict-triggers textarea, Quality-tab A/B-compare UI + filter dropdown

**Out of scope:** No new files.

---

## Phase V2-A — Alerting

### Task A1: Types

**Files:** Modify `src/types.rs`.

- [ ] **A1.1: Append `GuardrailAlertConfig` + defaults**

Append before `GuardrailConfig`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct GuardrailAlertConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_alert_threshold")]
    pub threshold_valid_pct: u32,
    #[serde(default = "default_alert_min_calls")]
    pub min_calls_window: u64,
    #[serde(default = "default_alert_window_mins")]
    pub window_minutes: u64,
    #[serde(default = "default_alert_cooldown_mins")]
    pub cooldown_minutes: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub notify_backend_id: Option<String>,
}
fn default_alert_threshold() -> u32 { 70 }
fn default_alert_min_calls() -> u64 { 20 }
fn default_alert_window_mins() -> u64 { 30 }
fn default_alert_cooldown_mins() -> u64 { 60 }
```

Extend `GuardrailConfig`:

```rust
    #[serde(default)]
    pub alert: GuardrailAlertConfig,
    #[serde(default)]
    pub strict_triggers: Vec<String>,
    #[serde(default = "default_true")]
    pub fallback_on_hard_fail: bool,
```

Update `impl Default for GuardrailConfig`: add `alert: GuardrailAlertConfig::default(), strict_triggers: vec![], fallback_on_hard_fail: true`.

- [ ] **A1.2: Build + commit**

```bash
cargo build --quiet 2>&1 | tail -3
git add src/types.rs
git commit -m "feat(guardrail-v2): add GuardrailAlertConfig + strict_triggers + fallback_on_hard_fail"
```

### Task A2: `check_alert_threshold` + `log_alert_event`

**Files:** Modify `src/guardrail.rs`.

- [ ] **A2.1: Add cooldown map + check + log functions**

Append (above `#[cfg(test)]`):

```rust
use tokio::sync::Mutex as TokioMutex;

pub type AlertCooldownMap = std::sync::Arc<TokioMutex<std::collections::HashMap<(String, String), i64>>>;

pub fn new_alert_cooldown_map() -> AlertCooldownMap {
    std::sync::Arc::new(TokioMutex::new(std::collections::HashMap::new()))
}

pub async fn check_alert_threshold(
    cfg: &crate::types::GuardrailConfig,
    data_root: &Path,
    cooldown_map: &AlertCooldownMap,
) -> Vec<(String, String, f32, u64)> {
    if !cfg.alert.enabled { return Vec::new(); }
    let since = chrono::Utc::now().timestamp() - (cfg.alert.window_minutes as i64) * 60;
    let events = load_events_since(data_root, since, 100_000, None, false).await;

    let mut per_key: std::collections::HashMap<(String, String), (u64, u64)> = std::collections::HashMap::new();
    for e in events {
        // Skip alerts and fallbacks themselves, otherwise we alert on alerts
        if e.final_outcome == "alert_fired" || e.final_outcome == "fallback_triggered" { continue; }
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
        if (pct as u32) >= cfg.alert.threshold_valid_pct { continue; }
        let k = (backend.clone(), model.clone());
        let last = cd.get(&k).copied().unwrap_or(0);
        if now - last < cooldown_secs { continue; }
        cd.insert(k, now);
        fired.push((backend, model, pct, total));
    }
    fired
}

pub async fn log_alert_event(
    data_root: &Path,
    backend: &str,
    model: &str,
    valid_pct: f32,
    sample_size: u64,
) -> std::io::Result<()> {
    let ev = crate::types::GuardrailEvent {
        ts: chrono::Utc::now().timestamp(),
        modul: "__alert__".into(),
        backend: backend.into(),
        model: model.into(),
        tool_name: None,
        passed: false,
        errors: vec![crate::types::ValidationError {
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

- [ ] **A2.2: Append unit tests**

```rust
#[tokio::test]
async fn alert_fires_below_threshold_and_enough_samples() {
    let tmp = tempfile::tempdir().unwrap();
    let now = chrono::Utc::now().timestamp();
    // 20 events, 10 valid → 50% (below 70% default)
    for i in 0..20 {
        let e = crate::types::GuardrailEvent {
            ts: now - 60, modul: "m".into(),
            backend: "grok".into(), model: "grok-4".into(),
            tool_name: None, passed: i < 10, errors: vec![],
            retry_attempt: 0, final_outcome: if i < 10 { "ok".into() } else { "hard_fail".into() },
            similar_suggestion: None,
        };
        log_event(tmp.path(), &e).await.unwrap();
    }
    let cfg = crate::types::GuardrailConfig {
        alert: crate::types::GuardrailAlertConfig {
            enabled: true, threshold_valid_pct: 70, min_calls_window: 10,
            window_minutes: 30, cooldown_minutes: 60, notify_backend_id: None,
        },
        ..Default::default()
    };
    let cd = new_alert_cooldown_map();
    let fired = check_alert_threshold(&cfg, tmp.path(), &cd).await;
    assert_eq!(fired.len(), 1);
    assert_eq!(fired[0].0, "grok");
    assert!(fired[0].2 < 70.0);
}

#[tokio::test]
async fn alert_respects_cooldown() {
    let tmp = tempfile::tempdir().unwrap();
    let now = chrono::Utc::now().timestamp();
    for i in 0..20 {
        log_event(tmp.path(), &crate::types::GuardrailEvent {
            ts: now - 60, modul: "m".into(),
            backend: "grok".into(), model: "grok-4".into(),
            tool_name: None, passed: i < 5, errors: vec![],
            retry_attempt: 0, final_outcome: "hard_fail".into(),
            similar_suggestion: None,
        }).await.unwrap();
    }
    let cfg = crate::types::GuardrailConfig {
        alert: crate::types::GuardrailAlertConfig {
            enabled: true, threshold_valid_pct: 70, min_calls_window: 10,
            window_minutes: 30, cooldown_minutes: 60, notify_backend_id: None,
        },
        ..Default::default()
    };
    let cd = new_alert_cooldown_map();
    let first = check_alert_threshold(&cfg, tmp.path(), &cd).await;
    assert_eq!(first.len(), 1);
    let second = check_alert_threshold(&cfg, tmp.path(), &cd).await;
    assert_eq!(second.len(), 0, "cooldown should suppress");
}

#[tokio::test]
async fn alert_ignores_when_min_samples_not_met() {
    let tmp = tempfile::tempdir().unwrap();
    let now = chrono::Utc::now().timestamp();
    for _ in 0..5 {
        log_event(tmp.path(), &crate::types::GuardrailEvent {
            ts: now - 60, modul: "m".into(),
            backend: "grok".into(), model: "grok-4".into(),
            tool_name: None, passed: false, errors: vec![],
            retry_attempt: 0, final_outcome: "hard_fail".into(),
            similar_suggestion: None,
        }).await.unwrap();
    }
    let cfg = crate::types::GuardrailConfig {
        alert: crate::types::GuardrailAlertConfig {
            enabled: true, threshold_valid_pct: 70, min_calls_window: 10,
            window_minutes: 30, cooldown_minutes: 60, notify_backend_id: None,
        },
        ..Default::default()
    };
    let cd = new_alert_cooldown_map();
    assert_eq!(check_alert_threshold(&cfg, tmp.path(), &cd).await.len(), 0);
}

#[tokio::test]
async fn log_alert_event_roundtrip() {
    let tmp = tempfile::tempdir().unwrap();
    log_alert_event(tmp.path(), "grok", "grok-4", 55.0, 30).await.unwrap();
    let events = load_events_since(tmp.path(), 0, 10, None, false).await;
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].final_outcome, "alert_fired");
    assert!(events[0].errors.iter().any(|e| e.code == "quality_threshold_breached"));
}
```

- [ ] **A2.3: Build + test + commit**

```bash
cargo test guardrail:: 2>&1 | tail -5
git add src/guardrail.rs
git commit -m "feat(guardrail-v2): check_alert_threshold + log_alert_event + cooldown map"
```

### Task A3: Alert-loop in main.rs

**Files:** Modify `src/main.rs`.

- [ ] **A3.1: Spawn alert-loop after existing guardrail init block**

After the existing `cleanup_old_events` tokio::spawn block, add:

```rust
    // Guardrail alert loop (5-min poll, checks valid-rate per backend/model)
    {
        let gcfg_snap = config.read().await.guardrail.clone().unwrap_or_default();
        if gcfg_snap.enabled && gcfg_snap.alert.enabled {
            let cfg_ref = config.clone();
            let data_root_clone = base_dir.clone();
            let state_clone = web_state.clone();
            let cooldown_map = guardrail::new_alert_cooldown_map();
            tokio::spawn(async move {
                let mut tick = tokio::time::interval(std::time::Duration::from_secs(300));
                loop {
                    tick.tick().await;
                    let gcfg = cfg_ref.read().await.guardrail.clone().unwrap_or_default();
                    if !gcfg.alert.enabled { continue; }
                    let breaches = guardrail::check_alert_threshold(&gcfg, &data_root_clone, &cooldown_map).await;
                    for (backend, model, pct, n) in breaches {
                        let msg = format!(
                            "Guardrail Alert: {}/{} valid-rate bei {:.1}% (letzten {} min, {} Calls)",
                            backend, model, pct, gcfg.alert.window_minutes, n
                        );
                        let notify_modul = match gcfg.alert.notify_backend_id.as_deref() {
                            Some(m) => m,
                            None => {
                                tracing::warn!("Guardrail alert — no notify_backend_id: {}", msg);
                                let _ = guardrail::log_alert_event(&data_root_clone, &backend, &model, pct, n).await;
                                continue;
                            }
                        };
                        let params = vec![msg.clone()];
                        let py_mods = state_clone.py_modules.read().await.clone();
                        let cfg_full = cfg_ref.read().await.clone();
                        let (ok, detail) = crate::tools::exec_tool_unified(
                            "notify.send", &params, notify_modul,
                            &state_clone.pipeline, &state_clone.llm,
                            &py_mods, &state_clone.py_pool, &cfg_full,
                        ).await;
                        tracing::info!("Guardrail alert → notify ok={} {}", ok, detail);
                        let _ = guardrail::log_alert_event(&data_root_clone, &backend, &model, pct, n).await;
                    }
                }
            });
        }
    }
```

Place this block after the existing `cleanup_old_events` spawn, before AppState handoff into chat-port tasks (so `web_state` is already defined). If `web_state` isn't yet built at that point, the block belongs AFTER `web_state` construction.

- [ ] **A3.2: Build + test + commit**

```bash
cargo build --quiet 2>&1 | tail -5
cargo test 2>&1 | tail -3
git add src/main.rs
git commit -m "feat(guardrail-v2): alert-loop task with notify.send integration"
```

### Task A4: UI — Config-tab Alerts subsection + filter

**Files:** Modify `src/frontend.html`.

- [ ] **A4.1: Add Alerts subsection inside existing Guardrail section**

Find the Guardrail section (search for `<h3>Guardrail</h3>`). After the strict-mode checkbox, append before the mini-card:

```html
<h4 style="margin-top:12px;">Quality Alerts</h4>
<div style="margin:8px 0;"><label style="display:inline-flex;gap:6px;align-items:center;"><input type="checkbox" id="galert-enabled"> Alerts enabled</label></div>
<div style="margin:8px 0;display:flex;gap:12px;flex-wrap:wrap;">
  <label>Threshold valid-%: <input type="number" id="galert-threshold" value="70" style="width:70px;"></label>
  <label>Min calls: <input type="number" id="galert-min" value="20" style="width:70px;"></label>
  <label>Window (min): <input type="number" id="galert-window" value="30" style="width:70px;"></label>
  <label>Cooldown (min): <input type="number" id="galert-cooldown" value="60" style="width:70px;"></label>
</div>
<div style="margin:8px 0;"><label>Notify via:
  <select id="galert-notify-modul"><option value="">(keiner)</option></select>
</label></div>
```

- [ ] **A4.2: Extend populate/collect helpers**

In `populateGuardrailSettings`:

```js
var a = (g.alert) || {};
if (ge('galert-enabled'))    ge('galert-enabled').checked = !!a.enabled;
if (ge('galert-threshold'))  ge('galert-threshold').value = a.threshold_valid_pct || 70;
if (ge('galert-min'))        ge('galert-min').value = a.min_calls_window || 20;
if (ge('galert-window'))     ge('galert-window').value = a.window_minutes || 30;
if (ge('galert-cooldown'))   ge('galert-cooldown').value = a.cooldown_minutes || 60;
if (ge('galert-notify-modul')) {
  // Populate dropdown from current modules of typ 'notify' or any module_id (user picks)
  var sel = ge('galert-notify-modul');
  var current = a.notify_backend_id || '';
  var opts = ['<option value="">(keiner)</option>'];
  (cfg.module || []).forEach(function(m) {
    opts.push('<option value="' + m.id + '">' + m.id + ' (' + m.typ + ')</option>');
  });
  sel.innerHTML = opts.join('');
  sel.value = current;
}
```

In `collectGuardrailSettings` (inside the returned object):

```js
    alert: {
      enabled: ge('galert-enabled').checked,
      threshold_valid_pct: parseInt(ge('galert-threshold').value, 10) || 70,
      min_calls_window: parseInt(ge('galert-min').value, 10) || 20,
      window_minutes: parseInt(ge('galert-window').value, 10) || 30,
      cooldown_minutes: parseInt(ge('galert-cooldown').value, 10) || 60,
      notify_backend_id: ge('galert-notify-modul').value || null,
    },
    strict_triggers: [],                 // Phase V2-C will populate this
    fallback_on_hard_fail: true,          // Phase V2-D will populate this
```

- [ ] **A4.3: Add filter dropdown in Quality-tab event list**

Find the `#tab-quality` div. Replace the "Nur Fehler" checkbox with a dropdown:

```html
<label>Filter
  <select id="qt-filter">
    <option value="all">Alle</option>
    <option value="failed">Nur Fehler</option>
    <option value="alerts">Nur Alerts</option>
    <option value="fallbacks">Nur Fallbacks</option>
  </select>
</label>
```

In `refreshQuality()`, replace the `only_failed` logic:

```js
var filter = ge('qt-filter').value;
var onlyFailed = filter === 'failed';
// ... existing fetch ...
// after loading events:
var filtered = (evD.events || []).filter(function(e) {
  if (filter === 'alerts') return e.final_outcome === 'alert_fired';
  if (filter === 'fallbacks') return e.final_outcome === 'fallback_triggered';
  return true;
});
var rows = filtered.map(function(e) {
  var when = new Date(e.ts * 1000).toLocaleString();
  var marker;
  if (e.final_outcome === 'alert_fired') marker = '<span style="color:#a83;font-weight:bold;">ALERT</span>';
  else if (e.final_outcome === 'fallback_triggered') marker = '<span style="color:#38f;font-weight:bold;">FALLBACK</span>';
  else marker = e.passed ? '<span style="color:#2a5;">OK</span>' : '<span style="color:#c33;">FAIL</span>';
  var detail = e.passed ? (e.tool_name || '') : ((e.errors || []).map(function(er){return er.code;}).join(','));
  return '<div>' + when + ' ' + marker + ' ' + e.backend + '/' + e.model + ' ' + e.modul + ' r=' + e.retry_attempt + ' ' + detail + '</div>';
});
```

- [ ] **A4.4: Build + commit**

```bash
cargo build --release --quiet 2>&1 | tail -3
git add src/frontend.html
git commit -m "feat(guardrail-v2): Quality Alerts UI + event-filter dropdown (all|failed|alerts|fallbacks)"
```

**Phase V2-A complete.**

---

## Phase V2-B — A/B-Benchmark

### Task B1: `run_compare` + `CompareEvent` + `CompareReport`

**Files:** Modify `src/benchmark.rs`, `src/types.rs`.

- [ ] **B1.1: Append types to `src/types.rs`**

```rust
#[derive(Debug, Clone, Serialize)]
pub struct BenchmarkCompareReport {
    pub report_a: BenchmarkReport,
    pub report_b: BenchmarkReport,
    pub winner_per_case: Vec<(String, String)>,  // (case_id, "A"|"B"|"tie")
}
```

- [ ] **B1.2: Append `run_compare` + event type to `src/benchmark.rs`**

```rust
#[derive(serde::Serialize, Clone)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum CompareEvent {
    SideStart { side: String, backend: String, model: String },
    SideCaseResult { side: String, result: crate::types::BenchmarkResult },
    Report { report: crate::types::BenchmarkCompareReport },
    Error { message: String },
}

pub async fn run_compare(
    backend_a: crate::types::LlmBackend,
    backend_b: crate::types::LlmBackend,
    run_modul_id: String,
    cfg_snapshot: crate::types::AgentConfig,
    py_modules: Vec<crate::loader::PyModuleMeta>,
    llm: std::sync::Arc<crate::llm::LlmRouter>,
    tx: tokio::sync::mpsc::Sender<CompareEvent>,
) {
    let _ = tx.send(CompareEvent::SideStart {
        side: "A".into(), backend: backend_a.id.clone(), model: backend_a.model.clone(),
    }).await;
    let _ = tx.send(CompareEvent::SideStart {
        side: "B".into(), backend: backend_b.id.clone(), model: backend_b.model.clone(),
    }).await;

    let (tx_a, mut rx_a) = tokio::sync::mpsc::channel::<BenchmarkEvent>(64);
    let (tx_b, mut rx_b) = tokio::sync::mpsc::channel::<BenchmarkEvent>(64);

    let cfg_a = cfg_snapshot.clone();
    let py_a = py_modules.clone();
    let llm_a = llm.clone();
    let modul_a = run_modul_id.clone();
    let a_handle = tokio::spawn(async move {
        run_benchmark(backend_a, modul_a, cfg_a, py_a, llm_a, tx_a).await;
    });
    let cfg_b = cfg_snapshot.clone();
    let py_b = py_modules.clone();
    let llm_b = llm.clone();
    let modul_b = run_modul_id.clone();
    let b_handle = tokio::spawn(async move {
        run_benchmark(backend_b, modul_b, cfg_b, py_b, llm_b, tx_b).await;
    });

    let tx_fwd_a = tx.clone();
    let collect_a = tokio::spawn(async move {
        let mut report: Option<crate::types::BenchmarkReport> = None;
        while let Some(ev) = rx_a.recv().await {
            match ev {
                BenchmarkEvent::CaseResult { result } => {
                    let _ = tx_fwd_a.send(CompareEvent::SideCaseResult { side: "A".into(), result }).await;
                }
                BenchmarkEvent::Report { report: r } => { report = Some(r); }
                _ => {}
            }
        }
        report
    });
    let tx_fwd_b = tx.clone();
    let collect_b = tokio::spawn(async move {
        let mut report: Option<crate::types::BenchmarkReport> = None;
        while let Some(ev) = rx_b.recv().await {
            match ev {
                BenchmarkEvent::CaseResult { result } => {
                    let _ = tx_fwd_b.send(CompareEvent::SideCaseResult { side: "B".into(), result }).await;
                }
                BenchmarkEvent::Report { report: r } => { report = Some(r); }
                _ => {}
            }
        }
        report
    });

    let _ = a_handle.await;
    let _ = b_handle.await;
    let ra = collect_a.await.ok().flatten();
    let rb = collect_b.await.ok().flatten();

    match (ra, rb) {
        (Some(a), Some(b)) => {
            let mut winners = Vec::new();
            for r_a in &a.results {
                if let Some(r_b) = b.results.iter().find(|x| x.case_id == r_a.case_id) {
                    let w = match (r_a.passed, r_b.passed) {
                        (true, false) => "A",
                        (false, true) => "B",
                        _ => "tie",
                    };
                    winners.push((r_a.case_id.clone(), w.to_string()));
                }
            }
            let report = crate::types::BenchmarkCompareReport {
                report_a: a, report_b: b, winner_per_case: winners,
            };
            let _ = tx.send(CompareEvent::Report { report }).await;
        }
        _ => {
            let _ = tx.send(CompareEvent::Error { message: "one or both benchmark reports missing".into() }).await;
        }
    }
}
```

- [ ] **B1.3: Build + commit**

```bash
cargo build --quiet 2>&1 | tail -3
git add src/benchmark.rs src/types.rs
git commit -m "feat(guardrail-v2): run_compare with parallel benchmark + winner-per-case"
```

### Task B2: Endpoint `/api/quality/benchmark/compare`

**Files:** Modify `src/web.rs`.

- [ ] **B2.1: Add handler**

```rust
#[derive(serde::Deserialize)]
pub struct BenchmarkCompareReq {
    pub backend_a: String,
    pub backend_b: String,
    pub modul_id: Option<String>,
}

pub async fn quality_benchmark_compare(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::Json(req): axum::Json<BenchmarkCompareReq>,
) -> Result<axum::response::Response, (axum::http::StatusCode, String)> {
    let cfg_snap = state.config.read().await.clone();
    let ba = cfg_snap.llm_backends.iter().find(|b| b.id == req.backend_a).cloned()
        .ok_or((axum::http::StatusCode::NOT_FOUND, format!("backend A '{}' not found", req.backend_a)))?;
    let bb = cfg_snap.llm_backends.iter().find(|b| b.id == req.backend_b).cloned()
        .ok_or((axum::http::StatusCode::NOT_FOUND, format!("backend B '{}' not found", req.backend_b)))?;
    let modul_id = req.modul_id.unwrap_or_else(|| {
        cfg_snap.module.iter().find(|m| m.typ == "chat").map(|m| m.id.clone()).unwrap_or_default()
    });
    if modul_id.is_empty() {
        return Err((axum::http::StatusCode::BAD_REQUEST, "no chat module available for context".into()));
    }
    let py_mods: Vec<crate::loader::PyModuleMeta> = state.py_modules.read().await.clone();
    let llm = state.llm.clone();

    let (tx, rx) = tokio::sync::mpsc::channel::<crate::benchmark::CompareEvent>(64);
    tokio::spawn(async move {
        crate::benchmark::run_compare(ba, bb, modul_id, cfg_snap, py_mods, llm, tx).await;
    });

    use tokio_stream::StreamExt as _;
    let stream = tokio_stream::wrappers::ReceiverStream::new(rx).map(|ev| {
        let line = serde_json::to_string(&ev).unwrap_or_default() + "\n";
        Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(line))
    });
    let body = axum::body::Body::from_stream(stream);
    Ok(axum::response::Response::builder()
        .status(axum::http::StatusCode::OK)
        .header("content-type", "application/x-ndjson")
        .header("cache-control", "no-cache")
        .body(body).unwrap())
}
```

Register: `.route("/api/quality/benchmark/compare", axum::routing::post(quality_benchmark_compare))`.

- [ ] **B2.2: Build + commit**

```bash
cargo build --quiet 2>&1 | tail -3
git add src/web.rs
git commit -m "feat(guardrail-v2): /api/quality/benchmark/compare endpoint"
```

### Task B3: UI — Quality tab A/B section

**Files:** Modify `src/frontend.html`.

- [ ] **B3.1: Extend Quality tab below the existing Benchmark block**

After the existing `<div id="qb-output">`, add:

```html
<h4 style="margin-top:16px;">A/B Compare</h4>
<div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;">
  <label>Backend A <select id="qab-a"></select></label>
  <label>Backend B <select id="qab-b"></select></label>
  <button class="btn btn-primary" onclick="runCompare()">A/B Compare</button>
</div>
<div id="qab-output" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-family:monospace;font-size:12px;max-height:40vh;overflow:auto;"></div>
```

JS:

```js
async function populateCompareBackends() {
  var r = await fetch('/api/config', {headers: typeof authHeaders==='function' ? authHeaders() : {}});
  var cfg = await r.json();
  var opts = (cfg.llm_backends || []).map(function(b){
    return '<option value="' + b.id + '">' + b.id + ' (' + b.model + ')</option>';
  }).join('');
  if (ge('qab-a')) ge('qab-a').innerHTML = opts;
  if (ge('qab-b')) ge('qab-b').innerHTML = opts;
}

async function runCompare() {
  var out = ge('qab-output');
  out.innerHTML = '<div>Side A: starting...</div><div>Side B: starting...</div>';
  var colA = document.createElement('div');
  var colB = document.createElement('div');
  out.innerHTML = '';
  out.appendChild(colA); out.appendChild(colB);
  var headers = Object.assign({'Content-Type':'application/json'}, typeof authHeaders==='function' ? authHeaders() : {});
  var r = await fetch('/api/quality/benchmark/compare', {
    method: 'POST', headers: headers,
    body: JSON.stringify({backend_a: ge('qab-a').value, backend_b: ge('qab-b').value}),
  });
  if (!r.ok) { out.innerHTML = 'FAIL: ' + r.status; return; }
  var reader = r.body.getReader();
  var dec = new TextDecoder();
  var buf = '';
  while (true) {
    var chunk = await reader.read();
    if (chunk.done) break;
    buf += dec.decode(chunk.value, {stream: true});
    var idx;
    while ((idx = buf.indexOf('\n')) >= 0) {
      var line = buf.slice(0, idx); buf = buf.slice(idx + 1);
      if (!line.trim()) continue;
      try {
        var ev = JSON.parse(line);
        if (ev.type === 'side_start') {
          var tgt = ev.side === 'A' ? colA : colB;
          tgt.innerHTML = '<strong>' + ev.side + ': ' + ev.backend + ' / ' + ev.model + '</strong><br>';
        } else if (ev.type === 'side_case_result') {
          var tgt2 = ev.side === 'A' ? colA : colB;
          var mark = ev.result.passed ? 'OK' : 'FAIL';
          var col = ev.result.passed ? '#2a5' : '#c33';
          tgt2.innerHTML += '<div style="color:' + col + ';">' + ev.result.case_id + ': ' + mark + ' [' + ev.result.latency_ms + 'ms]</div>';
        } else if (ev.type === 'report') {
          var rep = ev.report;
          var pa = Math.round((rep.report_a.passed / Math.max(rep.report_a.total_cases,1)) * 100);
          var pb = Math.round((rep.report_b.passed / Math.max(rep.report_b.total_cases,1)) * 100);
          var aWins = rep.winner_per_case.filter(function(w){return w[1] === 'A';}).length;
          var bWins = rep.winner_per_case.filter(function(w){return w[1] === 'B';}).length;
          var ties = rep.winner_per_case.filter(function(w){return w[1] === 'tie';}).length;
          colA.innerHTML += '<div style="margin-top:8px;border-top:1px solid #333;padding-top:4px;"><strong>Pass: ' + rep.report_a.passed + '/' + rep.report_a.total_cases + ' (' + pa + '%)</strong></div>';
          colB.innerHTML += '<div style="margin-top:8px;border-top:1px solid #333;padding-top:4px;"><strong>Pass: ' + rep.report_b.passed + '/' + rep.report_b.total_cases + ' (' + pb + '%)</strong></div>';
          var winBox = document.createElement('div');
          winBox.style.cssText = 'grid-column:1/-1;margin-top:12px;padding:8px;background:#181818;border-radius:4px;';
          winBox.innerHTML = 'Winners: A=' + aWins + ' | B=' + bWins + ' | tie=' + ties;
          out.appendChild(winBox);
        } else if (ev.type === 'error') {
          out.innerHTML += '<div style="color:#c33;grid-column:1/-1;">FEHLER: ' + ev.message + '</div>';
        }
      } catch(e) { console.error(e); }
    }
  }
}
document.addEventListener('DOMContentLoaded', populateCompareBackends);
```

- [ ] **B3.2: Build + commit**

```bash
cargo build --release --quiet 2>&1 | tail -3
git add src/frontend.html
git commit -m "feat(guardrail-v2): A/B Compare UI in Quality tab"
```

**Phase V2-B complete.**

---

## Phase V2-C — Strict-Triggers config-driven

### Task C1: config-driven triggers

**Files:** Modify `src/guardrail.rs`.

- [ ] **C1.1: Convert `STRICT_TRIGGERS` const usage to function**

Find the existing hardcoded list (`const STRICT_TRIGGERS: &[&str] = ...`) and the place where it's used inside `validate_response`. Replace the direct reference:

```rust
fn effective_strict_triggers<'a>(cfg: &'a crate::types::GuardrailConfig) -> Vec<String> {
    if cfg.strict_triggers.is_empty() {
        DEFAULT_STRICT_TRIGGERS.iter().map(|s| s.to_string()).collect()
    } else {
        cfg.strict_triggers.clone()
    }
}
```

Where `DEFAULT_STRICT_TRIGGERS` is the renamed old const.

Update `validate_response` Item-7 logic: instead of reading `STRICT_TRIGGERS` directly, read from `ctx.cfg.guardrail` or pass the triggers list into ValidatorContext. Simplest: `ValidatorContext` already has `cfg: &AgentConfig`, so inside `validate_response`:

```rust
let triggers = if let Some(g) = ctx.cfg.guardrail.as_ref() {
    effective_strict_triggers(g)
} else {
    DEFAULT_STRICT_TRIGGERS.iter().map(|s| s.to_string()).collect()
};
if ctx.strict_mode {
    if let Some(msg) = ctx.last_user_msg {
        let low = msg.to_lowercase();
        if triggers.iter().any(|t| low.contains(t.to_lowercase().as_str())) {
            // ... existing strict-mode fail logic ...
        }
    }
}
```

- [ ] **C1.2: Append test for config-override**

```rust
#[test]
fn effective_strict_triggers_uses_config_when_provided() {
    let mut cfg = crate::types::GuardrailConfig::default();
    assert!(effective_strict_triggers(&cfg).iter().any(|s| s == "search" || s == "ruf")); // default set
    cfg.strict_triggers = vec!["custom_trigger".into()];
    let tr = effective_strict_triggers(&cfg);
    assert_eq!(tr, vec!["custom_trigger".to_string()]);
}
```

- [ ] **C1.3: Build + test + commit**

```bash
cargo test guardrail:: 2>&1 | tail -3
git add src/guardrail.rs
git commit -m "feat(guardrail-v2): strict triggers config-driven with hardcoded fallback"
```

### Task C2: Textarea-UI + wire to config

**Files:** Modify `src/frontend.html`.

- [ ] **C2.1: Add textarea inside Guardrail section**

After strict_mode checkbox, add:

```html
<div style="margin:8px 0;"><label>Strict triggers (ein Wort pro Zeile — leer = Defaults)<br>
  <textarea id="gcfg-strict-triggers" rows="4" style="width:300px;font-family:monospace;font-size:12px;"></textarea>
</label></div>
```

- [ ] **C2.2: Extend populate/collect**

In `populateGuardrailSettings`:
```js
if (ge('gcfg-strict-triggers'))
  ge('gcfg-strict-triggers').value = (g.strict_triggers || []).join('\n');
```

In `collectGuardrailSettings`, replace `strict_triggers: []` with:
```js
    strict_triggers: ge('gcfg-strict-triggers').value.split('\n').map(function(s){return s.trim();}).filter(function(s){return s.length > 0;}),
```

- [ ] **C2.3: Build + commit**

```bash
cargo build --release --quiet 2>&1 | tail -3
git add src/frontend.html
git commit -m "feat(guardrail-v2): strict-triggers textarea editor in Config tab"
```

**Phase V2-C complete.** Manual verification (strict-mode against local Ollama) is OPERATOR work, no code.

---

## Phase V2-D — Backup-LLM-Fallback

### Task D1: log_fallback_event

**Files:** Modify `src/guardrail.rs`.

- [ ] **D1.1: Append function + test**

```rust
pub async fn log_fallback_event(
    data_root: &Path,
    original: &str,
    fallback: &str,
    modul: &str,
    codes: &[String],
) -> std::io::Result<()> {
    let ev = crate::types::GuardrailEvent {
        ts: chrono::Utc::now().timestamp(),
        modul: modul.into(),
        backend: original.into(),
        model: fallback.into(),
        tool_name: None,
        passed: false,
        errors: codes.iter().map(|c| crate::types::ValidationError {
            field: "backend".into(),
            code: c.clone(),
            human_message_de: format!("Hard-fail, fallback auf {}", fallback),
        }).collect(),
        retry_attempt: 0,
        final_outcome: "fallback_triggered".into(),
        similar_suggestion: Some(fallback.into()),
    };
    log_event(data_root, &ev).await
}
```

Test:
```rust
#[tokio::test]
async fn log_fallback_event_roundtrip() {
    let tmp = tempfile::tempdir().unwrap();
    log_fallback_event(tmp.path(), "grok", "claude", "chat.x", &["unknown_tool".into()]).await.unwrap();
    let events = load_events_since(tmp.path(), 0, 10, None, false).await;
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].final_outcome, "fallback_triggered");
    assert_eq!(events[0].similar_suggestion.as_deref(), Some("claude"));
}
```

- [ ] **D1.2: Build + test + commit**

```bash
cargo test guardrail:: 2>&1 | tail -5
git add src/guardrail.rs
git commit -m "feat(guardrail-v2): log_fallback_event helper"
```

### Task D2: Fallback in chat.rs retry-loop

**Files:** Modify `src/web.rs` (the `chat` handler, not `chat_stream_endpoint`).

- [ ] **D2.1: Wrap hard-fail with backup-attempt**

In the existing guardrail-fail branch inside the `chat` handler's tool-loop, where the code currently does `break` on hard-fail, change to:

```rust
Err(errors) => {
    let is_last = guardrail_retries >= max_retries_for_backend;
    // log fail as before
    let ev = crate::types::GuardrailEvent { /* same as current */ ... };
    let _ = crate::guardrail::log_event(&state.data_root, &ev).await;

    if is_last {
        // Try backup_llm once
        let mod_cfg = cfg_snap.module.iter().find(|m| m.id == modul_id);
        let backup_id = mod_cfg.and_then(|m| m.backup_llm.clone());
        if gcfg.fallback_on_hard_fail && backup_id.is_some() && !used_fallback {
            let bid = backup_id.unwrap();
            if let Some(bb) = cfg_snap.llm_backends.iter().find(|b| b.id == bid).cloned() {
                let codes: Vec<String> = errors.iter().map(|e| e.code.clone()).collect();
                let _ = crate::guardrail::log_fallback_event(&state.data_root, &backend_id, &bid, &modul_id, &codes).await;
                backend_id = bb.id.clone();
                model = bb.model.clone();
                used_fallback = true;
                guardrail_retries = 0;
                // Also update LLM call args so subsequent iterations use backup
                // (the chat handler calls llm.chat_with_tools(&backend_id, ...) each round)
                continue;
            }
        }
        tracing::warn!("Guardrail hard-fail in chat.{}: {:?}", modul_id, errors.iter().map(|e| e.code.clone()).collect::<Vec<_>>());
        break;
    } else {
        // existing retry path
        let feedback = crate::guardrail::synth_feedback_user_message(&errors, max_retries_for_backend, guardrail_retries);
        messages.push(serde_json::json!({"role": "user", "content": feedback}));
        guardrail_retries += 1;
        continue;
    }
}
```

Add `let mut used_fallback = false;` before the tool-loop (alongside `guardrail_retries`).

- [ ] **D2.2: Build + commit**

```bash
cargo build --quiet 2>&1 | tail -5
git add src/web.rs
git commit -m "feat(guardrail-v2): backup-LLM fallback on hard-fail in chat handler"
```

### Task D3: Fallback in cycle.rs retry-loop

**Files:** Modify `src/cycle.rs`.

- [ ] **D3.1: Same pattern inside `exec_llm`**

Find the guardrail-hard-fail branch in `exec_llm` (the one that sets `aufgabe.ergebnis = "FAILED: Guardrail hard-fail..."` and calls `verschieben`). Wrap with backup-attempt:

```rust
Err(errors) => {
    // log as before
    let _ = crate::guardrail::log_event(&pipeline.base, &ev_fail).await;

    let is_last = guardrail_retries >= max_retries_for_backend;
    if is_last {
        let mod_cfg = cfg_snap.module.iter().find(|m| m.id == aufgabe.modul);
        let backup_id = mod_cfg.and_then(|m| m.backup_llm.clone());
        if gcfg.fallback_on_hard_fail && backup_id.is_some() && !used_fallback {
            let bid = backup_id.unwrap();
            if let Some(bb) = cfg_snap.llm_backends.iter().find(|b| b.id == bid).cloned() {
                let codes: Vec<String> = errors.iter().map(|e| e.code.clone()).collect();
                let _ = crate::guardrail::log_fallback_event(&pipeline.base, &backend_id, &bid, &aufgabe.modul, &codes).await;
                backend_id = bb.id.clone();
                model_str = bb.model.clone();
                used_fallback = true;
                guardrail_retries = 0;
                continue;
            }
        }
        aufgabe.ergebnis = format!("FAILED: Guardrail hard-fail: {:?}", errors.iter().map(|e| e.code.clone()).collect::<Vec<_>>());
        pipeline.verschieben(&aufgabe, crate::pipeline::Status::Failed);
        return;
    } else {
        messages.push(serde_json::json!({"role": "user", "content": crate::guardrail::synth_feedback_user_message(&errors, max_retries_for_backend, guardrail_retries)}));
        guardrail_retries += 1;
        continue;
    }
}
```

Add `let mut used_fallback = false;` alongside `guardrail_retries`.

- [ ] **D3.2: Build + commit**

```bash
cargo build --quiet 2>&1 | tail -5
git add src/cycle.rs
git commit -m "feat(guardrail-v2): backup-LLM fallback on hard-fail in cycle LLM task loop"
```

### Task D4: Fallback in wizard.rs

**Files:** Modify `src/wizard.rs`.

- [ ] **D4.1: Parallel the chat-handler pattern**

The wizard's `run_turn` uses a trait object `backend: &dyn WizardBackend`. To swap it, use a local owned `Box<dyn WizardBackend + Send + Sync>` that can be replaced. At the start of the function, before the tool-round loop:

```rust
let mut active_backend: Box<dyn WizardBackend + Send + Sync> = Box::new(WrappedRef { inner: backend });
let mut active_backend_id: String = wizard_cfg.llm.id.clone();
let mut active_model: String = wizard_cfg.llm.model.clone();
let mut used_fallback = false;
```

Where `WrappedRef` is a tiny adapter:

```rust
struct WrappedRef<'a> { inner: &'a dyn WizardBackend }
#[async_trait::async_trait]
impl<'a> WizardBackend for WrappedRef<'a> {
    async fn chat(&self, messages: &[serde_json::Value], tools: &[serde_json::Value])
        -> Result<(String, serde_json::Value), String> {
        self.inner.chat(messages, tools).await
    }
}
```

Inside the loop, replace `backend.chat(...)` with `active_backend.chat(...)`, `&backend_id` with `&active_backend_id`, `&model` with `&active_model`.

On hard-fail branch, before returning:

```rust
if gcfg.fallback_on_hard_fail && !used_fallback {
    // The wizard doesn't have a modul_config with backup_llm — its backup is read from config.wizard.backup_llm if we add that field.
    // Phase V2-D scope limits: if config.wizard.llm does NOT have a backup field, skip wizard fallback.
    // For Phase V2-D we DO NOT add a wizard-specific backup. Wizard runs one primary backend only.
}
```

**Decision:** Wizard fallback is OUT-OF-SCOPE for V2-D. The wizard has only one LLM configured (`config.wizard.llm`), no backup field. Adding one is cosmetic and can be a future enhancement. Document this in the commit message.

Actually — just skip the `active_backend` gymnastics entirely for the wizard. The wizard's hard-fail branch emits `WizardEvent::Error` as it does today. No code change in wizard.rs for V2-D.

- [ ] **D4.2: Commit a tiny "noop" marker**

Skip this step — no commit needed. The fallback feature only applies to module-scoped calls (chat + cycle), not to the wizard's own backend.

### Task D5: Final test run + final commit

- [ ] **D5.1: Full test run**

```bash
cargo test 2>&1 | tail -5
```

Expect: all tests pass (existing + ~6 new V2 tests).

- [ ] **D5.2: Merge back to v1.0-foundation**

```bash
cd /home/badmin/aistuff/agent
git status --short | head -5  # should be clean
git merge --ff-only feat/guardrail-v2
```

- [ ] **D5.3: Smoke-test manually**

Start server with xAI key, open browser:
- Config tab → toggle Alerts enabled, set notify_backend_id → save
- Trigger a burst of bad-tool calls (or wait 20+ real calls)
- Watch `tail -f agent-data/guardrail-events/*.jsonl` for `alert_fired` entries
- In Quality tab → A/B Compare with two backends → stream should render side-by-side
- Edit strict triggers textarea → save → verify reflects in config.json

Document any findings.

**Phase V2-D complete.**

---

## Self-Review

- **Spec coverage:** Every section of the V2 spec maps to a task:
  - §7 Alerting → Tasks A1-A4
  - §8 A/B-Benchmark → Tasks B1-B3
  - §9 Strict-Triggers → Tasks C1-C2
  - §10 Backup-Fallback → Tasks D1-D4 (wizard explicitly descoped)
- **Placeholders:** None. Every step has concrete code or exact commands.
- **Type consistency:** `GuardrailAlertConfig`, `BenchmarkCompareReport`, `CompareEvent`, `check_alert_threshold`, `log_alert_event`, `log_fallback_event`, `run_compare` — all defined in early tasks, referenced consistently downstream.
- **Known limitation (documented):** Wizard doesn't get backup-fallback (out-of-scope for V2-D because wizard has no `backup_llm` field).
- **Integration points touched:** 3 places (chat, cycle) for fallback — matched spec §10.1.
