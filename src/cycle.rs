use crate::llm::LlmRouter;
use crate::pipeline::Pipeline;
use crate::tools;
use crate::turn::{ActivityMarker, mark_activity};
use crate::types::*;
use crate::util;
use std::collections::HashMap;
use std::sync::{
    Arc,
    atomic::{AtomicI64, Ordering},
};
use tokio::sync::RwLock;

const MAX_TASK_TOOL_RESULT_CHARS: usize = 4000;
const MAX_TASK_OLD_TOOL_RESULT_CHARS: usize = 500;
const MIN_TASK_IDLE_TIMEOUT_S: u64 = 30;

fn now_ts() -> i64 {
    chrono::Utc::now().timestamp()
}

fn idle_timed_out(last_activity_ts: i64, now_ts: i64, timeout_secs: u64) -> bool {
    now_ts.saturating_sub(last_activity_ts) >= timeout_secs.max(MIN_TASK_IDLE_TIMEOUT_S) as i64
}

async fn wait_for_idle_timeout(activity: ActivityMarker, timeout_secs: u64) {
    let timeout_secs = timeout_secs.max(MIN_TASK_IDLE_TIMEOUT_S);
    loop {
        let now = now_ts();
        let last = activity.load(Ordering::Relaxed);
        if idle_timed_out(last, now, timeout_secs) {
            return;
        }
        let elapsed = now.saturating_sub(last).max(0) as u64;
        let sleep_s = timeout_secs.saturating_sub(elapsed).clamp(1, 5);
        tokio::time::sleep(std::time::Duration::from_secs(sleep_s)).await;
    }
}

fn task_tool_result_for_llm(ok: bool, data: &str) -> String {
    let body = if data.chars().count() > MAX_TASK_TOOL_RESULT_CHARS {
        format!(
            "{}...[gekuerzt; vollstaendiges Ergebnis im Aufgaben-Board. Nutze gezieltere Tool-Aufrufe, z.B. kleinere Datei-/Zeilenbereiche, statt denselben grossen Output erneut zu laden.]",
            util::safe_truncate(data, MAX_TASK_TOOL_RESULT_CHARS)
        )
    } else {
        data.to_string()
    };

    if ok {
        format!("SUCCESS: {}", body)
    } else {
        format!(
            "FAILED: {}\nNEXT: Entscheide, ob du mit korrigierten Parametern erneut versuchst, ein anderes Tool nutzt oder dem User den Blocker konkret erklaerst.",
            body
        )
    }
}

/// Simple cron check: does the current time match a cron expression?
/// Supports: */N, specific numbers, ranges (1-5), and * (any)
/// Format: "minute hour day_of_month month day_of_week"
fn cron_matches_now(expression: &str) -> bool {
    let now = chrono::Local::now();
    let parts: Vec<&str> = expression.split_whitespace().collect();
    if parts.len() != 5 {
        return false;
    }

    let checks = [
        (
            parts[0],
            now.format("%M").to_string().parse::<u32>().unwrap_or(0),
        ),
        (
            parts[1],
            now.format("%H").to_string().parse::<u32>().unwrap_or(0),
        ),
        (
            parts[2],
            now.format("%d").to_string().parse::<u32>().unwrap_or(0),
        ),
        (
            parts[3],
            now.format("%m").to_string().parse::<u32>().unwrap_or(0),
        ),
    ];

    if !checks
        .iter()
        .all(|(pattern, current)| cron_field_matches(pattern, *current))
    {
        return false;
    }

    let dow_iso = now.format("%u").to_string().parse::<u32>().unwrap_or(1); // 1=Mo..7=So
    cron_dow_matches(parts[4], dow_iso)
}

/// Day-of-week-Match mit beiden Konventionen: Standard-Cron (0-6, 0=Sonntag)
/// UND ISO (1-7, 7=Sonntag). Vorher wurde nur %u (1-7) geprueft — ein Standard-
/// Cron-Ausdruck wie "0 9 * * 0" (sonntags 9:00) feuerte dadurch NIE.
fn cron_dow_matches(pattern: &str, dow_iso: u32) -> bool {
    let dow_std = dow_iso % 7; // 0=So..6=Sa
    cron_field_matches(pattern, dow_iso) || cron_field_matches(pattern, dow_std)
}

fn cron_field_matches(pattern: &str, value: u32) -> bool {
    if pattern == "*" {
        return true;
    }
    // */N — every N
    if let Some(step) = pattern.strip_prefix("*/") {
        if let Ok(n) = step.parse::<u32>() {
            return n > 0 && value % n == 0;
        }
    }
    // Range: 1-5
    if pattern.contains('-') {
        let parts: Vec<&str> = pattern.split('-').collect();
        if parts.len() == 2 {
            if let (Ok(start), Ok(end)) = (parts[0].parse::<u32>(), parts[1].parse::<u32>()) {
                return value >= start && value <= end;
            }
        }
    }
    // Comma list: 1,3,5
    if pattern.contains(',') {
        return pattern
            .split(',')
            .filter_map(|p| p.trim().parse::<u32>().ok())
            .any(|v| v == value);
    }
    // Exact number
    if let Ok(exact) = pattern.parse::<u32>() {
        return value == exact;
    }
    false
}

fn workflow_tick_params_target_specific(params: &[String]) -> bool {
    params.iter().any(|raw| {
        let trimmed = raw.trim();
        if trimmed.is_empty() || trimmed == "{}" {
            return false;
        }
        match serde_json::from_str::<serde_json::Value>(trimmed) {
            Ok(value) => value
                .as_object()
                .map(|obj| {
                    obj.get("workflow_id")
                        .or_else(|| obj.get("id"))
                        .and_then(|v| v.as_str())
                        .is_some_and(|s| !s.trim().is_empty())
                })
                .unwrap_or(true),
            Err(_) => true,
        }
    })
}

fn workflow_root_for_cron_tick(
    pipeline: &Pipeline,
    cfg: &AgentConfig,
    target_modul: &str,
) -> std::path::PathBuf {
    let raw = cfg
        .module
        .iter()
        .find(|m| m.id == target_modul || m.name == target_modul)
        .and_then(|m| m.settings.extra.get("default_output_dir"))
        .and_then(|v| v.as_str())
        .filter(|s| !s.trim().is_empty())
        .unwrap_or("agent-data/workflows");
    let path = std::path::PathBuf::from(raw);
    if path.is_absolute() {
        return path;
    }
    pipeline.base.parent().unwrap_or(&pipeline.base).join(path)
}

fn workflow_root_has_active_workflows(root: &std::path::Path) -> bool {
    let Ok(entries) = std::fs::read_dir(root) else {
        return false;
    };
    entries.filter_map(Result::ok).any(|entry| {
        let path = entry.path().join("workflow.json");
        let Ok(text) = std::fs::read_to_string(path) else {
            return false;
        };
        let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) else {
            return false;
        };
        value
            .get("status")
            .and_then(|v| v.as_str())
            .map(|s| matches!(s.to_ascii_lowercase().as_str(), "running" | "waiting"))
            .unwrap_or(false)
    })
}

fn should_skip_empty_workflow_tick(
    pipeline: &Pipeline,
    cfg: &AgentConfig,
    modul: &ModulConfig,
) -> bool {
    if modul.settings.cron_tool.as_deref() != Some("workflow_trigger.tick") {
        return false;
    }
    let params = modul.settings.cron_params.clone().unwrap_or_default();
    if workflow_tick_params_target_specific(&params) {
        return false;
    }
    let target = modul.settings.target_modul.as_deref().unwrap_or(&modul.id);
    let root = workflow_root_for_cron_tick(pipeline, cfg, target);
    if workflow_root_has_active_workflows(&root) {
        return false;
    }
    // Auch das Modul-Default-Verzeichnis pruefen: Workflows, die mit anderem
    // default_output_dir erzeugt wurden (CLI/Tests, Config-Umstellungen),
    // verhungerten sonst stumm als "running" — der Tick feuerte nie.
    let legacy = pipeline
        .base
        .parent()
        .unwrap_or(&pipeline.base)
        .join("agent-data/workflows");
    if legacy != root && workflow_root_has_active_workflows(&legacy) {
        return false;
    }
    true
}

/// RAII-Guard der busy/handles-Einträge garantiert aufräumt — auch bei Panic
/// in der Task-Ausführung (exec_llm/exec_direct `.unwrap()` auf korrupte Daten).
/// Ohne den Guard würde ein Panic den Cleanup-Block überspringen → busy-Map und
/// handles stale → Scheduler freezt stumm bei max_concurrent (Gemini-Finding).
/// Drop spawned einen kleinen cleanup-Task auf der aktuellen Tokio-Runtime;
/// das funktioniert sowohl im Happy-Path als auch während des Unwinding.
struct BusyGuard {
    busy: Option<BusyMap>,
    handles: Option<HandleMap>,
    modul_id: String,
    aufgabe_id: String,
}

impl BusyGuard {
    fn new(busy: BusyMap, handles: HandleMap, modul_id: String, aufgabe_id: String) -> Self {
        Self {
            busy: Some(busy),
            handles: Some(handles),
            modul_id,
            aufgabe_id,
        }
    }
}

impl Drop for BusyGuard {
    fn drop(&mut self) {
        let (Some(busy), Some(handles)) = (self.busy.take(), self.handles.take()) else {
            return;
        };
        let modul_id = std::mem::take(&mut self.modul_id);
        let aufgabe_id = std::mem::take(&mut self.aufgabe_id);
        // Cleanup-Task in Tokio-Runtime spawnen. Wenn die Runtime schon weg ist
        // (Prozess-Shutdown), wird try_current()=Err und wir ignorieren — der
        // Prozess beendet sich ohnehin.
        if let Ok(handle) = tokio::runtime::Handle::try_current() {
            handle.spawn(async move {
                {
                    let mut b = busy.write().await;
                    if let Some(ids) = b.get_mut(&modul_id) {
                        ids.retain(|id| id != &aufgabe_id);
                        if ids.is_empty() {
                            b.remove(&modul_id);
                        }
                    }
                }
                {
                    let mut h = handles.write().await;
                    if let Some(map) = h.get_mut(&modul_id) {
                        map.remove(&aufgabe_id);
                        if map.is_empty() {
                            h.remove(&modul_id);
                        }
                    }
                }
            });
        }
    }
}

/// Tracking welche Instanzen gerade busy sind
pub type BusyMap = Arc<RwLock<HashMap<String, Vec<String>>>>; // modul_id -> vec of aufgabe_ids

/// Parallel zu BusyMap: pro Aufgabe ein AbortHandle des tokio::spawn.
/// Watchdog nutzt das um bei totem Scheduler/Modul die noch laufenden Tasks
/// wirklich abzubrechen, bevor sie im Busy-Slot freigegeben werden — sonst
/// entsteht das Double-Execution-Race (Scheduler pickt denselben Task nochmal
/// während die alte Instanz noch läuft). Separater Typ statt BusyMap-Value-
/// Umbau, weil AbortHandle nicht Serialize implementiert und BusyMap aus web.rs
/// als JSON auf die Metrics-Seite serialisiert wird.
pub type HandleMap = Arc<RwLock<HashMap<String, HashMap<String, tokio::task::AbortHandle>>>>;

/// Per-Scheduler Heartbeats: modul_id -> epoch timestamp
pub type HeartbeatMap = Arc<RwLock<HashMap<String, u64>>>;

// ═══ Orchestrator ════════════════════════════════════
// Ersetzt den alten globalen Cycle. Spawnt pro Modul einen eigenen Scheduler.

