// src/util.rs — Shared utilities used across modules

use crate::types::{AgentConfig, LlmBackend, ModulConfig, ModulIdentity};
use std::path::Path;

/// Globaler Counter für Temp-Dateinamen in atomic_write — macht jedes Temp
/// eindeutig innerhalb des Prozesses. Vorher war der Name nur `.tmp.<pid>`,
/// das kollidiert bei zwei gleichzeitigen Writes auf denselben Pfad (z.B.
/// Wizard + Orchestrator speichern config.json parallel).
static ATOMIC_WRITE_COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// Write `bytes` atomically to `path`: writes to a sibling temp file first, then renames.
/// A crash between calls leaves either the old content OR the new content — never a
/// truncated/half-written file. Temp-Dateiname ist eindeutig pro Aufruf (PID + counter
/// + thread-id), also kollidieren auch gleichzeitige Writer im selben Prozess auf
/// denselben Pfad nicht auf derselben Temp-Datei. Für denselben Pfad gewinnt der
/// letzte rename (last-write-wins) — das ist das erwartete Verhalten für Config/State.
pub fn atomic_write(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let counter = ATOMIC_WRITE_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let tmp = {
        let mut t = path.as_os_str().to_owned();
        t.push(".tmp.");
        t.push(std::process::id().to_string());
        t.push(".");
        t.push(counter.to_string());
        t.push(".");
        // thread id als zusätzliche Kollisionsabsicherung (counter + pid wäre
        // eigentlich schon eindeutig, aber kostet nichts extra)
        t.push(format!("{:?}", std::thread::current().id()));
        std::path::PathBuf::from(t)
    };
    // Ensure parent exists (caller usually already did, but cheap to verify)
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&tmp, bytes)?;
    // rename() is atomic on POSIX when source + dest are on same FS (always true here).
    // On Windows it's atomic-enough for our needs.
    match std::fs::rename(&tmp, path) {
        Ok(()) => Ok(()),
        Err(e) => {
            // Clean up temp on failure so we don't leak
            let _ = std::fs::remove_file(&tmp);
            Err(e)
        }
    }
}

const DEFAULT_SYSTEM_PROMPT: &str = "Du bist ein hilfreicher Assistent.";

/// Resolve the identity for a module: use module identity if customized, else fall back
/// to the LLM backend's identity. Previously duplicated in cycle.rs, web.rs chat, web.rs
/// prompt_preview.
pub fn resolve_identity(modul: &ModulConfig, config: &AgentConfig) -> ModulIdentity {
    let backend_identity = config
        .llm_backends
        .iter()
        .find(|b| b.id == modul.llm_backend)
        .map(|b| b.identity.clone());

    let is_custom = !modul.identity.system_prompt.is_empty()
        && modul.identity.system_prompt != DEFAULT_SYSTEM_PROMPT;

    if is_custom {
        modul.identity.clone()
    } else {
        backend_identity.unwrap_or_else(|| modul.identity.clone())
    }
}

/// Parse a chat return route.
///
/// Supported:
/// - `chat:chat.llamacpp`
/// - `chat:chat.llamacpp:mhl58rc1abc12`
pub fn parse_chat_route(route: &str) -> Option<(String, Option<String>)> {
    let rest = route.strip_prefix("chat:")?;
    let mut parts = rest.splitn(2, ':');
    let modul_id = crate::security::safe_id(parts.next()?.trim())?;
    let convo_id = parts
        .next()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .and_then(crate::security::safe_id);
    Some((modul_id, convo_id))
}

