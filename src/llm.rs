use crate::types::{AgentConfig, LlmBackend, LlmTyp};
use futures_util::StreamExt;
use std::collections::{HashMap, HashSet};
use std::hash::{Hash, Hasher};
use std::sync::Arc;
use tokio::sync::mpsc;
use tokio::sync::{Mutex, RwLock};

/// Max Versuche pro Request fuer transiente Fehler (leerer Body, HTTP 408/429/5xx).
const OPENAI_COMPAT_TRANSIENT_RETRIES: usize = 3;
const DEFAULT_SAFE_MAX_OUTPUT_TOKENS: u32 = 8192;
const CONTEXT_AS_OUTPUT_THRESHOLD_PERCENT: u64 = 80;

fn is_transient_http_status(status: reqwest::StatusCode) -> bool {
    matches!(status.as_u16(), 408 | 500 | 502 | 503 | 504)
}

fn retry_after_duration(headers: &reqwest::header::HeaderMap) -> Option<std::time::Duration> {
    headers
        .get(reqwest::header::RETRY_AFTER)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.trim().parse::<u64>().ok())
        .map(std::time::Duration::from_secs)
}

fn content_value_text(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Array(parts) => parts
            .iter()
            .map(content_value_text)
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(""),
        serde_json::Value::Object(obj) => obj
            .get("text")
            .or_else(|| obj.get("content"))
            .map(content_value_text)
            .unwrap_or_default(),
        _ => String::new(),
    }
}

/// Result of a `claude -p` subprocess call in plain-LLM mode.
#[derive(Debug)]
struct ClaudeCliResult {
    text: String,
    cost_usd: f64,
    input_tokens: u64,
    output_tokens: u64,
}

/// Flatten an OpenAI-style message array into (system_prompt, transcript) for the
/// claude CLI: `system` messages drive `--system-prompt`, the rest become a
/// role-tagged transcript fed on stdin. Reuses `content_value_text` so string- and
/// array-shaped content both work.
fn flatten_messages(messages: &[serde_json::Value]) -> (String, String) {
    let mut system_parts: Vec<String> = Vec::new();
    let mut convo_parts: Vec<String> = Vec::new();
    for m in messages {
        let role = m.get("role").and_then(|v| v.as_str()).unwrap_or("user");
        let text = content_value_text(m.get("content").unwrap_or(&serde_json::Value::Null));
        if text.trim().is_empty() {
            continue;
        }
        if role == "system" {
            system_parts.push(text);
        } else {
            let label = match role {
                "assistant" => "Assistant",
                "tool" => "Tool",
                _ => "User",
            };
            convo_parts.push(format!("{label}: {text}"));
        }
    }
    let mut system = system_parts.join("\n\n");
    let mut prompt = convo_parts.join("\n\n");
    // claude -p braucht einen Prompt auf stdin. Gibt es nur System-Messages,
    // werden sie zum Prompt (sonst würde der Prozess auf Eingabe warten).
    if prompt.is_empty() {
        prompt = std::mem::take(&mut system);
    }
    (system, prompt)
}

/// Parse the single JSON object emitted by `claude -p --output-format json`.
fn parse_claude_cli_json(stdout: &str) -> Result<ClaudeCliResult, String> {
    let trimmed = stdout.trim();
    if trimmed.is_empty() {
        return Err("claude -p: leere Antwort".into());
    }
    let v: serde_json::Value = serde_json::from_str(trimmed).map_err(|e| {
        format!(
            "claude -p JSON parse: {e} — Raw: {}",
            trimmed.chars().take(300).collect::<String>()
        )
    })?;
    if v.get("is_error").and_then(|b| b.as_bool()) == Some(true) {
        let sub = v.get("subtype").and_then(|s| s.as_str()).unwrap_or("error");
        let res = v.get("result").and_then(|s| s.as_str()).unwrap_or("");
        return Err(format!("claude -p Fehler ({sub}): {res}"));
    }
    let text = v
        .get("result")
        .and_then(|s| s.as_str())
        .ok_or("claude -p: kein 'result' im JSON")?
        .to_string();
    let usage = v.get("usage");
    let utok = |k: &str| {
        usage
            .and_then(|u| u.get(k))
            .and_then(|t| t.as_u64())
            .unwrap_or(0)
    };
    Ok(ClaudeCliResult {
        text,
        cost_usd: v
            .get("total_cost_usd")
            .and_then(|c| c.as_f64())
            .unwrap_or(0.0),
        input_tokens: utok("input_tokens"),
        output_tokens: utok("output_tokens"),
    })
}

/// Run `claude -p` as a plain-LLM backend: own tools off, system prompt overridden,
/// conversation on stdin, single JSON object back. No HTTP.
async fn claude_cli_chat(
    backend: &LlmBackend,
    messages: &[serde_json::Value],
) -> Result<ClaudeCliResult, String> {
    use tokio::io::AsyncWriteExt;
    let (system_prompt, prompt) = flatten_messages(messages);
    let bin = backend.url.trim();
    let bin = if bin.is_empty() { "claude" } else { bin };

    let mut cmd = tokio::process::Command::new(bin);
    cmd.arg("-p")
        .arg("--output-format")
        .arg("json")
        .arg("--model")
        .arg(&backend.model)
        // Claude-Codes eigene Tools aus → reiner Text. Tool-Calling macht die
        // Turn-Engine über Text-Parsing (parse_dsml_tool_call).
        .arg("--allowedTools")
        .arg("")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    if !system_prompt.is_empty() {
        cmd.arg("--system-prompt").arg(&system_prompt);
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("claude -p spawn ({bin}): {e}"))?;
    if let Some(ref mut stdin) = child.stdin {
        stdin
            .write_all(prompt.as_bytes())
            .await
            .map_err(|e| format!("claude -p stdin: {e}"))?;
        stdin.shutdown().await.ok();
    }

    let timeout_s = if backend.timeout_s == 0 {
        120
    } else {
        backend.timeout_s
    };
    let output = match tokio::time::timeout(
        std::time::Duration::from_secs(timeout_s),
        child.wait_with_output(),
    )
    .await
    {
        Ok(r) => r.map_err(|e| format!("claude -p Prozess: {e}"))?,
        Err(_) => return Err(format!("claude -p Timeout ({timeout_s}s) — Prozess gekillt")),
    };

    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "claude -p Exit {}: {}",
            output.status,
            err.chars().take(500).collect::<String>()
        ));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let result = parse_claude_cli_json(&stdout)?;
    tracing::debug!(
        "claude -p: {} in / {} out tokens (fiktiv ${:.4})",
        result.input_tokens,
        result.output_tokens,
        result.cost_usd
    );
    Ok(result)
}

fn openai_compat_response_text(data: &serde_json::Value) -> String {
    for path in [
        "/choices/0/message/content",
        "/choices/0/text",
        "/output_text",
        "/text",
        "/message/content",
        "/response",
    ] {
        let text = data
            .pointer(path)
            .map(content_value_text)
            .unwrap_or_default();
        if !text.trim().is_empty() {
            return text;
        }
    }
    String::new()
}

fn estimate_provider_request_tokens(
    messages: &[serde_json::Value],
    tools: &[serde_json::Value],
) -> u64 {
    let messages_chars = serde_json::to_string(messages)
        .map(|s| s.len())
        .unwrap_or(0);
    let tools_chars = serde_json::to_string(tools).map(|s| s.len()).unwrap_or(0);
    // Conservative guard estimate: provider tokenizers and JSON/tool overhead differ.
    // Counting roughly one char as one token is intentionally pessimistic so we
    // clamp before providers reject with context_length errors.
    (messages_chars + tools_chars) as u64 + (messages.len() as u64 * 16) + (tools.len() as u64 * 64)
}

fn context_safety_margin(window: u32) -> u64 {
    ((window as u64) / 50).clamp(1024, 8192)
}

