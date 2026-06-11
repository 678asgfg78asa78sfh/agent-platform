//! Turn-Engine: die geteilte Kern-Logik einer LLM-Runde.
//!
//! Vorher existierte dieselbe ~300-Zeilen-Logik zweimal — in `cycle::exec_llm`
//! (Scheduler-Tasks) und im Chat-Loop in `web.rs`. Jeder Fix musste doppelt
//! gebaut werden, und die Kopien drifteten (der Chat-Loop validierte z.B.
//! Text-Tag-Tool-Calls NICHT durch den Guardrail — ein Bypass).
//!
//! Die Engine besitzt: Rate-Limit-Wartelogik, den LLM-Call inkl. Backup,
//! Token-Tracking, die Injektion von `<tool>`-Text-Tag-Calls als synthetische
//! tool_calls (damit der Guardrail sie sieht), die Guardrail-Schleife mit
//! Retry/Backend-Fallback sowie das Parsen ALLER Tool-Calls einer Runde.
//!
//! NICHT in der Engine (bewusst Caller-Sache): Cost-Cap-Handling (Scheduler
//! rescheduled, Chat antwortet), Tool-Gates (DeepDive/Research-Reject sind
//! Chat-spezifisch), Sub-Task-Buchhaltung und die Final-Answer-Verarbeitung.

use crate::llm::LlmRouter;
use crate::pipeline::Pipeline;
use crate::tools::{self, ParsedOpenAiCall};
use crate::types::{AgentConfig, GuardrailConfig, LogTyp, ModulConfig};
use std::future::Future;
use std::sync::{
    Arc,
    atomic::{AtomicI64, Ordering},
};
use tokio::sync::mpsc;

pub type ActivityMarker = Arc<AtomicI64>;

pub fn mark_activity(activity: &Option<ActivityMarker>) {
    if let Some(marker) = activity {
        marker.store(chrono::Utc::now().timestamp(), Ordering::Relaxed);
    }
}

/// Haelt den Activity-Marker eines Tasks am Leben, solange die Future laeuft
/// (alle 5s ein Heartbeat) — der Idle-Watchdog wuerde lange LLM-/Tool-Calls
/// sonst als haengend einstufen.
pub async fn with_activity_heartbeat<F>(activity: &Option<ActivityMarker>, future: F) -> F::Output
where
    F: Future,
{
    mark_activity(activity);
    let Some(marker) = activity.clone() else {
        return future.await;
    };
    tokio::pin!(future);
    loop {
        tokio::select! {
            result = &mut future => {
                marker.store(chrono::Utc::now().timestamp(), Ordering::Relaxed);
                return result;
            }
            _ = tokio::time::sleep(std::time::Duration::from_secs(5)) => {
                marker.store(chrono::Utc::now().timestamp(), Ordering::Relaxed);
            }
        }
    }
}

/// Token-Verbrauch einer Runde (ueber alle internen Guardrail-Retries summiert).
#[derive(Debug, Default, Clone, Copy)]
pub struct RoundUsage {
    pub input: u64,
    pub output: u64,
}

impl RoundUsage {
    pub fn total(&self) -> u64 {
        self.input + self.output
    }
}

/// Ergebnis einer LLM-Runde.
pub enum RoundOutcome {
    /// Keine Tool-Calls — finale Textantwort.
    Final { text: String },
    /// Mindestens ein Tool-Call. `raw_message` ist die unveraenderte
    /// Assistant-Message (inkl. provider-Feldern wie DeepSeek reasoning_content),
    /// `response_text` der Textanteil (fuer Gates/Retry-Prompts der Caller).
    ToolCalls {
        calls: Vec<ParsedOpenAiCall>,
        raw_message: serde_json::Value,
        response_text: String,
    },
    /// Guardrail hard-fail nach allen Retries/Fallbacks.
    GuardrailHardFail { codes: Vec<String> },
    /// LLM-Call fehlgeschlagen (inkl. Backup). Reservation wurde freigegeben.
    LlmError(String),
}

