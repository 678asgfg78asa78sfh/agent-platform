// src/wizard.rs — Conversational agent-creation wizard.
// Owns session lifecycle, tool handlers, and the `validate_for_commit` invariants.
// LLM communication goes through `crate::llm::LlmRouter::chat_with_tools_adhoc`.

use std::path::PathBuf;

use crate::types::{AgentConfig, DraftAgent, ValidationError, WizardMessage, WizardMode, WizardSession, WizardToolCall};

// ─── Session storage paths ─────────────────────────

pub fn sessions_dir(data_root: &std::path::Path) -> PathBuf {
    data_root.join("wizard-sessions")
}

pub fn archived_dir(data_root: &std::path::Path) -> PathBuf {
    data_root.join("wizard-sessions").join("archived")
}

pub fn session_path(data_root: &std::path::Path, session_id: &str) -> PathBuf {
    sessions_dir(data_root).join(format!("{}.json", session_id))
}

pub async fn ensure_dirs(data_root: &std::path::Path) -> std::io::Result<()> {
    tokio::fs::create_dir_all(sessions_dir(data_root)).await?;
    tokio::fs::create_dir_all(archived_dir(data_root)).await?;
    Ok(())
}

// ─── Session ID generation ─────────────────────────

/// Crypto-random 128-bit session ID, base64url-encoded (22 chars, no padding).
pub fn new_session_id() -> String {
    use rand::RngExt;
    let mut bytes = [0u8; 16];
    rand::rng().fill(&mut bytes);
    base64_url_encode(&bytes)
}

fn base64_url_encode(bytes: &[u8]) -> String {
    const CHARSET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity((bytes.len() * 4 + 2) / 3);
    let mut i = 0;
    while i + 3 <= bytes.len() {
        let b0 = bytes[i] as u32;
        let b1 = bytes[i + 1] as u32;
        let b2 = bytes[i + 2] as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(CHARSET[((n >> 18) & 63) as usize] as char);
        out.push(CHARSET[((n >> 12) & 63) as usize] as char);
        out.push(CHARSET[((n >> 6) & 63) as usize] as char);
        out.push(CHARSET[(n & 63) as usize] as char);
        i += 3;
    }
    if i < bytes.len() {
        let mut n = (bytes[i] as u32) << 16;
        if i + 1 < bytes.len() {
            n |= (bytes[i + 1] as u32) << 8;
        }
        out.push(CHARSET[((n >> 18) & 63) as usize] as char);
        out.push(CHARSET[((n >> 12) & 63) as usize] as char);
        if i + 1 < bytes.len() {
            out.push(CHARSET[((n >> 6) & 63) as usize] as char);
        }
    }
    out
}

// ─── Session persistence ───────────────────────────

pub async fn load_session(data_root: &std::path::Path, session_id: &str) -> Option<WizardSession> {
    let path = session_path(data_root, session_id);
    let raw = tokio::fs::read(&path).await.ok()?;
    serde_json::from_slice::<WizardSession>(&raw).ok()
}

pub async fn save_session(data_root: &std::path::Path, session: &WizardSession) -> std::io::Result<()> {
    let path = session_path(data_root, &session.session_id);
    let json = serde_json::to_vec_pretty(session)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    let tmp = path.with_extension("json.tmp");
    tokio::fs::write(&tmp, &json).await?;
    tokio::fs::rename(&tmp, &path).await?;
    Ok(())
}

pub async fn archive_session(data_root: &std::path::Path, session_id: &str) -> std::io::Result<()> {
    let src = session_path(data_root, session_id);
    let ts = chrono::Utc::now().format("%Y%m%d-%H%M%S");
    let dest = archived_dir(data_root).join(format!("{}-{}.json", session_id, ts));
    match tokio::fs::rename(&src, &dest).await {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

pub async fn delete_session(data_root: &std::path::Path, session_id: &str) -> std::io::Result<()> {
    let path = session_path(data_root, session_id);
    match tokio::fs::remove_file(&path).await {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

pub async fn list_active_sessions(data_root: &std::path::Path) -> Vec<WizardSession> {
    let dir = sessions_dir(data_root);
    let mut out = Vec::new();
    let mut entries = match tokio::fs::read_dir(&dir).await {
        Ok(e) => e,
        Err(_) => return out,
    };
    while let Ok(Some(entry)) = entries.next_entry().await {
        if entry.file_type().await.map(|t| t.is_file()).unwrap_or(false) {
            let path = entry.path();
            if path.extension().and_then(|s| s.to_str()) == Some("json") {
                if let Ok(bytes) = tokio::fs::read(&path).await {
                    if let Ok(s) = serde_json::from_slice::<WizardSession>(&bytes) {
                        out.push(s);
                    }
                }
            }
        }
    }
    out.sort_by_key(|s| -s.last_activity);
    out
}

// ─── Cleanup: expired sessions ─────────────────────

/// Delete sessions older than `timeout_secs` (based on last_activity).
/// Returns count of deleted sessions.
pub async fn cleanup_expired(
    data_root: &std::path::Path,
    timeout_secs: u64,
) -> usize {
    let cutoff = chrono::Utc::now().timestamp() - (timeout_secs as i64);
    let mut count = 0;
    for s in list_active_sessions(data_root).await {
        if s.last_activity < cutoff {
            if delete_session(data_root, &s.session_id).await.is_ok() {
                count += 1;
            }
        }
    }
    count
}

// ─── Commit validation ─────────────────────────────

const KNOWN_TYPES: &[&str] = &["chat", "filesystem", "websearch", "shell", "notify", "cron"];

pub fn validate_for_commit(
    draft: &DraftAgent,
    cfg: &AgentConfig,
    mode: &WizardMode,
) -> Result<(), Vec<ValidationError>> {
    let mut errs = Vec::new();

    // 1. id present and well-formed
    let id = match draft.id.as_deref() {
        None | Some("") => {
            errs.push(ValidationError {
                field: "id".into(),
                code: "missing".into(),
                human_message_de: "Der Agent braucht eine eindeutige ID (z.B. chat.roland).".into(),
            });
            ""
        }
        Some(v) => v,
    };
    if !id.is_empty() {
        if id.len() > 64 {
            errs.push(ValidationError {
                field: "id".into(), code: "too_long".into(),
                human_message_de: "ID darf höchstens 64 Zeichen haben.".into(),
            });
        }
        if !id_regex_ok(id) {
            errs.push(ValidationError {
                field: "id".into(), code: "invalid_format".into(),
                human_message_de: "ID darf nur aus a-z, 0-9, '.', '_', '-' bestehen und muss mit einem Buchstaben beginnen.".into(),
            });
        }
        let allow_same = matches!(mode, WizardMode::Edit { target_id } if target_id == id);
        if !allow_same && cfg.module.iter().any(|m| m.id == id) {
            errs.push(ValidationError {
                field: "id".into(), code: "collision".into(),
                human_message_de: format!("Ein Agent mit der ID '{}' existiert schon.", id),
            });
        }
    }

    // 2. typ
    match draft.typ.as_deref() {
        None | Some("") => errs.push(ValidationError {
            field: "typ".into(), code: "missing".into(),
            human_message_de: "Der Agent braucht einen Typ (chat, filesystem, websearch, shell, notify, cron).".into(),
        }),
        Some(t) if !KNOWN_TYPES.contains(&t) => errs.push(ValidationError {
            field: "typ".into(), code: "unknown_type".into(),
            human_message_de: format!("Unbekannter Typ '{}'. Erlaubt: {}", t, KNOWN_TYPES.join(", ")),
        }),
        _ => {}
    }

    // 3. llm_backend required for chat
    if draft.typ.as_deref() == Some("chat") {
        match draft.llm_backend.as_deref() {
            None | Some("") => errs.push(ValidationError {
                field: "llm_backend".into(), code: "missing".into(),
                human_message_de: "Chat-Agenten brauchen einen LLM-Backend.".into(),
            }),
            Some(b) if !cfg.llm_backends.iter().any(|x| x.id == b) => errs.push(ValidationError {
                field: "llm_backend".into(), code: "unknown_backend".into(),
                human_message_de: format!("LLM-Backend '{}' ist nicht konfiguriert.", b),
            }),
            _ => {}
        }
    }

    // 4. ranges
    if let Some(tb) = draft.token_budget {
        if tb == 0 {
            errs.push(ValidationError { field: "token_budget".into(), code: "out_of_range".into(),
                human_message_de: "token_budget muss > 0 sein.".into() });
        }
    }
    if let Some(si) = draft.scheduler_interval_ms {
        if si < 500 {
            errs.push(ValidationError { field: "scheduler_interval_ms".into(), code: "out_of_range".into(),
                human_message_de: "scheduler_interval_ms muss >= 500 sein.".into() });
        }
    }
    if let Some(mc) = draft.max_concurrent_tasks {
        if mc == 0 {
            errs.push(ValidationError { field: "max_concurrent_tasks".into(), code: "out_of_range".into(),
                human_message_de: "max_concurrent_tasks muss >= 1 sein.".into() });
        }
    }

    // 5. linked_modules exist
    for lm in &draft.linked_modules {
        if !cfg.module.iter().any(|m| &m.id == lm) {
            errs.push(ValidationError {
                field: "linked_modules".into(), code: "unknown_module".into(),
                human_message_de: format!("Verlinktes Modul '{}' existiert nicht.", lm),
            });
        }
    }

    // 6. berechtigungen: subset of derivable permissions
    let allowed = derive_allowed_permissions(&draft.linked_modules, draft.typ.as_deref(), cfg);
    for p in &draft.berechtigungen {
        if !allowed.contains(p.as_str()) {
            errs.push(ValidationError {
                field: "berechtigungen".into(), code: "not_allowed".into(),
                human_message_de: format!("Berechtigung '{}' ist nicht aus den verlinkten Modulen ableitbar.", p),
            });
        }
    }

    // 7. identity.bot_name, system_prompt
    match draft.identity.bot_name.as_deref() {
        None | Some("") => errs.push(ValidationError {
            field: "identity.bot_name".into(), code: "missing".into(),
            human_message_de: "Der Agent braucht einen Namen.".into(),
        }),
        Some(n) if n.len() > 64 => errs.push(ValidationError {
            field: "identity.bot_name".into(), code: "too_long".into(),
            human_message_de: "Name darf höchstens 64 Zeichen haben.".into(),
        }),
        _ => {}
    }
    match draft.identity.system_prompt.as_deref() {
        None | Some("") => errs.push(ValidationError {
            field: "identity.system_prompt".into(), code: "missing".into(),
            human_message_de: "Der System-Prompt darf nicht leer sein.".into(),
        }),
        Some(p) if p.chars().count() > 20_000 => errs.push(ValidationError {
            field: "identity.system_prompt".into(), code: "too_long".into(),
            human_message_de: "System-Prompt darf höchstens 20.000 Zeichen haben.".into(),
        }),
        _ => {}
    }

    if errs.is_empty() { Ok(()) } else { Err(errs) }
}

fn id_regex_ok(id: &str) -> bool {
    let mut chars = id.chars();
    match chars.next() {
        Some(c) if c.is_ascii_lowercase() => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '.' || c == '_' || c == '-')
}

fn derive_allowed_permissions(
    linked: &[String],
    typ: Option<&str>,
    cfg: &AgentConfig,
) -> std::collections::HashSet<&'static str> {
    use std::collections::HashSet;
    let mut out: HashSet<&'static str> = HashSet::new();
    out.insert("aufgaben");
    out.insert("rag.shared");
    out.insert("rag.private");
    for lm in linked {
        if let Some(m) = cfg.module.iter().find(|m| &m.id == lm) {
            match m.typ.as_str() {
                "filesystem" => { out.insert("files"); }
                "websearch"  => { out.insert("web"); }
                "shell"      => { out.insert("shell"); }
                "notify"     => { out.insert("notify"); }
                _ => {}
            }
        }
    }
    if typ == Some("cron") { out.insert("cron.fire"); }
    out
}

// ─── Tool descriptors ─────────────────────────────────

/// OpenAI-function-calling-compatible tool descriptors. Used in every chat call.
/// When `allow_code_gen` is true, the `wizard.create_py_module` tool is included.
pub fn wizard_tool_descriptors(allow_code_gen: bool) -> serde_json::Value {
    let mut tools = vec![
        serde_json::json!({
            "type": "function",
            "function": {
                "name": "wizard.propose",
                "description": "Schlägt einen Wert für ein Feld des DraftAgent vor. Patcht den Draft-State.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "description": "z.B. 'id', 'identity.bot_name', 'linked_modules'"},
                        "value": {"type": ["string", "number", "boolean", "array", "object", "null"], "description": "Neuer Wert (JSON, beliebiger Typ je Feld)."},
                        "reasoning": {"type": "string", "description": "Kurze Begründung für den User."}
                    },
                    "required": ["field", "value", "reasoning"]
                }
            }
        }),
        serde_json::json!({
            "type": "function",
            "function": {
                "name": "wizard.ask",
                "description": "Stellt dem User eine strukturierte Frage, ohne State zu ändern.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}, "description": "Optionale Antwortvorschläge."}
                    },
                    "required": ["question"]
                }
            }
        }),
        serde_json::json!({
            "type": "function",
            "function": {
                "name": "wizard.list_modules",
                "description": "Listet existierende Module (id, typ, bot_name, linked_modules).",
                "parameters": {"type": "object", "properties": {}}
            }
        }),
        serde_json::json!({
            "type": "function",
            "function": {
                "name": "wizard.inspect_module",
                "description": "Liefert die vollständige Config eines existierenden Moduls.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"]
                }
            }
        }),
        serde_json::json!({
            "type": "function",
            "function": {
                "name": "wizard.list_py_modules",
                "description": "Listet Python-Module mit ihren Tools.",
                "parameters": {"type": "object", "properties": {}}
            }
        }),
        serde_json::json!({
            "type": "function",
            "function": {
                "name": "wizard.commit",
                "description": "Schreibt den DraftAgent in config.json. Fehler bei Invariant-Bruch.",
                "parameters": {"type": "object", "properties": {}}
            }
        }),
        serde_json::json!({
            "type": "function",
            "function": {
                "name": "wizard.abort",
                "description": "Verwirft die Session ohne Änderungen.",
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"]
                }
            }
        }),
    ];
    if allow_code_gen {
        tools.push(serde_json::json!({
            "type": "function",
            "function": {
                "name": "wizard.create_py_module",
                "description": "Schlaegt ein neues Python-Modul zum Erstellen vor. Wartet auf User-Bestaetigung.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Modulname, a-z0-9_ only"},
                        "description": {"type": "string"},
                        "tools": {"type": "array", "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "params": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["name", "description"]
                        }},
                        "source_code": {"type": "string"}
                    },
                    "required": ["name", "description", "tools", "source_code"]
                }
            }
        }));
    }
    serde_json::Value::Array(tools)
}