pub struct Orchestrator {
    pub pipeline: Arc<Pipeline>,
    pub config: Arc<RwLock<AgentConfig>>,
    pub llm: Arc<LlmRouter>,
    pub py_modules: Arc<RwLock<Vec<crate::loader::PyModuleMeta>>>,
    pub py_pool: Arc<crate::loader::PyProcessPool>,
    pub busy: BusyMap,
    pub handles: HandleMap,
    pub heartbeats: HeartbeatMap,
    /// Shared token/cost tracker — same instance as `web::AppState::tokens` so the
    /// `daily_budget_usd` cap applies to scheduler-driven AND chat-driven LLM calls.
    pub tokens: crate::web::TokenTracker,
}

impl Orchestrator {
    pub fn new(
        pipeline: Arc<Pipeline>,
        config: Arc<RwLock<AgentConfig>>,
        llm: Arc<LlmRouter>,
        py_modules: Arc<RwLock<Vec<crate::loader::PyModuleMeta>>>,
        py_pool: Arc<crate::loader::PyProcessPool>,
        tokens: crate::web::TokenTracker,
    ) -> Self {
        // Migration: ein altes cron_state.json einmalig nach SQL übernehmen,
        // danach die Datei archivieren. Danach läuft der Cron-Dedup komplett
        // über store::cron_try_claim (atomar in SQL-Transaktion).
        let cron_state_path = pipeline.base.join("cron_state.json");
        if cron_state_path.exists() {
            if let Ok(content) = std::fs::read_to_string(&cron_state_path) {
                if let Ok(old_map) = serde_json::from_str::<HashMap<String, String>>(&content) {
                    for (modul, minute) in old_map.iter() {
                        let _ = crate::store::cron_try_claim(&pipeline.store.pool, modul, minute);
                    }
                }
            }
            let archived = cron_state_path.with_extension("json.migrated");
            let _ = std::fs::rename(&cron_state_path, &archived);
        }

        Self {
            pipeline,
            config,
            llm,
            py_modules,
            py_pool,
            busy: Arc::new(RwLock::new(HashMap::new())),
            handles: Arc::new(RwLock::new(HashMap::new())),
            heartbeats: Arc::new(RwLock::new(HashMap::new())),
            tokens,
        }
    }

    pub async fn run(&self) {
        self.pipeline
            .log("orchestrator", None, LogTyp::Info, "Orchestrator gestartet");

        let mut handles: HashMap<String, tokio::task::JoinHandle<()>> = HashMap::new();
        let mut last_cleanup = std::time::Instant::now();
        // Cron läuft separat vom Cleanup (das war der Bug: cleanup_interval_s > 60
        // hat Minuten-cron-Slots übersprungen — der minute-Key wurde nie geprüft).
        // 30s heißt wir prüfen jede Minute mindestens 1x (meist 2x, Dedup verhindert
        // Double-Fire). Unabhängig von cleanup_interval_s.
        let mut last_cron_check = std::time::Instant::now() - std::time::Duration::from_secs(60);

        loop {
            // Load temp module specs created by agent.spawn
            self.load_temp_modules().await;

            // 1. Config lesen, Modul-IDs sammeln
            let cfg = self.config.read().await;
            let blocked = crate::security::blocked_module_ids(&cfg);
            let modul_ids: Vec<String> = cfg
                .module
                .iter()
                .filter(|m| m.typ != "enhancer")
                .filter(|m| !blocked.contains(&m.id))
                .map(|m| m.id.clone())
                .collect();
            let cleanup_cfg = cfg.cleanup.clone();
            drop(cfg);

            // 2. Fuer jedes Modul: Scheduler pruefen/spawnen
            //
            // Liveness-Kriterium: Scheduler ist "tot" wenn
            //   (a) JoinHandle finished ist (Task ist sauber durchgelaufen oder panicked), ODER
            //   (b) der Watchdog den Heartbeat entfernt hat (Scheduler hängt blockiert,
            //       JoinHandle noch aktiv, aber kein Lebenszeichen mehr).
            // Ohne (b) würde ein hängender Scheduler (z.B. blockiert in sync I/O oder
            // Deadlock) nie ersetzt, weil JoinHandle "is_finished()==false" bleibt.
            // GPT-Finding: "Watchdog kann tote Scheduler nicht wirklich neu starten".
            let hb_snapshot: std::collections::HashSet<String> = {
                let hb = self.heartbeats.read().await;
                hb.keys().cloned().collect()
            };
            for modul_id in &modul_ids {
                let needs_spawn = match handles.get(modul_id) {
                    Some(handle) => handle.is_finished() || !hb_snapshot.contains(modul_id),
                    None => true,
                };

                if needs_spawn {
                    // Vorher hängenden Handle abort()en — der Scheduler-Task könnte noch
                    // in unkooperativem Sync-Block sein; mindestens beim nächsten await
                    // wird er dann beendet.
                    if let Some(old) = handles.remove(modul_id) {
                        if !old.is_finished() {
                            old.abort();
                        }
                    }
                    // Placeholder-Heartbeat vor Spawn — sonst sieht der nächste Tick
                    // "Heartbeat fehlt" und startet den frischen Scheduler sofort wieder
                    // neu (Respawn-Loop). Der Scheduler überschreibt den Wert bei seinem
                    // ersten echten Tick.
                    {
                        let mut hb = self.heartbeats.write().await;
                        hb.insert(modul_id.clone(), chrono::Utc::now().timestamp() as u64);
                    }
                    // Intervall aus Config holen
                    let cfg = self.config.read().await;
                    let interval_ms = cfg
                        .module
                        .iter()
                        .find(|m| m.id == *modul_id)
                        .and_then(|m| m.scheduler_interval_ms)
                        .unwrap_or(cfg.cycle_interval_ms);
                    let max_concurrent = cfg
                        .module
                        .iter()
                        .find(|m| m.id == *modul_id)
                        .and_then(|m| m.max_concurrent_tasks)
                        .unwrap_or(1);
                    drop(cfg);

                    let scheduler = ModulScheduler {
                        modul_id: modul_id.clone(),
                        interval_ms,
                        max_concurrent,
                        pipeline: self.pipeline.clone(),
                        config: self.config.clone(),
                        llm: self.llm.clone(),
                        py_modules: self.py_modules.clone(),
                        py_pool: self.py_pool.clone(),
                        busy: self.busy.clone(),
                        handles: self.handles.clone(),
                        heartbeats: self.heartbeats.clone(),
                        tokens: self.tokens.clone(),
                    };

                    self.pipeline.log(
                        "orchestrator",
                        None,
                        LogTyp::Info,
                        &format!(
                            "Scheduler '{}' wird gestartet (interval: {}ms)",
                            modul_id, interval_ms
                        ),
                    );

                    let handle = tokio::spawn(async move {
                        scheduler.run().await;
                    });
                    handles.insert(modul_id.clone(), handle);
                }
            }

            // 3. Handles fuer entfernte Module abbrechen
            let stale: Vec<String> = handles
                .keys()
                .filter(|id| !modul_ids.contains(id))
                .cloned()
                .collect();
            for id in stale {
                if let Some(handle) = handles.remove(&id) {
                    handle.abort();
                    self.pipeline.log(
                        "orchestrator",
                        None,
                        LogTyp::Warning,
                        &format!("Scheduler '{}' gestoppt (Modul entfernt)", id),
                    );
                }
                // Heartbeat entfernen
                self.heartbeats.write().await.remove(&id);
            }

            // 4a. Cron check alle 30s — unabhängig vom Cleanup-Intervall damit Minuten-
            // slots nie übersprungen werden (dedup gegen Double-Fire bleibt aktiv).
            if last_cron_check.elapsed().as_secs() >= 30 {
                last_cron_check = std::time::Instant::now();
                self.tick_cron().await;
            }

            // 4b. Cleanup nach konfiguriertem Intervall (default 60s)
            let cleanup_interval = {
                let cfg = self.config.read().await;
                cfg.cleanup
                    .as_ref()
                    .map(|c| c.cleanup_interval_s)
                    .unwrap_or(60)
            };
            if last_cleanup.elapsed().as_secs() >= cleanup_interval {
                last_cleanup = std::time::Instant::now();
                self.run_cleanup(&cleanup_cfg).await;
                self.py_pool.cleanup_idle().await;
                // Log rotation based on config retention
                let retention = self.config.read().await.log_retention_days;
                self.pipeline.cleanup_logs(retention);
                // Stale IN_PROGRESS-Marker nach 10 Minuten auto-expiren.
                // Crash-dead-end-Protection: sonst würde ein einmal hängen-
                // gebliebener Marker alle zukünftigen Retries blocken.
                let _ =
                    crate::store::idempotency_expire_in_progress(&self.pipeline.store.pool, 600);
                // Alte completed Idempotency-Einträge wegrotieren (30 Tage)
                let _ = crate::store::idempotency_cleanup(&self.pipeline.store.pool, 30);
            }

            tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;
        }
    }