pub struct TurnEngine<'a> {
    pub pipeline: &'a Arc<Pipeline>,
    pub llm: &'a Arc<LlmRouter>,
    pub cfg_snap: &'a AgentConfig,
    pub gcfg: &'a GuardrailConfig,
    pub py_mods_snap: &'a [crate::loader::PyModuleMeta],
    /// Modul fuer Schema-Lookups/Injektion. None = keine Tool-Schemata bekannt.
    pub modul: Option<&'a ModulConfig>,
    /// Label fuer pipeline.log (cycle: modul.name, chat: modul_id).
    pub log_label: &'a str,
    /// Task-Referenz fuer pipeline.log.
    pub log_task_id: Option<&'a str>,
    /// Modul-ID fuer Token-Attribution und Guardrail-Events.
    pub attribution_id: &'a str,
    pub tokens: &'a crate::web::TokenTracker,
    /// Chat-Status-Kanal (NDJSON-Strings) — None im Scheduler.
    pub status_tx: Option<mpsc::Sender<String>>,
    pub activity: Option<ActivityMarker>,
    pub tool_calls_disabled: bool,
    /// Backup-Backend (modul.backup_llm) fuer LLM-Fehler UND Guardrail-Fallback.
    pub backup_id: Option<String>,
    /// Einmaliges tool_choice fuer die NAECHSTE Runde ("required" erzwingt
    /// einen Tool-Call auf Protokollebene — ersetzt das STOPP-Prompt-Nudging
    /// als primaeren Mechanismus; die STOPP-Prompts bleiben Fallback fuer
    /// Provider, die tool_choice ignorieren). Wird nach der Runde geleert.
    pub tool_choice_once: Option<String>,
    // ── Runden-uebergreifender Zustand ──
    pub backend_id: String,
    pub model_str: String,
    pub guardrail_retries: u32,
    pub used_fallback: bool,
}