/// Apply a wizard.propose patch to a draft. Returns list of field paths actually changed.
pub fn apply_propose(
    draft: &mut DraftAgent,
    field: &str,
    value: &serde_json::Value,
) -> Result<Vec<String>, String> {
    use serde_json::Value;
    match field {
        "id" => draft.id = value.as_str().map(str::to_string),
        "typ" => draft.typ = value.as_str().map(str::to_string),
        "llm_backend" => draft.llm_backend = value.as_str().map(str::to_string),
        "backup_llm" => draft.backup_llm = value.as_str().map(str::to_string),
        "rag_pool" => draft.rag_pool = value.as_str().map(str::to_string),
        "persistent" => draft.persistent = value.as_bool().ok_or("persistent: expected bool")?,
        "timeout_s" => draft.timeout_s = value.as_u64(),
        "retry" => draft.retry = value.as_u64().map(|v| v as u32),
        "scheduler_interval_ms" => draft.scheduler_interval_ms = value.as_u64(),
        "max_concurrent_tasks" => draft.max_concurrent_tasks = value.as_u64().map(|v| v as u32),
        "token_budget" => draft.token_budget = value.as_u64(),
        "token_budget_warning" => draft.token_budget_warning = value.as_u64(),
        "berechtigungen" => draft.berechtigungen = as_string_vec(value)?,
        "linked_modules" => draft.linked_modules = as_string_vec(value)?,
        "identity.bot_name" => draft.identity.bot_name = value.as_str().map(str::to_string),
        "identity.display_name" => draft.identity.display_name = value.as_str().map(str::to_string),
        "identity.language" => draft.identity.language = value.as_str().map(str::to_string),
        "identity.personality" => draft.identity.personality = value.as_str().map(str::to_string),
        "identity.system_prompt" => draft.identity.system_prompt = value.as_str().map(str::to_string),
        "display_name" => draft.identity.display_name = value.as_str().map(str::to_string),
        "identity" => {
            // Whole-identity object patch: merge into draft.identity
            if let Some(obj) = value.as_object() {
                if let Some(v) = obj.get("bot_name").and_then(|v| v.as_str()) { draft.identity.bot_name = Some(v.to_string()); }
                if let Some(v) = obj.get("display_name").and_then(|v| v.as_str()) { draft.identity.display_name = Some(v.to_string()); }
                if let Some(v) = obj.get("language").and_then(|v| v.as_str()) { draft.identity.language = Some(v.to_string()); }
                if let Some(v) = obj.get("personality").and_then(|v| v.as_str()) { draft.identity.personality = Some(v.to_string()); }
                if let Some(v) = obj.get("system_prompt").and_then(|v| v.as_str()) { draft.identity.system_prompt = Some(v.to_string()); }
                // "greeting" is a ModulIdentity field but not a DraftIdentity field — silently ignore.
            } else {
                return Err("identity must be an object".into());
            }
        }
        // identity.greeting is not in DraftIdentity; accept + ignore silently to avoid confusing the LLM.
        "identity.greeting" => { /* no-op: greeting is materialized from elsewhere if needed */ }
        "settings" => draft.settings = value.clone(),
        other => {
            if let Some(rest) = other.strip_prefix("settings.") {
                if let Value::Object(ref mut map) = draft.settings {
                    map.insert(rest.to_string(), value.clone());
                } else {
                    let mut map = serde_json::Map::new();
                    map.insert(rest.to_string(), value.clone());
                    draft.settings = Value::Object(map);
                }
            } else {
                return Err(format!("Unbekanntes Feld: {}", field));
            }
        }
    }
    Ok(vec![field.to_string()])
}

fn as_string_vec(v: &serde_json::Value) -> Result<Vec<String>, String> {
    v.as_array()
        .ok_or("expected array")?
        .iter()
        .map(|e| e.as_str().map(str::to_string).ok_or("expected array of strings".to_string()))
        .collect()
}

// ─── Tool dispatcher ──────────────────────────────────

use std::path::Path;

/// Result of dispatching a single tool call. `state_changed` is true when the draft
/// was modified (so the caller emits a `draft_full` NDJSON event).
#[derive(Debug, Clone)]
pub struct ToolOutcome {
    pub result: serde_json::Value,
    pub state_changed: bool,
    pub user_ask: Option<(String, Vec<String>)>,
    pub abort_requested: Option<String>,
    pub commit_result: Option<serde_json::Value>,
    pub code_gen_proposed: Option<crate::types::WizardCodeGenProposal>,
}

impl Default for ToolOutcome {
    fn default() -> Self {
        Self { result: serde_json::json!({}), state_changed: false,
               user_ask: None, abort_requested: None, commit_result: None,
               code_gen_proposed: None }
    }
}

