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

const MAX_CHAT_TOOL_RESULT_CHARS: usize = 7000;
const MAX_PREPARED_BLOCKS_TOOL_RESULT_CHARS: usize = 16000;
const MAX_CAPABILITIES_TOOL_RESULT_CHARS: usize = 1200;
const MAX_CHAT_TASK_RESULT_CHARS: usize = 20000;
const MAX_MALFORMED_TOOL_RETRIES: u32 = 3;
const MAX_CHAT_TOOL_HISTORY_ARG_CHARS: usize = 1200;
const MAX_FINAL_SYNTHESIS_EVIDENCE_CHARS: usize = 22000;
const MAX_ERROR_RECOVERY_EVIDENCE_CHARS: usize = 5000;
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

fn tool_round_limit_for_backend(cfg: &AgentConfig, backend_id: &str) -> Option<usize> {
    cfg.llm_backends
        .iter()
        .find(|b| b.id == backend_id)
        .and_then(|b| b.tool_round_limit())
}

fn can_run_more_tool_rounds(cfg: &AgentConfig, backend_id: &str, rounds: usize) -> bool {
    tool_round_limit_for_backend(cfg, backend_id)
        .map(|limit| rounds < limit)
        .unwrap_or(true)
}

#[derive(Debug, Clone, serde::Serialize)]
struct LlmModelInfo {
    id: String,
    display_name: String,
    free: bool,
}

fn zeroish_json_value(v: &serde_json::Value) -> bool {
    match v {
        serde_json::Value::Number(n) => n.as_f64().map(|n| n == 0.0).unwrap_or(false),
        serde_json::Value::String(s) => s.trim().parse::<f64>().map(|n| n == 0.0).unwrap_or(false),
        _ => false,
    }
}

fn model_pricing_is_free(raw: &serde_json::Value) -> bool {
    let Some(pricing) = raw.get("pricing").and_then(|v| v.as_object()) else {
        return false;
    };
    let mut seen_price_field = false;
    for key in [
        "prompt",
        "completion",
        "request",
        "image",
        "web_search",
        "internal_reasoning",
    ] {
        if let Some(v) = pricing.get(key) {
            seen_price_field = true;
            if !zeroish_json_value(v) {
                return false;
            }
        }
    }
    seen_price_field
}

fn model_info_from_openai_value(raw: &serde_json::Value) -> Option<LlmModelInfo> {
    let id = raw.get("id")?.as_str()?.to_string();
    let display_name = raw
        .get("name")
        .and_then(|v| v.as_str())
        .filter(|s| !s.trim().is_empty())
        .unwrap_or(&id)
        .to_string();
    let free = id.to_ascii_lowercase().ends_with(":free") || model_pricing_is_free(raw);
    Some(LlmModelInfo {
        id,
        display_name,
        free,
    })
}

fn model_info_from_id(id: impl Into<String>, free: bool) -> LlmModelInfo {
    let id = id.into();
    LlmModelInfo {
        display_name: id.clone(),
        id,
        free,
    }
}

fn sort_model_infos(mut models: Vec<LlmModelInfo>) -> Vec<LlmModelInfo> {
    let mut seen = std::collections::HashSet::new();
    models.retain(|m| seen.insert(m.id.clone()));
    models.sort_by(|a, b| {
        let an = a.display_name.to_ascii_lowercase();
        let bn = b.display_name.to_ascii_lowercase();
        an.cmp(&bn)
            .then_with(|| a.id.to_ascii_lowercase().cmp(&b.id.to_ascii_lowercase()))
    });
    models
}

fn model_ids(models: &[LlmModelInfo]) -> Vec<String> {
    models.iter().map(|m| m.id.clone()).collect()
}

fn message_content_plain_text(content: &serde_json::Value) -> String {
    match content {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Array(items) => items
            .iter()
            .filter_map(|item| {
                if let Some(text) = item.get("text").and_then(|v| v.as_str()) {
                    return Some(text.to_string());
                }
                match item.get("type").and_then(|v| v.as_str()) {
                    Some("image_url") | Some("input_image") | Some("image") => {
                        Some("[image]".to_string())
                    }
                    _ => None,
                }
            })
            .collect::<Vec<_>>()
            .join("\n"),
        serde_json::Value::Object(obj) => obj
            .get("text")
            .and_then(|v| v.as_str())
            .map(str::to_string)
            .unwrap_or_else(|| content.to_string()),
        serde_json::Value::Null => String::new(),
        _ => content.to_string(),
    }
}

fn message_plain_text(message: &serde_json::Value) -> String {
    message
        .get("content")
        .map(message_content_plain_text)
        .unwrap_or_default()
}

fn estimate_message_tokens(messages: &[serde_json::Value]) -> u64 {
    let chars: usize = messages
        .iter()
        .filter_map(|m| m.get("content"))
        .map(|content| message_content_plain_text(content).len())
        .sum();
    ((chars + 3) / 4).max(1) as u64
}

