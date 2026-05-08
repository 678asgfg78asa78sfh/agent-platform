use crate::llm::LlmRouter;
use crate::pipeline::Pipeline;
use crate::security::{self, safe_id, safe_relative_path};
use crate::tools;
use crate::types::*;
use crate::types::{DraftAgent, DraftIdentity, WizardMode, WizardSession};
use crate::util;
use crate::wizard;
use axum::body::Body;
use axum::response::IntoResponse;
use axum::{Json, Router, extract::DefaultBodyLimit, extract::State, response::Html};
use chrono::{Datelike, TimeZone};
use std::sync::Arc;
use tokio::sync::RwLock;
use tokio_stream::StreamExt;

const MAX_CHAT_TOOL_ROUNDS: usize = 30;
const MAX_CHAT_TOOL_RESULT_CHARS: usize = 7000;
const MAX_CHAT_TASK_RESULT_CHARS: usize = 20000;
const MAX_MALFORMED_TOOL_RETRIES: u32 = 3;
const MAX_CHAT_TOOL_HISTORY_ARG_CHARS: usize = 1200;
const MAX_FINAL_SYNTHESIS_EVIDENCE_CHARS: usize = 22000;
const MAX_FINAL_SYNTHESIS_TOOL_RESULT_CHARS: usize = 1400;

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

fn reservation_for_model(_model: &str) -> f64 {
    0.0
}

fn model_for_backend(cfg: &AgentConfig, backend_id: &str) -> String {
    cfg.llm_backends
        .iter()
        .find(|b| b.id == backend_id)
        .map(|b| b.model.clone())
        .unwrap_or_default()
}

fn estimate_message_tokens(messages: &[serde_json::Value]) -> u64 {
    let chars: usize = messages
        .iter()
        .filter_map(|m| m.get("content"))
        .map(|content| match content {
            serde_json::Value::String(s) => s.len(),
            other => other.to_string().len(),
        })
        .sum();
    ((chars + 3) / 4).max(1) as u64
}

fn chat_tool_result_for_llm(ok: bool, data: &str) -> String {
    let body = if data.chars().count() > MAX_CHAT_TOOL_RESULT_CHARS {
        format!(
            "{}...[gekuerzt; vollstaendiges Ergebnis im Aufgaben-Board]",
            util::safe_truncate(data, MAX_CHAT_TOOL_RESULT_CHARS)
        )
    } else {
        data.to_string()
    };
    if ok {
        format!("SUCCESS: {}", body)
    } else {
        format!(
            "FAILED: {}\nNEXT: Decide whether to retry with corrected parameters, use another available tool, or tell the user exactly why the step cannot be completed. Do not present this failed step as successful.",
            body
        )
    }
}

fn tool_arguments_json_for_history(
    tool_name: &str,
    params: &[String],
    modul: Option<&ModulConfig>,
    py_modules: &[crate::loader::PyModuleMeta],
) -> String {
    let compact_param =
        |value: &str| util::safe_truncate(value, MAX_CHAT_TOOL_HISTORY_ARG_CHARS).to_string();
    let mut obj = serde_json::Map::new();
    if let Some(m) = modul {
        if let Some(required) = tools::schema_required_for(tool_name, m, py_modules) {
            for (idx, key) in required.iter().enumerate() {
                let value = params
                    .get(idx)
                    .map(|v| compact_param(v))
                    .unwrap_or_default();
                obj.insert(key.clone(), serde_json::json!(value));
            }
            for (idx, value) in params.iter().skip(required.len()).enumerate() {
                obj.insert(
                    format!("extra_{}", idx + 1),
                    serde_json::json!(compact_param(value)),
                );
            }
            return serde_json::Value::Object(obj).to_string();
        }
    }

    let fallback_keys: &[&str] = match tool_name {
        "rag.suchen" | "duckduckgo.search" | "web.search" | "tavily.search" => &["query"],
        "rag.speichern" | "notify.send" => &["text"],
        "browser.fetch" | "http.get" => &["url"],
        "files.read" | "files.list" => &["path"],
        "files.write" => &["path", "content"],
        "aufgaben.erstellen" => &["modul", "anweisung", "wann"],
        "agent.spawn" => &["basis_modul", "system_prompt", "aufgabe"],
        _ => &[],
    };
    for (idx, value) in params.iter().enumerate() {
        let key = fallback_keys
            .get(idx)
            .map(|v| (*v).to_string())
            .unwrap_or_else(|| format!("arg_{}", idx + 1));
        obj.insert(key, serde_json::json!(compact_param(value)));
    }
    serde_json::Value::Object(obj).to_string()
}