fn bounded_max_tokens(
    backend: &LlmBackend,
    messages: &[serde_json::Value],
    tools: &[serde_json::Value],
    requested: u32,
) -> Result<u32, String> {
    let mut max_tokens = requested.max(1);
    if let Some(window) = backend.context_window.filter(|w| *w > 0) {
        let threshold = (window as u64 * CONTEXT_AS_OUTPUT_THRESHOLD_PERCENT) / 100;
        if max_tokens as u64 >= threshold {
            let clamped = max_tokens.min(DEFAULT_SAFE_MAX_OUTPUT_TOKENS);
            tracing::warn!(
                "LLM backend '{}' requested max_tokens {} is >= {}% of context_window {}; clamping to {}",
                backend.id,
                max_tokens,
                CONTEXT_AS_OUTPUT_THRESHOLD_PERCENT,
                window,
                clamped
            );
            max_tokens = clamped;
        }

        let input_est = estimate_provider_request_tokens(messages, tools);
        let reserve = context_safety_margin(window);
        let window = window as u64;
        if input_est + reserve >= window {
            return Err(format!(
                "Kontextfenster zu klein: geschaetzter Input ~{} Tokens + Reserve {} >= Fenster {}. Verlauf/Tool-Evidenz muss gekuerzt werden.",
                input_est, reserve, window
            ));
        }
        let available = window - input_est - reserve;
        if max_tokens as u64 > available {
            let clamped = available.max(1).min(u32::MAX as u64) as u32;
            tracing::warn!(
                "LLM backend '{}' max_tokens {} exceeds remaining context budget {}; clamping to {} (window {}, input_est {}, reserve {})",
                backend.id,
                max_tokens,
                available,
                clamped,
                window,
                input_est,
                reserve
            );
            max_tokens = clamped;
        }
    }
    Ok(max_tokens)
}

fn apply_bounded_max_tokens(
    body: &mut serde_json::Value,
    backend: &LlmBackend,
    messages: &[serde_json::Value],
    tools: &[serde_json::Value],
    requested: Option<u32>,
) -> Result<(), String> {
    let Some(requested) = requested.filter(|v| *v > 0) else {
        return Ok(());
    };
    body["max_tokens"] =
        serde_json::json!(bounded_max_tokens(backend, messages, tools, requested)?);
    Ok(())
}

/// Sende-/Verbindungsfehler von reqwest, die transient sind (Connection-Reset,
/// DNS-Hickup, Broken Pipe). Diese duerfen NICHT den ganzen Task killen — ein
/// kurzer Backoff-Retry reicht. Echte Timeouts (langer Call lief wirklich aus)
/// werden NICHT geretryt, sonst wartet man pro Versuch erneut die volle
/// Timeout-Dauer.
fn is_retryable_send_error(e: &reqwest::Error) -> bool {
    !e.is_timeout() && (e.is_connect() || e.is_request() || e.is_body())
}

pub fn openai_compat_endpoint(base_url: &str, path: &str) -> String {
    let base = base_url.trim_end_matches('/');
    let path = path.trim_start_matches('/');
    if base.ends_with("/v1") {
        format!("{}/{}", base, path)
    } else {
        format!("{}/v1/{}", base, path)
    }
}

pub fn deepseek_endpoint(base_url: &str, path: &str) -> String {
    let base = base_url.trim_end_matches('/');
    let path = path.trim_start_matches('/');
    format!("{}/{}", base, path)
}

/// Request-Optionen fuer chat_with_tools_opts.
#[derive(Debug, Default, Clone)]
pub struct ChatOptions {
    /// OpenAI-Style tool_choice ("required" | "auto" | "none"). Wird nur
    /// gesendet, wenn Tools im Request stehen und das Backend es nicht per
    /// `tool_choice_supported=false` ausschliesst (Opt-out fuer alte
    /// llama.cpp-Builds). Anthropic bekommt das Mapping required→{"type":"any"}.
    pub tool_choice: Option<String>,
}

fn apply_tool_choice(
    body: &mut serde_json::Value,
    backend: &LlmBackend,
    has_tools: bool,
    opts: &ChatOptions,
) {
    let Some(choice) = opts.tool_choice.as_deref() else {
        return;
    };
    if !has_tools || backend.tool_choice_supported == Some(false) {
        return;
    }
    match backend.typ {
        LlmTyp::Anthropic => {
            let t = match choice {
                "required" => "any",
                "none" => "none",
                _ => "auto",
            };
            body["tool_choice"] = serde_json::json!({"type": t});
        }
        // Ollama /api/chat kennt kein tool_choice.
        LlmTyp::Ollama | LlmTyp::Embedding => {}
        // DeepSeek: Thinking-Mode lehnt tool_choice mit HTTP 400 ab
        // ("Thinking mode does not support this tool_choice").
        LlmTyp::DeepSeek if deepseek_thinking_enabled(backend) => {}
        _ => {
            body["tool_choice"] = serde_json::json!(choice);
        }
    }
}

fn provider_safe_name(name: &str) -> String {
    let mut out = String::new();
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' {
            out.push(ch);
        } else {
            out.push('_');
        }
    }
    if out.is_empty() {
        out.push_str("tool");
    }
    if out.len() > 64 {
        out.truncate(64);
    }
    out
}

fn stable_short_hash(value: &str) -> String {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    value.hash(&mut hasher);
    format!("{:08x}", hasher.finish() as u32)
}

fn unique_provider_alias(name: &str, used: &mut HashSet<String>) -> String {
    let mut alias = provider_safe_name(name);
    if !used.contains(&alias) {
        used.insert(alias.clone());
        return alias;
    }
    let suffix = stable_short_hash(name);
    let max_base = 64usize.saturating_sub(suffix.len() + 1);
    let mut base = provider_safe_name(name);
    if base.len() > max_base {
        base.truncate(max_base);
    }
    alias = format!("{}_{}", base, suffix);
    let mut n = 2usize;
    while used.contains(&alias) {
        let n_suffix = format!("{}_{}", suffix, n);
        let max_base = 64usize.saturating_sub(n_suffix.len() + 1);
        let mut base = provider_safe_name(name);
        if base.len() > max_base {
            base.truncate(max_base);
        }
        alias = format!("{}_{}", base, n_suffix);
        n += 1;
    }
    used.insert(alias.clone());
    alias
}

fn provider_safe_tools(
    tools: &[serde_json::Value],
) -> (
    Vec<serde_json::Value>,
    HashMap<String, String>,
    HashMap<String, String>,
) {
    let mut used = HashSet::new();
    let mut canonical_to_alias = HashMap::new();
    let mut alias_to_canonical = HashMap::new();
    let mut out = Vec::with_capacity(tools.len());
    for tool in tools {
        let mut t = tool.clone();
        if let Some(name) = t
            .pointer("/function/name")
            .and_then(|v| v.as_str())
            .map(|v| v.to_string())
        {
            let alias = unique_provider_alias(&name, &mut used);
            canonical_to_alias.insert(name.clone(), alias.clone());
            alias_to_canonical.insert(alias.clone(), name.clone());
            if let Some(obj) = t
                .get_mut("function")
                .and_then(|function| function.as_object_mut())
            {
                obj.insert("name".into(), serde_json::json!(alias));
                // Hinweis auf den kanonischen Namen nur, wenn das Aliasing den
                // Namen tatsaechlich veraendert hat — sonst ist es reiner
                // Prompt-Bloat auf jedem Call (zaehlt bei lokalen Modellen doppelt:
                // Tokens + Prompt-Cache-Invalidierung).
                if alias != name {
                    let desc = obj
                        .get("description")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    if !desc.contains(&name) {
                        obj.insert(
                            "description".into(),
                            serde_json::json!(
                                format!("{} Internal tool id: {}", desc, name).trim()
                            ),
                        );
                    }
                }
            }
        }
        out.push(t);
    }
    (out, canonical_to_alias, alias_to_canonical)
}

fn provider_safe_messages(
    messages: &[serde_json::Value],
    canonical_to_alias: &HashMap<String, String>,
) -> Vec<serde_json::Value> {
    messages
        .iter()
        .map(|message| {
            let mut msg = message.clone();
            if let Some(obj) = msg.as_object_mut() {
                if let Some(name) = obj.get("name").and_then(|v| v.as_str()).map(str::to_string) {
                    obj.insert("name".into(), serde_json::json!(provider_safe_name(&name)));
                }
                if let Some(calls) = obj.get_mut("tool_calls").and_then(|v| v.as_array_mut()) {
                    for call in calls {
                        if let Some(function) =
                            call.get_mut("function").and_then(|v| v.as_object_mut())
                        {
                            if let Some(name) = function
                                .get("name")
                                .and_then(|v| v.as_str())
                                .map(str::to_string)
                            {
                                let alias = canonical_to_alias
                                    .get(&name)
                                    .cloned()
                                    .unwrap_or_else(|| provider_safe_name(&name));
                                function.insert("name".into(), serde_json::json!(alias));
                            }
                        }
                    }
                }
            }
            msg
        })
        .collect()
}

