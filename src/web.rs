use axum::{Router, Json, response::Html, extract::State, extract::DefaultBodyLimit};
use axum::body::Body;
use axum::response::IntoResponse;
use std::sync::Arc;
use tokio::sync::RwLock;
use tokio_stream::StreamExt;
use crate::pipeline::Pipeline;
use crate::types::*;
use crate::llm::LlmRouter;
use crate::tools;
use crate::util;
use crate::security::{self, safe_id, safe_relative_path};
use crate::wizard;
use crate::types::{WizardSession, WizardMode, DraftAgent, DraftIdentity};

const MAX_CHAT_TOOL_ROUNDS: usize = 30;

/// Static price table: (input_per_1k_usd, output_per_1k_usd) per model id.
/// Prefix-match: longest matching prefix wins. Update as providers change pricing.
fn model_price_per_1k(model: &str) -> Option<(f64, f64)> {
    const TABLE: &[(&str, f64, f64)] = &[
        // Anthropic (as of 2026-04)
        ("claude-opus-4",     15.00, 75.00),
        ("claude-sonnet-4",    3.00, 15.00),
        ("claude-haiku-4",     1.00,  5.00),
        ("claude-opus-3",     15.00, 75.00),
        ("claude-sonnet-3",    3.00, 15.00),
        ("claude-haiku-3",     0.25,  1.25),
        // OpenAI
        ("gpt-5",              5.00, 15.00),
        ("gpt-4o-mini",        0.15,  0.60),
        ("gpt-4o",             2.50, 10.00),
        ("gpt-4",             30.00, 60.00),
        ("o1",                15.00, 60.00),
        // xAI Grok
        ("grok-4-1-fast",      0.20,  0.50),
        ("grok-4-1",           3.00, 15.00),
        ("grok-4",             3.00, 15.00),
        ("grok-3",             2.00, 10.00),
        ("grok-2",             2.00, 10.00),
        // Ollama / local (free)
        ("gemma",              0.00,  0.00),
        ("llama",              0.00,  0.00),
        ("qwen",               0.00,  0.00),
        ("mistral",            0.00,  0.00),
    ];
    let m = model.to_lowercase();
    let mut best: Option<(usize, f64, f64)> = None;
    for (prefix, i, o) in TABLE {
        if m.starts_with(prefix) {
            let len = prefix.len();
            if best.map(|(l, _, _)| len > l).unwrap_or(true) {
                best = Some((len, *i, *o));
            }
        }
    }
    best.map(|(_, i, o)| (i, o))
}

/// Token-Usage Tracking
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct TokenStats {
    pub total_input: u64,
    pub total_output: u64,
    pub total_calls: u64,
    pub calls: Vec<TokenCall>,
    /// Total cost accumulated since process start, in USD (computed from model prices).
    pub cost_usd_total: f64,
    /// Cost accumulated during the current UTC day. Resets at midnight UTC.
    pub cost_usd_today: f64,
    /// Unix timestamp of day-start for current `cost_usd_today` accumulator.
    pub day_started_ts: i64,
    /// Sum aller aktiven Reservations (USD). Wird bei Budget-Check mitgerechnet,
    /// damit N parallele Calls nicht alle den Check passieren bevor einer trackt.
    /// `track_tokens` dekrementiert wieder um die Reservation und addiert den actual.
    #[serde(default)]
    pub reserved_usd: f64,
    /// Zähler aktiver Reservations (für UI-Debug; nicht für Budget-Check).
    #[serde(default)]
    pub reserved_calls: u64,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct TokenCall {
    pub time: String,
    pub backend: String,
    pub model: String,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub modul: String,
}

/// Rückwärts-Kompatibel: Callers erwarten einen TokenTracker-Arc. Der innere Wert
/// ist jetzt nur noch ein Timestamp-Tracker für UI-Invalidation; die EIGENTLICHEN
/// Stats kommen aus SQLite (persistent, transaktional).
pub type TokenTracker = Arc<RwLock<TokenStats>>;

/// Fallback-Reservation wenn model-price nicht bekannt. Konservativ für $5-Cap.
const LLM_CALL_RESERVATION_FALLBACK_USD: f64 = 0.10;

/// Worst-Case-Reservation pro LLM-Call — berechnet aus model-pricing × Token-
/// Estimates. Input ~8k (durchschnittlicher System-Prompt + Context), Output
/// bis max_tokens 12k. Für teure Modelle (Opus $15/$75) ergibt das ~$1.05, für
/// billige (DeepSeek) ~$0.01. Das macht das Budget-Cap wirklich Cap statt
/// Advisory (GLM-Finding Run SQLite-7: "Budget ist Ratespiel ohne atomare
/// Reservation auf Model-Basis").
fn reservation_for_model(model: &str) -> f64 {
    const INPUT_ESTIMATE_K: f64 = 8.0;
    const OUTPUT_MAX_K: f64 = 12.0;
    model_price_per_1k(model)
        .map(|(ip, op)| INPUT_ESTIMATE_K * ip + OUTPUT_MAX_K * op)
        .unwrap_or(LLM_CALL_RESERVATION_FALLBACK_USD)
}

pub struct AppState {
    pub pipeline: Arc<Pipeline>,
    pub config: Arc<RwLock<AgentConfig>>,
    pub llm: Arc<LlmRouter>,
    pub heartbeats: crate::cycle::HeartbeatMap,
    pub py_modules: Arc<RwLock<Vec<crate::loader::PyModuleMeta>>>,
    pub py_pool: Arc<crate::loader::PyProcessPool>,
    pub busy: crate::cycle::BusyMap,
    pub tokens: TokenTracker,
    pub rate_limit: Arc<security::RateLimiter>,
    pub wizard_rate: Arc<security::RateLimiter>,
    pub data_root: std::path::PathBuf,
    pub config_path: std::path::PathBuf,
    pub wizard_turn_inflight: Arc<tokio::sync::Mutex<std::collections::HashSet<String>>>,
    // Config-Write-Lock lebt jetzt in Pipeline (geteilter Zugriff zwischen
    // Web-API und Orchestrator-Cleanup); s.pipeline.config_write_lock nutzen.
}

pub fn router(state: Arc<AppState>) -> Router {
    let body_limit = {
        // Best-effort sync read; fallback to 2MB
        state.config.try_read().map(|c| c.max_body_bytes).unwrap_or(2 * 1024 * 1024)
    };
    let auth_state = Arc::new(security::AuthState { config: state.config.clone() });
    Router::new()
        .route("/favicon.ico", axum::routing::get(favicon))
        .route("/", axum::routing::get(index))
        .route("/chat/{modul_id}", axum::routing::get(chat_page))
        .route("/chat/{modul_id}/{rest}", axum::routing::get(chat_page))
        .route("/wizard", axum::routing::get(wizard_page))
        .route("/setup", axum::routing::get(setup_page))
        .route("/api/setup/status", axum::routing::get(setup_status))
        .route("/api/setup/test-backend", axum::routing::post(setup_test_backend))
        .route("/api/setup/save-backend", axum::routing::post(setup_save_backend))
        .route("/api/config", axum::routing::get(get_config))
        .route("/api/config", axum::routing::post(save_config))
        .route("/api/config/backups", axum::routing::get(list_config_backups))
        .route("/api/config/restore/{slot}", axum::routing::post(restore_config_backup))
        .route("/api/aufgaben", axum::routing::get(get_aufgaben))
        .route("/api/aufgaben/{id}", axum::routing::delete(cancel_aufgabe))
        .route("/api/aufgaben/{id}", axum::routing::patch(edit_aufgabe))
        .route("/api/chat", axum::routing::post(chat))
        .route("/api/chat-stream", axum::routing::post(chat_stream_endpoint))
        .route("/api/logs/{datum}", axum::routing::get(get_logs))
        .route("/api/status", axum::routing::get(get_status))
        .route("/api/metrics", axum::routing::get(get_metrics))
        .route("/api/modules", axum::routing::get(get_py_modules))
        .route("/api/tokens", axum::routing::get(get_tokens))
        .route("/api/tokens/by-modul", axum::routing::get(get_tokens_by_modul))
        .route("/api/tokens/by-backend", axum::routing::get(get_tokens_by_backend))
        .route("/api/audit", axum::routing::get(get_audit))
        .route("/api/module-capabilities/{id}", axum::routing::get(get_module_capabilities))
        .route("/api/llm-models/{backend_id}", axum::routing::get(list_llm_models))
        .route("/api/module-config/{name}", axum::routing::get(get_module_config))
        .route("/api/module-config/{name}", axum::routing::post(save_module_config))
        .route("/api/convos/{modul_id}", axum::routing::get(list_convos))
        .route("/api/convos/{modul_id}/{convo_id}", axum::routing::get(load_convo))
        .route("/api/convos/{modul_id}/{convo_id}", axum::routing::put(save_convo))
        .route("/api/convos/{modul_id}/{convo_id}", axum::routing::delete(delete_convo))
        .route("/api/templates/{typ}", axum::routing::get(get_template))
        .route("/api/home/{modul_id}", axum::routing::get(list_home))
        .route("/api/home/{modul_id}/{path}", axum::routing::get(read_home_file))
        .route("/api/home/{modul_id}/{path}", axum::routing::delete(delete_home_file))
        .route("/api/home-clear/{modul_id}", axum::routing::delete(clear_home))
        .route("/api/prompt-preview/{modul_id}", axum::routing::get(prompt_preview))
        .route("/api/cron/{id}/trigger", axum::routing::post(trigger_cron))
        .route("/api/wizard/start", axum::routing::post(wizard_start))
        .route("/api/wizard/abort", axum::routing::post(wizard_abort))
        .route("/api/wizard/patch", axum::routing::post(wizard_patch))
        .route("/api/wizard/sessions", axum::routing::get(wizard_list_sessions))
        .route("/api/wizard/turn", axum::routing::post(wizard_turn))
        .route("/api/wizard/models", axum::routing::get(wizard_models))
        .route("/api/wizard/test-connection", axum::routing::post(wizard_test_connection))
        .route("/api/wizard/confirm-code-gen", axum::routing::post(wizard_confirm_code_gen))
        .route("/api/quality/stats", axum::routing::get(quality_stats))
        .route("/api/quality/events", axum::routing::get(quality_events))
        .route("/api/quality/benchmark/cases", axum::routing::get(quality_benchmark_cases))
        .route("/api/quality/benchmark/run", axum::routing::post(quality_benchmark_run))
        .route("/api/quality/benchmark/compare", axum::routing::post(quality_benchmark_compare))
        .layer(axum::middleware::from_fn_with_state(auth_state, security::auth_middleware))
        .layer(DefaultBodyLimit::max(body_limit))
        .with_state(state)
}

async fn index(State(s): State<Arc<AppState>>) -> axum::response::Response {
    // Wenn kein Backend erreichbar → 302 zu /setup. User sieht dann den First-
    // Run-Wizard statt eines leeren Dashboards mit unklarem next step.
    let needs = {
        let cfg = s.config.read().await;
        if cfg.llm_backends.is_empty() { true } else {
            let client = reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(2))
                .build().unwrap_or_default();
            let mut any = false;
            for b in &cfg.llm_backends {
                if crate::types::test_backend_reachable(&client, b).await { any = true; break; }
            }
            !any
        }
    };
    if needs {
        return axum::response::Redirect::to("/setup").into_response();
    }
    Html(include_str!("frontend.html")).into_response()
}

async fn chat_page() -> Html<&'static str> {
    Html(include_str!("chat.html"))
}

async fn wizard_page() -> Html<&'static str> {
    Html(include_str!("wizard.html"))
}

async fn favicon() -> impl IntoResponse {
    axum::response::Response::builder()
        .status(204)
        .body(Body::empty())
        .unwrap_or_else(|_| axum::response::Response::new(Body::empty()))
}

/// Eigenständiger Router für einen Chat-Port — bedient EINE Instanz
pub fn chat_router(state: Arc<AppState>, modul_id: String) -> Router {
    let mid = modul_id.clone();
    let body_limit = state.config.try_read().map(|c| c.max_body_bytes).unwrap_or(2 * 1024 * 1024);
    let auth_state = Arc::new(security::AuthState { config: state.config.clone() });
    Router::new()
        .route("/favicon.ico", axum::routing::get(favicon))
        .route("/", axum::routing::get(move || {
            let mid = mid.clone();
            async move {
                // chat.html mit injiziertem Meta-Tag für die Modul-ID
                let html = include_str!("chat.html");
                let injected = html.replace(
                    "<head>",
                    &format!("<head>\n<meta name=\"modul-id\" content=\"{}\">",
                        html_escape(&mid))
                );
                Html(injected)
            }
        }))
        .route("/api/config", axum::routing::get(get_config))
        .route("/api/chat", axum::routing::post(chat))
        .route("/api/home/{modul_id}", axum::routing::get(list_home))
        .route("/api/home/{modul_id}/{path}", axum::routing::get(read_home_file))
        .route("/api/home/{modul_id}/{path}", axum::routing::delete(delete_home_file))
        .route("/api/home-clear/{modul_id}", axum::routing::delete(clear_home))
        .route("/api/convos/{modul_id}", axum::routing::get(list_convos))
        .route("/api/convos/{modul_id}/{convo_id}", axum::routing::get(load_convo))
        .route("/api/convos/{modul_id}/{convo_id}", axum::routing::put(save_convo))
        .route("/api/convos/{modul_id}/{convo_id}", axum::routing::delete(delete_convo))
        .layer(axum::middleware::from_fn_with_state(auth_state, security::auth_middleware))
        .layer(DefaultBodyLimit::max(body_limit))
        .with_state(state)
}

fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
        .replace('"', "&quot;").replace('\'', "&#39;")
}

// ─── Config ────────────────────────────────────────