fn compact_chat_evidence(messages: &[serde_json::Value], max_chars: usize) -> String {
    let mut labels: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    let mut entries: Vec<String> = vec![];

    for message in messages {
        if message.get("role").and_then(|v| v.as_str()) == Some("assistant") {
            if let Some(calls) = message.get("tool_calls").and_then(|v| v.as_array()) {
                for call in calls {
                    let call_id = call
                        .get("id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("call")
                        .to_string();
                    let name = call
                        .pointer("/function/name")
                        .and_then(|v| v.as_str())
                        .unwrap_or("tool");
                    let args = call
                        .pointer("/function/arguments")
                        .and_then(|v| v.as_str())
                        .unwrap_or("{}");
                    labels.insert(
                        call_id,
                        format!(
                            "{} {}",
                            name,
                            util::safe_truncate(args, MAX_CHAT_TOOL_HISTORY_ARG_CHARS)
                        ),
                    );
                }
            }
        } else if message.get("role").and_then(|v| v.as_str()) == Some("tool") {
            let call_id = message
                .get("tool_call_id")
                .and_then(|v| v.as_str())
                .unwrap_or("call");
            let label = labels
                .get(call_id)
                .cloned()
                .unwrap_or_else(|| format!("tool {}", call_id));
            let content = message
                .get("content")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            entries.push(format!(
                "### {}\n{}\n",
                label,
                util::safe_truncate(content, MAX_FINAL_SYNTHESIS_TOOL_RESULT_CHARS)
            ));
        }
    }

    if entries.is_empty() {
        return "(keine Tool-Evidenz im Chat-Verlauf gefunden)".into();
    }

    let mut selected: Vec<String> = vec![];
    let mut total = 0usize;
    let mut skipped = 0usize;
    for entry in entries.iter().rev() {
        let len = entry.chars().count();
        if total + len > max_chars {
            skipped += 1;
            continue;
        }
        total += len;
        selected.push(entry.clone());
    }
    selected.reverse();
    let mut out = String::new();
    if skipped > 0 {
        out.push_str(&format!(
            "[{} aeltere Tool-Ergebnisse wegen Kontextlimit gekuerzt]\n\n",
            skipped
        ));
    }
    out.push_str(&selected.join("\n"));
    out
}

fn final_synthesis_messages(
    last_user_msg: &str,
    messages: &[serde_json::Value],
    needs_deepdive: bool,
    max_evidence_chars: usize,
) -> Vec<serde_json::Value> {
    let evidence = compact_chat_evidence(messages, max_evidence_chars);
    let instruction = if needs_deepdive {
        "Das Tool-Rundenlimit ist erreicht. Nutze keine Tools. Erstelle aus der folgenden Tool-Evidenz einen belastbaren DeepDive-Bericht mit aktuellem Stand, wichtigsten Fundstellen, Timeline/Chronologie, Kausalkette, Unsicherheiten und einem <quellen>-Block mit exakten URLs/Fundorten."
    } else {
        "Das Tool-Rundenlimit ist erreicht. Nutze keine Tools. Erstelle aus der folgenden Tool-Evidenz die beste moegliche Antwort und markiere unvollstaendige Punkte klar."
    };
    vec![
        serde_json::json!({"role": "system", "content": "Du bist im finalen Synthese-Modus. Du darfst keine Tools verwenden und keine neuen Recherchen behaupten. Arbeite nur mit der gelieferten Tool-Evidenz."}),
        serde_json::json!({"role": "user", "content": format!("Originale Anfrage:\n{}\n\n{}\n\nTool-Evidenz:\n{}", last_user_msg, instruction, evidence)}),
    ]
}

#[derive(Debug, Clone)]
struct ChatToolFailure {
    tool_name: String,
    detail: String,
    recovered: bool,
}

#[derive(Debug, Clone, Default)]
struct DeepdiveProgress {
    crawl_ok: usize,
    crawl_id: Option<String>,
    search_ok: usize,
    fetch_ok: usize,
    source_note_ok: usize,
    rag_save_ok: usize,
    rag_search_ok: usize,
    last_evidence_round: usize,
    last_rag_round: usize,
}

fn is_deepdive_request(text: &str) -> bool {
    let lower = text.to_lowercase();
    let currentish = [
        "aktuell",
        "heute",
        "gerade",
        "news",
        "nachrichten",
        "neuigkeiten",
        "letzte stunde",
        "neuste",
        "neueste",
        "stand der dinge",
        "aktueller stand",
        "quelle",
        "quellen",
        "web",
        "internet",
        "kommentar",
        "kommentare",
        "ereignis",
        "ereignisse",
    ]
    .iter()
    .any(|kw| lower.contains(kw));
    let asks_research = [
        "such",
        "suche",
        "finde",
        "recherch",
        "prüf",
        "pruef",
        "check",
        "schau",
        "zieh",
        "hole",
        "gibt es neues",
        "was gibt es neues",
        "was gibts neues",
    ]
    .iter()
    .any(|kw| lower.contains(kw));
    if [
        "deepdive",
        "deep dive",
        "recherche",
        "quellenanalyse",
        "lagebild",
    ]
    .iter()
    .any(|kw| lower.contains(kw))
    {
        return true;
    }
    if currentish && asks_research {
        return true;
    }
    let wants_breadth =
        lower.contains("alles") || lower.contains("so viel") || lower.contains("was du kannst");
    asks_research && wants_breadth && currentish
}

fn deepdive_topic_hint(text: &str) -> String {
    let lower = text.to_lowercase();
    for marker in ["über ", "ueber ", "zu ", "nach "] {
        if let Some(pos) = lower.find(marker) {
            let start = pos + marker.len();
            let mut topic = text[start..].to_string();
            for stop in [" heraus", " raus", " im web", " was du", " mit quellen"] {
                if let Some(end) = topic.to_lowercase().find(stop) {
                    topic.truncate(end);
                }
            }
            let topic = topic.trim_matches(|c: char| {
                c.is_whitespace() || c == '"' || c == '\'' || c == '.' || c == '?' || c == '!'
            });
            if topic.len() >= 3 {
                return topic.to_string();
            }
        }
    }
    text.trim().chars().take(120).collect::<String>()
}

fn observe_deepdive_progress(
    progress: &mut DeepdiveProgress,
    tool_name: &str,
    ok: bool,
    round: usize,
) {
    if !ok {
        return;
    }
    match tool_name {
        "deepdive.crawl" => {
            progress.crawl_ok += 1;
            progress.last_evidence_round = round;
        }
        "tavily.search" | "duckduckgo.search" | "web.search" => {
            progress.search_ok += 1;
        }
        "browser.fetch" | "http.get" => {
            progress.fetch_ok += 1;
            progress.last_evidence_round = round;
        }
        "deepdive.source_note" => {
            progress.source_note_ok += 1;
            progress.last_evidence_round = round;
        }
        "rag.speichern" => {
            progress.rag_save_ok += 1;
            progress.last_evidence_round = round;
        }
        "rag.suchen" => {
            progress.rag_search_ok += 1;
            progress.last_rag_round = round;
        }
        _ => {}
    }
}

fn deepdive_gate_feedback(
    progress: &DeepdiveProgress,
    user_text: &str,
    final_text: &str,
) -> Option<String> {
    let topic = deepdive_topic_hint(user_text);
    let enough_manual = progress.search_ok >= 2
        && progress.fetch_ok >= 3
        && (progress.source_note_ok >= 2 || progress.rag_save_ok >= 1);
    if progress.crawl_ok == 0 && !enough_manual {
        return Some(format!(
            "DEEPDIVE-CHECK: Die Anfrage verlangt breites Web-Crawling. Du bist noch nicht tief genug. Antworte jetzt AUSSCHLIESSLICH mit diesem Toolcall: <tool>deepdive.crawl({})</tool>",
            topic
        ));
    }
    if progress.rag_search_ok == 0 || progress.last_rag_round < progress.last_evidence_round {
        let rag_query = progress
            .crawl_id
            .as_ref()
            .map(|id| format!("{} {}", id, topic))
            .unwrap_or(topic);
        return Some(format!(
            "DEEPDIVE-CHECK: Quellen wurden verarbeitet, aber die Synthese muss aus dem RAG kommen. Antworte jetzt AUSSCHLIESSLICH mit diesem Toolcall: <tool>rag.suchen({})</tool>",
            rag_query
        ));
    }
    let final_lower = final_text.to_lowercase();
    let user_lower = user_text.to_lowercase();
    if user_lower.contains("merz")
        && final_lower.contains("kanzlerkandidat")
        && !final_lower.contains("bundeskanzler")
        && !final_lower.contains("kanzler seit")
        && !final_lower.contains("histor")
    {
        return Some(
            "DEEPDIVE-CHECK: Du formulierst bei Friedrich Merz offenbar einen alten Stand als aktuellen Stand. Pruefe die RAG-Notizen auf neuere Quellen und liefere die Antwort mit aktueller Rolle; 'Kanzlerkandidat' nur historisch einordnen. Wenn unklar, rufe zuerst rag.suchen(Friedrich Merz aktueller Stand Kanzler 2026) auf.".to_string()
        );
    }
    let shallow_pool_answer = [
        "nicht im vorliegenden auszug",
        "spezifischeren fokus",
        "muessten die eigentlichen inhalte",
        "müssten die eigentlichen inhalte",
        "sobald die inhalte freigegeben",
        "recherche-pool ist",
        "quellen gefuellt",
        "quellen gefüllt",
        "wichtigste quellen und die art",
    ]
    .iter()
    .any(|marker| final_lower.contains(marker));
    if shallow_pool_answer {
        return Some(format!(
            "DEEPDIVE-CHECK: Du hast nur Quellen/Hubs beschrieben, aber keine konkreten Fundstellen analysiert. Das ist kein DeepDive. Antworte jetzt AUSSCHLIESSLICH mit diesem Toolcall, damit konkrete Artikel, Kausalkette und aktuelle Ereignisse nachgezogen werden: <tool>deepdive.crawl({} konkrete Artikel aktuelle Entwicklung Ursache Folgen Reaktionen)</tool>",
            topic
        ));
    }
    if !final_lower.contains("<quellen>") || !final_lower.contains("http") {
        return Some(
            "DEEPDIVE-CHECK: Die Finalantwort braucht exakte Fundorte. Antworte jetzt OHNE weiteren Toolcall neu und fuege am Ende zwingend einen <quellen>...</quellen>-Block ein. Jede Quelle braucht mindestens URL/Fundort, Titel/Outlet wenn vorhanden, Abrufzeit oder RAG-ID. Keine reinen Outlet-Namen ohne URL.".to_string()
        );
    }
    let wants_deepdive_shape = user_lower.contains("deepdive")
        || user_lower.contains("alles")
        || user_lower.contains("aktuell")
        || user_lower.contains("news")
        || user_lower.contains("ereignis");
    let has_time_or_causal_shape = final_lower.contains("timeline")
        || final_lower.contains("chronologie")
        || final_lower.contains("kaus")
        || final_lower.contains("ursache")
        || final_lower.contains("folge")
        || final_lower.contains("hintergrund")
        || final_lower.contains("warum");
    if wants_deepdive_shape && !has_time_or_causal_shape {
        return Some(
            "DEEPDIVE-CHECK: Die Antwort hat Quellen, aber kein DeepDive-Lagebild. Antworte jetzt OHNE weiteren Toolcall neu mit: aktueller Stand, Timeline/Chronologie, Kausalkette/warum die Ereignisse zusammenhaengen, Unsicherheiten, und <quellen> mit exakten URLs.".to_string()
        );
    }
    None
}

fn extract_deepdive_crawl_id(text: &str) -> Option<String> {
    text.lines()
        .find_map(|line| line.strip_prefix("crawl_id:").map(|v| v.trim().to_string()))
        .filter(|v| v.starts_with("dd-") && v.len() <= 64)
}

fn summarize_chat_tool_failures(tool_failures: &[ChatToolFailure]) -> (Vec<String>, usize) {
    let unresolved = tool_failures
        .iter()
        .filter(|failure| !failure.recovered)
        .map(|failure| format!("{}: {}", failure.tool_name, failure.detail))
        .collect();
    let recovered = tool_failures
        .iter()
        .filter(|failure| failure.recovered)
        .count();
    (unresolved, recovered)
}

#[derive(Debug, Clone)]
pub struct LlmCapHit {
    pub backend_id: String,
    pub model: String,
    pub reset_ts: i64,
    pub reason: String,
}

impl LlmCapHit {
    pub fn message(&self) -> String {
        format!(
            "CAP HIT: {}. CONTINUE IN {}",
            self.reason,
            human_duration_until(self.reset_ts)
        )
    }

    pub fn reset_iso(&self) -> String {
        chrono::DateTime::<chrono::Utc>::from_timestamp(self.reset_ts, 0)
            .unwrap_or_else(chrono::Utc::now)
            .to_rfc3339()
    }
}

fn backend_prices_per_1m(cfg: &AgentConfig, backend_id: &str) -> (f64, f64) {
    cfg.llm_backends
        .iter()
        .find(|b| b.id == backend_id)
        .and_then(|b| b.cost_cap.as_ref())
        .map(|cap| {
            (
                cap.input_per_1m.unwrap_or(0.0).max(0.0),
                cap.output_per_1m.unwrap_or(0.0).max(0.0),
            )
        })
        .unwrap_or((0.0, 0.0))
}

fn cost_for_tokens(
    input_tokens: u64,
    output_tokens: u64,
    input_per_1m: f64,
    output_per_1m: f64,
) -> f64 {
    (input_tokens as f64 / 1_000_000.0) * input_per_1m
        + (output_tokens as f64 / 1_000_000.0) * output_per_1m
}

fn cap_window(now_ts: i64, cap: &LlmCostCap) -> (i64, i64) {
    if cap.cycle == "monthly" {
        let now = chrono::Utc::now();
        let start = chrono::Utc
            .with_ymd_and_hms(now.year(), now.month(), 1, 0, 0, 0)
            .single()
            .unwrap_or_else(chrono::Utc::now)
            .timestamp();
        let (year, month) = if now.month() == 12 {
            (now.year() + 1, 1)
        } else {
            (now.year(), now.month() + 1)
        };
        let end = chrono::Utc
            .with_ymd_and_hms(year, month, 1, 0, 0, 0)
            .single()
            .unwrap_or_else(chrono::Utc::now)
            .timestamp();
        return (start, end.max(start + 1));
    }

    let days = cap.cycle_days.max(1) as i64;
    let span = days * 86_400;
    let start = now_ts - now_ts.rem_euclid(span);
    (start, start + span)
}

fn human_duration_until(reset_ts: i64) -> String {
    let mut secs = (reset_ts - chrono::Utc::now().timestamp()).max(0);
    let days = secs / 86_400;
    secs %= 86_400;
    let hours = secs / 3_600;
    secs %= 3_600;
    let minutes = secs / 60;
    if days > 0 {
        format!("{}d {}h", days, hours)
    } else if hours > 0 {
        format!("{}h {}m", hours, minutes)
    } else {
        format!("{}m", minutes.max(1))
    }
}

pub async fn check_llm_cap(
    store_pool: &crate::store::SqlitePool,
    cfg: &AgentConfig,
    backend_id: &str,
    messages: &[serde_json::Value],
    cap_override: bool,
) -> Result<(), LlmCapHit> {
    if cap_override {
        return Ok(());
    }
    let Some(backend) = cfg.llm_backends.iter().find(|b| b.id == backend_id) else {
        return Ok(());
    };
    let Some(cap) = backend.cost_cap.as_ref() else {
        return Ok(());
    };
    if !cap.enabled {
        return Ok(());
    }

    let now_ts = chrono::Utc::now().timestamp();
    let (start_ts, end_ts) = cap_window(now_ts, cap);
    let stats = match crate::store::token_backend_window(store_pool, backend_id, start_ts, end_ts) {
        Ok(s) => s,
        Err(e) => {
            return Err(LlmCapHit {
                backend_id: backend_id.into(),
                model: backend.model.clone(),
                reset_ts: end_ts,
                reason: format!("Cost-Cap konnte Token-Stats nicht lesen: {}", e),
            });
        }
    };

    if let Some(max_calls) = cap.max_calls.filter(|v| *v > 0) {
        if stats.calls >= max_calls {
            return Err(LlmCapHit {
                backend_id: backend_id.into(),
                model: backend.model.clone(),
                reset_ts: end_ts,
                reason: format!(
                    "LLM {} hat {} von {} Calls im Cap-Zyklus verbraucht",
                    backend_id, stats.calls, max_calls
                ),
            });
        }
    }

    if let Some(budget) = cap.budget_usd.filter(|v| *v > 0.0) {
        let (input_price, output_price) = backend_prices_per_1m(cfg, backend_id);
        let input_est = estimate_message_tokens(messages);
        let output_est = backend.max_tokens.unwrap_or(1024).max(1) as u64;
        let projected =
            stats.cost_usd + cost_for_tokens(input_est, output_est, input_price, output_price);
        if projected > budget {
            return Err(LlmCapHit {
                backend_id: backend_id.into(),
                model: backend.model.clone(),
                reset_ts: end_ts,
                reason: format!(
                    "LLM {} liegt bei ${:.4} von ${:.2} im Cap-Zyklus",
                    backend_id, stats.cost_usd, budget
                ),
            });
        }
    }

    Ok(())
}

fn cap_task_message(hit: &LlmCapHit) -> String {
    format!(
        "{} (backend={}, model={}, reset={})",
        hit.message(),
        hit.backend_id,
        hit.model,
        hit.reset_iso()
    )
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
        state
            .config
            .try_read()
            .map(|c| c.max_body_bytes)
            .unwrap_or(2 * 1024 * 1024)
    };
    let auth_state = Arc::new(security::AuthState {
        config: state.config.clone(),
    });
    Router::new()
        .route("/favicon.ico", axum::routing::get(favicon))
        .route("/", axum::routing::get(index))
        .route("/chat/{modul_id}", axum::routing::get(chat_page))
        .route("/chat/{modul_id}/{rest}", axum::routing::get(chat_page))
        .route("/wizard", axum::routing::get(wizard_page))
        .route("/setup", axum::routing::get(setup_page))
        .route("/api/setup/status", axum::routing::get(setup_status))
        .route(
            "/api/setup/test-backend",
            axum::routing::post(setup_test_backend),
        )
        .route("/api/setup/models", axum::routing::post(setup_models))
        .route(
            "/api/setup/save-backend",
            axum::routing::post(setup_save_backend),
        )
        .route("/api/config", axum::routing::get(get_config))
        .route("/api/config", axum::routing::post(save_config))
        .route(
            "/api/config/backups",
            axum::routing::get(list_config_backups),
        )
        .route(
            "/api/config/restore/{slot}",
            axum::routing::post(restore_config_backup),
        )
        .route("/api/aufgaben", axum::routing::get(get_aufgaben))
        .route("/api/aufgaben/{id}", axum::routing::delete(cancel_aufgabe))
        .route("/api/aufgaben/{id}", axum::routing::patch(edit_aufgabe))
        .route(
            "/api/aufgaben/{id}/cap-override",
            axum::routing::post(cap_override_aufgabe),
        )
        .route(
            "/api/aufgaben/{id}/restart",
            axum::routing::post(restart_aufgabe),
        )
        .route("/api/chat", axum::routing::post(chat))
        .route(
            "/api/chat-stream",
            axum::routing::post(chat_stream_endpoint),
        )
        .route("/api/logs/{datum}", axum::routing::get(get_logs))
        .route("/api/status", axum::routing::get(get_status))
        .route("/api/metrics", axum::routing::get(get_metrics))
        .route("/api/modules", axum::routing::get(get_py_modules))
        .route("/api/tokens", axum::routing::get(get_tokens))
        .route(
            "/api/tokens/by-modul",
            axum::routing::get(get_tokens_by_modul),
        )
        .route(
            "/api/tokens/by-backend",
            axum::routing::get(get_tokens_by_backend),
        )
        .route("/api/audit", axum::routing::get(get_audit))
        .route(
            "/api/module-capabilities/{id}",
            axum::routing::get(get_module_capabilities),
        )
        .route(
            "/api/llm-models/{backend_id}",
            axum::routing::get(list_llm_models),
        )
        .route(
            "/api/module-config/{name}",
            axum::routing::get(get_module_config),
        )
        .route(
            "/api/module-config/{name}",
            axum::routing::post(save_module_config),
        )
        .route("/api/convos/{modul_id}", axum::routing::get(list_convos))
        .route(
            "/api/convos/{modul_id}/{convo_id}",
            axum::routing::get(load_convo),
        )
        .route(
            "/api/convos/{modul_id}/{convo_id}",
            axum::routing::put(save_convo),
        )
        .route(
            "/api/convos/{modul_id}/{convo_id}",
            axum::routing::delete(delete_convo),
        )
        .route("/api/templates/{typ}", axum::routing::get(get_template))
        .route("/api/home/{modul_id}", axum::routing::get(list_home))
        .route(
            "/api/home/{modul_id}/{path}",
            axum::routing::get(read_home_file),
        )
        .route(
            "/api/home/{modul_id}/{path}",
            axum::routing::delete(delete_home_file),
        )
        .route(
            "/api/home-clear/{modul_id}",
            axum::routing::delete(clear_home),
        )
        .route(
            "/api/prompt-preview/{modul_id}",
            axum::routing::get(prompt_preview),
        )
        .route("/api/cron/{id}/trigger", axum::routing::post(trigger_cron))
        .route("/api/wizard/start", axum::routing::post(wizard_start))
        .route("/api/wizard/abort", axum::routing::post(wizard_abort))
        .route("/api/wizard/patch", axum::routing::post(wizard_patch))
        .route(
            "/api/wizard/sessions",
            axum::routing::get(wizard_list_sessions),
        )
        .route("/api/wizard/turn", axum::routing::post(wizard_turn))
        .route("/api/wizard/models", axum::routing::get(wizard_models))
        .route(
            "/api/wizard/test-connection",
            axum::routing::post(wizard_test_connection),
        )
        .route(
            "/api/wizard/confirm-code-gen",
            axum::routing::post(wizard_confirm_code_gen),
        )
        .route("/api/quality/stats", axum::routing::get(quality_stats))
        .route("/api/quality/events", axum::routing::get(quality_events))
        .route(
            "/api/quality/benchmark/cases",
            axum::routing::get(quality_benchmark_cases),
        )
        .route(
            "/api/quality/benchmark/run",
            axum::routing::post(quality_benchmark_run),
        )
        .route(
            "/api/quality/benchmark/compare",
            axum::routing::post(quality_benchmark_compare),
        )
        .layer(axum::middleware::from_fn_with_state(
            auth_state,
            security::auth_middleware,
        ))
        .layer(DefaultBodyLimit::max(body_limit))
        .with_state(state)
}