pub async fn dispatch_tool(
    tool_name: &str,
    args: &serde_json::Value,
    session: &mut WizardSession,
    cfg_lock: &std::sync::Arc<tokio::sync::RwLock<AgentConfig>>,
    config_path: &Path,
    data_root: &Path,
) -> ToolOutcome {
    match tool_name {
        "wizard.propose" => {
            let cfg = cfg_lock.read().await;
            let field = args.get("field").and_then(|v| v.as_str()).unwrap_or("");
            let value = args.get("value").cloned().unwrap_or(serde_json::Value::Null);
            match apply_propose(&mut session.draft, field, &value) {
                Ok(_) => ToolOutcome {
                    result: serde_json::json!({
                        "ok": true,
                        "draft": session.draft,
                        "missing_for_commit": missing_fields(&session.draft, &*cfg, &session.mode),
                    }),
                    state_changed: true,
                    ..Default::default()
                },
                Err(e) => ToolOutcome {
                    result: serde_json::json!({"ok": false, "error": e}),
                    ..Default::default()
                },
            }
        }
        "wizard.ask" => {
            let q = args.get("question").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let opts: Vec<String> = args.get("options")
                .and_then(|v| v.as_array())
                .map(|arr| arr.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
                .unwrap_or_default();
            ToolOutcome {
                result: serde_json::json!({"ack": true}),
                user_ask: Some((q, opts)),
                ..Default::default()
            }
        }
        "wizard.list_modules" => {
            let cfg = cfg_lock.read().await;
            let list: Vec<_> = cfg.module.iter().map(|m| serde_json::json!({
                "id": m.id,
                "typ": m.typ,
                "bot_name": m.identity.bot_name,
                "linked_modules": m.linked_modules,
            })).collect();
            ToolOutcome { result: serde_json::json!({"modules": list}), ..Default::default() }
        }
        "wizard.inspect_module" => {
            let cfg = cfg_lock.read().await;
            let id = args.get("id").and_then(|v| v.as_str()).unwrap_or("");
            match cfg.module.iter().find(|m| m.id == id) {
                Some(m) => {
                    let mut val = serde_json::to_value(m).unwrap_or(serde_json::json!({}));
                    crate::security::redact_secrets(&mut val);
                    ToolOutcome {
                        result: val,
                        ..Default::default()
                    }
                }
                None => ToolOutcome {
                    result: serde_json::json!({"error": format!("module '{}' not found", id)}),
                    ..Default::default()
                },
            }
        }
        "wizard.list_py_modules" => {
            let list = list_py_modules(data_root).await;
            ToolOutcome { result: serde_json::json!({"py_modules": list}), ..Default::default() }
        }
        "wizard.abort" => {
            let reason = args.get("reason").and_then(|v| v.as_str()).unwrap_or("").to_string();
            ToolOutcome {
                result: serde_json::json!({"ok": true}),
                abort_requested: Some(reason),
                ..Default::default()
            }
        }
        "wizard.commit" => {
            // Pre-validate under read lock for early user-facing error response.
            // This is intentionally a best-effort check; the authoritative re-check
            // happens under the write lock below (defeats TOCTOU).
            {
                let cfg_r = cfg_lock.read().await;
                if let Err(errs) = validate_for_commit(&session.draft, &cfg_r, &session.mode) {
                    let errs_value = serde_json::to_value(&errs).unwrap_or(serde_json::json!([]));
                    return ToolOutcome {
                        result: serde_json::json!({"ok": false, "errors": errs_value.clone()}),
                        commit_result: Some(serde_json::json!({"errors": errs_value})),
                        ..Default::default()
                    };
                }
            }

            // Materialize (pure function, no locks)
            let new_module = match materialize(&session.draft) {
                Ok(m) => m,
                Err(msg) => return ToolOutcome {
                    result: serde_json::json!({"ok": false, "error": msg}),
                    ..Default::default()
                },
            };

            // Acquire write lock ONCE for the whole validate + mutate + snapshot sequence.
            let (snapshot, rollback_info) = {
                let mut cfg_w = cfg_lock.write().await;

                // Re-check invariants under the write lock (defeats TOCTOU)
                if let Err(errs) = validate_for_commit(&session.draft, &cfg_w, &session.mode) {
                    let errs_value = serde_json::to_value(&errs).unwrap_or(serde_json::json!([]));
                    return ToolOutcome {
                        result: serde_json::json!({"ok": false, "errors": errs_value.clone()}),
                        commit_result: Some(serde_json::json!({"errors": errs_value})),
                        ..Default::default()
                    };
                }

                // Apply the mutation in-memory. Record rollback info.
                let rollback = match &session.mode {
                    WizardMode::Edit { target_id } => {
                        if let Some(pos) = cfg_w.module.iter().position(|m| &m.id == target_id) {
                            let prev = cfg_w.module[pos].clone();
                            cfg_w.module[pos] = new_module.clone();
                            Some(("edit", pos, Some(prev)))
                        } else {
                            cfg_w.module.push(new_module.clone());
                            Some(("appended_in_edit_missing", cfg_w.module.len() - 1, None))
                        }
                    }
                    _ => {
                        cfg_w.module.push(new_module.clone());
                        Some(("push", cfg_w.module.len() - 1, None))
                    }
                };

                // Snapshot while still holding the write lock so we write consistent bytes.
                let snapshot = cfg_w.clone();
                (snapshot, rollback)
            };  // write lock dropped here

            // Disk write outside the lock
            let write_result = persist_config(&snapshot, config_path).await;

            // On failure, re-acquire write lock briefly and roll back
            if let Err(e) = write_result {
                let mut cfg_w = cfg_lock.write().await;
                if let Some((kind, pos, prev)) = rollback_info {
                    match kind {
                        "edit" => {
                            if let (Some(prev), true) = (prev, pos < cfg_w.module.len()) {
                                cfg_w.module[pos] = prev;
                            }
                        }
                        "push" | "appended_in_edit_missing" => {
                            // Only pop if the last element is still the one we pushed
                            if cfg_w.module.last().map(|m| &m.id) == Some(&new_module.id) {
                                cfg_w.module.pop();
                            }
                        }
                        _ => {}
                    }
                }
                return ToolOutcome {
                    result: serde_json::json!({"ok": false, "error": e}),
                    ..Default::default()
                };
            }

            ToolOutcome {
                result: serde_json::json!({"ok": true, "agent_id": new_module.id}),
                commit_result: Some(serde_json::json!({"agent_id": new_module.id})),
                ..Default::default()
            }
        }
        "wizard.create_py_module" => {
            // Flag check via config
            let allow = {
                let cfg = cfg_lock.read().await;
                cfg.wizard.as_ref().map(|w| w.allow_code_gen).unwrap_or(false)
            };
            if !allow {
                return ToolOutcome {
                    result: serde_json::json!({"ok": false, "error": "code-gen is disabled"}),
                    ..Default::default()
                };
            }
            let name = args.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let description = args.get("description").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let source_code = args.get("source_code").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let tools: Vec<crate::types::ProposedPyTool> = args.get("tools")
                .and_then(|v| serde_json::from_value(v.clone()).ok())
                .unwrap_or_default();

            // Validate module_name regex: ^[a-z][a-z0-9_]*$, max 32 chars
            if name.is_empty() || name.len() > 32
               || !name.chars().next().map(|c| c.is_ascii_lowercase()).unwrap_or(false)
               || !name.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_') {
                return ToolOutcome {
                    result: serde_json::json!({"ok": false, "error": "invalid module name (must be ^[a-z][a-z0-9_]*$, max 32)"}),
                    ..Default::default()
                };
            }
            // Protected system module names
            let system_modules = ["sysinfo","healthcheck","agent_meta","mailstore","imap","smtp","pop3","editor","module_builder","taskloop","tavily","duckduckgo_search","rss","screenshot"];
            if system_modules.contains(&name.as_str()) {
                return ToolOutcome {
                    result: serde_json::json!({"ok": false, "error": "cannot overwrite system module"}),
                    ..Default::default()
                };
            }
            // Collision check: module dir must not exist
            let modules_root = data_root.parent().unwrap_or(data_root).join("modules");
            let mod_dir = modules_root.join(&name);
            if mod_dir.exists() {
                return ToolOutcome {
                    result: serde_json::json!({"ok": false, "error": format!("module '{}' already exists", name)}),
                    ..Default::default()
                };
            }
            if source_code.len() > 50_000 {
                return ToolOutcome {
                    result: serde_json::json!({"ok": false, "error": "source_code exceeds 50 kB"}),
                    ..Default::default()
                };
            }

            // Store proposal, return awaiting_user_confirmation
            let proposal = crate::types::WizardCodeGenProposal {
                module_name: name.clone(),
                description,
                tools,
                source_code,
                decision: crate::types::CodeGenDecision::Pending,
            };
            session.code_gen_proposal = Some(proposal.clone());

            ToolOutcome {
                result: serde_json::json!({"awaiting_user_confirmation": true}),
                code_gen_proposed: Some(proposal),
                ..Default::default()
            }
        }
        other => ToolOutcome {
            result: serde_json::json!({"error": format!("unknown tool: {}", other)}),
            ..Default::default()
        },
    }
}

fn materialize(d: &DraftAgent) -> Result<crate::types::ModulConfig, String> {
    use crate::types::{ModulConfig, ModulIdentity, ModulSettings};
    let id = d.id.clone().ok_or("id missing")?;
    let typ = d.typ.clone().ok_or("typ missing")?;
    let bot_name = d.identity.bot_name.clone().unwrap_or_default();
    let display_name = d.identity.display_name.clone().unwrap_or_else(|| bot_name.clone());
    let system_prompt = d.identity.system_prompt.clone().unwrap_or_default();
    let settings: ModulSettings = serde_json::from_value(d.settings.clone())
        .unwrap_or_default();
    Ok(ModulConfig {
        id: id.clone(),
        name: id,
        typ,
        display_name,
        llm_backend: d.llm_backend.clone().unwrap_or_default(),
        backup_llm: d.backup_llm.clone(),
        berechtigungen: d.berechtigungen.clone(),
        timeout_s: d.timeout_s.unwrap_or(60),
        retry: d.retry.unwrap_or(2),
        settings,
        identity: ModulIdentity {
            bot_name: bot_name.clone(),
            system_prompt,
            greeting: String::new(),
        },
        rag_pool: d.rag_pool.clone(),
        linked_modules: d.linked_modules.clone(),
        persistent: d.persistent,
        spawned_by: None,
        spawn_ttl_s: None,
        created_at: None,
        scheduler_interval_ms: d.scheduler_interval_ms,
        max_concurrent_tasks: d.max_concurrent_tasks,
        token_budget: d.token_budget,
        token_budget_warning: d.token_budget_warning,
    })
}

async fn persist_config(cfg: &AgentConfig, path: &Path) -> Result<(), String> {
    let json = serde_json::to_string_pretty(cfg).map_err(|e| e.to_string())?;
    let tmp = path.with_extension("json.tmp");
    tokio::fs::write(&tmp, json.as_bytes()).await.map_err(|e| e.to_string())?;
    tokio::fs::rename(&tmp, path).await.map_err(|e| e.to_string())?;
    Ok(())
}

pub fn missing_fields(
    draft: &DraftAgent,
    cfg: &AgentConfig,
    mode: &WizardMode,
) -> Vec<String> {
    match validate_for_commit(draft, cfg, mode) {
        Ok(()) => vec![],
        Err(errs) => {
            let mut out: Vec<String> = errs.into_iter().map(|e| e.field).collect();
            out.sort();
            out.dedup();
            out
        }
    }
}