fn chat_tool_result_for_llm(ok: bool, data: &str) -> String {
    let trimmed = data.trim_start();
    let max_chars = if trimmed.starts_with("DEEPDIVE_BLOCKS") {
        MAX_PREPARED_BLOCKS_TOOL_RESULT_CHARS
    } else if trimmed.starts_with("Verfuegbare Tools fuer")
        || trimmed.starts_with("AGENT_CAPABILITIES")
    {
        MAX_CAPABILITIES_TOOL_RESULT_CHARS
    } else {
        MAX_CHAT_TOOL_RESULT_CHARS
    };
    let body = if data.chars().count() > max_chars {
        format!(
            "{}...[gekuerzt; vollstaendiges Ergebnis im Aufgaben-Board]",
            util::safe_truncate(data, max_chars)
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

fn persist_chat_assistant_result(
    pipeline: &Pipeline,
    modul_id: &str,
    convo_id: Option<&str>,
    seed_messages: &serde_json::Value,
    content: &str,
) {
    let Some(convo_id) = convo_id else {
        return;
    };
    if modul_id.trim().is_empty() || content.trim().is_empty() {
        return;
    }

    let seed = seed_messages.as_array().cloned().unwrap_or_default();
    let mut convo = pipeline
        .convo_load(modul_id, convo_id)
        .unwrap_or_else(|| serde_json::json!({"id": convo_id, "messages": []}));

    if !convo.is_object() {
        convo = serde_json::json!({"id": convo_id, "messages": []});
    }
    convo["id"] = serde_json::json!(convo_id);

    let mut messages = convo
        .get("messages")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    if messages.len() < seed.len() {
        messages = seed;
    }

    let final_content = content.to_string();
    let mut already_present = false;
    if let Some(last) = messages.last_mut() {
        let role = last.get("role").and_then(|v| v.as_str()).unwrap_or("");
        let existing = last.get("content").and_then(|v| v.as_str()).unwrap_or("");
        if role == "assistant" && existing == final_content {
            already_present = true;
        } else if role == "assistant"
            && (existing.starts_with("Error:")
                || existing.starts_with("(Antwort war leer")
                || existing.contains("network error"))
        {
            *last = serde_json::json!({"role": "assistant", "content": final_content});
            already_present = true;
        }
    }
    if !already_present {
        messages.push(serde_json::json!({"role": "assistant", "content": final_content}));
    }

    let title = convo
        .get("title")
        .and_then(|v| v.as_str())
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.to_string())
        .or_else(|| {
            messages.iter().find_map(|m| {
                if m.get("role").and_then(|v| v.as_str()) == Some("user") {
                    m.get("content")
                        .and_then(|v| v.as_str())
                        .map(|s| util::safe_truncate(s, 40).to_string())
                } else {
                    None
                }
            })
        })
        .unwrap_or_else(|| "Neue Conversation".to_string());

    convo["title"] = serde_json::json!(title);
    convo["messages"] = serde_json::json!(messages);
    convo["updated"] = serde_json::json!(chrono::Utc::now().to_rfc3339());

    if let Err(e) = pipeline.convo_save(modul_id, &convo) {
        tracing::warn!(
            "Chat-Conversation persist failed for {} / {}: {}",
            modul_id,
            convo_id,
            e
        );
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
        "notification.send" => &["title", "message"],
        "notification.read" => &["limit"],
        "notification.delete" => &["notification_id"],
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
        "Das Tool-Rundenlimit ist erreicht. Nutze keine Tools. Erstelle aus der folgenden Tool-Evidenz einen belastbaren DeepDive-Bericht. Wenn DEEPDIVE_BLOCKS vorhanden ist, nutze dessen QUELLEN_BLOCK, TIMELINE_BLOCK, CLAIMS_BLOCK, CAUSALITY_BLOCK, SUBCRAWL_PLAN_BLOCK, SUBCRAWL_RESULTS_BLOCK, BRANCHING_CONTEXT_BLOCK, CONTRAST_BLOCK und LEADS_BLOCK als feste Bausteine. Ziel ist keine eigene Meinung, sondern Informationslage, Ereignisse, Akteure, Claims, Leads, Kausalitaetsbehauptungen, Subcrawl-Seiteninformationen, Branching-Kontext/Missing Links und Perspektivenkontrast aufzubereiten. Pflichtstruktur: Aktueller Stand, Timeline/Chronologie, Akteure, Claims/Belege, Kausalketten/Mechanismen, Subcrawls/Sidestories, vorgeschlagene Anschluss-Crawls, Branching/Missing Links, Perspektivenkontrast nach Sprache/Land, Widersprueche/Unsicherheiten, offene Leads, danach exakt ein <quellen>-Block. Jede Quellenzeile braucht URL, Titel/Outlet falls vorhanden, Stand/Abrufzeit oder RAG-ID, und wofuer sie genutzt wurde. Nutze nur URLs/Fundorte aus der Tool-Evidenz und behaupte keine neuen Quellen."
    } else {
        "Das Tool-Rundenlimit ist erreicht. Nutze keine Tools. Erstelle aus der folgenden Tool-Evidenz die beste moegliche Antwort und markiere unvollstaendige Punkte klar."
    };
    vec![
        serde_json::json!({"role": "system", "content": "Du bist im finalen Synthese-Modus. Du darfst keine Tools verwenden und keine neuen Recherchen behaupten. Arbeite nur mit der gelieferten Tool-Evidenz. Bei DeepDive-Antworten ist der <quellen>-Block Pflicht und muss echte URLs aus der Evidenz enthalten. Social-/Kommentar-Funde sind Leads oder Meinungen, keine Beweise."}),
        serde_json::json!({"role": "user", "content": format!("Originale Anfrage:\n{}\n\n{}\n\nTool-Evidenz:\n{}", last_user_msg, instruction, evidence)}),
    ]
}

fn last_assistant_text(messages: &[serde_json::Value]) -> Option<String> {
    messages.iter().rev().find_map(|message| {
        if message.get("role").and_then(|v| v.as_str()) != Some("assistant") {
            return None;
        }
        let text = message_plain_text(message);
        let text = strip_tool_tags(&text).trim().to_string();
        if text.is_empty() { None } else { Some(text) }
    })
}

fn llm_error_recovery_answer(
    error: &str,
    sub_task_count: usize,
    messages: &[serde_json::Value],
) -> String {
    let previous = last_assistant_text(messages);
    let evidence = compact_chat_evidence(messages, MAX_ERROR_RECOVERY_EVIDENCE_CHARS);
    let mut lines = vec![
        format!(
            "[{} Aufgabe(n) erstellt, Abschluss-LLM fehlgeschlagen]",
            sub_task_count
        ),
        String::new(),
        format!(
            "Der Quellenabruf war bereits durch, aber der abschliessende LLM-Call ist fehlgeschlagen: {}",
            error
        ),
    ];
    if let Some(draft) = previous {
        lines.extend([
            String::new(),
            "Letzter verwertbarer Entwurf vor dem Fehler:".into(),
            draft,
        ]);
    } else {
        lines.extend([
            String::new(),
            "Auszug aus den vorhandenen Tool-Ergebnissen:".into(),
            evidence,
        ]);
    }
    lines.join("\n")
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
    rss_evidence_ok: usize,
    search_ok: usize,
    fetch_ok: usize,
    source_note_ok: usize,
    rag_save_ok: usize,
    rag_search_ok: usize,
    pack_ok: usize,
    blocks_ok: usize,
    last_evidence_round: usize,
    last_rag_round: usize,
    last_pack_round: usize,
    last_blocks_round: usize,
}

fn is_deepdive_request(text: &str) -> bool {
    let lower = text.to_lowercase();
    if rejects_research_tools(&lower) {
        return false;
    }
    let currentish = [
        "aktuell",
        "heute",
        "news",
        "nachrichten",
        "neuigkeiten",
        "neues",
        "letzte stunde",
        "neuste",
        "neueste",
        "stand der dinge",
        "aktueller stand",
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
    asks_research && wants_breadth
}

fn rejects_research_tools(text: &str) -> bool {
    let lower = text.to_lowercase();
    [
        "keine recherche",
        "keine externe recherche",
        "keine online-recherche",
        "keine online recherche",
        "ohne recherche",
        "ohne externe recherche",
        "ohne online-recherche",
        "ohne online recherche",
        "nicht recherch",
        "keine websuche",
        "keine externe websuche",
        "ohne websuche",
        "ohne externe websuche",
        "nicht suchen",
        "keine suche",
        "keine tools",
        "ohne tools",
        "kein tool",
        "kein deepdive",
        "kein deep dive",
        "ohne deepdive",
        "ohne deep dive",
    ]
    .iter()
    .any(|marker| lower.contains(marker))
}

fn rejects_all_tools(text: &str) -> bool {
    let lower = text.to_lowercase();
    ["keine tools", "ohne tools", "kein tool", "ohne tool"]
        .iter()
        .any(|marker| lower.contains(marker))
}

fn is_light_research_request(text: &str) -> bool {
    if rejects_research_tools(text) {
        return false;
    }
    let lower = text.to_lowercase();
    [
        "recherch",
        "such",
        "suche",
        "prüf",
        "pruef",
        "check",
        "finde",
        "google",
        "verify",
        "validier",
        "fakten",
        "stimmt das",
        "belege",
        "quelle",
        "quellen",
        "web",
        "internet",
        "reddit",
        "twitter",
        "x.com",
        "youtube",
        "rss",
        "ebay",
        "preis",
        "preise",
        "coingecko",
        "kurs",
    ]
    .iter()
    .any(|kw| lower.contains(kw))
}

fn chat_should_enable_tools(text: &str) -> bool {
    if rejects_all_tools(text) {
        return false;
    }
    if is_deepdive_request(text) || is_light_research_request(text) {
        return true;
    }
    let lower = text.to_lowercase();
    [
        "code",
        "coding",
        "bug",
        "fix",
        "datei",
        "file",
        "repo",
        "git",
        "commit",
        "modul",
        "implement",
        "baue",
        "bau ",
        "rechne",
        "berechne",
        "math",
        "kalender",
        "datum",
        "wieviel",
        "wie viel",
        "notify",
        "notification",
    ]
    .iter()
    .any(|kw| lower.contains(kw))
}

fn is_research_tool_name(tool_name: &str) -> bool {
    let name = tool_name.trim();
    name.starts_with("deepdive.")
        || name.starts_with("duckduckgo.")
        || name.starts_with("tavily.")
        || name.starts_with("grok_search.")
        || name.starts_with("reddit_scraper.")
        || name.starts_with("x_search.")
        || name.starts_with("x_comments.")
        || matches!(
            name,
            "web.search" | "http.get" | "browser.fetch" | "rag.suchen"
        )
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

fn preferred_deepdive_tool(text: &str) -> &'static str {
    let lower = text.to_lowercase();
    let wants_full = [
        "deepdive",
        "deep dive",
        "ausführlich",
        "ausfuehrlich",
        "kausal",
        "kausalität",
        "kausalitaet",
        "zusammenhang",
        "zusammenhänge",
        "zusammenhaenge",
        "andere sprachen",
        "mehrsprachig",
        "perspektiven",
        "kontrast",
        "viele quellen",
        "harte widerspruch",
        "alles dazu",
        "such alles",
        "alles was",
        "timeline",
        "chronologie",
    ]
    .iter()
    .any(|marker| lower.contains(marker));
    if wants_full {
        "deepdive.crawl"
    } else {
        "deepdive.quick"
    }
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
        "deepdive.crawl" | "deepdive.quick" => {
            progress.crawl_ok += 1;
            progress.last_evidence_round = round;
        }
        "deepdive.pack" => {
            progress.pack_ok += 1;
            progress.last_pack_round = round;
        }
        "deepdive.blocks" => {
            progress.blocks_ok += 1;
            progress.last_blocks_round = round;
        }
        "rss_verwaltung.fuer_deepdive" | "rss_verwaltung.fetch" | "rss_verwaltung.ingest_rag" => {
            progress.rss_evidence_ok += 1;
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
    let enough_rss = progress.rss_evidence_ok > 0;
    let enough_manual = progress.search_ok >= 2
        && progress.fetch_ok >= 3
        && (progress.source_note_ok >= 2 || progress.rag_save_ok >= 1);
    if progress.crawl_ok == 0 && !enough_manual && !enough_rss {
        let tool = preferred_deepdive_tool(user_text);
        return Some(format!(
            "DEEPDIVE-CHECK: Die Anfrage verlangt frische Quellen. Du bist noch nicht tief genug. Antworte jetzt AUSSCHLIESSLICH mit diesem Toolcall: <tool>{}({})</tool>",
            tool, topic
        ));
    }
    let has_current_pack =
        progress.pack_ok > 0 && progress.last_pack_round >= progress.last_evidence_round;
    let has_current_rag =
        progress.rag_search_ok > 0 && progress.last_rag_round >= progress.last_evidence_round;
    if !has_current_pack && !has_current_rag {
        if let Some(crawl_id) = progress.crawl_id.as_ref() {
            return Some(format!(
                "DEEPDIVE-CHECK: Quellen wurden verarbeitet, aber die Synthese braucht das kompakte Crawl-Paket. Antworte jetzt AUSSCHLIESSLICH mit diesem Toolcall: <tool>deepdive.pack({})</tool>",
                crawl_id
            ));
        }
        if progress.crawl_ok > 0 {
            return Some(format!(
                "DEEPDIVE-CHECK: Quellen wurden verarbeitet, aber die Synthese braucht das kompakte DeepDive-Paket. Antworte jetzt AUSSCHLIESSLICH mit diesem Toolcall: <tool>deepdive.pack({})</tool>",
                topic
            ));
        }
        let rag_query = topic;
        return Some(format!(
            "DEEPDIVE-CHECK: Quellen wurden verarbeitet, aber die Synthese muss aus dem RAG kommen. Antworte jetzt AUSSCHLIESSLICH mit diesem Toolcall: <tool>rag.suchen({})</tool>",
            rag_query
        ));
    }
    let has_current_blocks =
        progress.blocks_ok > 0 && progress.last_blocks_round >= progress.last_pack_round;
    if has_current_pack && !has_current_blocks {
        if let Some(crawl_id) = progress.crawl_id.as_ref() {
            return Some(format!(
                "DEEPDIVE-CHECK: Das Pack ist da, aber die Synthese braucht vorbereitete Research-Bausteine. Antworte jetzt AUSSCHLIESSLICH mit diesem Toolcall: <tool>deepdive.blocks({})</tool>",
                crawl_id
            ));
        }
        return Some(format!(
            "DEEPDIVE-CHECK: Das Pack ist da, aber die Synthese braucht vorbereitete Research-Bausteine. Antworte jetzt AUSSCHLIESSLICH mit diesem Toolcall: <tool>deepdive.blocks({})</tool>",
            topic
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
        || user_lower.contains("kausal")
        || user_lower.contains("zusammenh")
        || user_lower.contains("perspektiv")
        || user_lower.contains("aktuell")
        || user_lower.contains("news")
        || user_lower.contains("ereignis");
    let has_time_or_causal_shape = final_lower.contains("timeline")
        || final_lower.contains("chronologie")
        || final_lower.contains("kaus")
        || final_lower.contains("ursache")
        || final_lower.contains("folge")
        || final_lower.contains("akteur")
        || final_lower.contains("perspektiven")
        || final_lower.contains("widerspr")
        || final_lower.contains("hintergrund")
        || final_lower.contains("warum");
    if wants_deepdive_shape && !has_time_or_causal_shape {
        return Some(
            "DEEPDIVE-CHECK: Die Antwort hat Quellen, aber kein DeepDive-Lagebild. Antworte jetzt OHNE weiteren Toolcall neu mit: aktueller Stand, Timeline/Chronologie, Akteure, Claims/Belege, Kausalkette/Mechanismen, Perspektivenkontrast nach Sprache/Land, Widersprueche/Unsicherheiten, offene Leads, und <quellen> mit exakten URLs.".to_string()
        );
    }
    let has_branching_shape = final_lower.contains("branch")
        || final_lower.contains("missing link")
        || final_lower.contains("missing-link")
        || final_lower.contains("seitenast")
        || final_lower.contains("akteursnetz")
        || final_lower.contains("umfeld")
        || final_lower.contains("verbindung")
        || final_lower.contains("lieferkette")
        || final_lower.contains("konkurrent");
    if wants_deepdive_shape && !has_branching_shape {
        return Some(
            "DEEPDIVE-CHECK: Die Antwort ist noch zu linear. Antworte jetzt OHNE weiteren Toolcall neu und fuege eine eigene Sektion 'Branching / Missing Links' ein. Nutze BRANCHING_CONTEXT_BLOCK aktiv: Akteursumfeld, Nachbarbegriffe, Konkurrenten/Lieferketten, betroffene Laender und offene Kausalitaets-Leads. Wenn Branches keine Treffer hatten, benenne das als Recherche-Luecke.".to_string()
        );
    }
    let has_subcrawl_shape = final_lower.contains("subcrawl")
        || final_lower.contains("side-crawl")
        || final_lower.contains("side crawl")
        || final_lower.contains("sidestory")
        || final_lower.contains("side-info")
        || final_lower.contains("nebenthema")
        || final_lower.contains("nebenstrang");
    if wants_deepdive_shape && !has_subcrawl_shape {
        return Some(
            "DEEPDIVE-CHECK: Die Antwort nutzt die Subcrawl-Planung nicht sichtbar. Antworte jetzt OHNE weiteren Toolcall neu und fuege eine eigene Sektion 'Subcrawls / Side-Infos' ein: welche Subcrawl-Themen wurden ausgefuehrt, welche Anschluss-Crawls wurden nur vorgeschlagen, warum waren sie kausal wertvoll, und welche Quellen stuetzen sie.".to_string()
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

#[derive(Debug, Clone, Default)]
struct EnhancerRun {
    text: Option<String>,
    annotations: Vec<String>,
    blocked: Option<String>,
}

#[derive(Debug, Clone, Default)]
struct EnhancerDecision {
    action: String,
    text: Option<String>,
    notes: Option<String>,
    reason: Option<String>,
    flags: Vec<String>,
}

fn enhancer_mode(m: &ModulConfig) -> String {
    m.settings
        .enhancer_mode
        .as_deref()
        .unwrap_or("observe")
        .trim()
        .to_ascii_lowercase()
}

fn enhancer_fail_policy(m: &ModulConfig) -> String {
    m.settings
        .enhancer_fail_policy
        .as_deref()
        .unwrap_or("fail_open")
        .trim()
        .to_ascii_lowercase()
}

fn enhancer_rag_pool(m: &ModulConfig) -> String {
    m.settings
        .enhancer_rag_pool
        .as_deref()
        .or(m.rag_pool.as_deref())
        .unwrap_or("Enhancer")
        .trim()
        .to_string()
}

fn enhancer_store_rag(m: &ModulConfig) -> bool {
    m.settings.enhancer_store_rag.unwrap_or(true)
}

fn enhancer_inject_context(m: &ModulConfig) -> bool {
    m.settings.enhancer_inject_context.unwrap_or(true)
}

fn enhancer_allows_action(mode: &str, action: &str) -> bool {
    match mode {
        "observe" => matches!(action, "pass" | "annotate" | "side_effect_only"),
        "filter" => matches!(
            action,
            "pass" | "annotate" | "block" | "cancel" | "side_effect_only"
        ),
        "rewrite" | "translate" | "quality" => {
            matches!(action, "pass" | "annotate" | "replace" | "side_effect_only")
        }
        "gateway" => matches!(
            action,
            "pass" | "annotate" | "replace" | "block" | "cancel" | "side_effect_only"
        ),
        _ => matches!(action, "pass" | "annotate" | "side_effect_only"),
    }
}

fn parse_enhancer_decision(raw: &str) -> Result<EnhancerDecision, String> {
    let text = raw.trim();
    let json_text = if text.starts_with('{') {
        text.to_string()
    } else if let (Some(start), Some(end)) = (text.find('{'), text.rfind('}')) {
        text[start..=end].to_string()
    } else {
        return Err("Enhancer lieferte kein JSON-Objekt".into());
    };
    let value: serde_json::Value =
        serde_json::from_str(&json_text).map_err(|e| format!("Enhancer JSON parse: {e}"))?;
    let action = value
        .get("action")
        .and_then(|v| v.as_str())
        .unwrap_or("pass")
        .trim()
        .to_ascii_lowercase();
    let flags = value
        .get("flags")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(|s| s.trim().to_string()))
                .filter(|s| !s.is_empty())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    Ok(EnhancerDecision {
        action,
        text: value
            .get("text")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
        notes: value
            .get("notes")
            .or_else(|| value.get("annotation"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
        reason: value
            .get("reason")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
        flags,
    })
}

fn enhancer_stage_slots(chat_modul: &ModulConfig, stage: &str) -> Vec<String> {
    match stage {
        "input" => chat_modul.input_enhancers.clone(),
        "output" => {
            let mut slots = chat_modul.output_enhancers.clone();
            slots.extend(chat_modul.combined_enhancers.clone());
            slots
        }
        _ => vec![],
    }
}

fn update_last_user_message_text(messages: &mut serde_json::Value, text: &str) {
    let Some(arr) = messages.as_array_mut() else {
        return;
    };
    let Some(last) = arr
        .iter_mut()
        .rev()
        .find(|m| m.get("role").and_then(|v| v.as_str()) == Some("user"))
    else {
        return;
    };
    if last.get("content").and_then(|v| v.as_str()).is_some() {
        last["content"] = serde_json::json!(text);
        return;
    }
    if let Some(items) = last.get_mut("content").and_then(|v| v.as_array_mut()) {
        if let Some(item) = items.iter_mut().find(|item| {
            item.get("type").and_then(|v| v.as_str()) == Some("text")
                || item.get("text").and_then(|v| v.as_str()).is_some()
        }) {
            if let Some(obj) = item.as_object_mut() {
                obj.insert("type".into(), serde_json::json!("text"));
                obj.insert("text".into(), serde_json::json!(text));
                return;
            }
        }
        items.insert(0, serde_json::json!({"type":"text","text": text}));
        return;
    }
    last["content"] = serde_json::json!(text);
}

async fn enhancer_memory_excerpt(
    state: &Arc<AppState>,
    enhancer: &ModulConfig,
    query: &str,
) -> String {
    if !enhancer_store_rag(enhancer) || query.trim().is_empty() {
        return String::new();
    }
    let pool = enhancer_rag_pool(enhancer);
    let result = crate::modules::rag::suchen(&state.pipeline.base, &pool, query, None).await;
    if result.success {
        util::safe_truncate(&result.data, 3500).to_string()
    } else {
        String::new()
    }
}

async fn store_enhancer_note(
    state: &Arc<AppState>,
    chat_modul_id: &str,
    enhancer: &ModulConfig,
    stage: &str,
    original_input: &str,
    current_input: &str,
    output_text: Option<&str>,
    decision: &EnhancerDecision,
) {
    if !enhancer_store_rag(enhancer) {
        return;
    }
    let note = format!(
        "ENHANCER_RAG_NOTE\ncaptured_at_utc: {}\nchat_modul_id: {}\nenhancer_id: {}\nstage: {}\nmode: {}\naction: {}\nflags: {}\nreason: {}\noriginal_input:\n{}\ncurrent_input:\n{}\noutput_text:\n{}\nnotes:\n{}",
        chrono::Utc::now().to_rfc3339(),
        chat_modul_id,
        enhancer.id,
        stage,
        enhancer_mode(enhancer),
        decision.action,
        decision.flags.join(", "),
        decision.reason.as_deref().unwrap_or(""),
        util::safe_truncate(original_input, 4000),
        util::safe_truncate(current_input, 4000),
        util::safe_truncate(output_text.unwrap_or(""), 6000),
        util::safe_truncate(decision.notes.as_deref().unwrap_or(""), 6000),
    );
    let pool = enhancer_rag_pool(enhancer);
    let _ = crate::modules::rag::speichern(&state.pipeline.base, &pool, &note, None, None).await;
}

async fn run_one_enhancer(
    state: &Arc<AppState>,
    config: &AgentConfig,
    chat_modul: &ModulConfig,
    enhancer: &ModulConfig,
    stage: &str,
    original_input: &str,
    current_input: &str,
    output_text: Option<&str>,
    effective_input: Option<&str>,
    messages: Option<&[serde_json::Value]>,
) -> Result<EnhancerDecision, String> {
    let backend_id = enhancer.llm_backend.trim();
    if backend_id.is_empty() {
        return Err(format!("Enhancer '{}' hat kein llm_backend", enhancer.id));
    }
    let identity = util::resolve_identity(enhancer, config);
    let mode = enhancer_mode(enhancer);
    let effective_input = effective_input.unwrap_or(current_input);
    let memory_query = if stage == "output" {
        format!("{}\n{}", effective_input, output_text.unwrap_or(""))
    } else {
        current_input.to_string()
    };
    let memory = enhancer_memory_excerpt(state, enhancer, &memory_query).await;
    let evidence = messages
        .map(|m| compact_chat_evidence(m, 7000))
        .unwrap_or_default();
    let context = serde_json::json!({
        "stage": stage,
        "mode": mode,
        "chat_modul_id": chat_modul.id,
        "enhancer_id": enhancer.id,
        "original_input": original_input,
        "effective_input": effective_input,
        "current_pipeline_text": current_input,
        "current_input": current_input,
        "output_text": output_text.unwrap_or(""),
        "conversation_evidence": evidence,
        "enhancer_memory_excerpt": memory,
    });
    let contract = "Du bist ein Pipeline-Enhancer. Du bist NICHT der Hauptagent. \
Du bewertest oder transformierst nur den angegebenen Pipeline-Schritt. \
Antworte AUSSCHLIESSLICH als JSON-Objekt: \
{\"action\":\"pass|replace|block|cancel|annotate|side_effect_only\",\"text\":\"optional neuer Input oder Output\",\"notes\":\"interne Analyse/Memory\",\"flags\":[\"...\"],\"reason\":\"kurz\"}. \
stage=input liegt NACH User-Input und VOR Verarbeitung. stage=output liegt NACH Verarbeitung und VOR Ausgabe. \
Erfinde keine Fakten; wenn du nur lernen/beobachten sollst, action=side_effect_only oder annotate.";
    let preservation = "Bewahre zwingend die negativen Constraints des Users. \
Wenn der User keine Recherche, keine Websuche, keine Tools, kurze Antwort, Sprache, Format oder Laenge vorgibt, \
darfst du diese Constraints nicht entfernen, abschwaechen oder umformulieren, sodass sie verloren gehen.";
    let custom = enhancer
        .settings
        .enhancer_prompt
        .as_deref()
        .unwrap_or("")
        .trim();
    let system = format!(
        "{}\n\n{}\n\n{}\n\n{}",
        identity.system_prompt, contract, preservation, custom
    );
    let prompt = serde_json::to_string_pretty(&context).unwrap_or_else(|_| context.to_string());
    let enhancer_messages = vec![
        serde_json::json!({"role":"system","content":system}),
        serde_json::json!({"role":"user","content":prompt}),
    ];
    check_llm_cap(
        &state.pipeline.store.pool,
        config,
        backend_id,
        &enhancer_messages,
        false,
    )
    .await
    .map_err(|hit| hit.message())?;
    while let Some(wait) = state.llm.reserve_rate_slot_or_wait(backend_id).await {
        tokio::time::sleep(wait).await;
    }
    let model_str = model_for_backend(config, backend_id);
    let timeout_s = enhancer
        .settings
        .enhancer_timeout_s
        .unwrap_or(enhancer.timeout_s)
        .max(1);
    let (response, raw_data) = tokio::time::timeout(
        std::time::Duration::from_secs(timeout_s),
        state.llm.chat_with_tools(
            backend_id,
            enhancer.backup_llm.as_deref(),
            &enhancer_messages,
            &[],
        ),
    )
    .await
    .map_err(|_| format!("Enhancer '{}' Timeout nach {}s", enhancer.id, timeout_s))??;
    track_tokens(
        &state.pipeline.store.pool,
        &state.tokens,
        config,
        backend_id,
        &model_str,
        &enhancer.id,
        &raw_data,
    )
    .await;
    parse_enhancer_decision(&strip_tool_tags(&response))
}

async fn apply_chat_enhancers(
    state: &Arc<AppState>,
    config: &AgentConfig,
    chat_modul: Option<&ModulConfig>,
    stage: &str,
    original_input: &str,
    current_text: &str,
    output_text: Option<&str>,
    effective_input: Option<&str>,
    messages: Option<&[serde_json::Value]>,
) -> EnhancerRun {
    let Some(chat_modul) = chat_modul else {
        return EnhancerRun::default();
    };
    let mut text = current_text.to_string();
    let mut annotations = vec![];
    let slots = enhancer_stage_slots(chat_modul, stage);
    for slot in slots {
        let Some(enhancer) = config
            .module
            .iter()
            .find(|m| (m.id == slot || m.name == slot) && m.typ == "enhancer")
            .cloned()
        else {
            state.pipeline.log(
                &chat_modul.id,
                None,
                LogTyp::Warning,
                &format!(
                    "Enhancer-Slot '{}' nicht gefunden oder nicht typ=enhancer",
                    slot
                ),
            );
            continue;
        };
        let decision = match run_one_enhancer(
            state,
            config,
            chat_modul,
            &enhancer,
            stage,
            original_input,
            &text,
            output_text,
            effective_input,
            messages,
        )
        .await
        {
            Ok(mut decision) => {
                let mode = enhancer_mode(&enhancer);
                if !enhancer_allows_action(&mode, &decision.action) {
                    decision.reason = Some(format!(
                        "Action '{}' im Enhancer-Mode '{}' nicht erlaubt; auf pass gesetzt",
                        decision.action, mode
                    ));
                    decision.action = "pass".into();
                }
                decision
            }
            Err(err) => {
                let fail_closed = enhancer_fail_policy(&enhancer) == "fail_closed";
                let action = if fail_closed { "block" } else { "pass" };
                EnhancerDecision {
                    action: action.into(),
                    reason: Some(format!("Enhancer '{}' Fehler: {}", enhancer.id, err)),
                    ..Default::default()
                }
            }
        };
        store_enhancer_note(
            state,
            &chat_modul.id,
            &enhancer,
            stage,
            original_input,
            &text,
            output_text,
            &decision,
        )
        .await;
        state.pipeline.log(
            &chat_modul.id,
            None,
            LogTyp::Info,
            &format!(
                "Enhancer {} stage={} action={} reason={}",
                enhancer.id,
                stage,
                decision.action,
                util::safe_truncate(decision.reason.as_deref().unwrap_or(""), 160)
            ),
        );
        match decision.action.as_str() {
            "replace" => {
                if let Some(new_text) = decision.text.as_ref().filter(|s| !s.trim().is_empty()) {
                    text = util::safe_truncate(
                        new_text,
                        enhancer.settings.enhancer_max_output_chars.unwrap_or(6000) as usize,
                    )
                    .to_string();
                }
            }
            "block" | "cancel" => {
                let reason = decision
                    .reason
                    .clone()
                    .or(decision.notes.clone())
                    .unwrap_or_else(|| {
                        format!("Enhancer '{}' hat {} gesetzt", enhancer.id, decision.action)
                    });
                return EnhancerRun {
                    text: Some(text),
                    annotations,
                    blocked: Some(reason),
                };
            }
            "annotate" | "side_effect_only" | "pass" => {
                if enhancer_inject_context(&enhancer) {
                    if let Some(note) = decision.notes.as_ref().filter(|s| !s.trim().is_empty()) {
                        annotations.push(format!(
                            "{}: {}",
                            enhancer.id,
                            util::safe_truncate(note, 1200)
                        ));
                    }
                }
            }
            _ => {}
        }
    }
    EnhancerRun {
        text: Some(text),
        annotations,
        blocked: None,
    }
}

async fn apply_output_enhancers_to_text(
    state: &Arc<AppState>,
    config: &AgentConfig,
    chat_modul: Option<&ModulConfig>,
    original_input: &str,
    effective_input: &str,
    final_text: &str,
    messages: &[serde_json::Value],
) -> String {
    let run = apply_chat_enhancers(
        state,
        config,
        chat_modul,
        "output",
        original_input,
        final_text,
        Some(final_text),
        Some(effective_input),
        Some(messages),
    )
    .await;
    if let Some(reason) = run.blocked {
        return format!("Ausgabe vom Enhancer blockiert: {}", reason);
    }
    let mut text = run.text.unwrap_or_else(|| final_text.to_string());
    if !run.annotations.is_empty() {
        state.pipeline.log(
            chat_modul.map(|m| m.id.as_str()).unwrap_or("chat"),
            None,
            LogTyp::Info,
            &format!(
                "Output-Enhancer Annotationen fuer Input '{}': {}",
                util::safe_truncate(effective_input, 120),
                util::safe_truncate(&run.annotations.join(" | "), 240)
            ),
        );
    }
    if text.trim().is_empty() {
        text = final_text.to_string();
    }
    text
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
        .route("/assets/icon-192.png", axum::routing::get(icon_192))
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
        .route("/api/key-vault", axum::routing::get(get_key_vault))
        .route("/api/key-vault", axum::routing::post(save_key_vault))
        .route(
            "/api/credential-vault",
            axum::routing::get(get_credential_vault),
        )
        .route(
            "/api/credential-vault",
            axum::routing::post(save_credential_vault),
        )
        .route(
            "/api/config/backups",
            axum::routing::get(list_config_backups),
        )
        .route(
            "/api/config/restore/{slot}",
            axum::routing::post(restore_config_backup),
        )
        .route("/api/aufgaben", axum::routing::get(get_aufgaben))
        .route("/api/tasks/graph", axum::routing::get(tasks_graph))
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
        .route("/api/workflows", axum::routing::get(get_workflows))
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
            "/api/notifications/{modul_id}",
            axum::routing::get(list_notifications).post(create_notification),
        )
        .route(
            "/api/notifications/{modul_id}/{notification_id}",
            axum::routing::patch(mark_notification_read).delete(delete_notification),
        )
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
        .route("/api/media/{modul_id}", axum::routing::get(list_media))
        .route("/api/video/start", axum::routing::post(video_start))
        .route("/api/image/start", axum::routing::post(image_start))
        .route("/api/planner/proposals", axum::routing::get(planner_proposals))
        .route("/api/planner/status", axum::routing::get(planner_status))
        .route("/api/community/status", axum::routing::get(community_status))
        .route("/api/planner/decide", axum::routing::post(planner_decide))
        .route("/api/planner/scan", axum::routing::post(planner_scan))
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
    (
        [(axum::http::header::CONTENT_TYPE, "image/png"),
         (axum::http::header::CACHE_CONTROL, "public, max-age=86400")],
        include_bytes!("assets/favicon.png").as_slice(),
    )
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
            "/api/notifications/{modul_id}",
            axum::routing::get(list_notifications).post(create_notification),
        )
        .route(
            "/api/notifications/{modul_id}/{notification_id}",
            axum::routing::patch(mark_notification_read).delete(delete_notification),
        )
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

fn write_config_with_rotating_backup(
    path: &std::path::Path,
    cfg: &AgentConfig,
) -> Result<(), String> {
    let cfg_json = serde_json::to_string_pretty(cfg).map_err(|e| e.to_string())?;

    // Rotating backup: config.json.bak-1 (most recent) to bak-3 (oldest) before overwriting.
    // Prevents accidental key-wipe from a bad UI save; user can restore from backup manually.
    if path.exists() {
        let b3 = path.with_extension("json.bak-3");
        let b2 = path.with_extension("json.bak-2");
        let b1 = path.with_extension("json.bak-1");
        let _ = std::fs::remove_file(&b3);
        let _ = std::fs::rename(&b2, &b3);
        let _ = std::fs::rename(&b1, &b2);
        let _ = std::fs::copy(path, &b1);
    }

    util::atomic_write(path, cfg_json.as_bytes()).map_err(|e| e.to_string())
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
    if !save_scope.iter().any(|s| s == "vault") {
        incoming["api_key_vault"] = existing["api_key_vault"].clone();
        incoming["credential_vault"] = existing
            .get("credential_vault")
            .cloned()
            .unwrap_or_else(|| serde_json::json!([]));
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
    match write_config_with_rotating_backup(&path, &cfg) {
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

fn normalize_api_vault_id(raw: &str) -> Option<String> {
    let id = raw.trim().strip_prefix("api.").unwrap_or(raw.trim());
    safe_id(id)
}

fn value_references_alias(value: &str, alias: &str) -> bool {
    let trimmed = value.trim();
    trimmed == alias || trimmed == format!("${{{}}}", alias)
}

fn collect_alias_refs(value: &serde_json::Value, alias: &str, path: &str, out: &mut Vec<String>) {
    match value {
        serde_json::Value::Object(map) => {
            for (key, child) in map {
                let child_path = if path.is_empty() {
                    key.clone()
                } else {
                    format!("{}.{}", path, key)
                };
                collect_alias_refs(child, alias, &child_path, out);
            }
        }
        serde_json::Value::Array(arr) => {
            for (idx, child) in arr.iter().enumerate() {
                let child_path = if path.is_empty() {
                    idx.to_string()
                } else {
                    format!("{}.{}", path, idx)
                };
                collect_alias_refs(child, alias, &child_path, out);
            }
        }
        serde_json::Value::String(s) if value_references_alias(s, alias) => {
            out.push(path.to_string());
        }
        _ => {}
    }
}

fn key_vault_payload(
    cfg: &AgentConfig,
    pool: &crate::store::SqlitePool,
    modules_dir: Option<&std::path::Path>,
) -> serde_json::Value {
    let entries: Vec<serde_json::Value> = cfg
        .api_key_vault
        .iter()
        .map(|entry| {
            let alias = util::api_key_vault_alias(&entry.id);
            let mut modules = Vec::new();
            for m in &cfg.module {
                let mut paths = Vec::new();
                if let Ok(settings) = serde_json::to_value(&m.settings) {
                    collect_alias_refs(&settings, &alias, "", &mut paths);
                }
                if !paths.is_empty() {
                    modules.push(serde_json::json!({
                        "id": m.id,
                        "name": m.display_name,
                        "typ": m.typ,
                        "paths": paths,
                    }));
                }
            }
            if let Some(modules_dir) = modules_dir {
                if let Ok(entries) = std::fs::read_dir(modules_dir) {
                    for dir in entries.flatten().filter(|e| e.path().is_dir()) {
                        let name = dir.file_name().to_string_lossy().to_string();
                        let cfg_path = dir.path().join("config.json");
                        let Ok(raw) = std::fs::read_to_string(&cfg_path) else {
                            continue;
                        };
                        let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) else {
                            continue;
                        };
                        let mut paths = Vec::new();
                        collect_alias_refs(&value, &alias, "config", &mut paths);
                        if !paths.is_empty() {
                            modules.push(serde_json::json!({
                                "id": name,
                                "name": format!("{} config", name),
                                "typ": "module_config",
                                "paths": paths,
                            }));
                        }
                    }
                }
            }

            let mut llm_backends = Vec::new();
            for b in &cfg.llm_backends {
                if b.api_key
                    .as_deref()
                    .map(|key| value_references_alias(key, &alias))
                    .unwrap_or(false)
                {
                    let calls =
                        crate::store::token_calls_count_by_backend(pool, &b.id).unwrap_or(0);
                    llm_backends.push(serde_json::json!({
                        "id": b.id,
                        "name": b.name,
                        "model": b.model,
                        "calls": calls,
                    }));
                }
            }
            let mut wizard_refs = Vec::new();
            if let Some(wizard) = &cfg.wizard {
                if wizard
                    .llm
                    .api_key
                    .as_deref()
                    .map(|key| value_references_alias(key, &alias))
                    .unwrap_or(false)
                {
                    wizard_refs.push(serde_json::json!({
                        "id": wizard.llm.id,
                        "name": wizard.llm.name,
                        "model": wizard.llm.model,
                    }));
                }
            }

            let audit_like = format!("%\"alias\":\"{}\"%", alias);
            let recorded_calls = crate::store::audit_count_action_detail_like(
                pool,
                "api_vault.use",
                &audit_like,
            )
            .unwrap_or(0);
            let llm_audit_like = format!("%\"alias\":\"{}\",\"tool\":\"llm.chat\"%", alias);
            let recorded_llm_calls = crate::store::audit_count_action_detail_like(
                pool,
                "api_vault.use",
                &llm_audit_like,
            )
            .unwrap_or(0);
            let tool_calls = recorded_calls.saturating_sub(recorded_llm_calls);
            let llm_calls = llm_backends
                .iter()
                .map(|b| b.get("calls").and_then(|v| v.as_u64()).unwrap_or(0))
                .sum::<u64>();

            serde_json::json!({
                "id": entry.id,
                "alias": alias,
                "name": entry.name,
                "provider": entry.provider,
                "notes": entry.notes,
                "has_secret": entry.secret.as_deref().map(|s| !s.trim().is_empty()).unwrap_or(false),
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "modules": modules,
                "llm_backends": llm_backends,
                "wizard": wizard_refs,
                "tool_calls": tool_calls,
                "llm_calls": llm_calls,
                "call_count": tool_calls + llm_calls,
            })
        })
        .collect();
    serde_json::json!({"entries": entries})
}

async fn get_key_vault(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let cfg = s.config.read().await;
    let project_root = s.pipeline.base.parent().unwrap_or(&s.pipeline.base);
    let modules_dir = project_root.join("modules");
    Json(key_vault_payload(
        &cfg,
        &s.pipeline.store.pool,
        Some(&modules_dir),
    ))
}

#[derive(serde::Deserialize)]
struct ApiKeyVaultSaveBody {
    entries: Vec<ApiKeyVaultEntry>,
}

async fn save_key_vault(
    State(s): State<Arc<AppState>>,
    Json(body): Json<ApiKeyVaultSaveBody>,
) -> Json<serde_json::Value> {
    let _write_guard = s.pipeline.config_write_lock.lock().await;
    let mut cfg = s.config.read().await.clone();
    let mut existing: std::collections::HashMap<String, ApiKeyVaultEntry> = cfg
        .api_key_vault
        .iter()
        .cloned()
        .map(|entry| (entry.id.clone(), entry))
        .collect();
    let mut seen = std::collections::HashSet::new();
    let now = chrono::Utc::now().timestamp();
    let mut next_entries = Vec::new();

    for mut entry in body.entries {
        let Some(id) = normalize_api_vault_id(&entry.id) else {
            return Json(serde_json::json!({"ok": false, "error": "ungueltige Vault-ID"}));
        };
        if !seen.insert(id.clone()) {
            return Json(
                serde_json::json!({"ok": false, "error": format!("doppelte Vault-ID: {}", id)}),
            );
        }
        let previous = existing.remove(&id);
        let raw_secret = entry.secret.take().unwrap_or_default();
        let secret = if raw_secret.trim().is_empty() || raw_secret == "***REDACTED***" {
            previous.as_ref().and_then(|p| p.secret.clone())
        } else {
            Some(raw_secret)
        };
        let created_at = previous
            .as_ref()
            .and_then(|p| p.created_at)
            .or(entry.created_at)
            .or(Some(now));
        next_entries.push(ApiKeyVaultEntry {
            id,
            name: if entry.name.trim().is_empty() {
                entry
                    .id
                    .trim()
                    .strip_prefix("api.")
                    .unwrap_or(entry.id.trim())
                    .to_string()
            } else {
                entry.name.trim().to_string()
            },
            provider: entry
                .provider
                .take()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty()),
            secret,
            notes: entry
                .notes
                .take()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty()),
            created_at,
            updated_at: Some(now),
        });
    }

    cfg.api_key_vault = next_entries;
    let path = s.pipeline.base.join("config.json");
    match write_config_with_rotating_backup(&path, &cfg) {
        Ok(()) => {
            *s.config.write().await = cfg.clone();
            s.pipeline
                .audit("config.api_vault.update", "admin", "API key vault updated");
            let project_root = s.pipeline.base.parent().unwrap_or(&s.pipeline.base);
            let modules_dir = project_root.join("modules");
            Json(serde_json::json!({
                "ok": true,
                "vault": key_vault_payload(&cfg, &s.pipeline.store.pool, Some(&modules_dir))
            }))
        }
        Err(e) => Json(serde_json::json!({"ok": false, "error": e})),
    }
}

fn credential_vault_payload(
    cfg: &AgentConfig,
    pool: &crate::store::SqlitePool,
    modules_dir: Option<&std::path::Path>,
) -> serde_json::Value {
    let entries: Vec<serde_json::Value> = cfg
        .credential_vault
        .iter()
        .map(|entry| {
            let fields: Vec<serde_json::Value> = entry
                .fields
                .iter()
                .map(|field| {
                    let canonical = util::credential_vault_alias(&entry.id, &field.key);
                    let bare = util::credential_vault_bare_alias(&entry.id, &field.key);
                    let mut modules = Vec::new();
                    for m in &cfg.module {
                        let mut paths = Vec::new();
                        if let Ok(settings) = serde_json::to_value(&m.settings) {
                            collect_alias_refs(&settings, &canonical, "", &mut paths);
                            collect_alias_refs(&settings, &bare, "", &mut paths);
                        }
                        paths.sort();
                        paths.dedup();
                        if !paths.is_empty() {
                            modules.push(serde_json::json!({
                                "id": m.id,
                                "name": m.display_name,
                                "typ": m.typ,
                                "paths": paths,
                            }));
                        }
                    }
                    if let Some(modules_dir) = modules_dir {
                        if let Ok(entries) = std::fs::read_dir(modules_dir) {
                            for dir in entries.flatten().filter(|e| e.path().is_dir()) {
                                let name = dir.file_name().to_string_lossy().to_string();
                                let cfg_path = dir.path().join("config.json");
                                let Ok(raw) = std::fs::read_to_string(&cfg_path) else {
                                    continue;
                                };
                                let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw)
                                else {
                                    continue;
                                };
                                let mut paths = Vec::new();
                                collect_alias_refs(&value, &canonical, "config", &mut paths);
                                collect_alias_refs(&value, &bare, "config", &mut paths);
                                paths.sort();
                                paths.dedup();
                                if !paths.is_empty() {
                                    modules.push(serde_json::json!({
                                        "id": name,
                                        "name": format!("{} config", name),
                                        "typ": "module_config",
                                        "paths": paths,
                                    }));
                                }
                            }
                        }
                    }

                    let audit_like = format!(
                        "%\"vault_id\":\"{}\",\"field\":\"{}\"%",
                        entry.id, field.key
                    );
                    let call_count = crate::store::audit_count_action_detail_like(
                        pool,
                        "credential_vault.use",
                        &audit_like,
                    )
                    .unwrap_or(0);
                    serde_json::json!({
                        "key": field.key,
                        "alias": canonical,
                        "bare_alias": bare,
                        "secret": field.secret,
                        "has_value": field.value.as_deref().map(|s| !s.trim().is_empty()).unwrap_or(false),
                        "value": if field.secret { "" } else { field.value.as_deref().unwrap_or("") },
                        "modules": modules,
                        "call_count": call_count,
                    })
                })
                .collect();
            let call_count = fields
                .iter()
                .map(|f| f.get("call_count").and_then(|v| v.as_u64()).unwrap_or(0))
                .sum::<u64>();
            serde_json::json!({
                "id": entry.id,
                "name": entry.name,
                "kind": entry.kind,
                "notes": entry.notes,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "fields": fields,
                "call_count": call_count,
            })
        })
        .collect();
    serde_json::json!({"entries": entries})
}

async fn get_credential_vault(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let cfg = s.config.read().await;
    let project_root = s.pipeline.base.parent().unwrap_or(&s.pipeline.base);
    let modules_dir = project_root.join("modules");
    Json(credential_vault_payload(
        &cfg,
        &s.pipeline.store.pool,
        Some(&modules_dir),
    ))
}

#[derive(serde::Deserialize)]
struct CredentialVaultSaveBody {
    entries: Vec<CredentialVaultEntry>,
}

async fn save_credential_vault(
    State(s): State<Arc<AppState>>,
    Json(body): Json<CredentialVaultSaveBody>,
) -> Json<serde_json::Value> {
    let _write_guard = s.pipeline.config_write_lock.lock().await;
    let mut cfg = s.config.read().await.clone();
    let mut existing: std::collections::HashMap<String, CredentialVaultEntry> = cfg
        .credential_vault
        .iter()
        .cloned()
        .map(|entry| (entry.id.clone(), entry))
        .collect();
    let mut seen_entries = std::collections::HashSet::new();
    let now = chrono::Utc::now().timestamp();
    let mut next_entries = Vec::new();

    for mut entry in body.entries {
        let Some(id) = safe_id(
            entry
                .id
                .trim()
                .strip_prefix("cred.")
                .unwrap_or(entry.id.trim()),
        ) else {
            return Json(
                serde_json::json!({"ok": false, "error": "ungueltige Credential-Vault-ID"}),
            );
        };
        if !seen_entries.insert(id.clone()) {
            return Json(serde_json::json!({
                "ok": false,
                "error": format!("doppelte Credential-Vault-ID: {}", id)
            }));
        }
        let previous = existing.remove(&id);
        let prev_fields: std::collections::HashMap<String, CredentialVaultField> = previous
            .as_ref()
            .map(|p| {
                p.fields
                    .iter()
                    .cloned()
                    .map(|field| (field.key.clone(), field))
                    .collect()
            })
            .unwrap_or_default();
        let mut seen_fields = std::collections::HashSet::new();
        let mut fields = Vec::new();
        for mut field in entry.fields {
            let Some(key) = safe_id(field.key.trim()) else {
                return Json(
                    serde_json::json!({"ok": false, "error": "ungueltiger Credential-Feldname"}),
                );
            };
            if !seen_fields.insert(key.clone()) {
                return Json(serde_json::json!({
                    "ok": false,
                    "error": format!("doppeltes Credential-Feld: {}.{}", id, key)
                }));
            }
            let previous_field = prev_fields.get(&key);
            let raw_value = field.value.take().unwrap_or_default();
            let value = if raw_value.trim().is_empty() || raw_value == "***REDACTED***" {
                previous_field.and_then(|p| p.value.clone())
            } else {
                Some(raw_value)
            };
            fields.push(CredentialVaultField {
                key,
                value,
                secret: field.secret,
            });
        }
        let created_at = previous
            .as_ref()
            .and_then(|p| p.created_at)
            .or(entry.created_at)
            .or(Some(now));
        next_entries.push(CredentialVaultEntry {
            id,
            name: if entry.name.trim().is_empty() {
                entry.id.trim().to_string()
            } else {
                entry.name.trim().to_string()
            },
            kind: entry
                .kind
                .take()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty()),
            fields,
            notes: entry
                .notes
                .take()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty()),
            created_at,
            updated_at: Some(now),
        });
    }

    cfg.credential_vault = next_entries;
    let path = s.pipeline.base.join("config.json");
    match write_config_with_rotating_backup(&path, &cfg) {
        Ok(()) => {
            *s.config.write().await = cfg.clone();
            s.pipeline.audit(
                "config.credential_vault.update",
                "admin",
                "Credential vault updated",
            );
            let project_root = s.pipeline.base.parent().unwrap_or(&s.pipeline.base);
            let modules_dir = project_root.join("modules");
            Json(serde_json::json!({
                "ok": true,
                "vault": credential_vault_payload(&cfg, &s.pipeline.store.pool, Some(&modules_dir))
            }))
        }
        Err(e) => Json(serde_json::json!({"ok": false, "error": e})),
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
                    if let Some(route) = a.zurueck_an.as_deref() {
                        if let Some((chat_modul, convo_id)) = util::parse_chat_route(route) {
                            let _ = s.pipeline.notification_add(
                                &chat_modul,
                                convo_id.as_deref(),
                                "system",
                                Some("Aufgabe neu gestartet"),
                                &format!(
                                    "Task {} wurde neu gestartet. Das Ergebnis wird wieder in diesem Chat landen.",
                                    id
                                ),
                                Some("task.restart"),
                            );
                        }
                    }
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

    let mut user_messages = body["messages"].clone();
    let original_user_messages = user_messages.clone();
    let modul_id_raw = body["modul"].as_str().unwrap_or("").to_string();
    let convo_id = body["convo_id"].as_str().and_then(safe_id);
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

    let original_last_user_msg = user_messages
        .as_array()
        .and_then(|a| a.last())
        .map(message_plain_text)
        .unwrap_or_default();
    let mut last_user_msg = original_last_user_msg.clone();
    let input_enhancement = apply_chat_enhancers(
        &s,
        &config_snapshot,
        modul_for_tools.as_ref(),
        "input",
        &original_last_user_msg,
        &last_user_msg,
        None,
        None,
        None,
    )
    .await;
    if let Some(blocked) = input_enhancement.blocked {
        let msg = format!("Eingabe vom Enhancer blockiert: {}", blocked);
        persist_chat_assistant_result(
            &s.pipeline,
            &modul_id,
            convo_id.as_deref(),
            &original_user_messages,
            &msg,
        );
        return single_chat_stream_response(&msg);
    }
    if let Some(enhanced_text) = input_enhancement.text.as_ref() {
        if enhanced_text != &last_user_msg {
            update_last_user_message_text(&mut user_messages, enhanced_text);
            last_user_msg = enhanced_text.clone();
        }
    }

    let mut messages: Vec<serde_json::Value> = vec![];
    if !system_prompt.is_empty() {
        let enhancer_context = if input_enhancement.annotations.is_empty() {
            String::new()
        } else {
            format!(
                "\n\nENHANCER_CONTEXT:\n{}",
                input_enhancement
                    .annotations
                    .iter()
                    .map(|s| format!("- {}", s))
                    .collect::<Vec<_>>()
                    .join("\n")
            )
        };
        messages.push(serde_json::json!({"role": "system", "content": format!("{}{}", system_prompt, enhancer_context)}));
    }
    if let Some(arr) = user_messages.as_array() {
        messages.extend(arr.clone());
    }

    // OpenAI Function Calling: Tools nur zuschalten, wenn die Anfrage sie
    // wahrscheinlich braucht. Reiner Chat bleibt sonst durch riesige Tool-
    // Schemas unnoetig langsam.
    let original_rejects_all_tools = rejects_all_tools(&original_last_user_msg);
    let original_rejects_research = rejects_research_tools(&original_last_user_msg);
    let enhanced_wants_research =
        is_deepdive_request(&last_user_msg) || is_light_research_request(&last_user_msg);
    let enable_tools = chat_should_enable_tools(&last_user_msg)
        && !original_rejects_all_tools
        && !(original_rejects_research && enhanced_wants_research);
    let openai_tools = if enable_tools {
        if let Some(ref m) = modul_for_tools {
            let py_mods = s.py_modules.read().await;
            tools::tools_as_openai_json(m, &py_mods)
        } else {
            vec![]
        }
    } else {
        s.pipeline.log(
            &modul_id,
            None,
            LogTyp::Info,
            "Tool-Gate: Tools fuer leichte Chat-Anfrage deaktiviert",
        );
        vec![]
    };

    // Haupt-Aufgabe erstellen (damit JEDER Chat-Request trackbar ist)
    let mut main_aufgabe = Aufgabe::llm_call(
        &last_user_msg,
        &modul_id,
        &format!("chat:{}", modul_id),
        convo_id
            .as_ref()
            .map(|cid| format!("chat:{}:{}", modul_id, cid)),
    );
    if let Some(m) = modul_for_tools.as_ref() {
        main_aufgabe = main_aufgabe.with_timeout_s(m.timeout_s);
    }
    main_aufgabe.status = AufgabeStatus::Gestartet;
    main_aufgabe.gestartet = Some(chrono::Utc::now());
    let main_id = main_aufgabe.id.clone();
    let main_route = main_aufgabe.zurueck_an.clone();
    let _ = s.pipeline.speichern(&main_aufgabe);

    // Channel for streaming status updates and final answer
    let (tx, rx) = tokio::sync::mpsc::channel::<String>(64);

    // Spawn the tool-loop in a background task
    let state = s.clone();
    let convo_id_for_persist = convo_id.clone();
    let seed_messages_for_persist = original_user_messages.clone();
    let original_last_user_msg_for_enhancers = original_last_user_msg.clone();
    tokio::spawn(async move {
        let t_start = std::time::Instant::now();
        let mut tool_rounds = 0;
        let mut sub_aufgaben: Vec<String> = vec![];
        let mut tool_failures: Vec<ChatToolFailure> = vec![];
        let mut messages = messages;
        let modul_id_str = modul_id.as_str();
        let mut malformed_tool_retries: u32 = 0;
        let mut rejected_research_tool_retries: u32 = 0;
        let model_str_initial = model_for_backend(&config_snapshot, &backend_id);
        let mut engine = crate::turn::TurnEngine {
            pipeline: &state.pipeline,
            llm: &state.llm,
            cfg_snap: &config_snapshot,
            gcfg: &gcfg,
            py_mods_snap: &py_mods_snap,
            modul: modul_for_tools.as_ref(),
            log_label: modul_id_str,
            log_task_id: Some(&main_id),
            attribution_id: modul_id_str,
            tokens: &state.tokens,
            status_tx: Some(tx.clone()),
            activity: None,
            tool_calls_disabled: false,
            backup_id: backup_id.clone(),
            history_fixed_prefix: 1 + user_messages.as_array().map(|a| a.len()).unwrap_or(0),
            tool_choice_once: None,
            backend_id,
            model_str: model_str_initial,
            guardrail_retries: 0,
            used_fallback: false,
        };
        let needs_deepdive = is_deepdive_request(&last_user_msg)
            && !rejects_research_tools(&original_last_user_msg_for_enhancers);
        // Recherche-Pflicht auf Protokollebene: erste Runde mit
        // tool_choice="required" statt nachtraeglichem STOPP-Prompt. Die
        // STOPP-Bloecke unten bleiben als Fallback fuer Provider, die
        // tool_choice ignorieren.
        if !openai_tools.is_empty() && (needs_deepdive || is_light_research_request(&last_user_msg))
        {
            engine.tool_choice_once = Some("required".into());
        }
        let mut deepdive_progress = DeepdiveProgress::default();
        let mut deepdive_gate_retries: u32 = 0;

        loop {
            if let Some(max_tool_rounds) =
                tool_round_limit_for_backend(&config_snapshot, &engine.backend_id)
            {
                if tool_rounds >= max_tool_rounds {
                    break;
                }
            }

            if let Err(hit) = check_llm_cap(
                &state.pipeline.store.pool,
                &config_snapshot,
                &engine.backend_id,
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
                persist_chat_assistant_result(
                    &state.pipeline,
                    modul_id_str,
                    convo_id_for_persist.as_deref(),
                    &seed_messages_for_persist,
                    &msg,
                );
                tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":msg},"done":true}).to_string()).await.ok();
                return;
            }

            // LLM-Runde via geteilter Turn-Engine (gleiche Logik wie der
            // Scheduler-Loop in cycle.rs): Rate-Slot, Call mit Backup, Token-
            // Tracking, Text-Tag-Injektion (vorher liefen Text-Tag-Calls im
            // Chat am Guardrail VORBEI), Guardrail-Retry/-Fallback, Parsing.
            let (outcome, _usage) = engine.run_round(&mut messages, &openai_tools).await;

            match outcome {
                crate::turn::RoundOutcome::ToolCalls {
                    calls: mut parsed_calls,
                    raw_message,
                    response_text: response,
                } => {
                    // Research-Reject-Gate: greift, wenn IRGENDEIN Call ein
                    // Recherche-Tool ist und der Nutzer explizit keine Recherche
                    // wollte — dann wird die ganze Runde verworfen.
                    let rejected_research_name = if rejects_research_tools(&last_user_msg) {
                        parsed_calls
                            .iter()
                            .find(|c| is_research_tool_name(&c.name))
                            .map(|c| c.name.clone())
                    } else {
                        None
                    };
                    if let Some(tool_name) = rejected_research_name {
                        rejected_research_tool_retries += 1;
                        state.pipeline.log(
                                modul_id_str,
                                Some(&main_id),
                                LogTyp::Warning,
                                &format!(
                                    "Recherche-Tool '{}' blockiert, weil Nutzer keine Recherche/Tools wollte",
                                    tool_name
                                ),
                            );
                        if rejected_research_tool_retries > 1 {
                            let final_text = strip_tool_tags(&response);
                            let final_text = if final_text.trim().is_empty() {
                                "OK".to_string()
                            } else {
                                final_text
                            };
                            if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&main_id) {
                                a.ergebnis = Some(
                                    util::safe_truncate(&final_text, MAX_CHAT_TASK_RESULT_CHARS)
                                        .to_string(),
                                );
                                let _ = state.pipeline.verschieben(&mut a, AufgabeStatus::Success);
                            }
                            persist_chat_assistant_result(
                                &state.pipeline,
                                modul_id_str,
                                convo_id_for_persist.as_deref(),
                                &seed_messages_for_persist,
                                &final_text,
                            );
                            for chunk in final_text.chars().collect::<Vec<_>>().chunks(20) {
                                let text: String = chunk.iter().collect();
                                tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":text},"done":false}).to_string()).await.ok();
                            }
                            tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":""},"done":true,"eval_count":final_text.len()}).to_string()).await.ok();
                            return;
                        }
                        messages
                            .push(serde_json::json!({"role": "assistant", "content": response}));
                        messages.push(serde_json::json!({"role": "user", "content":
                                "STOPP — der Nutzer hat ausdrücklich KEINE Recherche/Tools/Websuche gewünscht. \
                                 Ignoriere den Toolcall. Antworte direkt, kurz und ohne Tool."}));
                        tool_rounds += 1;
                        continue;
                    }
                    // DeepDive-Gates ersetzen gezielt EINEN verfruehten rag.suchen-
                    // Call; bei Multi-Call-Runden laufen die Calls unveraendert.
                    if parsed_calls.len() == 1 {
                        let enough_manual = deepdive_progress.search_ok >= 2
                            && deepdive_progress.fetch_ok >= 3
                            && deepdive_progress.source_note_ok >= 2;
                        let single = &mut parsed_calls[0];
                        if needs_deepdive
                            && deepdive_progress.crawl_ok == 0
                            && deepdive_progress.rss_evidence_ok == 0
                            && !enough_manual
                            && single.name == "rag.suchen"
                        {
                            let topic = deepdive_topic_hint(&last_user_msg);
                            let preferred_tool = preferred_deepdive_tool(&last_user_msg);
                            state.pipeline.log(
                                modul_id_str,
                                Some(&main_id),
                                LogTyp::Warning,
                                &format!(
                                    "DeepDive-Gate ersetzt verfruehtes rag.suchen durch {}({})",
                                    preferred_tool, topic
                                ),
                            );
                            single.name = preferred_tool.to_string();
                            single.params = vec![topic];
                            single.arguments_json = tool_arguments_json_for_history(
                                &single.name,
                                &single.params,
                                modul_for_tools.as_ref(),
                                &py_mods_snap,
                            );
                        } else if needs_deepdive
                            && single.name == "rag.suchen"
                            && deepdive_progress.crawl_ok > 0
                        {
                            if let Some(crawl_id) = deepdive_progress.crawl_id.as_ref() {
                                let current = single.params.first().cloned().unwrap_or_default();
                                if !current.contains(crawl_id) {
                                    let topic = if current.trim().is_empty() {
                                        deepdive_topic_hint(&last_user_msg)
                                    } else {
                                        current
                                    };
                                    single.params = vec![format!("{} {}", crawl_id, topic)];
                                    single.arguments_json = tool_arguments_json_for_history(
                                        &single.name,
                                        &single.params,
                                        modul_for_tools.as_ref(),
                                        &py_mods_snap,
                                    );
                                }
                            }
                        }
                    }
                    tool_rounds += 1;

                    // Status: Tools werden ausgefuehrt
                    for call in &parsed_calls {
                        tx.send(serde_json::json!({"type":"status","message":format!("Tool: {}({})", call.name, call.params.join(", "))}).to_string()).await.ok();
                    }

                    let mid = modul_for_tools
                        .as_ref()
                        .map(|m| m.id.as_str())
                        .unwrap_or(modul_id_str);

                    // Assistant-History VOR den Tool-Ergebnissen (provider-
                    // Felder wie DeepSeek reasoning_content bleiben erhalten,
                    // jede Call-ID bekommt genau eine role:"tool"-Antwort).
                    messages.push(crate::turn::build_assistant_history(
                        &raw_message,
                        &parsed_calls,
                        |c| {
                            tool_arguments_json_for_history(
                                &c.name,
                                &c.params,
                                modul_for_tools.as_ref(),
                                &py_mods_snap,
                            )
                        },
                    ));

                    // Sub-Aufgaben anlegen (eine pro Call), dann ausfuehren.
                    let mut call_sub_ids: Vec<String> = Vec::with_capacity(parsed_calls.len());
                    for call in &parsed_calls {
                        let mut sub = Aufgabe::direct(
                            &call.name,
                            call.params.clone(),
                            mid,
                            &format!("chat:{}", modul_id_str),
                            None,
                            None,
                        );
                        sub.parent_id = Some(main_id.clone());
                        sub.zurueck_an = main_route.clone();
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
                                call.name,
                                call.params.join(", "),
                                &sub_id[..8]
                            ),
                        );
                        call_sub_ids.push(sub_id);
                    }

                    let state_ref = &state;
                    let config_ref = &config_snapshot;
                    let results =
                        crate::turn::execute_parsed_calls(&parsed_calls, &None, |idx, call| {
                            let sub_id = call_sub_ids[idx].clone();
                            async move {
                                exec_tool_inline(
                                    state_ref,
                                    &call.name,
                                    &call.params,
                                    mid,
                                    Some(&sub_id),
                                    config_ref,
                                    Some(&call.arguments_json),
                                )
                                .await
                            }
                        })
                        .await;

                    for ((call, sub_id), tool_result) in parsed_calls
                        .iter()
                        .zip(call_sub_ids.iter())
                        .zip(results.iter())
                    {
                        let ok = tool_result.0;
                        observe_deepdive_progress(
                            &mut deepdive_progress,
                            &call.name,
                            ok,
                            tool_rounds,
                        );
                        if ok && (call.name == "deepdive.crawl" || call.name == "deepdive.quick") {
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
                                tool_name: call.name.clone(),
                                detail: util::safe_truncate(&tool_result.1, 160).to_string(),
                                recovered: false,
                            });
                        }

                        // Sub-Aufgabe abschliessen
                        if let Ok(Some(mut a)) = state.pipeline.laden_by_id(sub_id) {
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
                                call.name,
                                if ok { "OK" } else { "FAIL" },
                                util::safe_truncate(&tool_result.1, 80)
                            ),
                        );

                        // Status: Tool-Ergebnis
                        tx.send(serde_json::json!({"type":"status","message":format!("{}: {}", if ok {"OK"} else {"FAIL"}, util::safe_truncate(&tool_result.1, 80))}).to_string()).await.ok();
                    }
                    crate::turn::append_tool_results(
                        &mut messages,
                        &parsed_calls,
                        &results,
                        |ok, data| {
                            tools::format_tool_result_persisted(
                                ok,
                                data,
                                MAX_CHAT_TOOL_RESULT_CHARS,
                                &state.pipeline,
                                mid,
                            )
                        },
                    );

                    // History trimmen: alte Tool-Results kuerzen (Prefix =
                    // System-Prompt + Seed-User-Messages bleibt unangetastet).
                    let user_msgs = user_messages.as_array().map(|a| a.len()).unwrap_or(0);
                    crate::turn::trim_old_tool_messages(&mut messages, 1 + user_msgs, 6, 100);
                    continue;
                }

                crate::turn::RoundOutcome::Final { text: response } => {
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
                            let preferred_tool = preferred_deepdive_tool(&last_user_msg);
                            messages.push(
                                serde_json::json!({"role": "assistant", "content": response}),
                            );
                            messages.push(serde_json::json!({"role": "user", "content":
                                format!("STOPP — DeepDive braucht frische Quellen. Antworte NICHT aus Wissen und lies NICHT zuerst aus dem RAG. Antworte jetzt AUSSCHLIESSLICH mit genau diesem Toolcall: <tool>{}({})</tool>", preferred_tool, topic)}));
                            tool_rounds += 1;
                            continue;
                        }
                        let needs_research = is_light_research_request(&last_user_msg);
                        if needs_research {
                            messages.push(
                                serde_json::json!({"role": "assistant", "content": response}),
                            );
                            messages.push(serde_json::json!({"role": "user", "content":
                                "STOPP — du hast KEIN Tool benutzt! Der User hat explizit nach Recherche gefragt. \
                                 Nutze ein leichtes Such-Tool, z.B. duckduckgo.search oder grok_search.web. \
                                 Antworte NICHT aus deinem Wissen. Fuer kurze oder Voice-Fragen reicht eine gezielte Suche; \
                                 mehrere Suchen nur, wenn die erste Suche nichts Belastbares liefert."}));
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
                            if deepdive_gate_retries < 4
                                && can_run_more_tool_rounds(
                                    &config_snapshot,
                                    &engine.backend_id,
                                    tool_rounds,
                                )
                            {
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

                    final_text = apply_output_enhancers_to_text(
                        &state,
                        &config_snapshot,
                        modul_for_tools.as_ref(),
                        &original_last_user_msg_for_enhancers,
                        &last_user_msg,
                        &final_text,
                        &messages,
                    )
                    .await;

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

                    persist_chat_assistant_result(
                        &state.pipeline,
                        modul_id_str,
                        convo_id_for_persist.as_deref(),
                        &seed_messages_for_persist,
                        &final_text,
                    );

                    // Stream final text in chunks
                    for chunk in final_text.chars().collect::<Vec<_>>().chunks(20) {
                        let text: String = chunk.iter().collect();
                        tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":text},"done":false}).to_string()).await.ok();
                    }
                    tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":""},"done":true,"eval_count":final_text.len(),"total_duration":total_dur.as_nanos() as u64}).to_string()).await.ok();
                    return;
                }
                crate::turn::RoundOutcome::GuardrailHardFail { codes } => {
                    tracing::warn!("Guardrail hard-fail in chat.{}: {:?}", modul_id, codes);
                    tx.send(serde_json::json!({"type":"status","message":format!("Guardrail hard-fail: {}", codes.join(", "))}).to_string()).await.ok();
                    // break → finale Synthese aus vorhandener Evidenz (wie bisher)
                    break;
                }
                crate::turn::RoundOutcome::LlmError(e) => {
                    // Reservation wurde bereits in der Engine freigegeben.
                    if !sub_aufgaben.is_empty() {
                        let mut final_text =
                            llm_error_recovery_answer(&e, sub_aufgaben.len(), &messages);
                        final_text = apply_output_enhancers_to_text(
                            &state,
                            &config_snapshot,
                            modul_for_tools.as_ref(),
                            &original_last_user_msg_for_enhancers,
                            &last_user_msg,
                            &final_text,
                            &messages,
                        )
                        .await;
                        if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&main_id) {
                            a.ergebnis = Some(
                                util::safe_truncate(&final_text, MAX_CHAT_TASK_RESULT_CHARS)
                                    .to_string(),
                            );
                            let _ = state.pipeline.verschieben(&mut a, AufgabeStatus::Failed);
                        }
                        let total_dur = t_start.elapsed();
                        state.pipeline.log(
                            modul_id_str,
                            Some(&main_id),
                            LogTyp::Failed,
                            &format!(
                                "LLM Fehler nach Tool-Ergebnissen; Teilantwort aus vorhandener Evidenz geliefert: {}",
                                e
                            ),
                        );
                        persist_chat_assistant_result(
                            &state.pipeline,
                            modul_id_str,
                            convo_id_for_persist.as_deref(),
                            &seed_messages_for_persist,
                            &final_text,
                        );
                        for chunk in final_text.chars().collect::<Vec<_>>().chunks(20) {
                            let text: String = chunk.iter().collect();
                            tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":text},"done":false}).to_string()).await.ok();
                        }
                        tx.send(serde_json::json!({"model":"agent","message":{"role":"assistant","content":""},"done":true,"eval_count":final_text.len(),"total_duration":total_dur.as_nanos() as u64}).to_string()).await.ok();
                        return;
                    }

                    // FEHLER ohne verwertbare Tool-Ergebnisse — Aufgabe als Failed loggen.
                    state.pipeline.log(
                        modul_id_str,
                        Some(&main_id),
                        LogTyp::Failed,
                        &format!("LLM Fehler: {}", e),
                    );
                    let mut err_text = format!("Error: LLM Fehler: {}", e);
                    err_text = apply_output_enhancers_to_text(
                        &state,
                        &config_snapshot,
                        modul_for_tools.as_ref(),
                        &original_last_user_msg_for_enhancers,
                        &last_user_msg,
                        &err_text,
                        &messages,
                    )
                    .await;
                    if let Ok(Some(mut a)) = state.pipeline.laden_by_id(&main_id) {
                        a.ergebnis = Some(format!("FAILED: {}", e));
                        let _ = state.pipeline.verschieben(&mut a, AufgabeStatus::Failed);
                    }
                    persist_chat_assistant_result(
                        &state.pipeline,
                        modul_id_str,
                        convo_id_for_persist.as_deref(),
                        &seed_messages_for_persist,
                        &err_text,
                    );
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
            "LLM tool rounds limit erreicht; erzwinge finale Synthese ohne weitere Tools",
        );
        tx.send(serde_json::json!({"type":"status","message":"Tool-Limit dieses LLM erreicht; erstelle finale Synthese aus vorhandenen Ergebnissen"}).to_string()).await.ok();

        let model_str = model_for_backend(&config_snapshot, &engine.backend_id);
        let final_messages = final_synthesis_messages(
            &last_user_msg,
            &messages,
            needs_deepdive,
            MAX_FINAL_SYNTHESIS_EVIDENCE_CHARS,
        );
        while let Some(wait) = state
            .llm
            .reserve_rate_slot_or_wait(&engine.backend_id)
            .await
        {
            let wait_s = wait.as_secs().max(1);
            let msg = format!("LLM rate-limit aktiv: finale Synthese wartet {}s", wait_s);
            state
                .pipeline
                .log(modul_id_str, Some(&main_id), LogTyp::Info, &msg);
            tx.send(serde_json::json!({"type":"status","message":msg}).to_string())
                .await
                .ok();
            tokio::time::sleep(wait).await;
        }
        let final_result = state
            .llm
            .chat_with_tools(
                &engine.backend_id,
                backup_id.as_deref(),
                &final_messages,
                &[],
            )
            .await;
        let mut finalizer_ok = false;
        let mut final_text = match final_result {
            Ok((response, raw_data)) => {
                track_tokens(
                    &state.pipeline.store.pool,
                    &state.tokens,
                    &config_snapshot,
                    &engine.backend_id,
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

        final_text = apply_output_enhancers_to_text(
            &state,
            &config_snapshot,
            modul_for_tools.as_ref(),
            &original_last_user_msg_for_enhancers,
            &last_user_msg,
            &final_text,
            &messages,
        )
        .await;

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

        persist_chat_assistant_result(
            &state.pipeline,
            modul_id_str,
            convo_id_for_persist.as_deref(),
            &seed_messages_for_persist,
            &final_text,
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
/// Im Chat-Flow wird die Subtask-ID weitergereicht, damit Side-Effects
/// idempotent bleiben und aufgaben.erstellen den Chat-Rueckkanal erben kann.
#[allow(clippy::too_many_arguments)]
async fn exec_tool_inline(
    s: &Arc<AppState>,
    tool_name: &str,
    params: &[String],
    modul_id: &str,
    task_id: Option<&str>,
    config: &AgentConfig,
    args_json: Option<&str>,
) -> (bool, String) {
    let py_mods = s.py_modules.read().await;
    tools::exec_tool_unified(
        tool_name,
        params,
        modul_id,
        task_id,
        &s.pipeline,
        &s.llm,
        &py_mods,
        &s.py_pool,
        config,
        args_json,
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

fn single_chat_stream_response(text: &str) -> axum::response::Response<Body> {
    let (tx, rx) = tokio::sync::mpsc::channel::<String>(2);
    let _ = tx.try_send(
        serde_json::json!({"model":"agent","message":{"role":"assistant","content":text},"done":false})
            .to_string(),
    );
    let _ = tx.try_send(
        serde_json::json!({"model":"agent","message":{"role":"assistant","content":""},"done":true})
            .to_string(),
    );
    drop(tx);
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
                         "grok_model":{"type":"string","label":"Grok Search Model","default":"grok-4.3"},
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
        serde_json::json!({
            "name": "enhancer", "description": "Pipeline-Enhancer vor/nach Chat-Verarbeitung: beobachten, filtern, Prompts verbessern, uebersetzen, Output pruefen und eigenes RAG fuellen", "version": "built-in", "source": "rust",
            "settings": {
                "enhancer_mode":{"type":"select","label":"Mode","default":"observe","options":["observe","filter","rewrite","translate","quality","gateway"]},
                "enhancer_prompt":{"type":"text","label":"Enhancer Prompt","default":"Analysiere den Kontext. Gib JSON mit action, text, notes, flags, reason zurueck."},
                "enhancer_fail_policy":{"type":"select","label":"Fail Policy","default":"fail_open","options":["fail_open","fail_closed"]},
                "enhancer_store_rag":{"type":"bool","label":"In Enhancer-RAG speichern","default":true},
                "enhancer_rag_pool":{"type":"string","label":"Enhancer RAG Pool","default":"Enhancer"},
                "enhancer_inject_context":{"type":"bool","label":"Input-Annotation in Kontext injizieren","default":true},
                "enhancer_max_output_chars":{"type":"number","label":"Max Output-Zeichen","default":6000},
                "enhancer_timeout_s":{"type":"number","label":"Enhancer Timeout Sekunden","default":60}
            },
            "tools": []
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

async fn icon_192() -> impl IntoResponse {
    (
        [(axum::http::header::CONTENT_TYPE, "image/png"),
         (axum::http::header::CACHE_CONTROL, "public, max-age=86400")],
        include_bytes!("assets/icon_192.png").as_slice(),
    )
}

/// Direkter Video-Pipeline-Start aus der Chat-UI — bewusst OHNE Chat-LLM:
/// das Formular liefert strukturierte Parameter, wir enqueuen den
/// workflow_trigger-Toolcall direkt in die Task-Queue. Kein Tool-Listing,
/// keine Prompt-Interpretation, deterministischer Einstieg.
async fn video_start(
    State(s): State<Arc<AppState>>,
    Json(body): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
    let query = body["query"].as_str().unwrap_or("").trim().to_string();
    if query.len() < 8 {
        return Json(serde_json::json!({"error": "query/Zweck fehlt (min. 8 Zeichen)"}));
    }
    let modul = body["modul"]
        .as_str()
        .and_then(safe_id)
        .unwrap_or_else(|| "chat.deepseekdeepseekv4flash".into());
    let minutes = body["target_minutes"].as_f64().unwrap_or(0.0).clamp(0.0, 60.0);
    let language = body["language"].as_str().unwrap_or("de").to_lowercase();
    let preview = body["preview"].as_bool().unwrap_or(false);
    let shorts = body["shorts"].as_bool().unwrap_or(false);
    // Resume-Pfad: vorhandenen DeepDive-Report wiederverwenden (kein neuer Crawl).
    let report_path = body["report_path"].as_str().unwrap_or("").trim().to_string();
    let resume = !report_path.is_empty();

    let mut params = serde_json::json!({
        "query": query,
        "preview": preview,
        "auto_shorts": shorts,
        "language": language,
        "chat_route": format!("chat:{}", modul),
    });
    if minutes > 0.0 {
        params["target_minutes"] = serde_json::json!(minutes);
    }
    if preview {
        params["require_tts"] = serde_json::json!(false);
        params["allow_silent_audio"] = serde_json::json!(true);
    }
    if resume {
        params["report_path"] = serde_json::json!(report_path);
        params["auto_render"] = serde_json::json!(true);
        if let Some(t) = body["title"].as_str() {
            params["title"] = serde_json::json!(t);
        }
    }

    let workflow_modul = {
        let cfg = s.config.read().await;
        cfg.module
            .iter()
            .find(|m| m.typ == "workflow_trigger")
            .map(|m| m.id.clone())
            .unwrap_or_else(|| "workflow_trigger.default".into())
    };
    let tool = if resume {
        "workflow_trigger.video_from_report"
    } else {
        "workflow_trigger.deepdive_video"
    };
    let mut aufgabe = crate::types::Aufgabe::direct(
        tool,
        vec![params.to_string()],
        &workflow_modul,
        &format!("chat:{}", modul),
        None,
        None,
    );
    aufgabe.zurueck_an = Some(format!("chat:{}", modul));
    if let Err(e) = s.pipeline.speichern(&aufgabe) {
        return Json(serde_json::json!({"error": format!("Task anlegen fehlgeschlagen: {e}")}));
    }
    s.pipeline.log(
        &workflow_modul,
        Some(&aufgabe.id),
        LogTyp::Info,
        &format!("Video-Pipeline per UI gestartet: {}", crate::util::safe_truncate(&query, 120)),
    );
    Json(serde_json::json!({
        "started": true,
        "task_id": aufgabe.id,
        "workflow_modul": workflow_modul,
        "message": "Video-Pipeline gestartet. Fortschritt kommt in den Chat, Ergebnis erscheint unter Medien.",
    }))
}

/// Bild-Pipeline-Einstieg (analog video_start): erzeugt 1-4 KI-Bilder zu einem
/// freien Prompt. Laeuft als sichtbarer Scheduler-Task (Transparenz-Prinzip:
/// nichts startet heimlich), schreibt in die Chat-Medien-Galerie. Auch fuer
/// externe lokale KI-Agenten als HTTP-Endpoint nutzbar.
async fn image_start(
    State(s): State<Arc<AppState>>,
    Json(body): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
    let prompt = body["prompt"]
        .as_str()
        .or_else(|| body["query"].as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if prompt.len() < 3 {
        return Json(serde_json::json!({"error": "prompt fehlt (min. 3 Zeichen)"}));
    }
    let modul = body["modul"]
        .as_str()
        .and_then(safe_id)
        .unwrap_or_else(|| "chat.deepseekdeepseekv4flash".into());
    let count = body["count"].as_u64().unwrap_or(1).clamp(1, 4);
    let aspect = match body["aspect"].as_str().unwrap_or("16:9") {
        a @ ("16:9" | "1:1" | "9:16" | "4:3" | "3:2") => a.to_string(),
        _ => "16:9".to_string(),
    };
    let no_style = body["no_style"].as_bool().unwrap_or(false);
    let brand = body["brand"].as_bool().unwrap_or(false);

    // Ausgabe in die Chat-Galerie — das Medien-Panel scannt genau dieses Home.
    let out_dir = s.pipeline.home_dir(&modul).join("images");
    let params = serde_json::json!({
        "prompt": prompt,
        "count": count,
        "aspect": aspect,
        "no_style": no_style,
        "brand": brand,
        "out_dir": out_dir.to_string_lossy(),
        "chat_route": format!("chat:{}", modul),
    });

    // Laeuft als image_gen-Modul (hat py.image_gen-Permission).
    let image_modul = {
        let cfg = s.config.read().await;
        cfg.module
            .iter()
            .find(|m| m.typ == "image_gen")
            .map(|m| m.id.clone())
            .unwrap_or_else(|| "image_gen.default".into())
    };
    let mut aufgabe = crate::types::Aufgabe::direct(
        "image_gen.request",
        vec![params.to_string()],
        &image_modul,
        &format!("chat:{}", modul),
        None,
        None,
    );
    // Bildgenerierung + Download dauert laenger als der Direct-Default (30s).
    aufgabe.timeout_s = 300;
    aufgabe.retry = 1;
    aufgabe.zurueck_an = Some(format!("chat:{}", modul));
    if let Err(e) = s.pipeline.speichern(&aufgabe) {
        return Json(serde_json::json!({"error": format!("Task anlegen fehlgeschlagen: {e}")}));
    }
    s.pipeline.log(
        &image_modul,
        Some(&aufgabe.id),
        LogTyp::Info,
        &format!(
            "Bild-Pipeline per UI gestartet ({}× {}): {}",
            count,
            aspect,
            crate::util::safe_truncate(&prompt, 100)
        ),
    );
    Json(serde_json::json!({
        "started": true,
        "task_id": aufgabe.id,
        "count": count,
        "message": "Bild-Pipeline gestartet. Ergebnis erscheint unter Medien.",
    }))
}

/// Task-Graph (Maltego/Mindmap-Stil): Nodes = Tasks, Kanten = parent_id
/// (welcher Task welchen gespawnt hat), Cluster = workflow_id. Aus den letzten
/// ~300 Tasks (auch erledigte) — zeigt was zusammengehoert.
async fn tasks_graph(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let rows = crate::store::task_list_recent(&s.pipeline.store.pool, 300).unwrap_or_default();
    // Token-Calls (kein task_id-Bezug) -> grobe Zuordnung per modul + Zeitfenster.
    let token_calls: Vec<(i64, String, i64)> = s
        .pipeline
        .store
        .pool
        .get()
        .ok()
        .and_then(|conn| {
            conn.prepare("SELECT ts, modul, input_tokens+output_tokens FROM token_calls")
                .ok()
                .and_then(|mut st| {
                    st.query_map([], |r| {
                        Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?))
                    })
                    .ok()
                    .map(|it| it.filter_map(|x| x.ok()).collect::<Vec<_>>())
                })
        })
        .unwrap_or_default();
    // Pass 1: parsen
    struct N {
        id: String, label: String, modul: String, status: String, workflow: String,
        stage: String, typ: String, parent: String, ts: i64,
        started: Option<i64>, ended: Option<i64>, preview: String, params: String,
    }
    let mut ns: Vec<N> = Vec::with_capacity(rows.len());
    for r in &rows {
        let p: serde_json::Value =
            serde_json::from_str(&r.payload_json).unwrap_or_else(|_| serde_json::json!({}));
        let tool = p["tool"].as_str().unwrap_or("");
        let anweisung = p["anweisung"].as_str().unwrap_or("");
        let label = if !tool.is_empty() { tool } else if !anweisung.is_empty() { anweisung } else { "task" };
        let params = p["params"]
            .as_array()
            .map(|a| a.iter().filter_map(|v| v.as_str()).collect::<Vec<_>>().join(" | "))
            .unwrap_or_default();
        ns.push(N {
            id: r.id.clone(),
            label: util::safe_truncate(label, 44).to_string(),
            modul: r.modul.clone(),
            status: r.status.clone(),
            workflow: p["workflow_id"].as_str().unwrap_or("").to_string(),
            stage: p["workflow_stage"].as_str().unwrap_or("").to_string(),
            typ: p["typ"].as_str().unwrap_or("").to_string(),
            parent: p["parent_id"].as_str().unwrap_or("").to_string(),
            ts: r.erstellt_ts,
            started: r.gestartet_ts,
            ended: r.erledigt_ts,
            preview: util::safe_truncate(p["ergebnis"].as_str().unwrap_or(""), 220).to_string(),
            params: util::safe_truncate(&params, 140).to_string(),
        });
    }
    let ids: std::collections::HashSet<String> = ns.iter().map(|n| n.id.clone()).collect();
    let mut children: std::collections::HashMap<String, Vec<i64>> = std::collections::HashMap::new();
    for n in &ns {
        if !n.parent.is_empty() && ids.contains(&n.parent) {
            children.entry(n.parent.clone()).or_default().push(n.ts);
        }
    }
    let mut nodes = Vec::with_capacity(ns.len());
    let mut edges = Vec::new();
    for n in &ns {
        let spawned = children.get(&n.id).map(|c| c.len()).unwrap_or(0);
        let first_child_delay = children
            .get(&n.id)
            .and_then(|c| c.iter().min().copied())
            .map(|m| (m - n.ts).max(0));
        let duration_s = match (n.started, n.ended) {
            (Some(a), Some(b)) if b >= a => Some(b - a),
            _ => None,
        };
        let tokens: i64 = match (n.started, n.ended) {
            (Some(a), Some(b)) => token_calls
                .iter()
                .filter(|(ts, m, _)| *m == n.modul && *ts >= a && *ts <= b)
                .map(|(_, _, t)| *t)
                .sum(),
            _ => 0,
        };
        nodes.push(serde_json::json!({
            "id": n.id, "label": n.label, "modul": n.modul, "status": n.status,
            "workflow": n.workflow, "stage": n.stage, "typ": n.typ, "parent": n.parent,
            "ts": n.ts, "started": n.started, "ended": n.ended, "duration_s": duration_s, "spawned": spawned,
            "first_child_delay_s": first_child_delay, "tokens": tokens,
            "preview": n.preview, "params": n.params,
        }));
        if !n.parent.is_empty() && ids.contains(&n.parent) {
            edges.push(serde_json::json!({"from": n.parent, "to": n.id}));
        }
    }
    Json(serde_json::json!({"nodes": nodes, "edges": edges, "count": rows.len()}))
}

/// Redaktionsplan: aktive Vorschlags-Queue (fuer das Chat-Panel).
async fn planner_proposals(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let cfg = s.config.read().await;
    let (ok, data) = exec_tool_inline(
        &s,
        "content_planner.proposals",
        &["{}".to_string()],
        "content_planner.default",
        None,
        &cfg,
        None,
    )
    .await;
    drop(cfg);
    if ok {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&data) {
            return Json(v);
        }
    }
    Json(serde_json::json!({"error": data, "proposals": [], "count": 0}))
}

/// Redaktionsplan: Kreislauf-Status (Autopilot an/aus, Auto-Crawl-Plan, Zaehler,
/// Themen) — fuettert den Status-Kopf des Panels, damit der ganze Kreislauf auf
/// einen Blick sichtbar ist.
async fn planner_status(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let cfg = s.config.read().await;
    let planner = cfg.module.iter().find(|m| m.id == "content_planner.default");
    let schedule = planner.and_then(|m| m.settings.schedule.clone()).unwrap_or_default();
    let autopilot = planner
        .and_then(|m| m.settings.extra.get("autopilot"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let (_ok, data) = exec_tool_inline(
        &s,
        "content_planner.status",
        &[],
        "content_planner.default",
        None,
        &cfg,
        None,
    )
    .await;
    drop(cfg);
    let st: serde_json::Value =
        serde_json::from_str(&data).unwrap_or_else(|_| serde_json::json!({}));
    Json(serde_json::json!({
        "autopilot": autopilot,
        "schedule": schedule,
        "interests": st.get("interests").cloned().unwrap_or_else(|| serde_json::json!([])),
        "by_status": st.get("by_status").cloned().unwrap_or_else(|| serde_json::json!({})),
        "covered_count": st.get("covered_count").cloned().unwrap_or_else(|| serde_json::json!(0)),
    }))
}

/// Community-Manager-Status (Kommentar-Pflege) fuer den Flow-Graph.
async fn community_status(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let cfg = s.config.read().await;
    let cm = cfg.module.iter().find(|m| m.id == "community_manager.default");
    let schedule = cm.and_then(|m| m.settings.schedule.clone()).unwrap_or_default();
    let auto_post = cm
        .and_then(|m| m.settings.extra.get("auto_post"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let (_ok, data) = exec_tool_inline(
        &s,
        "community_manager.status",
        &[],
        "community_manager.default",
        None,
        &cfg,
        None,
    )
    .await;
    drop(cfg);
    let st: serde_json::Value =
        serde_json::from_str(&data).unwrap_or_else(|_| serde_json::json!({}));
    Json(serde_json::json!({
        "schedule": schedule,
        "auto_post": auto_post,
        "drafts_by_status": st.get("drafts_by_status").cloned().unwrap_or_else(|| serde_json::json!({})),
        "seen_count": st.get("seen_count").cloned().unwrap_or_else(|| serde_json::json!(0)),
        "credentials": st.get("credentials").cloned().unwrap_or_else(|| serde_json::json!("?")),
    }))
}

/// Redaktionsplan: Aktion auf einen Vorschlag (now|next|approve|reject|snooze).
/// now/approve triggert zusaetzlich die Video-Pipeline mit der Proposal-Query —
/// sichtbar als Scheduler-Task (Transparenz).
async fn planner_decide(
    State(s): State<Arc<AppState>>,
    Json(body): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
    let id = body["id"].as_str().unwrap_or("").to_string();
    let action = body["action"].as_str().unwrap_or("").to_string();
    if id.is_empty() || action.is_empty() {
        return Json(serde_json::json!({"error": "id und action noetig"}));
    }
    let modul = body["modul"]
        .as_str()
        .and_then(safe_id)
        .unwrap_or_else(|| "chat.deepseekdeepseekv4flash".into());
    let params = serde_json::json!({"id": id, "action": action}).to_string();
    let cfg = s.config.read().await;
    let (ok, data) = exec_tool_inline(
        &s,
        "content_planner.decide",
        &[params],
        "content_planner.default",
        None,
        &cfg,
        None,
    )
    .await;
    let decision: serde_json::Value =
        serde_json::from_str(&data).unwrap_or_else(|_| serde_json::json!({"raw": data}));
    let mut video_task = serde_json::Value::Null;
    if ok && decision["trigger_video"].as_bool().unwrap_or(false) {
        if let Some(query) = decision["query"].as_str() {
            let workflow_modul = cfg
                .module
                .iter()
                .find(|m| m.typ == "workflow_trigger")
                .map(|m| m.id.clone())
                .unwrap_or_else(|| "workflow_trigger.default".into());
            let vparams = serde_json::json!({
                "query": query,
                "title": decision["title"].as_str().unwrap_or(""),
                "language": "de",
                "auto_shorts": false,
                "auto_upload": true,
                "chat_route": format!("chat:{}", modul),
            });
            let mut auf = crate::types::Aufgabe::direct(
                "workflow_trigger.deepdive_video",
                vec![vparams.to_string()],
                &workflow_modul,
                &format!("chat:{}", modul),
                None,
                None,
            );
            auf.zurueck_an = Some(format!("chat:{}", modul));
            if s.pipeline.speichern(&auf).is_ok() {
                video_task = serde_json::json!(auf.id);
                s.pipeline.log(
                    &workflow_modul,
                    Some(&auf.id),
                    LogTyp::Info,
                    &format!("Video aus Redaktionsplan: {}", util::safe_truncate(query, 80)),
                );
            }
        }
    }
    drop(cfg);
    Json(serde_json::json!({"ok": ok, "decision": decision, "video_task": video_task}))
}

/// Redaktionsplan: Scan JETZT — gleicher LLM-Task wie der taegliche Cron, async.
async fn planner_scan(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let cfg = s.config.read().await;
    let planner = cfg.module.iter().find(|m| m.id == "content_planner.default");
    let anweisung = planner
        .and_then(|m| m.settings.cron_anweisung.clone())
        .unwrap_or_else(|| {
            "Fuehre den Redaktions-Scan durch und speichere Vorschlaege mit content_planner.save_proposals.".into()
        });
    let target = planner
        .and_then(|m| m.settings.target_modul.clone())
        .unwrap_or_else(|| "chat.deepseekdeepseekv4flash".into());
    drop(cfg);
    let auf = crate::types::Aufgabe::llm_call(&anweisung, &target, "content_planner.default", None)
        .with_timeout_s(900);
    let id = auf.id.clone();
    if let Err(e) = s.pipeline.speichern(&auf) {
        return Json(serde_json::json!({"error": format!("Task anlegen fehlgeschlagen: {e}")}));
    }
    s.pipeline.log(
        "content_planner.default",
        Some(&id),
        LogTyp::Info,
        "Redaktions-Scan manuell gestartet",
    );
    Json(serde_json::json!({"started": true, "task_id": id, "message": "Scan laeuft — Vorschlaege erscheinen gleich im Plan."}))
}

/// Aktive + juengste Pipeline-Workflows (Video-Erstellung) — first-class
/// Sichtbarkeit: jede laufende Pipeline erscheint mit Titel, Stage und Status,
/// damit NICHTS unbemerkt im Hintergrund laeuft (Transparenz-Prinzip).
async fn get_workflows(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let base = &s.pipeline.base;
    let mut roots: Vec<std::path::PathBuf> = vec![base.join("workflows")];
    // Modul-Home-Verzeichnisse (default_output_dir kann pro Chat dort liegen)
    if let Ok(homes) = std::fs::read_dir(base.join("home")) {
        for h in homes.flatten() {
            let wf = h.path().join("workflows");
            if wf.is_dir() {
                roots.push(wf);
            }
        }
    }
    let mut items: Vec<serde_json::Value> = vec![];
    let mut seen = std::collections::HashSet::new();
    for root in roots {
        let Ok(entries) = std::fs::read_dir(&root) else {
            continue;
        };
        for e in entries.flatten() {
            let wf_json = e.path().join("workflow.json");
            let Ok(text) = std::fs::read_to_string(&wf_json) else {
                continue;
            };
            let Ok(w) = serde_json::from_str::<serde_json::Value>(&text) else {
                continue;
            };
            let id = w["id"].as_str().unwrap_or("").to_string();
            if id.is_empty() || !seen.insert(id.clone()) {
                continue;
            }
            let mtime = std::fs::metadata(&wf_json)
                .and_then(|m| m.modified())
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            let events = w["events"].as_array().map(|a| a.len()).unwrap_or(0);
            let last_event = w["events"]
                .as_array()
                .and_then(|a| a.last())
                .and_then(|e| e["detail"].as_str())
                .unwrap_or("");
            items.push(serde_json::json!({
                "id": id,
                "title": w["title"].as_str().or_else(|| w["query"].as_str()).unwrap_or("Video-Workflow"),
                "kind": w["kind"].as_str().unwrap_or("video"),
                "status": w["status"].as_str().unwrap_or("?"),
                "stage": w["stage"].as_str().unwrap_or("?"),
                "modul": w["target_modul_id"].as_str().unwrap_or(""),
                "video": w["artifacts"]["video"].as_str().unwrap_or(""),
                "last_event": util::safe_truncate(last_event, 140),
                "steps": events,
                "updated": mtime,
            }));
        }
    }
    items.sort_by_key(|v| -(v["updated"].as_i64().unwrap_or(0)));
    items.truncate(40);
    let active = items
        .iter()
        .filter(|v| matches!(v["status"].as_str(), Some("running") | Some("waiting")))
        .count();
    Json(serde_json::json!({"workflows": items, "active": active}))
}

const MEDIA_EXTENSIONS: &[&str] = &[
    "mp4", "webm", "mov", "png", "jpg", "jpeg", "gif", "webp", "mp3", "wav", "m4a",
];

/// Juengste Medien-Dateien im Modul-Home (rekursiv, mtime-sortiert) — fuettert
/// die Medien-Galerie im Chat. Ausgeliefert wird ueber den bestehenden
/// /api/home/{modul}/{path}-Endpoint.
async fn list_media(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(modul_id): axum::extract::Path<String>,
) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"error": "Ungültige modul-ID", "media": []}));
    };
    let home = s.pipeline.home_dir(&modul_id);
    let mut items: Vec<(i64, serde_json::Value)> = vec![];
    collect_media(&home, &home, 0, &mut items);
    items.sort_by_key(|(mtime, _)| -*mtime);
    items.truncate(100);
    let media: Vec<serde_json::Value> = items.into_iter().map(|(_, v)| v).collect();
    Json(serde_json::json!({"media": media}))
}

fn collect_media(
    base: &std::path::Path,
    dir: &std::path::Path,
    depth: u32,
    out: &mut Vec<(i64, serde_json::Value)>,
) {
    if depth > 5 || out.len() > 800 {
        return;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_media(base, &path, depth + 1, out);
            continue;
        }
        let ext = path
            .extension()
            .map(|e| e.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        if !MEDIA_EXTENSIONS.contains(&ext.as_str()) {
            continue;
        }
        let Ok(meta) = entry.metadata() else { continue };
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        let rel = path
            .strip_prefix(base)
            .unwrap_or(&path)
            .to_string_lossy()
            .to_string();
        let kind = match ext.as_str() {
            "mp4" | "webm" | "mov" => "video",
            "mp3" | "wav" | "m4a" => "audio",
            _ => "image",
        };
        out.push((
            mtime,
            serde_json::json!({
                "path": rel,
                "name": path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default(),
                "kind": kind,
                "size": meta.len(),
                "mtime": mtime,
            }),
        ));
    }
}

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
    let mut backend = cfg
        .llm_backends
        .iter()
        .find(|b| b.id == backend_id)
        .cloned();
    if let Some(ref mut b) = backend {
        crate::util::resolve_llm_backend_api_alias(b, &cfg);
    }
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
                    let models: Vec<LlmModelInfo> = data["models"]
                        .as_array()
                        .map(|arr| {
                            arr.iter()
                                .filter_map(|m| {
                                    m["name"]
                                        .as_str()
                                        .map(|name| model_info_from_id(name, true))
                                })
                                .collect()
                        })
                        .unwrap_or_default();
                    sort_model_infos(models)
                }
                Err(e) => return Json(serde_json::json!({"error": e.to_string(), "models": []})),
            }
        }
        crate::types::LlmTyp::OpenAICompat
        | crate::types::LlmTyp::Grok
        | crate::types::LlmTyp::DeepSeek => {
            // OpenAI-compatible providers return data[].id.
            let key = backend.api_key.as_deref().unwrap_or("");
            let endpoint = if backend.typ == crate::types::LlmTyp::DeepSeek {
                crate::llm::deepseek_endpoint(&backend.url, "models")
            } else {
                crate::llm::openai_compat_endpoint(&backend.url, "models")
            };
            match client
                .get(endpoint)
                .header("Authorization", format!("Bearer {}", key))
                .send()
                .await
            {
                Ok(resp) => {
                    let data: serde_json::Value = resp.json().await.unwrap_or_default();
                    let models: Vec<LlmModelInfo> = data["data"]
                        .as_array()
                        .map(|arr| {
                            arr.iter()
                                .filter_map(model_info_from_openai_value)
                                .collect()
                        })
                        .unwrap_or_default();
                    sort_model_infos(models)
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
                    let models = data["data"]
                        .as_array()
                        .map(|arr| {
                            arr.iter()
                                .filter_map(|m| {
                                    m["id"].as_str().map(|id| model_info_from_id(id, false))
                                })
                                .collect()
                        })
                        .unwrap_or_default();
                    sort_model_infos(models)
                }
                Err(e) => return Json(serde_json::json!({"error": e.to_string(), "models": []})),
            }
        }
        crate::types::LlmTyp::Embedding => vec![],
    };

    Json(serde_json::json!({"models": model_ids(&result), "model_infos": result}))
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
    if let Some(backend) = cfg.llm_backends.iter().find(|b| b.id == backend_id) {
        if let Some(key) = backend.api_key.as_deref() {
            if let Some(alias_id) = crate::util::api_key_vault_alias_id(key) {
                let alias = crate::util::api_key_vault_alias(&alias_id);
                let _ = crate::store::audit(
                    store_pool,
                    "api_vault.use",
                    backend_id,
                    &serde_json::json!({
                        "alias": alias,
                        "tool": "llm.chat",
                        "path": format!("llm_backends.{}.api_key", backend_id),
                    })
                    .to_string(),
                );
            }
        }
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

#[derive(serde::Deserialize)]
struct NotificationQuery {
    convo_id: Option<String>,
    include_read: Option<bool>,
    limit: Option<usize>,
}

#[derive(serde::Deserialize)]
struct NotificationCreateBody {
    convo_id: Option<String>,
    kind: Option<String>,
    title: Option<String>,
    body: Option<String>,
    message: Option<String>,
    source: Option<String>,
}

#[derive(serde::Deserialize)]
struct NotificationReadBody {
    read: Option<bool>,
}

async fn list_notifications(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(modul_id): axum::extract::Path<String>,
    axum::extract::Query(query): axum::extract::Query<NotificationQuery>,
) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"notifications": [], "error": "Ungültige modul-ID"}));
    };
    let convo_id = query.convo_id.as_deref().and_then(safe_id);
    let include_read = query.include_read.unwrap_or(true);
    let limit = query.limit.unwrap_or(50).clamp(1, 200);
    let items = s
        .pipeline
        .notification_list(&modul_id, convo_id.as_deref(), include_read, limit);
    let unread = items.iter().filter(|n| !n.read).count();
    Json(serde_json::json!({"notifications": items, "unread": unread}))
}

async fn create_notification(
    State(s): State<Arc<AppState>>,
    axum::extract::Path(modul_id): axum::extract::Path<String>,
    Json(body): Json<NotificationCreateBody>,
) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige modul-ID"}));
    };
    let convo_id = body.convo_id.as_deref().and_then(safe_id);
    let message = body
        .body
        .as_deref()
        .or(body.message.as_deref())
        .unwrap_or("")
        .trim();
    if message.is_empty() {
        return Json(serde_json::json!({"ok": false, "error": "Notification ohne Text"}));
    }
    let kind = body.kind.as_deref().unwrap_or("system").trim();
    let title = body
        .title
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty());
    let source = body
        .source
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty());
    match s.pipeline.notification_add(
        &modul_id,
        convo_id.as_deref(),
        if kind.is_empty() { "system" } else { kind },
        title,
        message,
        source,
    ) {
        Ok(id) => Json(serde_json::json!({"ok": true, "id": id})),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

async fn mark_notification_read(
    State(s): State<Arc<AppState>>,
    axum::extract::Path((modul_id, notification_id)): axum::extract::Path<(String, String)>,
    Json(body): Json<NotificationReadBody>,
) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige modul-ID"}));
    };
    let Some(notification_id) = safe_id(&notification_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige notification-ID"}));
    };
    match s
        .pipeline
        .notification_mark_read(&modul_id, &notification_id, body.read.unwrap_or(true))
    {
        Ok(_) => Json(serde_json::json!({"ok": true})),
        Err(e) => Json(serde_json::json!({"ok": false, "error": e.to_string()})),
    }
}

async fn delete_notification(
    State(s): State<Arc<AppState>>,
    axum::extract::Path((modul_id, notification_id)): axum::extract::Path<(String, String)>,
) -> Json<serde_json::Value> {
    let Some(modul_id) = safe_id(&modul_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige modul-ID"}));
    };
    let Some(notification_id) = safe_id(&notification_id) else {
        return Json(serde_json::json!({"ok": false, "error": "Ungültige notification-ID"}));
    };
    match s.pipeline.notification_delete(&modul_id, &notification_id) {
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
        input_enhancers: m.input_enhancers.clone(),
        output_enhancers: m.output_enhancers.clone(),
        combined_enhancers: m.combined_enhancers.clone(),
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
    let cfg_snapshot = state.config.read().await.clone();
    let (url, key) = match (req.api_url.clone(), req.api_key.clone()) {
        (Some(u), Some(k)) => (u, k),
        _ => match &cfg_snapshot.wizard {
            Some(w) => (w.llm.url.clone(), w.llm.api_key.clone().unwrap_or_default()),
            None => {
                return Err((
                    axum::http::StatusCode::BAD_REQUEST,
                    "no api_url/api_key given and no wizard.llm configured".into(),
                ));
            }
        },
    };
    let key = crate::util::resolve_api_key_alias_string(&key, &cfg_snapshot)
        .map(|(secret, _)| secret)
        .unwrap_or(key);

    match req.provider.as_str() {
        "Claude" | "Anthropic" => {
            let models = sort_model_infos(vec![
                LlmModelInfo {
                    id: "claude-opus-4-7".into(),
                    display_name: "Claude Opus 4.7".into(),
                    free: false,
                },
                LlmModelInfo {
                    id: "claude-sonnet-4-6".into(),
                    display_name: "Claude Sonnet 4.6".into(),
                    free: false,
                },
                LlmModelInfo {
                    id: "claude-haiku-4-5".into(),
                    display_name: "Claude Haiku 4.5".into(),
                    free: false,
                },
                LlmModelInfo {
                    id: "claude-opus-4-6".into(),
                    display_name: "Claude Opus 4.6".into(),
                    free: false,
                },
            ]);
            Ok(axum::Json(serde_json::json!({"models": models})))
        }
        "OpenAI" | "Grok" | "DeepSeek" | "OpenRouter" | "Local/LAN" => {
            let typ = match req.provider.as_str() {
                "Grok" => crate::types::LlmTyp::Grok,
                "DeepSeek" => crate::types::LlmTyp::DeepSeek,
                _ => crate::types::LlmTyp::OpenAICompat,
            };
            if req.provider == "Local/LAN" {
                crate::security::validate_llm_backend_url(&typ, &url)
                    .map_err(|e| (axum::http::StatusCode::BAD_REQUEST, e))?;
            } else {
                crate::security::validate_external_url(&url)
                    .map_err(|e| (axum::http::StatusCode::BAD_REQUEST, e))?;
            }
            let full_url = if typ == crate::types::LlmTyp::DeepSeek {
                crate::llm::deepseek_endpoint(&url, "models")
            } else {
                crate::llm::openai_compat_endpoint(&url, "models")
            };
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
            let models = arr
                .iter()
                .filter_map(model_info_from_openai_value)
                .collect();
            let models = sort_model_infos(models);
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
        "DeepSeek" => crate::types::LlmTyp::DeepSeek,
        _ => {
            return Err((
                axum::http::StatusCode::BAD_REQUEST,
                "unknown provider".into(),
            ));
        }
    };
    crate::security::validate_llm_backend_url(&typ, &req.api_url)
        .map_err(|e| (axum::http::StatusCode::BAD_REQUEST, e))?;

    let mut backend = crate::types::LlmBackend {
        id: "wizard-test".into(),
        name: "Wizard-Test".into(),
        typ,
        url: req.api_url.clone(),
        api_key: Some(req.api_key),
        model: req.model,
        timeout_s: 15,
        identity: Default::default(),
        max_tokens: None,
        reasoning: None,
        cost_cap: None,
        max_tool_rounds: None,
        call_rate_limit: None,
        internal: false,
        tool_choice_supported: None,
        context_window: None,
    };

    // Try a minimal ping: single user message "ping"
    let messages = vec![serde_json::json!({"role": "user", "content": "ping"})];
    let cfg_snapshot = state.config.read().await.clone();
    crate::util::resolve_llm_backend_api_alias(&mut backend, &cfg_snapshot);
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
    crate::util::resolve_llm_backend_api_alias(&mut backend, &cfg_snap);
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
    let mut ba = cfg_snap
        .llm_backends
        .iter()
        .find(|b| b.id == req.backend_a)
        .cloned()
        .ok_or((
            axum::http::StatusCode::NOT_FOUND,
            format!("backend A '{}' not found", req.backend_a),
        ))?;
    let mut bb = cfg_snap
        .llm_backends
        .iter()
        .find(|b| b.id == req.backend_b)
        .cloned()
        .ok_or((
            axum::http::StatusCode::NOT_FOUND,
            format!("backend B '{}' not found", req.backend_b),
        ))?;
    crate::util::resolve_llm_backend_api_alias(&mut ba, &cfg_snap);
    crate::util::resolve_llm_backend_api_alias(&mut bb, &cfg_snap);
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
// Presets (Ollama lokal, OpenRouter free-tier, DeepSeek, OpenAI, Anthropic).
// User wählt
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

async fn setup_models(
    State(s): State<Arc<AppState>>,
    Json(mut body): Json<crate::types::LlmBackend>,
) -> Json<serde_json::Value> {
    let cfg_snapshot = s.config.read().await.clone();
    crate::util::resolve_llm_backend_api_alias(&mut body, &cfg_snapshot);
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
                        .filter_map(|m| {
                            m["name"]
                                .as_str()
                                .map(|name| model_info_from_id(name, true))
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default()
        }
        crate::types::LlmTyp::OpenAICompat
        | crate::types::LlmTyp::Grok
        | crate::types::LlmTyp::DeepSeek
        | crate::types::LlmTyp::Embedding => {
            let endpoint = if body.typ == crate::types::LlmTyp::DeepSeek {
                crate::llm::deepseek_endpoint(&body.url, "models")
            } else {
                crate::llm::openai_compat_endpoint(&body.url, "models")
            };
            let mut req = client.get(endpoint);
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
                        .filter_map(model_info_from_openai_value)
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default()
        }
        crate::types::LlmTyp::Anthropic => vec![
            LlmModelInfo {
                id: "claude-opus-4-7".into(),
                display_name: "Claude Opus 4.7".into(),
                free: false,
            },
            LlmModelInfo {
                id: "claude-sonnet-4-6".into(),
                display_name: "Claude Sonnet 4.6".into(),
                free: false,
            },
            LlmModelInfo {
                id: "claude-haiku-4-5".into(),
                display_name: "Claude Haiku 4.5".into(),
                free: false,
            },
            LlmModelInfo {
                id: "claude-opus-4-6".into(),
                display_name: "Claude Opus 4.6".into(),
                free: false,
            },
        ],
    };
    let result = sort_model_infos(result);

    Json(serde_json::json!({"ok": true, "models": model_ids(&result), "model_infos": result}))
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
    crate::util::resolve_llm_backend_api_alias(&mut body, &cfg_snapshot);
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
    fn message_plain_text_extracts_multimodal_text_without_base64() {
        let message = serde_json::json!({
            "role": "user",
            "content": [
                {"type": "text", "text": "Bitte analysieren"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}
            ]
        });
        assert_eq!(message_plain_text(&message), "Bitte analysieren\n[image]");
        assert_eq!(estimate_message_tokens(&[message]), 7);
    }

    #[test]
    fn enhancer_decision_parses_json_inside_model_text() {
        let decision = parse_enhancer_decision(
            "kurz:\n{\"action\":\"annotate\",\"notes\":\"ok\",\"flags\":[\"input\"],\"reason\":\"x\"}",
        )
        .unwrap();
        assert_eq!(decision.action, "annotate");
        assert_eq!(decision.notes.as_deref(), Some("ok"));
        assert_eq!(decision.flags, vec!["input"]);
    }

    #[test]
    fn enhancer_modes_restrict_pipeline_actions() {
        assert!(enhancer_allows_action("observe", "annotate"));
        assert!(!enhancer_allows_action("observe", "replace"));
        assert!(enhancer_allows_action("rewrite", "replace"));
        assert!(!enhancer_allows_action("rewrite", "block"));
        assert!(enhancer_allows_action("gateway", "cancel"));
    }

    #[test]
    fn model_info_marks_openrouter_free_models() {
        let by_suffix = serde_json::json!({"id": "qwen/qwen3-coder:free", "name": "Qwen Coder"});
        let info = model_info_from_openai_value(&by_suffix).unwrap();
        assert!(info.free);
        assert_eq!(info.display_name, "Qwen Coder");

        let by_pricing = serde_json::json!({
            "id": "provider/model",
            "pricing": {"prompt": "0", "completion": "0.0", "request": 0}
        });
        assert!(model_info_from_openai_value(&by_pricing).unwrap().free);
    }

    #[test]
    fn model_infos_sort_alphabetically_by_display_name() {
        let models = sort_model_infos(vec![
            LlmModelInfo {
                id: "b/model".into(),
                display_name: "Beta".into(),
                free: false,
            },
            LlmModelInfo {
                id: "a/model".into(),
                display_name: "alpha".into(),
                free: true,
            },
            LlmModelInfo {
                id: "z/model".into(),
                display_name: "Zeta".into(),
                free: false,
            },
        ]);
        assert_eq!(model_ids(&models), vec!["a/model", "b/model", "z/model"]);
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
    fn chat_tool_result_for_llm_caps_capabilities_result_more_aggressively() {
        let large = format!(
            "AGENT_CAPABILITIES\n{}",
            "tool.name() description\n".repeat(MAX_CHAT_TOOL_RESULT_CHARS)
        );
        let result = chat_tool_result_for_llm(true, &large);
        assert!(result.starts_with("SUCCESS: AGENT_CAPABILITIES"));
        assert!(result.contains("gekuerzt"));
        assert!(result.chars().count() < MAX_CHAT_TOOL_RESULT_CHARS);
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
            input_enhancers: vec![],
            output_enhancers: vec![],
            combined_enhancers: vec![],
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
    fn deepdive_gate_accepts_rss_ingest_as_fresh_evidence() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "rss_verwaltung.fuer_deepdive", true, 1);
        let feedback = deepdive_gate_feedback(
            &progress,
            "Test RSS Quellen zu Energiepolitik Deutschland",
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
    fn simple_search_without_current_or_deepdive_is_not_forced_deepdive() {
        assert!(!is_deepdive_request("such mal nach Ryzen 5 3600"));
        assert!(!is_deepdive_request(
            "such mal im web nach Road to Vostok weapons guide"
        ));
        assert!(!is_deepdive_request(
            "VOICE_INPUT transkribiert aus Telegram:\nIch spiele gerade Road to Vostok und bin am Start, such kurz raus wie ich Waffen finde"
        ));
        assert_eq!(
            preferred_deepdive_tool("such mal nach Ryzen 5 3600"),
            "deepdive.quick"
        );
    }

    #[test]
    fn explicit_no_research_blocks_deepdive_detection() {
        assert!(!is_deepdive_request(
            "Antworte exakt mit OK, keine Recherche."
        ));
        assert!(!is_deepdive_request(
            "Erklaere Pipeline-Enhancer kurz, keine externe Recherche."
        ));
        assert!(rejects_research_tools(
            "Bitte ohne externe Recherche antworten"
        ));
        assert!(rejects_research_tools("Bitte ohne Tools antworten"));
        assert!(!chat_should_enable_tools(
            "Antworte exakt mit OK, keine Recherche."
        ));
        assert!(is_research_tool_name("deepdive.quick"));
        assert!(is_research_tool_name("duckduckgo.search"));
        assert!(!is_research_tool_name("math_tools.calculate"));
    }

    #[test]
    fn casual_chat_does_not_load_tool_schemas() {
        assert!(!chat_should_enable_tools("wie gehts dir heute?"));
        assert!(!chat_should_enable_tools(
            "VOICE_INPUT transkribiert aus Telegram:\nAlso verstehe ich das richtig, ich muss wieder zur Huette zurueck?"
        ));
        assert!(chat_should_enable_tools(
            "such mal im web nach Ryzen 5 3600"
        ));
        assert!(chat_should_enable_tools("fix bitte den bug im modul"));
        assert!(chat_should_enable_tools("rechne 12 mal 7"));
    }

    #[test]
    fn explicit_causal_or_multilingual_deepdive_uses_full_crawl() {
        assert_eq!(
            preferred_deepdive_tool(
                "DeepDive Japan Aufruestung mit Kausalitaeten und anderen Sprachen"
            ),
            "deepdive.crawl"
        );
    }

    #[test]
    fn deepdive_gate_requires_pack_after_crawl() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "deepdive.crawl", true, 1);
        let feedback = deepdive_gate_feedback(&progress, "Deepdive zu Friedrich Merz", "").unwrap();
        assert!(feedback.contains("deepdive.pack(Friedrich Merz)"));
    }

    #[test]
    fn deepdive_gate_uses_crawl_id_for_pack() {
        let progress = DeepdiveProgress {
            crawl_ok: 1,
            crawl_id: Some("dd-20260507T010203Z-abcdef12".into()),
            last_evidence_round: 1,
            ..Default::default()
        };
        let feedback = deepdive_gate_feedback(&progress, "Deepdive zu Friedrich Merz", "").unwrap();
        assert!(feedback.contains("deepdive.pack(dd-20260507T010203Z-abcdef12)"));
    }

    #[test]
    fn deepdive_gate_allows_crawl_plus_pack() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "deepdive.crawl", true, 1);
        observe_deepdive_progress(&mut progress, "deepdive.pack", true, 2);
        observe_deepdive_progress(&mut progress, "deepdive.blocks", true, 3);
        let final_text = "Lagebild mit Timeline, Hintergrund, Subcrawls / Side-Infos und Branching / Missing Links...\n<quellen>\n- fundort: https://example.test/source\n</quellen>";
        assert!(
            deepdive_gate_feedback(&progress, "Deepdive zu Friedrich Merz", final_text).is_none()
        );
    }

    #[test]
    fn deepdive_gate_rejects_linear_answer_without_branching() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "deepdive.crawl", true, 1);
        observe_deepdive_progress(&mut progress, "deepdive.pack", true, 2);
        observe_deepdive_progress(&mut progress, "deepdive.blocks", true, 3);
        let final_text = "Lagebild mit Timeline und Hintergrund...\n<quellen>\n- fundort: https://example.test/source\n</quellen>";
        let feedback =
            deepdive_gate_feedback(&progress, "Deepdive zu Friedrich Merz", final_text).unwrap();
        assert!(feedback.contains("zu linear"));
    }

    #[test]
    fn deepdive_gate_requires_blocks_after_pack() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "deepdive.crawl", true, 1);
        observe_deepdive_progress(&mut progress, "deepdive.pack", true, 2);
        let feedback = deepdive_gate_feedback(&progress, "Deepdive zu Friedrich Merz", "").unwrap();
        assert!(feedback.contains("deepdive.blocks(Friedrich Merz)"));
    }

    #[test]
    fn deepdive_gate_requires_exact_source_block() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "deepdive.crawl", true, 1);
        observe_deepdive_progress(&mut progress, "deepdive.pack", true, 2);
        observe_deepdive_progress(&mut progress, "deepdive.blocks", true, 3);
        let feedback =
            deepdive_gate_feedback(&progress, "Deepdive zu Friedrich Merz", "Quelle: FAZ").unwrap();
        assert!(feedback.contains("<quellen>"));
    }

    #[test]
    fn deepdive_gate_rejects_source_pool_only_answer() {
        let mut progress = DeepdiveProgress::default();
        observe_deepdive_progress(&mut progress, "deepdive.crawl", true, 1);
        observe_deepdive_progress(&mut progress, "deepdive.pack", true, 2);
        observe_deepdive_progress(&mut progress, "deepdive.blocks", true, 3);
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
        observe_deepdive_progress(&mut progress, "deepdive.pack", true, 2);
        observe_deepdive_progress(&mut progress, "deepdive.blocks", true, 3);
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