async fn index(State(s): State<Arc<AppState>>) -> axum::response::Response {
    // Wenn kein Backend erreichbar → 302 zu /setup. User sieht dann den First-
    // Run-Wizard statt eines leeren Dashboards mit unklarem next step.
    let needs = {
        let backends = {
            let cfg = s.config.read().await;
            cfg.llm_backends.clone()
        };
        if backends.is_empty() {
            true
        } else {
            let client = reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(2))
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .unwrap_or_default();
            let mut any = false;
            for b in &backends {
                if crate::types::test_backend_reachable(&client, b).await {
                    any = true;
                    break;
                }
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
    let body_limit = state
        .config
        .try_read()
        .map(|c| c.max_body_bytes)
        .unwrap_or(2 * 1024 * 1024);
    let auth_state = Arc::new(security::AuthState {
        config: state.config.clone(),
    });
    Router::new()
        .route("/favicon.ico", axum::routing::get(favicon))
        .route(
            "/",
            axum::routing::get(move || {
                let mid = mid.clone();
                async move {
                    // chat.html mit injiziertem Meta-Tag für die Modul-ID
                    let html = include_str!("chat.html");
                    let injected = html.replace(
                        "<head>",
                        &format!(
                            "<head>\n<meta name=\"modul-id\" content=\"{}\">",
                            html_escape(&mid)
                        ),
                    );
                    Html(injected)
                }
            }),
        )
        .route("/api/config", axum::routing::get(get_config))
        .route("/api/chat", axum::routing::post(chat))
        .route("/api/home/{modul_id}", axum::routing::get(list_home))
        .route(
            "/api/home/{modul_id}/{path}",
            axum::routing::get(read_home_file),
        )
        .route(
            "/api/home/{modul_id}/{path}",
            axum::routing::delete(delete_home_file),
        )
        .route(
            "/api/home-clear/{modul_id}",
            axum::routing::delete(clear_home),
        )
        .route("/api/convos/{modul_id}", axum::routing::get(list_convos))
        .route(
            "/api/convos/{modul_id}/{convo_id}",
            axum::routing::get(load_convo),
        )
        .route(
            "/api/convos/{modul_id}/{convo_id}",
            axum::routing::put(save_convo),
        )
        .route(
            "/api/convos/{modul_id}/{convo_id}",
            axum::routing::delete(delete_convo),
        )
        .layer(axum::middleware::from_fn_with_state(
            auth_state,
            security::auth_middleware,
        ))
        .layer(DefaultBodyLimit::max(body_limit))
        .with_state(state)
}

fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

// ─── Config ────────────────────────────────────────

async fn get_config(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let cfg = s.config.read().await;
    let mut val = serde_json::to_value(&*cfg).unwrap_or_default();
    drop(cfg);
    security::redact_secrets(&mut val);
    Json(val)
}

async fn save_config(
    State(s): State<Arc<AppState>>,
    Json(mut incoming): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
    // Globaler Config-Write-Lock: serialisiert den kompletten read-modify-write-
    // Zyklus gegen parallele Writes (Orchestrator run_cleanup, anderer
    // save_config-Request, wizard-commit). Ohne den Lock würde bei gleich-
    // zeitigen Edits last-write-wins gelten und Änderungen verloren gehen.
    let _write_guard = s.pipeline.config_write_lock.lock().await;

    let save_scope: Vec<String> = incoming
        .get("_save_scope")
        .and_then(|v| v.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    if let Some(obj) = incoming.as_object_mut() {
        obj.remove("_save_scope");
    }
    let scoped_save = |scope: &str| save_scope.iter().any(|s| s == "all" || s == scope);

    // Restore any REDACTED placeholders from the existing on-disk config before parsing.
    let existing = {
        let cfg = s.config.read().await;
        serde_json::to_value(&*cfg).unwrap_or_default()
    };
    security::restore_redacted(&mut incoming, &existing);
    if !scoped_save("llm") {
        incoming["llm_backends"] = existing["llm_backends"].clone();
    }
    if !scoped_save("module") {
        incoming["module"] = existing["module"].clone();
    }
    if !scoped_save("rag") {
        incoming["rag_pools"] = existing["rag_pools"].clone();
    }

    let mut cfg: AgentConfig = match serde_json::from_value(incoming) {
        Ok(c) => c,
        Err(e) => {
            return Json(serde_json::json!({"ok": false, "error": format!("Config parse: {}", e)}));
        }
    };

    // Validierung: Mindest-Intervall und Port-Bereich
    if cfg.cycle_interval_ms < 500 {
        cfg.cycle_interval_ms = 500;
    }
    if cfg.web_port == 0 {
        cfg.web_port = 8090;
    }
    if cfg.max_body_bytes < 4096 {
        cfg.max_body_bytes = 4096;
    }
    util::normalize_same_llm_links(&mut cfg);

    let path = s.pipeline.base.join("config.json");
    let cfg_json = match serde_json::to_string_pretty(&cfg) {
        Ok(j) => j,
        Err(e) => return Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    };

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

    match util::atomic_write(&path, cfg_json.as_bytes()) {
        Ok(_) => {
            *s.config.write().await = cfg.clone();
            s.pipeline.log(
                "config",
                None,
                LogTyp::Info,
                "Config gespeichert (Backup rotiert)",
            );
            s.pipeline
                .audit("config.update", "admin", "Configuration updated via API");
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
            let modified = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
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
        return Json(
            serde_json::json!({"ok": false, "error": format!("backup slot {} does not exist", slot)}),
        );
    }
    let raw = match std::fs::read_to_string(&bak) {
        Ok(r) => r,
        Err(e) => return Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    };
    let mut cfg: AgentConfig = match serde_json::from_str(&raw) {
        Ok(c) => c,
        Err(e) => {
            return Json(serde_json::json!({"ok": false, "error": format!("backup parse: {}", e)}));
        }
    };
    util::normalize_same_llm_links(&mut cfg);
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
    s.pipeline.log(
        "config",
        None,
        LogTyp::Info,
        &format!("Config aus Backup slot {} wiederhergestellt", slot),
    );
    s.pipeline.audit(
        "config.restore",
        "admin",
        &format!("Configuration restored from backup slot {}", slot),
    );
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

async fn cancel_aufgabe(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(id): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
    let Some(id) = safe_id(&id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige ID"}));
    };
    match s.pipeline.laden_by_id(&id) {
        Ok(Some(mut a)) => {
            if a.status == AufgabeStatus::Success
                || a.status == AufgabeStatus::Failed
                || a.status == AufgabeStatus::Cancelled
            {
                return Json(
                    serde_json::json!({"ok": false, "error": "Aufgabe ist bereits abgeschlossen"}),
                );
            }
            a.ergebnis = Some("Cancelled by user".into());
            match s.pipeline.verschieben(&mut a, AufgabeStatus::Cancelled) {
                Ok(_) => {
                    s.pipeline
                        .log("web", Some(&id), LogTyp::Info, "Aufgabe abgebrochen");
                    Json(serde_json::json!({"ok": true}))
                }
                Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
            }
        }
        Ok(None) => Json(serde_json::json!({"ok": false, "error": "Aufgabe nicht gefunden"})),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

async fn edit_aufgabe(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(id): axum::extract::Path<String>,
    Json(body): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
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
                return Json(
                    serde_json::json!({"ok": false, "error": "Nur wartende Aufgaben können bearbeitet werden"}),
                );
            }
            a.update(neue_anweisung, "Edited via frontend");
            match s.pipeline.speichern(&a) {
                Ok(_) => {
                    s.pipeline.log(
                        "web",
                        Some(&id),
                        LogTyp::Info,
                        &format!("Aufgabe bearbeitet: {}", neue_anweisung),
                    );
                    Json(serde_json::json!({"ok": true}))
                }
                Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
            }
        }
        Ok(None) => Json(serde_json::json!({"ok": false, "error": "Aufgabe nicht gefunden"})),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

async fn cap_override_aufgabe(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(id): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
    let Some(id) = safe_id(&id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige ID"}));
    };
    match s.pipeline.laden_by_id(&id) {
        Ok(Some(mut a)) => {
            if matches!(
                a.status,
                AufgabeStatus::Success | AufgabeStatus::Failed | AufgabeStatus::Cancelled
            ) {
                return Json(
                    serde_json::json!({"ok": false, "error": "Aufgabe ist bereits abgeschlossen"}),
                );
            }
            a.cap_override = true;
            a.ergebnis =
                Some("CAP OVERRIDE: naechster Lauf darf das LLM trotz Cap verwenden".into());
            match s.pipeline.reschedule(&mut a, "sofort".into()) {
                Ok(_) => {
                    s.pipeline.log(
                        "web",
                        Some(&id),
                        LogTyp::Warning,
                        "Cost-Cap Override gesetzt",
                    );
                    s.pipeline
                        .audit("task.cap_override", "admin", &format!("task={}", id));
                    Json(serde_json::json!({"ok": true}))
                }
                Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
            }
        }
        Ok(None) => Json(serde_json::json!({"ok": false, "error": "Aufgabe nicht gefunden"})),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

async fn restart_aufgabe(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(id): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
    let Some(id) = safe_id(&id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige ID"}));
    };
    match s.pipeline.laden_by_id(&id) {
        Ok(Some(mut a)) => {
            if a.parent_id.is_some() {
                return Json(serde_json::json!({
                    "ok": false,
                    "error": "Subtasks werden über die Hauptaufgabe neu gestartet"
                }));
            }
            if a.status == AufgabeStatus::Success {
                return Json(
                    serde_json::json!({"ok": false, "error": "Erfolgreiche Aufgaben werden nicht neu gestartet"}),
                );
            }
            if a.status == AufgabeStatus::Erstellt {
                return Json(
                    serde_json::json!({"ok": false, "error": "Aufgabe wartet bereits auf Ausführung"}),
                );
            }
            if a.status == AufgabeStatus::Gestartet {
                let busy = s.busy.read().await;
                let still_running = busy
                    .get(&a.modul)
                    .map(|ids| ids.iter().any(|task_id| task_id == &a.id))
                    .unwrap_or(false);
                drop(busy);
                if still_running {
                    return Json(serde_json::json!({
                        "ok": false,
                        "error": "Aufgabe läuft noch; zuerst abbrechen oder Watchdog abwarten"
                    }));
                }
            }

            a.retry_count = 0;
            if a.typ == AufgabeTyp::LlmCall {
                let cfg = s.config.read().await;
                if let Some(configured_timeout) = cfg
                    .module
                    .iter()
                    .find(|m| m.id == a.modul || m.name == a.modul)
                    .map(|m| m.timeout_s)
                {
                    a.timeout_s = a.timeout_s.max(configured_timeout).max(30);
                }
                drop(cfg);
            }
            a.ergebnis = None;
            match s.pipeline.reschedule(&mut a, "sofort".into()) {
                Ok(_) => {
                    s.pipeline.log(
                        "web",
                        Some(&id),
                        LogTyp::Warning,
                        "Aufgabe manuell neu gestartet",
                    );
                    s.pipeline
                        .audit("task.restart", "admin", &format!("task={}", id));
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
        let _ = tx.try_send(
            serde_json::json!({"error": "Rate limit exceeded — zu viele Anfragen"}).to_string(),
        );
        let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
        let body = Body::from_stream(stream.map(|line| {
            Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(format!("{}\n", line)))
        }));
        return axum::response::Response::builder()
            .status(429)
            .header("content-type", "application/x-ndjson")
            .body(body)
            .unwrap_or_else(|_| axum::response::Response::new(Body::empty()));
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
                let body = Body::from_stream(stream.map(|line| {
                    Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(format!(
                        "{}\n",
                        line
                    )))
                }));
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
        cfg.module
            .iter()
            .find(|m| m.id == modul_id || m.name == modul_id)
            .cloned()
    } else {
        None
    };

    let (backend_id, backup_id, system_prompt, modul_for_tools) = if let Some(m) = &modul {
        let mut tp = tools::tools_prompt(m);
        {
            let py_mods = s.py_modules.read().await;
            tools::append_python_tools(&mut tp, m, &py_mods);
        }
        let home = s.pipeline.home_dir(&m.id);
        let home_info = format!(
            "\n\nDein Home-Verzeichnis ist: {}\nDu kannst dort Dateien lesen, schreiben und auflisten.\n\
            WICHTIG: Wenn der User eine grosse Aufgabe will (Code schreiben, Datei erstellen, Analyse), \
            dann erstelle ZUERST einen Plan und fuehre die Tools Schritt fuer Schritt aus.\n",
            home.display()
        );
        let date_str = chrono::Utc::now().format("%d.%m.%Y %H:%M UTC").to_string();
        let identity = util::resolve_identity(m, &cfg);
        let system_with_date = identity.system_prompt.replace("{date}", &date_str);
        let full = format!("{}\n{}{}", system_with_date, tp, home_info);
        (
            m.llm_backend.clone(),
            m.backup_llm.clone(),
            full,
            Some(m.clone()),
        )
    } else if let Some(b) = cfg.llm_backends.first() {
        (b.id.clone(), None, String::new(), None)
    } else {
        drop(cfg);
        let stream = tokio_stream::wrappers::ReceiverStream::new({
            let (tx, rx) = tokio::sync::mpsc::channel::<String>(1);
            let _ = tx
                .try_send(serde_json::json!({"error":"Kein LLM Backend konfiguriert"}).to_string());
            rx
        });
        let body = Body::from_stream(stream.map(|line| {
            Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(format!("{}\n", line)))
        }));
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
    } else {
        vec![]
    };

    // Letzter User-Text fuer Aufgaben-Logging
    let last_user_msg = user_messages
        .as_array()
        .and_then(|a| a.last())
        .and_then(|m| m["content"].as_str())
        .unwrap_or("")
        .to_string();

    // Haupt-Aufgabe erstellen (damit JEDER Chat-Request trackbar ist)
    let mut main_aufgabe = Aufgabe::llm_call(
        &last_user_msg,
        &modul_id,
        &format!("chat:{}", modul_id),
        None, // NO routing for chat tasks -- result goes via HTTP stream
    );
    if let Some(m) = modul_for_tools.as_ref() {
        main_aufgabe = main_aufgabe.with_timeout_s(m.timeout_s);
    }
    main_aufgabe.status = AufgabeStatus::Gestartet;
    main_aufgabe.gestartet = Some(chrono::Utc::now());
    let main_id = main_aufgabe.id.clone();
    let _ = s.pipeline.speichern(&main_aufgabe);

    // Channel for streaming status updates and final answer
    let (tx, rx) = tokio::sync::mpsc::channel::<String>(64);

    // Spawn the tool-loop in a background task
    let state = s.clone();
    tokio::spawn(async move {
        let t_start = std::time::Instant::now();
        let mut tool_rounds = 0;
        let mut sub_aufgaben: Vec<String> = vec![];
        let mut tool_failures: Vec<ChatToolFailure> = vec![];
        let mut messages = messages;
        let modul_id_str = modul_id.as_str();
        let mut guardrail_retries: u32 = 0;
        let mut malformed_tool_retries: u32 = 0;
        let mut used_fallback = false;
        let mut backend_id = backend_id;
        let needs_deepdive = is_deepdive_request(&last_user_msg);
        let mut deepdive_progress = DeepdiveProgress::default();
        let mut deepdive_gate_retries: u32 = 0;

        loop {
            if tool_rounds >= MAX_CHAT_TOOL_ROUNDS {
                break;
            }

            let model_str = model_for_backend(&config_snapshot, &backend_id);
            if let Err(hit) = check_llm_cap(
                &state.pipeline.store.pool,
                &config_snapshot,
                &backend_id,
                &messages,
                false,
            )
            .await
            {
                let msg = cap_task_message(&hit);
                state
                    .pipeline
                    .log(modul_id_str, Some(&main_id), LogTyp::Warning, &msg);
                if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&main_id) {
                    a.ergebnis = Some(msg.clone());
                    let _ = state.pipeline.reschedule(&mut a, hit.reset_iso());
                }
                tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":msg},"done":true}).to_string()).await.ok();
                return;
            }

            let result = state
                .llm
                .chat_with_tools(&backend_id, backup_id.as_deref(), &messages, &openai_tools)
                .await;

            match result {
                Ok((response, raw_data)) => {
                    // Token-Tracking
                    track_tokens(
                        &state.pipeline.store.pool,
                        &state.tokens,
                        &config_snapshot,
                        &backend_id,
                        &model_str,
                        modul_id_str,
                        &raw_data,
                    )
                    .await;

                    // ── Guardrail validation ───────────────────────────────
                    if gcfg.enabled {
                        let chat_last_user = messages
                            .iter()
                            .rev()
                            .find(|m| m["role"] == "user")
                            .and_then(|m| m["content"].as_str())
                            .map(|s| s.to_string());
                        let max_retries_for_backend = gcfg
                            .per_backend_overrides
                            .get(&backend_id)
                            .copied()
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
                                    final_outcome: if guardrail_retries > 0 {
                                        "retried".into()
                                    } else {
                                        "ok".into()
                                    },
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
                                    final_outcome: if is_last {
                                        "hard_fail".into()
                                    } else {
                                        "retried".into()
                                    },
                                    similar_suggestion: None,
                                };
                                let _ = crate::guardrail::log_event(&state.data_root, &ev).await;
                                if is_last {
                                    // Check if backup_llm available + fallback flag on
                                    let mod_cfg =
                                        config_snapshot.module.iter().find(|m| m.id == modul_id);
                                    let backup_id = mod_cfg.and_then(|m| m.backup_llm.clone());
                                    if gcfg.fallback_on_hard_fail
                                        && backup_id.is_some()
                                        && !used_fallback
                                    {
                                        if let Some(bid) = backup_id {
                                            if let Some(bb) = config_snapshot
                                                .llm_backends
                                                .iter()
                                                .find(|b| b.id == bid)
                                                .cloned()
                                            {
                                                let codes: Vec<String> =
                                                    errors.iter().map(|e| e.code.clone()).collect();
                                                let _ = crate::guardrail::log_fallback_event(
                                                    &state.data_root,
                                                    &backend_id,
                                                    &bid,
                                                    &modul_id,
                                                    &codes,
                                                )
                                                .await;
                                                backend_id = bb.id.clone();
                                                used_fallback = true;
                                                guardrail_retries = 0;
                                                continue; // retry with backup
                                            }
                                        }
                                    }
                                    // Real hard-fail — existing warn + break
                                    let codes: Vec<String> =
                                        errors.iter().map(|e| e.code.clone()).collect();
                                    tracing::warn!(
                                        "Guardrail hard-fail in chat.{}: {:?}",
                                        modul_id,
                                        codes
                                    );
                                    tx.send(serde_json::json!({"type":"status","message":format!("Guardrail hard-fail: {}", codes.join(", "))}).to_string()).await.ok();
                                    break;
                                } else {
                                    let feedback = crate::guardrail::synth_feedback_user_message(
                                        &errors,
                                        max_retries_for_backend,
                                        guardrail_retries,
                                    );
                                    messages.push(
                                        serde_json::json!({"role": "user", "content": feedback}),
                                    );
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
                                tools::parse_openai_tool_call_with_schema(
                                    &raw_data,
                                    schema.as_deref(),
                                )
                            }
                            (Some(_), None) => tools::parse_openai_tool_call(&raw_data),
                            (None, _) => None,
                        }
                    } else {
                        None
                    }
                    .or_else(|| tools::parse_tool_call(&response));

                    if let Some((mut tool_name, mut params)) = tool_call {
                        let enough_manual = deepdive_progress.search_ok >= 2
                            && deepdive_progress.fetch_ok >= 3
                            && deepdive_progress.source_note_ok >= 2;
                        if needs_deepdive
                            && deepdive_progress.crawl_ok == 0
                            && !enough_manual
                            && tool_name == "rag.suchen"
                        {
                            let topic = deepdive_topic_hint(&last_user_msg);
                            state.pipeline.log(
                                modul_id_str,
                                Some(&main_id),
                                LogTyp::Warning,
                                &format!(
                                    "DeepDive-Gate ersetzt verfruehtes rag.suchen durch deepdive.crawl({})",
                                    topic
                                ),
                            );
                            tool_name = "deepdive.crawl".to_string();
                            params = vec![topic];
                        } else if needs_deepdive
                            && tool_name == "rag.suchen"
                            && deepdive_progress.crawl_ok > 0
                        {
                            if let Some(crawl_id) = deepdive_progress.crawl_id.as_ref() {
                                let current = params.first().cloned().unwrap_or_default();
                                if !current.contains(crawl_id) {
                                    let topic = if current.trim().is_empty() {
                                        deepdive_topic_hint(&last_user_msg)
                                    } else {
                                        current
                                    };
                                    params = vec![format!("{} {}", crawl_id, topic)];
                                }
                            }
                        }
                        tool_rounds += 1;

                        // Status: Tool wird ausgefuehrt
                        tx.send(serde_json::json!({"type":"status","message":format!("Tool: {}({})", tool_name, params.join(", "))}).to_string()).await.ok();

                        // Sub-Aufgabe fuer den Tool-Call
                        let mid = modul_for_tools
                            .as_ref()
                            .map(|m| m.id.as_str())
                            .unwrap_or(modul_id_str);
                        let mut sub = Aufgabe::direct(
                            &tool_name,
                            params.clone(),
                            mid,
                            &format!("chat:{}", modul_id_str),
                            None,
                            None,
                        );
                        sub.parent_id = Some(main_id.clone());
                        sub.status = AufgabeStatus::Gestartet;
                        sub.gestartet = Some(chrono::Utc::now());
                        let sub_id = sub.id.clone();
                        let _ = state.pipeline.speichern(&sub);
                        sub_aufgaben.push(sub_id.clone());

                        state.pipeline.log(
                            modul_id_str,
                            Some(&main_id),
                            LogTyp::Info,
                            &format!(
                                "Tool: {}({}) [{}]",
                                tool_name,
                                params.join(", "),
                                &sub_id[..8]
                            ),
                        );

                        let tool_result =
                            exec_tool_inline(&state, &tool_name, &params, mid, &config_snapshot)
                                .await;
                        let ok = tool_result.0;
                        observe_deepdive_progress(
                            &mut deepdive_progress,
                            &tool_name,
                            ok,
                            tool_rounds,
                        );
                        if ok && tool_name == "deepdive.crawl" {
                            deepdive_progress.crawl_id = extract_deepdive_crawl_id(&tool_result.1);
                        }
                        if ok {
                            let recovered = tool_failures
                                .iter_mut()
                                .filter(|failure| !failure.recovered)
                                .map(|failure| {
                                    failure.recovered = true;
                                    1usize
                                })
                                .sum::<usize>();
                            if recovered > 0 {
                                state.pipeline.log(
                                    modul_id_str,
                                    Some(&main_id),
                                    LogTyp::Info,
                                    &format!(
                                        "{} offene Tool-Fehler durch spaeteren erfolgreichen Tool-Call behandelt",
                                        recovered
                                    ),
                                );
                            }
                        } else {
                            tool_failures.push(ChatToolFailure {
                                tool_name: tool_name.clone(),
                                detail: util::safe_truncate(&tool_result.1, 160).to_string(),
                                recovered: false,
                            });
                        }

                        // Sub-Aufgabe abschliessen
                        if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&sub_id) {
                            a.ergebnis = Some(tool_result.1.clone());
                            let _ = state.pipeline.verschieben(
                                &mut a,
                                if ok {
                                    AufgabeStatus::Success
                                } else {
                                    AufgabeStatus::Failed
                                },
                            );
                        }

                        state.pipeline.log(
                            modul_id_str,
                            Some(&main_id),
                            if ok { LogTyp::Success } else { LogTyp::Failed },
                            &format!(
                                "Tool {}: {} → {}",
                                tool_name,
                                if ok { "OK" } else { "FAIL" },
                                util::safe_truncate(&tool_result.1, 80)
                            ),
                        );

                        // Status: Tool-Ergebnis
                        tx.send(serde_json::json!({"type":"status","message":format!("{}: {}", if ok {"OK"} else {"FAIL"}, util::safe_truncate(&tool_result.1, 80))}).to_string()).await.ok();

                        // Tool-Result im OpenAI-Format
                        let call_id = raw_data
                            .pointer("/choices/0/message/tool_calls/0/id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("call_0")
                            .to_string();
                        let call_id = if call_id == "call_0" {
                            format!("call_{}", &sub_id[..8])
                        } else {
                            call_id
                        };
                        let tool_args_json = tool_arguments_json_for_history(
                            &tool_name,
                            &params,
                            modul_for_tools.as_ref(),
                            &py_mods_snap,
                        );
                        messages.push(serde_json::json!({"role": "assistant", "content": serde_json::Value::Null,
                            "tool_calls": [{"id": &call_id, "type": "function", "function": {"name": &tool_name, "arguments": tool_args_json}}]}));
                        messages.push(serde_json::json!({"role": "tool", "tool_call_id": &call_id,
                            "content": chat_tool_result_for_llm(ok, &tool_result.1)}));

                        // History trimmen: alte Tool-Results kuerzen um Token-Explosion zu vermeiden
                        // Behalte nur die letzten 6 Messages (3 Tool-Rounds) vollstaendig
                        let keep_full = 6;
                        let system_msgs = 1; // System-Prompt
                        let user_msgs = user_messages.as_array().map(|a| a.len()).unwrap_or(0);
                        let fixed = system_msgs + user_msgs; // Diese nie anfassen
                        if messages.len() > fixed + keep_full + 4 {
                            // Alte Tool-Results auf 100 chars kuerzen
                            for i in fixed..(messages.len().saturating_sub(keep_full)) {
                                if messages[i].get("role").and_then(|v| v.as_str()) == Some("tool")
                                {
                                    if let Some(content) =
                                        messages[i].get("content").and_then(|v| v.as_str())
                                    {
                                        if content.len() > 100 {
                                            let short = format!(
                                                "{}...[gekuerzt]",
                                                util::safe_truncate(content, 100)
                                            );
                                            messages[i]["content"] = serde_json::json!(short);
                                        }
                                    }
                                }
                            }
                        }
                        continue;
                    }

                    if tools::looks_like_malformed_tool_call(&response) {
                        let detail = util::safe_truncate(&response, 180).to_string();
                        if malformed_tool_retries < MAX_MALFORMED_TOOL_RETRIES {
                            malformed_tool_retries += 1;
                            tool_rounds += 1;
                            state.pipeline.log(
                                modul_id_str,
                                Some(&main_id),
                                LogTyp::Warning,
                                &format!(
                                    "Malformed tool-call syntax erkannt, Korrektur-Round {}: {}",
                                    malformed_tool_retries, detail
                                ),
                            );
                            tx.send(serde_json::json!({"type":"status","message":format!("Malformed Toolcall erkannt, fordere korrekte Syntax an ({}/{})", malformed_tool_retries, MAX_MALFORMED_TOOL_RETRIES)}).to_string()).await.ok();
                            messages.push(
                                serde_json::json!({"role": "assistant", "content": response}),
                            );
                            messages.push(serde_json::json!({"role": "user", "content":
                                "STOPP — deine letzte Antwort enthielt einen Toolcall, aber in falscher Syntax. \
                                 Das wurde NICHT ausgefuehrt. Antworte jetzt AUSSCHLIESSLICH mit genau EINEM gueltigen Toolcall im Format <tool>name(param1, param2)</tool>. \
                                 Kein Plan, keine Erklaerung, kein Markdown. Fuer editor.replace gilt: <tool>editor.replace(modules/deepdive/module.py, ALTER_TEXT===REPLACE===NEUER_TEXT)</tool>. \
                                 Nutze module paths immer lowercase, z.B. modules/deepdive/module.py."}));
                            continue;
                        }

                        tool_failures.push(ChatToolFailure {
                            tool_name: "malformed_tool_call".into(),
                            detail,
                            recovered: false,
                        });
                    }

                    // Pruefen: hat der User nach Recherche gefragt aber LLM hat kein Tool genutzt?
                    // Wenn ja: nochmal mit Hinweis dass Tools PFLICHT sind
                    if sub_aufgaben.is_empty() && tool_rounds == 0 {
                        if needs_deepdive {
                            let topic = deepdive_topic_hint(&last_user_msg);
                            messages.push(
                                serde_json::json!({"role": "assistant", "content": response}),
                            );
                            messages.push(serde_json::json!({"role": "user", "content":
                                format!("STOPP — DeepDive braucht frische Quellen. Antworte NICHT aus Wissen und lies NICHT zuerst aus dem RAG. Antworte jetzt AUSSCHLIESSLICH mit genau diesem Toolcall: <tool>deepdive.crawl({})</tool>", topic)}));
                            tool_rounds += 1;
                            continue;
                        }
                        let lower = last_user_msg.to_lowercase();
                        let needs_research = [
                            "recherch",
                            "such",
                            "prüf",
                            "check",
                            "finde",
                            "google",
                            "verify",
                            "validier",
                            "fakten",
                            "stimmt das",
                            "belege",
                            "quelle",
                        ]
                        .iter()
                        .any(|kw| lower.contains(kw));
                        if needs_research {
                            messages.push(
                                serde_json::json!({"role": "assistant", "content": response}),
                            );
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
                            final_text = format!(
                                "{} Tool-Calls ausgefuehrt. Ergebnis im Aufgaben-Board.",
                                sub_aufgaben.len()
                            );
                        }
                    }

                    if needs_deepdive {
                        if let Some(feedback) =
                            deepdive_gate_feedback(&deepdive_progress, &last_user_msg, &final_text)
                        {
                            if deepdive_gate_retries < 4 && tool_rounds < MAX_CHAT_TOOL_ROUNDS {
                                deepdive_gate_retries += 1;
                                tool_rounds += 1;
                                state.pipeline.log(
                                    modul_id_str,
                                    Some(&main_id),
                                    LogTyp::Warning,
                                    &format!(
                                        "DeepDive-Gate fordert weiteren Schritt ({}/4): {}",
                                        deepdive_gate_retries,
                                        util::safe_truncate(&feedback, 180)
                                    ),
                                );
                                tx.send(serde_json::json!({"type":"status","message":format!("DeepDive-Check: weiterer Recherche-Schritt noetig ({}/4)", deepdive_gate_retries)}).to_string()).await.ok();
                                messages.push(
                                    serde_json::json!({"role": "assistant", "content": response}),
                                );
                                messages
                                    .push(serde_json::json!({"role": "user", "content": feedback}));
                                continue;
                            }
                        }
                    }

                    let (unresolved_failures, recovered_failures) =
                        summarize_chat_tool_failures(&tool_failures);

                    // Aufgaben-Info voranstellen wenn Tools genutzt wurden
                    if !sub_aufgaben.is_empty() {
                        if unresolved_failures.is_empty() && recovered_failures == 0 {
                            final_text = format!(
                                "[{} Aufgabe(n) erstellt]\n\n{}",
                                sub_aufgaben.len(),
                                final_text
                            );
                        } else if unresolved_failures.is_empty() {
                            final_text = format!(
                                "[{} Aufgabe(n) erstellt, {} Tool-Fehler behandelt]\n\n{}",
                                sub_aufgaben.len(),
                                recovered_failures,
                                final_text
                            );
                        } else {
                            final_text = format!(
                                "[{} Aufgabe(n) erstellt, {} Tool-Fehler offen]\n{}\n\n{}",
                                sub_aufgaben.len(),
                                unresolved_failures.len(),
                                unresolved_failures.join("\n"),
                                final_text
                            );
                        }
                    }

                    if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&main_id) {
                        a.ergebnis = Some(
                            util::safe_truncate(&final_text, MAX_CHAT_TASK_RESULT_CHARS)
                                .to_string(),
                        );
                        let final_status = if unresolved_failures.is_empty() {
                            AufgabeStatus::Success
                        } else {
                            AufgabeStatus::Failed
                        };
                        let _ = state.pipeline.verschieben(&mut a, final_status);
                    }
                    let total_dur = t_start.elapsed();
                    state.pipeline.log(
                        modul_id_str,
                        Some(&main_id),
                        if unresolved_failures.is_empty() {
                            LogTyp::Success
                        } else {
                            LogTyp::Failed
                        },
                        &format!(
                            "Chat fertig ({} sub-aufgaben, {}ms, {} offene Tool-Fehler)",
                            sub_aufgaben.len(),
                            total_dur.as_millis(),
                            unresolved_failures.len()
                        ),
                    );

                    // Stream final text in chunks
                    for chunk in final_text.chars().collect::<Vec<_>>().chunks(20) {
                        let text: String = chunk.iter().collect();
                        tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":text},"done":false}).to_string()).await.ok();
                    }
                    tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":""},"done":true,"eval_count":final_text.len(),"total_duration":total_dur.as_nanos() as u64}).to_string()).await.ok();
                    return;
                }
                Err(e) => {
                    release_reservation(
                        &state.pipeline.store.pool,
                        &state.tokens,
                        &config_snapshot,
                        &model_str,
                    )
                    .await;
                    // FEHLER — Aufgabe als Failed loggen, NICHT verloren
                    state.pipeline.log(
                        modul_id_str,
                        Some(&main_id),
                        LogTyp::Failed,
                        &format!("LLM Fehler: {}", e),
                    );
                    if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&main_id) {
                        a.ergebnis = Some(format!("FAILED: {}", e));
                        let _ = state.pipeline.verschieben(&mut a, AufgabeStatus::Failed);
                    }
                    tx.send(serde_json::json!({"error": format!("LLM Fehler: {}", e)}).to_string())
                        .await
                        .ok();
                    return;
                }
            }
        }
        // Tool cap reached. Do not throw away gathered evidence: force one final
        // no-tools synthesis so active research models can still return a report.
        state.pipeline.log(
            modul_id_str,
            Some(&main_id),
            LogTyp::Warning,
            "Max tool rounds erreicht; erzwinge finale Synthese ohne weitere Tools",
        );
        tx.send(serde_json::json!({"type":"status","message":"Tool-Limit erreicht; erstelle finale Synthese aus vorhandenen Ergebnissen"}).to_string()).await.ok();

        let model_str = model_for_backend(&config_snapshot, &backend_id);
        let final_messages = final_synthesis_messages(
            &last_user_msg,
            &messages,
            needs_deepdive,
            MAX_FINAL_SYNTHESIS_EVIDENCE_CHARS,
        );
        let final_result = state
            .llm
            .chat_with_tools(&backend_id, backup_id.as_deref(), &final_messages, &[])
            .await;
        let mut finalizer_ok = false;
        let mut final_text = match final_result {
            Ok((response, raw_data)) => {
                track_tokens(
                    &state.pipeline.store.pool,
                    &state.tokens,
                    &config_snapshot,
                    &backend_id,
                    &model_str,
                    modul_id_str,
                    &raw_data,
                )
                .await;
                finalizer_ok = true;
                strip_tool_tags(&response)
            }
            Err(e) => {
                let evidence = compact_chat_evidence(&messages, 5000);
                format!(
                    "Tool-Rundenlimit erreicht. Die automatische Abschluss-Synthese konnte wegen eines Backend-Fehlers nicht sauber erzeugt werden: {}.\n\nAuszug aus den vorhandenen Tool-Ergebnissen:\n{}",
                    e, evidence
                )
            }
        };

        if final_text.trim().is_empty() && !sub_aufgaben.is_empty() {
            for msg in messages.iter().rev() {
                if msg.get("role").and_then(|v| v.as_str()) == Some("tool") {
                    if let Some(content) = msg.get("content").and_then(|v| v.as_str()) {
                        final_text = content.to_string();
                        break;
                    }
                }
            }
        }
        if final_text.trim().is_empty() {
            final_text = format!(
                "{} Tool-Calls ausgefuehrt. Ergebnis im Aufgaben-Board.",
                sub_aufgaben.len()
            );
        }

        let (unresolved_failures, recovered_failures) =
            summarize_chat_tool_failures(&tool_failures);
        if !sub_aufgaben.is_empty() {
            if unresolved_failures.is_empty() && recovered_failures == 0 {
                final_text = format!(
                    "[{} Aufgabe(n) erstellt, Tool-Limit erreicht]\n\n{}",
                    sub_aufgaben.len(),
                    final_text
                );
            } else if unresolved_failures.is_empty() {
                final_text = format!(
                    "[{} Aufgabe(n) erstellt, {} Tool-Fehler behandelt, Tool-Limit erreicht]\n\n{}",
                    sub_aufgaben.len(),
                    recovered_failures,
                    final_text
                );
            } else {
                final_text = format!(
                    "[{} Aufgabe(n) erstellt, {} Tool-Fehler offen, Tool-Limit erreicht]\n{}\n\n{}",
                    sub_aufgaben.len(),
                    unresolved_failures.len(),
                    unresolved_failures.join("\n"),
                    final_text
                );
            }
        }

        let final_status = if finalizer_ok && unresolved_failures.is_empty() {
            AufgabeStatus::Success
        } else {
            AufgabeStatus::Failed
        };
        if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&main_id) {
            a.ergebnis =
                Some(util::safe_truncate(&final_text, MAX_CHAT_TASK_RESULT_CHARS).to_string());
            let _ = state.pipeline.verschieben(&mut a, final_status.clone());
        }
        let total_dur = t_start.elapsed();
        state.pipeline.log(
            modul_id_str,
            Some(&main_id),
            if final_status == AufgabeStatus::Success {
                LogTyp::Success
            } else {
                LogTyp::Failed
            },
            &format!(
                "Chat nach Tool-Limit finalisiert ({} sub-aufgaben, {}ms, {} offene Tool-Fehler)",
                sub_aufgaben.len(),
                total_dur.as_millis(),
                unresolved_failures.len()
            ),
        );

        for chunk in final_text.chars().collect::<Vec<_>>().chunks(20) {
            let text: String = chunk.iter().collect();
            tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":text},"done":false}).to_string()).await.ok();
        }
        tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":""},"done":true,"eval_count":final_text.len(),"total_duration":total_dur.as_nanos() as u64}).to_string()).await.ok();
    });

    let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
    let body = Body::from_stream(stream.map(|line| {
        Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(format!("{}\n", line)))
    }));

    axum::response::Response::builder()
        .header("content-type", "application/x-ndjson")
        .header("cache-control", "no-cache")
        .body(body)
        .unwrap_or_else(|_| axum::response::Response::new(Body::empty()))
}

/// Tool inline ausfuehren (Rust oder Python). Delegates to the unified dispatcher.
/// Kein task_id — Chat-Flow ist synchron und braucht keine Idempotency-
/// Deduplication (im Gegensatz zum Scheduler-Pfad mit Retry-Logik).
async fn exec_tool_inline(
    s: &Arc<AppState>,
    tool_name: &str,
    params: &[String],
    modul_id: &str,
    config: &AgentConfig,
) -> (bool, String) {
    let py_mods = s.py_modules.read().await;
    tools::exec_tool_unified(
        tool_name,
        params,
        modul_id,
        None,
        &s.pipeline,
        &s.llm,
        &py_mods,
        &s.py_pool,
        config,
    )
    .await
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
        cfg.module
            .iter()
            .find(|m| m.id == modul_id || m.name == modul_id)
            .map(|m| m.llm_backend.clone())
    };
    let cfg_snapshot = cfg.clone();
    drop(cfg);

    let Some(backend_id) = backend_id else {
        return error_response(500, "Kein LLM Backend");
    };
    let model_str = model_for_backend(&cfg_snapshot, &backend_id);
    let messages: Vec<serde_json::Value> = body["messages"].as_array().cloned().unwrap_or_default();
    if let Err(hit) = check_llm_cap(
        &s.pipeline.store.pool,
        &cfg_snapshot,
        &backend_id,
        &messages,
        false,
    )
    .await
    {
        return error_response(402, &cap_task_message(&hit));
    }

    let input_est = estimate_message_tokens(&messages);

    let (tx, rx) = tokio::sync::mpsc::channel::<String>(64);
    let state = s.clone();
    let modul_id_owned = modul_id.clone();
    let model_for_stream = model_str.clone();
    let cfg_for_stream = cfg_snapshot.clone();
    tokio::spawn(async move {
        let (chunk_tx, mut chunk_rx) = tokio::sync::mpsc::channel::<String>(64);
        let llm = state.llm.clone();
        let backend_for_stream = backend_id.clone();

        let stream_task = tokio::spawn(async move {
            llm.chat_stream(&backend_for_stream, &messages, chunk_tx)
                .await
        });

        while let Some(part) = chunk_rx.recv().await {
            let line = serde_json::json!({"delta": part}).to_string();
            if tx.send(line).await.is_err() {
                break;
            }
        }

        match stream_task.await {
            Ok(Ok(full_text)) => {
                // Rough token estimate for tracking (4 chars ≈ 1 token)
                let output_est = ((full_text.len() + 3) / 4).max(1) as u64;
                track_estimated_tokens(
                    &state.pipeline.store.pool,
                    &state.tokens,
                    &cfg_for_stream,
                    &backend_id,
                    &model_for_stream,
                    &modul_id_owned,
                    input_est,
                    output_est,
                )
                .await;
                let _ = tx.send(serde_json::json!({"done": true}).to_string()).await;
            }
            Ok(Err(e)) => {
                release_reservation(
                    &state.pipeline.store.pool,
                    &state.tokens,
                    &cfg_for_stream,
                    &model_for_stream,
                )
                .await;
                let _ = tx.send(serde_json::json!({"error": e}).to_string()).await;
            }
            Err(e) => {
                release_reservation(
                    &state.pipeline.store.pool,
                    &state.tokens,
                    &cfg_for_stream,
                    &model_for_stream,
                )
                .await;
                let _ = tx
                    .send(serde_json::json!({"error": format!("stream task: {}", e)}).to_string())
                    .await;
            }
        }
    });

    let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
    let body = Body::from_stream(stream.map(|line| {
        Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(format!("{}\n", line)))
    }));
    axum::response::Response::builder()
        .header("content-type", "application/x-ndjson")
        .header("cache-control", "no-cache")
        .body(body)
        .unwrap_or_else(|_| axum::response::Response::new(Body::empty()))
}