async fn list_py_modules(data_root: &Path) -> Vec<serde_json::Value> {
    // modules/ liegt typisch neben agent-data/; fallback auf relative "modules"
    // für dev-run aus dem Projekt-Root.
    let modules_root = data_root.parent().map(|p| p.join("modules"))
        .unwrap_or_else(|| std::path::PathBuf::from("modules"));

    // Volle Metadata — inkl. description, settings (mit labels/defaults) und
    // tools — damit der Wizard den User durch Python-Modul-Config führen
    // kann. Vorher gab's nur den Namen → Wizard wusste nicht was imap.host
    // ist oder was smtp.send tut. Blocking-Call in spawn_blocking, weil
    // `discover_modules` Python-Subprozesse spawnt.
    let discovered = tokio::task::spawn_blocking(move || {
        crate::loader::discover_modules(&modules_root)
    }).await.unwrap_or_default();

    let mut out: Vec<serde_json::Value> = discovered.into_iter().map(|m| {
        let settings_view: Vec<serde_json::Value> = m.settings.iter().map(|(key, spec)| {
            // Spec ist typisch ein Object mit type/label/default/required-Feldern
            let s = spec.as_object();
            serde_json::json!({
                "key": key,
                "type": s.and_then(|o| o.get("type")).cloned().unwrap_or(serde_json::Value::Null),
                "label": s.and_then(|o| o.get("label")).cloned().unwrap_or(serde_json::Value::Null),
                "default": s.and_then(|o| o.get("default")).cloned().unwrap_or(serde_json::Value::Null),
                "required": s.and_then(|o| o.get("required")).cloned().unwrap_or(serde_json::Value::Bool(false)),
            })
        }).collect();
        let tools_view: Vec<serde_json::Value> = m.tools.iter().map(|t| serde_json::json!({
            "name": t.name,
            "description": t.description,
            "params": t.params,
        })).collect();
        serde_json::json!({
            "name": m.name,
            "description": m.description,
            "version": m.version,
            "settings": settings_view,
            "tools": tools_view,
        })
    }).collect();
    out.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
    out
}

// ─── WizardBackend trait + real impl ──────────────────

use async_trait::async_trait;

/// Abstraction over the LLM call used by the wizard. Real impl wraps LlmRouter;
/// test impl returns scripted tool-call sequences.
#[async_trait]
pub trait WizardBackend: Send + Sync {
    async fn chat(
        &self,
        messages: &[serde_json::Value],
        tools: &[serde_json::Value],
    ) -> Result<(String, serde_json::Value), String>;
}

pub struct RealWizardBackend {
    pub router: std::sync::Arc<crate::llm::LlmRouter>,
    pub backend: crate::types::LlmBackend,
    /// Optional token-tracker (UI-Mirror) + Store-Pool (persistent accounting).
    pub tokens: Option<crate::web::TokenTracker>,
    pub store_pool: Option<crate::store::SqlitePool>,
}

#[async_trait]
impl WizardBackend for RealWizardBackend {
    async fn chat(
        &self,
        messages: &[serde_json::Value],
        tools: &[serde_json::Value],
    ) -> Result<(String, serde_json::Value), String> {
        let result = self.router.chat_with_tools_adhoc(&self.backend, messages, tools).await;
        if let Ok((_text, raw)) = &result {
            if let (Some(tr), Some(pool)) = (&self.tokens, &self.store_pool) {
                crate::web::track_tokens(pool, tr, &self.backend.id, &self.backend.model, "__wizard__", raw).await;
            }
        }
        result
    }
}

use tokio::sync::mpsc;

/// Events emitted during a turn. Serialized as NDJSON lines by the HTTP layer.
#[derive(Debug, Clone, serde::Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum WizardEvent {
    Session { session_id: String, mode: WizardMode },
    AssistantText { delta: String },
    ToolCall { tool: String, arguments: serde_json::Value },
    DraftFull {
        draft: DraftAgent,
        missing_for_commit: Vec<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        next_suggested: Option<String>,
    },
    Ask { question: String, #[serde(skip_serializing_if = "Vec::is_empty", default)] options: Vec<String> },
    CommitOk { agent_id: String },
    CommitError { errors: Vec<ValidationError> },
    Frozen { reason: String },
    Error { message: String },
    Done,
    CodeGenProposal { proposal: crate::types::WizardCodeGenProposal },
    CodeGenStep {
        step: String,         // "scaffold" | "write" | "test" | "activate"
        status: String,       // "running" | "ok" | "fail"
        #[serde(skip_serializing_if = "Option::is_none", default)]
        output: Option<String>,
    },
}

/// Runs one user turn through the backend. Streams events via `tx`.
/// Returns Ok on normal completion, Err(msg) on unrecoverable error.
pub async fn run_turn(
    backend: &dyn WizardBackend,
    session: &mut WizardSession,
    cfg_lock: &std::sync::Arc<tokio::sync::RwLock<AgentConfig>>,
    config_path: &Path,
    wizard_cfg: &crate::types::WizardConfig,
    data_root: &Path,
    user_text: String,
    tx: mpsc::Sender<WizardEvent>,
    py_modules: &[crate::loader::PyModuleMeta],
) -> Result<(), String> {
    if session.frozen_reason.is_some() {
        let _ = tx.send(WizardEvent::Frozen { reason: session.frozen_reason.clone().unwrap() }).await;
        let _ = tx.send(WizardEvent::Done).await;
        return Ok(());
    }
    // Append user message
    let last_user_msg = user_text.clone();
    session.transcript.push(WizardMessage {
        role: "user".into(),
        content: user_text.clone(),
        tool_calls: vec![],
        tool_call_id: None,
        tool_result: None,
        timestamp: chrono::Utc::now().timestamp(),
    });

    // Snapshot guardrail config once (drop lock immediately).
    let (gcfg, cfg_snap) = {
        let cfg_guard = cfg_lock.read().await;
        let gcfg = cfg_guard.guardrail.clone()
            .unwrap_or_else(crate::types::GuardrailConfig::default);
        let cfg_snap = cfg_guard.clone();
        (gcfg, cfg_snap)
    };
    let strict_mode = gcfg.strict_mode;
    let backend_id = &wizard_cfg.llm.id;
    let model = &wizard_cfg.llm.model;
    let max_retries = gcfg.per_backend_overrides
        .get(backend_id.as_str()).copied()
        .unwrap_or(gcfg.max_retries);

    let tools_json = wizard_tool_descriptors(wizard_cfg.allow_code_gen);
    let tools_arr = tools_json.as_array().cloned().unwrap_or_default();
    let tool_cap = wizard_cfg.max_tool_rounds_per_turn;
    let round_cap = wizard_cfg.max_rounds_per_session;

    let mut guardrail_retries = 0u32;

    for _tool_round in 0..tool_cap {
        if session.llm_rounds_used >= round_cap {
            session.frozen_reason = Some("round_cap_reached".into());
            let _ = tx.send(WizardEvent::Frozen { reason: "round_cap_reached".into() }).await;
            break;
        }
        session.llm_rounds_used += 1;

        let messages = {
            let cfg_guard = cfg_lock.read().await;
            build_provider_messages(session, &*cfg_guard)
        };

        let (assistant_text, tool_calls_json) = match backend.chat(&messages, &tools_arr).await {
            Ok(r) => r,
            Err(e) => {
                let _ = tx.send(WizardEvent::Error { message: e }).await;
                let _ = tx.send(WizardEvent::Done).await;
                return Ok(());
            }
        };

        // ── Guardrail validation (inline fallback, plan A7.4) ──────────────
        if gcfg.enabled {
            let vctx = crate::guardrail::ValidatorContext {
                modul_id: "__wizard__",
                cfg: &cfg_snap,
                py_modules,
                last_user_msg: Some(last_user_msg.as_str()),
                strict_mode,
            };
            match crate::guardrail::validate_response(&tool_calls_json, &vctx) {
                Ok(_parsed) => {
                    // Validation passed — log ok event.
                    let ev = crate::types::GuardrailEvent {
                        ts: chrono::Utc::now().timestamp(),
                        modul: "__wizard__".into(),
                        backend: backend_id.clone(),
                        model: model.clone(),
                        tool_name: _parsed.first().map(|c| c.tool_name.clone()),
                        passed: true,
                        errors: vec![],
                        retry_attempt: guardrail_retries,
                        final_outcome: if guardrail_retries > 0 { "retried".into() } else { "ok".into() },
                        similar_suggestion: None,
                    };
                    let _ = crate::guardrail::log_event(data_root, &ev).await;
                    // Reset retry counter for the next LLM call in this turn.
                    guardrail_retries = 0;
                }
                Err(errors) => {
                    let is_last = guardrail_retries >= max_retries;
                    let ev = crate::types::GuardrailEvent {
                        ts: chrono::Utc::now().timestamp(),
                        modul: "__wizard__".into(),
                        backend: backend_id.clone(),
                        model: model.clone(),
                        tool_name: None,
                        passed: false,
                        errors: errors.clone(),
                        retry_attempt: guardrail_retries,
                        final_outcome: if is_last { "hard_fail".into() } else { "retried".into() },
                        similar_suggestion: None,
                    };
                    let _ = crate::guardrail::log_event(data_root, &ev).await;

                    if is_last {
                        let codes: Vec<String> = errors.iter().map(|e| e.code.clone()).collect();
                        let msg = format!("Guardrail hard fail: {}", codes.join(", "));
                        let _ = tx.send(WizardEvent::Error { message: msg }).await;
                        let _ = save_session(data_root, session).await;
                        let _ = tx.send(WizardEvent::Done).await;
                        return Ok(());
                    }

                    // Append synthetic feedback user message and retry.
                    let feedback = crate::guardrail::synth_feedback_user_message(
                        &errors, max_retries, guardrail_retries,
                    );
                    session.transcript.push(WizardMessage {
                        role: "user".into(),
                        content: feedback,
                        tool_calls: vec![],
                        tool_call_id: None,
                        tool_result: None,
                        timestamp: chrono::Utc::now().timestamp(),
                    });
                    guardrail_retries += 1;
                    // Continue outer loop to re-call LLM with updated transcript.
                    continue;
                }
            }
        }
        // ── End guardrail validation ────────────────────────────────────────

        if !assistant_text.is_empty() {
            let _ = tx.send(WizardEvent::AssistantText { delta: assistant_text.clone() }).await;
        }

        let calls = parse_tool_calls(&tool_calls_json);

        session.transcript.push(WizardMessage {
            role: "assistant".into(),
            content: assistant_text,
            tool_calls: calls.clone(),
            tool_call_id: None,
            tool_result: None,
            timestamp: chrono::Utc::now().timestamp(),
        });

        if calls.is_empty() {
            break;
        }

        let mut aborted = false;
        let mut committed = false;
        for call in calls {
            let _ = tx.send(WizardEvent::ToolCall {
                tool: call.tool_name.clone(),
                arguments: call.arguments.clone(),
            }).await;

            let outcome = dispatch_tool(&call.tool_name, &call.arguments, session, cfg_lock, config_path, data_root).await;

            if outcome.state_changed {
                let cfg_guard = cfg_lock.read().await;
                let _ = tx.send(WizardEvent::DraftFull {
                    draft: session.draft.clone(),
                    missing_for_commit: missing_fields(&session.draft, &*cfg_guard, &session.mode),
                    next_suggested: suggest_next(&session.draft),
                }).await;
            }
            if let Some((q, opts)) = outcome.user_ask.clone() {
                let _ = tx.send(WizardEvent::Ask { question: q, options: opts }).await;
            }
            if let Some(prop) = outcome.code_gen_proposed.clone() {
                let _ = tx.send(WizardEvent::CodeGenProposal { proposal: prop }).await;
            }
            if outcome.abort_requested.is_some() {
                let _ = delete_session(data_root, &session.session_id).await;
                aborted = true;
            }
            if let Some(commit_res) = outcome.commit_result.clone() {
                if let Some(agent_id) = commit_res.get("agent_id").and_then(|v| v.as_str()) {
                    let _ = tx.send(WizardEvent::CommitOk { agent_id: agent_id.to_string() }).await;
                    let _ = archive_session(data_root, &session.session_id).await;
                    committed = true;
                } else if let Some(errs_val) = commit_res.get("errors") {
                    if let Ok(errs) = serde_json::from_value::<Vec<ValidationError>>(errs_val.clone()) {
                        let _ = tx.send(WizardEvent::CommitError { errors: errs }).await;
                    }
                }
            }

            session.transcript.push(WizardMessage {
                role: "tool".into(),
                content: String::new(),
                tool_calls: vec![],
                tool_call_id: Some(call.id.clone()),
                tool_result: Some(outcome.result),
                timestamp: chrono::Utc::now().timestamp(),
            });

            if aborted || committed { break; }
        }

        if let Err(e) = save_session(data_root, session).await {
            let _ = tx.send(WizardEvent::Error {
                message: format!("Session-State konnte nicht gespeichert werden: {}", e)
            }).await;
        }

        if aborted || committed {
            let _ = tx.send(WizardEvent::Done).await;
            return Ok(());
        }
    }

    session.last_activity = chrono::Utc::now().timestamp();
    if let Err(e) = save_session(data_root, session).await {
        let _ = tx.send(WizardEvent::Error {
            message: format!("Session-State konnte nicht gespeichert werden: {}", e)
        }).await;
    }
    let _ = tx.send(WizardEvent::Done).await;
    Ok(())
}