async fn get_config(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let cfg = s.config.read().await;
    let mut val = serde_json::to_value(&*cfg).unwrap_or_default();
    drop(cfg);
    security::redact_secrets(&mut val);
    Json(val)
}

async fn save_config(State(s): State<Arc<AppState>>, Json(mut incoming): Json<serde_json::Value>) -> Json<serde_json::Value> {
    // Globaler Config-Write-Lock: serialisiert den kompletten read-modify-write-
    // Zyklus gegen parallele Writes (Orchestrator run_cleanup, anderer
    // save_config-Request, wizard-commit). Ohne den Lock würde bei gleich-
    // zeitigen Edits last-write-wins gelten und Änderungen verloren gehen.
    let _write_guard = s.pipeline.config_write_lock.lock().await;

    // Restore any REDACTED placeholders from the existing on-disk config before parsing.
    let existing = {
        let cfg = s.config.read().await;
        serde_json::to_value(&*cfg).unwrap_or_default()
    };
    security::restore_redacted(&mut incoming, &existing);

    let mut cfg: AgentConfig = match serde_json::from_value(incoming) {
        Ok(c) => c,
        Err(e) => return Json(serde_json::json!({"ok": false, "error": format!("Config parse: {}", e)})),
    };

    // Validierung: Mindest-Intervall und Port-Bereich
    if cfg.cycle_interval_ms < 500 { cfg.cycle_interval_ms = 500; }
    if cfg.web_port == 0 { cfg.web_port = 8090; }
    if cfg.max_body_bytes < 4096 { cfg.max_body_bytes = 4096; }

    let path = s.pipeline.base.join("config.json");

    // Rotating backup: config.json.bak-1 (most recent) to bak-3 (oldest) before overwriting.
    // Prevents accidental key-wipe from a bad UI save; user can restore from backup manually.
    if path.exists() {
        let b3 = path.with_extension("json.bak-3");
        let b2 = path.with_extension("json.bak-2");
        let b1 = path.with_extension("json.bak-1");
        let _ = std::fs::remove_file(&b3);
        let _ = std::fs::rename(&b2, &b3);
        let _ = std::fs::rename(&b1, &b2);
        let _ = std::fs::copy(&path, &b1);
    }

    *s.config.write().await = cfg.clone();
    let cfg_json = match serde_json::to_string_pretty(&cfg) {
        Ok(j) => j,
        Err(e) => return Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    };
    match util::atomic_write(&path, cfg_json.as_bytes()) {
        Ok(_) => {
            s.pipeline.log("config", None, LogTyp::Info, "Config gespeichert (Backup rotiert)");
            s.pipeline.audit("config.update", "admin", "Configuration updated via API");
            Json(serde_json::json!({"ok": true}))
        }
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

async fn list_config_backups(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let base = s.pipeline.base.join("config.json");
    let mut slots = Vec::new();
    for slot in 1..=3 {
        let p = base.with_extension(format!("json.bak-{}", slot));
        if let Ok(meta) = std::fs::metadata(&p) {
            let modified = meta.modified().ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs() as i64).unwrap_or(0);
            slots.push(serde_json::json!({
                "slot": slot,
                "exists": true,
                "modified_ts": modified,
                "size_bytes": meta.len(),
            }));
        } else {
            slots.push(serde_json::json!({"slot": slot, "exists": false}));
        }
    }
    Json(serde_json::json!({"backups": slots}))
}

async fn restore_config_backup(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(slot): axum::extract::Path<u8>,
) -> Json<serde_json::Value> {
    if !(1..=3).contains(&slot) {
        return Json(serde_json::json!({"ok": false, "error": "slot must be 1, 2, or 3"}));
    }
    let _write_guard = s.pipeline.config_write_lock.lock().await;
    let base = s.pipeline.base.join("config.json");
    let bak = base.with_extension(format!("json.bak-{}", slot));
    if !bak.exists() {
        return Json(serde_json::json!({"ok": false, "error": format!("backup slot {} does not exist", slot)}));
    }
    let raw = match std::fs::read_to_string(&bak) {
        Ok(r) => r,
        Err(e) => return Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    };
    let cfg: AgentConfig = match serde_json::from_str(&raw) {
        Ok(c) => c,
        Err(e) => return Json(serde_json::json!({"ok": false, "error": format!("backup parse: {}", e)})),
    };
    // Rotate current config into bak-1, then write backup as new current
    if base.exists() {
        let b3 = base.with_extension("json.bak-3");
        let b2 = base.with_extension("json.bak-2");
        let b1 = base.with_extension("json.bak-1");
        let _ = std::fs::remove_file(&b3);
        let _ = std::fs::rename(&b2, &b3);
        let _ = std::fs::rename(&b1, &b2);
        let _ = std::fs::copy(&base, &b1);
    }
    let cfg_json = match serde_json::to_string_pretty(&cfg) {
        Ok(j) => j,
        Err(e) => return Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    };
    if let Err(e) = util::atomic_write(&base, cfg_json.as_bytes()) {
        return Json(serde_json::json!({"ok": false, "error": e.to_string()}));
    }
    *s.config.write().await = cfg;
    s.pipeline.log("config", None, LogTyp::Info, &format!("Config aus Backup slot {} wiederhergestellt", slot));
    s.pipeline.audit("config.restore", "admin", &format!("Configuration restored from backup slot {}", slot));
    Json(serde_json::json!({"ok": true, "slot": slot}))
}

// ─── Aufgaben ──────────────────────────────────────

async fn get_aufgaben(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "erstellt": s.pipeline.erstellt(),
        "gestartet": s.pipeline.gestartet(),
        "erledigt": s.pipeline.erledigt(),
    }))
}

// ─── Aufgaben Cancel / Edit ───────────────────────