    async fn tick_cron(&self) {
        let cfg = self.config.read().await;
        // Blockierte Module duerfen auch ueber Cron/Autopilot NICHT feuern —
        // sonst umgeht ein geblocktes Modul die run()-Sperre (Z. 380–387).
        let blocked = crate::security::blocked_module_ids(&cfg);
        // Gefeuert werden cron-Module UND Autopilot-Module (settings.autopilot==true):
        // letztere tragen schedule + cron_anweisung (cron_typ "llm") direkt am Tool-Modul
        // (z.B. content_planner, typ "content_planner"). Vorher feuerte tick_cron NUR
        // typ=="cron" — der Autopilot-Scan lief deshalb NIE (der eigentliche Bug).
        for modul in cfg.module.iter().filter(|m| {
            !blocked.contains(&m.id)
                && (m.typ == "cron"
                    || m.settings.extra.get("autopilot").and_then(|v| v.as_bool()) == Some(true))
        }) {
            let Some(ref schedule) = modul.settings.schedule else {
                continue;
            };
            if !cron_matches_now(schedule) {
                continue;
            }

            if should_skip_empty_workflow_tick(&self.pipeline, &cfg, modul) {
                continue;
            }

            // Dedup guard: store::cron_try_claim atomar in SQL-Transaktion. Schließt
            // die Race zwischen "dedup-check" und "task-spawn" die das alte JSON-
            // File-basierte System hatte (Crash zwischen den beiden → Cron feuerte
            // nach Restart doppelt).
            let now_key = chrono::Local::now().format("%Y-%m-%d %H:%M").to_string();
            match crate::store::cron_try_claim(&self.pipeline.store.pool, &modul.id, &now_key) {
                Ok(true) => { /* claimed — weiter */ }
                Ok(false) => continue, // schon gefeuert diese Minute
                Err(e) => {
                    self.pipeline.log(
                        "cron",
                        None,
                        LogTyp::Error,
                        &format!("cron_try_claim fehlgeschlagen: {}", e),
                    );
                    continue;
                }
            }

            let cron_typ = modul.settings.cron_typ.as_deref().unwrap_or("direct");

            match cron_typ {
                "direct" => {
                    // Direct tool call — no LLM
                    if let Some(ref tool) = modul.settings.cron_tool {
                        let params = modul.settings.cron_params.clone().unwrap_or_default();
                        let target = modul.settings.target_modul.as_deref().unwrap_or(&modul.id);

                        // If on_success/on_failure configured, wrap in a chain
                        let mut chain_steps = vec![crate::types::ChainStep {
                            tool: tool.clone(),
                            params: params.clone(),
                            condition: None,
                            stop_on_fail: true,
                        }];

                        if let Some(ref on_success) = modul.settings.on_success {
                            chain_steps.push(crate::types::ChainStep {
                                tool: on_success.clone(),
                                params: modul
                                    .settings
                                    .on_success_params
                                    .clone()
                                    .unwrap_or_default(),
                                condition: Some("success".to_string()),
                                stop_on_fail: false,
                            });
                        }
                        if let Some(ref on_failure) = modul.settings.on_failure {
                            chain_steps.push(crate::types::ChainStep {
                                tool: on_failure.clone(),
                                params: modul
                                    .settings
                                    .on_failure_params
                                    .clone()
                                    .unwrap_or_default(),
                                condition: Some("failed".to_string()),
                                stop_on_fail: false,
                            });
                        }

                        if chain_steps.len() > 1 {
                            // Has callbacks — use chain execution
                            let chain_json =
                                serde_json::to_string(&chain_steps).unwrap_or_default();
                            let mut aufgabe = Aufgabe::direct(
                                "__chain__",
                                vec![chain_json],
                                target,
                                &modul.id,
                                None,
                                None,
                            );
                            aufgabe.anweisung = format!("Cron: {} + callbacks", tool);
                            if self.pipeline.speichern(&aufgabe).is_ok() {
                                self.pipeline.log(
                                    "cron",
                                    Some(&aufgabe.id),
                                    LogTyp::Info,
                                    &format!(
                                        "Cron '{}' triggered: {} (with callbacks)",
                                        modul.id, tool
                                    ),
                                );
                            }
                        } else {
                            // Simple direct task, no callbacks
                            let aufgabe =
                                Aufgabe::direct(tool, params, target, &modul.id, None, None);
                            if self.pipeline.speichern(&aufgabe).is_ok() {
                                self.pipeline.log(
                                    "cron",
                                    Some(&aufgabe.id),
                                    LogTyp::Info,
                                    &format!("Cron '{}' triggered: {}()", modul.id, tool),
                                );
                            }
                        }
                    }
                }
                "llm" => {
                    // LLM task
                    let target = modul.settings.target_modul.as_deref().unwrap_or(&modul.id);
                    let anweisung = modul
                        .settings
                        .cron_anweisung
                        .as_deref()
                        .unwrap_or("Cron task");
                    let target_timeout = cfg
                        .module
                        .iter()
                        .find(|m| m.id == target || m.name == target)
                        .map(|m| m.timeout_s)
                        .unwrap_or(modul.timeout_s);
                    let aufgabe = Aufgabe::llm_call(anweisung, target, &modul.id, None)
                        .with_timeout_s(target_timeout);
                    if self.pipeline.speichern(&aufgabe).is_ok() {
                        self.pipeline.log(
                            "cron",
                            Some(&aufgabe.id),
                            LogTyp::Info,
                            &format!("Cron '{}' triggered: LLM task for {}", modul.id, target),
                        );
                    }
                }
                "chain" => {
                    if let Some(ref chain) = modul.settings.chain {
                        if chain.is_empty() {
                            continue;
                        }
                        let target = modul.settings.target_modul.as_deref().unwrap_or(&modul.id);

                        // Create a task that will execute the full chain
                        // We store the chain spec in the params field as JSON
                        let chain_json = serde_json::to_string(chain).unwrap_or_default();
                        let mut aufgabe = Aufgabe::direct(
                            "__chain__",
                            vec![chain_json],
                            target,
                            &modul.id,
                            None,
                            None,
                        );
                        aufgabe.anweisung = format!("Chain: {} steps", chain.len());
                        if self.pipeline.speichern(&aufgabe).is_ok() {
                            self.pipeline.log(
                                "cron",
                                Some(&aufgabe.id),
                                LogTyp::Info,
                                &format!(
                                    "Cron chain '{}' triggered: {} steps",
                                    modul.id,
                                    chain.len()
                                ),
                            );
                        }
                    }
                }
                _ => {
                    self.pipeline.log(
                        "cron",
                        None,
                        LogTyp::Warning,
                        &format!("Unknown cron_typ '{}' for {}", cron_typ, modul.id),
                    );
                }
            }
        }
    }

    async fn run_cleanup(&self, cleanup_cfg: &Option<CleanupConfig>) {
        // Erledigt-Cleanup
        if let Some(cc) = cleanup_cfg {
            self.pipeline
                .cleanup_erledigt(cc.max_erledigt, cc.max_alter_tage);
        }

        // Temp-Agent-Cleanup: TTL gegen `created_at` prüfen, NICHT gegen Heartbeat.
        // Der Heartbeat wird alle ~2s aktualisiert (Scheduler-Loop), also war
        // `now - heartbeat` immer klein und der TTL hat nie getriggert. Jetzt
        // prüfen wir "wann wurde das Modul geboren?" gegen TTL.
        let cfg = self.config.read().await;
        let now = chrono::Utc::now().timestamp() as u64;
        let expired: Vec<String> = cfg
            .module
            .iter()
            .filter(|m| m.spawned_by.is_some() && m.spawn_ttl_s.is_some())
            .filter(|m| {
                let ttl = m.spawn_ttl_s.unwrap();
                // Rückwärtskompatibilität: falls created_at fehlt (alte Module) nicht
                // canceln — sonst würde ein Upgrade alle bestehenden Temp-Agents killen.
                match m.created_at {
                    Some(born) => now.saturating_sub(born) > ttl,
                    None => false,
                }
            })
            .map(|m| m.id.clone())
            .collect();
        drop(cfg);

        // Lock-Order-Invariante: Mutex ZUERST, RwLock DRIN. Alle Config-
        // Mutations-Pfade (Web-API, Orchestrator-Cleanup, load_temp_modules,
        // Wizard-Commit) nutzen dieselbe Reihenfolge → kein Deadlock, kein
        // stale-snapshot-Problem (GLM-Finding Run SQLite-9: das frühere
        // "drop RwLock, dann Mutex" hatte ein Race-Window in dem ein anderer
        // Writer persistieren konnte, der Orchestrator-Snapshot wurde dann
        // stale überschrieben). Mit Mutex-first ist der komplette read-
        // modify-write-Zyklus atomar; Reader die nur config.read() wollen
        // warten bloß auf den RwLock-Write-Guard, was kurz ist (kein Disk-I/O
        // unter RwLock-Write; der atomic_write passiert nachdem der RwLock
        // gedroppt ist aber innerhalb des Mutex).
        let write_guard = self.pipeline.config_write_lock.lock().await;
        let (serialized, changed) = {
            let mut cfg = self.config.write().await;
            let erstellt = self.pipeline.erstellt();
            let gestartet = self.pipeline.gestartet();
            let busy_snapshot = self.busy.read().await.clone();
            let before_count = cfg.module.len();

            cfg.module.retain(|m| {
                if m.persistent {
                    return true;
                }
                if m.spawned_by.is_none() {
                    return true;
                }

                if expired.contains(&m.id) {
                    self.pipeline.log(
                        "orchestrator",
                        None,
                        LogTyp::Info,
                        &format!("Temp-Agent '{}' TTL abgelaufen — wird entfernt", m.id),
                    );
                    return false;
                }

                let has_active = erstellt.iter().any(|a| a.modul == m.id)
                    || gestartet.iter().any(|a| a.modul == m.id);
                if has_active {
                    return true;
                }
                if busy_snapshot.contains_key(&m.id) {
                    return true;
                }

                self.pipeline.log(
                    "orchestrator",
                    None,
                    LogTyp::Info,
                    &format!(
                        "Temp-Agent '{}' aufgeraeumt (idle, spawned by {})",
                        m.id,
                        m.spawned_by.as_deref().unwrap_or("?")
                    ),
                );
                false
            });

            let changed = cfg.module.len() < before_count;
            let json = if changed {
                serde_json::to_string_pretty(&*cfg).ok()
            } else {
                None
            };
            (json, changed)
            // RwLock-Write gedroppt beim scope-exit — Reader können wieder durch
        };
        if changed {
            if let Some(json) = serialized {
                let path = self.pipeline.base.join("config.json");
                let _ = util::atomic_write(&path, json.as_bytes());
            }
        }
        drop(write_guard); // Mutex explizit droppen für Klarheit

        // Prune stale cron fire tracking (Module die nicht mehr existieren).
        // Read-only snapshot — kein Write-Lock mehr nötig da cfg oben gedroppt.
        let module_ids: std::collections::HashSet<String> = {
            let cfg = self.config.read().await;
            cfg.module.iter().map(|m| m.id.clone()).collect()
        };
        let module_ids_vec: Vec<String> = module_ids.iter().cloned().collect();
        let _ = crate::store::cron_prune_stale(&self.pipeline.store.pool, &module_ids_vec);

        // Orphan-Task-Cleanup: Tasks deren Modul gelöscht wurde werden als
        // FAILED markiert — sonst liegen sie ewig in erstellt/ und niemand
        // claimed sie. Sichtbar für den User im UI statt stumm zu leaken
        // (DeepSeek-Finding Run SQLite-8).
        for status in &["erstellt", "gestartet"] {
            if let Ok(rows) = crate::store::task_list_by_status(&self.pipeline.store.pool, status) {
                for row in rows {
                    if !module_ids.contains(&row.modul) {
                        if let Ok(mut a) = serde_json::from_str::<Aufgabe>(&row.payload_json) {
                            a.ergebnis = Some(format!(
                                "FAILED: Zielmodul '{}' existiert nicht mehr (gelöscht nach Task-Erstellung)",
                                a.modul,
                            ));
                            let _ = self.pipeline.verschieben(&mut a, AufgabeStatus::Failed);
                            self.pipeline.log(
                                "orchestrator",
                                Some(&a.id),
                                LogTyp::Warning,
                                &format!(
                                    "Orphan-Task (Modul '{}' weg) auf FAILED gesetzt",
                                    row.modul
                                ),
                            );
                        }
                    }
                }
            }
        }
    }

