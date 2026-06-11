use crate::types::{AgentConfig, LlmBackend, LlmTyp};
use futures_util::StreamExt;
use std::collections::{HashMap, HashSet};
use std::hash::{Hash, Hasher};
use std::sync::Arc;
use tokio::sync::mpsc;
use tokio::sync::{Mutex, RwLock};

/// Max Versuche pro Request fuer transiente Fehler (leerer Body, HTTP 408/429/5xx).
const OPENAI_COMPAT_TRANSIENT_RETRIES: usize = 3;

fn is_transient_http_status(status: reqwest::StatusCode) -> bool {
    matches!(status.as_u16(), 408 | 429 | 500 | 502 | 503 | 504)
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
                if let Some(max_tokens) = backend.max_tokens {
                    body["max_tokens"] = serde_json::json!(max_tokens);
                }
                apply_provider_reasoning_config(&mut body, &backend);
                let endpoint = match backend.typ {
                    LlmTyp::DeepSeek => deepseek_endpoint(&backend.url, "chat/completions"),
                    _ => openai_compat_endpoint(&backend.url, "chat/completions"),
                };
                let resp = client
                    .post(endpoint)
                    .header("Authorization", format!("Bearer {key}"))
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
                let max_tokens = backend.max_tokens.unwrap_or(4096);
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
                if let Some(max_tokens) = backend.max_tokens {
                    body["max_tokens"] = serde_json::json!(max_tokens);
                }
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
                    let resp = client
                        .post(&endpoint)
                        .header("Authorization", format!("Bearer {key}"))
                        .json(&body)
                        .send()
                        .await
                        .map_err(|e| format!("API: {e}"))?;
                    let status = resp.status();
                    let body_text = resp.text().await.unwrap_or_default();
                    if !status.is_success() {
                        // 408/429/5xx sind bei OpenRouter/DeepSeek/lokalen Servern
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
                    let content = data["choices"][0]["message"]["content"]
                        .as_str()
                        .unwrap_or("")
                        .to_string();
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
                let max_tokens = backend.max_tokens.unwrap_or(4096);
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
                    req = req.header("Authorization", format!("Bearer {key}"));
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