/// The UI model is "LLM Gem owns attached modules". Tool access still uses the
/// older linked_modules field internally, so normalize chat modules to link all
/// persistent sibling modules attached to the same LLM backend.
pub fn normalize_same_llm_links(config: &mut AgentConfig) -> bool {
    let modules = config.module.clone();
    let mut changed = false;

    for module in &mut config.module {
        if module.typ != "chat" || module.llm_backend.is_empty() {
            continue;
        }

        for sibling in modules.iter().filter(|m| {
            m.id != module.id
                && m.persistent
                && m.llm_backend == module.llm_backend
                && m.typ != "telegram_bot"
        }) {
            if !module.linked_modules.iter().any(|id| id == &sibling.id) {
                module.linked_modules.push(sibling.id.clone());
                changed = true;
            }
        }

        if !module.linked_modules.is_empty()
            && !module.berechtigungen.iter().any(|p| p == "aufgaben")
        {
            module.berechtigungen.push("aufgaben".into());
            changed = true;
        }
    }

    changed
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiVaultUse {
    pub id: String,
    pub alias: String,
    pub path: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CredentialVaultUse {
    pub id: String,
    pub field: String,
    pub alias: String,
    pub path: String,
}

pub fn api_key_vault_alias(id: &str) -> String {
    let clean = id.trim().strip_prefix("api.").unwrap_or(id.trim());
    format!("api.{}", clean)
}

pub fn api_key_vault_alias_id(value: &str) -> Option<String> {
    let mut s = value.trim();
    if let Some(inner) = s.strip_prefix("${").and_then(|v| v.strip_suffix('}')) {
        s = inner.trim();
    }
    let id = s.strip_prefix("api.")?.trim();
    if id.is_empty() {
        return None;
    }
    if !id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '-' || c == '_')
    {
        return None;
    }
    Some(id.to_string())
}

pub fn is_secret_like_key(key: &str) -> bool {
    let k = key.to_ascii_lowercase();
    k == "secret"
        || k.contains("api_key")
        || k.contains("apikey")
        || k.contains("token")
        || k.contains("password")
        || k.contains("secret")
        || k.contains("bearer")
        || k == "client_id"
        || k == "client_secret"
}

pub fn resolve_api_key_alias_string(
    value: &str,
    config: &AgentConfig,
) -> Option<(String, ApiVaultUse)> {
    let id = api_key_vault_alias_id(value)?;
    let secret = config
        .api_key_vault
        .iter()
        .find(|entry| entry.id == id)
        .and_then(|entry| entry.secret.as_deref())
        .map(str::trim)
        .filter(|secret| !secret.is_empty())?;
    Some((
        secret.to_string(),
        ApiVaultUse {
            alias: api_key_vault_alias(&id),
            id,
            path: String::new(),
        },
    ))
}

pub fn credential_vault_alias(id: &str, field: &str) -> String {
    format!("cred.{}.{}", id.trim(), field.trim())
}

pub fn credential_vault_bare_alias(id: &str, field: &str) -> String {
    format!("{}.{}", id.trim(), field.trim())
}

fn credential_alias_matches(value: &str, id: &str, field: &str) -> Option<String> {
    let trimmed = value.trim();
    let inner = trimmed
        .strip_prefix("${")
        .and_then(|v| v.strip_suffix('}'))
        .map(str::trim)
        .unwrap_or(trimmed);
    let canonical = credential_vault_alias(id, field);
    let bare = credential_vault_bare_alias(id, field);
    if inner == canonical {
        Some(canonical)
    } else if inner == bare {
        Some(bare)
    } else {
        None
    }
}

pub fn resolve_credential_alias_string(
    value: &str,
    config: &AgentConfig,
) -> Option<(String, CredentialVaultUse)> {
    for entry in &config.credential_vault {
        for field in &entry.fields {
            let Some(alias) = credential_alias_matches(value, &entry.id, &field.key) else {
                continue;
            };
            let resolved = field
                .value
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())?;
            return Some((
                resolved.to_string(),
                CredentialVaultUse {
                    id: entry.id.clone(),
                    field: field.key.clone(),
                    alias,
                    path: String::new(),
                },
            ));
        }
    }
    None
}

pub fn resolve_llm_backend_api_alias(
    backend: &mut LlmBackend,
    config: &AgentConfig,
) -> Vec<ApiVaultUse> {
    let Some(current) = backend.api_key.as_deref() else {
        return Vec::new();
    };
    let Some((secret, mut used)) = resolve_api_key_alias_string(current, config) else {
        return Vec::new();
    };
    backend.api_key = Some(secret);
    used.path = format!("llm_backends.{}.api_key", backend.id);
    vec![used]
}

pub fn resolve_modul_config_api_aliases(
    modul: &mut ModulConfig,
    config: &AgentConfig,
) -> Vec<ApiVaultUse> {
    let mut value = match serde_json::to_value(&modul.settings) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let mut uses = resolve_api_key_aliases_in_json(&mut value, config);
    if uses.is_empty() {
        return uses;
    }
    for used in &mut uses {
        if used.path.is_empty() {
            used.path = format!("module.{}.settings", modul.id);
        } else {
            used.path = format!("module.{}.settings.{}", modul.id, used.path);
        }
    }
    if let Ok(settings) = serde_json::from_value(value) {
        modul.settings = settings;
    }
    uses
}