impl TurnEngine<'_> {
    fn schema_for(&self, name: &str) -> Option<Vec<String>> {
        self.modul
            .and_then(|m| tools::schema_required_for(name, m, self.py_mods_snap))
    }

    async fn send_status(&self, message: String) {
        if let Some(tx) = &self.status_tx {
            let _ = tx
                .send(serde_json::json!({"type": "status", "message": message}).to_string())
                .await;
        }
    }

    /// Fuehrt eine LLM-Runde aus: Rate-Slot, Call (mit Backup), Token-Tracking,
    /// Text-Tag-Injektion, Guardrail-Schleife, Tool-Call-Parsing.
    pub async fn run_round(
        &mut self,
        messages: &mut Vec<serde_json::Value>,
        tools_json: &[serde_json::Value],
    ) -> (RoundOutcome, RoundUsage) {
        let mut usage = RoundUsage::default();
        // Gilt fuer alle Versuche DIESER Runde (inkl. Guardrail-Retries),
        // danach zuruecksetzen.
        let opts = crate::llm::ChatOptions {
            tool_choice: self.tool_choice_once.take(),
        };
        loop {
            while let Some(wait) = self.llm.reserve_rate_slot_or_wait(&self.backend_id).await {
                mark_activity(&self.activity);
                let wait_s = wait.as_secs().max(1);
                self.pipeline.log(
                    self.log_label,
                    self.log_task_id,
                    LogTyp::Info,
                    &format!(
                        "LLM call rate-limit aktiv: backend '{}' pausiert {}s",
                        self.backend_id, wait_s
                    ),
                );
                self.send_status(format!(
                    "LLM rate-limit aktiv: backend '{}' wartet {}s",
                    self.backend_id, wait_s
                ))
                .await;
                tokio::time::sleep(wait).await;
            }

            let result = with_activity_heartbeat(
                &self.activity,
                self.llm.chat_with_tools_opts(
                    &self.backend_id,
                    self.backup_id.as_deref(),
                    messages,
                    tools_json,
                    &opts,
                ),
            )
            .await;

            let (response, mut raw_data) = match result {
                Ok(r) => r,
                Err(e) => {
                    // Reservation aus check_llm_cap zurueckbuchen — track_tokens
                    // laeuft hier nicht (keine Response).
                    crate::web::release_reservation(
                        &self.pipeline.store.pool,
                        self.tokens,
                        self.cfg_snap,
                        &self.model_str,
                    )
                    .await;
                    return (RoundOutcome::LlmError(e), usage);
                }
            };
            mark_activity(&self.activity);

            let input_tokens = raw_data
                .pointer("/usage/prompt_tokens")
                .and_then(|v| v.as_u64())
                .or_else(|| {
                    raw_data
                        .pointer("/prompt_eval_count")
                        .and_then(|v| v.as_u64())
                })
                .unwrap_or(0);
            let output_tokens = raw_data
                .pointer("/usage/completion_tokens")
                .and_then(|v| v.as_u64())
                .or_else(|| raw_data.pointer("/eval_count").and_then(|v| v.as_u64()))
                .unwrap_or(0);
            usage.input += input_tokens;
            usage.output += output_tokens;

            crate::web::track_tokens(
                &self.pipeline.store.pool,
                self.tokens,
                self.cfg_snap,
                &self.backend_id,
                &self.model_str,
                self.attribution_id,
                &raw_data,
            )
            .await;

            // <tool>name(params)</tool> im Response-Text → als synthetische
            // tool_calls injizieren, damit Guardrail UND Parser sie einheitlich
            // sehen. (Im alten Chat-Loop liefen Text-Tag-Calls am Guardrail
            // vorbei — Whitelist-Bypass.)
            if let Some(modul) = self.modul {
                if !self.tool_calls_disabled
                    && raw_data.pointer("/choices/0/message/tool_calls").is_none()
                {
                    if let Some((t_name, t_params)) = tools::parse_tool_call(&response) {
                        let schema = tools::schema_required_for(&t_name, modul, self.py_mods_snap);
                        let mut args = serde_json::Map::new();
                        if let Some(ref schema_keys) = schema {
                            for (i, key) in schema_keys.iter().enumerate() {
                                let val = t_params.get(i).cloned().unwrap_or_default();
                                args.insert(key.clone(), serde_json::json!(val));
                            }
                        } else {
                            // Ohne Schema: param<i>-Namen — der schema-basierte
                            // Parser findet sie nicht, Guardrail rejected als
                            // unknown tool (fail-safe, gewolltes Verhalten).
                            for (i, p) in t_params.iter().enumerate() {
                                args.insert(format!("param{}", i), serde_json::json!(p));
                            }
                        }
                        let args_str = serde_json::to_string(&args).unwrap_or("{}".into());
                        let synthetic_call = serde_json::json!({
                            "id": "call_fallback_tag",
                            "type": "function",
                            "function": {"name": t_name, "arguments": args_str},
                        });
                        if let Some(choice) = raw_data.pointer_mut("/choices/0/message") {
                            if let Some(obj) = choice.as_object_mut() {
                                obj.insert(
                                    "tool_calls".into(),
                                    serde_json::json!([synthetic_call]),
                                );
                            }
                        } else if let Some(choices) = raw_data
                            .pointer_mut("/choices")
                            .and_then(|v| v.as_array_mut())
                        {
                            if choices.is_empty() {
                                choices.push(
                                    serde_json::json!({"message": {"tool_calls": [synthetic_call]}}),
                                );
                            }
                        } else if let Some(obj) = raw_data.as_object_mut() {
                            obj.insert(
                                "choices".into(),
                                serde_json::json!([
                                    {"message": {"tool_calls": [synthetic_call]}}
                                ]),
                            );
                        }
                    }
                }
            }

            // ── Guardrail ────────────────────────────────────────────────
            if self.gcfg.enabled {
                let last_user_msg = messages
                    .iter()
                    .rev()
                    .find(|m| m["role"] == "user")
                    .map(plain_text_of_message)
                    .filter(|s| !s.trim().is_empty());
                let max_retries_for_backend = self
                    .gcfg
                    .per_backend_overrides
                    .get(&self.backend_id)
                    .copied()
                    .unwrap_or(self.gcfg.max_retries);
                let vctx = crate::guardrail::ValidatorContext {
                    modul_id: self.attribution_id,
                    cfg: self.cfg_snap,
                    py_modules: self.py_mods_snap,
                    last_user_msg: last_user_msg.as_deref(),
                    strict_mode: self.gcfg.strict_mode,
                };
                match crate::guardrail::validate_response(&raw_data, &vctx) {
                    Ok(parsed) => {
                        let ev = crate::types::GuardrailEvent {
                            ts: chrono::Utc::now().timestamp(),
                            modul: self.attribution_id.to_string(),
                            backend: self.backend_id.clone(),
                            model: self.model_str.clone(),
                            tool_name: parsed.first().map(|c| c.tool_name.clone()),
                            passed: true,
                            errors: vec![],
                            retry_attempt: self.guardrail_retries,
                            final_outcome: if self.guardrail_retries > 0 {
                                "retried".into()
                            } else {
                                "ok".into()
                            },
                            similar_suggestion: None,
                        };
                        let _ = crate::guardrail::log_event(&self.pipeline.base, &ev).await;
                        self.guardrail_retries = 0;
                    }
                    Err(errors) => {
                        let is_last = self.guardrail_retries >= max_retries_for_backend;
                        let ev = crate::types::GuardrailEvent {
                            ts: chrono::Utc::now().timestamp(),
                            modul: self.attribution_id.to_string(),
                            backend: self.backend_id.clone(),
                            model: self.model_str.clone(),
                            tool_name: None,
                            passed: false,
                            errors: errors.clone(),
                            retry_attempt: self.guardrail_retries,
                            final_outcome: if is_last {
                                "hard_fail".into()
                            } else {
                                "retried".into()
                            },
                            similar_suggestion: None,
                        };
                        let _ = crate::guardrail::log_event(&self.pipeline.base, &ev).await;
                        if is_last {
                            if self.gcfg.fallback_on_hard_fail
                                && !self.used_fallback
                                && let Some(bid) = self.backup_id.clone()
                                && let Some(bb) = self
                                    .cfg_snap
                                    .llm_backends
                                    .iter()
                                    .find(|b| b.id == bid)
                                    .cloned()
                            {
                                let codes: Vec<String> =
                                    errors.iter().map(|e| e.code.clone()).collect();
                                let _ = crate::guardrail::log_fallback_event(
                                    &self.pipeline.base,
                                    &self.backend_id,
                                    &bid,
                                    self.attribution_id,
                                    &codes,
                                )
                                .await;
                                self.backend_id = bb.id.clone();
                                self.model_str = bb.model.clone();
                                self.used_fallback = true;
                                self.guardrail_retries = 0;
                                continue; // retry mit Backup-Backend
                            }
                            let codes: Vec<String> =
                                errors.iter().map(|e| e.code.clone()).collect();
                            return (RoundOutcome::GuardrailHardFail { codes }, usage);
                        }
                        mark_activity(&self.activity);
                        // Fehlversuch in die History, BEVOR das Feedback kommt.
                        if !response.trim().is_empty() {
                            messages.push(serde_json::json!({
                                "role": "assistant", "content": &response
                            }));
                        }
                        let feedback = crate::guardrail::synth_feedback_user_message(
                            &errors,
                            max_retries_for_backend,
                            self.guardrail_retries,
                        );
                        messages.push(serde_json::json!({"role": "user", "content": feedback}));
                        self.guardrail_retries += 1;
                        continue;
                    }
                }
            }
            // ── Ende Guardrail ───────────────────────────────────────────

            let mut parsed_calls: Vec<ParsedOpenAiCall> = if self.tool_calls_disabled {
                Vec::new()
            } else if raw_data != serde_json::Value::Null {
                tools::parse_openai_tool_calls_multi(&raw_data, |name| self.schema_for(name))
            } else {
                Vec::new()
            };
            if parsed_calls.is_empty()
                && !self.tool_calls_disabled
                && let Some((name, params)) = tools::parse_tool_call(&response)
            {
                let arguments_json = raw_data
                    .pointer("/choices/0/message/tool_calls/0/function/arguments")
                    .and_then(|v| v.as_str())
                    .unwrap_or("{}")
                    .to_string();
                parsed_calls.push(ParsedOpenAiCall {
                    id: "call_fallback_tag".into(),
                    name,
                    params,
                    arguments_json,
                });
            }

            if parsed_calls.is_empty() {
                return (RoundOutcome::Final { text: response }, usage);
            }
            let raw_message = raw_data
                .pointer("/choices/0/message")
                .cloned()
                .unwrap_or_else(
                    || serde_json::json!({"role": "assistant", "content": serde_json::Value::Null}),
                );
            return (
                RoundOutcome::ToolCalls {
                    calls: parsed_calls,
                    raw_message,
                    response_text: response,
                },
                usage,
            );
        }
    }
}

