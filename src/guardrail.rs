// src/guardrail.rs — Deterministic tool-call validator, event logger, stats aggregator.
//
// Hooked in before exec_tool_unified (cycle/chat) and before wizard::dispatch_tool.
// Every check emits a GuardrailEvent; events land in agent-data/guardrail-events/<YYYY-MM-DD>.jsonl.
// Aggregate counts are held in-memory, rebuilt from the last 7 days of logs at startup.

use std::path::{Path, PathBuf};
use crate::types::{AgentConfig, GuardrailConfig, GuardrailEvent, StatsSummary, ValidationError};

// ─── Paths ─────────────────────────────────────────

pub fn events_dir(data_root: &Path) -> PathBuf {
    data_root.join("guardrail-events")
}
pub fn day_log_path(data_root: &Path, day: &str) -> PathBuf {
    events_dir(data_root).join(format!("{}.jsonl", day))
}
pub async fn ensure_dirs(data_root: &Path) -> std::io::Result<()> {
    tokio::fs::create_dir_all(events_dir(data_root)).await
}

fn today_str() -> String {
    chrono::Utc::now().format("%Y-%m-%d").to_string()
}

// ─── Levenshtein similarity ────────────────────────

fn levenshtein(a: &str, b: &str) -> usize {
    let (n, m) = (a.chars().count(), b.chars().count());
    if n == 0 { return m; }
    if m == 0 { return n; }
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let mut prev: Vec<usize> = (0..=m).collect();
    let mut curr = vec![0usize; m + 1];
    for i in 1..=n {
        curr[0] = i;
        for j in 1..=m {
            let cost = if a[i-1] == b[j-1] { 0 } else { 1 };
            curr[j] = (prev[j] + 1)
                .min(curr[j-1] + 1)
                .min(prev[j-1] + cost);
        }
        std::mem::swap(&mut prev, &mut curr);
    }
    prev[m]
}

/// Returns a known tool name that is "close" to `bad` (Levenshtein <= 2), or None.
pub fn suggest_similar_tool(bad: &str, known: &[String]) -> Option<String> {
    let mut best: Option<(&String, usize)> = None;
    for name in known {
        let d = levenshtein(bad, name);
        if d <= 2 && d > 0 {
            if best.map(|(_, b)| d < b).unwrap_or(true) {
                best = Some((name, d));
            }
        }
    }
    best.map(|(s, _)| s.clone())
}

// ─── Validator ─────────────────────────────────────

/// Parsed + validated tool call. Caller passes this to exec_tool_unified / dispatch_tool.
#[derive(Debug, Clone)]
pub struct ParsedCall {
    pub id: String,           // LLM call id (OpenAI-style); empty if absent
    pub tool_name: String,
    pub arguments: serde_json::Value,
}

/// The context a validator needs besides the raw response.
pub struct ValidatorContext<'a> {
    pub modul_id: &'a str,
    pub cfg: &'a AgentConfig,
    pub py_modules: &'a [crate::loader::PyModuleMeta],
    pub last_user_msg: Option<&'a str>,
    pub strict_mode: bool,
}

const TOOL_NAME_RE_FIRST: &str = "abcdefghijklmnopqrstuvwxyz";
const TOOL_NAME_RE_REST: &str = "abcdefghijklmnopqrstuvwxyz0123456789._";
const DEFAULT_STRICT_TRIGGERS: &[&str] = &["ruf", "schicke", "sende", "list", "show", "search", "suche", "create", "lies", "speicher", "löschen", "teste"];

fn effective_strict_triggers(cfg: &crate::types::GuardrailConfig) -> Vec<String> {
    if cfg.strict_triggers.is_empty() {
        DEFAULT_STRICT_TRIGGERS.iter().map(|s| s.to_string()).collect()
    } else {
        cfg.strict_triggers.clone()
    }
}