pub fn resolve_api_key_aliases_in_json(
    value: &mut serde_json::Value,
    config: &AgentConfig,
) -> Vec<ApiVaultUse> {
    let mut uses = Vec::new();
    resolve_api_key_aliases_in_json_inner(value, config, "", None, &mut uses);
    uses
}

pub fn resolve_credential_aliases_in_json(
    value: &mut serde_json::Value,
    config: &AgentConfig,
) -> Vec<CredentialVaultUse> {
    let mut uses = Vec::new();
    resolve_credential_aliases_in_json_inner(value, config, "", &mut uses);
    uses
}

fn resolve_credential_aliases_in_json_inner(
    value: &mut serde_json::Value,
    config: &AgentConfig,
    path: &str,
    uses: &mut Vec<CredentialVaultUse>,
) {
    match value {
        serde_json::Value::Object(map) => {
            for (key, child) in map.iter_mut() {
                let child_path = if path.is_empty() {
                    key.clone()
                } else {
                    format!("{}.{}", path, key)
                };
                resolve_credential_aliases_in_json_inner(child, config, &child_path, uses);
            }
        }
        serde_json::Value::Array(arr) => {
            for (idx, child) in arr.iter_mut().enumerate() {
                let child_path = if path.is_empty() {
                    idx.to_string()
                } else {
                    format!("{}.{}", path, idx)
                };
                resolve_credential_aliases_in_json_inner(child, config, &child_path, uses);
            }
        }
        serde_json::Value::String(s) => {
            let Some((resolved, mut used)) = resolve_credential_alias_string(s, config) else {
                return;
            };
            *s = resolved;
            used.path = path.to_string();
            uses.push(used);
        }
        _ => {}
    }
}

fn resolve_api_key_aliases_in_json_inner(
    value: &mut serde_json::Value,
    config: &AgentConfig,
    path: &str,
    key_name: Option<&str>,
    uses: &mut Vec<ApiVaultUse>,
) {
    match value {
        serde_json::Value::Object(map) => {
            for (key, child) in map.iter_mut() {
                let child_path = if path.is_empty() {
                    key.clone()
                } else {
                    format!("{}.{}", path, key)
                };
                resolve_api_key_aliases_in_json_inner(
                    child,
                    config,
                    &child_path,
                    Some(key.as_str()),
                    uses,
                );
            }
        }
        serde_json::Value::Array(arr) => {
            for (idx, child) in arr.iter_mut().enumerate() {
                let child_path = if path.is_empty() {
                    idx.to_string()
                } else {
                    format!("{}.{}", path, idx)
                };
                resolve_api_key_aliases_in_json_inner(child, config, &child_path, key_name, uses);
            }
        }
        serde_json::Value::String(s) => {
            if !key_name.map(is_secret_like_key).unwrap_or(false) {
                return;
            }
            let Some((secret, mut used)) = resolve_api_key_alias_string(s, config) else {
                return;
            };
            *s = secret;
            used.path = path.to_string();
            uses.push(used);
        }
        _ => {}
    }
}

/// UTF-8-safe truncation returning a string slice. Never cuts mid-character.
pub fn safe_truncate(s: &str, max: usize) -> &str {
    if s.len() <= max {
        return s;
    }
    let mut end = max;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
}