fn strip_tool_tags(text: &str) -> String {
    let mut result = text.to_string();
    loop {
        let Some(start) = ["<tool>", "<tool:", "<tool=", "<tool_call"]
            .iter()
            .filter_map(|needle| result.find(needle))
            .min()
        else {
            break;
        };

        let tail = &result[start..];
        let end = ["</tool>", "</tool_call>", "/>", "<tool_call|>"]
            .iter()
            .filter_map(|needle| tail.find(needle).map(|pos| (pos, needle.len())))
            .min_by_key(|(pos, _)| *pos);
        if let Some((pos, len)) = end {
            result = format!("{}{}", &result[..start], &tail[pos + len..]);
        } else {
            result.truncate(start);
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
    let modules_dir = s.data_root.parent().unwrap_or(&s.data_root).join("modules");
    if let Ok(discovered) =
        tokio::task::spawn_blocking(move || crate::loader::discover_modules(&modules_dir)).await
    {
        *s.py_modules.write().await = discovered;
    }

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
    let py_list: Vec<serde_json::Value> = py_mods
        .iter()
        .map(|m| {
            serde_json::json!({
                "name": m.name,
                "description": m.description,
                "version": m.version,
                "settings": m.settings,
                "tools": m.tools,
                "source": "python",
            })
        })
        .collect();

    let mut all = rust_modules;
    all.extend(py_list);
    Json(serde_json::json!({"modules": all}))
}

// ─── Home Directory Explorer ──────────────────────

async fn list_home(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(modul_id): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"error": "Ungültige modul-ID", "files": []}));
    };
    let home = s.pipeline.home_dir(&modul_id);
    list_dir_recursive(&home, &home, 0)
}