/// Konvertiert OpenAI-Format-History in Anthropic /v1/messages-Format.
/// Noetig fuer Multi-Turn-Tool-Calling: OpenAI nutzt role:"tool" + assistant.tool_calls,
/// Anthropic erwartet tool_result-Blocks in user-Turns und tool_use-Blocks in
/// assistant-Turns. Vorher wurden die Messages 1:1 durchgereicht — der zweite
/// Tool-Round gegen ein Anthropic-Backend schlug damit immer mit HTTP 400 fehl.
fn openai_messages_to_anthropic(messages: &[serde_json::Value]) -> Vec<serde_json::Value> {
    let mut out: Vec<serde_json::Value> = Vec::new();
    for m in messages {
        match m.get("role").and_then(|v| v.as_str()).unwrap_or("user") {
            "system" => continue, // wird separat als top-level "system" gesendet
            "tool" => {
                let block = serde_json::json!({
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id").and_then(|v| v.as_str()).unwrap_or("call_0"),
                    "content": m.get("content").and_then(|v| v.as_str()).unwrap_or(""),
                });
                // Aufeinanderfolgende tool_results in EINEM user-Turn buendeln
                // (Anthropic verlangt alle Results eines assistant-Turns im
                // direkt folgenden user-Turn).
                if let Some(last) = out.last_mut() {
                    if last["role"] == "user" && last["content"].is_array() {
                        if let Some(arr) = last["content"].as_array_mut() {
                            arr.push(block);
                            continue;
                        }
                    }
                }
                out.push(serde_json::json!({"role": "user", "content": [block]}));
            }
            "assistant" => {
                let mut blocks: Vec<serde_json::Value> = Vec::new();
                if let Some(text) = m.get("content").and_then(|v| v.as_str()) {
                    if !text.trim().is_empty() {
                        blocks.push(serde_json::json!({"type": "text", "text": text}));
                    }
                }
                if let Some(calls) = m.get("tool_calls").and_then(|v| v.as_array()) {
                    for c in calls {
                        let input: serde_json::Value = match &c["function"]["arguments"] {
                            serde_json::Value::String(s) => {
                                serde_json::from_str(s).unwrap_or_else(|_| serde_json::json!({}))
                            }
                            v if v.is_object() => v.clone(),
                            _ => serde_json::json!({}),
                        };
                        blocks.push(serde_json::json!({
                            "type": "tool_use",
                            "id": c.get("id").and_then(|v| v.as_str()).unwrap_or("call_0"),
                            "name": c["function"]["name"].as_str().unwrap_or(""),
                            "input": input,
                        }));
                    }
                }
                // Leere assistant-Messages (kein Text, keine Calls) ueberspringen —
                // Anthropic lehnt leere content-Blocks ab.
                if !blocks.is_empty() {
                    out.push(serde_json::json!({"role": "assistant", "content": blocks}));
                }
            }
            _ => out.push(m.clone()),
        }
    }
    out
}

fn apply_reasoning_config(body: &mut serde_json::Value, backend: &LlmBackend) {
    if let Some(reasoning) = backend.reasoning.as_ref().and_then(|r| r.request_json()) {
        body["reasoning"] = reasoning;
    }
}

fn normalized_reasoning_effort(effort: Option<&str>) -> Option<&'static str> {
    match effort.map(str::trim).map(str::to_lowercase).as_deref() {
        Some("none") | Some("off") | Some("disabled") => Some("none"),
        Some("minimal") => Some("minimal"),
        Some("low") => Some("low"),
        Some("medium") => Some("medium"),
        Some("high") => Some("high"),
        Some("xhigh") => Some("xhigh"),
        Some("max") => Some("max"),
        _ => None,
    }
}

fn deepseek_reasoning_effort(effort: Option<&str>) -> Option<&'static str> {
    match normalized_reasoning_effort(effort) {
        Some("xhigh") | Some("max") => Some("max"),
        Some("minimal") | Some("low") | Some("medium") | Some("high") => Some("high"),
        _ => None,
    }
}

/// True wenn der DeepSeek-Request im Thinking-Mode laeuft. Der Thinking-Mode
/// lehnt u.a. tool_choice hart mit HTTP 400 ab ("Thinking mode does not
/// support this tool_choice").
fn deepseek_thinking_enabled(backend: &LlmBackend) -> bool {
    let Some(reasoning) = backend.reasoning.as_ref() else {
        return false;
    };
    let normalized_effort = normalized_reasoning_effort(reasoning.effort.as_deref());
    if reasoning.enabled == Some(false) || normalized_effort == Some("none") {
        return false;
    }
    reasoning.enabled == Some(true)
        || reasoning.max_tokens.filter(|v| *v > 0).is_some()
        || normalized_effort.is_some()
}

fn apply_deepseek_reasoning_config(body: &mut serde_json::Value, backend: &LlmBackend) {
    let Some(reasoning) = backend.reasoning.as_ref() else {
        return;
    };
    let normalized_effort = normalized_reasoning_effort(reasoning.effort.as_deref());
    let disabled = reasoning.enabled == Some(false) || normalized_effort == Some("none");
    if disabled {
        body["thinking"] = serde_json::json!({"type": "disabled"});
        return;
    }
    let should_enable = reasoning.enabled == Some(true)
        || reasoning.max_tokens.filter(|v| *v > 0).is_some()
        || normalized_effort.is_some();
    if should_enable {
        body["thinking"] = serde_json::json!({"type": "enabled"});
    }
    if let Some(effort) = deepseek_reasoning_effort(reasoning.effort.as_deref()) {
        body["reasoning_effort"] = serde_json::json!(effort);
    }
}

fn apply_provider_reasoning_config(body: &mut serde_json::Value, backend: &LlmBackend) {
    match backend.typ {
        LlmTyp::DeepSeek => apply_deepseek_reasoning_config(body, backend),
        _ => apply_reasoning_config(body, backend),
    }
}

fn restore_provider_tool_names(
    mut data: serde_json::Value,
    alias_to_canonical: &HashMap<String, String>,
) -> serde_json::Value {
    fn restore_in_calls(
        calls: &mut [serde_json::Value],
        alias_to_canonical: &HashMap<String, String>,
    ) {
        for call in calls {
            if let Some(function) = call.get_mut("function").and_then(|v| v.as_object_mut()) {
                if let Some(name) = function
                    .get("name")
                    .and_then(|v| v.as_str())
                    .map(str::to_string)
                {
                    if let Some(canonical) = alias_to_canonical.get(&name) {
                        function.insert("name".into(), serde_json::json!(canonical));
                    }
                }
            }
        }
    }

    if let Some(calls) = data
        .pointer_mut("/choices/0/message/tool_calls")
        .and_then(|v| v.as_array_mut())
    {
        restore_in_calls(calls, alias_to_canonical);
    }
    if let Some(calls) = data
        .pointer_mut("/message/tool_calls")
        .and_then(|v| v.as_array_mut())
    {
        restore_in_calls(calls, alias_to_canonical);
    }
    data
}

pub struct LlmRouter {
    config: Arc<RwLock<AgentConfig>>,
    clients: Mutex<HashMap<u64, reqwest::Client>>, // timeout_s -> client
    call_rate: Mutex<HashMap<String, LlmCallRateState>>,
}

#[derive(Debug, Clone)]
struct LlmCallRateState {
    window_started: std::time::Instant,
    calls: u32,
}

impl LlmRouter {
    pub fn new(config: Arc<RwLock<AgentConfig>>) -> Self {
        Self {
            config,
            clients: Mutex::new(HashMap::new()),
            call_rate: Mutex::new(HashMap::new()),
        }
    }