fn tool_name_ok(name: &str) -> bool {
    if name.is_empty() || name.len() > 64 { return false; }
    let mut chars = name.chars();
    match chars.next() {
        Some(c) if TOOL_NAME_RE_FIRST.contains(c) => {}
        _ => return false,
    }
    chars.all(|c| TOOL_NAME_RE_REST.contains(c))
}

fn looks_like_prose(name: &str) -> bool {
    // Obvious prose patterns: whitespace, colons, brackets, or suspicious keywords
    name.chars().any(|c| c.is_whitespace() || c == ':' || c == '(' || c == '[')
        || name.starts_with("call_")    // OpenAI call IDs that leaked into tool_name field
        || name.to_lowercase().contains("please")
        || name.to_lowercase().contains("use the")
}

/// Known tools for a given modul: built-ins, linked-module tools, and py-module tools.
fn known_tools_for_modul(
    modul_id: &str,
    cfg: &AgentConfig,
    py_modules: &[crate::loader::PyModuleMeta],
) -> Vec<String> {
    // Special case: __wizard__ uses wizard tool descriptors
    if modul_id == "__wizard__" {
        let descriptors = crate::wizard::wizard_tool_descriptors(true);
        let mut out = Vec::new();
        if let Some(arr) = descriptors.as_array() {
            for t in arr {
                if let Some(n) = t.pointer("/function/name").and_then(|v| v.as_str()) {
                    out.push(n.to_string());
                }
            }
        }
        return out;
    }

    let mut out: Vec<String> = Vec::new();
    let modul = match cfg.module.iter().find(|m| m.id == modul_id || m.name == modul_id) {
        Some(m) => m,
        None => return out,
    };
    // OpenAI tools format: we reuse tools_as_openai_json to derive reachable names.
    for t in crate::tools::tools_as_openai_json(modul, py_modules) {
        if let Some(n) = t.pointer("/function/name").and_then(|v| v.as_str()) {
            out.push(n.to_string());
        }
    }
    out
}