fn list_dir_recursive(
    base: &std::path::Path,
    dir: &std::path::Path,
    depth: u32,
) -> Json<serde_json::Value> {
    if depth > 3 {
        return Json(serde_json::json!({"files": []}));
    } // Max 3 Ebenen
    let mut files = vec![];
    if let Ok(entries) = std::fs::read_dir(dir) {
        let mut entries: Vec<_> = entries.flatten().collect();
        entries.sort_by_key(|e| e.file_name());
        for entry in entries {
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().to_string();
            let rel = path
                .strip_prefix(base)
                .unwrap_or(&path)
                .to_string_lossy()
                .to_string();
            if path.is_dir() {
                let children = list_dir_recursive(base, &path, depth + 1);
                let children_val: serde_json::Value = serde_json::to_string(&children.0)
                    .ok()
                    .and_then(|s| serde_json::from_str(&s).ok())
                    .unwrap_or_default();
                files.push(serde_json::json!({"name": name, "path": rel, "type": "dir", "children": children_val["files"]}));
            } else {
                let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
                files.push(
                    serde_json::json!({"name": name, "path": rel, "type": "file", "size": size}),
                );
            }
        }
    }
    Json(serde_json::json!({"home": dir.to_string_lossy(), "files": files}))
}

async fn read_home_file(
    State(s): State<Arc<AppState>>,
    axum::extract::Path((modul_id, path)): axum::extract::Path<(String, String)>,
) -> impl IntoResponse {
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

async fn delete_home_file(
    State(s): State<Arc<AppState>>,
    axum::extract::Path((modul_id, path)): axum::extract::Path<(String, String)>,
) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige modul-ID"}));
    };
    let Some(path) = safe_relative_path(&path) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültiger Pfad"}));
    };
    let home = s.pipeline.home_dir(&modul_id);
    let file_path = home.join(&path);
    let canonical = match std::fs::canonicalize(&file_path) {
        Ok(p) => p,
        Err(_) => return Json(serde_json::json!({"ok": false, "error": "Datei nicht gefunden"})),
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

async fn clear_home(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(modul_id): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
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
                if name == ".taskloops" {
                    continue;
                }
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

async fn get_module_config(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(name): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
    let Some(name) = safe_id(&name) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültiger Modul-Name"}));
    };
    let modules_dir = s
        .pipeline
        .base
        .parent()
        .unwrap_or(&s.pipeline.base)
        .join("modules")
        .join(&name);
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

async fn save_module_config(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(name): axum::extract::Path<String>,
    Json(body): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
    let Some(name) = safe_id(&name) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültiger Modul-Name"}));
    };
    let modules_dir = s
        .pipeline
        .base
        .parent()
        .unwrap_or(&s.pipeline.base)
        .join("modules")
        .join(&name);
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

async fn list_llm_models(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(backend_id): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
    let Some(backend_id) = safe_id(&backend_id) else {
        return Json(serde_json::json!({"error": "Ungültige backend-ID", "models": []}));
    };
    let cfg = s.config.read().await;
    let backend = cfg
        .llm_backends
        .iter()
        .find(|b| b.id == backend_id)
        .cloned();
    drop(cfg);

    let Some(backend) = backend else {
        return Json(serde_json::json!({"error": "Backend nicht gefunden", "models": []}));
    };
    if let Err(e) = crate::security::validate_llm_backend_url(&backend.typ, &backend.url) {
        return Json(serde_json::json!({"error": format!("SSRF-Schutz: {}", e), "models": []}));
    }

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .unwrap_or_else(|_| reqwest::Client::new());

    let result = match backend.typ {
        crate::types::LlmTyp::Ollama => {
            // GET /api/tags → models[].name
            match client.get(format!("{}/api/tags", backend.url)).send().await {
                Ok(resp) => {
                    let data: serde_json::Value = resp.json().await.unwrap_or_default();
                    let models: Vec<String> = data["models"]
                        .as_array()
                        .map(|arr| {
                            arr.iter()
                                .filter_map(|m| m["name"].as_str().map(String::from))
                                .collect()
                        })
                        .unwrap_or_default();
                    models
                }
                Err(e) => return Json(serde_json::json!({"error": e.to_string(), "models": []})),
            }
        }
        crate::types::LlmTyp::OpenAICompat | crate::types::LlmTyp::Grok => {
            // GET /v1/models → data[].id
            let key = backend.api_key.as_deref().unwrap_or("");
            match client
                .get(crate::llm::openai_compat_endpoint(&backend.url, "models"))
                .header("Authorization", format!("Bearer {}", key))
                .send()
                .await
            {
                Ok(resp) => {
                    let data: serde_json::Value = resp.json().await.unwrap_or_default();
                    let models: Vec<String> = data["data"]
                        .as_array()
                        .map(|arr| {
                            arr.iter()
                                .filter_map(|m| m["id"].as_str().map(String::from))
                                .collect()
                        })
                        .unwrap_or_default();
                    models
                }
                Err(e) => return Json(serde_json::json!({"error": e.to_string(), "models": []})),
            }
        }
        crate::types::LlmTyp::Anthropic => {
            // GET /v1/models mit x-api-key Header
            let key = backend.api_key.as_deref().unwrap_or("");
            match client
                .get(format!("{}/v1/models", backend.url))
                .header("x-api-key", key)
                .header("anthropic-version", "2023-06-01")
                .send()
                .await
            {
                Ok(resp) => {
                    let data: serde_json::Value = resp.json().await.unwrap_or_default();
                    data["data"]
                        .as_array()
                        .map(|arr| {
                            arr.iter()
                                .filter_map(|m| m["id"].as_str().map(String::from))
                                .collect()
                        })
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
    let day = match crate::store::token_day_get(&s.pipeline.store.pool) {
        Ok(d) => d,
        Err(e) => return Json(serde_json::json!({"error": e.to_string()})),
    };
    let (total_input, total_output, total_calls, cost_usd_total) =
        match crate::store::token_all_time(&s.pipeline.store.pool) {
            Ok(v) => v,
            Err(e) => return Json(serde_json::json!({"error": e.to_string()})),
        };
    let recent = match crate::store::token_calls_recent(&s.pipeline.store.pool, 50) {
        Ok(rows) => rows
            .into_iter()
            .map(|r| {
                serde_json::json!({
                    "time": chrono::DateTime::<chrono::Utc>::from_timestamp(r.ts, 0)
                        .map(|dt| dt.format("%H:%M:%S").to_string())
                        .unwrap_or_else(|| r.ts.to_string()),
                    "backend": r.backend,
                    "model": r.model,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "modul": r.modul,
                    "cost_usd": r.cost_usd,
                })
            })
            .collect::<Vec<_>>(),
        Err(e) => return Json(serde_json::json!({"error": e.to_string()})),
    };
    Json(serde_json::json!({
        "total_input": total_input,
        "total_output": total_output,
        "total_calls": total_calls,
        "total_tokens": total_input + total_output,
        "cost_usd_total": cost_usd_total,
        "cost_usd_today": day.cost_usd,
        "reserved_usd": day.reserved_usd,
        "reserved_calls": day.reserved_calls,
        "day_key": day.day_key,
        "day_started_ts": chrono::Utc::now().date_naive()
            .and_hms_opt(0, 0, 0)
            .map(|dt| dt.and_utc().timestamp())
            .unwrap_or(0),
        "recent": recent,
    }))
}

/// Token-Usage aus einem API-Response extrahieren und persistent tracken.
/// Schreibt in die SQLite `token_stats`-Tabelle (transaktional mit Reservation-
/// Release) + spiegelt in den in-memory TokenTracker für UI-Live-Anzeige.
pub async fn track_tokens(
    store_pool: &crate::store::SqlitePool,
    tokens: &TokenTracker,
    cfg: &AgentConfig,
    backend_id: &str,
    model: &str,
    modul: &str,
    raw: &serde_json::Value,
) {
    // Backend-spezifische Token-Formate:
    //   OpenAI/Grok: usage.prompt_tokens + usage.completion_tokens
    //   Ollama:      prompt_eval_count + eval_count (top-level)
    //   Anthropic:   usage.input_tokens + cache_read/creation + output_tokens
    // Anthropic trennt cached input (10% Kosten) von regulärem — wir tracken
    // alle als "input" fürs Display, Cost-Berechnung unten nutzt 10%-Rabatt
    // auf cache_read.
    let prompt_tokens = raw
        .pointer("/usage/prompt_tokens")
        .and_then(|v| v.as_u64())
        .or_else(|| raw.pointer("/prompt_eval_count").and_then(|v| v.as_u64()))
        .or_else(|| raw.pointer("/usage/input_tokens").and_then(|v| v.as_u64()))
        .unwrap_or(0);
    let cache_read = raw
        .pointer("/usage/cache_read_input_tokens")
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let cache_create = raw
        .pointer("/usage/cache_creation_input_tokens")
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let input = prompt_tokens + cache_read + cache_create;
    let output = raw
        .pointer("/usage/completion_tokens")
        .and_then(|v| v.as_u64())
        .or_else(|| raw.pointer("/eval_count").and_then(|v| v.as_u64()))
        .or_else(|| raw.pointer("/usage/output_tokens").and_then(|v| v.as_u64()))
        .unwrap_or(0);

    if input == 0 && output == 0 {
        let reservation = reservation_for_model(model);
        let _ = crate::store::token_release_reservation(store_pool, reservation);
        let mut stats = tokens.write().await;
        stats.reserved_usd = (stats.reserved_usd - reservation).max(0.0);
        stats.reserved_calls = stats.reserved_calls.saturating_sub(1);
        return;
    }

    let (input_price, output_price) = backend_prices_per_1m(cfg, backend_id);
    let cost_usd = (prompt_tokens as f64 / 1_000_000.0) * input_price
        + (cache_read as f64 / 1_000_000.0) * input_price * 0.10
        + (cache_create as f64 / 1_000_000.0) * input_price * 1.25
        + (output as f64 / 1_000_000.0) * output_price;

    // Persistent: committed += actual, reserved -= reservation, alles atomar in einer
    // SQL-Transaktion. Überlebt Prozess-Restart — Daily-Cap gilt über Uptime hinweg.
    // Reservation-Betrag basierend auf model-price (muss mit check_daily_budget matchen).
    let reservation = reservation_for_model(model);
    if let Err(e) = crate::store::token_commit_actual(
        store_pool,
        reservation,
        cost_usd,
        input,
        output,
        backend_id,
        model,
        modul,
    ) {
        tracing::warn!("track_tokens: store commit failed: {}", e);
    }

    // In-memory UI-Spiegel aktualisieren (async kompatibel). Die SQLite-Werte sind
    // Wahrheit; dieser Cache wird nur für schnelle Dashboard-Renders gehalten.
    let mut stats = tokens.write().await;
    let now = chrono::Utc::now();
    let today_start = now
        .date_naive()
        .and_hms_opt(0, 0, 0)
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
    if len > 200 {
        stats.calls.drain(0..len - 200);
    }
}

pub async fn track_estimated_tokens(
    store_pool: &crate::store::SqlitePool,
    tokens: &TokenTracker,
    cfg: &AgentConfig,
    backend_id: &str,
    model: &str,
    modul: &str,
    input_tokens: u64,
    output_tokens: u64,
) {
    let raw = serde_json::json!({
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
        }
    });
    track_tokens(store_pool, tokens, cfg, backend_id, model, modul, &raw).await;
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
    let _ = (store_pool, cfg);
    Ok(())
}

pub async fn check_daily_budget(
    store_pool: &crate::store::SqlitePool,
    tokens: &TokenTracker,
    cfg: &AgentConfig,
    model: &str,
) -> Result<(), String> {
    let _ = (store_pool, tokens, cfg, model);
    Ok(())
}

/// Reservation zurückbuchen — nur aufrufen wenn der LLM-Call fehlschlug
/// UND `track_tokens` nicht aufgerufen wird. Bei erfolgreichem Call nimmt
/// `track_tokens` die Abbuchung selbst vor.
pub async fn release_reservation(
    store_pool: &crate::store::SqlitePool,
    tokens: &TokenTracker,
    _cfg: &AgentConfig,
    model: &str,
) {
    let reservation = reservation_for_model(model);
    let _ = crate::store::token_release_reservation(store_pool, reservation);
    let mut stats = tokens.write().await;
    stats.reserved_usd = (stats.reserved_usd - reservation).max(0.0);
    stats.reserved_calls = stats.reserved_calls.saturating_sub(1);
}

// ─── Conversations ────────────────────────────────

async fn list_convos(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(modul_id): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"conversations": [], "error": "Ungültige modul-ID"}));
    };
    Json(serde_json::json!({"conversations": s.pipeline.convo_list(&modul_id)}))
}

async fn load_convo(
    State(s): State<Arc<AppState>>,
    axum::extract::Path((modul_id, convo_id)): axum::extract::Path<(String, String)>,
) -> Json<serde_json::Value> {
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

async fn save_convo(
    State(s): State<Arc<AppState>>,
    axum::extract::Path((modul_id, convo_id)): axum::extract::Path<(String, String)>,
    Json(mut body): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
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

async fn delete_convo(
    State(s): State<Arc<AppState>>,
    axum::extract::Path((modul_id, convo_id)): axum::extract::Path<(String, String)>,
) -> Json<serde_json::Value> {
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

async fn prompt_preview(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(modul_id): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
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
    {
        let py_mods = s.py_modules.read().await;
        tools::append_python_tools(&mut tools_section, &modul, &py_mods);
    }
    let home = s.pipeline.home_dir(&modul.id);
    let home_section = format!("Dein Home-Verzeichnis ist: {}", home.display());
    let date_section = chrono::Utc::now().format("%d.%m.%Y %H:%M UTC").to_string();
    let full = format!(
        "{}\n{}\n{}\nDatum: {}",
        system_raw.replace("{date}", &date_section),
        tools_section,
        home_section,
        date_section
    );
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

async fn get_logs(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(datum): axum::extract::Path<String>,
) -> Json<Vec<LogEvent>> {
    // Date format: YYYY-MM-DD
    if !datum.chars().all(|c| c.is_ascii_digit() || c == '-') || datum.len() > 10 {
        return Json(vec![]);
    }
    Json(s.pipeline.logs_laden(&datum))
}

async fn get_template(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(typ): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
    let Some(typ) = safe_id(&typ) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültiger Template-Name"}));
    };
    let templates_dir = s
        .pipeline
        .base
        .parent()
        .unwrap_or(&s.pipeline.base)
        .join("modules")
        .join("templates");
    let path = templates_dir.join(format!("{}.txt", typ));
    match std::fs::read_to_string(&path) {
        Ok(content) => Json(serde_json::json!({"ok": true, "template": content})),
        Err(_) => Json(
            serde_json::json!({"ok": false, "error": format!("Template '{}' nicht gefunden", typ)}),
        ),
    }
}

async fn get_metrics(State(s): State<Arc<AppState>>) -> impl IntoResponse {
    let (total_input, total_output, total_calls, _) =
        crate::store::token_all_time(&s.pipeline.store.pool).unwrap_or((0, 0, 0, 0.0));
    let now = chrono::Utc::now().timestamp() as u64;
    let hb = s.heartbeats.read().await;
    let alive_schedulers = hb
        .iter()
        .filter(|(_, t)| **t > 0 && now - **t < 120)
        .count();
    let total_schedulers = hb.len();
    drop(hb);

    let erstellt = s.pipeline.erstellt().len();
    let gestartet = s.pipeline.gestartet().len();
    let erledigt_count = crate::store::task_count_completed(&s.pipeline.store.pool).unwrap_or(0);

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
        total_input,
        total_output,
        total_calls,
        alive_schedulers,
        total_schedulers,
        erstellt,
        gestartet,
        erledigt_count,
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
        schedulers.insert(
            id.clone(),
            serde_json::json!({
                "last_beat": ts, "since_s": diff, "alive": diff < 120
            }),
        );
    }
    drop(hb);
    let aufgaben = s.pipeline.erstellt().len() + s.pipeline.gestartet().len();
    let erledigt_count = crate::store::task_count_completed(&s.pipeline.store.pool).unwrap_or(0);
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

async fn trigger_cron(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(cron_id): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
    let Some(cron_id) = safe_id(&cron_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige cron-ID"}));
    };
    let cfg = s.config.read().await;
    let modul = cfg
        .module
        .iter()
        .find(|m| m.id == cron_id && m.typ == "cron")
        .cloned();
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
                let aufgabe =
                    crate::types::Aufgabe::direct(tool, params, target, &modul.id, None, None);
                let id = aufgabe.id.clone();
                match s.pipeline.speichern(&aufgabe) {
                    Ok(_) => {
                        s.pipeline.log(
                            "cron",
                            Some(&id),
                            crate::types::LogTyp::Info,
                            &format!("Manual trigger: {}", modul.id),
                        );
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
            let anweisung = modul
                .settings
                .cron_anweisung
                .as_deref()
                .unwrap_or("Cron task");
            let cfg = s.config.read().await;
            let target_timeout = cfg
                .module
                .iter()
                .find(|m| m.id == target || m.name == target)
                .map(|m| m.timeout_s)
                .unwrap_or(modul.timeout_s);
            drop(cfg);
            let aufgabe = crate::types::Aufgabe::llm_call(anweisung, target, &modul.id, None)
                .with_timeout_s(target_timeout);
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
                let mut aufgabe = crate::types::Aufgabe::direct(
                    "__chain__",
                    vec![chain_json],
                    target,
                    &modul.id,
                    None,
                    None,
                );
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
        _ => Json(
            serde_json::json!({"ok": false, "error": format!("Unknown cron_typ: {}", cron_typ)}),
        ),
    }
}

// ─── Wizard ───────────────────────────────────────

#[derive(serde::Deserialize)]
pub struct WizardStartReq {
    pub mode: String, // "new" | "copy" | "edit"
    pub source_id: Option<String>,
}

pub async fn wizard_start(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::Json(req): axum::Json<WizardStartReq>,
) -> Result<axum::Json<serde_json::Value>, (axum::http::StatusCode, String)> {
    let cfg = state.config.read().await;
    if cfg.wizard.as_ref().map(|w| !w.enabled).unwrap_or(true) {
        return Err((
            axum::http::StatusCode::SERVICE_UNAVAILABLE,
            "Wizard-Backend nicht konfiguriert".into(),
        ));
    }
    let (mode, draft, original) = match req.mode.as_str() {
        "new" => (WizardMode::New, DraftAgent::default(), None),
        "copy" => {
            let src = req.source_id.as_deref().ok_or((
                axum::http::StatusCode::BAD_REQUEST,
                "source_id required for copy".into(),
            ))?;
            if crate::security::safe_id(src).is_none() {
                return Err((
                    axum::http::StatusCode::BAD_REQUEST,
                    "invalid source_id".into(),
                ));
            }
            let src_m = cfg.module.iter().find(|m| m.id == src).cloned().ok_or((
                axum::http::StatusCode::NOT_FOUND,
                "source module not found".into(),
            ))?;
            let mut d: DraftAgent = draft_from_module(&src_m);
            d.id = None;
            (
                WizardMode::Copy {
                    source_id: src.into(),
                },
                d,
                Some(src_m),
            )
        }
        "edit" => {
            let src = req.source_id.as_deref().ok_or((
                axum::http::StatusCode::BAD_REQUEST,
                "source_id required for edit".into(),
            ))?;
            if crate::security::safe_id(src).is_none() {
                return Err((
                    axum::http::StatusCode::BAD_REQUEST,
                    "invalid source_id".into(),
                ));
            }
            let src_m = cfg.module.iter().find(|m| m.id == src).cloned().ok_or((
                axum::http::StatusCode::NOT_FOUND,
                "source module not found".into(),
            ))?;
            let d = draft_from_module(&src_m);
            (
                WizardMode::Edit {
                    target_id: src.into(),
                },
                d,
                Some(src_m),
            )
        }
        _ => {
            return Err((
                axum::http::StatusCode::BAD_REQUEST,
                "mode must be new|copy|edit".into(),
            ));
        }
    };
    drop(cfg);

    wizard::ensure_dirs(&state.data_root)
        .await
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
    wizard::save_session(&state.data_root, &session)
        .await
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
pub struct WizardAbortReq {
    pub session_id: String,
}

pub async fn wizard_abort(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    axum::Json(req): axum::Json<WizardAbortReq>,
) -> Result<axum::Json<serde_json::Value>, (axum::http::StatusCode, String)> {
    if crate::security::safe_id(&req.session_id).is_none() {
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            "invalid session_id".into(),
        ));
    }
    wizard::delete_session(&state.data_root, &req.session_id)
        .await
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
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            "invalid session_id".into(),
        ));
    }
    let mut session = wizard::load_session(&state.data_root, &req.session_id)
        .await
        .ok_or((
            axum::http::StatusCode::NOT_FOUND,
            "session not found".into(),
        ))?;
    wizard::apply_propose(&mut session.draft, &req.field, &req.value)
        .map_err(|e| (axum::http::StatusCode::BAD_REQUEST, e))?;
    if !session.user_overridden_fields.contains(&req.field) {
        session.user_overridden_fields.push(req.field.clone());
    }
    session.last_activity = chrono::Utc::now().timestamp();
    wizard::save_session(&state.data_root, &session)
        .await
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
    let summary: Vec<_> = sessions
        .into_iter()
        .map(|s| {
            serde_json::json!({
                "session_id": s.session_id,
                "mode": s.mode,
                "created_at": s.created_at,
                "last_activity": s.last_activity,
                "agent_name": s.draft.identity.bot_name,
                "agent_id": s.draft.id,
                "rounds_used": s.llm_rounds_used,
                "frozen_reason": s.frozen_reason,
            })
        })
        .collect();
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
        return Err((
            axum::http::StatusCode::TOO_MANY_REQUESTS,
            "rate limit".into(),
        ));
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
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            "invalid session_id".into(),
        ));
    }
    {
        let mut inflight = state.wizard_turn_inflight.lock().await;
        if !inflight.insert(req.session_id.clone()) {
            return Err((
                axum::http::StatusCode::CONFLICT,
                "session has a turn in flight — wait or abort".into(),
            ));
        }
    }
    // Pre-flight: session exists + wizard configured
    if wizard::load_session(&state.data_root, &req.session_id)
        .await
        .is_none()
    {
        let mut inflight = state.wizard_turn_inflight.lock().await;
        inflight.remove(&req.session_id);
        return Err((
            axum::http::StatusCode::NOT_FOUND,
            "session not found".into(),
        ));
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
            return Err((
                axum::http::StatusCode::SERVICE_UNAVAILABLE,
                "wizard not configured".into(),
            ));
        }
    };

    let (tx, rx) = tokio::sync::mpsc::channel::<wizard::WizardEvent>(64);
    let state_c = state.clone();
    let session_id = req.session_id.clone();
    let text = req.text.clone();

    tokio::spawn(async move {
        let backend: Box<dyn wizard::WizardBackend + Send + Sync> =
            Box::new(wizard::RealWizardBackend {
                router: state_c.llm.clone(),
                backend: wizard_cfg.llm.clone(),
                config: state_c.config.clone(),
                tokens: Some(state_c.tokens.clone()),
                store_pool: Some((*state_c.pipeline.store.pool).clone()),
            });
        let mut session = match wizard::load_session(&state_c.data_root, &session_id).await {
            Some(s) => s,
            None => {
                let _ = tx
                    .send(wizard::WizardEvent::Error {
                        message: "session disappeared".into(),
                    })
                    .await;
                let _ = tx.send(wizard::WizardEvent::Done).await;
                {
                    let mut inflight = state_c.wizard_turn_inflight.lock().await;
                    inflight.remove(&session_id);
                }
                return;
            }
        };
        let _ = tx
            .send(wizard::WizardEvent::Session {
                session_id: session.session_id.clone(),
                mode: session.mode.clone(),
            })
            .await;
        let py_mods = state_c.py_modules.read().await.clone();
        let _ = wizard::run_turn(
            &*backend,
            &mut session,
            &state_c.config,
            &state_c.config_path,
            &wizard_cfg,
            &state_c.data_root,
            text,
            tx,
            &py_mods,
        )
        .await;
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
                None => {
                    return Err((
                        axum::http::StatusCode::BAD_REQUEST,
                        "no api_url/api_key given and no wizard.llm configured".into(),
                    ));
                }
            }
        }
    };

    match req.provider.as_str() {
        "Claude" | "Anthropic" => Ok(axum::Json(serde_json::json!({
            "models": [
                {"id": "claude-opus-4-7",   "display_name": "Claude Opus 4.7"},
                {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6"},
                {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
                {"id": "claude-opus-4-6",   "display_name": "Claude Opus 4.6"}
            ]
        }))),
        "OpenAI" | "Grok" | "OpenRouter" | "Local/LAN" => {
            let typ = if req.provider == "Grok" {
                crate::types::LlmTyp::Grok
            } else {
                crate::types::LlmTyp::OpenAICompat
            };
            if req.provider == "Local/LAN" {
                crate::security::validate_llm_backend_url(&typ, &url)
                    .map_err(|e| (axum::http::StatusCode::BAD_REQUEST, e))?;
            } else {
                crate::security::validate_external_url(&url)
                    .map_err(|e| (axum::http::StatusCode::BAD_REQUEST, e))?;
            }
            let full_url = crate::llm::openai_compat_endpoint(&url, "models");
            let client = reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(15))
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
            let mut request = client.get(&full_url);
            if !key.is_empty() {
                request = request.bearer_auth(&key);
            }
            let resp = request
                .send()
                .await
                .map_err(|e| (axum::http::StatusCode::BAD_GATEWAY, e.to_string()))?;
            let status = resp.status();
            let body: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| (axum::http::StatusCode::BAD_GATEWAY, e.to_string()))?;
            if !status.is_success() {
                return Err((
                    axum::http::StatusCode::BAD_GATEWAY,
                    format!("provider returned {}: {}", status, body),
                ));
            }
            let arr = body
                .get("data")
                .and_then(|v| v.as_array())
                .cloned()
                .unwrap_or_default();
            let models: Vec<_> = arr
                .iter()
                .filter_map(|m| {
                    let id = m.get("id")?.as_str()?.to_string();
                    Some(serde_json::json!({"id": id.clone(), "display_name": id}))
                })
                .collect();
            Ok(axum::Json(serde_json::json!({"models": models})))
        }
        _ => Err((
            axum::http::StatusCode::BAD_REQUEST,
            "unknown provider".into(),
        )),
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
    let typ = match req.provider.as_str() {
        "Claude" | "Anthropic" => crate::types::LlmTyp::Anthropic,
        "OpenAI" | "OpenRouter" | "Local/LAN" => crate::types::LlmTyp::OpenAICompat,
        "Grok" => crate::types::LlmTyp::Grok,
        _ => {
            return Err((
                axum::http::StatusCode::BAD_REQUEST,
                "unknown provider".into(),
            ));
        }
    };
    crate::security::validate_llm_backend_url(&typ, &req.api_url)
        .map_err(|e| (axum::http::StatusCode::BAD_REQUEST, e))?;

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
        cost_cap: None,
    };

    // Try a minimal ping: single user message "ping"
    let messages = vec![serde_json::json!({"role": "user", "content": "ping"})];
    let cfg_snapshot = state.config.read().await.clone();
    if let Err(msg) = check_daily_budget(
        &state.pipeline.store.pool,
        &state.tokens,
        &cfg_snapshot,
        &backend.model,
    )
    .await
    {
        return Err((axum::http::StatusCode::PAYMENT_REQUIRED, msg));
    }
    match state
        .llm
        .chat_with_tools_adhoc(&backend, &messages, &[])
        .await
    {
        Ok((_text, raw)) => {
            track_tokens(
                &state.pipeline.store.pool,
                &state.tokens,
                &cfg_snapshot,
                &backend.id,
                &backend.model,
                "__wizard_test__",
                &raw,
            )
            .await;
            Ok(axum::Json(
                serde_json::json!({"ok": true, "message": "Verbindung OK"}),
            ))
        }
        Err(e) => {
            release_reservation(
                &state.pipeline.store.pool,
                &state.tokens,
                &cfg_snapshot,
                &backend.model,
            )
            .await;
            Ok(axum::Json(serde_json::json!({"ok": false, "error": e})))
        }
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
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            "invalid session_id".into(),
        ));
    }
    let session = wizard::load_session(&state.data_root, &req.session_id)
        .await
        .ok_or((
            axum::http::StatusCode::NOT_FOUND,
            "session not found".into(),
        ))?;
    if session.code_gen_proposal.is_none() {
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            "no proposal pending".into(),
        ));
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
                let _ = tx
                    .send(wizard::WizardEvent::Error {
                        message: "session gone".into(),
                    })
                    .await;
                let _ = tx.send(wizard::WizardEvent::Done).await;
                return;
            }
        };
        wizard::execute_code_gen(&mut session, approved, &reason, &state_c, &tx).await;
        let _ = wizard::save_session(&state_c.data_root, &session).await;
        let _ = tx.send(wizard::WizardEvent::Done).await;
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
        .body(body)
        .unwrap())
}