    /// Liest gespawnte Temp-Agent-Specs aus `temp_modules/*.json` und integriert sie
    /// in die Live-Config. Two-Phase-Commit: erst config.json atomic persistieren
    /// (damit der Temp-Agent einen Neustart überlebt), DANN die Spec-Datei löschen.
    /// Crash zwischen push und persist → Spec wird beim nächsten Start erneut geladen.
    /// Crash zwischen persist und spec-delete → nächster Start sieht das Modul bereits
    /// in der Config und löscht nur noch die Spec (idempotent).
    /// GPT-Finding: vorher wurde nur in-memory gepusht, Spec sofort gelöscht → Crash =
    /// Temp-Agent weg, Task aber bereits in erstellt/, blieb für immer orphan.
    async fn load_temp_modules(&self) {
        let temp_dir = self.pipeline.base.join("temp_modules");
        if !temp_dir.exists() {
            return;
        }

        let entries: Vec<_> = match std::fs::read_dir(&temp_dir) {
            Ok(e) => e.flatten().collect(),
            Err(_) => return,
        };
        if entries.is_empty() {
            return;
        }

        let config_path = self.pipeline.base.join("config.json");

        // Lock-Order: Mutex zuerst, dann RwLock — selbe Reihenfolge wie im
        // run_cleanup-Pfad und wie in save_config (Web-API). Kein Deadlock
        // solange alle Schreiber dieser Ordnung folgen. Keine stale-snapshot-
        // Races mehr (GLM-Finding Run SQLite-9).
        for entry in entries {
            if !entry.path().extension().is_some_and(|e| e == "json") {
                continue;
            }

            let content = match std::fs::read_to_string(entry.path()) {
                Ok(c) => c,
                Err(_) => continue,
            };
            let spec: serde_json::Value = match serde_json::from_str(&content) {
                Ok(v) => v,
                Err(_) => continue,
            };
            let Ok(modul) =
                serde_json::from_value::<crate::types::ModulConfig>(spec["module"].clone())
            else {
                continue;
            };

            let write_guard = self.pipeline.config_write_lock.lock().await;
            let mut cfg = self.config.write().await;

            if cfg.module.iter().any(|m| m.id == modul.id) {
                drop(cfg);
                drop(write_guard);
                let _ = std::fs::remove_file(entry.path());
                continue;
            }

            self.pipeline.log(
                "orchestrator",
                None,
                LogTyp::Info,
                &format!("Temp-Agent '{}' aus spec geladen", modul.id),
            );
            cfg.module.push(modul.clone());

            let persist_ok = match serde_json::to_string_pretty(&*cfg) {
                Ok(json) => util::atomic_write(&config_path, json.as_bytes()).is_ok(),
                Err(_) => false,
            };

            if !persist_ok {
                // Rollback in-memory push, Spec bleibt (nächster Tick retries)
                cfg.module.retain(|m| m.id != modul.id);
                drop(cfg);
                drop(write_guard);
                self.pipeline.log(
                    "orchestrator",
                    None,
                    LogTyp::Error,
                    &format!(
                        "load_temp_modules '{}': config persist failed, rollback",
                        modul.id
                    ),
                );
                continue;
            }

            // Locks können jetzt gedroppt werden — Task-Schreiben + Spec-Cleanup
            // brauchen sie nicht.
            drop(cfg);
            drop(write_guard);

            if let Ok(aufgabe) = serde_json::from_value::<Aufgabe>(spec["task"].clone()) {
                let _ = self.pipeline.speichern(&aufgabe);
            }
            let _ = std::fs::remove_file(entry.path());
        }
    }
}

// ═══ ModulScheduler ══════════════════════════════════
// Einer pro Modul, laeuft als eigener Tokio-Task.

struct ModulScheduler {
    modul_id: String,
    interval_ms: u64,
    max_concurrent: u32,
    pipeline: Arc<Pipeline>,
    config: Arc<RwLock<AgentConfig>>,
    llm: Arc<LlmRouter>,
    py_modules: Arc<RwLock<Vec<crate::loader::PyModuleMeta>>>,
    py_pool: Arc<crate::loader::PyProcessPool>,
    busy: BusyMap,
    handles: HandleMap,
    heartbeats: HeartbeatMap,
    tokens: crate::web::TokenTracker,
}

impl ModulScheduler {
    async fn run(&self) {
        self.pipeline.log(
            &self.modul_id,
            None,
            LogTyp::Info,
            &format!(
                "ModulScheduler '{}' laeuft (interval: {}ms)",
                self.modul_id, self.interval_ms
            ),
        );

        loop {
            // Heartbeat updaten
            {
                let mut hb = self.heartbeats.write().await;
                hb.insert(self.modul_id.clone(), chrono::Utc::now().timestamp() as u64);
            }

            self.tick().await;

            tokio::time::sleep(tokio::time::Duration::from_millis(self.interval_ms)).await;
        }
    }

    async fn tick(&self) {
        // Crash recovery — nur wenn Instanz noch Kapazitaet hat
        for aufgabe in self.pipeline.gestartet() {
            if aufgabe.modul != self.modul_id {
                continue;
            } // Nur EIGENE Aufgaben
            if aufgabe.erstellt_von.starts_with("chat:") {
                continue;
            } // Live-HTTP-Chat gehoert nicht dem Scheduler
            if aufgabe.parent_id.is_some() {
                continue;
            } // Tool-Subtasks sind Nachvollziehbarkeits-Records, keine eigenstaendigen Recovery-Jobs
            if aufgabe.status == AufgabeStatus::Failed {
                continue;
            }
            let b = self.busy.read().await;
            let current = b.get(&self.modul_id).map(|v| v.len()).unwrap_or(0);
            if current >= self.max_concurrent as usize {
                drop(b);
                continue;
            } // Kapazitaet erreicht
            // Check if this specific task is already being processed
            if b.get(&self.modul_id)
                .map(|v| v.contains(&aufgabe.id))
                .unwrap_or(false)
            {
                drop(b);
                continue;
            }
            drop(b);
            self.pipeline.log(
                &self.modul_id,
                Some(&aufgabe.id),
                LogTyp::Warning,
                "Recovery: Aufgabe wird fortgesetzt",
            );
            self.spawn_aufgabe(aufgabe).await;
        }

        // Neue Aufgaben — atomic claim via SQL mit Fälligkeits-Filter direkt in
        // der WHERE-Clause (faellig_ab_ts <= now). Das fixt den Scheduler-Fairness-
        // Bug: früher wurde die ÄLTESTE Task geclaimed, falls die noch nicht
        // fällig war → break → spätere Tasks derselben Queue die eigentlich
        // schon fällig waren, wurden bis zum nächsten Tick nie erreicht.
        loop {
            let b = self.busy.read().await;
            let current = b.get(&self.modul_id).map(|v| v.len()).unwrap_or(0);
            if current >= self.max_concurrent as usize {
                drop(b);
                break;
            }
            drop(b);

            match self.pipeline.claim_for_modul(&self.modul_id) {
                Ok(Some(aufgabe)) => self.spawn_aufgabe(aufgabe).await,
                Ok(None) => break, // keine fälligen Tasks mehr in dieser Tick
                Err(e) => {
                    self.pipeline.log(
                        &self.modul_id,
                        None,
                        LogTyp::Error,
                        &format!("claim_for_modul failed: {}", e),
                    );
                    break;
                }
            }
        }
    }

    async fn spawn_aufgabe(&self, mut aufgabe: Aufgabe) {
        // Tasks coming from erstellt/ are already atomically claimed (status=Gestartet).
        // Only crash-recovery path comes in with status=Gestartet already too — so we
        // never need to call verschieben here; just ensure status is Gestartet.
        if aufgabe.status == AufgabeStatus::Erstellt {
            if let Err(e) = self
                .pipeline
                .verschieben(&mut aufgabe, AufgabeStatus::Gestartet)
            {
                self.pipeline.log(
                    &self.modul_id,
                    Some(&aufgabe.id),
                    LogTyp::Error,
                    &format!("Verschieben failed: {e}"),
                );
                return;
            }
        }

        // Instanz als busy markieren
        {
            let mut b = self.busy.write().await;
            let current = b.get(&self.modul_id).map(|v| v.len()).unwrap_or(0);
            if current >= self.max_concurrent as usize {
                // At capacity — another tick beat us to it, skip
                return;
            }
            b.entry(aufgabe.modul.clone())
                .or_default()
                .push(aufgabe.id.clone());
        }

        self.pipeline.log(
            &self.modul_id,
            Some(&aufgabe.id),
            LogTyp::Info,
            &format!(
                "[{}] {} (async)",
                match aufgabe.typ {
                    AufgabeTyp::Direct => "DIRECT",
                    AufgabeTyp::LlmCall => "LLM",
                    AufgabeTyp::ChatReply => "REPLY",
                },
                util::safe_truncate(&aufgabe.anweisung, 80)
            ),
        );

        // Alles clonen fuer den spawned Task
        let pipeline = self.pipeline.clone();
        let config = self.config.clone();
        let llm = self.llm.clone();
        let py_modules = self.py_modules.clone();
        let py_pool = self.py_pool.clone();
        let busy = self.busy.clone();
        let handles = self.handles.clone();
        let tokens = self.tokens.clone();

        let aufgabe_id_outer = aufgabe.id.clone();
        let aufgabe_modul_outer = aufgabe.modul.clone();

        let join = tokio::spawn(async move {
            let idle_timeout_s = aufgabe.timeout_s.max(MIN_TASK_IDLE_TIMEOUT_S);
            let aufgabe_id = aufgabe.id.clone();
            let aufgabe_modul = aufgabe.modul.clone();
            let aufgabe_timeout = aufgabe.timeout_s;
            let aufgabe_typ = aufgabe.typ.clone();

            // RAII-Cleanup-Guard. Räumt busy + handles IMMER auf — egal ob
            // exec_llm normal returned, timeout fired, oder ein Panic hochkommt
            // (Unwinding ruft Drop). Ohne Guard würde ein Panic den Task
            // abrupt killen und die Map-Einträge für immer stehenlassen
            // (→ Modul frozen bei max_concurrent).
            let _guard = BusyGuard::new(
                busy.clone(),
                handles.clone(),
                aufgabe_modul.clone(),
                aufgabe_id.clone(),
            );

            let timed_out = match aufgabe_typ {
                AufgabeTyp::Direct => {
                    let timeout_duration = std::time::Duration::from_secs(idle_timeout_s);
                    tokio::time::timeout(
                        timeout_duration,
                        exec_direct(
                            &mut aufgabe,
                            &pipeline,
                            &config,
                            &llm,
                            &py_modules,
                            &py_pool,
                        ),
                    )
                    .await
                    .is_err()
                }
                AufgabeTyp::LlmCall => {
                    let activity = Arc::new(AtomicI64::new(now_ts()));
                    tokio::select! {
                        _ = exec_llm(
                            &mut aufgabe,
                            &pipeline,
                            &config,
                            &llm,
                            &py_modules,
                            &py_pool,
                            &tokens,
                            Some(activity.clone()),
                        ) => false,
                        _ = wait_for_idle_timeout(activity, idle_timeout_s) => true,
                    }
                }
                AufgabeTyp::ChatReply => {
                    aufgabe.ergebnis = Some(aufgabe.anweisung.clone());
                    if let Err(e) = pipeline.verschieben(&mut aufgabe, AufgabeStatus::Success) {
                        pipeline.log(
                            "cycle",
                            Some(&aufgabe.id),
                            LogTyp::Error,
                            &format!("Verschieben failed: {e}"),
                        );
                    }
                    false
                }
            };

            if timed_out {
                pipeline.log(
                    "cycle",
                    Some(&aufgabe_id),
                    LogTyp::Error,
                    &format!(
                        "Task idle-timeout nach {}s ohne Fortschritt — abgebrochen",
                        aufgabe_timeout.max(MIN_TASK_IDLE_TIMEOUT_S)
                    ),
                );
                if let Ok(Some(mut failed)) = pipeline.laden_by_id(&aufgabe_id) {
                    failed.ergebnis = Some(format!(
                        "FAILED: Idle-Timeout nach {}s ohne Fortschritt",
                        aufgabe_timeout.max(MIN_TASK_IDLE_TIMEOUT_S)
                    ));
                    if let Err(e) = pipeline.verschieben(&mut failed, AufgabeStatus::Failed) {
                        pipeline.log(
                            "cycle",
                            Some(&aufgabe_id),
                            LogTyp::Error,
                            &format!("Verschieben failed: {e}"),
                        );
                    }
                }
            }
            // Guard dropped hier → Cleanup-Task wird gespawnt. Funktioniert auch
            // wenn wir via Panic statt normalem Return hier rauskommen.
        });

        // AbortHandle in HandleMap eintragen, damit der Watchdog bei Scheduler-Tod
        // die Task hart abbrechen kann BEVOR BusyMap freigegeben wird.
        // Ohne das Abort würde der alte Task weiterlaufen während der neue Scheduler
        // denselben Task re-pickt (Double-Execution, das 7/7-Finding).
        {
            let mut h = self.handles.write().await;
            h.entry(aufgabe_modul_outer)
                .or_default()
                .insert(aufgabe_id_outer, join.abort_handle());
        }
    }
}