fn suggest_next(draft: &DraftAgent) -> Option<String> {
    if draft.identity.bot_name.is_none() { return Some("identity".into()); }
    if draft.typ.is_none() { return Some("typ".into()); }
    if draft.typ.as_deref() == Some("chat") && draft.llm_backend.is_none() { return Some("llm_backend".into()); }
    if draft.linked_modules.is_empty() { return Some("linking".into()); }
    if draft.identity.system_prompt.is_none() { return Some("system_prompt".into()); }
    Some("review".into())
}

// ─── Code-gen execution ───────────────────────────────

async fn call_py_tool(
    pool: &std::sync::Arc<crate::loader::PyProcessPool>,
    modules_root: &Path,
    module_name: &str,
    tool_name: &str,
    params: Vec<String>,
) -> Result<serde_json::Value, String> {
    let module_path = modules_root.join(module_name).join("module.py");
    let (success, data) = pool.call(&module_path, module_name, tool_name, &params, &serde_json::json!({})).await?;
    Ok(serde_json::json!({"success": success, "data": data}))
}

pub async fn execute_code_gen(
    session: &mut WizardSession,
    approved: bool,
    reason: &str,
    app_state: &std::sync::Arc<crate::web::AppState>,
    tx: &tokio::sync::mpsc::Sender<WizardEvent>,
) {
    let proposal = match session.code_gen_proposal.clone() {
        Some(p) => p,
        None => return,
    };
    if !approved {
        session.code_gen_proposal = None;
        session.transcript.push(WizardMessage {
            role: "tool".into(),
            content: String::new(),
            tool_calls: vec![],
            tool_call_id: None,
            tool_result: Some(serde_json::json!({
                "rejected": true,
                "reason": reason,
            })),
            timestamp: chrono::Utc::now().timestamp(),
        });
        let _ = tx.send(WizardEvent::CodeGenStep {
            step: "rejected".into(), status: "ok".into(),
            output: Some(reason.to_string()),
        }).await;
        return;
    }
    // Approved: run the 4-step chain
    let pool = app_state.py_pool.clone();
    let modules_root = app_state.data_root.parent()
        .unwrap_or(&app_state.data_root)
        .join("modules");
    let mod_dir = modules_root.join(&proposal.module_name);

    // Step A: scaffold via module_builder
    let _ = tx.send(WizardEvent::CodeGenStep {
        step: "scaffold".into(), status: "running".into(), output: None,
    }).await;
    let tools_csv = proposal.tools.iter().map(|t| t.name.as_str()).collect::<Vec<_>>().join(",");
    let scaffold_params = vec![
        proposal.module_name.clone(),
        proposal.description.clone(),
        tools_csv,
    ];
    match call_py_tool(&pool, &modules_root, "module_builder", "module_builder.scaffold", scaffold_params).await {
        Ok(v) => {
            let _ = tx.send(WizardEvent::CodeGenStep {
                step: "scaffold".into(), status: "ok".into(),
                output: Some(v.to_string()),
            }).await;
        }
        Err(e) => {
            let _ = tx.send(WizardEvent::CodeGenStep {
                step: "scaffold".into(), status: "fail".into(),
                output: Some(e.clone()),
            }).await;
            session.code_gen_proposal = None;
            session.transcript.push(WizardMessage {
                role: "tool".into(),
                content: String::new(),
                tool_calls: vec![],
                tool_call_id: None,
                tool_result: Some(serde_json::json!({"ok": false, "failed_step": "scaffold", "output": e})),
                timestamp: chrono::Utc::now().timestamp(),
            });
            return;
        }
    }

    // Step B: overwrite the scaffolded module.py with LLM-provided source
    let _ = tx.send(WizardEvent::CodeGenStep {
        step: "write".into(), status: "running".into(), output: None,
    }).await;
    let module_py = mod_dir.join("module.py");
    if let Err(e) = tokio::fs::write(&module_py, &proposal.source_code).await {
        let emsg = e.to_string();
        let _ = tx.send(WizardEvent::CodeGenStep {
            step: "write".into(), status: "fail".into(), output: Some(emsg.clone()),
        }).await;
        session.code_gen_proposal = None;
        return;
    }
    let _ = tx.send(WizardEvent::CodeGenStep {
        step: "write".into(), status: "ok".into(), output: None,
    }).await;

    // Step C: test via module_builder
    let _ = tx.send(WizardEvent::CodeGenStep {
        step: "test".into(), status: "running".into(), output: None,
    }).await;
    match call_py_tool(&pool, &modules_root, "module_builder", "module_builder.test", vec![proposal.module_name.clone()]).await {
        Ok(v) => {
            let _ = tx.send(WizardEvent::CodeGenStep {
                step: "test".into(), status: "ok".into(), output: Some(v.to_string()),
            }).await;
        }
        Err(e) => {
            let _ = tx.send(WizardEvent::CodeGenStep {
                step: "test".into(), status: "fail".into(), output: Some(e.clone()),
            }).await;
            session.code_gen_proposal = None;
            session.transcript.push(WizardMessage {
                role: "tool".into(), content: String::new(),
                tool_calls: vec![], tool_call_id: None,
                tool_result: Some(serde_json::json!({"ok": false, "failed_step": "test", "output": e})),
                timestamp: chrono::Utc::now().timestamp(),
            });
            return;
        }
    }

    // Step D: activate
    let _ = tx.send(WizardEvent::CodeGenStep {
        step: "activate".into(), status: "running".into(), output: None,
    }).await;
    match call_py_tool(&pool, &modules_root, "module_builder", "module_builder.activate", vec![proposal.module_name.clone()]).await {
        Ok(_v) => {
            let _ = tx.send(WizardEvent::CodeGenStep {
                step: "activate".into(), status: "ok".into(), output: None,
            }).await;
            session.transcript.push(WizardMessage {
                role: "tool".into(), content: String::new(),
                tool_calls: vec![], tool_call_id: None,
                tool_result: Some(serde_json::json!({"ok": true, "module_name": proposal.module_name})),
                timestamp: chrono::Utc::now().timestamp(),
            });
        }
        Err(e) => {
            let _ = tx.send(WizardEvent::CodeGenStep {
                step: "activate".into(), status: "fail".into(), output: Some(e.clone()),
            }).await;
            session.transcript.push(WizardMessage {
                role: "tool".into(), content: String::new(),
                tool_calls: vec![], tool_call_id: None,
                tool_result: Some(serde_json::json!({"ok": false, "failed_step": "activate", "output": e})),
                timestamp: chrono::Utc::now().timestamp(),
            });
        }
    }
    session.code_gen_proposal = None;
}

fn parse_tool_calls(raw: &serde_json::Value) -> Vec<WizardToolCall> {
    // Accept OpenAI-style: choices[0].message.tool_calls[*] = {id, function:{name, arguments: JSON string}}
    // OR direct array format (as returned by our llm.rs converter).
    // Also Anthropic tool_use blocks.
    let mut out = Vec::new();
    // Try OpenAI nested form
    if let Some(calls) = raw.pointer("/choices/0/message/tool_calls").and_then(|v| v.as_array()) {
        for item in calls {
            if let Some(func) = item.get("function") {
                let args_str = func.get("arguments").and_then(|v| v.as_str()).unwrap_or("{}");
                let args: serde_json::Value = serde_json::from_str(args_str).unwrap_or(serde_json::json!({}));
                out.push(WizardToolCall {
                    id: item.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                    tool_name: func.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                    arguments: args,
                });
            }
        }
        return out;
    }
    // Try direct array form (convenient for tests and possible converter outputs)
    let arr = match raw.as_array() { Some(a) => a, None => return vec![] };
    for item in arr {
        if let Some(func) = item.get("function") {
            let args_str = func.get("arguments").and_then(|v| v.as_str()).unwrap_or("{}");
            let args: serde_json::Value = serde_json::from_str(args_str).unwrap_or(serde_json::json!({}));
            out.push(WizardToolCall {
                id: item.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                tool_name: func.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                arguments: args,
            });
        } else if item.get("type").and_then(|v| v.as_str()) == Some("tool_use") {
            out.push(WizardToolCall {
                id: item.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                tool_name: item.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                arguments: item.get("input").cloned().unwrap_or(serde_json::json!({})),
            });
        }
    }
    out
}