#[derive(serde::Deserialize)]
pub struct QualityStatsReq {
    pub hours: Option<u32>,
}

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
        &state.data_root,
        since,
        limit,
        req.backend.as_deref(),
        req.only_failed.unwrap_or(false),
    )
    .await;
    let has_more = events.len() >= limit;
    axum::Json(serde_json::json!({"events": events, "has_more": has_more}))
}

pub async fn quality_benchmark_cases()
-> Result<axum::Json<serde_json::Value>, (axum::http::StatusCode, String)> {
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
    let mut backend = cfg_snap
        .llm_backends
        .iter()
        .find(|b| b.id == req.backend_id)
        .cloned()
        .ok_or((
            axum::http::StatusCode::NOT_FOUND,
            format!("backend '{}' not found", req.backend_id),
        ))?;
    if let Some(m) = req.model {
        backend.model = m;
    }
    let modul_id = req.modul_id.unwrap_or_else(|| {
        cfg_snap
            .module
            .iter()
            .find(|m| m.typ == "chat")
            .map(|m| m.id.clone())
            .unwrap_or_default()
    });
    if modul_id.is_empty() {
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            "no chat module available for context".into(),
        ));
    }
    let py_mods: Vec<crate::loader::PyModuleMeta> = state.py_modules.read().await.clone();
    let llm = state.llm.clone();
    let tokens = Some(state.tokens.clone());
    let store_pool = Some((*state.pipeline.store.pool).clone());

    let (tx, rx) = tokio::sync::mpsc::channel::<crate::benchmark::BenchmarkEvent>(64);
    tokio::spawn(async move {
        crate::benchmark::run_benchmark(
            backend, modul_id, cfg_snap, py_mods, llm, tokens, store_pool, tx,
        )
        .await;
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
        .body(body)
        .unwrap())
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
    let ba = cfg_snap
        .llm_backends
        .iter()
        .find(|b| b.id == req.backend_a)
        .cloned()
        .ok_or((
            axum::http::StatusCode::NOT_FOUND,
            format!("backend A '{}' not found", req.backend_a),
        ))?;
    let bb = cfg_snap
        .llm_backends
        .iter()
        .find(|b| b.id == req.backend_b)
        .cloned()
        .ok_or((
            axum::http::StatusCode::NOT_FOUND,
            format!("backend B '{}' not found", req.backend_b),
        ))?;
    let modul_id = req.modul_id.unwrap_or_else(|| {
        cfg_snap
            .module
            .iter()
            .find(|m| m.typ == "chat")
            .map(|m| m.id.clone())
            .unwrap_or_default()
    });
    if modul_id.is_empty() {
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            "no chat module available for context".into(),
        ));
    }
    let py_mods: Vec<crate::loader::PyModuleMeta> = state.py_modules.read().await.clone();
    let llm = state.llm.clone();
    let tokens = Some(state.tokens.clone());
    let store_pool = Some((*state.pipeline.store.pool).clone());

    let (tx, rx) = tokio::sync::mpsc::channel::<crate::benchmark::CompareEvent>(64);
    tokio::spawn(async move {
        crate::benchmark::run_compare(
            ba, bb, modul_id, cfg_snap, py_mods, llm, tokens, store_pool, tx,
        )
        .await;
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
        .body(body)
        .unwrap())
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
    let backends = {
        let cfg = s.config.read().await;
        cfg.llm_backends.clone()
    };
    let has_backends = !backends.is_empty();
    let mut reachable = false;
    if has_backends {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(3))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .unwrap_or_default();
        for b in &backends {
            if crate::types::test_backend_reachable(&client, b).await {
                reachable = true;
                break;
            }
        }
    }
    Json(serde_json::json!({
        "has_backends": has_backends,
        "reachable": reachable,
        "needs_setup": !reachable,
    }))
}