pub fn validate_response(
    raw: &serde_json::Value,
    ctx: &ValidatorContext,
) -> Result<Vec<ParsedCall>, Vec<ValidationError>> {
    let mut errs: Vec<ValidationError> = Vec::new();

    // Pull tool_calls from raw. Accept OpenAI-nested, direct array (our mock tests),
    // and Anthropic tool_use format already flattened to OpenAI by llm.rs::dispatch_chat.
    let tool_calls_val = raw.pointer("/choices/0/message/tool_calls")
        .cloned()
        .or_else(|| if raw.is_array() { Some(raw.clone()) } else { None });

    let arr = match tool_calls_val.as_ref().and_then(|v| v.as_array()) {
        Some(a) => a.clone(),
        None => Vec::new(),
    };

    // If raw has no tool_calls at all and strict_mode triggers: fail.
    if arr.is_empty() {
        if ctx.strict_mode {
            if let Some(msg) = ctx.last_user_msg {
                let low = msg.to_lowercase();
                let triggers = if let Some(g) = ctx.cfg.guardrail.as_ref() {
                    effective_strict_triggers(g)
                } else {
                    DEFAULT_STRICT_TRIGGERS.iter().map(|s| s.to_string()).collect()
                };
                if triggers.iter().any(|t| low.contains(t.to_lowercase().as_str())) {
                    errs.push(ValidationError {
                        field: "tool_calls".into(),
                        code: "no_tool_call_when_expected".into(),
                        human_message_de: "Die Antwort enthaelt keinen Tool-Call, obwohl der User-Prompt ein Tool verlangte.".into(),
                    });
                    return Err(errs);
                }
            }
        }
        return Ok(Vec::new());
    }

    let known = known_tools_for_modul(ctx.modul_id, ctx.cfg, ctx.py_modules);
    let mut calls: Vec<ParsedCall> = Vec::new();
    let modul = ctx.cfg.module.iter().find(|m| m.id == ctx.modul_id || m.name == ctx.modul_id);

    for (idx, item) in arr.iter().enumerate() {
        let prefix = format!("tool_calls[{}]", idx);
        // --- Shape: OpenAI-style {id, function:{name, arguments: string}}
        // Or Anthropic-style {type:"tool_use", id, name, input: object}
        let (id, tool_name, args_val): (String, String, serde_json::Value) =
            if let Some(func) = item.get("function") {
                let id = item.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let name = func.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let args_str = func.get("arguments").and_then(|v| v.as_str()).unwrap_or("{}");
                let args = match serde_json::from_str::<serde_json::Value>(args_str) {
                    Ok(v) => v,
                    Err(_) => {
                        errs.push(ValidationError {
                            field: format!("{}.arguments", prefix),
                            code: "bad_json".into(),
                            human_message_de: format!("Tool-Call '{}' hat ungueltiges JSON in arguments.", name),
                        });
                        continue;
                    }
                };
                (id, name, args)
            } else if item.get("type").and_then(|v| v.as_str()) == Some("tool_use") {
                let id = item.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let name = item.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let args = item.get("input").cloned().unwrap_or(serde_json::json!({}));
                (id, name, args)
            } else {
                errs.push(ValidationError {
                    field: prefix.clone(),
                    code: "bad_shape".into(),
                    human_message_de: "Unbekanntes Tool-Call-Format in der Response.".into(),
                });
                continue;
            };

        // Anti-Gibberish: shape of the name itself
        if !tool_name_ok(&tool_name) || looks_like_prose(&tool_name) {
            let mut err = ValidationError {
                field: format!("{}.name", prefix),
                code: "gibberish".into(),
                human_message_de: format!("Tool-Name '{}' enthaelt unzulaessige Zeichen oder sieht nach Prosa aus.", tool_name),
            };
            if let Some(s) = suggest_similar_tool(&tool_name, &known) {
                err.human_message_de = format!("{} Meinst du '{}'?", err.human_message_de, s);
            }
            errs.push(err);
            continue;
        }

        // Known tool?
        if !known.iter().any(|k| k == &tool_name) {
            let mut err = ValidationError {
                field: format!("{}.name", prefix),
                code: "unknown_tool".into(),
                human_message_de: format!("Tool '{}' existiert nicht fuer Modul '{}'.", tool_name, ctx.modul_id),
            };
            if let Some(s) = suggest_similar_tool(&tool_name, &known) {
                err.human_message_de = format!("{} Meinst du '{}'?", err.human_message_de, s);
            }
            errs.push(err);
            continue;
        }

        // Permission check (catches linked-modules drift)
        if let Some(m) = modul {
            if !crate::tools::has_permission_with_py(m, &tool_name, ctx.py_modules) {
                errs.push(ValidationError {
                    field: format!("{}.name", prefix),
                    code: "no_permission".into(),
                    human_message_de: format!("Modul '{}' hat keine Berechtigung fuer Tool '{}'.", ctx.modul_id, tool_name),
                });
                continue;
            }
        }

        // Arguments must be an object (per OpenAI schema convention)
        if !args_val.is_object() && !args_val.is_null() {
            errs.push(ValidationError {
                field: format!("{}.arguments", prefix),
                code: "bad_param_type".into(),
                human_message_de: "arguments muss ein Objekt sein.".into(),
            });
            continue;
        }

        calls.push(ParsedCall { id, tool_name, arguments: args_val });
    }

    if !errs.is_empty() { Err(errs) } else { Ok(calls) }
}

pub fn synth_feedback_user_message(errors: &[ValidationError], max_retries: u32, attempt: u32) -> String {
    let mut lines = vec![format!("SYSTEM-FEEDBACK (Retry {}/{}): Dein letzter Tool-Call war ungueltig:", attempt + 1, max_retries)];
    for e in errors {
        lines.push(format!("- {} [{}]: {}", e.field, e.code, e.human_message_de));
    }
    lines.push("Bitte korrigieren und den Tool-Call erneut senden.".into());
    lines.join("\n")
}