async fn cancel_aufgabe(State(s): State<Arc<AppState>>, axum::extract::Path(id): axum::extract::Path<String>) -> Json<serde_json::Value> {
    let Some(id) = safe_id(&id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige ID"}));
    };
    match s.pipeline.laden_by_id(&id) {
        Ok(Some(mut a)) => {
            if a.status == AufgabeStatus::Success || a.status == AufgabeStatus::Failed || a.status == AufgabeStatus::Cancelled {
                return Json(serde_json::json!({"ok": false, "error": "Aufgabe ist bereits abgeschlossen"}));
            }
            a.ergebnis = Some("Cancelled by user".into());
            match s.pipeline.verschieben(&mut a, AufgabeStatus::Cancelled) {
                Ok(_) => {
                    s.pipeline.log("web", Some(&id), LogTyp::Info, "Aufgabe abgebrochen");
                    Json(serde_json::json!({"ok": true}))
                }
                Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
            }
        }
        Ok(None) => Json(serde_json::json!({"ok": false, "error": "Aufgabe nicht gefunden"})),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

async fn edit_aufgabe(State(s): State<Arc<AppState>>, axum::extract::Path(id): axum::extract::Path<String>, Json(body): Json<serde_json::Value>) -> Json<serde_json::Value> {
    let Some(id) = safe_id(&id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige ID"}));
    };
    let neue_anweisung = body["anweisung"].as_str().unwrap_or("");
    if neue_anweisung.is_empty() {
        return Json(serde_json::json!({"ok": false, "error": "Anweisung darf nicht leer sein"}));
    }
    match s.pipeline.laden_by_id(&id) {
        Ok(Some(mut a)) => {
            if a.status != AufgabeStatus::Erstellt {
                return Json(serde_json::json!({"ok": false, "error": "Nur wartende Aufgaben können bearbeitet werden"}));
            }
            a.update(neue_anweisung, "Edited via frontend");
            match s.pipeline.speichern(&a) {
                Ok(_) => {
                    s.pipeline.log("web", Some(&id), LogTyp::Info, &format!("Aufgabe bearbeitet: {}", neue_anweisung));
                    Json(serde_json::json!({"ok": true}))
                }
                Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
            }
        }
        Ok(None) => Json(serde_json::json!({"ok": false, "error": "Aufgabe nicht gefunden"})),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

// ─── Chat Dispatcher ──────────────────────────────
// Chat-LLM ist ein DISPATCHER. Zwei Modi:
//   1) Einfache Fragen/Tool-Calls → inline beantworten (schnell)
//   2) Grosse Aufgaben (Code, Analyse) → Aufgabe erstellen, Cycle erledigt es
// Jede Aktion wird als Aufgabe im Pool geloggt. Bei Fehler/Timeout geht nichts verloren.

async fn chat(
    State(s): State<Arc<AppState>>,
    axum::extract::ConnectInfo(addr): axum::extract::ConnectInfo<std::net::SocketAddr>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    // Rate limit per client IP
    if !s.rate_limit.check(addr.ip()).await {
        let (tx, rx) = tokio::sync::mpsc::channel::<String>(1);
        let _ = tx.try_send(serde_json::json!({"error": "Rate limit exceeded — zu viele Anfragen"}).to_string());
        let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
        let body = Body::from_stream(stream.map(|line| Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(format!("{}\n", line)))));
        return axum::response::Response::builder()
            .status(429)
            .header("content-type", "application/x-ndjson")
            .body(body)
            .unwrap_or_else(|_| axum::response::Response::new(Body::empty()));
    }

    // Daily USD budget hard-stop — peek only (non-reservierend), die echte
    // atomare Reservation passiert später im inneren LLM-Call-Pfad.
    {
        let cfg = s.config.read().await.clone();
        if let Err(msg) = peek_daily_budget(&s.pipeline.store.pool, &cfg).await {
            let (tx, rx) = tokio::sync::mpsc::channel::<String>(1);
            let _ = tx.try_send(serde_json::json!({"error": msg}).to_string());
            let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
            let body = Body::from_stream(stream.map(|line| Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(format!("{}\n", line)))));
            return axum::response::Response::builder()
                .status(402)
                .header("content-type", "application/x-ndjson")
                .body(body)
                .unwrap_or_else(|_| axum::response::Response::new(Body::empty()));
        }
    }

    let user_messages = body["messages"].clone();
    let modul_id_raw = body["modul"].as_str().unwrap_or("").to_string();
    let modul_id = if modul_id_raw.is_empty() {
        String::new()
    } else {
        match safe_id(&modul_id_raw) {
            Some(s) => s,
            None => {
                let (tx, rx) = tokio::sync::mpsc::channel::<String>(1);
                let _ = tx.try_send(serde_json::json!({"error": "Ungültige modul-ID"}).to_string());
                let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
                let body = Body::from_stream(stream.map(|line| Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(format!("{}\n", line)))));
                return axum::response::Response::builder()
                    .status(400)
                    .header("content-type", "application/x-ndjson")
                    .body(body)
                    .unwrap_or_else(|_| axum::response::Response::new(Body::empty()));
            }
        }
    };

    let cfg = s.config.read().await;
    let modul = if !modul_id.is_empty() {
        cfg.module.iter().find(|m| m.id == modul_id || m.name == modul_id).cloned()
    } else { None };

    let (backend_id, backup_id, system_prompt, modul_for_tools) = if let Some(m) = &modul {
        let mut tp = tools::tools_prompt(m);
        { let py_mods = s.py_modules.read().await; tools::append_python_tools(&mut tp, m, &py_mods); }
        let home = s.pipeline.home_dir(&m.id);
        let home_info = format!("\n\nDein Home-Verzeichnis ist: {}\nDu kannst dort Dateien lesen, schreiben und auflisten.\n\
            WICHTIG: Wenn der User eine grosse Aufgabe will (Code schreiben, Datei erstellen, Analyse), \
            dann erstelle ZUERST einen Plan und fuehre die Tools Schritt fuer Schritt aus.\n", home.display());
        let date_str = chrono::Utc::now().format("%d.%m.%Y %H:%M UTC").to_string();
        let identity = util::resolve_identity(m, &cfg);
        let system_with_date = identity.system_prompt.replace("{date}", &date_str);
        let full = format!("{}\n{}{}", system_with_date, tp, home_info);
        (m.llm_backend.clone(), m.backup_llm.clone(), full, Some(m.clone()))
    } else if let Some(b) = cfg.llm_backends.first() {
        (b.id.clone(), None, String::new(), None)
    } else {
        drop(cfg);
        let stream = tokio_stream::wrappers::ReceiverStream::new({
            let (tx, rx) = tokio::sync::mpsc::channel::<String>(1);
            let _ = tx.try_send(serde_json::json!({"error":"Kein LLM Backend konfiguriert"}).to_string());
            rx
        });
        let body = Body::from_stream(stream.map(|line| Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(format!("{}\n", line)))));
        return axum::response::Response::builder()
            .status(500)
            .header("content-type", "application/x-ndjson")
            .header("cache-control", "no-cache")
            .body(body)
            .unwrap_or_else(|_| axum::response::Response::new(Body::empty()));
    };

    let config_snapshot = cfg.clone();
    let gcfg = cfg.guardrail.clone().unwrap_or_default();
    drop(cfg);

    let py_mods_snap: Vec<crate::loader::PyModuleMeta> = s.py_modules.read().await.clone();

    let mut messages: Vec<serde_json::Value> = vec![];
    if !system_prompt.is_empty() {
        messages.push(serde_json::json!({"role": "system", "content": system_prompt}));
    }
    if let Some(arr) = user_messages.as_array() {
        messages.extend(arr.clone());
    }

    // OpenAI Function Calling: Tools als JSON-Schema
    let openai_tools = if let Some(ref m) = modul_for_tools {
        let py_mods = s.py_modules.read().await;
        tools::tools_as_openai_json(m, &py_mods)
    } else { vec![] };

    // Letzter User-Text fuer Aufgaben-Logging
    let last_user_msg = user_messages.as_array()
        .and_then(|a| a.last())
        .and_then(|m| m["content"].as_str())
        .unwrap_or("").to_string();

    // Haupt-Aufgabe erstellen (damit JEDER Chat-Request trackbar ist)
    let mut main_aufgabe = Aufgabe::llm_call(
        &last_user_msg, &modul_id, &format!("chat:{}", modul_id),
        None,  // NO routing for chat tasks -- result goes via HTTP stream
    );
    let main_id = main_aufgabe.id.clone();
    let _ = s.pipeline.speichern(&main_aufgabe);
    // Mark as gestartet immediately so scheduler doesn't also pick it up
    let _ = s.pipeline.verschieben(&mut main_aufgabe, AufgabeStatus::Gestartet);

    // Channel for streaming status updates and final answer
    let (tx, rx) = tokio::sync::mpsc::channel::<String>(64);

    // Spawn the tool-loop in a background task
    let state = s.clone();
    tokio::spawn(async move {
        let t_start = std::time::Instant::now();
        let mut tool_rounds = 0;
        let mut sub_aufgaben: Vec<String> = vec![];
        let mut messages = messages;
        let modul_id_str = modul_id.as_str();
        let mut guardrail_retries: u32 = 0;
        let mut used_fallback = false;
        let mut backend_id = backend_id;

        loop {
            if tool_rounds >= MAX_CHAT_TOOL_ROUNDS { break; }

            let result = state.llm.chat_with_tools(&backend_id, backup_id.as_deref(), &messages, &openai_tools).await;

            match result {
                Ok((response, raw_data)) => {
                    // Token-Tracking
                    track_tokens(&state.pipeline.store.pool, &state.tokens, &backend_id, "", modul_id_str, &raw_data).await;

                    // ── Guardrail validation ───────────────────────────────
                    if gcfg.enabled {
                        let chat_last_user = messages.iter().rev()
                            .find(|m| m["role"] == "user")
                            .and_then(|m| m["content"].as_str())
                            .map(|s| s.to_string());
                        let model_str = config_snapshot.llm_backends.iter()
                            .find(|b| b.id == backend_id)
                            .map(|b| b.model.clone())
                            .unwrap_or_default();
                        let max_retries_for_backend = gcfg.per_backend_overrides
                            .get(&backend_id).copied()
                            .unwrap_or(gcfg.max_retries);
                        let vctx = crate::guardrail::ValidatorContext {
                            modul_id: modul_id_str,
                            cfg: &config_snapshot,
                            py_modules: &py_mods_snap,
                            last_user_msg: chat_last_user.as_deref(),
                            strict_mode: gcfg.strict_mode,
                        };
                        match crate::guardrail::validate_response(&raw_data, &vctx) {
                            Ok(_parsed) => {
                                let ev = crate::types::GuardrailEvent {
                                    ts: chrono::Utc::now().timestamp(),
                                    modul: modul_id.clone(),
                                    backend: backend_id.clone(),
                                    model: model_str.clone(),
                                    tool_name: None,
                                    passed: true,
                                    errors: vec![],
                                    retry_attempt: guardrail_retries,
                                    final_outcome: if guardrail_retries > 0 { "retried".into() } else { "ok".into() },
                                    similar_suggestion: None,
                                };
                                let _ = crate::guardrail::log_event(&state.data_root, &ev).await;
                                guardrail_retries = 0;
                            }
                            Err(errors) => {
                                let is_last = guardrail_retries >= max_retries_for_backend;
                                let ev = crate::types::GuardrailEvent {
                                    ts: chrono::Utc::now().timestamp(),
                                    modul: modul_id.clone(),
                                    backend: backend_id.clone(),
                                    model: model_str.clone(),
                                    tool_name: None,
                                    passed: false,
                                    errors: errors.clone(),
                                    retry_attempt: guardrail_retries,
                                    final_outcome: if is_last { "hard_fail".into() } else { "retried".into() },
                                    similar_suggestion: None,
                                };
                                let _ = crate::guardrail::log_event(&state.data_root, &ev).await;
                                if is_last {
                                    // Check if backup_llm available + fallback flag on
                                    let mod_cfg = config_snapshot.module.iter().find(|m| m.id == modul_id);
                                    let backup_id = mod_cfg.and_then(|m| m.backup_llm.clone());
                                    if gcfg.fallback_on_hard_fail && backup_id.is_some() && !used_fallback {
                                        if let Some(bid) = backup_id {
                                            if let Some(bb) = config_snapshot.llm_backends.iter().find(|b| b.id == bid).cloned() {
                                                let codes: Vec<String> = errors.iter().map(|e| e.code.clone()).collect();
                                                let _ = crate::guardrail::log_fallback_event(&state.data_root, &backend_id, &bid, &modul_id, &codes).await;
                                                backend_id = bb.id.clone();
                                                used_fallback = true;
                                                guardrail_retries = 0;
                                                continue;  // retry with backup
                                            }
                                        }
                                    }
                                    // Real hard-fail — existing warn + break
                                    let codes: Vec<String> = errors.iter().map(|e| e.code.clone()).collect();
                                    tracing::warn!("Guardrail hard-fail in chat.{}: {:?}", modul_id, codes);
                                    tx.send(serde_json::json!({"type":"status","message":format!("Guardrail hard-fail: {}", codes.join(", "))}).to_string()).await.ok();
                                    break;
                                } else {
                                    let feedback = crate::guardrail::synth_feedback_user_message(
                                        &errors, max_retries_for_backend, guardrail_retries,
                                    );
                                    messages.push(serde_json::json!({"role": "user", "content": feedback}));
                                    guardrail_retries += 1;
                                    continue;
                                }
                            }
                        }
                    }
                    // ── End guardrail ──────────────────────────────────────

                    // Erst OpenAI tool_calls checken (Schema-basierte Param-Order wenn
                    // Modul bekannt), dann Fallback auf <tool> XML-Tags.
                    let tool_call = if raw_data != serde_json::Value::Null {
                        let tmp_name = tools::parse_openai_tool_call(&raw_data).map(|(n, _)| n);
                        match (tmp_name, modul_for_tools.as_ref()) {
                            (Some(name), Some(m)) => {
                                let schema = tools::schema_required_for(&name, m, &py_mods_snap);
                                tools::parse_openai_tool_call_with_schema(&raw_data, schema.as_deref())
                            }
                            (Some(_), None) => tools::parse_openai_tool_call(&raw_data),
                            (None, _) => None,
                        }
                    } else {
                        None
                    }.or_else(|| tools::parse_tool_call(&response));

                    if let Some((tool_name, params)) = tool_call {
                        tool_rounds += 1;

                        // Status: Tool wird ausgefuehrt
                        tx.send(serde_json::json!({"type":"status","message":format!("Tool: {}({})", tool_name, params.join(", "))}).to_string()).await.ok();

                        // Sub-Aufgabe fuer den Tool-Call
                        let mid = modul_for_tools.as_ref().map(|m| m.id.as_str()).unwrap_or(modul_id_str);
                        let sub = Aufgabe::direct(&tool_name, params.clone(), mid,
                            &format!("chat:{}", modul_id_str), None, None);
                        let sub_id = sub.id.clone();
                        let _ = state.pipeline.speichern(&sub);
                        sub_aufgaben.push(sub_id.clone());

                        state.pipeline.log(modul_id_str, Some(&main_id), LogTyp::Info,
                            &format!("Tool: {}({}) [{}]", tool_name, params.join(", "), &sub_id[..8]));

                        let tool_result = exec_tool_inline(&state, &tool_name, &params, mid, &config_snapshot).await;
                        let ok = tool_result.0;

                        // Sub-Aufgabe abschliessen
                        if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&sub_id) {
                            a.ergebnis = Some(tool_result.1.clone());
                            let _ = state.pipeline.verschieben(&mut a, if ok { AufgabeStatus::Success } else { AufgabeStatus::Failed });
                        }

                        state.pipeline.log(modul_id_str, Some(&main_id),
                            if ok { LogTyp::Success } else { LogTyp::Failed },
                            &format!("Tool {}: {} → {}", tool_name, if ok {"OK"} else {"FAIL"}, util::safe_truncate(&tool_result.1, 80)));

                        // Status: Tool-Ergebnis
                        tx.send(serde_json::json!({"type":"status","message":format!("{}: {}", if ok {"OK"} else {"FAIL"}, util::safe_truncate(&tool_result.1, 80))}).to_string()).await.ok();

                        // Tool-Result im OpenAI-Format
                        let call_id = raw_data.pointer("/choices/0/message/tool_calls/0/id")
                            .and_then(|v| v.as_str()).unwrap_or("call_0").to_string();
                        messages.push(serde_json::json!({"role": "assistant", "content": serde_json::Value::Null,
                            "tool_calls": [{"id": &call_id, "type": "function", "function": {"name": &tool_name, "arguments": "{}"}}]}));
                        messages.push(serde_json::json!({"role": "tool", "tool_call_id": &call_id,
                            "content": format!("{}: {}", if ok {"SUCCESS"} else {"FAILED"}, tool_result.1)}));

                        // History trimmen: alte Tool-Results kuerzen um Token-Explosion zu vermeiden
                        // Behalte nur die letzten 6 Messages (3 Tool-Rounds) vollstaendig
                        let keep_full = 6;
                        let system_msgs = 1; // System-Prompt
                        let user_msgs = user_messages.as_array().map(|a| a.len()).unwrap_or(0);
                        let fixed = system_msgs + user_msgs; // Diese nie anfassen
                        if messages.len() > fixed + keep_full + 4 {
                            // Alte Tool-Results auf 100 chars kuerzen
                            for i in fixed..(messages.len().saturating_sub(keep_full)) {
                                if messages[i].get("role").and_then(|v| v.as_str()) == Some("tool") {
                                    if let Some(content) = messages[i].get("content").and_then(|v| v.as_str()) {
                                        if content.len() > 100 {
                                            let short = format!("{}...[gekuerzt]", util::safe_truncate(content, 100));
                                            messages[i]["content"] = serde_json::json!(short);
                                        }
                                    }
                                }
                            }
                        }
                        continue;
                    }

                    // Pruefen: hat der User nach Recherche gefragt aber LLM hat kein Tool genutzt?
                    // Wenn ja: nochmal mit Hinweis dass Tools PFLICHT sind
                    if sub_aufgaben.is_empty() && tool_rounds == 0 {
                        let lower = last_user_msg.to_lowercase();
                        let needs_research = ["recherch", "such", "prüf", "check", "finde", "google",
                            "verify", "validier", "fakten", "stimmt das", "belege", "quelle"].iter()
                            .any(|kw| lower.contains(kw));
                        if needs_research {
                            messages.push(serde_json::json!({"role": "assistant", "content": response}));
                            messages.push(serde_json::json!({"role": "user", "content":
                                "STOPP — du hast KEIN Tool benutzt! Der User hat explizit nach Recherche gefragt. \
                                 Du MUSST duckduckgo.search nutzen um im Web zu suchen. Antworte NICHT aus deinem Wissen. \
                                 Mache MEHRERE Suchen um verschiedene Aspekte abzudecken."}));
                            tool_rounds += 1; // Zähle als Round damit wir nicht endlos loopen
                            continue;
                        }
                    }

                    // Finale Antwort — Haupt-Aufgabe abschliessen
                    let mut final_text = strip_tool_tags(&response);

                    // Wenn finale Antwort leer aber Tool-Ergebnisse vorhanden: letztes Ergebnis nutzen
                    if final_text.trim().is_empty() && !sub_aufgaben.is_empty() {
                        // Letztes Tool-Result aus Messages holen
                        for msg in messages.iter().rev() {
                            if msg.get("role").and_then(|v| v.as_str()) == Some("tool") {
                                if let Some(content) = msg.get("content").and_then(|v| v.as_str()) {
                                    final_text = content.to_string();
                                    break;
                                }
                            }
                        }
                        if final_text.trim().is_empty() {
                            final_text = format!("{} Tool-Calls ausgefuehrt. Ergebnis im Aufgaben-Board.", sub_aufgaben.len());
                        }
                    }

                    // Aufgaben-Info voranstellen wenn Tools genutzt wurden
                    if !sub_aufgaben.is_empty() {
                        final_text = format!("[{} Aufgabe(n) erstellt]\n\n{}", sub_aufgaben.len(), final_text);
                    }

                    if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&main_id) {
                        a.ergebnis = Some(util::safe_truncate(&final_text, 500).to_string());
                        let _ = state.pipeline.verschieben(&mut a, AufgabeStatus::Success);
                    }
                    let total_dur = t_start.elapsed();
                    state.pipeline.log(modul_id_str, Some(&main_id), LogTyp::Success,
                        &format!("Chat fertig ({} sub-aufgaben, {}ms)", sub_aufgaben.len(), total_dur.as_millis()));

                    // Stream final text in chunks
                    for chunk in final_text.chars().collect::<Vec<_>>().chunks(20) {
                        let text: String = chunk.iter().collect();
                        tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":text},"done":false}).to_string()).await.ok();
                    }
                    tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":""},"done":true,"eval_count":final_text.len(),"total_duration":total_dur.as_nanos() as u64}).to_string()).await.ok();
                    return;
                }
                Err(e) => {
                    // FEHLER — Aufgabe als Failed loggen, NICHT verloren
                    state.pipeline.log(modul_id_str, Some(&main_id), LogTyp::Failed, &format!("LLM Fehler: {}", e));
                    if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&main_id) {
                        a.ergebnis = Some(format!("FAILED: {}", e));
                        let _ = state.pipeline.verschieben(&mut a, AufgabeStatus::Failed);
                    }
                    tx.send(serde_json::json!({"error": format!("LLM Fehler: {}", e)}).to_string()).await.ok();
                    return;
                }
            }
        }
        // Max rounds — Aufgabe als Failed
        state.pipeline.log(modul_id_str, Some(&main_id), LogTyp::Warning, "Max tool rounds erreicht");
        if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&main_id) {
            a.ergebnis = Some("Max tool rounds erreicht".into());
            let _ = state.pipeline.verschieben(&mut a, AufgabeStatus::Failed);
        }
        tx.send(serde_json::json!({"error": "Max tool rounds erreicht"}).to_string()).await.ok();
    });

    let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
    let body = Body::from_stream(stream.map(|line| Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(format!("{}\n", line)))));

    axum::response::Response::builder()
        .header("content-type", "application/x-ndjson")
        .header("cache-control", "no-cache")
        .body(body)
        .unwrap_or_else(|_| axum::response::Response::new(Body::empty()))
}