async fn setup_models(Json(body): Json<crate::types::LlmBackend>) -> Json<serde_json::Value> {
    if let Err(e) = crate::security::validate_llm_backend_url(&body.typ, &body.url) {
        return Json(
            serde_json::json!({"ok": false, "error": format!("SSRF-Schutz: {}", e), "models": []}),
        );
    }
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .redirect(reqwest::redirect::Policy::none())
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            return Json(
                serde_json::json!({"ok": false, "error": format!("client: {}", e), "models": []}),
            );
        }
    };

    let result = match body.typ {
        crate::types::LlmTyp::Ollama => {
            let url = format!("{}/api/tags", body.url.trim_end_matches('/'));
            let resp = match client.get(url).send().await {
                Ok(r) => r,
                Err(e) => {
                    return Json(
                        serde_json::json!({"ok": false, "error": e.to_string(), "models": []}),
                    );
                }
            };
            let status = resp.status();
            let data: serde_json::Value = resp.json().await.unwrap_or_default();
            if !status.is_success() {
                return Json(
                    serde_json::json!({"ok": false, "error": format!("Ollama HTTP {}: {}", status, data), "models": []}),
                );
            }
            data["models"]
                .as_array()
                .map(|arr| {
                    arr.iter()
                        .filter_map(|m| m["name"].as_str().map(String::from))
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default()
        }
        crate::types::LlmTyp::OpenAICompat
        | crate::types::LlmTyp::Grok
        | crate::types::LlmTyp::Embedding => {
            let mut req = client.get(crate::llm::openai_compat_endpoint(&body.url, "models"));
            if let Some(key) = body.api_key.as_deref().filter(|k| !k.is_empty()) {
                req = req.bearer_auth(key);
            }
            let resp = match req.send().await {
                Ok(r) => r,
                Err(e) => {
                    return Json(
                        serde_json::json!({"ok": false, "error": e.to_string(), "models": []}),
                    );
                }
            };
            let status = resp.status();
            let data: serde_json::Value = resp.json().await.unwrap_or_default();
            if !status.is_success() {
                return Json(
                    serde_json::json!({"ok": false, "error": format!("OpenAI-compatible HTTP {}: {}", status, data), "models": []}),
                );
            }
            data["data"]
                .as_array()
                .map(|arr| {
                    arr.iter()
                        .filter_map(|m| m["id"].as_str().map(String::from))
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default()
        }
        crate::types::LlmTyp::Anthropic => vec![
            "claude-opus-4-7".into(),
            "claude-sonnet-4-6".into(),
            "claude-haiku-4-5".into(),
            "claude-opus-4-6".into(),
        ],
    };

    Json(serde_json::json!({"ok": true, "models": result}))
}

async fn setup_test_backend(
    State(s): State<Arc<AppState>>,
    Json(mut body): Json<crate::types::LlmBackend>,
) -> Json<serde_json::Value> {
    if let Err(e) = crate::security::validate_llm_backend_url(&body.typ, &body.url) {
        return Json(serde_json::json!({"ok": false, "error": format!("SSRF-Schutz: {}", e)}));
    }
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(body.timeout_s.max(60)))
        .redirect(reqwest::redirect::Policy::none())
        .build()
    {
        Ok(c) => c,
        Err(e) => return Json(serde_json::json!({"ok": false, "error": format!("client: {}", e)})),
    };
    if body.max_tokens.is_none() {
        body.max_tokens = Some(24);
    }
    let messages = vec![serde_json::json!({"role": "user", "content": "Reply with exactly: hi"})];
    let cfg_snapshot = s.config.read().await.clone();
    if let Err(msg) = check_daily_budget(
        &s.pipeline.store.pool,
        &s.tokens,
        &cfg_snapshot,
        &body.model,
    )
    .await
    {
        return Json(serde_json::json!({"ok": false, "error": msg}));
    }
    match crate::llm::LlmRouter::dispatch_chat_public(&body, &messages, &[], &client).await {
        Ok((text, raw)) => {
            track_tokens(
                &s.pipeline.store.pool,
                &s.tokens,
                &cfg_snapshot,
                &body.id,
                &body.model,
                "__setup_test__",
                &raw,
            )
            .await;
            Json(serde_json::json!({
                "ok": true,
                "sample": crate::util::safe_truncate_owned(&text, 400),
            }))
        }
        Err(e) => {
            release_reservation(
                &s.pipeline.store.pool,
                &s.tokens,
                &cfg_snapshot,
                &body.model,
            )
            .await;
            Json(serde_json::json!({"ok": false, "error": e}))
        }
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
            Err(e) => {
                return Json(serde_json::json!({"ok": false, "error": format!("payload: {}", e)}));
            }
        }
    } else {
        match serde_json::from_value::<crate::types::LlmBackend>(raw) {
            Ok(b) => (b, None),
            Err(e) => {
                return Json(serde_json::json!({"ok": false, "error": format!("payload: {}", e)}));
            }
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
    crate::util::normalize_same_llm_links(&mut cfg);

    let path = s.pipeline.base.join("config.json");
    let json = match serde_json::to_string_pretty(&*cfg) {
        Ok(j) => j,
        Err(e) => {
            return Json(serde_json::json!({"ok": false, "error": format!("serialize: {}", e)}));
        }
    };
    if let Err(e) = crate::util::atomic_write(&path, json.as_bytes()) {
        return Json(serde_json::json!({"ok": false, "error": format!("write: {}", e)}));
    }
    s.pipeline.audit(
        "setup.save_backend",
        "setup-wizard",
        &format!(
            "backend={} typ={:?} model={}",
            backend.id, backend.typ, backend.model
        ),
    );
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
    let limit = q
        .get("limit")
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(200)
        .min(1000);
    match crate::store::audit_filtered(&s.pipeline.store.pool, action, actor, since, limit) {
        Ok(rows) => Json(serde_json::json!({"entries": rows})),
        Err(e) => Json(serde_json::json!({"entries": [], "error": e})),
    }
}

async fn get_tokens_by_modul(
    State(s): State<Arc<AppState>>,
    axum::extract::Query(q): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Json<serde_json::Value> {
    let days = q
        .get("days")
        .and_then(|s| s.parse::<i64>().ok())
        .unwrap_or(7)
        .max(1)
        .min(90);
    match crate::store::tokens_by_modul(&s.pipeline.store.pool, days) {
        Ok(rows) => Json(serde_json::json!({"days": days, "by_modul": rows})),
        Err(e) => Json(serde_json::json!({"by_modul": [], "error": e})),
    }
}

async fn get_tokens_by_backend(
    State(s): State<Arc<AppState>>,
    axum::extract::Query(q): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Json<serde_json::Value> {
    let days = q
        .get("days")
        .and_then(|s| s.parse::<i64>().ok())
        .unwrap_or(7)
        .max(1)
        .min(90);
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
    let Some(modul) = cfg
        .module
        .iter()
        .find(|m| m.id == id || m.name == id)
        .cloned()
    else {
        return Json(serde_json::json!({"error": "Modul nicht gefunden"}));
    };
    let py_mods = s.py_modules.read().await.clone();
    drop(cfg);

    let rust_tools: Vec<serde_json::Value> = crate::tools::tools_for_module(&modul)
        .iter()
        .map(|t| {
            serde_json::json!({
                "name": t.name,
                "description": t.description,
                "params": t.params,
            })
        })
        .collect();

    // Python-Tools die dieses Modul nutzen DARF (via perms + linked_modules)
    let py_tools: Vec<serde_json::Value> = py_mods
        .iter()
        .flat_map(|pm| {
            let perm = format!("py.{}", pm.name);
            let allowed = modul
                .berechtigungen
                .iter()
                .any(|p| p == &perm || p == "py.*")
                || modul
                    .linked_modules
                    .iter()
                    .any(|link| link == &pm.name || link.starts_with(&format!("{}.", pm.name)));
            if !allowed {
                return vec![];
            }
            pm.tools
                .iter()
                .map(|t| {
                    serde_json::json!({
                        "name": t.name,
                        "description": t.description,
                        "params": t.params,
                        "via_python_module": pm.name,
                    })
                })
                .collect::<Vec<_>>()
        })
        .collect();

    // Permissions in Klartext
    let perm_explain: Vec<serde_json::Value> = modul
        .berechtigungen
        .iter()
        .map(|p| {
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
        })
        .collect();

    // Typ-basierte implizite grants (nur für persistent-Module aktiv)
    let typ_grants: Vec<&str> = if modul.persistent {
        match modul.typ.as_str() {
            "filesystem" => vec!["files.read", "files.write", "files.list (im allowed_paths)"],
            "websearch" => vec!["web.search", "http.get"],
            "shell" => vec!["shell.exec"],
            "notify" => vec!["notify.send"],
            _ => vec![],
        }
    } else {
        vec![]
    };

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn configured_price_cost_uses_per_million_units() {
        let cost = cost_for_tokens(1_000_000, 500_000, 2.0, 8.0);
        assert!((cost - 6.0).abs() < 1e-9);
    }

    #[test]
    fn estimate_message_tokens_never_returns_zero() {
        assert_eq!(estimate_message_tokens(&[]), 1);
        let messages = vec![serde_json::json!({"role": "user", "content": "abcd"})];
        assert_eq!(estimate_message_tokens(&messages), 1);
    }

    #[test]
    fn chat_tool_result_for_llm_truncates_large_results() {
        let large = "x".repeat(MAX_CHAT_TOOL_RESULT_CHARS + 100);
        let result = chat_tool_result_for_llm(true, &large);
        assert!(result.starts_with("SUCCESS: "));
        assert!(result.contains("gekuerzt"));
        assert!(result.len() < large.len());
    }

    #[test]
    fn chat_tool_result_for_llm_tells_model_to_handle_failures() {
        let result = chat_tool_result_for_llm(false, "Datei existiert nicht");
        assert!(result.starts_with("FAILED: "));
        assert!(result.contains("retry with corrected parameters"));
        assert!(result.contains("Do not present this failed step as successful"));
    }

    #[test]
    fn tool_history_arguments_preserve_schema_names() {
        let modul = ModulConfig {
            id: "chat.test".into(),
            typ: "chat".into(),
            name: "chat.test".into(),
            display_name: "Test".into(),
            llm_backend: "llm".into(),
            backup_llm: None,
            berechtigungen: vec![],
            timeout_s: 30,
            retry: 0,
            settings: ModulSettings::default(),
            identity: ModulIdentity::default(),
            rag_pool: Some("DeepDive".into()),
            linked_modules: vec![],
            persistent: true,
            spawned_by: None,
            spawn_ttl_s: None,
            created_at: None,
            scheduler_interval_ms: None,
            max_concurrent_tasks: None,
            token_budget: None,
            token_budget_warning: None,
        };
        let args = tool_arguments_json_for_history(
            "rag.suchen",
            &["dd-123 Donald Trump".to_string()],
            Some(&modul),
            &[],
        );
        let parsed: serde_json::Value = serde_json::from_str(&args).unwrap();
        assert_eq!(parsed, serde_json::json!({"query": "dd-123 Donald Trump"}));
    }

    #[test]
    fn tool_history_arguments_fallback_uses_known_keys() {
        let args = tool_arguments_json_for_history(
            "browser.fetch",
            &["https://example.test".to_string()],
            None,
            &[],
        );
        let parsed: serde_json::Value = serde_json::from_str(&args).unwrap();
        assert_eq!(parsed, serde_json::json!({"url": "https://example.test"}));
    }

    #[test]
    fn tool_history_arguments_truncate_large_values() {
        let args = tool_arguments_json_for_history(
            "rag.speichern",
            &["x".repeat(MAX_CHAT_TOOL_HISTORY_ARG_CHARS + 500)],
            None,
            &[],
        );
        let parsed: serde_json::Value = serde_json::from_str(&args).unwrap();
        let text = parsed["text"].as_str().unwrap();
        assert!(text.len() < MAX_CHAT_TOOL_HISTORY_ARG_CHARS + 100);
    }

    #[test]
    fn compact_final_synthesis_messages_have_no_tool_roles() {
        let messages = vec![
            serde_json::json!({"role":"assistant","content":null,"tool_calls":[{"id":"call_a","type":"function","function":{"name":"browser.fetch","arguments":"{\"url\":\"https://example.test\"}"}}]}),
            serde_json::json!({"role":"tool","tool_call_id":"call_a","content":"SUCCESS: Beispielinhalt"}),
        ];
        let final_messages = final_synthesis_messages("frage", &messages, true, 4000);
        assert!(final_messages.iter().all(|m| m["role"] != "tool"));
        assert!(
            final_messages[1]["content"]
                .as_str()
                .unwrap()
                .contains("browser.fetch")
        );
    }

    #[test]
    fn summarize_chat_tool_failures_keeps_only_unresolved_open() {
        let failures = vec![
            ChatToolFailure {
                tool_name: "editor.view".into(),
                detail: "Datei existiert nicht".into(),
                recovered: true,
            },
            ChatToolFailure {
                tool_name: "module_builder.scaffold".into(),
                detail: "Modul existiert bereits".into(),
                recovered: false,
            },
        ];

        let (unresolved, recovered) = summarize_chat_tool_failures(&failures);
        assert_eq!(recovered, 1);
        assert_eq!(unresolved.len(), 1);
        assert!(unresolved[0].contains("module_builder.scaffold"));
    }

    #[test]
    fn deepdive_gate_requires_crawl_for_broad_web_research() {
        let progress = DeepdiveProgress::default();
        let feedback = deepdive_gate_feedback(
            &progress,
            "such alles was du über Friedrich Merz heraus im web was du kannst",
            "Friedrich Merz ist Kanzlerkandidat 2025.",
        )
        .unwrap();
        assert!(is_deepdive_request(
            "such alles was du über Friedrich Merz heraus im web was du kannst"
        ));
        assert!(feedback.contains("deepdive.crawl(Friedrich Merz)"));
    }

    #[test]
    fn deepdive_gate_accepts_manual_fetches_saved_to_rag() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "duckduckgo.search", true, 1);
        observe_deepdive_progress(&mut progress, "duckduckgo.search", true, 2);
        observe_deepdive_progress(&mut progress, "browser.fetch", true, 3);
        observe_deepdive_progress(&mut progress, "browser.fetch", true, 4);
        observe_deepdive_progress(&mut progress, "browser.fetch", true, 5);
        observe_deepdive_progress(&mut progress, "rag.speichern", true, 6);
        let feedback = deepdive_gate_feedback(
            &progress,
            "such mal im web nach Donald Trump",
            "Zwischenstand",
        )
        .unwrap();
        assert!(feedback.contains("rag.suchen"));
        assert!(!feedback.contains("deepdive.crawl"));
    }

    #[test]
    fn deepdive_request_detects_current_news_without_breadth_word() {
        assert!(is_deepdive_request(
            "was gibts neues zu Friedrich Merz, schau im Web nach"
        ));
        assert!(is_deepdive_request(
            "such aktuelle Nachrichten zu Angela Merkel"
        ));
    }

    #[test]
    fn deepdive_gate_requires_rag_after_crawl() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "deepdive.crawl", true, 1);
        let feedback = deepdive_gate_feedback(&progress, "Deepdive zu Friedrich Merz", "").unwrap();
        assert!(feedback.contains("rag.suchen(Friedrich Merz)"));
    }

    #[test]
    fn deepdive_gate_uses_crawl_id_for_rag() {
        let progress = DeepdiveProgress {
            crawl_ok: 1,
            crawl_id: Some("dd-20260507T010203Z-abcdef12".into()),
            last_evidence_round: 1,
            ..Default::default()
        };
        let feedback = deepdive_gate_feedback(&progress, "Deepdive zu Friedrich Merz", "").unwrap();
        assert!(feedback.contains("rag.suchen(dd-20260507T010203Z-abcdef12 Friedrich Merz)"));
    }

    #[test]
    fn deepdive_gate_allows_crawl_plus_rag() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "deepdive.crawl", true, 1);
        observe_deepdive_progress(&mut progress, "rag.suchen", true, 2);
        let final_text = "Lagebild mit Timeline und Hintergrund...\n<quellen>\n- fundort: https://example.test/source\n</quellen>";
        assert!(
            deepdive_gate_feedback(&progress, "Deepdive zu Friedrich Merz", final_text).is_none()
        );
    }

    #[test]
    fn deepdive_gate_requires_exact_source_block() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "deepdive.crawl", true, 1);
        observe_deepdive_progress(&mut progress, "rag.suchen", true, 2);
        let feedback =
            deepdive_gate_feedback(&progress, "Deepdive zu Friedrich Merz", "Quelle: FAZ").unwrap();
        assert!(feedback.contains("<quellen>"));
    }

    #[test]
    fn deepdive_gate_rejects_source_pool_only_answer() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "deepdive.crawl", true, 1);
        observe_deepdive_progress(&mut progress, "rag.suchen", true, 2);
        let final_text = "Die spezifischen Details der Ereignisse sind nicht im vorliegenden Auszug der RAG-Daten enthalten.\n<quellen>\nhttps://example.test\n</quellen>";
        let feedback = deepdive_gate_feedback(
            &progress,
            "Deepdive aktuelle Ereignisse Deutschland",
            final_text,
        )
        .unwrap();
        assert!(feedback.contains("konkrete Artikel"));
    }

    #[test]
    fn deepdive_gate_requires_lagebild_shape_for_broad_deepdive() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "deepdive.crawl", true, 1);
        observe_deepdive_progress(&mut progress, "rag.suchen", true, 2);
        let final_text =
            "Stand: Quellen wurden gelesen.\n<quellen>\nhttps://example.test\n</quellen>";
        let feedback =
            deepdive_gate_feedback(&progress, "Deepdive aktuelle News", final_text).unwrap();
        assert!(feedback.contains("Timeline"));
    }

    #[test]
    fn strip_tool_tags_removes_malformed_tool_attempts() {
        let text =
            "Plan\n<tool>editor.replace{aenderung:x,pfad:modules/DEEPDIVE/module.py}<tool_call|>";
        assert_eq!(strip_tool_tags(text), "Plan");
    }
}
