use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

// ─── LLM Backend ───────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmBackend {
    pub id: String,
    pub name: String,
    pub typ: LlmTyp,
    pub url: String,
    pub api_key: Option<String>,
    pub model: String,
    pub timeout_s: u64,
    #[serde(default)]
    pub identity: ModulIdentity,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum LlmTyp {
    Ollama,
    OpenAICompat,
    Anthropic,
    Grok,
    Embedding,
}

// ─── Modul ─────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModulConfig {
    pub id: String,
    pub typ: String,             // "chat", "mail", "filesystem", "websearch", "aufgaben", "rag", "cron", "notify", "shell", "webhook"
    pub name: String,            // "chat.roland", "mail.privat", "web.kolobri"
    pub display_name: String,    // "Roland", "Privat Mail", etc
    pub llm_backend: String,     // id des LLM backends
    pub backup_llm: Option<String>,
    pub berechtigungen: Vec<String>,  // ids anderer module auf die zugegriffen werden darf
    pub timeout_s: u64,
    pub retry: u32,
    pub settings: ModulSettings,
    pub identity: ModulIdentity,
    pub rag_pool: Option<String>,     // id des RAG pools (oder None)

    // ── v1.0 Neue Felder ─────────────────────
    #[serde(default)]
    pub linked_modules: Vec<String>,
    #[serde(default = "default_true")]
    pub persistent: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub spawned_by: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub spawn_ttl_s: Option<u64>,
    /// Unix-Timestamp wann das Modul erstellt wurde. Für Temp-Agents wird der
    /// TTL gegen DIESES Feld geprüft, NICHT gegen den Scheduler-Heartbeat —
    /// letzterer wird alle ~2s aktualisiert, also ist `now - heartbeat` immer
    /// klein, und der TTL-Check hat nie getriggert (GLM-Finding).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub created_at: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scheduler_interval_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_concurrent_tasks: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token_budget: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token_budget_warning: Option<u64>,
}

fn default_true() -> bool { true }