// ─── Event log ─────────────────────────────────────

pub async fn log_event(data_root: &Path, event: &GuardrailEvent) -> std::io::Result<()> {
    let day = chrono::DateTime::<chrono::Utc>::from_timestamp(event.ts, 0)
        .map(|dt| dt.format("%Y-%m-%d").to_string())
        .unwrap_or_else(today_str);
    let path = day_log_path(data_root, &day);
    ensure_dirs(data_root).await?;
    let mut redacted = serde_json::to_value(event)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    crate::security::redact_secrets(&mut redacted);
    let mut line = serde_json::to_string(&redacted)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    line.push('\n');
    // Atomic-ish append: open with append mode. Single-writer assumption holds
    // because guardrail events are emitted from one process (the agent binary).
    use tokio::io::AsyncWriteExt;
    let mut f = tokio::fs::OpenOptions::new()
        .create(true).append(true).open(&path).await?;
    f.write_all(line.as_bytes()).await?;
    f.flush().await?;
    Ok(())
}

pub async fn load_events_since(
    data_root: &Path,
    since_ts: i64,
    limit: usize,
    backend_filter: Option<&str>,
    only_failed: bool,
) -> Vec<GuardrailEvent> {
    let dir = events_dir(data_root);
    let mut entries = match tokio::fs::read_dir(&dir).await {
        Ok(e) => e,
        Err(_) => return Vec::new(),
    };
    let mut files: Vec<PathBuf> = Vec::new();
    while let Ok(Some(e)) = entries.next_entry().await {
        if e.path().extension().and_then(|s| s.to_str()) == Some("jsonl") {
            files.push(e.path());
        }
    }
    files.sort();
    files.reverse();  // newest first

    let mut out: Vec<GuardrailEvent> = Vec::new();
    for f in files {
        let bytes = match tokio::fs::read(&f).await {
            Ok(b) => b,
            Err(_) => continue,
        };
        let text = String::from_utf8_lossy(&bytes);
        for line in text.lines().rev() {
            if line.trim().is_empty() { continue; }
            if let Ok(ev) = serde_json::from_str::<GuardrailEvent>(line) {
                if ev.ts < since_ts { return out; }
                if let Some(b) = backend_filter {
                    if ev.backend != b { continue; }
                }
                if only_failed && ev.passed { continue; }
                out.push(ev);
                if out.len() >= limit { return out; }
            }
        }
    }
    out
}

pub async fn compute_stats(
    data_root: &Path,
    window_hours: u32,
) -> StatsSummary {
    let since = chrono::Utc::now().timestamp() - (window_hours as i64) * 3600;
    let events = load_events_since(data_root, since, 100_000, None, false).await;

    let mut s = StatsSummary { window_hours, ..Default::default() };
    let mut err_counts: std::collections::HashMap<String, u64> = std::collections::HashMap::new();

    for e in &events {
        s.total += 1;
        if e.passed {
            s.valid += 1;
        } else {
            s.invalid += 1;
        }
        if e.retry_attempt > 0 { s.retried += 1; }
        if e.final_outcome == "hard_fail" { s.hard_failed += 1; }

        let be = s.per_backend.entry(e.backend.clone()).or_default();
        be.total += 1;
        if e.passed { be.valid += 1; }
        if e.final_outcome == "hard_fail" { be.hard_failed += 1; }

        let m = be.per_model.entry(e.model.clone()).or_default();
        m.total += 1;
        if e.passed { m.valid += 1; }
        if e.final_outcome == "hard_fail" { m.hard_failed += 1; }
        if e.ts > m.last_ts { m.last_ts = e.ts; }

        for err in &e.errors {
            *err_counts.entry(err.code.clone()).or_insert(0) += 1;
        }
    }
    let mut top: Vec<(String, u64)> = err_counts.into_iter().collect();
    top.sort_by(|a, b| b.1.cmp(&a.1));
    top.truncate(5);
    s.top_errors = top;
    s
}