/// Tool inline ausfuehren (Rust oder Python). Delegates to the unified dispatcher.
/// Kein task_id — Chat-Flow ist synchron und braucht keine Idempotency-
/// Deduplication (im Gegensatz zum Scheduler-Pfad mit Retry-Logik).
async fn exec_tool_inline(s: &Arc<AppState>, tool_name: &str, params: &[String], modul_id: &str, config: &AgentConfig) -> (bool, String) {
    let py_mods = s.py_modules.read().await;
    tools::exec_tool_unified(tool_name, params, modul_id, None, &s.pipeline, &s.llm, &py_mods, &s.py_pool, config).await
}

/// True SSE-style streaming chat. No tool calling — just text generation streamed from
/// the LLM. For chat UX that wants immediate character-by-character output without the
/// buffered tool-call loop overhead. Emits NDJSON lines with {"delta": "..."} per chunk.
async fn chat_stream_endpoint(
    State(s): State<Arc<AppState>>,
    axum::extract::ConnectInfo(addr): axum::extract::ConnectInfo<std::net::SocketAddr>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    if !s.rate_limit.check(addr.ip()).await {
        return error_response(429, "Rate limit exceeded");
    }

    let modul_id_raw = body["modul"].as_str().unwrap_or("").to_string();
    let modul_id = match safe_id(&modul_id_raw) {
        Some(s) => s,
        None if modul_id_raw.is_empty() => String::new(),
        None => return error_response(400, "Ungültige modul-ID"),
    };

    let cfg = s.config.read().await;
    let backend_id = if modul_id.is_empty() {
        cfg.llm_backends.first().map(|b| b.id.clone())
    } else {
        cfg.module.iter()
            .find(|m| m.id == modul_id || m.name == modul_id)
            .map(|m| m.llm_backend.clone())
    };
    drop(cfg);

    let Some(backend_id) = backend_id else {
        return error_response(500, "Kein LLM Backend");
    };

    let messages: Vec<serde_json::Value> = body["messages"].as_array()
        .cloned()
        .unwrap_or_default();

    let (tx, rx) = tokio::sync::mpsc::channel::<String>(64);
    let state = s.clone();
    let modul_id_owned = modul_id.clone();
    tokio::spawn(async move {
        let (chunk_tx, mut chunk_rx) = tokio::sync::mpsc::channel::<String>(64);
        let llm = state.llm.clone();
        let tokens = state.tokens.clone();
        let backend_for_stream = backend_id.clone();

        let stream_task = tokio::spawn(async move {
            llm.chat_stream(&backend_for_stream, &messages, chunk_tx).await
        });

        while let Some(part) = chunk_rx.recv().await {
            let line = serde_json::json!({"delta": part}).to_string();
            if tx.send(line).await.is_err() { break; }
        }

        match stream_task.await {
            Ok(Ok(full_text)) => {
                // Rough token estimate for tracking (4 chars ≈ 1 token)
                let est_tokens = (full_text.len() / 4) as u64;
                {
                    let mut stats = tokens.write().await;
                    stats.total_output += est_tokens;
                    stats.total_calls += 1;
                    stats.calls.push(TokenCall {
                        time: chrono::Utc::now().format("%H:%M:%S").to_string(),
                        backend: backend_id.clone(),
                        model: String::new(),
                        input_tokens: 0,
                        output_tokens: est_tokens,
                        modul: modul_id_owned.clone(),
                    });
                    let len = stats.calls.len();
                    if len > 200 { stats.calls.drain(0..len-200); }
                }
                let _ = tx.send(serde_json::json!({"done": true}).to_string()).await;
            }
            Ok(Err(e)) => {
                let _ = tx.send(serde_json::json!({"error": e}).to_string()).await;
            }
            Err(e) => {
                let _ = tx.send(serde_json::json!({"error": format!("stream task: {}", e)}).to_string()).await;
            }
        }
    });

    let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
    let body = Body::from_stream(stream.map(|line| Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(format!("{}\n", line)))));
    axum::response::Response::builder()
        .header("content-type", "application/x-ndjson")
        .header("cache-control", "no-cache")
        .body(body)
        .unwrap_or_else(|_| axum::response::Response::new(Body::empty()))
}

fn strip_tool_tags(text: &str) -> String {
    let mut result = text.to_string();
    while let Some(start) = result.find("<tool>") {
        if let Some(end) = result.find("</tool>") {
            result = format!("{}{}", &result[..start], &result[end + 7..]);
        } else {
            break;
        }
    }
    result.trim().to_string()
}

fn error_response(status: u16, msg: &str) -> axum::response::Response<Body> {
    axum::response::Response::builder()
        .status(status)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::json!({"error": msg}).to_string()))
        .unwrap_or_else(|_| axum::response::Response::new(Body::empty()))
}

// ─── Python Modules ───────────────────────────────

async fn get_py_modules(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    // Rust built-in Module
    let rust_modules = vec![
        serde_json::json!({
            "name": "chat", "description": "Chat-Interface mit Tool-Calling", "version": "built-in", "source": "rust",
            "settings": {"port":{"type":"number","label":"Port","default":8091}},
            "tools": [{"name":"rag.suchen","description":"Durchsucht das Wissens-Archiv","params":["query"]},
                      {"name":"rag.speichern","description":"Speichert im Wissens-Archiv","params":["text"]},
                      {"name":"aufgaben.erstellen","description":"Erstellt eine Aufgabe","params":["modul","anweisung","wann"]}]
        }),
        // "mail" Rust-Modul entfernt — IMAP/SMTP/POP3 sind jetzt Python-Module
        serde_json::json!({
            "name": "filesystem", "description": "Dateisystem-Zugriff (lesen/schreiben/listen)", "version": "built-in", "source": "rust",
            "settings": {"allowed_paths":{"type":"list","label":"Erlaubte Pfade","default":[]},
                         "max_file_size":{"type":"number","label":"Max Dateigröße (bytes)","default":4000},
                         "allow_write":{"type":"bool","label":"Schreibzugriff","default":true}},
            "tools": [{"name":"files.read","description":"Liest eine Datei","params":["path"]},
                      {"name":"files.write","description":"Schreibt eine Datei","params":["path","content"]},
                      {"name":"files.list","description":"Listet ein Verzeichnis","params":["path"]}]
        }),
        serde_json::json!({
            "name": "websearch", "description": "Web-Suche (DuckDuckGo, Brave, Google, Grok)", "version": "built-in", "source": "rust",
            "settings": {"search_engine":{"type":"select","label":"Suchmaschine","default":"duckduckgo","options":["duckduckgo","brave","serper","google","grok"]},
                         "brave_api_key":{"type":"password","label":"Brave API Key","default":""},
                         "serper_api_key":{"type":"password","label":"Serper API Key","default":""},
                         "google_api_key":{"type":"password","label":"Google API Key","default":""},
                         "google_cx":{"type":"string","label":"Google CX","default":""},
                         "grok_api_key":{"type":"password","label":"Grok API Key","default":""},
                         "max_results":{"type":"number","label":"Max Ergebnisse","default":8}},
            "tools": [{"name":"web.search","description":"Web-Suche","params":["query"]},
                      {"name":"http.get","description":"URL abrufen","params":["url"]}]
        }),
        serde_json::json!({
            "name": "shell", "description": "Shell-Befehle ausfuehren (Whitelist)", "version": "built-in", "source": "rust",
            "settings": {"allowed_commands":{"type":"list","label":"Erlaubte Befehle","default":[]},
                         "working_dir":{"type":"string","label":"Arbeitsverzeichnis","default":"."}},
            "tools": [{"name":"shell.exec","description":"Fuehrt einen Befehl aus","params":["command"]}]
        }),
        serde_json::json!({
            "name": "notify", "description": "Push-Benachrichtigungen (ntfy/gotify/telegram)", "version": "built-in", "source": "rust",
            "settings": {"notify_type":{"type":"select","label":"Typ","default":"ntfy","options":["ntfy","gotify","telegram"]},
                         "notify_url":{"type":"string","label":"URL","default":""},
                         "notify_token":{"type":"password","label":"Token","default":""},
                         "notify_topic":{"type":"string","label":"Topic/Chat-ID","default":"agent"}},
            "tools": [{"name":"notify.send","description":"Sendet eine Benachrichtigung","params":["message"]}]
        }),
    ];

    // Python-Module
    let py_mods = s.py_modules.read().await;
    let py_list: Vec<serde_json::Value> = py_mods.iter().map(|m| {
        serde_json::json!({
            "name": m.name,
            "description": m.description,
            "version": m.version,
            "settings": m.settings,
            "tools": m.tools,
            "source": "python",
        })
    }).collect();

    let mut all = rust_modules;
    all.extend(py_list);
    Json(serde_json::json!({"modules": all}))
}

// ─── Home Directory Explorer ──────────────────────

async fn list_home(State(s): State<Arc<AppState>>, axum::extract::Path(modul_id): axum::extract::Path<String>) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"error": "Ungültige modul-ID", "files": []}));
    };
    let home = s.pipeline.home_dir(&modul_id);
    list_dir_recursive(&home, &home, 0)
}

fn list_dir_recursive(base: &std::path::Path, dir: &std::path::Path, depth: u32) -> Json<serde_json::Value> {
    if depth > 3 { return Json(serde_json::json!({"files": []})); } // Max 3 Ebenen
    let mut files = vec![];
    if let Ok(entries) = std::fs::read_dir(dir) {
        let mut entries: Vec<_> = entries.flatten().collect();
        entries.sort_by_key(|e| e.file_name());
        for entry in entries {
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().to_string();
            let rel = path.strip_prefix(base).unwrap_or(&path).to_string_lossy().to_string();
            if path.is_dir() {
                let children = list_dir_recursive(base, &path, depth + 1);
                let children_val: serde_json::Value = serde_json::to_string(&children.0)
                    .ok()
                    .and_then(|s| serde_json::from_str(&s).ok())
                    .unwrap_or_default();
                files.push(serde_json::json!({"name": name, "path": rel, "type": "dir", "children": children_val["files"]}));
            } else {
                let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
                files.push(serde_json::json!({"name": name, "path": rel, "type": "file", "size": size}));
            }
        }
    }
    Json(serde_json::json!({"home": dir.to_string_lossy(), "files": files}))
}

async fn read_home_file(State(s): State<Arc<AppState>>, axum::extract::Path((modul_id, path)): axum::extract::Path<(String, String)>) -> impl IntoResponse {
    let Some(modul_id) = safe_id(&modul_id) else {
        return error_response(400, "Ungültige modul-ID");
    };
    let Some(path) = safe_relative_path(&path) else {
        return error_response(400, "Ungültiger Pfad");
    };
    let home = s.pipeline.home_dir(&modul_id);
    let file_path = home.join(&path);
    // Security: muss im Home bleiben
    let canonical = match std::fs::canonicalize(&file_path) {
        Ok(p) => p,
        Err(_) => return error_response(404, "Datei nicht gefunden"),
    };
    let home_canonical = std::fs::canonicalize(&home).unwrap_or(home);
    if !canonical.starts_with(&home_canonical) {
        return error_response(403, "Zugriff verweigert");
    }
    match std::fs::read(&canonical) {
        Ok(content) => {
            // Content-Type erraten
            let ext = file_path.extension().and_then(|e| e.to_str()).unwrap_or("");
            let ct = match ext {
                "html" | "htm" => "text/html; charset=utf-8",
                "css" => "text/css",
                "js" => "application/javascript",
                "json" => "application/json",
                "txt" | "md" | "log" => "text/plain; charset=utf-8",
                "png" => "image/png",
                "jpg" | "jpeg" => "image/jpeg",
                "svg" => "image/svg+xml",
                "pdf" => "application/pdf",
                _ => "application/octet-stream",
            };
            axum::response::Response::builder()
                .header("content-type", ct)
                .body(Body::from(content))
                .unwrap_or_else(|_| error_response(500, "Interner Fehler"))
        }
        Err(_) => error_response(404, "Datei nicht gefunden"),
    }
}

async fn delete_home_file(State(s): State<Arc<AppState>>, axum::extract::Path((modul_id, path)): axum::extract::Path<(String, String)>) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige modul-ID"}));
    };
    let Some(path) = safe_relative_path(&path) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültiger Pfad"}));
    };
    let home = s.pipeline.home_dir(&modul_id);
    let file_path = home.join(&path);
    let canonical = match std::fs::canonicalize(&file_path) {
        Ok(p) => p, Err(_) => return Json(serde_json::json!({"ok": false, "error": "Datei nicht gefunden"})),
    };
    let home_canonical = std::fs::canonicalize(&home).unwrap_or(home);
    if !canonical.starts_with(&home_canonical) {
        return Json(serde_json::json!({"ok": false, "error": "Zugriff verweigert"}));
    }
    match std::fs::remove_file(&canonical) {
        Ok(_) => Json(serde_json::json!({"ok": true})),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

async fn clear_home(State(s): State<Arc<AppState>>, axum::extract::Path(modul_id): axum::extract::Path<String>) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige modul-ID"}));
    };
    let home = s.pipeline.home_dir(&modul_id);
    let mut deleted = 0;
    fn remove_recursive(dir: &std::path::Path, deleted: &mut i32) {
        if let Ok(entries) = std::fs::read_dir(dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                let name = entry.file_name().to_string_lossy().to_string();
                // .taskloops Ordner behalten (Loop-State)
                if name == ".taskloops" { continue; }
                if path.is_dir() {
                    std::fs::remove_dir_all(&path).ok();
                    *deleted += 1;
                } else {
                    std::fs::remove_file(&path).ok();
                    *deleted += 1;
                }
            }
        }
    }
    remove_recursive(&home, &mut deleted);
    Json(serde_json::json!({"ok": true, "deleted": deleted}))
}