// ─── Typspezifische Modul-Settings ────────────────
// Jeder Modultyp hat seine eigenen Config-Felder.
// Alle Felder sind Optional für Rückwärtskompatibilität.

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ModulSettings {
    // ── Mail ──────────────────────────────────────
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub imap_host: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub imap_port: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub smtp_host: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub smtp_port: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub password: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mail_ordner: Option<String>,       // default: INBOX
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_mails: Option<u32>,            // default: 10

    // ── Filesystem ───────────────────────────────
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub allowed_paths: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_file_size: Option<u64>,        // default: 4000 bytes
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub allow_write: Option<bool>,         // default: true

    // ── Websearch ────────────────────────────────
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub search_engine: Option<String>,     // "duckduckgo", "brave", "serper", "google", "grok"
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub brave_api_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub serper_api_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub google_api_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub google_cx: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub grok_api_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_results: Option<u32>,          // default: 8

    // ── Cron ─────────────────────────────────────
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub schedule: Option<String>,          // cron expression z.B. "0 */6 * * *"
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_modul: Option<String>,      // welches Modul getriggert wird
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cron_anweisung: Option<String>,    // was soll gemacht werden
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cron_typ: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cron_tool: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cron_params: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub on_success: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub on_success_params: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub on_failure: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub on_failure_params: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub chain: Option<Vec<ChainStep>>,

    // ── Notify ───────────────────────────────────
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub notify_type: Option<String>,       // "ntfy", "gotify", "telegram"
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub notify_url: Option<String>,        // Endpoint URL
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub notify_token: Option<String>,      // Auth token
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub notify_topic: Option<String>,      // Topic/Channel

    // ── Shell ────────────────────────────────────
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub allowed_commands: Option<Vec<String>>,  // Whitelist: ["git", "docker", "systemctl"]
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub working_dir: Option<String>,

    // ── Chat (eigener Port pro Instanz) ──────────
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub port: Option<u16>,                 // z.B. 8091, 8092, ...

    // ── Webhook ──────────────────────────────────
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub listen_path: Option<String>,       // z.B. "/webhook/github"
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auth_token: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub allowed_ips: Option<Vec<String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChainStep {
    pub tool: String,
    #[serde(default)]
    pub params: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub condition: Option<String>,
    #[serde(default = "default_true")]
    pub stop_on_fail: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModulIdentity {
    pub bot_name: String,       // "Roland", "MailBot", etc
    pub greeting: String,       // "Hallo! Ich bin Roland..."
    pub system_prompt: String,  // System prompt für das LLM
}

impl Default for ModulIdentity {
    fn default() -> Self {
        Self {
            bot_name: "Agent".into(),
            greeting: "Hallo!".into(),
            system_prompt: "Du bist ein hilfreicher Assistent.".into(),
        }
    }
}

// ─── RAG Pool ──────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RagPool {
    pub id: String,
    pub name: String,           // "shared", "mail", "projekte"
    pub typ: RagTyp,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum RagTyp {
    Shared,
    Private,
}

// ─── Aufgabe ───────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Aufgabe {
    pub id: String,
    pub version: u32,
    pub wann: String,            // "sofort" oder ISO datetime
    pub typ: AufgabeTyp,         // direct, llm_call, chat_reply
    pub tool: Option<String>,    // Tool das aufgerufen werden soll (bei direct/llm_call)
    pub params: Vec<String>,     // Tool-Parameter
    pub modul: String,           // Welches Modul das Tool bereitstellt
    pub anweisung: String,       // Original-Anweisung / Kontext
    pub antwort_template: Option<String>,  // z.B. "Es ist <RESULT>."
    pub zurueck_an: Option<String>,        // Wohin: "chat:chat.roland"
    pub braucht_ki: bool,        // Braucht die Ausführung ein LLM?
    pub timeout_s: u64,
    pub retry: u32,
    pub retry_count: u32,
    pub status: AufgabeStatus,
    pub ergebnis: Option<String>,
    pub erstellt_von: String,    // welches modul/chat hat die aufgabe erstellt
    pub erstellt: DateTime<Utc>,
    pub gestartet: Option<DateTime<Utc>>,
    pub erledigt: Option<DateTime<Utc>>,
    pub history: Vec<AufgabeVersion>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum AufgabeTyp {
    #[serde(rename = "direct")]
    Direct,      // Tool direkt aufrufen, kein LLM
    #[serde(rename = "llm_call")]
    LlmCall,     // LLM wird fuer Verarbeitung gebraucht
    #[serde(rename = "chat_reply")]
    ChatReply,   // Reine Chat-Antwort, kein Tool
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum AufgabeStatus {
    Erstellt,
    Gestartet,
    Success,
    Failed,
    Cancelled,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AufgabeVersion {
    pub version: u32,
    pub anweisung: String,
    pub geaendert: DateTime<Utc>,
    pub grund: String,
}

impl Aufgabe {
    /// Erstellt eine Direct-Aufgabe (Tool direkt, kein LLM)
    pub fn direct(tool: &str, params: Vec<String>, modul: &str, erstellt_von: &str, template: Option<String>, zurueck_an: Option<String>) -> Self {
        Self {
            id: uuid::Uuid::new_v4().to_string(),
            version: 1,
            wann: "sofort".into(),
            typ: AufgabeTyp::Direct,
            tool: Some(tool.into()),
            params,
            modul: modul.into(),
            anweisung: format!("Tool: {}", tool),
            antwort_template: template,
            zurueck_an,
            braucht_ki: false,
            timeout_s: 30,
            retry: 0, retry_count: 0,
            status: AufgabeStatus::Erstellt,
            ergebnis: None,
            erstellt_von: erstellt_von.into(),
            erstellt: Utc::now(),
            gestartet: None, erledigt: None,
            history: vec![],
        }
    }

    /// Erstellt eine LLM-Aufgabe (braucht KI fuer Verarbeitung)
    pub fn llm_call(anweisung: &str, modul: &str, erstellt_von: &str, zurueck_an: Option<String>) -> Self {
        Self {
            id: uuid::Uuid::new_v4().to_string(),
            version: 1,
            wann: "sofort".into(),
            typ: AufgabeTyp::LlmCall,
            tool: None,
            params: vec![],
            modul: modul.into(),
            anweisung: anweisung.into(),
            antwort_template: None,
            zurueck_an,
            braucht_ki: true,
            timeout_s: 60,
            retry: 0, retry_count: 0,
            status: AufgabeStatus::Erstellt,
            ergebnis: None,
            erstellt_von: erstellt_von.into(),
            erstellt: Utc::now(),
            gestartet: None, erledigt: None,
            history: vec![],
        }
    }

    /// Legacy: einfache Aufgabe (Rückwärtskompatibel)
    pub fn neu(modul: &str, anweisung: &str, wann: &str, erstellt_von: &str) -> Self {
        Self {
            id: uuid::Uuid::new_v4().to_string(),
            version: 1,
            wann: wann.into(),
            typ: AufgabeTyp::LlmCall,
            tool: None,
            params: vec![],
            modul: modul.into(),
            anweisung: anweisung.into(),
            antwort_template: None,
            zurueck_an: None,
            braucht_ki: true,
            timeout_s: 30,
            retry: 0, retry_count: 0,
            status: AufgabeStatus::Erstellt,
            ergebnis: None,
            erstellt_von: erstellt_von.into(),
            erstellt: Utc::now(),
            gestartet: None, erledigt: None,
            history: vec![],
        }
    }

    pub fn datei(&self) -> String {
        format!("{}.json", self.id)
    }

    /// Aufgabe updaten → alte Version in History schieben
    pub fn update(&mut self, neue_anweisung: &str, grund: &str) {
        self.history.push(AufgabeVersion {
            version: self.version,
            anweisung: self.anweisung.clone(),
            geaendert: Utc::now(),
            grund: grund.into(),
        });
        self.version += 1;
        self.anweisung = neue_anweisung.into();
    }
}

// ─── Gesamt-Config ─────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentConfig {
    pub name: String,
    pub web_port: u16,
    pub cycle_interval_ms: u64,
    pub llm_backends: Vec<LlmBackend>,
    pub module: Vec<ModulConfig>,
    pub rag_pools: Vec<RagPool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embedding_backend: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cleanup: Option<CleanupConfig>,
    /// Bearer token required for /api/* routes. If None, only 127.0.0.1 is allowed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub api_auth_token: Option<String>,
    /// Bind address, default 127.0.0.1. Set to "0.0.0.0" to expose to network (requires api_auth_token).
    #[serde(default = "default_bind")]
    pub bind_address: String,
    /// Max request body size in bytes. Default 2MB.
    #[serde(default = "default_body_limit")]
    pub max_body_bytes: usize,
    /// Chat rate limit per IP per minute. 0 = disabled. Default 60.
    #[serde(default = "default_rate_limit")]
    pub chat_rate_limit_per_min: u32,
    /// Keep log files for N days (0 = forever). Default 30.
    #[serde(default = "default_log_retention")]
    pub log_retention_days: u32,
    /// Wizard backend configuration. None disables all /api/wizard/* routes.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wizard: Option<WizardConfig>,
    /// Guardrail validator configuration. None means no guardrail.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub guardrail: Option<GuardrailConfig>,
    /// Hard daily USD budget across all LLM calls. None/0 = unlimited.
    /// Checked before every LLM call; blocks further calls once the day's cost exceeds this.
    /// Resets at UTC midnight.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub daily_budget_usd: Option<f64>,
}

fn default_bind() -> String { "127.0.0.1".into() }
fn default_body_limit() -> usize { 2 * 1024 * 1024 }
fn default_rate_limit() -> u32 { 60 }
fn default_log_retention() -> u32 { 30 }

/// Einfacher HEAD/OPTIONS-basierter Reachability-Check pro Backend-Typ.
/// Wird vom Setup-Flow benutzt um zu entscheiden ob der User auf die Setup-
/// Page geleitet werden soll (no working backend yet) oder direkt ins
/// Dashboard. Kein LLM-Roundtrip — nur Server-erreichbar ja/nein.
pub async fn test_backend_reachable(client: &reqwest::Client, b: &LlmBackend) -> bool {
    let url = match b.typ {
        LlmTyp::Ollama => format!("{}/api/tags", b.url.trim_end_matches('/')),
        LlmTyp::OpenAICompat | LlmTyp::Grok | LlmTyp::Embedding => {
            let base = b.url.trim_end_matches('/');
            if base.ends_with("/v1") { format!("{}/models", base) } else { format!("{}/v1/models", base) }
        }
        LlmTyp::Anthropic => {
            // Anthropic hat keinen /v1/models endpoint ohne auth — wir pingen die root
            b.url.trim_end_matches('/').to_string()
        }
    };
    let mut req = client.get(&url);
    if let Some(ref key) = b.api_key {
        if !key.is_empty() {
            req = req.bearer_auth(key);
        }
    }
    matches!(req.send().await, Ok(r) if r.status().as_u16() < 500)
}

impl Default for AgentConfig {
    fn default() -> Self {
        // Saubere, ehrliche Default-Config: KEIN vorkonfiguriertes LLM-Backend.
        // Der User sieht nach dem ersten Start garantiert den /setup-Flow wo
        // er auswählt was er nutzen will (OpenRouter mit freiem Tier, Ollama
        // lokal, OpenAI, Anthropic, llama.cpp). Keine fake Ollama-Einträge die
        // suggerieren "hier läuft was" wenn auf der Zielmaschine gar kein
        // Ollama ist. Wizard ist aktiviert aber zeigt sich erst nachdem der
        // Setup-Flow ein funktionierendes Backend eingetragen hat (Setup
        // zeigt die Wizard-Config automatisch aufs neu gewählte Backend).
        Self {
            name: "Agent Platform".into(),
            web_port: 8090,
            cycle_interval_ms: 2000,
            llm_backends: vec![],
            module: vec![],
            rag_pools: vec![],
            embedding_backend: None,
            cleanup: Some(CleanupConfig {
                max_erledigt: 500,
                max_alter_tage: 30,
                cleanup_interval_s: 60,
            }),
            daily_budget_usd: Some(5.0),
            api_auth_token: None,
            bind_address: default_bind(),
            max_body_bytes: default_body_limit(),
            chat_rate_limit_per_min: default_rate_limit(),
            log_retention_days: default_log_retention(),
            wizard: None,  // Wizard wird vom Setup-Flow aktiviert sobald ein Backend funktioniert
            guardrail: Some(GuardrailConfig::default()),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CleanupConfig {
    pub max_erledigt: usize,
    pub max_alter_tage: u32,
    pub cleanup_interval_s: u64,
}

// ─── Wizard ──────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WizardConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    pub llm: LlmBackend,                         // reuse existing LlmBackend struct
    #[serde(default)]
    pub allow_code_gen: bool,                    // Phase 3; default false
    #[serde(default = "default_wizard_max_rounds")]
    pub max_rounds_per_session: u32,
    #[serde(default = "default_wizard_tool_rounds")]
    pub max_tool_rounds_per_turn: u32,
    #[serde(default = "default_wizard_session_timeout")]
    pub session_timeout_secs: u64,
    #[serde(default = "default_wizard_rate_limit")]
    pub rate_limit_per_min: u32,
    #[serde(default = "default_wizard_max_prompt")]
    pub max_system_prompt_chars: usize,
}

fn default_wizard_max_rounds() -> u32 { 30 }
fn default_wizard_tool_rounds() -> u32 { 5 }
fn default_wizard_session_timeout() -> u64 { 600 }
fn default_wizard_rate_limit() -> u32 { 10 }
fn default_wizard_max_prompt() -> usize { 20_000 }

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DraftIdentity {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bot_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub language: Option<String>,                // "de" | "en"
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub personality: Option<String>,             // "professional" | "friendly" | ...
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub system_prompt: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DraftAgent {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub typ: Option<String>,                     // "chat" | "filesystem" | ...
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub llm_backend: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub backup_llm: Option<String>,
    #[serde(default)]
    pub berechtigungen: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout_s: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rag_pool: Option<String>,
    #[serde(default)]
    pub linked_modules: Vec<String>,
    #[serde(default)]
    pub persistent: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scheduler_interval_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_concurrent_tasks: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token_budget: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token_budget_warning: Option<u64>,
    #[serde(default)]
    pub identity: DraftIdentity,
    /// Typspezifische Settings als JSON (cron-Schedule, shell-whitelist, etc.).
    /// Im Commit-Schritt in ModulSettings materialisiert.
    #[serde(default)]
    pub settings: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum WizardMode {
    New,
    Copy { source_id: String },
    Edit { target_id: String },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WizardToolCall {
    pub id: String,
    pub tool_name: String,
    pub arguments: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WizardMessage {
    pub role: String,                            // "user" | "assistant" | "tool"
    #[serde(default)]
    pub content: String,
    #[serde(default)]
    pub tool_calls: Vec<WizardToolCall>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_result: Option<serde_json::Value>,
    pub timestamp: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProposedPyTool {
    pub name: String,
    pub description: String,
    #[serde(default)]
    pub params: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum CodeGenDecision {
    Pending,
    Approved,
    Rejected { reason: String },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WizardCodeGenProposal {
    pub module_name: String,
    pub description: String,
    pub tools: Vec<ProposedPyTool>,
    pub source_code: String,
    #[serde(default = "default_pending")]
    pub decision: CodeGenDecision,
}

fn default_pending() -> CodeGenDecision { CodeGenDecision::Pending }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WizardSession {
    pub session_id: String,
    pub mode: WizardMode,
    pub draft: DraftAgent,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub original: Option<ModulConfig>,
    #[serde(default)]
    pub transcript: Vec<WizardMessage>,
    #[serde(default)]
    pub llm_rounds_used: u32,
    pub created_at: i64,
    pub last_activity: i64,
    #[serde(default)]
    pub user_overridden_fields: Vec<String>,     // for Wizard/Du badge
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub frozen_reason: Option<String>,           // e.g. "round_cap_reached"
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub code_gen_proposal: Option<WizardCodeGenProposal>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ValidationError {
    pub field: String,
    pub code: String,                            // "missing" | "invalid_format" | "collision" | ...
    pub human_message_de: String,
}

// ─── Log Event ─────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogEvent {
    pub zeit: DateTime<Utc>,
    pub modul: String,
    pub aufgabe_id: Option<String>,
    pub typ: LogTyp,
    pub nachricht: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum LogTyp {
    Info,
    Success,
    Failed,
    Warning,
    Error,
}

// ─── Guardrail ──────────────────────────────────

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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GuardrailConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_guardrail_retries")]
    pub max_retries: u32,
    #[serde(default)]
    pub strict_mode: bool,
    #[serde(default)]
    pub per_backend_overrides: std::collections::HashMap<String, u32>,
    #[serde(default = "default_guardrail_max_events_per_turn")]
    pub max_events_per_turn: u32,
    #[serde(default)]
    pub alert: GuardrailAlertConfig,
    #[serde(default)]
    pub strict_triggers: Vec<String>,
    #[serde(default = "default_true")]
    pub fallback_on_hard_fail: bool,
}
fn default_guardrail_retries() -> u32 { 2 }
fn default_guardrail_max_events_per_turn() -> u32 { 10 }

impl Default for GuardrailConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            max_retries: default_guardrail_retries(),
            strict_mode: false,
            per_backend_overrides: std::collections::HashMap::new(),
            max_events_per_turn: default_guardrail_max_events_per_turn(),
            alert: GuardrailAlertConfig::default(),
            strict_triggers: vec![],
            fallback_on_hard_fail: true,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GuardrailEvent {
    pub ts: i64,
    pub modul: String,
    pub backend: String,
    pub model: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    pub passed: bool,
    #[serde(default)]
    pub errors: Vec<ValidationError>,
    pub retry_attempt: u32,
    pub final_outcome: String,  // "ok" | "retried" | "hard_fail"
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub similar_suggestion: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct ModelStats {
    pub total: u64,
    pub valid: u64,
    pub hard_failed: u64,
    pub last_ts: i64,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct BackendStats {
    pub total: u64,
    pub valid: u64,
    pub hard_failed: u64,
    pub per_model: std::collections::HashMap<String, ModelStats>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct StatsSummary {
    pub total: u64,
    pub valid: u64,
    pub invalid: u64,
    pub retried: u64,
    pub hard_failed: u64,
    pub per_backend: std::collections::HashMap<String, BackendStats>,
    pub top_errors: Vec<(String, u64)>,
    pub window_hours: u32,
}

// ─── Benchmark ──────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BenchmarkCase {
    pub id: String,
    pub prompt: String,
    pub expected: BenchmarkExpectation,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum BenchmarkExpectation {
    ToolCalled { tool_name: String },
    NoToolCall,
    Denied,
}

#[derive(Debug, Clone, Serialize)]
pub struct BenchmarkResult {
    pub case_id: String,
    pub prompt: String,
    pub passed: bool,
    pub actual_tool: Option<String>,
    pub errors: Vec<ValidationError>,
    pub latency_ms: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct BenchmarkReport {
    pub backend: String,
    pub model: String,
    pub started_at: i64,
    pub total_cases: usize,
    pub passed: usize,
    pub failed: usize,
    pub denied: usize,
    pub total_latency_ms: u64,
    pub results: Vec<BenchmarkResult>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BenchmarkCompareReport {
    pub report_a: BenchmarkReport,
    pub report_b: BenchmarkReport,
    pub winner_per_case: Vec<(String, String)>,  // (case_id, "A"|"B"|"tie")
}