    async fn get_client(&self, timeout_s: u64) -> Result<reqwest::Client, String> {
        let mut clients = self.clients.lock().await;
        if let Some(client) = clients.get(&timeout_s) {
            return Ok(client.clone());
        }
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(timeout_s))
            .pool_max_idle_per_host(5)
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|e| format!("Client error: {e}"))?;
        clients.insert(timeout_s, client.clone());
        Ok(client)
    }

    async fn backend(&self, id: &str) -> Option<LlmBackend> {
        let cfg = self.config.read().await;
        let mut backend = cfg.llm_backends.iter().find(|b| b.id == id).cloned()?;
        crate::util::resolve_llm_backend_api_alias(&mut backend, &cfg);
        Some(backend)
    }

    /// Reserves one API-call slot for a backend. If the configured per-LLM rate
    /// window is full, returns the remaining wait time without reserving.
    pub async fn reserve_rate_slot_or_wait(&self, backend_id: &str) -> Option<std::time::Duration> {
        let backend = self.backend(backend_id).await?;
        let (max_calls, window) = backend.call_rate_limit_effective()?;
        let now = std::time::Instant::now();
        let mut states = self.call_rate.lock().await;
        let state = states
            .entry(backend_id.to_string())
            .or_insert(LlmCallRateState {
                window_started: now,
                calls: 0,
            });
        if now.duration_since(state.window_started) >= window {
            state.window_started = now;
            state.calls = 0;
        }
        if state.calls < max_calls {
            state.calls = state.calls.saturating_add(1);
            None
        } else {
            let elapsed = now.duration_since(state.window_started);
            Some(window.saturating_sub(elapsed))
        }
    }

    /// Chat with tools (OpenAI Function Calling format)
    /// Returns: Ok((content_text, raw_response_json)) or Err
    pub async fn chat_with_tools(
        &self,
        backend_id: &str,
        backup_id: Option<&str>,
        messages: &[serde_json::Value],
        tools: &[serde_json::Value],
    ) -> Result<(String, serde_json::Value), String> {
        self.chat_with_tools_opts(
            backend_id,
            backup_id,
            messages,
            tools,
            &ChatOptions::default(),
        )
        .await
    }

    /// Wie `chat_with_tools`, mit Request-Optionen (z.B. tool_choice="required",
    /// um Recherche-Pflicht auf Protokollebene zu erzwingen statt per
    /// STOPP-Prompt-Nudging).
    pub async fn chat_with_tools_opts(
        &self,
        backend_id: &str,
        backup_id: Option<&str>,
        messages: &[serde_json::Value],
        tools: &[serde_json::Value],
        opts: &ChatOptions,
    ) -> Result<(String, serde_json::Value), String> {
        match self
            .chat_with_tools_single(backend_id, messages, tools, opts)
            .await
        {
            Ok(r) => Ok(r),
            Err(e) => {
                if let Some(bkp) = backup_id {
                    tracing::warn!("LLM {} failed, trying backup {}: {}", backend_id, bkp, e);
                    self.chat_with_tools_single(bkp, messages, tools, opts)
                        .await
                } else {
                    Err(e)
                }
            }
        }
    }

    /// Streaming chat: emits text chunks to `on_chunk` as they arrive. Returns the full
    /// accumulated text + raw JSON (for tool-call extraction) once done.
    /// Only used for the "no tools allowed" final-answer phase. Tool calls are not
    /// streamed — if the model tries one here, it comes back as text in the buffer.
    pub async fn chat_stream(
        &self,
        backend_id: &str,
        messages: &[serde_json::Value],
        on_chunk: mpsc::Sender<String>,
    ) -> Result<String, String> {
        let backend = self
            .backend(backend_id)
            .await
            .ok_or_else(|| format!("LLM Backend '{}' nicht gefunden", backend_id))?;
        crate::security::validate_llm_backend_url(&backend.typ, &backend.url)
            .map_err(|e| format!("LLM URL denied: {e}"))?;
        let client = self.get_client(backend.timeout_s).await?;

        match backend.typ {
            LlmTyp::Ollama => {
                let mut body = serde_json::json!({"model": backend.model, "messages": messages, "stream": true});
                if let Some(max_tokens) = backend.max_tokens {
                    body["options"] = serde_json::json!({"num_predict": max_tokens});
                }
                let resp = client
                    .post(format!("{}/api/chat", backend.url.trim_end_matches('/')))
                    .json(&body)
                    .send()
                    .await
                    .map_err(|e| format!("Ollama: {e}"))?;
                if !resp.status().is_success() {
                    return Err(format!("Ollama HTTP {}", resp.status()));
                }
                let mut stream = resp.bytes_stream();
                let mut accumulated = String::new();
                let mut buf = Vec::new();
                while let Some(chunk) = stream.next().await {
                    let bytes = chunk.map_err(|e| format!("stream: {e}"))?;
                    buf.extend_from_slice(&bytes);
                    while let Some(nl_pos) = buf.iter().position(|b| *b == b'\n') {
                        let line: Vec<u8> = buf.drain(..=nl_pos).collect();
                        let line = String::from_utf8_lossy(&line);
                        let line = line.trim();
                        if line.is_empty() {
                            continue;
                        }
                        if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
                            if let Some(part) =
                                v.pointer("/message/content").and_then(|v| v.as_str())
                            {
                                if !part.is_empty() {
                                    accumulated.push_str(part);
                                    let _ = on_chunk.send(part.to_string()).await;
                                }
                            }
                        }
                    }
                }
                Ok(accumulated)
            }
            LlmTyp::OpenAICompat | LlmTyp::Grok | LlmTyp::DeepSeek => {
                let key = backend.api_key.as_deref().unwrap_or("");
                let safe_messages = provider_safe_messages(messages, &HashMap::new());
                let mut body = serde_json::json!({"model": backend.model, "messages": safe_messages, "stream": true});
                apply_bounded_max_tokens(
                    &mut body,
                    &backend,
                    &safe_messages,
                    &[],
                    backend.max_tokens,
                )?;
                apply_provider_reasoning_config(&mut body, &backend);
                let endpoint = match backend.typ {
                    LlmTyp::DeepSeek => deepseek_endpoint(&backend.url, "chat/completions"),
                    _ => openai_compat_endpoint(&backend.url, "chat/completions"),
                };
                let resp = client
                    .post(endpoint)
                    .bearer_auth(crate::util::bearer_token_value(key))
                    .header("Accept", "text/event-stream")
                    .json(&body)
                    .send()
                    .await
                    .map_err(|e| format!("API: {e}"))?;
                if !resp.status().is_success() {
                    return Err(format!("API HTTP {}", resp.status()));
                }
                parse_sse_deltas(resp, on_chunk, "openai").await
            }
            LlmTyp::Anthropic => {
                let key = backend
                    .api_key
                    .as_deref()
                    .ok_or("Anthropic braucht API key")?;
                let sys = messages
                    .iter()
                    .find(|m| m["role"] == "system")
                    .and_then(|m| m["content"].as_str());
                let non_sys_raw: Vec<_> = messages
                    .iter()
                    .filter(|m| m["role"] != "system")
                    .cloned()
                    .collect();
                // History kann role:"tool"/tool_calls aus frueheren Runden enthalten —
                // muss auch im Streaming-Pfad ins Anthropic-Format konvertiert werden.
                let non_sys = openai_messages_to_anthropic(&non_sys_raw);
                let max_tokens = bounded_max_tokens(
                    &backend,
                    &non_sys,
                    &[],
                    backend.max_tokens.unwrap_or(4096),
                )?;
                let mut body = serde_json::json!({
                    "model": backend.model,
                    "max_tokens": max_tokens,
                    "messages": non_sys,
                    "stream": true,
                });
                if let Some(s) = sys {
                    body["system"] = serde_json::json!(s);
                }
                let resp = client
                    .post(format!("{}/v1/messages", backend.url))
                    .header("x-api-key", key)
                    .header("anthropic-version", "2023-06-01")
                    .json(&body)
                    .send()
                    .await
                    .map_err(|e| format!("Anthropic: {e}"))?;
                if !resp.status().is_success() {
                    return Err(format!("Anthropic HTTP {}", resp.status()));
                }
                parse_sse_deltas(resp, on_chunk, "anthropic").await
            }
            LlmTyp::ClaudeCode => {
                // Subprozess-Backend: kein echtes Streaming — Gesamttext einmalig senden.
                let r = claude_cli_chat(&backend, messages).await?;
                let _ = on_chunk.send(r.text.clone()).await;
                Ok(r.text)
            }
            LlmTyp::Embedding => Err("Embedding backend unterstützt kein Chat".into()),
        }
    }

    async fn chat_with_tools_single(
        &self,
        id: &str,
        messages: &[serde_json::Value],
        tools: &[serde_json::Value],
        opts: &ChatOptions,
    ) -> Result<(String, serde_json::Value), String> {
        let backend = self
            .backend(id)
            .await
            .ok_or_else(|| format!("LLM Backend '{}' nicht gefunden", id))?;
        crate::security::validate_llm_backend_url(&backend.typ, &backend.url)
            .map_err(|e| format!("LLM URL denied: {e}"))?;
        let client = self.get_client(backend.timeout_s).await?;
        Self::dispatch_chat(&backend, messages, tools, &client, opts).await
    }

    /// Ad-hoc variant: takes a full LlmBackend struct instead of a registry ID.
    /// Use this for backends not registered in config.llm_backends (e.g. wizard-owned backends).
    pub async fn chat_with_tools_adhoc(
        &self,
        backend: &crate::types::LlmBackend,
        messages: &[serde_json::Value],
        tools: &[serde_json::Value],
    ) -> Result<(String, serde_json::Value), String> {
        crate::security::validate_llm_backend_url(&backend.typ, &backend.url)
            .map_err(|e| format!("LLM URL denied: {e}"))?;
        let client = self.get_client(backend.timeout_s).await?;
        Self::dispatch_chat(backend, messages, tools, &client, &ChatOptions::default()).await
    }

    /// Public-Wrapper für setup_test_backend — same dispatch, aber ohne den
    /// LlmRouter-Context (brauchen wir im Setup nicht, da kein Pool).
    pub async fn dispatch_chat_public(
        backend: &LlmBackend,
        messages: &[serde_json::Value],
        tools: &[serde_json::Value],
        client: &reqwest::Client,
    ) -> Result<(String, serde_json::Value), String> {
        crate::security::validate_llm_backend_url(&backend.typ, &backend.url)
            .map_err(|e| format!("LLM URL denied: {e}"))?;
        Self::dispatch_chat(backend, messages, tools, client, &ChatOptions::default()).await
    }

    async fn dispatch_chat(
        backend: &LlmBackend,
        messages: &[serde_json::Value],
        tools: &[serde_json::Value],
        client: &reqwest::Client,
        opts: &ChatOptions,
    ) -> Result<(String, serde_json::Value), String> {
        match backend.typ {
            LlmTyp::Ollama => {
                // Ollama: tools im Ollama-Format
                let mut body = serde_json::json!({"model": backend.model, "messages": messages, "stream": false});
                if !tools.is_empty() {
                    body["tools"] = serde_json::json!(tools);
                }
                // Ollama ignoriert OpenAI-Style "max_tokens" auf /api/chat —
                // das Limit heisst dort options.num_predict. Vorher wurde
                // backend.max_tokens fuer Ollama stillschweigend verworfen.
                if let Some(max_tokens) = backend.max_tokens {
                    body["options"] = serde_json::json!({"num_predict": max_tokens});
                }
                let resp = client
                    .post(format!("{}/api/chat", backend.url.trim_end_matches('/')))
                    .json(&body)
                    .send()
                    .await
                    .map_err(|e| format!("Ollama: {e}"))?;
                let status = resp.status();
                let data: serde_json::Value = resp
                    .json()
                    .await
                    .map_err(|e| format!("Ollama parse: {e}"))?;
                if !status.is_success() {
                    return Err(format!(
                        "Ollama HTTP {}: {}",
                        status,
                        data.get("error").unwrap_or(&data)
                    ));
                }
                let content = data["message"]["content"]
                    .as_str()
                    .unwrap_or("")
                    .to_string();
                // Ollama tool_calls sind in message.tool_calls
                if data["message"]["tool_calls"].is_array() {
                    // Konvertiere Ollama-Format in OpenAI-Format fuer einheitliches Parsing
                    let converted = serde_json::json!({
                        "choices": [{"message": data["message"].clone()}],
                        "prompt_eval_count": data["prompt_eval_count"].clone(),
                        "eval_count": data["eval_count"].clone(),
                    });
                    Ok((content, converted))
                } else {
                    Ok((content, data))
                }
            }
            LlmTyp::OpenAICompat | LlmTyp::Grok | LlmTyp::DeepSeek => {
                let key = backend.api_key.as_deref().unwrap_or("");
                let (safe_tools, canonical_to_alias, alias_to_canonical) =
                    provider_safe_tools(tools);
                let safe_messages = provider_safe_messages(messages, &canonical_to_alias);
                let mut body =
                    serde_json::json!({"model": backend.model, "messages": safe_messages});
                apply_bounded_max_tokens(
                    &mut body,
                    backend,
                    &safe_messages,
                    &safe_tools,
                    backend.max_tokens,
                )?;
                apply_provider_reasoning_config(&mut body, backend);
                if !safe_tools.is_empty() {
                    body["tools"] = serde_json::json!(safe_tools);
                }
                apply_tool_choice(&mut body, backend, !safe_tools.is_empty(), opts);
                let endpoint = match backend.typ {
                    LlmTyp::DeepSeek => deepseek_endpoint(&backend.url, "chat/completions"),
                    _ => openai_compat_endpoint(&backend.url, "chat/completions"),
                };
                for attempt in 1..=OPENAI_COMPAT_TRANSIENT_RETRIES {
                    let resp = match client
                        .post(&endpoint)
                        .bearer_auth(crate::util::bearer_token_value(key))
                        .header("Accept", "application/json")
                        .json(&body)
                        .send()
                        .await
                    {
                        Ok(r) => r,
                        Err(e) => {
                            // Verbindungs-/Sende-Fehler sind transient — retry
                            // statt den ganzen DeepDive/Task wegzuwerfen.
                            if attempt < OPENAI_COMPAT_TRANSIENT_RETRIES
                                && is_retryable_send_error(&e)
                            {
                                tracing::warn!(
                                    "LLM backend '{}' send-error (attempt {}/{}): {} — retry",
                                    backend.id,
                                    attempt,
                                    OPENAI_COMPAT_TRANSIENT_RETRIES,
                                    e
                                );
                                tokio::time::sleep(std::time::Duration::from_millis(
                                    750 * attempt as u64,
                                ))
                                .await;
                                continue;
                            }
                            return Err(format!("API: {e}"));
                        }
                    };
                    let status = resp.status();
                    let retry_after = retry_after_duration(resp.headers());
                    let body_text = resp.text().await.unwrap_or_default();
                    if !status.is_success() {
                        if status.as_u16() == 429 && attempt < OPENAI_COMPAT_TRANSIENT_RETRIES {
                            // Use a sane Retry-After if present, otherwise fall back to
                            // standard backoff — a 429 with a missing/non-numeric/over-long
                            // Retry-After header must still be retried, not failed hard.
                            let wait = retry_after.filter(|d| d.as_secs() <= 30).unwrap_or_else(
                                || std::time::Duration::from_millis(750 * attempt as u64),
                            );
                            tracing::warn!(
                                "LLM backend '{}' HTTP {} — retry in {}ms (attempt {}/{})",
                                backend.id,
                                status,
                                wait.as_millis(),
                                attempt,
                                OPENAI_COMPAT_TRANSIENT_RETRIES
                            );
                            tokio::time::sleep(wait).await;
                            continue;
                        }
                        // 408/5xx sind bei OpenRouter/DeepSeek/lokalen Servern
                        // meist transient (Ueberlast, Slot busy, Gateway-Hiccup) —
                        // kurzer Backoff-Retry statt sofort den ganzen Task-Retry/
                        // Backup-Fallback anzuwerfen. 4xx-Clientfehler failen sofort.
                        if is_transient_http_status(status)
                            && attempt < OPENAI_COMPAT_TRANSIENT_RETRIES
                        {
                            tracing::warn!(
                                "LLM backend '{}' HTTP {} (attempt {}/{}) — retry",
                                backend.id,
                                status,
                                attempt,
                                OPENAI_COMPAT_TRANSIENT_RETRIES
                            );
                            tokio::time::sleep(std::time::Duration::from_millis(
                                500 * attempt as u64,
                            ))
                            .await;
                            continue;
                        }
                        return Err(format!(
                            "API HTTP {}: {}",
                            status,
                            body_text.chars().take(500).collect::<String>()
                        ));
                    }
                    if body_text.trim().is_empty() {
                        if attempt < OPENAI_COMPAT_TRANSIENT_RETRIES {
                            tracing::warn!(
                                "LLM backend '{}' returned HTTP {} with empty body (attempt {}/{})",
                                backend.id,
                                status,
                                attempt,
                                OPENAI_COMPAT_TRANSIENT_RETRIES
                            );
                            tokio::time::sleep(std::time::Duration::from_millis(
                                250 * attempt as u64,
                            ))
                            .await;
                            continue;
                        }
                        return Err(format!(
                            "API HTTP {}: empty response body after {} attempts",
                            status, OPENAI_COMPAT_TRANSIENT_RETRIES
                        ));
                    }
                    let data: serde_json::Value =
                        serde_json::from_str(&body_text).map_err(|e| format!("API parse: {e}"))?;
                    let data = restore_provider_tool_names(data, &alias_to_canonical);
                    let content = openai_compat_response_text(&data);
                    if content.trim().is_empty()
                        && data.pointer("/choices/0/message/tool_calls").is_none()
                    {
                        tracing::warn!(
                            "LLM backend '{}' returned empty assistant content: {}",
                            backend.id,
                            body_text.chars().take(600).collect::<String>()
                        );
                    }
                    return Ok((content, data));
                }
                Err("API: unreachable empty-body retry state".into())
            }
            LlmTyp::Anthropic => {
                let key = backend
                    .api_key
                    .as_deref()
                    .ok_or("Anthropic braucht API key")?;
                let sys = messages
                    .iter()
                    .find(|m| m["role"] == "system")
                    .and_then(|m| m["content"].as_str());
                let (safe_tools, canonical_to_alias, alias_to_canonical) =
                    provider_safe_tools(tools);
                let non_sys_raw: Vec<_> = messages
                    .iter()
                    .filter(|m| m["role"] != "system")
                    .cloned()
                    .collect();
                let aliased = provider_safe_messages(&non_sys_raw, &canonical_to_alias);
                let non_sys = openai_messages_to_anthropic(&aliased);
                let max_tokens = bounded_max_tokens(
                    backend,
                    &non_sys,
                    &safe_tools,
                    backend.max_tokens.unwrap_or(4096),
                )?;
                let mut body = serde_json::json!({"model": backend.model, "max_tokens": max_tokens, "messages": non_sys});

                // Prompt-Caching: Anthropic cached den System-Prompt wenn wir
                // ihn als Blocks mit cache_control=ephemeral schicken. Effekt:
                // 90% Rabatt auf die Input-Token des System-Prompts bei jedem
                // Folge-Call binnen 5 Minuten. Unser System-Prompt ist statisch
                // pro Modul (identity + tools-Beschreibung) — genau der Use-
                // Case für den Cache. Nur anwenden wenn der Prompt groß genug
                // ist (min 1024 tokens ≈ 4000 chars) — sonst zahlt der cache-
                // write-Overhead mehr als er spart.
                if let Some(s) = sys {
                    if s.len() >= 4000 {
                        body["system"] = serde_json::json!([{
                            "type": "text",
                            "text": s,
                            "cache_control": {"type": "ephemeral"},
                        }]);
                    } else {
                        body["system"] = serde_json::json!(s);
                    }
                }
                if !safe_tools.is_empty() {
                    let anthro_tools: Vec<serde_json::Value> = safe_tools
                        .iter()
                        .map(|t| {
                            serde_json::json!({
                                "name": t["function"]["name"],
                                "description": t["function"]["description"],
                                "input_schema": t["function"]["parameters"],
                            })
                        })
                        .collect();
                    body["tools"] = serde_json::json!(anthro_tools);
                }
                apply_tool_choice(&mut body, backend, !safe_tools.is_empty(), opts);
                let resp = client
                    .post(format!("{}/v1/messages", backend.url))
                    .header("x-api-key", key)
                    .header("anthropic-version", "2023-06-01")
                    .json(&body)
                    .send()
                    .await
                    .map_err(|e| format!("Anthropic: {e}"))?;
                let status = resp.status();
                let data: serde_json::Value = resp
                    .json()
                    .await
                    .map_err(|e| format!("Anthropic parse: {e}"))?;
                if !status.is_success() {
                    let err = data["error"]["message"].as_str().unwrap_or("Unknown");
                    return Err(format!("Anthropic HTTP {}: {}", status, err));
                }
                // Anthropic tool_use Blocks konvertieren in OpenAI-Format
                let mut content = String::new();
                let mut tool_calls = vec![];
                if let Some(blocks) = data["content"].as_array() {
                    for block in blocks {
                        if block["type"] == "text" {
                            content.push_str(block["text"].as_str().unwrap_or(""));
                        } else if block["type"] == "tool_use" {
                            let alias = block["name"].as_str().unwrap_or("");
                            let canonical = alias_to_canonical
                                .get(alias)
                                .map(String::as_str)
                                .unwrap_or(alias);
                            tool_calls.push(serde_json::json!({
                                "id": block["id"],
                                "type": "function",
                                "function": {
                                    "name": canonical,
                                    "arguments": serde_json::to_string(&block["input"]).unwrap_or_default(),
                                }
                            }));
                        }
                    }
                }
                if !tool_calls.is_empty() {
                    let converted = serde_json::json!({
                        "choices": [{"message": {"tool_calls": tool_calls, "content": content}}],
                        "usage": data["usage"].clone(),
                    });
                    Ok((content, converted))
                } else {
                    Ok((content, data))
                }
            }
            LlmTyp::ClaudeCode => {
                // Plain-LLM-Subprozess; Tool-Calls kommen als Text im result und
                // werden von der Turn-Engine geparst → leeres Roh-JSON genügt.
                let r = claude_cli_chat(backend, messages).await?;
                Ok((r.text, serde_json::json!({})))
            }
            LlmTyp::Embedding => Err("Embedding backend unterstützt kein Chat".into()),
        }
    }

    /// Generate embedding vector for text
    pub async fn embed(&self, backend_id: &str, text: &str) -> Result<Vec<f32>, String> {
        let backend = self
            .backend(backend_id)
            .await
            .ok_or_else(|| format!("Embedding backend '{}' nicht gefunden", backend_id))?;
        crate::security::validate_llm_backend_url(&backend.typ, &backend.url)
            .map_err(|e| format!("Embedding URL denied: {e}"))?;
        let client = self.get_client(backend.timeout_s).await?;

        match backend.typ {
            LlmTyp::Embedding | LlmTyp::OpenAICompat | LlmTyp::Grok => {
                // OpenAI-compatible: POST /v1/embeddings
                let mut req = client
                    .post(openai_compat_endpoint(&backend.url, "embeddings"))
                    .json(&serde_json::json!({"model": backend.model, "input": text}));
                if let Some(key) = &backend.api_key {
                    let token = crate::util::bearer_token_value(key);
                    if !token.is_empty() {
                        req = req.bearer_auth(token);
                    }
                }
                let resp = req.send().await.map_err(|e| format!("Embed: {e}"))?;
                let status = resp.status();
                let data: serde_json::Value =
                    resp.json().await.map_err(|e| format!("Embed parse: {e}"))?;
                if !status.is_success() {
                    let err = data["error"]["message"].as_str().unwrap_or("Unknown");
                    return Err(format!("Embed HTTP {}: {}", status, err));
                }
                data["data"][0]["embedding"]
                    .as_array()
                    .ok_or_else(|| "No embedding in response".to_string())?
                    .iter()
                    .map(|v| {
                        v.as_f64()
                            .map(|f| f as f32)
                            .ok_or_else(|| "Invalid embedding value".to_string())
                    })
                    .collect()
            }
            LlmTyp::Ollama => {
                // Ollama: POST /api/embeddings (older) or /api/embed (newer)
                let body = serde_json::json!({"model": backend.model, "prompt": text});
                let resp = client
                    .post(format!(
                        "{}/api/embeddings",
                        backend.url.trim_end_matches('/')
                    ))
                    .json(&body)
                    .send()
                    .await
                    .map_err(|e| format!("Embed: {e}"))?;
                let data: serde_json::Value =
                    resp.json().await.map_err(|e| format!("Embed parse: {e}"))?;
                data["embedding"]
                    .as_array()
                    .ok_or_else(|| "No embedding in Ollama response".to_string())?
                    .iter()
                    .map(|v| {
                        v.as_f64()
                            .map(|f| f as f32)
                            .ok_or_else(|| "Invalid value".to_string())
                    })
                    .collect()
            }
            LlmTyp::Anthropic => Err("Anthropic does not support embeddings directly".to_string()),
            LlmTyp::DeepSeek => Err("DeepSeek does not support embeddings directly".to_string()),
            LlmTyp::ClaudeCode => Err("claude -p unterstützt keine Embeddings".to_string()),
        }
    }
}