// ─── Python Module Config ─────────────────────────

async fn get_module_config(State(s): State<Arc<AppState>>, axum::extract::Path(name): axum::extract::Path<String>) -> Json<serde_json::Value> {
    let Some(name) = safe_id(&name) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültiger Modul-Name"}));
    };
    let modules_dir = s.pipeline.base.parent().unwrap_or(&s.pipeline.base).join("modules").join(&name);
    let cfg_path = modules_dir.join("config.json");
    if cfg_path.exists() {
        match std::fs::read_to_string(&cfg_path) {
            Ok(content) => {
                let val: serde_json::Value = serde_json::from_str(&content).unwrap_or_default();
                Json(serde_json::json!({"ok": true, "config": val}))
            }
            Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
        }
    } else {
        Json(serde_json::json!({"ok": true, "config": {}}))
    }
}

async fn save_module_config(State(s): State<Arc<AppState>>, axum::extract::Path(name): axum::extract::Path<String>, Json(body): Json<serde_json::Value>) -> Json<serde_json::Value> {
    let Some(name) = safe_id(&name) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültiger Modul-Name"}));
    };
    let modules_dir = s.pipeline.base.parent().unwrap_or(&s.pipeline.base).join("modules").join(&name);
    if !modules_dir.exists() {
        return Json(serde_json::json!({"ok": false, "error": "Modul nicht gefunden"}));
    }
    let cfg_path = modules_dir.join("config.json");
    let json = match serde_json::to_string_pretty(&body) {
        Ok(j) => j,
        Err(e) => return Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    };
    match util::atomic_write(&cfg_path, json.as_bytes()) {
        Ok(_) => Json(serde_json::json!({"ok": true})),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

// ─── LLM Models (live vom Backend abrufen) ────────

async fn list_llm_models(State(s): State<Arc<AppState>>, axum::extract::Path(backend_id): axum::extract::Path<String>) -> Json<serde_json::Value> {
    let Some(backend_id) = safe_id(&backend_id) else {
        return Json(serde_json::json!({"error": "Ungültige backend-ID", "models": []}));
    };
    let cfg = s.config.read().await;
    let backend = cfg.llm_backends.iter().find(|b| b.id == backend_id).cloned();
    drop(cfg);

    let Some(backend) = backend else {
        return Json(serde_json::json!({"error": "Backend nicht gefunden", "models": []}));
    };

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .unwrap_or_else(|_| reqwest::Client::new());

    let result = match backend.typ {
        crate::types::LlmTyp::Ollama => {
            // GET /api/tags → models[].name
            match client.get(format!("{}/api/tags", backend.url)).send().await {
                Ok(resp) => {
                    let data: serde_json::Value = resp.json().await.unwrap_or_default();
                    let models: Vec<String> = data["models"].as_array()
                        .map(|arr| arr.iter().filter_map(|m| m["name"].as_str().map(String::from)).collect())
                        .unwrap_or_default();
                    models
                }
                Err(e) => return Json(serde_json::json!({"error": e.to_string(), "models": []})),
            }
        }
        crate::types::LlmTyp::OpenAICompat | crate::types::LlmTyp::Grok => {
            // GET /v1/models → data[].id
            let key = backend.api_key.as_deref().unwrap_or("");
            match client.get(format!("{}/v1/models", backend.url))
                .header("Authorization", format!("Bearer {}", key))
                .send().await {
                Ok(resp) => {
                    let data: serde_json::Value = resp.json().await.unwrap_or_default();
                    let models: Vec<String> = data["data"].as_array()
                        .map(|arr| arr.iter().filter_map(|m| m["id"].as_str().map(String::from)).collect())
                        .unwrap_or_default();
                    models
                }
                Err(e) => return Json(serde_json::json!({"error": e.to_string(), "models": []})),
            }
        }
        crate::types::LlmTyp::Anthropic => {
            // GET /v1/models mit x-api-key Header
            let key = backend.api_key.as_deref().unwrap_or("");
            match client.get(format!("{}/v1/models", backend.url))
                .header("x-api-key", key)
                .header("anthropic-version", "2023-06-01")
                .send().await {
                Ok(resp) => {
                    let data: serde_json::Value = resp.json().await.unwrap_or_default();
                    data["data"].as_array()
                        .map(|arr| arr.iter().filter_map(|m| m["id"].as_str().map(String::from)).collect())
                        .unwrap_or_default()
                }
                Err(e) => return Json(serde_json::json!({"error": e.to_string(), "models": []})),
            }
        }
        crate::types::LlmTyp::Embedding => vec![],
    };

    Json(serde_json::json!({"models": result}))
}

// ─── Token Tracking ───────────────────────────────

async fn get_tokens(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let stats = s.tokens.read().await;
    Json(serde_json::json!({
        "total_input": stats.total_input,
        "total_output": stats.total_output,
        "total_calls": stats.total_calls,
        "total_tokens": stats.total_input + stats.total_output,
        "cost_usd_total": stats.cost_usd_total,
        "cost_usd_today": stats.cost_usd_today,
        "day_started_ts": stats.day_started_ts,
        "recent": stats.calls.iter().rev().take(50).collect::<Vec<_>>(),
    }))
}

/// Token-Usage aus einem API-Response extrahieren und persistent tracken.
/// Schreibt in die SQLite `token_stats`-Tabelle (transaktional mit Reservation-
/// Release) + spiegelt in den in-memory TokenTracker für UI-Live-Anzeige.
pub async fn track_tokens(store_pool: &crate::store::SqlitePool, tokens: &TokenTracker,
    backend_id: &str, model: &str, modul: &str, raw: &serde_json::Value)
{
    // Backend-spezifische Token-Formate:
    //   OpenAI/Grok: usage.prompt_tokens + usage.completion_tokens
    //   Ollama:      prompt_eval_count + eval_count (top-level)
    //   Anthropic:   usage.input_tokens + cache_read/creation + output_tokens
    // Anthropic trennt cached input (10% Kosten) von regulärem — wir tracken
    // alle als "input" fürs Display, Cost-Berechnung unten nutzt 10%-Rabatt
    // auf cache_read.
    let prompt_tokens = raw.pointer("/usage/prompt_tokens").and_then(|v| v.as_u64())
        .or_else(|| raw.pointer("/prompt_eval_count").and_then(|v| v.as_u64()))
        .or_else(|| raw.pointer("/usage/input_tokens").and_then(|v| v.as_u64()))
        .unwrap_or(0);
    let cache_read = raw.pointer("/usage/cache_read_input_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
    let cache_create = raw.pointer("/usage/cache_creation_input_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
    let input = prompt_tokens + cache_read + cache_create;
    let output = raw.pointer("/usage/completion_tokens").and_then(|v| v.as_u64())
        .or_else(|| raw.pointer("/eval_count").and_then(|v| v.as_u64()))
        .or_else(|| raw.pointer("/usage/output_tokens").and_then(|v| v.as_u64()))
        .unwrap_or(0);

    if input == 0 && output == 0 { return; }

    // Cost: regulärer Input voll, cached reads 10%, cache-writes 125% (25% extra),
    // Output voll. Anthropic's tatsächliche Rates laut Docs.
    let cost_usd = model_price_per_1k(model)
        .map(|(ip, op)| {
            (prompt_tokens as f64 / 1000.0) * ip
                + (cache_read as f64 / 1000.0) * ip * 0.10
                + (cache_create as f64 / 1000.0) * ip * 1.25
                + (output as f64 / 1000.0) * op
        })
        .unwrap_or(0.0);

    // Persistent: committed += actual, reserved -= reservation, alles atomar in einer
    // SQL-Transaktion. Überlebt Prozess-Restart — Daily-Cap gilt über Uptime hinweg.
    // Reservation-Betrag basierend auf model-price (muss mit check_daily_budget matchen).
    let reservation = reservation_for_model(model);
    if let Err(e) = crate::store::token_commit_actual(
        store_pool, reservation, cost_usd, input, output, backend_id, model, modul,
    ) {
        tracing::warn!("track_tokens: store commit failed: {}", e);
    }

    // In-memory UI-Spiegel aktualisieren (async kompatibel). Die SQLite-Werte sind
    // Wahrheit; dieser Cache wird nur für schnelle Dashboard-Renders gehalten.
    let mut stats = tokens.write().await;
    let now = chrono::Utc::now();
    let today_start = now.date_naive().and_hms_opt(0, 0, 0)
        .and_then(|dt| dt.and_utc().timestamp().checked_add(0))
        .unwrap_or(0);
    if stats.day_started_ts != today_start {
        stats.cost_usd_today = 0.0;
        stats.day_started_ts = today_start;
    }
    stats.total_input += input;
    stats.total_output += output;
    stats.total_calls += 1;
    stats.cost_usd_total += cost_usd;
    stats.cost_usd_today += cost_usd;
    stats.reserved_usd = (stats.reserved_usd - reservation).max(0.0);
    stats.reserved_calls = stats.reserved_calls.saturating_sub(1);
    stats.calls.push(TokenCall {
        time: now.format("%H:%M:%S").to_string(),
        backend: backend_id.into(),
        model: model.into(),
        input_tokens: input,
        output_tokens: output,
        modul: modul.into(),
    });
    let len = stats.calls.len();
    if len > 200 { stats.calls.drain(0..len-200); }
}

/// Pre-call budget check + Reservation. Atomar in SQLite: SELECT+UPDATE
/// unter `BEGIN IMMEDIATE`. Wenn der Call passen würde (committed + reserved +
/// estimated <= budget), wird die Reservation sofort gebucht — nachfolgende
/// parallele Calls sehen sie im nächsten Check. Persistent über Prozess-
/// Restarts (SQLite-Tabelle statt in-memory).
///
/// Callers müssen `release_reservation` aufrufen wenn der LLM-Call fehlschlägt.
/// Bei erfolg macht `track_tokens` die Gegenbuchung (release + actual commit
/// in einer Transaktion).
/// Non-reservierender Budget-Check — nur Pre-Flight-Sanity ("ist Tag ausgegeben?").
/// NICHT gefolgt von track_tokens oder release_reservation; nutzen für
/// Request-Entry-Points die SPÄTER check_daily_budget rufen werden und sonst
/// doppelt reservieren würden. GLM-konforme Lösung gegen Reservation-Leak.
pub async fn peek_daily_budget(
    store_pool: &crate::store::SqlitePool,
    cfg: &AgentConfig,
) -> Result<(), String> {
    let budget = match cfg.daily_budget_usd {
        Some(b) if b > 0.0 => b,
        _ => return Ok(()),
    };
    match crate::store::token_day_get(store_pool) {
        Ok(day) => {
            if day.cost_usd + day.reserved_usd >= budget {
                Err(format!(
                    "Daily USD budget erreicht: committed ${:.4} + reserved ${:.4} >= ${:.2}",
                    day.cost_usd, day.reserved_usd, budget
                ))
            } else { Ok(()) }
        }
        Err(e) => Err(format!("peek_daily_budget store error: {}", e)),
    }
}

pub async fn check_daily_budget(
    store_pool: &crate::store::SqlitePool,
    tokens: &TokenTracker,
    cfg: &AgentConfig,
    model: &str,
) -> Result<(), String> {
    let budget = cfg.daily_budget_usd.filter(|b| *b > 0.0);
    let reservation = reservation_for_model(model);
    match crate::store::token_reserve(store_pool, reservation, budget) {
        Ok(Ok(_)) => {
            let mut stats = tokens.write().await;
            stats.reserved_usd += reservation;
            stats.reserved_calls += 1;
            Ok(())
        }
        Ok(Err(msg)) => Err(msg),
        Err(e) => Err(format!("budget check store error: {}", e)),
    }
}

/// Reservation zurückbuchen — nur aufrufen wenn der LLM-Call fehlschlug
/// UND `track_tokens` nicht aufgerufen wird. Bei erfolgreichem Call nimmt
/// `track_tokens` die Abbuchung selbst vor.
pub async fn release_reservation(
    store_pool: &crate::store::SqlitePool,
    tokens: &TokenTracker,
    cfg: &AgentConfig,
    model: &str,
) {
    if cfg.daily_budget_usd.is_none_or(|b| b <= 0.0) { return; }
    let reservation = reservation_for_model(model);
    let _ = crate::store::token_release_reservation(store_pool, reservation);
    let mut stats = tokens.write().await;
    stats.reserved_usd = (stats.reserved_usd - reservation).max(0.0);
    stats.reserved_calls = stats.reserved_calls.saturating_sub(1);
}

// ─── Conversations ────────────────────────────────

async fn list_convos(State(s): State<Arc<AppState>>, axum::extract::Path(modul_id): axum::extract::Path<String>) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"conversations": [], "error": "Ungültige modul-ID"}));
    };
    Json(serde_json::json!({"conversations": s.pipeline.convo_list(&modul_id)}))
}

async fn load_convo(State(s): State<Arc<AppState>>, axum::extract::Path((modul_id, convo_id)): axum::extract::Path<(String, String)>) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"error": "Ungültige modul-ID"}));
    };
    let Some(convo_id) = safe_id(&convo_id) else {
        return Json(serde_json::json!({"error": "Ungültige convo-ID"}));
    };
    match s.pipeline.convo_load(&modul_id, &convo_id) {
        Some(c) => Json(c),
        None => Json(serde_json::json!({"error": "Conversation nicht gefunden"})),
    }
}