// ─── Retry loop ────────────────────────────────────

/// Runs an async LLM call in a retry loop. On validator failure, synthesizes a
/// feedback user message and invokes `push_feedback` so the caller can append
/// it to its transcript. Returns the parsed calls on first success, or hard-fail error.
pub async fn with_validation<'ctx, Fut, F, Push, B>(
    cfg: &GuardrailConfig,
    ctx_builder: B,
    backend_id: &str,
    modul_id: &str,
    model: &str,
    data_root: &Path,
    mut call: F,
    mut push_feedback: Push,
) -> Result<Vec<ParsedCall>, String>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<serde_json::Value, String>>,
    Push: FnMut(&str),
    B: Fn() -> ValidatorContext<'ctx>,
{
    let max_retries = cfg.per_backend_overrides
        .get(backend_id).copied()
        .unwrap_or(cfg.max_retries);

    for attempt in 0..=max_retries {
        let raw = match call().await {
            Ok(v) => v,
            Err(e) => return Err(e),
        };
        let ctx = ctx_builder();
        match validate_response(&raw, &ctx) {
            Ok(calls) => {
                let ev = GuardrailEvent {
                    ts: chrono::Utc::now().timestamp(),
                    modul: modul_id.into(),
                    backend: backend_id.into(),
                    model: model.into(),
                    tool_name: calls.first().map(|c| c.tool_name.clone()),
                    passed: true,
                    errors: vec![],
                    retry_attempt: attempt,
                    final_outcome: if attempt > 0 { "retried".into() } else { "ok".into() },
                    similar_suggestion: None,
                };
                let _ = log_event(data_root, &ev).await;
                return Ok(calls);
            }
            Err(errors) => {
                let is_last = attempt == max_retries;
                let ev = GuardrailEvent {
                    ts: chrono::Utc::now().timestamp(),
                    modul: modul_id.into(),
                    backend: backend_id.into(),
                    model: model.into(),
                    tool_name: None,
                    passed: false,
                    errors: errors.clone(),
                    retry_attempt: attempt,
                    final_outcome: if is_last { "hard_fail".into() } else { "retried".into() },
                    similar_suggestion: None,
                };
                let _ = log_event(data_root, &ev).await;
                if is_last {
                    let codes: Vec<String> = errors.iter().map(|e| e.code.clone()).collect();
                    return Err(format!("Guardrail hard-fail nach {} Retries. Codes: {:?}",
                                       max_retries, codes));
                }
                let feedback = synth_feedback_user_message(&errors, max_retries, attempt);
                push_feedback(&feedback);
                // Loop continues with next attempt; caller has already appended feedback
            }
        }
    }
    Err("unreachable".into())
}