/// Parse SSE "data: {...}\n\n" stream and forward delta text via on_chunk.
/// Format parameter: "openai" or "anthropic" (different event shapes).
async fn parse_sse_deltas(
    resp: reqwest::Response,
    on_chunk: mpsc::Sender<String>,
    format: &str,
) -> Result<String, String> {
    let mut stream = resp.bytes_stream();
    let mut accumulated = String::new();
    let mut buf = Vec::new();
    while let Some(chunk) = stream.next().await {
        let bytes = chunk.map_err(|e| format!("stream: {e}"))?;
        buf.extend_from_slice(&bytes);
        // SSE events end with \n\n. Process complete events.
        while let Some(end_pos) = find_subseq(&buf, b"\n\n") {
            let event: Vec<u8> = buf.drain(..end_pos + 2).collect();
            let event_str = String::from_utf8_lossy(&event);
            for line in event_str.lines() {
                let line = line.trim();
                if !line.starts_with("data: ") {
                    continue;
                }
                let data = &line[6..];
                if data == "[DONE]" {
                    return Ok(accumulated);
                }
                let v: serde_json::Value = match serde_json::from_str(data) {
                    Ok(v) => v,
                    Err(_) => continue,
                };
                let part = match format {
                    "openai" => v
                        .pointer("/choices/0/delta/content")
                        .and_then(|x| x.as_str()),
                    "anthropic" => {
                        // content_block_delta events carry delta.text
                        if v.get("type").and_then(|t| t.as_str()) == Some("content_block_delta") {
                            v.pointer("/delta/text").and_then(|x| x.as_str())
                        } else {
                            None
                        }
                    }
                    _ => None,
                };
                if let Some(part) = part {
                    if !part.is_empty() {
                        accumulated.push_str(part);
                        let _ = on_chunk.send(part.to_string()).await;
                    }
                }
            }
        }
    }
    Ok(accumulated)
}