async fn save_convo(State(s): State<Arc<AppState>>, axum::extract::Path((modul_id, convo_id)): axum::extract::Path<(String, String)>, Json(mut body): Json<serde_json::Value>) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige modul-ID"}));
    };
    let Some(convo_id) = safe_id(&convo_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige convo-ID"}));
    };
    // Force the id in body to match the path, preventing the body from picking the filename
    body["id"] = serde_json::Value::String(convo_id.clone());
    match s.pipeline.convo_save(&modul_id, &body) {
        Ok(_) => Json(serde_json::json!({"ok": true})),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

async fn delete_convo(State(s): State<Arc<AppState>>, axum::extract::Path((modul_id, convo_id)): axum::extract::Path<(String, String)>) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige modul-ID"}));
    };
    let Some(convo_id) = safe_id(&convo_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige convo-ID"}));
    };
    match s.pipeline.convo_delete(&modul_id, &convo_id) {
        Ok(_) => Json(serde_json::json!({"ok": true})),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

// ─── Prompt Preview ───────────────────────────────

async fn prompt_preview(State(s): State<Arc<AppState>>, axum::extract::Path(modul_id): axum::extract::Path<String>) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"error": "Ungültige modul-ID"}));
    };
    let cfg = s.config.read().await;
    let modul = cfg.module.iter().find(|m| m.id == modul_id).cloned();
    drop(cfg);

    let Some(modul) = modul else {
        return Json(serde_json::json!({"error": "Modul nicht gefunden"}));
    };

    let identity = {
        let cfg2 = s.config.read().await;
        util::resolve_identity(&modul, &cfg2)
    };
    let system_raw = &identity.system_prompt;
    let mut tools_section = tools::tools_prompt(&modul);
    { let py_mods = s.py_modules.read().await; tools::append_python_tools(&mut tools_section, &modul, &py_mods); }
    let home = s.pipeline.home_dir(&modul.id);
    let home_section = format!("Dein Home-Verzeichnis ist: {}", home.display());
    let date_section = chrono::Utc::now().format("%d.%m.%Y %H:%M UTC").to_string();
    let full = format!("{}\n{}\n{}\nDatum: {}", system_raw.replace("{date}", &date_section), tools_section, home_section, date_section);
    let estimated_tokens = full.len() / 4;

    Json(serde_json::json!({
        "system_prompt_raw": system_raw,
        "tools_section": tools_section,
        "home_section": home_section,
        "date_section": date_section,
        "full_assembled": full,
        "estimated_tokens": estimated_tokens,
    }))
}

// ─── Logs ──────────────────────────────────────────

async fn get_logs(State(s): State<Arc<AppState>>, axum::extract::Path(datum): axum::extract::Path<String>) -> Json<Vec<LogEvent>> {
    // Date format: YYYY-MM-DD
    if !datum.chars().all(|c| c.is_ascii_digit() || c == '-') || datum.len() > 10 {
        return Json(vec![]);
    }
    Json(s.pipeline.logs_laden(&datum))
}

async fn get_template(State(s): State<Arc<AppState>>, axum::extract::Path(typ): axum::extract::Path<String>) -> Json<serde_json::Value> {
    let Some(typ) = safe_id(&typ) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültiger Template-Name"}));
    };
    let templates_dir = s.pipeline.base.parent().unwrap_or(&s.pipeline.base).join("modules").join("templates");
    let path = templates_dir.join(format!("{}.txt", typ));
    match std::fs::read_to_string(&path) {
        Ok(content) => Json(serde_json::json!({"ok": true, "template": content})),
        Err(_) => Json(serde_json::json!({"ok": false, "error": format!("Template '{}' nicht gefunden", typ)})),
    }
}

async fn get_metrics(State(s): State<Arc<AppState>>) -> impl IntoResponse {
    let tokens = s.tokens.read().await;
    let now = chrono::Utc::now().timestamp() as u64;
    let hb = s.heartbeats.read().await;
    let alive_schedulers = hb.iter().filter(|(_, t)| **t > 0 && now - **t < 120).count();
    let total_schedulers = hb.len();
    drop(hb);

    let erstellt = s.pipeline.erstellt().len();
    let gestartet = s.pipeline.gestartet().len();
    let erledigt_count = std::fs::read_dir(s.pipeline.base.join("erledigt"))
        .map(|e| e.count())
        .unwrap_or(0);

    let body = format!(
        "# HELP agent_tokens_input_total Total input tokens consumed\n\
         # TYPE agent_tokens_input_total counter\n\
         agent_tokens_input_total {}\n\
         # HELP agent_tokens_output_total Total output tokens consumed\n\
         # TYPE agent_tokens_output_total counter\n\
         agent_tokens_output_total {}\n\
         # HELP agent_llm_calls_total Total LLM API calls\n\
         # TYPE agent_llm_calls_total counter\n\
         agent_llm_calls_total {}\n\
         # HELP agent_schedulers_alive Number of schedulers with recent heartbeat\n\
         # TYPE agent_schedulers_alive gauge\n\
         agent_schedulers_alive {}\n\
         # HELP agent_schedulers_total Total number of registered schedulers\n\
         # TYPE agent_schedulers_total gauge\n\
         agent_schedulers_total {}\n\
         # HELP agent_tasks_pending Tasks in erstellt/\n\
         # TYPE agent_tasks_pending gauge\n\
         agent_tasks_pending {}\n\
         # HELP agent_tasks_running Tasks in gestartet/\n\
         # TYPE agent_tasks_running gauge\n\
         agent_tasks_running {}\n\
         # HELP agent_tasks_completed Tasks in erledigt/\n\
         # TYPE agent_tasks_completed counter\n\
         agent_tasks_completed {}\n",
        tokens.total_input, tokens.total_output, tokens.total_calls,
        alive_schedulers, total_schedulers,
        erstellt, gestartet, erledigt_count,
    );

    axum::response::Response::builder()
        .header("content-type", "text/plain; version=0.0.4")
        .body(Body::from(body))
        .unwrap_or_else(|_| axum::response::Response::new(Body::empty()))
}

// ─── Status / Heartbeat ───────────────────────────

async fn get_status(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let now = chrono::Utc::now().timestamp() as u64;
    let hb = s.heartbeats.read().await;
    let mut schedulers = serde_json::Map::new();
    for (id, ts) in hb.iter() {
        let diff = if *ts > 0 { now - ts } else { 0 };
        schedulers.insert(id.clone(), serde_json::json!({
            "last_beat": ts, "since_s": diff, "alive": diff < 120
        }));
    }
    drop(hb);
    let aufgaben = s.pipeline.erstellt().len() + s.pipeline.gestartet().len();
    let erledigt_count = std::fs::read_dir(s.pipeline.base.join("erledigt"))
        .map(|entries| entries.count())
        .unwrap_or(0);
    let busy = s.busy.read().await;
    let busy_map: serde_json::Value = serde_json::to_value(&*busy).unwrap_or_default();
    Json(serde_json::json!({
        "schedulers": schedulers,
        "aufgaben_wartend": aufgaben,
        "aufgaben_erledigt": erledigt_count,
        "busy": busy_map,
    }))
}

// ─── Cron Trigger ─────────────────────────────────

async fn trigger_cron(State(s): State<Arc<AppState>>, axum::extract::Path(cron_id): axum::extract::Path<String>) -> Json<serde_json::Value> {
    let Some(cron_id) = safe_id(&cron_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige cron-ID"}));
    };
    let cfg = s.config.read().await;
    let modul = cfg.module.iter().find(|m| m.id == cron_id && m.typ == "cron").cloned();
    drop(cfg);

    let Some(modul) = modul else {
        return Json(serde_json::json!({"ok": false, "error": "Cron module not found"}));
    };

    let cron_typ = modul.settings.cron_typ.as_deref().unwrap_or("direct");

    match cron_typ {
        "direct" => {
            if let Some(ref tool) = modul.settings.cron_tool {
                let params = modul.settings.cron_params.clone().unwrap_or_default();
                let target = modul.settings.target_modul.as_deref().unwrap_or(&modul.id);
                let aufgabe = crate::types::Aufgabe::direct(tool, params, target, &modul.id, None, None);
                let id = aufgabe.id.clone();
                match s.pipeline.speichern(&aufgabe) {
                    Ok(_) => {
                        s.pipeline.log("cron", Some(&id), crate::types::LogTyp::Info,
                            &format!("Manual trigger: {}", modul.id));
                        Json(serde_json::json!({"ok": true, "task_id": id}))
                    }
                    Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
                }
            } else {
                Json(serde_json::json!({"ok": false, "error": "No cron_tool configured"}))
            }
        }
        "llm" => {
            let target = modul.settings.target_modul.as_deref().unwrap_or(&modul.id);
            let anweisung = modul.settings.cron_anweisung.as_deref().unwrap_or("Cron task");
            let aufgabe = crate::types::Aufgabe::llm_call(anweisung, target, &modul.id, None);
            let id = aufgabe.id.clone();
            match s.pipeline.speichern(&aufgabe) {
                Ok(_) => Json(serde_json::json!({"ok": true, "task_id": id})),
                Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
            }
        }
        "chain" => {
            if let Some(ref chain) = modul.settings.chain {
                let chain_json = serde_json::to_string(chain).unwrap_or_default();
                let target = modul.settings.target_modul.as_deref().unwrap_or(&modul.id);
                let mut aufgabe = crate::types::Aufgabe::direct("__chain__", vec![chain_json], target, &modul.id, None, None);
                aufgabe.anweisung = format!("Manual: chain {} steps", chain.len());
                let id = aufgabe.id.clone();
                match s.pipeline.speichern(&aufgabe) {
                    Ok(_) => Json(serde_json::json!({"ok": true, "task_id": id})),
                    Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
                }
            } else {
                Json(serde_json::json!({"ok": false, "error": "No chain configured"}))
            }
        }
        _ => Json(serde_json::json!({"ok": false, "error": format!("Unknown cron_typ: {}", cron_typ)})),
    }
}

// ─── Wizard ───────────────────────────────────────

#[derive(serde::Deserialize)]
pub struct WizardStartReq {
    pub mode: String,                     // "new" | "copy" | "edit"
    pub source_id: Option<String>,
}

pub async fn wizard_start(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::Json(req): axum::Json<WizardStartReq>,
) -> Result<axum::Json<serde_json::Value>, (axum::http::StatusCode, String)> {
    let cfg = state.config.read().await;
    if cfg.wizard.as_ref().map(|w| !w.enabled).unwrap_or(true) {
        return Err((axum::http::StatusCode::SERVICE_UNAVAILABLE,
                    "Wizard-Backend nicht konfiguriert".into()));
    }
    let (mode, draft, original) = match req.mode.as_str() {
        "new" => (WizardMode::New, DraftAgent::default(), None),
        "copy" => {
            let src = req.source_id.as_deref().ok_or((
                axum::http::StatusCode::BAD_REQUEST,
                "source_id required for copy".into()))?;
            if crate::security::safe_id(src).is_none() {
                return Err((axum::http::StatusCode::BAD_REQUEST, "invalid source_id".into()));
            }
            let src_m = cfg.module.iter().find(|m| m.id == src).cloned()
                .ok_or((axum::http::StatusCode::NOT_FOUND, "source module not found".into()))?;
            let mut d: DraftAgent = draft_from_module(&src_m);
            d.id = None;
            (WizardMode::Copy { source_id: src.into() }, d, Some(src_m))
        }
        "edit" => {
            let src = req.source_id.as_deref().ok_or((
                axum::http::StatusCode::BAD_REQUEST,
                "source_id required for edit".into()))?;
            if crate::security::safe_id(src).is_none() {
                return Err((axum::http::StatusCode::BAD_REQUEST, "invalid source_id".into()));
            }
            let src_m = cfg.module.iter().find(|m| m.id == src).cloned()
                .ok_or((axum::http::StatusCode::NOT_FOUND, "source module not found".into()))?;
            let d = draft_from_module(&src_m);
            (WizardMode::Edit { target_id: src.into() }, d, Some(src_m))
        }
        _ => return Err((axum::http::StatusCode::BAD_REQUEST, "mode must be new|copy|edit".into())),
    };
    drop(cfg);

    wizard::ensure_dirs(&state.data_root).await
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    let now = chrono::Utc::now().timestamp();
    let session = WizardSession {
        session_id: wizard::new_session_id(),
        mode: mode.clone(),
        draft: draft.clone(),
        original,
        transcript: vec![],
        llm_rounds_used: 0,
        created_at: now,
        last_activity: now,
        user_overridden_fields: vec![],
        frozen_reason: None,
        code_gen_proposal: None,
    };
    wizard::save_session(&state.data_root, &session).await
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    let cfg_read = state.config.read().await;
    let missing = wizard::missing_fields(&session.draft, &cfg_read, &session.mode);
    drop(cfg_read);
    Ok(axum::Json(serde_json::json!({
        "session_id": session.session_id,
        "mode": session.mode,
        "draft": session.draft,
        "missing_for_commit": missing,
    })))
}

pub fn draft_from_module(m: &crate::types::ModulConfig) -> DraftAgent {
    DraftAgent {
        id: Some(m.id.clone()),
        typ: Some(m.typ.clone()),
        llm_backend: Some(m.llm_backend.clone()),
        backup_llm: m.backup_llm.clone(),
        berechtigungen: m.berechtigungen.clone(),
        timeout_s: Some(m.timeout_s),
        retry: Some(m.retry),
        rag_pool: m.rag_pool.clone(),
        linked_modules: m.linked_modules.clone(),
        persistent: m.persistent,
        scheduler_interval_ms: m.scheduler_interval_ms,
        max_concurrent_tasks: m.max_concurrent_tasks,
        token_budget: m.token_budget,
        token_budget_warning: m.token_budget_warning,
        identity: DraftIdentity {
            bot_name: Some(m.identity.bot_name.clone()),
            display_name: Some(m.display_name.clone()),
            system_prompt: Some(m.identity.system_prompt.clone()),
            ..Default::default()
        },
        settings: serde_json::to_value(&m.settings).unwrap_or(serde_json::json!({})),
    }
}