// ist_faellig() wurde entfernt — der Fälligkeits-Filter läuft jetzt direkt in
// SQL via `claim_one_for_modul` WHERE faellig_ab_ts <= now. store::parse_faellig_ab
// parst "wann" beim speichern zu einem Timestamp und speichert ihn in der
// tasks.faellig_ab_ts-Spalte. Scheduler-Fairness ist dadurch auf DB-Ebene
// garantiert (keine Starvation durch zukunfts-datierte Tasks).

// ═══ Chain Execution Engine ══════════════════════════════

/// Execute a chain of tool steps sequentially. No LLM involved.
/// `task_id` wird an jeden Step weitergegeben → Chain-Steps sind idempotent
/// (Step 1 lief schon → Step 2 crasht → Recovery ruft Chain nochmal → Step 1
/// liefert cached result, Step 2 läuft neu).
async fn execute_chain(
    chain: &[crate::types::ChainStep],
    modul_id: &str,
    task_id: Option<&str>,
    pipeline: &Arc<Pipeline>,
    config: &Arc<RwLock<AgentConfig>>,
    llm: &Arc<LlmRouter>,
    py_modules: &Arc<RwLock<Vec<crate::loader::PyModuleMeta>>>,
    py_pool: &Arc<crate::loader::PyProcessPool>,
) -> (bool, String) {
    let mut last_result = String::new();
    let mut last_success = true;

    for (i, step) in chain.iter().enumerate() {
        // Evaluate condition if present
        if let Some(ref cond) = step.condition {
            if !evaluate_condition(cond, &last_result, last_success) {
                pipeline.log(
                    "chain",
                    None,
                    LogTyp::Info,
                    &format!("Step {} skipped (condition '{}' not met)", i + 1, cond),
                );
                continue;
            }
        }

        // Replace {result} placeholder in params
        let params: Vec<String> = step
            .params
            .iter()
            .map(|p| p.replace("{result}", &last_result))
            .collect();

        pipeline.log(
            "chain",
            None,
            LogTyp::Info,
            &format!(
                "Chain step {}/{}: {}({})",
                i + 1,
                chain.len(),
                step.tool,
                params.join(", ")
            ),
        );

        // Chain-Step-Idempotency: task_id + Step-Index als stabiler Key. Ein
        // bereits-erfolgreicher Step wird bei Retry (Watchdog-Abort + Re-Claim)
        // aus Cache bedient, sein Seiteneffekt nicht doppelt ausgeführt.
        let step_task_id = task_id.map(|t| format!("{}#step{}", t, i));
        let result = exec_tool(
            &step.tool,
            &params,
            modul_id,
            step_task_id.as_deref(),
            pipeline,
            config,
            llm,
            py_modules,
            py_pool,
            None,
        )
        .await;
        last_success = result.0;
        last_result = result.1;

        pipeline.log(
            "chain",
            None,
            if last_success {
                LogTyp::Success
            } else {
                LogTyp::Failed
            },
            &format!(
                "Chain step {}: {} -> {}",
                i + 1,
                if last_success { "OK" } else { "FAIL" },
                util::safe_truncate(&last_result, 80)
            ),
        );

        // Stop on failure if configured
        if !last_success && step.stop_on_fail {
            pipeline.log(
                "chain",
                None,
                LogTyp::Warning,
                &format!("Chain aborted at step {} (stop_on_fail=true)", i + 1),
            );
            break;
        }
    }

    (last_success, last_result)
}

/// Evaluate a chain step condition
fn evaluate_condition(condition: &str, last_result: &str, last_success: bool) -> bool {
    let cond = condition.trim();

    if cond == "success" {
        return last_success;
    }
    if cond == "failed" {
        return !last_success;
    }

    if let Some(text) = cond.strip_prefix("contains:") {
        return last_result.contains(text.trim());
    }
    if let Some(text) = cond.strip_prefix("not_contains:") {
        return !last_result.contains(text.trim());
    }
    if let Some(text) = cond.strip_prefix("starts_with:") {
        return last_result.starts_with(text.trim());
    }
    if let Some(text) = cond.strip_prefix("equals:") {
        return last_result.trim() == text.trim();
    }

    // Unknown condition — default to true (execute the step)
    true
}

// ═══ Standalone Funktionen (fuer tokio::spawn) ═══════════

async fn exec_direct(
    aufgabe: &mut Aufgabe,
    pipeline: &Arc<Pipeline>,
    config: &Arc<RwLock<AgentConfig>>,
    llm: &Arc<LlmRouter>,
    py_modules: &Arc<RwLock<Vec<crate::loader::PyModuleMeta>>>,
    py_pool: &Arc<crate::loader::PyProcessPool>,
) {
    let tool_name = match &aufgabe.tool {
        Some(t) => t.clone(),
        None => {
            aufgabe.ergebnis = Some("FAILED: Kein Tool angegeben".into());
            if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                pipeline.log(
                    "cycle",
                    Some(&aufgabe.id),
                    LogTyp::Error,
                    &format!("Verschieben failed: {e}"),
                );
            }
            return;
        }
    };

    // Chain execution: special tool name "__chain__"
    if tool_name == "__chain__" {
        let chain_json = aufgabe.params.first().map(|s| s.as_str()).unwrap_or("[]");
        match serde_json::from_str::<Vec<crate::types::ChainStep>>(chain_json) {
            Ok(chain) => {
                let (success, result) = execute_chain(
                    &chain,
                    &aufgabe.modul,
                    Some(&aufgabe.id),
                    pipeline,
                    config,
                    llm,
                    py_modules,
                    py_pool,
                )
                .await;
                aufgabe.ergebnis = Some(result);
                if let Err(e) = pipeline.verschieben(
                    aufgabe,
                    if success {
                        AufgabeStatus::Success
                    } else {
                        AufgabeStatus::Failed
                    },
                ) {
                    pipeline.log(
                        "cycle",
                        Some(&aufgabe.id),
                        LogTyp::Error,
                        &format!("Verschieben failed: {e}"),
                    );
                }
                // Route result
                let cfg = config.read().await;
                route_ergebnis(aufgabe, pipeline, &cfg);
                return;
            }
            Err(e) => {
                aufgabe.ergebnis = Some(format!("FAILED: Chain parse error: {}", e));
                if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                    pipeline.log(
                        "cycle",
                        Some(&aufgabe.id),
                        LogTyp::Error,
                        &format!("Verschieben failed: {e}"),
                    );
                }
                return;
            }
        }
    }

    pipeline.log(
        "cycle",
        Some(&aufgabe.id),
        LogTyp::Info,
        &format!("Direct tool: {}({})", tool_name, aufgabe.params.join(", ")),
    );

    let result = exec_tool(
        &tool_name,
        &aufgabe.params,
        &aufgabe.modul,
        Some(&aufgabe.id),
        pipeline,
        config,
        llm,
        py_modules,
        py_pool,
        None,
    )
    .await;

    let status = if result.0 { "SUCCESS" } else { "FAILED" };
    pipeline.log(
        "cycle",
        Some(&aufgabe.id),
        if result.0 {
            LogTyp::Success
        } else {
            LogTyp::Failed
        },
        &format!(
            "Tool {}: {} → {}",
            tool_name,
            status,
            util::safe_truncate(&result.1, 100)
        ),
    );

    let antwort = if let Some(template) = &aufgabe.antwort_template {
        template.replace("<RESULT>", &result.1)
    } else {
        result.1.clone()
    };

    aufgabe.ergebnis = Some(antwort);
    if let Err(e) = pipeline.verschieben(
        aufgabe,
        if result.0 {
            AufgabeStatus::Success
        } else {
            AufgabeStatus::Failed
        },
    ) {
        pipeline.log(
            "cycle",
            Some(&aufgabe.id),
            LogTyp::Error,
            &format!("Verschieben failed: {e}"),
        );
    }
    // Route result back if zurueck_an is set
    if aufgabe.status == AufgabeStatus::Success || aufgabe.status == AufgabeStatus::Failed {
        let cfg = config.read().await;
        route_ergebnis(aufgabe, pipeline, &cfg);
    }
}