fn find_subseq(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use tokio::sync::RwLock;

    #[test]
    fn test_flatten_messages_splits_system_and_tags_roles() {
        let msgs = vec![
            serde_json::json!({"role": "system", "content": "Du bist Bob."}),
            serde_json::json!({"role": "user", "content": "Hallo"}),
            serde_json::json!({"role": "assistant", "content": "Hi!"}),
            serde_json::json!({"role": "tool", "content": "result=42"}),
        ];
        let (system, prompt) = flatten_messages(&msgs);
        assert_eq!(system, "Du bist Bob.");
        assert_eq!(prompt, "User: Hallo\n\nAssistant: Hi!\n\nTool: result=42");
    }

    #[test]
    fn test_flatten_messages_handles_array_content_and_skips_empty() {
        let msgs = vec![
            serde_json::json!({"role": "user", "content": [
                {"type": "text", "text": "Teil1"},
                {"type": "text", "text": "Teil2"}
            ]}),
            serde_json::json!({"role": "assistant", "content": ""}),
        ];
        let (system, prompt) = flatten_messages(&msgs);
        assert_eq!(system, "");
        // content_value_text joins array text parts; empty assistant turn is skipped.
        assert_eq!(prompt, "User: Teil1Teil2");
    }

    #[test]
    fn test_flatten_messages_system_only_becomes_prompt() {
        let msgs = vec![serde_json::json!({"role": "system", "content": "nur system"})];
        let (system, prompt) = flatten_messages(&msgs);
        assert_eq!(system, "");
        assert_eq!(prompt, "nur system");
    }

    #[test]
    fn test_parse_claude_cli_json_success() {
        let out = r#"{"subtype":"success","is_error":false,"result":"42","total_cost_usd":0.05,"usage":{"input_tokens":3,"output_tokens":5}}"#;
        let r = parse_claude_cli_json(out).unwrap();
        assert_eq!(r.text, "42");
        assert_eq!(r.input_tokens, 3);
        assert_eq!(r.output_tokens, 5);
        assert!((r.cost_usd - 0.05).abs() < 1e-9);
    }

    #[test]
    fn test_parse_claude_cli_json_error_flag() {
        let out = r#"{"is_error":true,"subtype":"error_max_turns","result":"too many turns"}"#;
        let err = parse_claude_cli_json(out).unwrap_err();
        assert!(err.contains("error_max_turns"));
        assert!(err.contains("too many turns"));
    }

    #[test]
    fn test_parse_claude_cli_json_garbage_and_missing_result() {
        assert!(parse_claude_cli_json("not json at all").is_err());
        assert!(parse_claude_cli_json("").is_err());
        assert!(parse_claude_cli_json(r#"{"is_error":false}"#).is_err());
    }

    #[test]
    fn test_openai_compat_endpoint_accepts_base_with_or_without_v1() {
        assert_eq!(
            openai_compat_endpoint("http://llm-box:8080", "chat/completions"),
            "http://llm-box:8080/v1/chat/completions"
        );
        assert_eq!(
            openai_compat_endpoint("http://llm-box:8080/v1/", "/models"),
            "http://llm-box:8080/v1/models"
        );
        assert_eq!(
            openai_compat_endpoint("https://openrouter.ai/api", "models"),
            "https://openrouter.ai/api/v1/models"
        );
        assert_eq!(
            deepseek_endpoint("https://api.deepseek.com", "chat/completions"),
            "https://api.deepseek.com/chat/completions"
        );
    }

    #[test]
    fn test_deepseek_reasoning_mapping() {
        let mut body = serde_json::json!({"model": "deepseek-v4-pro", "messages": []});
        let backend = LlmBackend {
            id: "deepseek".into(),
            name: "DeepSeek".into(),
            typ: LlmTyp::DeepSeek,
            url: "https://api.deepseek.com".into(),
            api_key: Some("x".into()),
            model: "deepseek-v4-pro".into(),
            timeout_s: 1,
            identity: crate::types::ModulIdentity::default(),
            max_tokens: None,
            reasoning: Some(crate::types::LlmReasoningConfig {
                enabled: None,
                effort: Some("xhigh".into()),
                max_tokens: None,
                exclude: Some(true),
            }),
            cost_cap: None,
            max_tool_rounds: None,
            call_rate_limit: None,
            internal: false,
            tool_choice_supported: None,
            context_window: None,
        };
        apply_provider_reasoning_config(&mut body, &backend);
        assert_eq!(body["thinking"]["type"], "enabled");
        assert_eq!(body["reasoning_effort"], "max");

        let mut body = serde_json::json!({});
        let mut backend = backend;
        backend.reasoning = Some(crate::types::LlmReasoningConfig {
            enabled: Some(false),
            effort: None,
            max_tokens: None,
            exclude: None,
        });
        apply_provider_reasoning_config(&mut body, &backend);
        assert_eq!(body["thinking"]["type"], "disabled");
        assert!(body.get("reasoning_effort").is_none());
    }

    #[test]
    fn test_provider_safe_tools_alias_dotted_names_and_restore_response() {
        let tools = vec![
            serde_json::json!({
                "type": "function",
                "function": {
                    "name": "rag.suchen",
                    "description": "RAG Suche",
                    "parameters": {"type": "object", "properties": {}}
                }
            }),
            serde_json::json!({
                "type": "function",
                "function": {
                    "name": "rag_suchen",
                    "description": "Collision",
                    "parameters": {"type": "object", "properties": {}}
                }
            }),
        ];

        let (safe_tools, canonical_to_alias, alias_to_canonical) = provider_safe_tools(&tools);
        let alias = canonical_to_alias.get("rag.suchen").unwrap();
        assert_eq!(alias, "rag_suchen");
        assert_ne!(
            safe_tools[0]
                .pointer("/function/name")
                .and_then(|v| v.as_str()),
            safe_tools[1]
                .pointer("/function/name")
                .and_then(|v| v.as_str())
        );

        let messages = vec![serde_json::json!({
            "role": "assistant",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "rag.suchen", "arguments": "{}"}
            }]
        })];
        let safe_messages = provider_safe_messages(&messages, &canonical_to_alias);
        assert_eq!(
            safe_messages[0]
                .pointer("/tool_calls/0/function/name")
                .and_then(|v| v.as_str()),
            Some(alias.as_str())
        );

        let raw_response = serde_json::json!({
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": alias, "arguments": "{}"}
                    }]
                }
            }]
        });
        let restored = restore_provider_tool_names(raw_response, &alias_to_canonical);
        assert_eq!(
            restored
                .pointer("/choices/0/message/tool_calls/0/function/name")
                .and_then(|v| v.as_str()),
            Some("rag.suchen")
        );
    }

    #[test]
    fn openai_compat_response_text_accepts_string_array_and_text_fallbacks() {
        let plain = serde_json::json!({
            "choices": [{"message": {"content": "Hallo"}}]
        });
        assert_eq!(openai_compat_response_text(&plain), "Hallo");

        let parts = serde_json::json!({
            "choices": [{"message": {"content": [
                {"type": "text", "text": "Hal"},
                {"type": "text", "text": "lo"}
            ]}}]
        });
        assert_eq!(openai_compat_response_text(&parts), "Hallo");

        let text_choice = serde_json::json!({
            "choices": [{"text": "Fallback"}]
        });
        assert_eq!(openai_compat_response_text(&text_choice), "Fallback");
    }

    #[test]
    fn bounded_max_tokens_clamps_context_sized_output_requests() {
        let backend = LlmBackend {
            id: "openrouter".into(),
            name: "OpenRouter".into(),
            typ: LlmTyp::OpenAICompat,
            url: "https://openrouter.ai/api".into(),
            api_key: Some("x".into()),
            model: "m".into(),
            timeout_s: 1,
            identity: Default::default(),
            max_tokens: Some(240_144),
            reasoning: None,
            cost_cap: None,
            max_tool_rounds: None,
            call_rate_limit: None,
            internal: false,
            tool_choice_supported: None,
            context_window: Some(240_144),
        };
        let messages = vec![serde_json::json!({"role": "user", "content": "kurz"})];

        assert_eq!(
            bounded_max_tokens(&backend, &messages, &[], 240_144).unwrap(),
            DEFAULT_SAFE_MAX_OUTPUT_TOKENS
        );
    }

    #[test]
    fn bounded_max_tokens_rejects_input_that_exhausts_context() {
        let backend = LlmBackend {
            id: "small".into(),
            name: "small".into(),
            typ: LlmTyp::OpenAICompat,
            url: "https://example.test/v1".into(),
            api_key: Some("x".into()),
            model: "m".into(),
            timeout_s: 1,
            identity: Default::default(),
            max_tokens: Some(100),
            reasoning: None,
            cost_cap: None,
            max_tool_rounds: None,
            call_rate_limit: None,
            internal: false,
            tool_choice_supported: None,
            context_window: Some(2000),
        };
        let messages = vec![serde_json::json!({"role": "user", "content": "x".repeat(2500)})];

        assert!(bounded_max_tokens(&backend, &messages, &[], 100).is_err());
    }

    #[test]
    fn test_openai_messages_to_anthropic_converts_tool_history() {
        let messages = vec![
            serde_json::json!({"role": "system", "content": "sys"}),
            serde_json::json!({"role": "user", "content": "frage"}),
            serde_json::json!({"role": "assistant", "content": "ich suche", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "web_search", "arguments": "{\"query\":\"x\"}"}},
                {"id": "call_2", "type": "function",
                 "function": {"name": "rag_suchen", "arguments": "{\"query\":\"y\"}"}}
            ]}),
            serde_json::json!({"role": "tool", "tool_call_id": "call_1", "content": "resultat 1"}),
            serde_json::json!({"role": "tool", "tool_call_id": "call_2", "content": "resultat 2"}),
            serde_json::json!({"role": "assistant", "content": ""}),
        ];
        let out = openai_messages_to_anthropic(&messages);
        // system raus, leere assistant-Message raus → user, assistant, user(tool_results)
        assert_eq!(out.len(), 3);
        assert_eq!(out[0]["role"], "user");
        assert_eq!(out[1]["role"], "assistant");
        let blocks = out[1]["content"].as_array().unwrap();
        assert_eq!(blocks[0]["type"], "text");
        assert_eq!(blocks[1]["type"], "tool_use");
        assert_eq!(blocks[1]["id"], "call_1");
        assert_eq!(blocks[1]["input"]["query"], "x");
        assert_eq!(blocks[2]["type"], "tool_use");
        // Beide tool_results landen gebuendelt im EINEN folgenden user-Turn
        assert_eq!(out[2]["role"], "user");
        let results = out[2]["content"].as_array().unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(results[0]["type"], "tool_result");
        assert_eq!(results[0]["tool_use_id"], "call_1");
        assert_eq!(results[1]["tool_use_id"], "call_2");
    }

    #[test]
    fn test_apply_tool_choice_per_provider() {
        let mut backend = LlmBackend {
            id: "b".into(),
            name: "b".into(),
            typ: LlmTyp::OpenAICompat,
            url: "http://x".into(),
            api_key: None,
            model: "m".into(),
            timeout_s: 1,
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
        let opts = ChatOptions {
            tool_choice: Some("required".into()),
        };

        // OpenAI-compat: String-Wert
        let mut body = serde_json::json!({});
        apply_tool_choice(&mut body, &backend, true, &opts);
        assert_eq!(body["tool_choice"], "required");

        // Ohne Tools: nichts senden
        let mut body = serde_json::json!({});
        apply_tool_choice(&mut body, &backend, false, &opts);
        assert!(body.get("tool_choice").is_none());

        // Backend-Opt-out
        backend.tool_choice_supported = Some(false);
        let mut body = serde_json::json!({});
        apply_tool_choice(&mut body, &backend, true, &opts);
        assert!(body.get("tool_choice").is_none());
        backend.tool_choice_supported = None;

        // Anthropic: required → {"type":"any"}
        backend.typ = LlmTyp::Anthropic;
        let mut body = serde_json::json!({});
        apply_tool_choice(&mut body, &backend, true, &opts);
        assert_eq!(body["tool_choice"]["type"], "any");

        // Ollama: kein tool_choice
        backend.typ = LlmTyp::Ollama;
        let mut body = serde_json::json!({});
        apply_tool_choice(&mut body, &backend, true, &opts);
        assert!(body.get("tool_choice").is_none());

        // DeepSeek MIT Thinking (effort gesetzt): kein tool_choice (HTTP-400-Schutz)
        backend.typ = LlmTyp::DeepSeek;
        backend.reasoning = Some(crate::types::LlmReasoningConfig {
            enabled: None,
            effort: Some("xhigh".into()),
            max_tokens: None,
            exclude: Some(true),
        });
        let mut body = serde_json::json!({});
        apply_tool_choice(&mut body, &backend, true, &opts);
        assert!(body.get("tool_choice").is_none());

        // DeepSeek OHNE Thinking: tool_choice erlaubt
        backend.reasoning = Some(crate::types::LlmReasoningConfig {
            enabled: Some(false),
            effort: None,
            max_tokens: None,
            exclude: None,
        });
        let mut body = serde_json::json!({});
        apply_tool_choice(&mut body, &backend, true, &opts);
        assert_eq!(body["tool_choice"], "required");
    }

    #[tokio::test]
    async fn test_adhoc_returns_err_on_unreachable_backend() {
        use crate::types::{LlmBackend, LlmTyp, ModulIdentity};
        let cfg = Arc::new(RwLock::new(crate::types::AgentConfig::default()));
        let router = LlmRouter::new(cfg);
        let backend = LlmBackend {
            id: "test".into(),
            name: "test".into(),
            typ: LlmTyp::OpenAICompat,
            url: "http://127.0.0.1:1/v1".into(),
            api_key: Some("x".into()),
            model: "dummy".into(),
            timeout_s: 1,
            identity: ModulIdentity::default(),
            max_tokens: None,
            reasoning: None,
            cost_cap: None,
            max_tool_rounds: None,
            call_rate_limit: None,
            internal: false,
            tool_choice_supported: None,
            context_window: None,
        };
        let r = router.chat_with_tools_adhoc(&backend, &[], &[]).await;
        assert!(
            r.is_err(),
            "expected Err when backend is unreachable (port 1 always refuses)"
        );
    }
}