#[derive(serde::Deserialize)]
pub struct WizardAbortReq { pub session_id: String }

pub async fn wizard_abort(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::Json(req): axum::Json<WizardAbortReq>,
) -> Result<axum::Json<serde_json::Value>, (axum::http::StatusCode, String)> {
    if crate::security::safe_id(&req.session_id).is_none() {
        return Err((axum::http::StatusCode::BAD_REQUEST, "invalid session_id".into()));
    }
    wizard::delete_session(&state.data_root, &req.session_id).await
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(axum::Json(serde_json::json!({"ok": true})))
}

#[derive(serde::Deserialize)]
pub struct WizardPatchReq {
    pub session_id: String,
    pub field: String,
    pub value: serde_json::Value,
}

pub async fn wizard_patch(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::Json(req): axum::Json<WizardPatchReq>,
) -> Result<axum::Json<serde_json::Value>, (axum::http::StatusCode, String)> {
    if crate::security::safe_id(&req.session_id).is_none() {
        return Err((axum::http::StatusCode::BAD_REQUEST, "invalid session_id".into()));
    }
    let mut session = wizard::load_session(&state.data_root, &req.session_id).await
        .ok_or((axum::http::StatusCode::NOT_FOUND, "session not found".into()))?;
    wizard::apply_propose(&mut session.draft, &req.field, &req.value)
        .map_err(|e| (axum::http::StatusCode::BAD_REQUEST, e))?;
    if !session.user_overridden_fields.contains(&req.field) {
        session.user_overridden_fields.push(req.field.clone());
    }
    session.last_activity = chrono::Utc::now().timestamp();
    wizard::save_session(&state.data_root, &session).await
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    let cfg = state.config.read().await;
    let missing = wizard::missing_fields(&session.draft, &cfg, &session.mode);
    drop(cfg);

    Ok(axum::Json(serde_json::json!({
        "ok": true,
        "draft": session.draft,
        "missing_for_commit": missing,
    })))
}

pub async fn wizard_list_sessions(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
) -> axum::Json<serde_json::Value> {
    let sessions = wizard::list_active_sessions(&state.data_root).await;
    let summary: Vec<_> = sessions.into_iter().map(|s| serde_json::json!({
        "session_id": s.session_id,
        "mode": s.mode,
        "created_at": s.created_at,
        "last_activity": s.last_activity,
        "agent_name": s.draft.identity.bot_name,
        "agent_id": s.draft.id,
        "rounds_used": s.llm_rounds_used,
        "frozen_reason": s.frozen_reason,
    })).collect();
    axum::Json(serde_json::json!({"sessions": summary}))
}

#[derive(serde::Deserialize)]
pub struct WizardTurnReq {
    pub session_id: String,
    pub text: String,
}

pub async fn wizard_turn(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::extract::ConnectInfo(addr): axum::extract::ConnectInfo<std::net::SocketAddr>,
    axum::Json(req): axum::Json<WizardTurnReq>,
) -> Result<axum::response::Response, (axum::http::StatusCode, String)> {
    if !state.wizard_rate.check(addr.ip()).await {
        return Err((axum::http::StatusCode::TOO_MANY_REQUESTS, "rate limit".into()));
    }
    // Daily USD budget hard-stop — peek only, die echte Reservation passiert
    // im Wizard-Backend beim LLM-Call.
    {
        let cfg = state.config.read().await.clone();
        if let Err(msg) = peek_daily_budget(&state.pipeline.store.pool, &cfg).await {
            return Err((axum::http::StatusCode::PAYMENT_REQUIRED, msg));
        }
    }
    if crate::security::safe_id(&req.session_id).is_none() {
        return Err((axum::http::StatusCode::BAD_REQUEST, "invalid session_id".into()));
    }
    {
        let mut inflight = state.wizard_turn_inflight.lock().await;
        if !inflight.insert(req.session_id.clone()) {
            return Err((axum::http::StatusCode::CONFLICT,
                        "session has a turn in flight — wait or abort".into()));
        }
    }
    // Pre-flight: session exists + wizard configured
    if wizard::load_session(&state.data_root, &req.session_id).await.is_none() {
        let mut inflight = state.wizard_turn_inflight.lock().await;
        inflight.remove(&req.session_id);
        return Err((axum::http::StatusCode::NOT_FOUND, "session not found".into()));
    }
    let wizard_cfg = {
        let cfg = state.config.read().await;
        cfg.wizard.clone()
    };
    let wizard_cfg = match wizard_cfg {
        Some(w) => w,
        None => {
            let mut inflight = state.wizard_turn_inflight.lock().await;
            inflight.remove(&req.session_id);
            return Err((axum::http::StatusCode::SERVICE_UNAVAILABLE,
                        "wizard not configured".into()));
        }
    };

    let (tx, rx) = tokio::sync::mpsc::channel::<wizard::WizardEvent>(64);
    let state_c = state.clone();
    let session_id = req.session_id.clone();
    let text = req.text.clone();

    tokio::spawn(async move {
        let backend: Box<dyn wizard::WizardBackend + Send + Sync> = Box::new(
            wizard::RealWizardBackend {
                router: state_c.llm.clone(),
                backend: wizard_cfg.llm.clone(),
                tokens: Some(state_c.tokens.clone()),
                store_pool: Some((*state_c.pipeline.store.pool).clone()),
            }
        );
        let mut session = match wizard::load_session(&state_c.data_root, &session_id).await {
            Some(s) => s,
            None => {
                let _ = tx.send(wizard::WizardEvent::Error { message: "session disappeared".into() }).await;
                let _ = tx.send(wizard::WizardEvent::Done).await;
                {
                    let mut inflight = state_c.wizard_turn_inflight.lock().await;
                    inflight.remove(&session_id);
                }
                return;
            }
        };
        let _ = tx.send(wizard::WizardEvent::Session {
            session_id: session.session_id.clone(),
            mode: session.mode.clone(),
        }).await;
        let py_mods = state_c.py_modules.read().await.clone();
        let _ = wizard::run_turn(
            &*backend, &mut session, &state_c.config, &state_c.config_path,
            &wizard_cfg, &state_c.data_root, text, tx, &py_mods,
        ).await;
        {
            let mut inflight = state_c.wizard_turn_inflight.lock().await;
            inflight.remove(&session_id);
        }
    });

    let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
    let body = Body::from_stream(stream.map(|ev| {
        let line = serde_json::to_string(&ev).unwrap_or_default() + "\n";
        Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(line))
    }));
    let resp = axum::response::Response::builder()
        .status(axum::http::StatusCode::OK)
        .header("content-type", "application/x-ndjson")
        .header("cache-control", "no-cache")
        .body(body)
        .unwrap();
    Ok(resp)
}

#[derive(serde::Deserialize)]
pub struct WizardModelsReq {
    pub provider: String,
    pub api_url: Option<String>,
    pub api_key: Option<String>,
}

pub async fn wizard_models(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::extract::Query(req): axum::extract::Query<WizardModelsReq>,
) -> Result<axum::Json<serde_json::Value>, (axum::http::StatusCode, String)> {
    let (url, key) = match (req.api_url.clone(), req.api_key.clone()) {
        (Some(u), Some(k)) => (u, k),
        _ => {
            let cfg = state.config.read().await;
            match &cfg.wizard {
                Some(w) => (w.llm.url.clone(), w.llm.api_key.clone().unwrap_or_default()),
                None => return Err((
                    axum::http::StatusCode::BAD_REQUEST,
                    "no api_url/api_key given and no wizard.llm configured".into(),
                )),
            }
        }
    };

    match req.provider.as_str() {
        "Claude" | "Anthropic" => {
            Ok(axum::Json(serde_json::json!({
                "models": [
                    {"id": "claude-opus-4-7",   "display_name": "Claude Opus 4.7"},
                    {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6"},
                    {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
                    {"id": "claude-opus-4-6",   "display_name": "Claude Opus 4.6"}
                ]
            })))
        }
        "OpenAI" | "Grok" | "OpenRouter" => {
            crate::security::validate_external_url(&url)
                .map_err(|e| (axum::http::StatusCode::BAD_REQUEST, e))?;
            let path = if req.provider == "OpenRouter" { "/api/v1/models" } else { "/v1/models" };
            let full_url = format!("{}{}", url.trim_end_matches('/'), path);
            let client = reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(15))
                .build()
                .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
            let resp = client.get(&full_url)
                .bearer_auth(&key)
                .send().await
                .map_err(|e| (axum::http::StatusCode::BAD_GATEWAY, e.to_string()))?;
            let status = resp.status();
            let body: serde_json::Value = resp.json().await
                .map_err(|e| (axum::http::StatusCode::BAD_GATEWAY, e.to_string()))?;
            if !status.is_success() {
                return Err((axum::http::StatusCode::BAD_GATEWAY,
                            format!("provider returned {}: {}", status, body)));
            }
            let arr = body.get("data").and_then(|v| v.as_array()).cloned().unwrap_or_default();
            let models: Vec<_> = arr.iter().filter_map(|m| {
                let id = m.get("id")?.as_str()?.to_string();
                Some(serde_json::json!({"id": id.clone(), "display_name": id}))
            }).collect();
            Ok(axum::Json(serde_json::json!({"models": models})))
        }
        _ => Err((axum::http::StatusCode::BAD_REQUEST, "unknown provider".into())),
    }
}

#[derive(serde::Deserialize)]
pub struct WizardTestConnReq {
    pub provider: String,
    pub api_url: String,
    pub api_key: String,
    pub model: String,
}

pub async fn wizard_test_connection(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::Json(req): axum::Json<WizardTestConnReq>,
) -> Result<axum::Json<serde_json::Value>, (axum::http::StatusCode, String)> {
    crate::security::validate_external_url(&req.api_url)
        .map_err(|e| (axum::http::StatusCode::BAD_REQUEST, e))?;

    let typ = match req.provider.as_str() {
        "Claude" | "Anthropic" => crate::types::LlmTyp::Anthropic,
        "OpenAI" | "OpenRouter" => crate::types::LlmTyp::OpenAICompat,
        "Grok" => crate::types::LlmTyp::Grok,
        _ => return Err((axum::http::StatusCode::BAD_REQUEST, "unknown provider".into())),
    };

    let backend = crate::types::LlmBackend {
        id: "wizard-test".into(),
        name: "Wizard-Test".into(),
        typ,
        url: req.api_url.clone(),
        api_key: Some(req.api_key),
        model: req.model,
        timeout_s: 15,
        identity: Default::default(),
        max_tokens: None,
    };

    // Try a minimal ping: single user message "ping"
    let messages = vec![serde_json::json!({"role": "user", "content": "ping"})];
    match state.llm.chat_with_tools_adhoc(&backend, &messages, &[]).await {
        Ok((_text, _raw)) => Ok(axum::Json(serde_json::json!({"ok": true, "message": "Verbindung OK"}))),
        Err(e) => Ok(axum::Json(serde_json::json!({"ok": false, "error": e}))),
    }
}

// ─── Wizard code-gen confirmation endpoint ────────────

#[derive(serde::Deserialize)]
pub struct WizardConfirmCodeGenReq {
    pub session_id: String,
    pub approved: bool,
    pub reason: Option<String>,
}

pub async fn wizard_confirm_code_gen(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::Json(req): axum::Json<WizardConfirmCodeGenReq>,
) -> Result<axum::response::Response, (axum::http::StatusCode, String)> {
    if crate::security::safe_id(&req.session_id).is_none() {
        return Err((axum::http::StatusCode::BAD_REQUEST, "invalid session_id".into()));
    }
    let session = wizard::load_session(&state.data_root, &req.session_id).await
        .ok_or((axum::http::StatusCode::NOT_FOUND, "session not found".into()))?;
    if session.code_gen_proposal.is_none() {
        return Err((axum::http::StatusCode::BAD_REQUEST, "no proposal pending".into()));
    }

    let (tx, rx) = tokio::sync::mpsc::channel::<wizard::WizardEvent>(32);
    let state_c = state.clone();
    let session_id = req.session_id.clone();
    let approved = req.approved;
    let reason = req.reason.unwrap_or_default();

    tokio::spawn(async move {
        let mut session = match wizard::load_session(&state_c.data_root, &session_id).await {
            Some(s) => s,
            None => {
                let _ = tx.send(wizard::WizardEvent::Error { message: "session gone".into() }).await;
                let _ = tx.send(wizard::WizardEvent::Done).await;
                return;
            }
        };
        wizard::execute_code_gen(&mut session, approved, &reason, &state_c, &tx).await;
        let _ = wizard::save_session(&state_c.data_root, &session).await;
        let _ = tx.send(wizard::WizardEvent::Done).await;
    });

    let stream = tokio_stream::wrappers::ReceiverStream::new(rx)
        .map(|ev| {
            let line = serde_json::to_string(&ev).unwrap_or_default() + "\n";
            Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(line))
        });

    let body = axum::body::Body::from_stream(stream);
    Ok(axum::response::Response::builder()
        .status(axum::http::StatusCode::OK)
        .header("content-type", "application/x-ndjson")
        .header("cache-control", "no-cache")
        .body(body)
        .unwrap())
}

#[derive(serde::Deserialize)]
pub struct QualityStatsReq { pub hours: Option<u32> }