/// Plain-Text aus einer Message ziehen — content kann String ODER Multimodal-
/// Array sein (Chat-UI mit Bildern).
fn plain_text_of_message(message: &serde_json::Value) -> String {
    match message.get("content") {
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(serde_json::Value::Array(parts)) => parts
            .iter()
            .filter_map(|p| {
                p.get("text")
                    .and_then(|t| t.as_str())
                    .or_else(|| p.as_str())
            })
            .collect::<Vec<_>>()
            .join(" "),
        _ => String::new(),
    }
}

/// Assistant-History-Message fuer die naechste Runde: Original-Message behalten
/// (provider-Felder wie DeepSeek reasoning_content bleiben erhalten), tool_calls
/// normalisiert auf die tatsaechlich ausgefuehrten Calls — jede Call-ID bekommt
/// genau eine role:"tool"-Antwort.
pub fn build_assistant_history(
    raw_message: &serde_json::Value,
    calls: &[ParsedOpenAiCall],
    args_for: impl Fn(&ParsedOpenAiCall) -> String,
) -> serde_json::Value {
    let mut assistant_history = raw_message.clone();
    if let Some(obj) = assistant_history.as_object_mut() {
        obj.insert("role".into(), serde_json::json!("assistant"));
        obj.entry("content").or_insert(serde_json::Value::Null);
        let calls_json: Vec<serde_json::Value> = calls
            .iter()
            .map(|c| {
                serde_json::json!({
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": args_for(c)}
                })
            })
            .collect();
        obj.insert("tool_calls".into(), serde_json::json!(calls_json));
    }
    assistant_history
}