fn build_provider_messages(session: &WizardSession, cfg: &AgentConfig) -> Vec<serde_json::Value> {
    let mut msgs = vec![serde_json::json!({
        "role": "system",
        "content": build_system_prompt(&session.draft, &session.mode, cfg),
    })];
    for m in &session.transcript {
        match m.role.as_str() {
            "user" => msgs.push(serde_json::json!({"role": "user", "content": m.content})),
            "assistant" => {
                let mut obj = serde_json::json!({"role": "assistant", "content": m.content});
                if !m.tool_calls.is_empty() {
                    let calls: Vec<_> = m.tool_calls.iter().map(|c| serde_json::json!({
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.tool_name, "arguments": c.arguments.to_string()},
                    })).collect();
                    obj["tool_calls"] = serde_json::json!(calls);
                }
                msgs.push(obj);
            }
            "tool" => msgs.push(serde_json::json!({
                "role": "tool",
                "tool_call_id": m.tool_call_id,
                "content": m.tool_result.as_ref().map(|v| v.to_string()).unwrap_or_default(),
            })),
            _ => {}
        }
    }
    msgs
}

fn build_system_prompt(draft: &DraftAgent, mode: &WizardMode, cfg: &AgentConfig) -> String {
    let template = include_str!("../modules/templates/wizard.txt");
    let module_list = cfg.module.iter()
        .map(|m| format!("  - {} ({}): {}", m.id, m.typ, m.identity.bot_name))
        .collect::<Vec<_>>()
        .join("\n");
    let backend_list = cfg.llm_backends.iter()
        .map(|b| format!("  - {} ({:?}): {}", b.id, b.typ, b.model))
        .collect::<Vec<_>>()
        .join("\n");
    let mode_label = match mode {
        WizardMode::New => "neu".to_string(),
        WizardMode::Copy { source_id } => format!("kopieren von {}", source_id),
        WizardMode::Edit { target_id } => format!("editieren {}", target_id),
    };
    let draft_json = serde_json::to_string_pretty(draft).unwrap_or_default();
    template
        .replace("{{MODE}}", &mode_label)
        .replace("{{MODULES}}", &module_list)
        .replace("{{LLM_BACKENDS}}", &backend_list)
        .replace("{{DRAFT_JSON}}", &draft_json)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_id_is_22_chars_url_safe() {
        let id = new_session_id();
        assert_eq!(id.len(), 22, "expected 22 chars, got {}: {}", id.len(), id);
        assert!(id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'),
                "non-URL-safe char in: {}", id);
    }

    #[test]
    fn session_ids_are_unique_over_10k() {
        use std::collections::HashSet;
        let mut seen = HashSet::new();
        for _ in 0..10_000 {
            assert!(seen.insert(new_session_id()));
        }
    }

    #[tokio::test]
    async fn session_roundtrip_saves_and_loads() {
        let tmp = tempfile::tempdir().unwrap();
        ensure_dirs(tmp.path()).await.unwrap();
        let s = WizardSession {
            session_id: "abc123".into(),
            mode: crate::types::WizardMode::New,
            draft: Default::default(),
            original: None,
            transcript: vec![],
            llm_rounds_used: 0,
            created_at: 100,
            last_activity: 200,
            user_overridden_fields: vec![],
            frozen_reason: None, code_gen_proposal: None,
        };
        save_session(tmp.path(), &s).await.unwrap();
        let loaded = load_session(tmp.path(), "abc123").await.unwrap();
        assert_eq!(loaded.session_id, "abc123");
        assert_eq!(loaded.created_at, 100);
    }

    use crate::types::{AgentConfig, DraftAgent, WizardMode};

    #[test]
    fn validate_rejects_missing_id() {
        let cfg = AgentConfig::default();
        let draft = DraftAgent::default();
        let errs = validate_for_commit(&draft, &cfg, &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "id" && e.code == "missing"));
    }

    fn sample_cfg() -> AgentConfig {
        use crate::types::{LlmBackend, LlmTyp, ModulIdentity};
        let mut cfg = AgentConfig::default();
        cfg.llm_backends.push(LlmBackend {
            id: "grok".into(), name: "Grok".into(), typ: LlmTyp::Grok,
            url: "https://api.x.ai".into(), api_key: Some("k".into()),
            model: "grok-4".into(), timeout_s: 30,
            identity: ModulIdentity::default(),
            max_tokens: None,
        });
        cfg
    }

    fn valid_chat_draft() -> DraftAgent {
        DraftAgent {
            id: Some("chat.roland".into()),
            typ: Some("chat".into()),
            llm_backend: Some("grok".into()),
            identity: crate::types::DraftIdentity {
                bot_name: Some("Roland".into()),
                system_prompt: Some("Du bist Roland.".into()),
                ..Default::default()
            },
            ..Default::default()
        }
    }

    #[test]
    fn validate_passes_on_minimal_valid_chat() {
        assert!(validate_for_commit(&valid_chat_draft(), &sample_cfg(), &WizardMode::New).is_ok());
    }

    #[test]
    fn validate_rejects_id_bad_format() {
        let mut d = valid_chat_draft();
        d.id = Some("Chat Roland!".into());
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "id" && e.code == "invalid_format"));
    }

    #[test]
    fn validate_rejects_unknown_type() {
        let mut d = valid_chat_draft();
        d.typ = Some("ueberraschung".into());
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "typ" && e.code == "unknown_type"));
    }

    #[test]
    fn validate_rejects_chat_without_llm_backend() {
        let mut d = valid_chat_draft();
        d.llm_backend = None;
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "llm_backend" && e.code == "missing"));
    }

    #[test]
    fn validate_rejects_unknown_llm_backend() {
        let mut d = valid_chat_draft();
        d.llm_backend = Some("no_such_backend".into());
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "llm_backend" && e.code == "unknown_backend"));
    }

    fn sample_module(id: &str, typ: &str) -> crate::types::ModulConfig {
        crate::types::ModulConfig {
            id: id.into(), typ: typ.into(), name: id.into(),
            display_name: id.into(), llm_backend: "grok".into(), backup_llm: None,
            berechtigungen: vec![], timeout_s: 60, retry: 2,
            settings: crate::types::ModulSettings::default(),
            identity: crate::types::ModulIdentity::default(),
            rag_pool: None, linked_modules: vec![], persistent: true,
            spawned_by: None, spawn_ttl_s: None, created_at: None, scheduler_interval_ms: None,
            max_concurrent_tasks: None, token_budget: None, token_budget_warning: None,
        }
    }

    #[test]
    fn validate_rejects_id_collision_in_new_mode() {
        let mut cfg = sample_cfg();
        cfg.module.push(sample_module("chat.roland", "chat"));
        let errs = validate_for_commit(&valid_chat_draft(), &cfg, &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "id" && e.code == "collision"));
    }

    #[test]
    fn validate_allows_same_id_in_edit_mode() {
        let mut cfg = sample_cfg();
        cfg.module.push(sample_module("chat.roland", "chat"));
        let r = validate_for_commit(&valid_chat_draft(), &cfg, &WizardMode::Edit { target_id: "chat.roland".into() });
        assert!(r.is_ok(), "{:?}", r);
    }

    #[test]
    fn validate_rejects_unknown_linked_module() {
        let mut d = valid_chat_draft();
        d.linked_modules.push("web.ghost".into());
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "linked_modules" && e.code == "unknown_module"));
    }

    #[test]
    fn validate_rejects_permission_not_derivable() {
        let mut d = valid_chat_draft();
        d.berechtigungen.push("shell".into());
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "berechtigungen" && e.code == "not_allowed"));
    }

    #[test]
    fn validate_accepts_permission_from_linked_module() {
        let mut cfg = sample_cfg();
        cfg.module.push(sample_module("shell.ops", "shell"));
        let mut d = valid_chat_draft();
        d.linked_modules.push("shell.ops".into());
        d.berechtigungen.push("shell".into());
        assert!(validate_for_commit(&d, &cfg, &WizardMode::New).is_ok());
    }

    #[test]
    fn validate_rejects_empty_system_prompt() {
        let mut d = valid_chat_draft();
        d.identity.system_prompt = Some("".into());
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "identity.system_prompt" && e.code == "missing"));
    }

    #[test]
    fn validate_rejects_oversized_system_prompt() {
        let mut d = valid_chat_draft();
        d.identity.system_prompt = Some("A".repeat(20_001));
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "identity.system_prompt" && e.code == "too_long"));
    }

    #[test]
    fn validate_rejects_zero_token_budget() {
        let mut d = valid_chat_draft();
        d.token_budget = Some(0);
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "token_budget" && e.code == "out_of_range"));
    }

    #[test]
    fn validate_rejects_tiny_scheduler_interval() {
        let mut d = valid_chat_draft();
        d.scheduler_interval_ms = Some(100);
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "scheduler_interval_ms" && e.code == "out_of_range"));
    }

    #[test]
    fn validate_returns_all_errors_not_just_first() {
        let d = DraftAgent::default();
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.len() >= 4, "expected >= 4 errors, got {}: {:?}", errs.len(), errs);
    }

    #[test]
    fn validate_rejects_oversized_bot_name() {
        let mut d = valid_chat_draft();
        d.identity.bot_name = Some("A".repeat(65));
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "identity.bot_name" && e.code == "too_long"));
    }

    #[test]
    fn validate_rejects_zero_max_concurrent_tasks() {
        let mut d = valid_chat_draft();
        d.max_concurrent_tasks = Some(0);
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "max_concurrent_tasks" && e.code == "out_of_range"));
    }

    #[test]
    fn validate_accepts_cron_fire_permission_for_cron_typ() {
        let mut d = valid_chat_draft();
        d.typ = Some("cron".into());
        d.llm_backend = None;        // cron doesn't require llm_backend
        d.berechtigungen.push("cron.fire".into());
        assert!(validate_for_commit(&d, &sample_cfg(), &WizardMode::New).is_ok());
    }

    #[test]
    fn validate_rejects_cron_fire_permission_for_non_cron_typ() {
        let mut d = valid_chat_draft();
        // typ stays "chat"
        d.berechtigungen.push("cron.fire".into());
        let errs = validate_for_commit(&d, &sample_cfg(), &WizardMode::New).unwrap_err();
        assert!(errs.iter().any(|e| e.field == "berechtigungen" && e.code == "not_allowed"));
    }

    // ─── MockBackend ───────────────────────────────────

    use std::sync::Mutex as StdMutex;

    pub struct MockBackend {
        script: StdMutex<Vec<Result<(String, serde_json::Value), String>>>,
    }

    impl MockBackend {
        pub fn new(script: Vec<Result<(String, serde_json::Value), String>>) -> Self {
            Self { script: StdMutex::new(script) }
        }
    }

    #[async_trait::async_trait]
    impl super::WizardBackend for MockBackend {
        async fn chat(
            &self,
            _messages: &[serde_json::Value],
            _tools: &[serde_json::Value],
        ) -> Result<(String, serde_json::Value), String> {
            let mut s = self.script.lock().unwrap();
            if s.is_empty() {
                Err("mock script exhausted".into())
            } else {
                s.remove(0)
            }
        }
    }

    #[tokio::test]
    async fn mock_backend_returns_script_in_order() {
        let mb = MockBackend::new(vec![
            Ok(("first".into(), serde_json::json!([]))),
            Ok(("second".into(), serde_json::json!([]))),
        ]);
        let (t1, _) = mb.chat(&[], &[]).await.unwrap();
        let (t2, _) = mb.chat(&[], &[]).await.unwrap();
        assert_eq!(t1, "first");
        assert_eq!(t2, "second");
        assert!(mb.chat(&[], &[]).await.is_err());
    }

    #[test]
    fn apply_propose_sets_id() {
        let mut d = DraftAgent::default();
        apply_propose(&mut d, "id", &serde_json::json!("chat.test")).unwrap();
        assert_eq!(d.id.as_deref(), Some("chat.test"));
    }

    #[test]
    fn apply_propose_sets_nested_identity() {
        let mut d = DraftAgent::default();
        apply_propose(&mut d, "identity.bot_name", &serde_json::json!("Aria")).unwrap();
        assert_eq!(d.identity.bot_name.as_deref(), Some("Aria"));
    }

    #[test]
    fn apply_propose_sets_linked_modules_array() {
        let mut d = DraftAgent::default();
        apply_propose(&mut d, "linked_modules", &serde_json::json!(["a", "b"])).unwrap();
        assert_eq!(d.linked_modules, vec!["a".to_string(), "b".to_string()]);
    }

    #[test]
    fn apply_propose_rejects_unknown_field() {
        let mut d = DraftAgent::default();
        let e = apply_propose(&mut d, "nonsense", &serde_json::json!(1)).unwrap_err();
        assert!(e.contains("Unbekanntes Feld"));
    }

    #[test]
    fn apply_propose_rejects_wrong_type_for_bool() {
        let mut d = DraftAgent::default();
        let e = apply_propose(&mut d, "persistent", &serde_json::json!("yes")).unwrap_err();
        assert!(e.contains("expected bool"));
    }

    #[test]
    fn apply_propose_sets_settings_subfield() {
        let mut d = DraftAgent::default();
        apply_propose(&mut d, "settings.schedule", &serde_json::json!("0 * * * *")).unwrap();
        assert_eq!(d.settings["schedule"], serde_json::json!("0 * * * *"));
    }

    #[test]
    fn apply_propose_flat_display_name_sets_identity_display_name() {
        let mut d = DraftAgent::default();
        apply_propose(&mut d, "display_name", &serde_json::json!("Roland")).unwrap();
        assert_eq!(d.identity.display_name.as_deref(), Some("Roland"));
    }

    #[test]
    fn apply_propose_whole_identity_object_merges() {
        let mut d = DraftAgent::default();
        apply_propose(&mut d, "identity", &serde_json::json!({
            "bot_name": "Roland",
            "system_prompt": "Du bist Roland.",
            "language": "de"
        })).unwrap();
        assert_eq!(d.identity.bot_name.as_deref(), Some("Roland"));
        assert_eq!(d.identity.system_prompt.as_deref(), Some("Du bist Roland."));
        assert_eq!(d.identity.language.as_deref(), Some("de"));
    }

    #[test]
    fn apply_propose_identity_greeting_is_accepted_silently() {
        let mut d = DraftAgent::default();
        assert!(apply_propose(&mut d, "identity.greeting", &serde_json::json!("Hi!")).is_ok());
    }

    #[tokio::test]
    async fn dispatch_propose_updates_draft() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(sample_cfg()));
        let cfg_path = tmp.path().join("config.json");
        let mut s = WizardSession {
            session_id: "x".into(), mode: WizardMode::New,
            draft: Default::default(), original: None, transcript: vec![],
            llm_rounds_used: 0, created_at: 0, last_activity: 0,
            user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };
        let out = dispatch_tool(
            "wizard.propose",
            &serde_json::json!({"field": "id", "value": "chat.foo", "reasoning": "x"}),
            &mut s, &cfg_lock, &cfg_path, tmp.path(),
        ).await;
        assert!(out.state_changed);
        assert_eq!(s.draft.id.as_deref(), Some("chat.foo"));
    }

    #[tokio::test]
    async fn dispatch_ask_returns_question() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(sample_cfg()));
        let cfg_path = tmp.path().join("config.json");
        let mut s = WizardSession {
            session_id: "x".into(), mode: WizardMode::New, draft: Default::default(),
            original: None, transcript: vec![], llm_rounds_used: 0, created_at: 0,
            last_activity: 0, user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };
        let out = dispatch_tool(
            "wizard.ask",
            &serde_json::json!({"question": "Welcher Typ?", "options": ["chat","shell"]}),
            &mut s, &cfg_lock, &cfg_path, tmp.path(),
        ).await;
        assert!(out.user_ask.is_some());
        let (q, opts) = out.user_ask.unwrap();
        assert_eq!(q, "Welcher Typ?");
        assert_eq!(opts, vec!["chat".to_string(), "shell".into()]);
    }

    #[tokio::test]
    async fn dispatch_list_modules_returns_existing() {
        let tmp = tempfile::tempdir().unwrap();
        let mut cfg = sample_cfg();
        cfg.module.push(sample_module("chat.alice", "chat"));
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(cfg));
        let cfg_path = tmp.path().join("config.json");
        let mut s = WizardSession {
            session_id: "x".into(), mode: WizardMode::New, draft: Default::default(),
            original: None, transcript: vec![], llm_rounds_used: 0, created_at: 0,
            last_activity: 0, user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };
        let out = dispatch_tool("wizard.list_modules", &serde_json::json!({}),
                                &mut s, &cfg_lock, &cfg_path, tmp.path()).await;
        let arr = out.result["modules"].as_array().unwrap();
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["id"], "chat.alice");
    }

    #[tokio::test]
    async fn dispatch_inspect_module_returns_module_config() {
        let tmp = tempfile::tempdir().unwrap();
        let mut cfg = sample_cfg();
        cfg.module.push(sample_module("shell.ops", "shell"));
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(cfg));
        let cfg_path = tmp.path().join("config.json");
        let mut s = WizardSession {
            session_id: "x".into(), mode: WizardMode::New, draft: Default::default(),
            original: None, transcript: vec![], llm_rounds_used: 0, created_at: 0,
            last_activity: 0, user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };
        let out = dispatch_tool("wizard.inspect_module",
                                 &serde_json::json!({"id": "shell.ops"}),
                                 &mut s, &cfg_lock, &cfg_path, tmp.path()).await;
        assert_eq!(out.result["id"], "shell.ops");
        assert_eq!(out.result["typ"], "shell");
    }

    #[tokio::test]
    async fn dispatch_inspect_module_missing_returns_error() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(sample_cfg()));
        let cfg_path = tmp.path().join("config.json");
        let mut s = WizardSession {
            session_id: "x".into(), mode: WizardMode::New, draft: Default::default(),
            original: None, transcript: vec![], llm_rounds_used: 0, created_at: 0,
            last_activity: 0, user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };
        let out = dispatch_tool("wizard.inspect_module",
                                 &serde_json::json!({"id": "nope.none"}),
                                 &mut s, &cfg_lock, &cfg_path, tmp.path()).await;
        assert!(out.result["error"].as_str().unwrap_or("").contains("not found"));
    }

    #[tokio::test]
    async fn dispatch_abort_signals_abort() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(sample_cfg()));
        let cfg_path = tmp.path().join("config.json");
        let mut s = WizardSession {
            session_id: "x".into(), mode: WizardMode::New, draft: Default::default(),
            original: None, transcript: vec![], llm_rounds_used: 0, created_at: 0,
            last_activity: 0, user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };
        let out = dispatch_tool("wizard.abort", &serde_json::json!({"reason": "user cancelled"}),
                                &mut s, &cfg_lock, &cfg_path, tmp.path()).await;
        assert_eq!(out.abort_requested.as_deref(), Some("user cancelled"));
    }

    #[tokio::test]
    async fn dispatch_unknown_tool_returns_error() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(sample_cfg()));
        let cfg_path = tmp.path().join("config.json");
        let mut s = WizardSession {
            session_id: "x".into(), mode: WizardMode::New, draft: Default::default(),
            original: None, transcript: vec![], llm_rounds_used: 0, created_at: 0,
            last_activity: 0, user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };
        let out = dispatch_tool("wizard.bogus", &serde_json::json!({}),
                                &mut s, &cfg_lock, &cfg_path, tmp.path()).await;
        assert!(out.result["error"].as_str().unwrap_or("").contains("unknown tool"));
    }

    #[tokio::test]
    async fn cleanup_removes_old_sessions() {
        let tmp = tempfile::tempdir().unwrap();
        ensure_dirs(tmp.path()).await.unwrap();
        let now = chrono::Utc::now().timestamp();
        let fresh = WizardSession {
            session_id: "fresh".into(),
            mode: crate::types::WizardMode::New,
            draft: Default::default(),
            original: None,
            transcript: vec![],
            llm_rounds_used: 0,
            created_at: now - 100,
            last_activity: now - 100,
            user_overridden_fields: vec![],
            frozen_reason: None, code_gen_proposal: None,
        };
        let stale = WizardSession {
            session_id: "stale".into(),
            last_activity: now - 3600,
            ..fresh.clone()
        };
        save_session(tmp.path(), &fresh).await.unwrap();
        save_session(tmp.path(), &stale).await.unwrap();

        let deleted = cleanup_expired(tmp.path(), 600).await;
        assert_eq!(deleted, 1);
        assert!(load_session(tmp.path(), "fresh").await.is_some());
        assert!(load_session(tmp.path(), "stale").await.is_none());
    }

    fn minimal_wizard_cfg() -> crate::types::WizardConfig {
        use crate::types::{LlmBackend, LlmTyp, ModulIdentity};
        crate::types::WizardConfig {
            enabled: true,
            llm: LlmBackend {
                id: "wizard".into(), name: "Wizard".into(), typ: LlmTyp::Anthropic,
                url: "https://api.anthropic.com".into(), api_key: Some("sk".into()),
                model: "claude-haiku-4-5".into(), timeout_s: 30,
                identity: ModulIdentity::default(),
                max_tokens: None,
            },
            allow_code_gen: false,
            max_rounds_per_session: 30,
            max_tool_rounds_per_turn: 5,
            session_timeout_secs: 600,
            rate_limit_per_min: 10,
            max_system_prompt_chars: 20_000,
        }
    }

    #[tokio::test]
    async fn run_turn_handles_single_propose_then_done() {
        let tmp = tempfile::tempdir().unwrap();
        ensure_dirs(tmp.path()).await.unwrap();
        // Scripted: first LLM response calls wizard.propose(id), second returns plain text + no calls.
        let script = vec![
            Ok((
                "".into(),
                serde_json::json!([{
                    "id": "call_1",
                    "function": {"name": "wizard.propose", "arguments": "{\"field\":\"id\",\"value\":\"chat.test\",\"reasoning\":\"user asked\"}"}
                }]),
            )),
            Ok(("Ich habe 'chat.test' als ID vorgeschlagen.".into(), serde_json::json!([]))),
        ];
        let mock = MockBackend::new(script);
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(sample_cfg()));
        let cfg_path = tmp.path().join("config.json");
        let wcfg = minimal_wizard_cfg();

        let mut session = WizardSession {
            session_id: "sess1".into(), mode: WizardMode::New,
            draft: Default::default(), original: None, transcript: vec![],
            llm_rounds_used: 0, created_at: 0, last_activity: 0,
            user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };
        save_session(tmp.path(), &session).await.unwrap();

        let (tx, mut rx) = tokio::sync::mpsc::channel(32);
        let res = run_turn(&mock, &mut session, &cfg_lock, &cfg_path, &wcfg, tmp.path(),
                           "Ich will einen Chat-Agent namens test".into(), tx, &[]).await;
        assert!(res.is_ok());

        let mut events = vec![];
        while let Ok(Some(ev)) = tokio::time::timeout(std::time::Duration::from_millis(50), rx.recv()).await {
            events.push(ev);
        }
        assert!(events.iter().any(|e| matches!(e, WizardEvent::ToolCall{tool, ..} if tool == "wizard.propose")));
        assert!(events.iter().any(|e| matches!(e, WizardEvent::DraftFull{..})));
        assert!(events.iter().any(|e| matches!(e, WizardEvent::Done)));
        assert_eq!(session.draft.id.as_deref(), Some("chat.test"));
    }

    #[tokio::test]
    async fn run_turn_freezes_at_round_cap() {
        let tmp = tempfile::tempdir().unwrap();
        ensure_dirs(tmp.path()).await.unwrap();
        let mock = MockBackend::new(vec![]);
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(sample_cfg()));
        let cfg_path = tmp.path().join("config.json");
        let wcfg = minimal_wizard_cfg();

        let mut session = WizardSession {
            session_id: "sess1".into(), mode: WizardMode::New, draft: Default::default(),
            original: None, transcript: vec![], llm_rounds_used: 30, created_at: 0,
            last_activity: 0, user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };

        let (tx, mut rx) = tokio::sync::mpsc::channel(16);
        run_turn(&mock, &mut session, &cfg_lock, &cfg_path, &wcfg, tmp.path(), "hallo".into(), tx, &[]).await.unwrap();
        let mut got_frozen = false;
        while let Ok(Some(ev)) = tokio::time::timeout(std::time::Duration::from_millis(50), rx.recv()).await {
            if matches!(ev, WizardEvent::Frozen{..}) { got_frozen = true; }
        }
        assert!(got_frozen);
        assert_eq!(session.frozen_reason.as_deref(), Some("round_cap_reached"));
    }

    #[tokio::test]
    async fn commit_writes_new_module_on_valid_draft() {
        let tmp = tempfile::tempdir().unwrap();
        ensure_dirs(tmp.path()).await.unwrap();
        let cfg_path = tmp.path().join("config.json");
        tokio::fs::write(&cfg_path, b"{}").await.unwrap();
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(sample_cfg()));
        let mut s = WizardSession {
            session_id: "sess".into(), mode: WizardMode::New,
            draft: valid_chat_draft(), original: None, transcript: vec![],
            llm_rounds_used: 0, created_at: 0, last_activity: 0,
            user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };
        let outcome = dispatch_tool("wizard.commit", &serde_json::json!({}),
                                    &mut s, &cfg_lock, &cfg_path, tmp.path()).await;
        assert_eq!(outcome.result["ok"], true, "{:?}", outcome.result);
        let cfg_read = cfg_lock.read().await;
        assert!(cfg_read.module.iter().any(|m| m.id == "chat.roland"));
    }

    #[tokio::test]
    async fn commit_returns_errors_on_invalid_draft() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg_path = tmp.path().join("config.json");
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(sample_cfg()));
        let mut s = WizardSession {
            session_id: "sess".into(), mode: WizardMode::New,
            draft: DraftAgent::default(), original: None, transcript: vec![],
            llm_rounds_used: 0, created_at: 0, last_activity: 0,
            user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };
        let outcome = dispatch_tool("wizard.commit", &serde_json::json!({}),
                                    &mut s, &cfg_lock, &cfg_path, tmp.path()).await;
        assert_eq!(outcome.result["ok"], false);
        let errs = outcome.result["errors"].as_array().unwrap();
        assert!(errs.len() >= 4);
    }

    #[tokio::test]
    async fn commit_edit_mode_overwrites_existing() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg_path = tmp.path().join("config.json");
        let mut initial = sample_cfg();
        let mut existing = sample_module("chat.roland", "chat");
        existing.identity.system_prompt = "old".into();
        initial.module.push(existing);
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(initial));
        let mut draft = valid_chat_draft();
        draft.identity.system_prompt = Some("neuer prompt".into());
        let mut s = WizardSession {
            session_id: "sess".into(),
            mode: WizardMode::Edit { target_id: "chat.roland".into() },
            draft, original: None, transcript: vec![], llm_rounds_used: 0,
            created_at: 0, last_activity: 0, user_overridden_fields: vec![], frozen_reason: None, code_gen_proposal: None,
        };
        let outcome = dispatch_tool("wizard.commit", &serde_json::json!({}),
                                    &mut s, &cfg_lock, &cfg_path, tmp.path()).await;
        assert_eq!(outcome.result["ok"], true, "{:?}", outcome.result);
        let cfg_read = cfg_lock.read().await;
        let m = cfg_read.module.iter().find(|m| m.id == "chat.roland").unwrap();
        assert_eq!(m.identity.system_prompt, "neuer prompt");
        assert_eq!(cfg_read.module.iter().filter(|m| m.id == "chat.roland").count(), 1);
    }

    #[tokio::test]
    async fn copy_mode_creates_new_agent_from_source() {
        let tmp = tempfile::tempdir().unwrap();
        ensure_dirs(tmp.path()).await.unwrap();
        let cfg_path = tmp.path().join("config.json");
        let mut cfg = sample_cfg();
        cfg.module.push(sample_module("chat.src", "chat"));
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(cfg));

        // Draft has all valid fields but a NEW id (so commit creates a copy).
        let mut draft = valid_chat_draft();
        draft.id = Some("chat.copy".into());
        let mut session = WizardSession {
            session_id: "sess_copy".into(),
            mode: WizardMode::Copy { source_id: "chat.src".into() },
            draft,
            original: None,
            transcript: vec![],
            llm_rounds_used: 0,
            created_at: 0,
            last_activity: 0,
            user_overridden_fields: vec![],
            frozen_reason: None, code_gen_proposal: None,
        };
        let outcome = dispatch_tool(
            "wizard.commit",
            &serde_json::json!({}),
            &mut session,
            &cfg_lock,
            &cfg_path,
            tmp.path(),
        ).await;
        assert_eq!(outcome.result["ok"], true, "{:?}", outcome.result);

        let r = cfg_lock.read().await;
        assert!(r.module.iter().any(|m| m.id == "chat.src"), "source must remain");
        assert!(r.module.iter().any(|m| m.id == "chat.copy"), "copy must be created");
        assert_eq!(r.module.iter().filter(|m| m.id == "chat.src").count(), 1);
    }

    #[tokio::test]
    async fn abort_midway_deletes_session_file_on_disk() {
        let tmp = tempfile::tempdir().unwrap();
        ensure_dirs(tmp.path()).await.unwrap();
        let cfg_path = tmp.path().join("config.json");
        let cfg_lock = std::sync::Arc::new(tokio::sync::RwLock::new(sample_cfg()));

        let session = WizardSession {
            session_id: "sess_abort_disk".into(),
            mode: WizardMode::New,
            draft: DraftAgent::default(),
            original: None,
            transcript: vec![],
            llm_rounds_used: 0,
            created_at: 0,
            last_activity: 0,
            user_overridden_fields: vec![],
            frozen_reason: None, code_gen_proposal: None,
        };
        save_session(tmp.path(), &session).await.unwrap();
        assert!(session_path(tmp.path(), "sess_abort_disk").exists(),
                "session file should exist before abort");

        // Dispatch-level abort does NOT itself delete the file — deletion happens in run_turn's
        // abort branch. Simulate that side-effect directly to verify the disk operation.
        let mut s = session.clone();
        let outcome = dispatch_tool(
            "wizard.abort",
            &serde_json::json!({"reason": "user abort"}),
            &mut s,
            &cfg_lock,
            &cfg_path,
            tmp.path(),
        ).await;
        assert!(outcome.abort_requested.is_some());

        // run_turn's deletion step:
        delete_session(tmp.path(), "sess_abort_disk").await.unwrap();
        assert!(!session_path(tmp.path(), "sess_abort_disk").exists(),
                "session file should be gone after delete_session");
    }
}