async fn exec_llm(
    aufgabe: &mut Aufgabe,
    pipeline: &Arc<Pipeline>,
    config: &Arc<RwLock<AgentConfig>>,
    llm: &Arc<LlmRouter>,
    py_modules: &Arc<RwLock<Vec<crate::loader::PyModuleMeta>>>,
    py_pool: &Arc<crate::loader::PyProcessPool>,
    tokens: &crate::web::TokenTracker,
    activity: Option<ActivityMarker>,
) {
    mark_activity(&activity);
    let cfg = config.read().await;
    let modul = cfg
        .module
        .iter()
        .find(|m| m.id == aufgabe.modul)
        .or_else(|| cfg.module.iter().find(|m| m.name == aufgabe.modul));
    let Some(modul) = modul.cloned() else {
        aufgabe.ergebnis = Some(format!("FAILED: Modul '{}' nicht gefunden", aufgabe.modul));
        let _ = pipeline.verschieben(aufgabe, AufgabeStatus::Failed);
        return;
    };
    drop(cfg);

    let token_budget = modul.token_budget.unwrap_or(0);
    let token_budget_warning = modul.token_budget_warning.unwrap_or(0);

    let home = pipeline.home_dir(&modul.id);
    let home_info = format!("\nDein Home-Verzeichnis ist: {}\n", home.display());
    let date_str = chrono::Utc::now().format("%d.%m.%Y %H:%M UTC").to_string();

    // Get identity — falls back to LLM backend identity if module doesn't customize
    let identity = {
        let cfg2 = config.read().await;
        util::resolve_identity(&modul, &cfg2)
    };
    let system_with_date = identity.system_prompt.replace("{date}", &date_str);
    let full_system = format!("{}{}", system_with_date, home_info);
    let tool_calls_disabled = modul
        .settings
        .extra
        .get("disable_tool_calls")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    // OpenAI Function Calling Tools
    let (openai_tools, py_mods_snap) = {
        let py_mods = py_modules.read().await;
        let tools = if tool_calls_disabled {
            Vec::new()
        } else {
            tools::tools_as_openai_json(&modul, &py_mods)
        };
        let snap = py_mods.clone();
        (tools, snap)
    };

    // Snapshot guardrail config and full config once before the loop.
    let (gcfg, cfg_snap) = {
        let cfg_guard = config.read().await;
        let gcfg = cfg_guard
            .guardrail
            .clone()
            .unwrap_or_else(crate::types::GuardrailConfig::default);
        let cfg_snap = cfg_guard.clone();
        (gcfg, cfg_snap)
    };
    let backend_id = modul.llm_backend.clone();
    let model_str = cfg_snap
        .llm_backends
        .iter()
        .find(|b| b.id == backend_id)
        .map(|b| b.model.clone())
        .unwrap_or_default();
    let task_id = aufgabe.id.clone();
    let task_modul_id = aufgabe.modul.clone();
    let mut engine = crate::turn::TurnEngine {
        pipeline,
        llm,
        cfg_snap: &cfg_snap,
        gcfg: &gcfg,
        py_mods_snap: &py_mods_snap,
        modul: Some(&modul),
        log_label: &modul.name,
        log_task_id: Some(&task_id),
        attribution_id: &task_modul_id,
        tokens,
        status_tx: None,
        activity: activity.clone(),
        tool_calls_disabled,
        backup_id: modul.backup_llm.clone(),
        history_fixed_prefix: 2, // system + user-Auftrag
        tool_choice_once: None,
        backend_id,
        model_str,
        guardrail_retries: 0,
        used_fallback: false,
    };

    let mut messages: Vec<serde_json::Value> = vec![];
    messages.push(serde_json::json!({"role": "system", "content": full_system}));
    messages.push(serde_json::json!({"role": "user", "content": aufgabe.anweisung.clone()}));

    let final_answer;
    let mut tool_round = 0;
    let mut total_tokens: u64 = 0;

    loop {
        if let Some(max_tool_rounds) = cfg_snap
            .llm_backends
            .iter()
            .find(|b| b.id == engine.backend_id)
            .and_then(|b| b.tool_round_limit())
        {
            if tool_round >= max_tool_rounds {
                aufgabe.ergebnis = Some(format!(
                    "FAILED: LLM tool rounds limit ({}) erreicht",
                    max_tool_rounds
                ));
                pipeline.log(
                    &modul.name,
                    Some(&aufgabe.id),
                    LogTyp::Failed,
                    &format!(
                        "LLM tool rounds limit ({}) erreicht — Task abgebrochen",
                        max_tool_rounds
                    ),
                );
                if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                    pipeline.log(
                        "cycle",
                        Some(&aufgabe.id),
                        LogTyp::Error,
                        &format!("Verschieben failed: {e}"),
                    );
                }
                return;
            }
        }

        // Token budget check (per-modul, zählt Tokens dieses Tasks)
        if token_budget > 0 && total_tokens > token_budget {
            aufgabe.ergebnis = Some(format!(
                "FAILED: Token-Budget ueberschritten ({}/{})",
                total_tokens, token_budget
            ));
            pipeline.log(
                &modul.name,
                Some(&aufgabe.id),
                LogTyp::Failed,
                &format!(
                    "Token-Budget ueberschritten: {}/{} — Task abgebrochen",
                    total_tokens, token_budget
                ),
            );
            if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                pipeline.log(
                    "cycle",
                    Some(&aufgabe.id),
                    LogTyp::Error,
                    &format!("Verschieben failed: {e}"),
                );
            }
            return;
        }

        // Per-LLM Cost/Call-Cap. Wenn erreicht, wird der Task bis zum Fenster-Reset
        // zurueckgestellt statt als Failed markiert.
        {
            let cfg_live = config.read().await;
            if let Err(hit) = crate::web::check_llm_cap(
                &pipeline.store.pool,
                &cfg_live,
                &engine.backend_id,
                &messages,
                aufgabe.cap_override,
            )
            .await
            {
                drop(cfg_live);
                let msg = format!(
                    "{} (backend={}, model={}, reset={})",
                    hit.message(),
                    hit.backend_id,
                    hit.model,
                    hit.reset_iso()
                );
                pipeline.log(&modul.name, Some(&aufgabe.id), LogTyp::Warning, &msg);
                aufgabe.ergebnis = Some(msg);
                if let Err(e) = pipeline.reschedule(aufgabe, hit.reset_iso()) {
                    pipeline.log(
                        "cycle",
                        Some(&aufgabe.id),
                        LogTyp::Error,
                        &format!("Reschedule failed: {e}"),
                    );
                }
                return;
            }
        }

        // LLM-Runde: Rate-Slot, Call (mit Backup), Token-Tracking, Text-Tag-
        // Injektion, Guardrail-Retry/-Fallback, Multi-Call-Parsing — alles in
        // der geteilten Turn-Engine (gleiche Logik wie im Chat-Loop, web.rs).
        let (outcome, usage) = engine.run_round(&mut messages, &openai_tools).await;
        total_tokens += usage.total();

        // Token budget warning (pro Modul)
        if token_budget_warning > 0
            && total_tokens > token_budget_warning
            && total_tokens - usage.total() <= token_budget_warning
        {
            pipeline.log(
                &modul.name,
                Some(&aufgabe.id),
                LogTyp::Warning,
                &format!(
                    "Token-Budget Warnung: {}/{} Tokens verbraucht",
                    total_tokens, token_budget
                ),
            );
        }

        match outcome {
            crate::turn::RoundOutcome::ToolCalls {
                calls: parsed_calls,
                raw_message,
                ..
            } => {
                mark_activity(&activity);
                tool_round += 1;

                // Assistant-History VOR den Tool-Ergebnissen (provider-Felder wie
                // DeepSeek reasoning_content bleiben erhalten, jede Call-ID bekommt
                // genau eine role:"tool"-Antwort).
                messages.push(crate::turn::build_assistant_history(
                    &raw_message,
                    &parsed_calls,
                    |c| c.arguments_json.clone(),
                ));

                // Sub-Aufgaben anlegen (eine pro Call), bevor die Ausfuehrung startet.
                let mut sub_ids: Vec<String> = Vec::with_capacity(parsed_calls.len());
                for call in &parsed_calls {
                    pipeline.log(
                        &modul.name,
                        Some(&aufgabe.id),
                        LogTyp::Info,
                        &format!("Tool call: {}({})", call.name, call.params.join(", ")),
                    );
                    let mut tool_subtask = Aufgabe::direct(
                        &call.name,
                        call.params.clone(),
                        &aufgabe.modul,
                        &format!("task:{}", aufgabe.id),
                        None,
                        None,
                    );
                    tool_subtask.parent_id = Some(aufgabe.id.clone());
                    tool_subtask.status = AufgabeStatus::Gestartet;
                    tool_subtask.gestartet = Some(chrono::Utc::now());
                    sub_ids.push(tool_subtask.id.clone());
                    let _ = pipeline.speichern(&tool_subtask);
                }

                // Tool-Round im Idempotency-Key: LLM kann dasselbe Tool in einer
                // Task mehrfach rufen — nur KOMPLETTE Task-Wiederholungen werden
                // dedupliziert. task_id + round + call-index macht den Key pro
                // Iteration und pro Call eindeutig.
                let task_id_ref: &str = &task_id;
                let task_modul_ref: &str = &task_modul_id;
                let results =
                    crate::turn::execute_parsed_calls(&parsed_calls, &activity, |idx, call| {
                        let tool_task_id = format!("{}#r{}c{}", task_id_ref, tool_round, idx);
                        async move {
                            exec_tool(
                                &call.name,
                                &call.params,
                                task_modul_ref,
                                Some(&tool_task_id),
                                pipeline,
                                config,
                                llm,
                                py_modules,
                                py_pool,
                                Some(&call.arguments_json),
                            )
                            .await
                        }
                    })
                    .await;

                for ((call, sub_id), tool_result) in
                    parsed_calls.iter().zip(sub_ids.iter()).zip(results.iter())
                {
                    let status = if tool_result.0 { "SUCCESS" } else { "FAILED" };
                    if let Ok(Some(mut sub)) = pipeline.laden_by_id(sub_id) {
                        sub.ergebnis = Some(tool_result.1.clone());
                        let _ = pipeline.verschieben(
                            &mut sub,
                            if tool_result.0 {
                                AufgabeStatus::Success
                            } else {
                                AufgabeStatus::Failed
                            },
                        );
                    }
                    pipeline.log(
                        &modul.name,
                        Some(&aufgabe.id),
                        if tool_result.0 {
                            LogTyp::Success
                        } else {
                            LogTyp::Failed
                        },
                        &format!(
                            "Tool {}: {} → {}",
                            call.name,
                            status,
                            util::safe_truncate(&tool_result.1, 100)
                        ),
                    );
                }
                crate::turn::append_tool_results(
                    &mut messages,
                    &parsed_calls,
                    &results,
                    |ok, data| {
                        tools::format_tool_result_persisted(
                            ok,
                            data,
                            MAX_TASK_TOOL_RESULT_CHARS,
                            pipeline,
                            &task_modul_id,
                        )
                    },
                );
                mark_activity(&activity);

                // History trimmen: alte Tool-Results kuerzen
                crate::turn::trim_old_tool_messages(
                    &mut messages,
                    2,
                    6,
                    MAX_TASK_OLD_TOOL_RESULT_CHARS,
                );
                continue;
            }
            crate::turn::RoundOutcome::Final { text } => {
                mark_activity(&activity);
                final_answer = text;
                break;
            }
            crate::turn::RoundOutcome::GuardrailHardFail { codes } => {
                let msg = format!("Guardrail hard-fail: {}", codes.join(", "));
                pipeline.log(&modul.name, Some(&aufgabe.id), LogTyp::Failed, &msg);
                aufgabe.ergebnis = Some(format!("FAILED: {}", msg));
                if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                    pipeline.log(
                        "cycle",
                        Some(&aufgabe.id),
                        LogTyp::Error,
                        &format!("Verschieben failed: {e}"),
                    );
                }
                return;
            }
            crate::turn::RoundOutcome::LlmError(e) => {
                // Reservation wurde bereits in der Engine freigegeben.
                aufgabe.retry_count += 1;
                if aufgabe.retry_count <= aufgabe.retry {
                    pipeline.log(
                        &modul.name,
                        Some(&aufgabe.id),
                        LogTyp::Warning,
                        &format!("RETRY {}/{}: {}", aufgabe.retry_count, aufgabe.retry, e),
                    );
                    if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Erstellt) {
                        pipeline.log(
                            "cycle",
                            Some(&aufgabe.id),
                            LogTyp::Error,
                            &format!("Verschieben failed: {e}"),
                        );
                    }
                } else {
                    aufgabe.ergebnis = Some(format!("FAILED: {e}"));
                    pipeline.log(
                        &modul.name,
                        Some(&aufgabe.id),
                        LogTyp::Failed,
                        &format!("FAILED: {e}"),
                    );
                    if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                        pipeline.log(
                            "cycle",
                            Some(&aufgabe.id),
                            LogTyp::Error,
                            &format!("Verschieben failed: {e}"),
                        );
                    }
                }
                return;
            }
        }
    }

    aufgabe.ergebnis = Some(final_answer.clone());
    pipeline.log(
        "cycle",
        Some(&aufgabe.id),
        LogTyp::Success,
        &format!("SUCCESS: {}", util::safe_truncate(&final_answer, 100)),
    );
    if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Success) {
        pipeline.log(
            "cycle",
            Some(&aufgabe.id),
            LogTyp::Error,
            &format!("Verschieben failed: {e}"),
        );
    }
    // Route result back if zurueck_an is set
    if aufgabe.status == AufgabeStatus::Success || aufgabe.status == AufgabeStatus::Failed {
        let cfg = config.read().await;
        route_ergebnis(aufgabe, pipeline, &cfg);
    }
}