/// Cleanup old event files according to retention policy.
pub async fn cleanup_old_events(data_root: &Path, retention_days: u32) -> usize {
    if retention_days == 0 { return 0; }
    let cutoff = chrono::Utc::now().timestamp() - (retention_days as i64) * 86400;
    let dir = events_dir(data_root);
    let mut entries = match tokio::fs::read_dir(&dir).await {
        Ok(e) => e,
        Err(_) => return 0,
    };
    let mut removed = 0;
    while let Ok(Some(entry)) = entries.next_entry().await {
        if let Ok(meta) = entry.metadata().await {
            if let Ok(modified) = meta.modified() {
                if let Ok(dur) = modified.duration_since(std::time::UNIX_EPOCH) {
                    if (dur.as_secs() as i64) < cutoff {
                        let _ = tokio::fs::remove_file(entry.path()).await;
                        removed += 1;
                    }
                }
            }
        }
    }
    removed
}

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

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn ensure_dirs_creates_events_dir() {
        let tmp = tempfile::tempdir().unwrap();
        ensure_dirs(tmp.path()).await.unwrap();
        assert!(events_dir(tmp.path()).exists());
    }

    #[test]
    fn day_log_path_uses_yyyy_mm_dd() {
        let p = day_log_path(Path::new("/x"), "2026-04-18");
        assert_eq!(p, PathBuf::from("/x/guardrail-events/2026-04-18.jsonl"));
    }

    #[test]
    fn levenshtein_basic() {
        assert_eq!(levenshtein("", ""), 0);
        assert_eq!(levenshtein("a", "b"), 1);
        assert_eq!(levenshtein("kitten", "sitting"), 3);
    }

    #[test]
    fn suggest_similar_catches_typos() {
        let known = vec!["shell.exec".to_string(), "files.read".to_string(), "web.search".to_string()];
        assert_eq!(suggest_similar_tool("sehel.exec", &known), Some("shell.exec".to_string()));
        assert_eq!(suggest_similar_tool("file.read", &known), Some("files.read".to_string()));
        assert_eq!(suggest_similar_tool("totally_unrelated_name", &known), None);
        assert_eq!(suggest_similar_tool("shell.exec", &known), None);  // identical, not a suggestion
    }

    use crate::types::ModulConfig;

    fn test_cfg() -> AgentConfig {
        use crate::types::{LlmBackend, LlmTyp, ModulIdentity, ModulSettings};
        let mut cfg = AgentConfig::default();
        cfg.llm_backends.push(LlmBackend {
            id: "grok".into(), name: "Grok".into(), typ: LlmTyp::Grok,
            url: "https://api.x.ai".into(), api_key: Some("k".into()),
            model: "grok-4".into(), timeout_s: 30,
            identity: ModulIdentity::default(), max_tokens: None,
        });
        cfg.module.push(ModulConfig {
            id: "chat.bob".into(), typ: "chat".into(), name: "chat.bob".into(),
            display_name: "Bob".into(), llm_backend: "grok".into(), backup_llm: None,
            berechtigungen: vec!["aufgaben".into()], timeout_s: 60, retry: 2,
            settings: ModulSettings::default(),
            identity: ModulIdentity { bot_name: "Bob".into(), greeting: "".into(), system_prompt: "bob".into(), ..Default::default() },
            rag_pool: None, linked_modules: vec![], persistent: true,
            spawned_by: None, spawn_ttl_s: None, created_at: None, scheduler_interval_ms: None,
            max_concurrent_tasks: None, token_budget: None, token_budget_warning: None,
        });
        cfg
    }

    fn ctx<'a>(cfg: &'a AgentConfig) -> ValidatorContext<'a> {
        ValidatorContext {
            modul_id: "chat.bob",
            cfg,
            py_modules: &[],
            last_user_msg: None,
            strict_mode: false,
        }
    }

    #[test]
    fn validate_empty_response_is_ok_when_not_strict() {
        let cfg = test_cfg();
        let r = validate_response(&serde_json::json!({"choices":[{"message":{}}]}), &ctx(&cfg));
        assert!(matches!(r, Ok(v) if v.is_empty()));
    }

    #[test]
    fn validate_empty_with_strict_and_trigger_fails() {
        let cfg = test_cfg();
        let mut c = ctx(&cfg);
        c.strict_mode = true;
        c.last_user_msg = Some("bitte ruf das tool auf");
        let r = validate_response(&serde_json::json!({"choices":[{"message":{}}]}), &c);
        let errs = r.unwrap_err();
        assert!(errs.iter().any(|e| e.code == "no_tool_call_when_expected"));
    }

    #[test]
    fn validate_unknown_tool_rejected_with_suggestion() {
        let cfg = test_cfg();
        let raw = serde_json::json!([{
            "id":"c1","function":{"name":"aufgabn.erstellen","arguments":"{}"}
        }]);
        let errs = validate_response(&raw, &ctx(&cfg)).unwrap_err();
        assert!(errs.iter().any(|e| e.code == "unknown_tool"));
        // suggestion message should reference a close match if any
        assert!(errs.iter().any(|e| e.human_message_de.contains("Meinst du") || e.code == "unknown_tool"));
    }

    #[test]
    fn validate_bad_json_in_arguments() {
        let cfg = test_cfg();
        let raw = serde_json::json!([{
            "id":"c1","function":{"name":"aufgaben.erstellen","arguments":"{not json}"}
        }]);
        let errs = validate_response(&raw, &ctx(&cfg)).unwrap_err();
        assert!(errs.iter().any(|e| e.code == "bad_json"));
    }

    #[test]
    fn validate_gibberish_tool_name() {
        let cfg = test_cfg();
        let raw = serde_json::json!([{
            "id":"c1","function":{"name":"please call the shell","arguments":"{}"}
        }]);
        let errs = validate_response(&raw, &ctx(&cfg)).unwrap_err();
        assert!(errs.iter().any(|e| e.code == "gibberish"));
    }

    #[test]
    fn validate_known_tool_passes() {
        let cfg = test_cfg();
        let raw = serde_json::json!([{
            "id":"c1","function":{"name":"aufgaben.erstellen","arguments":"{\"ziel\":\"web.search\",\"anweisung\":\"x\"}"}
        }]);
        let out = validate_response(&raw, &ctx(&cfg)).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].tool_name, "aufgaben.erstellen");
    }

    #[test]
    fn synth_feedback_includes_code_and_message() {
        let e = vec![ValidationError {
            field: "f".into(), code: "unknown_tool".into(),
            human_message_de: "X".into(),
        }];
        let msg = synth_feedback_user_message(&e, 2, 0);
        assert!(msg.contains("unknown_tool"));
        assert!(msg.contains("X"));
        assert!(msg.contains("Retry 1/2"));
    }

    #[tokio::test]
    async fn log_and_reload_event_roundtrip() {
        let tmp = tempfile::tempdir().unwrap();
        let ev = GuardrailEvent {
            ts: chrono::Utc::now().timestamp(),
            modul: "chat.bob".into(),
            backend: "grok".into(),
            model: "grok-4".into(),
            tool_name: Some("aufgaben.erstellen".into()),
            passed: true,
            errors: vec![],
            retry_attempt: 0,
            final_outcome: "ok".into(),
            similar_suggestion: None,
        };
        log_event(tmp.path(), &ev).await.unwrap();
        let events = load_events_since(tmp.path(), 0, 10, None, false).await;
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].tool_name.as_deref(), Some("aufgaben.erstellen"));
    }

    #[tokio::test]
    async fn compute_stats_aggregates_per_backend_model() {
        let tmp = tempfile::tempdir().unwrap();
        let now = chrono::Utc::now().timestamp();
        for (backend, model, passed) in [
            ("grok", "grok-4", true),
            ("grok", "grok-4", false),
            ("grok", "grok-4", true),
            ("anthropic", "claude-haiku", true),
        ] {
            log_event(tmp.path(), &GuardrailEvent {
                ts: now, modul: "m".into(),
                backend: backend.into(), model: model.into(),
                tool_name: None, passed, errors: vec![],
                retry_attempt: 0, final_outcome: if passed { "ok".into() } else { "hard_fail".into() },
                similar_suggestion: None,
            }).await.unwrap();
        }
        let s = compute_stats(tmp.path(), 24).await;
        assert_eq!(s.total, 4);
        assert_eq!(s.valid, 3);
        assert_eq!(s.invalid, 1);
        let grok = s.per_backend.get("grok").unwrap();
        assert_eq!(grok.total, 3);
        assert_eq!(grok.valid, 2);
    }

    #[tokio::test]
    async fn load_events_respects_backend_filter() {
        let tmp = tempfile::tempdir().unwrap();
        let now = chrono::Utc::now().timestamp();
        for backend in ["grok", "anthropic", "grok"] {
            log_event(tmp.path(), &GuardrailEvent {
                ts: now, modul: "m".into(),
                backend: backend.into(), model: "x".into(),
                tool_name: None, passed: true, errors: vec![],
                retry_attempt: 0, final_outcome: "ok".into(),
                similar_suggestion: None,
            }).await.unwrap();
        }
        let e = load_events_since(tmp.path(), 0, 10, Some("grok"), false).await;
        assert_eq!(e.len(), 2);
    }

    #[tokio::test]
    async fn with_validation_retries_then_succeeds() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg = test_cfg();
        let gcfg = GuardrailConfig {
            enabled: true, max_retries: 2, strict_mode: false,
            per_backend_overrides: Default::default(), max_events_per_turn: 10,
            ..Default::default()
        };

        let bad = serde_json::json!([{
            "id":"c1","function":{"name":"totally.unknown","arguments":"{}"}
        }]);
        let good = serde_json::json!([{
            "id":"c2","function":{"name":"aufgaben.erstellen","arguments":"{}"}
        }]);
        let responses = std::sync::Arc::new(std::sync::Mutex::new(vec![bad, good]));
        let feedback_recv = std::sync::Arc::new(std::sync::Mutex::new(Vec::<String>::new()));

        let resp_clone = responses.clone();
        let fb_clone = feedback_recv.clone();
        let calls = with_validation(
            &gcfg,
            || ValidatorContext {
                modul_id: "chat.bob", cfg: &cfg, py_modules: &[],
                last_user_msg: None, strict_mode: false,
            },
            "grok", "chat.bob", "grok-4",
            tmp.path(),
            || {
                let resps = resp_clone.clone();
                async move {
                    let mut g = resps.lock().unwrap();
                    Ok(g.remove(0))
                }
            },
            |msg| {
                fb_clone.lock().unwrap().push(msg.to_string());
            },
        ).await.unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].tool_name, "aufgaben.erstellen");
        assert_eq!(feedback_recv.lock().unwrap().len(), 1); // one feedback message pushed
    }

    #[tokio::test]
    async fn with_validation_hard_fails_after_max_retries() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg = test_cfg();
        let gcfg = GuardrailConfig {
            enabled: true, max_retries: 1, strict_mode: false,
            per_backend_overrides: Default::default(), max_events_per_turn: 10,
            ..Default::default()
        };

        let bad = serde_json::json!([{
            "id":"c1","function":{"name":"nope","arguments":"{}"}
        }]);
        let responses = std::sync::Arc::new(std::sync::Mutex::new(vec![bad.clone(), bad]));
        let resp_clone = responses.clone();
        let r = with_validation(
            &gcfg,
            || ValidatorContext {
                modul_id: "chat.bob", cfg: &cfg, py_modules: &[],
                last_user_msg: None, strict_mode: false,
            },
            "grok", "chat.bob", "grok-4",
            tmp.path(),
            || {
                let resps = resp_clone.clone();
                async move {
                    let mut g = resps.lock().unwrap();
                    Ok(g.remove(0))
                }
            },
            |_| {},
        ).await;
        assert!(r.is_err(), "{:?}", r);
        assert!(r.unwrap_err().contains("Guardrail hard-fail"));
    }

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

    #[test]
    fn effective_strict_triggers_uses_config_when_provided() {
        let mut cfg = crate::types::GuardrailConfig::default();
        assert!(effective_strict_triggers(&cfg).iter().any(|s| s == "search" || s == "ruf")); // default set
        cfg.strict_triggers = vec!["custom_trigger".into()];
        let tr = effective_strict_triggers(&cfg);
        assert_eq!(tr, vec!["custom_trigger".to_string()]);
    }

    #[tokio::test]
    async fn log_fallback_event_roundtrip() {
        let tmp = tempfile::tempdir().unwrap();
        log_fallback_event(tmp.path(), "grok", "claude", "chat.x", &["unknown_tool".into()]).await.unwrap();
        let events = load_events_since(tmp.path(), 0, 10, None, false).await;
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].final_outcome, "fallback_triggered");
        assert_eq!(events[0].similar_suggestion.as_deref(), Some("claude"));
    }
}