/// Fuehrt alle Calls einer Runde aus. Read-only Calls (is_parallel_safe_tool)
/// laufen nebenlaeufig — bei Multi-Search-Runden der groesste Latenzgewinn.
/// Alles andere strikt sequenziell in Emissions-Reihenfolge.
pub async fn execute_parsed_calls<'c, F, Fut>(
    calls: &'c [ParsedOpenAiCall],
    activity: &Option<ActivityMarker>,
    exec: F,
) -> Vec<(bool, String)>
where
    F: Fn(usize, &'c ParsedOpenAiCall) -> Fut,
    Fut: Future<Output = (bool, String)>,
{
    let parallel = calls.len() > 1 && calls.iter().all(|c| tools::is_parallel_safe_tool(&c.name));
    if parallel {
        with_activity_heartbeat(
            activity,
            futures_util::future::join_all(
                calls.iter().enumerate().map(|(idx, call)| exec(idx, call)),
            ),
        )
        .await
    } else {
        let mut out = Vec::with_capacity(calls.len());
        for (idx, call) in calls.iter().enumerate() {
            out.push(with_activity_heartbeat(activity, exec(idx, call)).await);
        }
        out
    }
}

/// Haengt pro Call genau eine role:"tool"-Antwort an — in Emissions-Reihenfolge,
/// IDs passend zur Assistant-Message aus build_assistant_history.
pub fn append_tool_results(
    messages: &mut Vec<serde_json::Value>,
    calls: &[ParsedOpenAiCall],
    results: &[(bool, String)],
    format_result: impl Fn(bool, &str) -> String,
) {
    for (call, result) in calls.iter().zip(results.iter()) {
        messages.push(serde_json::json!({
            "role": "tool",
            "tool_call_id": &call.id,
            "content": format_result(result.0, &result.1),
        }));
    }
}

/// Kuerzt alte Tool-Results in der History (alles ausser den letzten
/// `keep_full` Messages, der feste Prefix bleibt unangetastet).
pub fn trim_old_tool_messages(
    messages: &mut [serde_json::Value],
    fixed_prefix: usize,
    keep_full: usize,
    max_chars: usize,
) {
    if messages.len() <= fixed_prefix + keep_full + 4 {
        return;
    }
    for i in fixed_prefix..(messages.len().saturating_sub(keep_full)) {
        if messages[i].get("role").and_then(|v| v.as_str()) == Some("tool") {
            if let Some(content) = messages[i].get("content").and_then(|v| v.as_str()) {
                if content.chars().count() > max_chars {
                    let short = format!(
                        "{}...[gekuerzt]",
                        crate::util::safe_truncate(content, max_chars)
                    );
                    messages[i]["content"] = serde_json::json!(short);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call(id: &str, name: &str) -> ParsedOpenAiCall {
        ParsedOpenAiCall {
            id: id.into(),
            name: name.into(),
            params: vec!["x".into()],
            arguments_json: "{\"q\":\"x\"}".into(),
        }
    }

    #[test]
    fn build_assistant_history_keeps_provider_fields_and_all_calls() {
        let raw = serde_json::json!({
            "role": "assistant",
            "content": "denke...",
            "reasoning_content": "internes reasoning",
        });
        let calls = vec![call("c1", "web.search"), call("c2", "rag.suchen")];
        let history = build_assistant_history(&raw, &calls, |c| c.arguments_json.clone());
        assert_eq!(history["reasoning_content"], "internes reasoning");
        assert_eq!(history["tool_calls"].as_array().unwrap().len(), 2);
        assert_eq!(history["tool_calls"][1]["function"]["name"], "rag.suchen");
    }

    #[tokio::test]
    async fn execute_parsed_calls_runs_parallel_safe_concurrently() {
        // Beide Calls schlafen 80ms — nebenlaeufig muss das deutlich unter
        // der sequenziellen Summe bleiben.
        let calls = vec![call("c1", "web.search"), call("c2", "rag.suchen")];
        let start = std::time::Instant::now();
        let results = execute_parsed_calls(&calls, &None, |idx, c| {
            let name = c.name.clone();
            async move {
                tokio::time::sleep(std::time::Duration::from_millis(80)).await;
                (true, format!("{}:{}", idx, name))
            }
        })
        .await;
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].1, "0:web.search");
        assert_eq!(results[1].1, "1:rag.suchen");
        assert!(
            start.elapsed() < std::time::Duration::from_millis(150),
            "parallel-safe Calls muessen nebenlaeufig laufen ({}ms)",
            start.elapsed().as_millis()
        );
    }

    #[tokio::test]
    async fn execute_parsed_calls_sequential_for_side_effect_tools() {
        let calls = vec![call("c1", "editor.replace"), call("c2", "web.search")];
        let start = std::time::Instant::now();
        let results = execute_parsed_calls(&calls, &None, |_, _| async {
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            (true, "ok".to_string())
        })
        .await;
        assert_eq!(results.len(), 2);
        assert!(
            start.elapsed() >= std::time::Duration::from_millis(95),
            "Seiteneffekt-Tools muessen sequenziell laufen"
        );
    }

    #[test]
    fn trim_keeps_prefix_and_recent_messages() {
        let mut messages: Vec<serde_json::Value> = vec![
            serde_json::json!({"role": "system", "content": "sys"}),
            serde_json::json!({"role": "user", "content": "frage"}),
        ];
        for i in 0..10 {
            messages.push(serde_json::json!({"role": "assistant", "content": null}));
            messages.push(serde_json::json!({
                "role": "tool", "tool_call_id": format!("c{}", i),
                "content": "X".repeat(600),
            }));
        }
        trim_old_tool_messages(&mut messages, 2, 6, 100);
        // Alte Tool-Results gekuerzt …
        assert!(
            messages[3]["content"].as_str().unwrap().len() < 200,
            "altes Tool-Result muss gekuerzt sein"
        );
        // … die letzten keep_full Messages nicht.
        let last_tool = messages.last().unwrap();
        assert_eq!(last_tool["content"].as_str().unwrap().len(), 600);
        // Prefix unangetastet
        assert_eq!(messages[1]["content"], "frage");
    }

    #[test]
    fn plain_text_of_message_handles_string_and_array() {
        assert_eq!(
            plain_text_of_message(&serde_json::json!({"content": "hallo"})),
            "hallo"
        );
        let multi = serde_json::json!({"content": [
            {"type": "text", "text": "teil1"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
            {"type": "text", "text": "teil2"},
        ]});
        assert_eq!(plain_text_of_message(&multi), "teil1 teil2");
    }
}