fn route_ergebnis(aufgabe: &Aufgabe, pipeline: &Pipeline, config: &AgentConfig) {
    let Some(ref zurueck) = aufgabe.zurueck_an else {
        return;
    };

    // Routing ist IMMER ChatReply — Target sieht Text als Nachricht, keine
    // Auto-LLM-Execution mehr. Kein `llm:`-Opt-In mehr (GLM-Finding Run
    // SQLite-7: das opt-in war Prompt-Injection-Escalation-Vector, jeder
    // verlinkte Source konnte Instruktionen ins Target durchreichen).
    //
    // Wer auto-LLM-Verarbeitung von Resultaten will, muss das explizit im
    // System-Prompt des Targets coden: "wenn eine Nachricht vom Format
    // '[Ergebnis von X]: ...' kommt, rufe Tool Y". Das macht die Attack-
    // Surface explizit im User-Prompt sichtbar statt implizit via Routing.
    //
    // Prefix-Syntax: "chat:target" → chat routing (für UI), ohne prefix →
    // module-zu-module ChatReply mit linking check.
    let (is_chat, target, convo_id) =
        if let Some((target, convo_id)) = util::parse_chat_route(zurueck) {
            (true, target, convo_id)
        } else {
            (false, zurueck.to_string(), None)
        };

    // Linking-Check nur für non-chat: Target muss verlinkt oder Selbst sein
    if !is_chat && aufgabe.modul != target {
        let source_modul = config.module.iter().find(|m| m.id == aufgabe.modul);
        if let Some(source) = source_modul {
            if !source.linked_modules.contains(&target) {
                pipeline.log(
                    "routing",
                    Some(&aufgabe.id),
                    LogTyp::Warning,
                    &format!(
                        "Routing blocked: {} not linked to {}",
                        aufgabe.modul, target
                    ),
                );
                return;
            }
        }
    }

    let ergebnis = aufgabe.ergebnis.as_deref().unwrap_or("Kein Ergebnis");
    let payload = format!("[Ergebnis von {}]: {}", aufgabe.modul, ergebnis);
    if is_chat {
        if let Some(cid) = convo_id.as_deref() {
            match append_message_to_convo(pipeline, &target, cid, "assistant", &payload) {
                Ok(_) => {
                    enqueue_telegram_route_if_needed(aufgabe, pipeline, config, cid, ergebnis);
                    let source = format!("task:{}", aufgabe.id);
                    let _ = pipeline.notification_add(
                        &target,
                        Some(cid),
                        "system",
                        Some("Aufgabe fertig"),
                        &format!(
                            "Task {} hat ein Ergebnis in den Chat geschrieben.",
                            util::safe_truncate(&aufgabe.id, 8)
                        ),
                        Some(&source),
                    );
                    pipeline.log(
                        "routing",
                        Some(&aufgabe.id),
                        LogTyp::Info,
                        &format!("Ergebnis in Conversation {}:{} geschrieben", target, cid),
                    );
                    return;
                }
                Err(e) => {
                    pipeline.log(
                        "routing",
                        Some(&aufgabe.id),
                        LogTyp::Warning,
                        &format!(
                            "Conversation-Routing nach {}:{} fehlgeschlagen: {}",
                            target, cid, e
                        ),
                    );
                }
            }
        }
    }

    let mut result_task = Aufgabe::direct(
        "__chat_reply__",
        vec![payload],
        &target,
        &aufgabe.modul,
        None,
        None,
    );
    result_task.typ = crate::types::AufgabeTyp::ChatReply;
    result_task.anweisung = format!("[Ergebnis von {}]", aufgabe.modul);
    result_task.tool = None;
    let _ = pipeline.speichern(&result_task);
    pipeline.log(
        "routing",
        Some(&aufgabe.id),
        LogTyp::Info,
        &format!(
            "Ergebnis geroutet an {}{} (ChatReply)",
            if is_chat { "chat:" } else { "" },
            target
        ),
    );
}

fn enqueue_telegram_route_if_needed(
    aufgabe: &Aufgabe,
    pipeline: &Pipeline,
    config: &AgentConfig,
    convo_id: &str,
    text: &str,
) {
    let Some(chat_id) = convo_id.strip_prefix("telegram_") else {
        return;
    };
    if chat_id.trim().is_empty() || text.trim().is_empty() {
        return;
    }
    let Some(telegram_module) = config
        .module
        .iter()
        .find(|m| m.typ == "telegram_bot" || m.id.starts_with("telegram_bot."))
    else {
        pipeline.log(
            "routing",
            Some(&aufgabe.id),
            LogTyp::Warning,
            "Telegram Conversation erkannt, aber kein telegram_bot Modul gefunden",
        );
        return;
    };
    let send = Aufgabe::direct(
        "telegram_bot.send",
        vec![chat_id.to_string(), text.to_string()],
        &telegram_module.id,
        &aufgabe.modul,
        None,
        None,
    )
    .with_timeout_s(telegram_module.timeout_s);
    match pipeline.speichern(&send) {
        Ok(_) => pipeline.log(
            "routing",
            Some(&aufgabe.id),
            LogTyp::Info,
            &format!(
                "Telegram Send-Task {} fuer Conversation {} erstellt",
                send.id, convo_id
            ),
        ),
        Err(e) => pipeline.log(
            "routing",
            Some(&aufgabe.id),
            LogTyp::Warning,
            &format!("Telegram Send-Task konnte nicht erstellt werden: {}", e),
        ),
    }
}

fn append_message_to_convo(
    pipeline: &Pipeline,
    modul_id: &str,
    convo_id: &str,
    role: &str,
    content: &str,
) -> std::io::Result<()> {
    let mut convo = pipeline.convo_load(modul_id, convo_id).unwrap_or_else(|| {
        serde_json::json!({
            "id": convo_id,
            "title": "Task Ergebnis",
            "messages": [],
            "updated": chrono::Utc::now().to_rfc3339(),
        })
    });

    if !convo.get("messages").is_some_and(|v| v.is_array()) {
        convo["messages"] = serde_json::json!([]);
    }
    if let Some(messages) = convo["messages"].as_array_mut() {
        messages.push(serde_json::json!({
            "role": role,
            "content": content,
        }));
    }
    if convo
        .get("title")
        .and_then(|v| v.as_str())
        .map(|s| s.trim().is_empty())
        .unwrap_or(true)
    {
        convo["title"] = serde_json::json!("Task Ergebnis");
    }
    convo["updated"] = serde_json::json!(chrono::Utc::now().to_rfc3339());
    pipeline.convo_save(modul_id, &convo)
}