/// UTF-8-safe truncation returning an owned String with "[abgeschnitten]" suffix.
pub fn safe_truncate_owned(s: &str, max: usize) -> String {
    if s.len() <= max {
        return s.to_string();
    }
    let mut end = max;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}...[abgeschnitten]", &s[..end])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_safe_truncate_short_string() {
        assert_eq!(safe_truncate("hello", 10), "hello");
    }

    #[test]
    fn test_safe_truncate_exact_length() {
        assert_eq!(safe_truncate("hello", 5), "hello");
    }

    #[test]
    fn test_safe_truncate_cuts() {
        assert_eq!(safe_truncate("hello world", 5), "hello");
    }

    #[test]
    fn test_safe_truncate_utf8_boundary() {
        let text = "W\u{00f6}rld"; // Wörld — ö is 2 bytes
        let result = safe_truncate(text, 2);
        assert!(result.len() <= 2);
        assert_eq!(result, "W");
    }

    #[test]
    fn test_safe_truncate_emoji() {
        let text = "Hi \u{1f30d} world"; // 🌍 is 4 bytes
        let result = safe_truncate(text, 4);
        assert!(result.len() <= 4);
        assert_eq!(result, "Hi ");
    }

    #[test]
    fn test_safe_truncate_empty() {
        assert_eq!(safe_truncate("", 10), "");
    }

    #[test]
    fn test_safe_truncate_owned_suffix() {
        let result = safe_truncate_owned("hello world this is long", 10);
        assert!(result.contains("...[abgeschnitten]"));
        assert!(result.starts_with("hello worl"));
    }

    #[test]
    fn test_safe_truncate_owned_short() {
        assert_eq!(safe_truncate_owned("hi", 10), "hi");
    }

    #[test]
    fn test_atomic_write_creates_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.txt");
        atomic_write(&path, b"hello world").unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"hello world");
    }

    #[test]
    fn test_atomic_write_overwrites() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.txt");
        atomic_write(&path, b"first").unwrap();
        atomic_write(&path, b"second").unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"second");
    }

    #[test]
    fn test_atomic_write_leaves_no_tmp_on_success() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.txt");
        atomic_write(&path, b"data").unwrap();
        let files: Vec<_> = std::fs::read_dir(dir.path()).unwrap().flatten().collect();
        // only the final file, no stray .tmp.* sibling
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].file_name(), "test.txt");
    }

    #[test]
    fn test_atomic_write_concurrent_same_path_no_collision() {
        // Regression: zwei Threads schreiben gleichzeitig denselben Pfad mit
        // unterschiedlichen Inhalten. Mit per-PID-only-Temp hatten sie dieselbe
        // Temp-Datei und eine Schreiboperation überschrieb die andere → Lost-Update.
        // Mit counter-per-call darf das nicht mehr passieren; am Ende steht
        // einer der beiden Werte vollständig in der Datei (last-rename-wins).
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("concurrent.txt");
        let p = std::sync::Arc::new(path.clone());

        let handles: Vec<_> = (0..20)
            .map(|i| {
                let p = p.clone();
                let content: Vec<u8> = format!("content-from-writer-{:03}", i).into_bytes();
                std::thread::spawn(move || {
                    atomic_write(&p, &content).unwrap();
                })
            })
            .collect();
        for h in handles {
            h.join().unwrap();
        }

        // Nach allen Threads: genau eine Datei, genau ein vollständiger Inhalt
        // (keine abgeschnittene oder leere Datei).
        let contents = std::fs::read(&path).unwrap();
        assert!(
            contents.starts_with(b"content-from-writer-"),
            "datei muss vollständigen content eines writers haben, nicht fragment: {:?}",
            contents
        );
        // Keine .tmp.* Leichen im Verzeichnis
        let files: Vec<_> = std::fs::read_dir(dir.path()).unwrap().flatten().collect();
        let tmp_count = files
            .iter()
            .filter(|f| f.file_name().to_string_lossy().contains(".tmp."))
            .count();
        assert_eq!(tmp_count, 0, "keine .tmp.* Leichen erwartet");
    }

    #[test]
    fn normalize_same_llm_links_exposes_sibling_modules_to_chat() {
        let mut cfg = AgentConfig::default();
        cfg.llm_backends.push(crate::types::LlmBackend {
            id: "local".into(),
            name: "Local".into(),
            typ: crate::types::LlmTyp::OpenAICompat,
            url: "http://127.0.0.1:8080".into(),
            api_key: None,
            model: "local".into(),
            timeout_s: 30,
            identity: Default::default(),
            max_tokens: None,
            reasoning: None,
            cost_cap: None,
            max_tool_rounds: None,
            call_rate_limit: None,
            internal: false,
            tool_choice_supported: None,
            context_window: None,
        });
        cfg.module.push(crate::types::ModulConfig {
            id: "chat.local".into(),
            typ: "chat".into(),
            name: "chat.local".into(),
            display_name: "Chat".into(),
            llm_backend: "local".into(),
            backup_llm: None,
            berechtigungen: vec![],
            timeout_s: 30,
            retry: 0,
            settings: Default::default(),
            identity: Default::default(),
            rag_pool: None,
            secure: None,
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
        });
        cfg.module.push(crate::types::ModulConfig {
            id: "tavily.default".into(),
            typ: "tavily".into(),
            name: "tavily.default".into(),
            display_name: "Tavily".into(),
            llm_backend: "local".into(),
            backup_llm: None,
            berechtigungen: vec![],
            timeout_s: 30,
            retry: 0,
            settings: Default::default(),
            identity: Default::default(),
            rag_pool: None,
            secure: None,
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
        });
        cfg.module.push(crate::types::ModulConfig {
            id: "telegram_bot.default".into(),
            typ: "telegram_bot".into(),
            name: "telegram_bot.default".into(),
            display_name: "Telegram".into(),
            llm_backend: "local".into(),
            backup_llm: None,
            berechtigungen: vec![],
            timeout_s: 30,
            retry: 0,
            settings: Default::default(),
            identity: Default::default(),
            rag_pool: None,
            secure: None,
            linked_modules: vec!["chat.local".into()],
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
        });

        assert!(normalize_same_llm_links(&mut cfg));
        let chat = cfg.module.iter().find(|m| m.id == "chat.local").unwrap();
        assert!(chat.linked_modules.iter().any(|id| id == "tavily.default"));
        assert!(
            !chat
                .linked_modules
                .iter()
                .any(|id| id == "telegram_bot.default")
        );
        assert!(chat.berechtigungen.iter().any(|p| p == "aufgaben"));
    }

    #[test]
    fn parse_chat_route_supports_optional_conversation_id() {
        assert_eq!(
            parse_chat_route("chat:chat.local"),
            Some(("chat.local".into(), None))
        );
        assert_eq!(
            parse_chat_route("chat:chat.local:abc123"),
            Some(("chat.local".into(), Some("abc123".into())))
        );
        assert!(parse_chat_route("chat:../bad:abc123").is_none());
    }

    #[test]
    fn api_vault_resolves_only_secret_like_json_fields() {
        let mut cfg = AgentConfig::default();
        cfg.api_key_vault.push(crate::types::ApiKeyVaultEntry {
            id: "deepseek".into(),
            name: "DeepSeek".into(),
            provider: Some("deepseek".into()),
            secret: Some("real-secret".into()),
            notes: None,
            created_at: None,
            updated_at: None,
        });
        let mut value = serde_json::json!({
            "api_key": "api.deepseek",
            "label": "api.deepseek"
        });

        let uses = resolve_api_key_aliases_in_json(&mut value, &cfg);

        assert_eq!(value["api_key"], "real-secret");
        assert_eq!(value["label"], "api.deepseek");
        assert_eq!(uses.len(), 1);
        assert_eq!(uses[0].alias, "api.deepseek");
        assert_eq!(uses[0].path, "api_key");
    }

    #[test]
    fn credential_vault_resolves_group_fields_anywhere() {
        let mut cfg = AgentConfig::default();
        cfg.credential_vault
            .push(crate::types::CredentialVaultEntry {
                id: "mail.private".into(),
                name: "Private Mail".into(),
                kind: Some("mail".into()),
                fields: vec![
                    crate::types::CredentialVaultField {
                        key: "host".into(),
                        value: Some("imap.example.test".into()),
                        secret: false,
                    },
                    crate::types::CredentialVaultField {
                        key: "password".into(),
                        value: Some("real-password".into()),
                        secret: true,
                    },
                ],
                notes: None,
                created_at: None,
                updated_at: None,
            });
        let mut value = serde_json::json!({
            "host": "mail.private.host",
            "password": "${cred.mail.private.password}",
            "label": "mail.private.unknown"
        });

        let uses = resolve_credential_aliases_in_json(&mut value, &cfg);

        assert_eq!(value["host"], "imap.example.test");
        assert_eq!(value["password"], "real-password");
        assert_eq!(value["label"], "mail.private.unknown");
        assert_eq!(uses.len(), 2);
        assert_eq!(uses[0].path, "host");
        assert_eq!(uses[1].alias, "cred.mail.private.password");
    }
}