pub async fn quality_stats(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::extract::Query(req): axum::extract::Query<QualityStatsReq>,
) -> axum::Json<crate::types::StatsSummary> {
    let hours = req.hours.unwrap_or(24);
    let s = crate::guardrail::compute_stats(&state.data_root, hours).await;
    axum::Json(s)
}

#[derive(serde::Deserialize)]
pub struct EventsReq {
    pub since: Option<i64>,
    pub limit: Option<usize>,
    pub backend: Option<String>,
    pub only_failed: Option<bool>,
}

pub async fn quality_events(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::extract::Query(req): axum::extract::Query<EventsReq>,
) -> axum::Json<serde_json::Value> {
    let since = req.since.unwrap_or(chrono::Utc::now().timestamp() - 86400);
    let limit = req.limit.unwrap_or(100).min(1000);
    let events = crate::guardrail::load_events_since(
        &state.data_root, since, limit,
        req.backend.as_deref(), req.only_failed.unwrap_or(false),
    ).await;
    let has_more = events.len() >= limit;
    axum::Json(serde_json::json!({"events": events, "has_more": has_more}))
}

pub async fn quality_benchmark_cases() -> Result<axum::Json<serde_json::Value>, (axum::http::StatusCode, String)> {
    let cases = crate::benchmark::load_suite()
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e))?;
    Ok(axum::Json(serde_json::json!({"cases": cases})))
}

#[derive(serde::Deserialize)]
pub struct BenchmarkRunReq {
    pub backend_id: String,
    pub modul_id: Option<String>,
    pub model: Option<String>,
}

pub async fn quality_benchmark_run(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::Json(req): axum::Json<BenchmarkRunReq>,
) -> Result<axum::response::Response, (axum::http::StatusCode, String)> {
    let cfg_snap = state.config.read().await.clone();
    let mut backend = cfg_snap.llm_backends.iter().find(|b| b.id == req.backend_id).cloned()
        .ok_or((axum::http::StatusCode::NOT_FOUND, format!("backend '{}' not found", req.backend_id)))?;
    if let Some(m) = req.model { backend.model = m; }
    let modul_id = req.modul_id.unwrap_or_else(|| {
        cfg_snap.module.iter().find(|m| m.typ == "chat").map(|m| m.id.clone()).unwrap_or_default()
    });
    if modul_id.is_empty() {
        return Err((axum::http::StatusCode::BAD_REQUEST, "no chat module available for context".into()));
    }
    let py_mods: Vec<crate::loader::PyModuleMeta> = state.py_modules.read().await.clone();
    let llm = state.llm.clone();

    let (tx, rx) = tokio::sync::mpsc::channel::<crate::benchmark::BenchmarkEvent>(64);
    tokio::spawn(async move {
        crate::benchmark::run_benchmark(backend, modul_id, cfg_snap, py_mods, llm, tx).await;
    });

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

// ═══ First-Run Setup-Wizard ══════════════════════════════
// Zeigt dem User beim ersten Start eine einfache Seite mit vier Backend-
// Presets (Ollama lokal, OpenRouter free-tier, OpenAI, Anthropic). User wählt
// einen, gibt API-Key ein, klickt Test, klickt Save. Danach Redirect zum
// Dashboard wo der Agent-Creation-Wizard sofort bereit steht.

async fn setup_page() -> Html<&'static str> {
    Html(SETUP_HTML)
}

async fn setup_status(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let cfg = s.config.read().await;
    let has_backends = !cfg.llm_backends.is_empty();
    let mut reachable = false;
    if has_backends {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(3))
            .build().unwrap_or_default();
        for b in &cfg.llm_backends {
            if crate::types::test_backend_reachable(&client, b).await {
                reachable = true; break;
            }
        }
    }
    Json(serde_json::json!({
        "has_backends": has_backends,
        "reachable": reachable,
        "needs_setup": !reachable,
    }))
}

async fn setup_test_backend(
    State(_s): State<Arc<AppState>>,
    Json(body): Json<crate::types::LlmBackend>,
) -> Json<serde_json::Value> {
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(20))
        .build()
    {
        Ok(c) => c,
        Err(e) => return Json(serde_json::json!({"ok": false, "error": format!("client: {}", e)})),
    };
    let messages = vec![serde_json::json!({"role": "user", "content": "Sag kurz 'hi'."})];
    match crate::llm::LlmRouter::dispatch_chat_public(&body, &messages, &[], &client).await {
        Ok((text, _raw)) => Json(serde_json::json!({
            "ok": true,
            "sample": crate::util::safe_truncate_owned(&text, 400),
        })),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e})),
    }
}

#[derive(serde::Deserialize)]
struct SetupSavePayload {
    backend: crate::types::LlmBackend,
    #[serde(default)]
    locale: Option<String>,
}

async fn setup_save_backend(
    State(s): State<Arc<AppState>>,
    Json(raw): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
    // Payload: entweder {backend, locale} (neu) oder direkt LlmBackend (alt) —
    // akzeptiere beides für Abwärtskompatibilität.
    let (backend, locale) = if raw.get("backend").is_some() {
        match serde_json::from_value::<SetupSavePayload>(raw) {
            Ok(p) => (p.backend, p.locale),
            Err(e) => return Json(serde_json::json!({"ok": false, "error": format!("payload: {}", e)})),
        }
    } else {
        match serde_json::from_value::<crate::types::LlmBackend>(raw) {
            Ok(b) => (b, None),
            Err(e) => return Json(serde_json::json!({"ok": false, "error": format!("payload: {}", e)})),
        }
    };

    let _lock = s.pipeline.config_write_lock.lock().await;
    let mut cfg = s.config.write().await;

    // Locale übernehmen falls mitgeschickt — wizard nutzt es als Default-Sprache
    if let Some(loc) = locale {
        if loc == "en" || loc == "de" {
            cfg.locale = loc;
        }
    }

    // Alten Ollama-Placeholder entfernen wenn User ein echtes Backend einrichtet
    if backend.id != "ollama-local" {
        cfg.llm_backends.retain(|b| b.id != "ollama-local");
    }

    if let Some(existing) = cfg.llm_backends.iter_mut().find(|b| b.id == backend.id) {
        *existing = backend.clone();
    } else {
        cfg.llm_backends.push(backend.clone());
    }

    // Wizard auf neues Backend pointen
    if let Some(ref mut w) = cfg.wizard {
        w.llm = backend.clone();
        w.enabled = true;
    } else {
        cfg.wizard = Some(crate::types::WizardConfig {
            enabled: true,
            llm: backend.clone(),
            allow_code_gen: false,
            max_rounds_per_session: 30,
            max_tool_rounds_per_turn: 8,
            session_timeout_secs: 1800,
            rate_limit_per_min: 10,
            max_system_prompt_chars: 20000,
        });
    }

    let path = s.pipeline.base.join("config.json");
    let json = match serde_json::to_string_pretty(&*cfg) {
        Ok(j) => j,
        Err(e) => return Json(serde_json::json!({"ok": false, "error": format!("serialize: {}", e)})),
    };
    if let Err(e) = crate::util::atomic_write(&path, json.as_bytes()) {
        return Json(serde_json::json!({"ok": false, "error": format!("write: {}", e)}));
    }
    s.pipeline.audit("setup.save_backend", "setup-wizard",
        &format!("backend={} typ={:?} model={}", backend.id, backend.typ, backend.model));
    Json(serde_json::json!({"ok": true}))
}

const SETUP_HTML: &str = include_str!("setup.html");

// ═══ Insight-APIs: Audit-Trail + per-Modul/per-Backend Token-Breakdown ═══════
// Das sind die UX-Löcher die "wissen was der bot macht / darf / kostet" echt
// lösen: Audit-Trail zeigt jeden Side-Effect-Tool-Call forensisch, Tokens nach
// Modul zeigen welcher Agent wie viel brennt, Tokens nach Backend erlauben
// Kostenvergleich zwischen "wir nutzen GPT vs DeepSeek".

async fn get_audit(
    State(s): State<Arc<AppState>>,
    axum::extract::Query(q): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Json<serde_json::Value> {
    let action = q.get("action").map(|s| s.as_str());
    let actor = q.get("actor").map(|s| s.as_str());
    let since = q.get("since").and_then(|s| s.parse::<i64>().ok());
    let limit = q.get("limit").and_then(|s| s.parse::<usize>().ok()).unwrap_or(200).min(1000);
    match crate::store::audit_filtered(&s.pipeline.store.pool, action, actor, since, limit) {
        Ok(rows) => Json(serde_json::json!({"entries": rows})),
        Err(e) => Json(serde_json::json!({"entries": [], "error": e})),
    }
}

async fn get_tokens_by_modul(
    State(s): State<Arc<AppState>>,
    axum::extract::Query(q): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Json<serde_json::Value> {
    let days = q.get("days").and_then(|s| s.parse::<i64>().ok()).unwrap_or(7).max(1).min(90);
    match crate::store::tokens_by_modul(&s.pipeline.store.pool, days) {
        Ok(rows) => Json(serde_json::json!({"days": days, "by_modul": rows})),
        Err(e) => Json(serde_json::json!({"by_modul": [], "error": e})),
    }
}

async fn get_tokens_by_backend(
    State(s): State<Arc<AppState>>,
    axum::extract::Query(q): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Json<serde_json::Value> {
    let days = q.get("days").and_then(|s| s.parse::<i64>().ok()).unwrap_or(7).max(1).min(90);
    match crate::store::tokens_by_backend(&s.pipeline.store.pool, days) {
        Ok(rows) => Json(serde_json::json!({"days": days, "by_backend": rows})),
        Err(e) => Json(serde_json::json!({"by_backend": [], "error": e})),
    }
}

/// "Was darf + was kann Modul X": strukturierter Read-Only-Dump für das Module-
/// Capabilities-Modal. Listet Berechtigungen in Klartext + die tatsächlich
/// nutzbaren Tools (Rust + Python) inkl. Args/Defaults. Der User sieht damit
/// sofort "Modul `chat.roland` kann files.read im Pfad /tmp, kann mail.send via
/// Python-Modul smtp, hat linked_modules = [mail.privat]".
async fn get_module_capabilities(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(id): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
    let cfg = s.config.read().await;
    let Some(modul) = cfg.module.iter().find(|m| m.id == id || m.name == id).cloned() else {
        return Json(serde_json::json!({"error": "Modul nicht gefunden"}));
    };
    let py_mods = s.py_modules.read().await.clone();
    drop(cfg);

    let rust_tools: Vec<serde_json::Value> = crate::tools::tools_for_module(&modul).iter().map(|t| {
        serde_json::json!({
            "name": t.name,
            "description": t.description,
            "params": t.params,
        })
    }).collect();

    // Python-Tools die dieses Modul nutzen DARF (via perms + linked_modules)
    let py_tools: Vec<serde_json::Value> = py_mods.iter().flat_map(|pm| {
        let perm = format!("py.{}", pm.name);
        let allowed = modul.berechtigungen.iter().any(|p| p == &perm || p == "py.*")
            || modul.linked_modules.iter().any(|link|
                link == &pm.name || link.starts_with(&format!("{}.", pm.name)));
        if !allowed { return vec![]; }
        pm.tools.iter().map(|t| serde_json::json!({
            "name": t.name,
            "description": t.description,
            "params": t.params,
            "via_python_module": pm.name,
        })).collect::<Vec<_>>()
    }).collect();

    // Permissions in Klartext
    let perm_explain: Vec<serde_json::Value> = modul.berechtigungen.iter().map(|p| {
        let human = match p.as_str() {
            "aufgaben" => "darf neue Aufgaben für verlinkte Module erstellen",
            "websearch" => "web.search + http.get (mit SSRF-Schutz)",
            "files" => "files.read/write/list im allowed_paths Whitelist",
            "files.home" => "files.* nur im eigenen home-Verzeichnis",
            "files.*" => "files.* überall (POWER!)",
            "shell" => "shell.exec mit command-whitelist + path-blacklist",
            "notify" => "notify.send (ntfy/gotify/telegram)",
            "agent.spawn" => "darf temp-Sub-Agenten spawnen",
            "agent.*" => "alle agent.* tools",
            "py.*" => "alle Python-Module (ADMIN!)",
            _ if p.starts_with("rag.") => "RAG-Suche/Speichern im angegebenen Pool",
            _ if p.starts_with("py.") => "Zugriff auf Python-Modul",
            _ => "Custom permission",
        };
        serde_json::json!({"permission": p, "explanation": human})
    }).collect();

    // Typ-basierte implizite grants (nur für persistent-Module aktiv)
    let typ_grants: Vec<&str> = if modul.persistent {
        match modul.typ.as_str() {
            "filesystem" => vec!["files.read", "files.write", "files.list (im allowed_paths)"],
            "websearch" => vec!["web.search", "http.get"],
            "shell" => vec!["shell.exec"],
            "notify" => vec!["notify.send"],
            _ => vec![],
        }
    } else { vec![] };

    Json(serde_json::json!({
        "id": modul.id,
        "name": modul.name,
        "typ": modul.typ,
        "persistent": modul.persistent,
        "llm_backend": modul.llm_backend,
        "backup_llm": modul.backup_llm,
        "linked_modules": modul.linked_modules,
        "rag_pool": modul.rag_pool,
        "token_budget": modul.token_budget,
        "timeout_s": modul.timeout_s,
        "retry": modul.retry,
        "berechtigungen": perm_explain,
        "typ_implicit_grants": typ_grants,
        "rust_tools": rust_tools,
        "python_tools": py_tools,
        "identity": {
            "bot_name": modul.identity.bot_name,
            "system_prompt_preview": crate::util::safe_truncate(&modul.identity.system_prompt, 400),
            "system_prompt_chars": modul.identity.system_prompt.len(),
        },
    }))
}