#[allow(clippy::too_many_arguments)]
async fn exec_tool(
    tool_name: &str,
    params: &[String],
    modul_id: &str,
    task_id: Option<&str>,
    pipeline: &Arc<Pipeline>,
    config: &Arc<RwLock<AgentConfig>>,
    llm: &Arc<LlmRouter>,
    py_modules: &Arc<RwLock<Vec<crate::loader::PyModuleMeta>>>,
    py_pool: &Arc<crate::loader::PyProcessPool>,
    args_json: Option<&str>,
) -> (bool, String) {
    let config_snapshot = config.read().await.clone();
    let py_mods = py_modules.read().await;
    tools::exec_tool_unified(
        tool_name,
        params,
        modul_id,
        task_id,
        pipeline,
        llm,
        &py_mods,
        py_pool,
        &config_snapshot,
        args_json,
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cron_field_wildcard() {
        assert!(cron_field_matches("*", 0));
        assert!(cron_field_matches("*", 59));
    }

    #[test]
    fn test_cron_field_step() {
        assert!(cron_field_matches("*/5", 0));
        assert!(cron_field_matches("*/5", 5));
        assert!(cron_field_matches("*/5", 10));
        assert!(!cron_field_matches("*/5", 3));
    }

    #[test]
    fn test_cron_field_exact() {
        assert!(cron_field_matches("30", 30));
        assert!(!cron_field_matches("30", 31));
    }

    #[test]
    fn test_cron_field_range() {
        assert!(cron_field_matches("1-5", 1));
        assert!(cron_field_matches("1-5", 3));
        assert!(cron_field_matches("1-5", 5));
        assert!(!cron_field_matches("1-5", 0));
        assert!(!cron_field_matches("1-5", 6));
    }

    #[test]
    fn test_cron_field_list() {
        assert!(cron_field_matches("1,3,5", 1));
        assert!(cron_field_matches("1,3,5", 3));
        assert!(!cron_field_matches("1,3,5", 2));
    }

    #[test]
    fn test_cron_dow_accepts_both_sunday_conventions() {
        // ISO-Sonntag (%u = 7) muss sowohl "0" (Standard-Cron) als auch "7" (ISO) matchen
        assert!(cron_dow_matches("0", 7));
        assert!(cron_dow_matches("7", 7));
        assert!(cron_dow_matches("*", 7));
        // Montag (%u = 1) matcht "1", aber weder "0" noch "7"
        assert!(cron_dow_matches("1", 1));
        assert!(!cron_dow_matches("0", 1));
        assert!(!cron_dow_matches("7", 1));
        // Listen und Ranges funktionieren in beiden Konventionen
        assert!(cron_dow_matches("0,3", 7));
        assert!(cron_dow_matches("5-7", 6));
        assert!(!cron_dow_matches("2-4", 7));
    }

    #[test]
    fn test_evaluate_condition_success() {
        assert!(evaluate_condition("success", "", true));
        assert!(!evaluate_condition("success", "", false));
    }

    #[test]
    fn test_evaluate_condition_failed() {
        assert!(evaluate_condition("failed", "", false));
        assert!(!evaluate_condition("failed", "", true));
    }

    #[test]
    fn test_evaluate_condition_contains() {
        assert!(evaluate_condition(
            "contains:ERROR",
            "Task ERROR occurred",
            true
        ));
        assert!(!evaluate_condition("contains:ERROR", "All good", true));
    }

    #[test]
    fn test_evaluate_condition_not_contains() {
        assert!(evaluate_condition(
            "not_contains:FAIL",
            "SUCCESS done",
            true
        ));
        assert!(!evaluate_condition(
            "not_contains:FAIL",
            "FAIL happened",
            true
        ));
    }

    #[test]
    fn test_evaluate_condition_starts_with() {
        assert!(evaluate_condition("starts_with:OK", "OK all good", true));
        assert!(!evaluate_condition("starts_with:OK", "Not OK", true));
    }

    #[test]
    fn test_evaluate_condition_unknown_defaults_true() {
        assert!(evaluate_condition("unknown_condition", "", true));
    }

    #[test]
    fn task_tool_result_for_llm_truncates_large_outputs() {
        let large = "x".repeat(MAX_TASK_TOOL_RESULT_CHARS + 1000);
        let result = task_tool_result_for_llm(true, &large);
        assert!(result.starts_with("SUCCESS: "));
        assert!(result.contains("gekuerzt"));
        assert!(result.len() < large.len());
    }

    #[test]
    fn task_tool_result_for_llm_keeps_failure_actionable() {
        let result = task_tool_result_for_llm(false, "Datei existiert nicht");
        assert!(result.starts_with("FAILED: "));
        assert!(result.contains("NEXT:"));
    }

    #[test]
    fn idle_timeout_uses_last_activity_not_task_start() {
        assert!(idle_timed_out(100, 160, 60));
        assert!(!idle_timed_out(140, 160, 60));
    }

    #[test]
    fn idle_timeout_has_minimum_floor() {
        assert!(!idle_timed_out(100, 120, 1));
        assert!(idle_timed_out(100, 130, 1));
    }

    #[tokio::test]
    async fn activity_heartbeat_marks_long_running_future() {
        let activity = Some(Arc::new(AtomicI64::new(100)));
        let marker = activity.as_ref().unwrap().clone();
        let result = crate::turn::with_activity_heartbeat(&activity, async {
            tokio::time::sleep(std::time::Duration::from_secs(6)).await;
            42
        })
        .await;
        assert_eq!(result, 42);
        assert!(marker.load(Ordering::Relaxed) > 100);
    }

    // ═══ E2E: Mock-OpenAI-Server gegen den exec_llm Tool-Loop ═══════════════
    // Treibt den ECHTEN Loop (exec_llm → LlmRouter → HTTP → Parser → exec_tool)
    // gegen einen in-process Mock-Server. Das ist der Integrationstest, der
    // vorher fehlte — die Single-Call-/History-Bugs hätten hier sofort geknallt.

    type MockRecorder = Arc<tokio::sync::Mutex<Vec<serde_json::Value>>>;

    async fn spawn_mock_openai(responses: Vec<serde_json::Value>) -> (String, MockRecorder) {
        use axum::{Json, Router, routing::post};
        let received: MockRecorder = Arc::new(tokio::sync::Mutex::new(Vec::new()));
        let queue = Arc::new(tokio::sync::Mutex::new(std::collections::VecDeque::from(
            responses,
        )));
        let rec = received.clone();
        let app = Router::new().route(
            "/v1/chat/completions",
            post(move |Json(body): Json<serde_json::Value>| {
                let rec = rec.clone();
                let queue = queue.clone();
                async move {
                    rec.lock().await.push(body);
                    let next = queue.lock().await.pop_front().unwrap_or_else(|| {
                        serde_json::json!({
                            "choices": [{"message": {"role": "assistant", "content": "FERTIG"}}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1}
                        })
                    });
                    Json(next)
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (format!("http://{}", addr), received)
    }

    fn e2e_backend(url: &str) -> crate::types::LlmBackend {
        crate::types::LlmBackend {
            id: "mock".into(),
            name: "mock".into(),
            typ: crate::types::LlmTyp::OpenAICompat,
            url: url.to_string(),
            api_key: Some("test".into()),
            model: "mock-1".into(),
            timeout_s: 5,
            identity: Default::default(),
            max_tokens: None,
            reasoning: None,
            cost_cap: None,
            max_tool_rounds: None,
            call_rate_limit: None,
            internal: false,
            tool_choice_supported: None,
            context_window: None,
        }
    }

    fn e2e_modul(id: &str) -> ModulConfig {
        ModulConfig {
            id: id.into(),
            typ: "filesystem".into(),
            name: id.into(),
            display_name: id.into(),
            llm_backend: "mock".into(),
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
        }
    }

    struct E2eEnv {
        _dir: tempfile::TempDir,
        pipeline: Arc<Pipeline>,
        config: Arc<RwLock<AgentConfig>>,
        llm: Arc<LlmRouter>,
        py_modules: Arc<RwLock<Vec<crate::loader::PyModuleMeta>>>,
        py_pool: Arc<crate::loader::PyProcessPool>,
        tokens: crate::web::TokenTracker,
    }

    fn e2e_env(mock_url: &str, modul_id: &str) -> E2eEnv {
        let dir = tempfile::tempdir().unwrap();
        let pipeline = Arc::new(Pipeline::new(dir.path()).unwrap());
        let mut cfg = AgentConfig::default();
        cfg.llm_backends.push(e2e_backend(mock_url));
        cfg.module.push(e2e_modul(modul_id));
        // Guardrail bewusst aus — hier wird der nackte Loop getestet.
        cfg.guardrail = Some(crate::types::GuardrailConfig {
            enabled: false,
            ..Default::default()
        });
        let config = Arc::new(RwLock::new(cfg));
        let llm = Arc::new(LlmRouter::new(config.clone()));
        E2eEnv {
            _dir: dir,
            pipeline,
            config,
            llm,
            py_modules: Arc::new(RwLock::new(vec![])),
            py_pool: crate::loader::PyProcessPool::new(60),
            tokens: Arc::new(RwLock::new(Default::default())),
        }
    }

    fn tool_call_json(id: &str, name: &str, args: serde_json::Value) -> serde_json::Value {
        serde_json::json!({
            "id": id,
            "type": "function",
            "function": {"name": name, "arguments": args.to_string()}
        })
    }

    async fn run_exec_llm(env: &E2eEnv, modul_id: &str, anweisung: &str) -> Aufgabe {
        let mut aufgabe = Aufgabe::neu(modul_id, anweisung, "sofort", "e2e-test");
        aufgabe.status = AufgabeStatus::Gestartet;
        aufgabe.gestartet = Some(chrono::Utc::now());
        env.pipeline.speichern(&aufgabe).unwrap();
        exec_llm(
            &mut aufgabe,
            &env.pipeline,
            &env.config,
            &env.llm,
            &env.py_modules,
            &env.py_pool,
            &env.tokens,
            None,
        )
        .await;
        aufgabe
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn e2e_multi_tool_calls_all_executed_and_answered() {
        // Runde 1: Modell sendet ZWEI parallele files.write-Calls.
        // Runde 2: finale Antwort. Beide Dateien muessen existieren, und der
        // zweite Request muss pro Call genau eine role:"tool"-Antwort tragen.
        // Env zuerst (wegen der absoluten Home-Pfade in den Mock-Responses),
        // Mock-URL danach in die Config geschrieben.
        let env = e2e_env("http://127.0.0.1:1", "fs.e2e");
        let home = env.pipeline.home_dir("fs.e2e");
        let path_a = home.join("a.txt").to_string_lossy().to_string();
        let path_b = home.join("b.txt").to_string_lossy().to_string();

        let round1 = serde_json::json!({
            "choices": [{"message": {"role": "assistant", "content": "",
                "tool_calls": [
                    tool_call_json("call_a", "files.write",
                        serde_json::json!({"path": path_a, "content": "INHALT-A"})),
                    tool_call_json("call_b", "files.write",
                        serde_json::json!({"path": path_b, "content": "INHALT-B"})),
                ]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}
        });
        let round2 = serde_json::json!({
            "choices": [{"message": {"role": "assistant", "content": "Beide Dateien geschrieben."}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5}
        });
        let (url, received3) = spawn_mock_openai(vec![round1, round2]).await;
        env.config.write().await.llm_backends[0].url = url;

        let aufgabe = run_exec_llm(&env, "fs.e2e", "Schreibe zwei Dateien").await;

        assert_eq!(aufgabe.status, AufgabeStatus::Success);
        assert_eq!(
            aufgabe.ergebnis.as_deref(),
            Some("Beide Dateien geschrieben.")
        );
        // Beide Tool-Calls wurden wirklich ausgefuehrt:
        assert_eq!(
            std::fs::read_to_string(home.join("a.txt")).unwrap(),
            "INHALT-A"
        );
        assert_eq!(
            std::fs::read_to_string(home.join("b.txt")).unwrap(),
            "INHALT-B"
        );

        let reqs = received3.lock().await;
        assert_eq!(reqs.len(), 2, "genau zwei LLM-Runden erwartet");
        let msgs = reqs[1]["messages"].as_array().unwrap();
        let assistant_with_calls = msgs
            .iter()
            .find(|m| m["tool_calls"].is_array())
            .expect("assistant-Message mit tool_calls in Runde 2");
        assert_eq!(
            assistant_with_calls["tool_calls"].as_array().unwrap().len(),
            2,
            "History muss BEIDE Calls enthalten"
        );
        let tool_msgs: Vec<_> = msgs.iter().filter(|m| m["role"] == "tool").collect();
        assert_eq!(tool_msgs.len(), 2, "eine role:tool-Antwort pro Call");
        assert_eq!(tool_msgs[0]["tool_call_id"], "call_a");
        assert_eq!(tool_msgs[1]["tool_call_id"], "call_b");
        assert!(
            tool_msgs[0]["content"]
                .as_str()
                .unwrap()
                .starts_with("SUCCESS:")
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn e2e_final_answer_without_tools() {
        let round = serde_json::json!({
            "choices": [{"message": {"role": "assistant", "content": "Direkte Antwort ohne Tools."}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5}
        });
        let (url, received) = spawn_mock_openai(vec![round]).await;
        let env = e2e_env(&url, "fs.e2e2");
        let aufgabe = run_exec_llm(&env, "fs.e2e2", "Sag was").await;
        assert_eq!(aufgabe.status, AufgabeStatus::Success);
        assert_eq!(
            aufgabe.ergebnis.as_deref(),
            Some("Direkte Antwort ohne Tools.")
        );
        assert_eq!(received.lock().await.len(), 1);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn e2e_script_exec_runs_tool_chain_in_one_round() {
        // PTC: EIN script.exec-Call schreibt zwei Dateien via call() und
        // aggregiert — Zwischenergebnisse erreichen das LLM nie, nur stdout.
        let env = e2e_env("http://127.0.0.1:1", "fs.ptc");
        env.config.write().await.module[0].berechtigungen = vec!["script.exec".into()];
        let home = env.pipeline.home_dir("fs.ptc");
        let code = format!(
            "a = call(\"files.write\", \"{h}/p1.txt\", \"AAA\")\n\
             b = call(\"files.write\", \"{h}/p2.txt\", \"BBB\")\n\
             listing = call(\"files.list\", \"{h}\")\n\
             print(\"GESCHRIEBEN:\", \"p1.txt\" in listing and \"p2.txt\" in listing)",
            h = home.to_string_lossy()
        );
        let round1 = serde_json::json!({
            "choices": [{"message": {"role": "assistant", "content": "",
                "tool_calls": [tool_call_json("call_s", "script.exec",
                    serde_json::json!({"python_code": code}))]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}
        });
        let round2 = serde_json::json!({
            "choices": [{"message": {"role": "assistant", "content": "Pipeline fertig."}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4}
        });
        let (url, received) = spawn_mock_openai(vec![round1, round2]).await;
        env.config.write().await.llm_backends[0].url = url;

        let aufgabe = run_exec_llm(&env, "fs.ptc", "Schreibe zwei Dateien per Skript").await;
        assert_eq!(aufgabe.status, AufgabeStatus::Success);
        assert_eq!(std::fs::read_to_string(home.join("p1.txt")).unwrap(), "AAA");
        assert_eq!(std::fs::read_to_string(home.join("p2.txt")).unwrap(), "BBB");
        let reqs = received.lock().await;
        assert_eq!(
            reqs.len(),
            2,
            "drei Tool-Aktionen, aber nur EINE Tool-Runde"
        );
        let msgs = reqs[1]["messages"].as_array().unwrap();
        let tool_msg = msgs.iter().find(|m| m["role"] == "tool").unwrap();
        let content = tool_msg["content"].as_str().unwrap();
        assert!(
            content.contains("GESCHRIEBEN: True"),
            "stdout fehlt: {}",
            content
        );
        assert!(
            content.contains("3 tool-calls"),
            "tool-call-zaehler fehlt: {}",
            content
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn e2e_tool_round_limit_fails_task() {
        // Modell will in jeder Runde ein Tool — Limit 1 muss den Task nach der
        // ersten Runde mit klarer Fehlermeldung beenden.
        let make_round = |id: &str| {
            serde_json::json!({
                "choices": [{"message": {"role": "assistant", "content": "",
                    "tool_calls": [tool_call_json(id, "files.list", serde_json::json!({"path": "."}))]}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5}
            })
        };
        let (url, _received) = spawn_mock_openai(vec![make_round("c1"), make_round("c2")]).await;
        let env = e2e_env(&url, "fs.e2e3");
        env.config.write().await.llm_backends[0].max_tool_rounds = Some(1);
        let aufgabe = run_exec_llm(&env, "fs.e2e3", "Liste Dateien endlos").await;
        assert_eq!(aufgabe.status, AufgabeStatus::Failed);
        assert!(
            aufgabe
                .ergebnis
                .as_deref()
                .unwrap_or("")
                .contains("tool rounds limit")
        );
    }
}
