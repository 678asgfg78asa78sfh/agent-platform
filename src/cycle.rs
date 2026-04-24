use std::sync::Arc;
use std::collections::HashMap;
use tokio::sync::RwLock;
use crate::pipeline::Pipeline;
use crate::types::*;
use crate::llm::LlmRouter;
use crate::tools;
use crate::util;

const MAX_TOOL_ROUNDS: usize = 30;

/// Simple cron check: does the current time match a cron expression?
/// Supports: */N, specific numbers, ranges (1-5), and * (any)
/// Format: "minute hour day_of_month month day_of_week"
fn cron_matches_now(expression: &str) -> bool {
    let now = chrono::Local::now();
    let parts: Vec<&str> = expression.split_whitespace().collect();
    if parts.len() != 5 { return false; }

    let checks = [
        (parts[0], now.format("%M").to_string().parse::<u32>().unwrap_or(0), 59),
        (parts[1], now.format("%H").to_string().parse::<u32>().unwrap_or(0), 23),
        (parts[2], now.format("%d").to_string().parse::<u32>().unwrap_or(0), 31),
        (parts[3], now.format("%m").to_string().parse::<u32>().unwrap_or(0), 12),
        (parts[4], now.format("%u").to_string().parse::<u32>().unwrap_or(0), 7), // 1=Mon, 7=Sun
    ];

    checks.iter().all(|(pattern, current, _max)| cron_field_matches(pattern, *current))
}

fn cron_field_matches(pattern: &str, value: u32) -> bool {
    if pattern == "*" { return true; }
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
        return pattern.split(',')
            .filter_map(|p| p.trim().parse::<u32>().ok())
            .any(|v| v == value);
    }
    // Exact number
    if let Ok(exact) = pattern.parse::<u32>() {
        return value == exact;
    }
    false
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
        Self { busy: Some(busy), handles: Some(handles), modul_id, aufgabe_id }
    }
}

impl Drop for BusyGuard {
    fn drop(&mut self) {
        let (Some(busy), Some(handles)) = (self.busy.take(), self.handles.take())
            else { return };
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
                        if ids.is_empty() { b.remove(&modul_id); }
                    }
                }
                {
                    let mut h = handles.write().await;
                    if let Some(map) = h.get_mut(&modul_id) {
                        map.remove(&aufgabe_id);
                        if map.is_empty() { h.remove(&modul_id); }
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
        self.pipeline.log("orchestrator", None, LogTyp::Info, "Orchestrator gestartet");

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
            let modul_ids: Vec<String> = cfg.module.iter().map(|m| m.id.clone()).collect();
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
                    Some(handle) => {
                        handle.is_finished() || !hb_snapshot.contains(modul_id)
                    }
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
                    let interval_ms = cfg.module.iter()
                        .find(|m| m.id == *modul_id)
                        .and_then(|m| m.scheduler_interval_ms)
                        .unwrap_or(cfg.cycle_interval_ms);
                    let max_concurrent = cfg.module.iter()
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

                    self.pipeline.log("orchestrator", None, LogTyp::Info,
                        &format!("Scheduler '{}' wird gestartet (interval: {}ms)", modul_id, interval_ms));

                    let handle = tokio::spawn(async move {
                        scheduler.run().await;
                    });
                    handles.insert(modul_id.clone(), handle);
                }
            }

            // 3. Handles fuer entfernte Module abbrechen
            let stale: Vec<String> = handles.keys()
                .filter(|id| !modul_ids.contains(id))
                .cloned()
                .collect();
            for id in stale {
                if let Some(handle) = handles.remove(&id) {
                    handle.abort();
                    self.pipeline.log("orchestrator", None, LogTyp::Warning,
                        &format!("Scheduler '{}' gestoppt (Modul entfernt)", id));
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
                cfg.cleanup.as_ref().map(|c| c.cleanup_interval_s).unwrap_or(60)
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
                let _ = crate::store::idempotency_expire_in_progress(&self.pipeline.store.pool, 600);
                // Alte completed Idempotency-Einträge wegrotieren (30 Tage)
                let _ = crate::store::idempotency_cleanup(&self.pipeline.store.pool, 30);
            }

            tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;
        }
    }

    async fn tick_cron(&self) {
        let cfg = self.config.read().await;
        for modul in cfg.module.iter().filter(|m| m.typ == "cron") {
            let Some(ref schedule) = modul.settings.schedule else { continue; };
            if !cron_matches_now(schedule) { continue; }

            // Dedup guard: store::cron_try_claim atomar in SQL-Transaktion. Schließt
            // die Race zwischen "dedup-check" und "task-spawn" die das alte JSON-
            // File-basierte System hatte (Crash zwischen den beiden → Cron feuerte
            // nach Restart doppelt).
            let now_key = chrono::Local::now().format("%Y-%m-%d %H:%M").to_string();
            match crate::store::cron_try_claim(&self.pipeline.store.pool, &modul.id, &now_key) {
                Ok(true) => { /* claimed — weiter */ }
                Ok(false) => continue,  // schon gefeuert diese Minute
                Err(e) => {
                    self.pipeline.log("cron", None, LogTyp::Error,
                        &format!("cron_try_claim fehlgeschlagen: {}", e));
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
                                params: modul.settings.on_success_params.clone().unwrap_or_default(),
                                condition: Some("success".to_string()),
                                stop_on_fail: false,
                            });
                        }
                        if let Some(ref on_failure) = modul.settings.on_failure {
                            chain_steps.push(crate::types::ChainStep {
                                tool: on_failure.clone(),
                                params: modul.settings.on_failure_params.clone().unwrap_or_default(),
                                condition: Some("failed".to_string()),
                                stop_on_fail: false,
                            });
                        }

                        if chain_steps.len() > 1 {
                            // Has callbacks — use chain execution
                            let chain_json = serde_json::to_string(&chain_steps).unwrap_or_default();
                            let mut aufgabe = Aufgabe::direct(
                                "__chain__", vec![chain_json], target, &modul.id, None, None,
                            );
                            aufgabe.anweisung = format!("Cron: {} + callbacks", tool);
                            if self.pipeline.speichern(&aufgabe).is_ok() {
                                self.pipeline.log("cron", Some(&aufgabe.id), LogTyp::Info,
                                    &format!("Cron '{}' triggered: {} (with callbacks)", modul.id, tool));
                            }
                        } else {
                            // Simple direct task, no callbacks
                            let aufgabe = Aufgabe::direct(
                                tool, params, target, &modul.id, None, None,
                            );
                            if self.pipeline.speichern(&aufgabe).is_ok() {
                                self.pipeline.log("cron", Some(&aufgabe.id), LogTyp::Info,
                                    &format!("Cron '{}' triggered: {}()", modul.id, tool));
                            }
                        }
                    }
                }
                "llm" => {
                    // LLM task
                    let target = modul.settings.target_modul.as_deref().unwrap_or(&modul.id);
                    let anweisung = modul.settings.cron_anweisung.as_deref().unwrap_or("Cron task");
                    let aufgabe = Aufgabe::llm_call(anweisung, target, &modul.id, None);
                    if self.pipeline.speichern(&aufgabe).is_ok() {
                        self.pipeline.log("cron", Some(&aufgabe.id), LogTyp::Info,
                            &format!("Cron '{}' triggered: LLM task for {}", modul.id, target));
                    }
                }
                "chain" => {
                    if let Some(ref chain) = modul.settings.chain {
                        if chain.is_empty() { continue; }
                        let target = modul.settings.target_modul.as_deref().unwrap_or(&modul.id);

                        // Create a task that will execute the full chain
                        // We store the chain spec in the params field as JSON
                        let chain_json = serde_json::to_string(chain).unwrap_or_default();
                        let mut aufgabe = Aufgabe::direct(
                            "__chain__", vec![chain_json], target, &modul.id,
                            None, None,
                        );
                        aufgabe.anweisung = format!("Chain: {} steps", chain.len());
                        if self.pipeline.speichern(&aufgabe).is_ok() {
                            self.pipeline.log("cron", Some(&aufgabe.id), LogTyp::Info,
                                &format!("Cron chain '{}' triggered: {} steps", modul.id, chain.len()));
                        }
                    }
                }
                _ => {
                    self.pipeline.log("cron", None, LogTyp::Warning,
                        &format!("Unknown cron_typ '{}' for {}", cron_typ, modul.id));
                }
            }
        }
    }

    async fn run_cleanup(&self, cleanup_cfg: &Option<CleanupConfig>) {
        // Erledigt-Cleanup
        if let Some(cc) = cleanup_cfg {
            self.pipeline.cleanup_erledigt(cc.max_erledigt, cc.max_alter_tage);
        }

        // Temp-Agent-Cleanup: TTL gegen `created_at` prüfen, NICHT gegen Heartbeat.
        // Der Heartbeat wird alle ~2s aktualisiert (Scheduler-Loop), also war
        // `now - heartbeat` immer klein und der TTL hat nie getriggert. Jetzt
        // prüfen wir "wann wurde das Modul geboren?" gegen TTL.
        let cfg = self.config.read().await;
        let now = chrono::Utc::now().timestamp() as u64;
        let expired: Vec<String> = cfg.module.iter()
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
                if m.persistent { return true; }
                if m.spawned_by.is_none() { return true; }

                if expired.contains(&m.id) {
                    self.pipeline.log("orchestrator", None, LogTyp::Info,
                        &format!("Temp-Agent '{}' TTL abgelaufen — wird entfernt", m.id));
                    return false;
                }

                let has_active = erstellt.iter().any(|a| a.modul == m.id)
                    || gestartet.iter().any(|a| a.modul == m.id);
                if has_active { return true; }
                if busy_snapshot.contains_key(&m.id) { return true; }

                self.pipeline.log("orchestrator", None, LogTyp::Info,
                    &format!("Temp-Agent '{}' aufgeraeumt (idle, spawned by {})",
                        m.id, m.spawned_by.as_deref().unwrap_or("?")));
                false
            });

            let changed = cfg.module.len() < before_count;
            let json = if changed { serde_json::to_string_pretty(&*cfg).ok() } else { None };
            (json, changed)
            // RwLock-Write gedroppt beim scope-exit — Reader können wieder durch
        };
        if changed {
            if let Some(json) = serialized {
                let path = self.pipeline.base.join("config.json");
                let _ = util::atomic_write(&path, json.as_bytes());
            }
        }
        drop(write_guard);  // Mutex explizit droppen für Klarheit

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
                            self.pipeline.log("orchestrator", Some(&a.id), LogTyp::Warning,
                                &format!("Orphan-Task (Modul '{}' weg) auf FAILED gesetzt", row.modul));
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
        if !temp_dir.exists() { return; }

        let entries: Vec<_> = match std::fs::read_dir(&temp_dir) {
            Ok(e) => e.flatten().collect(),
            Err(_) => return,
        };
        if entries.is_empty() { return; }

        let config_path = self.pipeline.base.join("config.json");

        // Lock-Order: Mutex zuerst, dann RwLock — selbe Reihenfolge wie im
        // run_cleanup-Pfad und wie in save_config (Web-API). Kein Deadlock
        // solange alle Schreiber dieser Ordnung folgen. Keine stale-snapshot-
        // Races mehr (GLM-Finding Run SQLite-9).
        for entry in entries {
            if !entry.path().extension().is_some_and(|e| e == "json") { continue; }

            let content = match std::fs::read_to_string(entry.path()) {
                Ok(c) => c,
                Err(_) => continue,
            };
            let spec: serde_json::Value = match serde_json::from_str(&content) {
                Ok(v) => v,
                Err(_) => continue,
            };
            let Ok(modul) = serde_json::from_value::<crate::types::ModulConfig>(spec["module"].clone()) else { continue };

            let write_guard = self.pipeline.config_write_lock.lock().await;
            let mut cfg = self.config.write().await;

            if cfg.module.iter().any(|m| m.id == modul.id) {
                drop(cfg);
                drop(write_guard);
                let _ = std::fs::remove_file(entry.path());
                continue;
            }

            self.pipeline.log("orchestrator", None, LogTyp::Info,
                &format!("Temp-Agent '{}' aus spec geladen", modul.id));
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
                self.pipeline.log("orchestrator", None, LogTyp::Error,
                    &format!("load_temp_modules '{}': config persist failed, rollback", modul.id));
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
        self.pipeline.log(&self.modul_id, None, LogTyp::Info,
            &format!("ModulScheduler '{}' laeuft (interval: {}ms)", self.modul_id, self.interval_ms));

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
            if aufgabe.modul != self.modul_id { continue; } // Nur EIGENE Aufgaben
            if aufgabe.status == AufgabeStatus::Failed { continue; }
            let b = self.busy.read().await;
            let current = b.get(&self.modul_id).map(|v| v.len()).unwrap_or(0);
            if current >= self.max_concurrent as usize { drop(b); continue; } // Kapazitaet erreicht
            // Check if this specific task is already being processed
            if b.get(&self.modul_id).map(|v| v.contains(&aufgabe.id)).unwrap_or(false) { drop(b); continue; }
            drop(b);
            self.pipeline.log(&self.modul_id, Some(&aufgabe.id), LogTyp::Warning,
                "Recovery: Aufgabe wird fortgesetzt");
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
            if current >= self.max_concurrent as usize { drop(b); break; }
            drop(b);

            match self.pipeline.claim_for_modul(&self.modul_id) {
                Ok(Some(aufgabe)) => self.spawn_aufgabe(aufgabe).await,
                Ok(None) => break,  // keine fälligen Tasks mehr in dieser Tick
                Err(e) => {
                    self.pipeline.log(&self.modul_id, None, LogTyp::Error,
                        &format!("claim_for_modul failed: {}", e));
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
            if let Err(e) = self.pipeline.verschieben(&mut aufgabe, AufgabeStatus::Gestartet) {
                self.pipeline.log(&self.modul_id, Some(&aufgabe.id), LogTyp::Error,
                    &format!("Verschieben failed: {e}"));
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
            b.entry(aufgabe.modul.clone()).or_default().push(aufgabe.id.clone());
        }

        self.pipeline.log(&self.modul_id, Some(&aufgabe.id), LogTyp::Info,
            &format!("[{}] {} (async)", match aufgabe.typ {
                AufgabeTyp::Direct => "DIRECT",
                AufgabeTyp::LlmCall => "LLM",
                AufgabeTyp::ChatReply => "REPLY",
            }, util::safe_truncate(&aufgabe.anweisung, 80)));

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
            let timeout_duration = std::time::Duration::from_secs(aufgabe.timeout_s.max(30));
            let aufgabe_id = aufgabe.id.clone();
            let aufgabe_modul = aufgabe.modul.clone();
            let aufgabe_timeout = aufgabe.timeout_s;

            // RAII-Cleanup-Guard. Räumt busy + handles IMMER auf — egal ob
            // exec_llm normal returned, timeout fired, oder ein Panic hochkommt
            // (Unwinding ruft Drop). Ohne Guard würde ein Panic den Task
            // abrupt killen und die Map-Einträge für immer stehenlassen
            // (→ Modul frozen bei max_concurrent).
            let _guard = BusyGuard::new(
                busy.clone(), handles.clone(),
                aufgabe_modul.clone(), aufgabe_id.clone(),
            );

            let timed_out = tokio::time::timeout(timeout_duration, async {
                match aufgabe.typ {
                    AufgabeTyp::Direct => exec_direct(&mut aufgabe, &pipeline, &config, &llm, &py_modules, &py_pool).await,
                    AufgabeTyp::LlmCall => exec_llm(&mut aufgabe, &pipeline, &config, &llm, &py_modules, &py_pool, &tokens).await,
                    AufgabeTyp::ChatReply => {
                        aufgabe.ergebnis = Some(aufgabe.anweisung.clone());
                        if let Err(e) = pipeline.verschieben(&mut aufgabe, AufgabeStatus::Success) {
                            pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error, &format!("Verschieben failed: {e}"));
                        }
                    }
                }
            }).await;

            if timed_out.is_err() {
                pipeline.log("cycle", Some(&aufgabe_id), LogTyp::Error,
                    &format!("Task timeout nach {}s — abgebrochen", aufgabe_timeout));
                if let Ok(Some(mut failed)) = pipeline.laden_by_id(&aufgabe_id) {
                    failed.ergebnis = Some(format!("FAILED: Timeout nach {}s", aufgabe_timeout));
                    if let Err(e) = pipeline.verschieben(&mut failed, AufgabeStatus::Failed) {
                        pipeline.log("cycle", Some(&aufgabe_id), LogTyp::Error, &format!("Verschieben failed: {e}"));
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
            h.entry(aufgabe_modul_outer).or_default().insert(aufgabe_id_outer, join.abort_handle());
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
                pipeline.log("chain", None, LogTyp::Info,
                    &format!("Step {} skipped (condition '{}' not met)", i + 1, cond));
                continue;
            }
        }

        // Replace {result} placeholder in params
        let params: Vec<String> = step.params.iter()
            .map(|p| p.replace("{result}", &last_result))
            .collect();

        pipeline.log("chain", None, LogTyp::Info,
            &format!("Chain step {}/{}: {}({})", i + 1, chain.len(), step.tool, params.join(", ")));

        // Chain-Step-Idempotency: task_id + Step-Index als stabiler Key. Ein
        // bereits-erfolgreicher Step wird bei Retry (Watchdog-Abort + Re-Claim)
        // aus Cache bedient, sein Seiteneffekt nicht doppelt ausgeführt.
        let step_task_id = task_id.map(|t| format!("{}#step{}", t, i));
        let result = exec_tool(&step.tool, &params, modul_id, step_task_id.as_deref(), pipeline, config, llm, py_modules, py_pool).await;
        last_success = result.0;
        last_result = result.1;

        pipeline.log("chain", None,
            if last_success { LogTyp::Success } else { LogTyp::Failed },
            &format!("Chain step {}: {} -> {}", i + 1,
                if last_success { "OK" } else { "FAIL" },
                util::safe_truncate(&last_result, 80)));

        // Stop on failure if configured
        if !last_success && step.stop_on_fail {
            pipeline.log("chain", None, LogTyp::Warning,
                &format!("Chain aborted at step {} (stop_on_fail=true)", i + 1));
            break;
        }
    }

    (last_success, last_result)
}

/// Evaluate a chain step condition
fn evaluate_condition(condition: &str, last_result: &str, last_success: bool) -> bool {
    let cond = condition.trim();

    if cond == "success" { return last_success; }
    if cond == "failed" { return !last_success; }

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
                pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error, &format!("Verschieben failed: {e}"));
            }
            return;
        }
    };

    // Chain execution: special tool name "__chain__"
    if tool_name == "__chain__" {
        let chain_json = aufgabe.params.first().map(|s| s.as_str()).unwrap_or("[]");
        match serde_json::from_str::<Vec<crate::types::ChainStep>>(chain_json) {
            Ok(chain) => {
                let (success, result) = execute_chain(&chain, &aufgabe.modul, Some(&aufgabe.id), pipeline, config, llm, py_modules, py_pool).await;
                aufgabe.ergebnis = Some(result);
                if let Err(e) = pipeline.verschieben(aufgabe, if success { AufgabeStatus::Success } else { AufgabeStatus::Failed }) {
                    pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error, &format!("Verschieben failed: {e}"));
                }
                // Route result
                let cfg = config.read().await;
                route_ergebnis(aufgabe, pipeline, &cfg);
                return;
            }
            Err(e) => {
                aufgabe.ergebnis = Some(format!("FAILED: Chain parse error: {}", e));
                if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                    pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error, &format!("Verschieben failed: {e}"));
                }
                return;
            }
        }
    }

    pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Info,
        &format!("Direct tool: {}({})", tool_name, aufgabe.params.join(", ")));

    let result = exec_tool(&tool_name, &aufgabe.params, &aufgabe.modul, Some(&aufgabe.id), pipeline, config, llm, py_modules, py_pool).await;

    let status = if result.0 { "SUCCESS" } else { "FAILED" };
    pipeline.log("cycle", Some(&aufgabe.id),
        if result.0 { LogTyp::Success } else { LogTyp::Failed },
        &format!("Tool {}: {} → {}", tool_name, status, util::safe_truncate(&result.1, 100)));

    let antwort = if let Some(template) = &aufgabe.antwort_template {
        template.replace("<RESULT>", &result.1)
    } else {
        result.1.clone()
    };

    aufgabe.ergebnis = Some(antwort);
    if let Err(e) = pipeline.verschieben(aufgabe, if result.0 { AufgabeStatus::Success } else { AufgabeStatus::Failed }) {
        pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error, &format!("Verschieben failed: {e}"));
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
) {
    let cfg = config.read().await;
    let modul = cfg.module.iter().find(|m| m.id == aufgabe.modul)
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
    // OpenAI Function Calling Tools
    let (openai_tools, py_mods_snap) = {
        let py_mods = py_modules.read().await;
        let tools = tools::tools_as_openai_json(&modul, &py_mods);
        let snap = py_mods.clone();
        (tools, snap)
    };

    // Snapshot guardrail config and full config once before the loop.
    let (gcfg, cfg_snap) = {
        let cfg_guard = config.read().await;
        let gcfg = cfg_guard.guardrail.clone()
            .unwrap_or_else(crate::types::GuardrailConfig::default);
        let cfg_snap = cfg_guard.clone();
        (gcfg, cfg_snap)
    };
    let mut backend_id = modul.llm_backend.clone();
    let mut model_str = cfg_snap.llm_backends.iter()
        .find(|b| b.id == backend_id)
        .map(|b| b.model.clone())
        .unwrap_or_default();

    let mut messages: Vec<serde_json::Value> = vec![];
    messages.push(serde_json::json!({"role": "system", "content": full_system}));
    messages.push(serde_json::json!({"role": "user", "content": aufgabe.anweisung.clone()}));

    let final_answer;
    let mut tool_round = 0;
    let mut total_tokens: u64 = 0;
    let mut guardrail_retries: u32 = 0;
    let mut used_fallback = false;

    loop {
        if tool_round >= MAX_TOOL_ROUNDS {
            aufgabe.ergebnis = Some(format!("FAILED: Maximum tool rounds ({}) erreicht", MAX_TOOL_ROUNDS));
            pipeline.log(&modul.name, Some(&aufgabe.id), LogTyp::Failed,
                &format!("Max tool rounds ({}) erreicht — Task abgebrochen", MAX_TOOL_ROUNDS));
            if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error, &format!("Verschieben failed: {e}"));
            }
            return;
        }

        // Token budget check (per-modul, zählt Tokens dieses Tasks)
        if token_budget > 0 && total_tokens > token_budget {
            aufgabe.ergebnis = Some(format!("FAILED: Token-Budget ueberschritten ({}/{})", total_tokens, token_budget));
            pipeline.log(&modul.name, Some(&aufgabe.id), LogTyp::Failed,
                &format!("Token-Budget ueberschritten: {}/{} — Task abgebrochen", total_tokens, token_budget));
            if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error, &format!("Verschieben failed: {e}"));
            }
            return;
        }

        // Global daily USD budget check — pre-call, fail-closed. Liest den aktuellen
        // Config-Snapshot (nicht cfg_snap) damit UI-seitige Cap-Änderungen sofort greifen.
        {
            let cfg_live = config.read().await;
            if let Err(msg) = crate::web::check_daily_budget(&pipeline.store.pool, tokens, &cfg_live, &model_str).await {
                drop(cfg_live);
                pipeline.log(&modul.name, Some(&aufgabe.id), LogTyp::Failed, &msg);
                aufgabe.ergebnis = Some(format!("FAILED: {}", msg));
                if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                    pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error, &format!("Verschieben failed: {e}"));
                }
                return;
            }
        }

        let result = llm.chat_with_tools(&backend_id, modul.backup_llm.as_deref(), &messages, &openai_tools).await;

        match result {
            Ok((response, mut raw_data)) => {
                // Token tracking lokal (Modul-Budget) + global (USD-Cap über web::track_tokens).
                let input_tokens = raw_data.pointer("/usage/prompt_tokens").and_then(|v| v.as_u64())
                    .or_else(|| raw_data.pointer("/prompt_eval_count").and_then(|v| v.as_u64()))
                    .unwrap_or(0);
                let output_tokens = raw_data.pointer("/usage/completion_tokens").and_then(|v| v.as_u64())
                    .or_else(|| raw_data.pointer("/eval_count").and_then(|v| v.as_u64()))
                    .unwrap_or(0);
                total_tokens += input_tokens + output_tokens;

                // Globaler Token/USD-Tracker — damit daily_budget_usd aus dem nächsten
                // exec_llm-Call (und /api/chat) den Stand sehen kann.
                crate::web::track_tokens(&pipeline.store.pool, tokens, &backend_id, &model_str, &aufgabe.modul, &raw_data).await;

                // Guardrail-Bypass-Fix: wenn das LLM ein <tool>name(params)</tool> im
                // Response-Text hatte statt OpenAI tool_calls, injiziere den Call als
                // synthetische tool_calls in raw_data. Args werden mit den Schema-
                // Namen versehen (required[] aus tools_as_openai_json) statt param0/
                // param1 — sonst findet der downstream schema-aware Parser die Keys
                // nicht und liefert alle Parameter leer zurück (GLM-Finding Run 5).
                // Ohne Schema (unbekanntes Tool) fällt auf param<i> zurück; der Call
                // wird dann beim Guardrail wegen "unknown tool" rejected — das ist
                // das gewünschte Verhalten (fail-safe).
                if raw_data.pointer("/choices/0/message/tool_calls").is_none() {
                    if let Some((t_name, t_params)) = crate::tools::parse_tool_call(&response) {
                        let schema = crate::tools::schema_required_for(&t_name, &modul, &py_mods_snap);
                        let mut args = serde_json::Map::new();
                        if let Some(ref schema_keys) = schema {
                            // Schema-Reihenfolge: mappt params positionsweise auf required-Namen
                            for (i, key) in schema_keys.iter().enumerate() {
                                let val = t_params.get(i).cloned().unwrap_or_default();
                                args.insert(key.clone(), serde_json::json!(val));
                            }
                        } else {
                            // Kein Schema → synthetische param<i> Namen; der nachfolgende
                            // Schema-basierte Parser findet sie nicht → alle Params empty →
                            // Guardrail rejected das als unknown tool / invalid args.
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
                                obj.insert("tool_calls".into(), serde_json::json!([synthetic_call]));
                            }
                        } else if let Some(choices) = raw_data.pointer_mut("/choices").and_then(|v| v.as_array_mut()) {
                            if choices.is_empty() {
                                choices.push(serde_json::json!({"message": {"tool_calls": [synthetic_call]}}));
                            }
                        } else if let Some(obj) = raw_data.as_object_mut() {
                            obj.insert("choices".into(), serde_json::json!([
                                {"message": {"tool_calls": [synthetic_call]}}
                            ]));
                        }
                    }
                }

                // Token budget warning (pro Modul)
                if token_budget_warning > 0 && total_tokens > token_budget_warning && total_tokens - (input_tokens + output_tokens) <= token_budget_warning {
                    pipeline.log(&modul.name, Some(&aufgabe.id), LogTyp::Warning,
                        &format!("Token-Budget Warnung: {}/{} Tokens verbraucht", total_tokens, token_budget));
                }

                // ── Guardrail validation ───────────────────────────────────
                if gcfg.enabled {
                    let last_user_msg = messages.iter().rev()
                        .find(|m| m["role"] == "user")
                        .and_then(|m| m["content"].as_str())
                        .map(|s| s.to_string());
                    let max_retries_for_backend = gcfg.per_backend_overrides
                        .get(&backend_id).copied()
                        .unwrap_or(gcfg.max_retries);
                    let vctx = crate::guardrail::ValidatorContext {
                        modul_id: &aufgabe.modul,
                        cfg: &cfg_snap,
                        py_modules: &py_mods_snap,
                        last_user_msg: last_user_msg.as_deref(),
                        strict_mode: gcfg.strict_mode,
                    };
                    match crate::guardrail::validate_response(&raw_data, &vctx) {
                        Ok(_parsed) => {
                            let ev = crate::types::GuardrailEvent {
                                ts: chrono::Utc::now().timestamp(),
                                modul: aufgabe.modul.clone(),
                                backend: backend_id.clone(),
                                model: model_str.clone(),
                                tool_name: _parsed.first().map(|c| c.tool_name.clone()),
                                passed: true,
                                errors: vec![],
                                retry_attempt: guardrail_retries,
                                final_outcome: if guardrail_retries > 0 { "retried".into() } else { "ok".into() },
                                similar_suggestion: None,
                            };
                            let _ = crate::guardrail::log_event(&pipeline.base, &ev).await;
                            guardrail_retries = 0;
                        }
                        Err(errors) => {
                            let is_last = guardrail_retries >= max_retries_for_backend;
                            let ev = crate::types::GuardrailEvent {
                                ts: chrono::Utc::now().timestamp(),
                                modul: aufgabe.modul.clone(),
                                backend: backend_id.clone(),
                                model: model_str.clone(),
                                tool_name: None,
                                passed: false,
                                errors: errors.clone(),
                                retry_attempt: guardrail_retries,
                                final_outcome: if is_last { "hard_fail".into() } else { "retried".into() },
                                similar_suggestion: None,
                            };
                            let _ = crate::guardrail::log_event(&pipeline.base, &ev).await;
                            if is_last {
                                // Check if backup_llm available + fallback flag on
                                let mod_cfg = cfg_snap.module.iter().find(|m| m.id == aufgabe.modul);
                                let backup_id = mod_cfg.and_then(|m| m.backup_llm.clone());
                                if gcfg.fallback_on_hard_fail && backup_id.is_some() && !used_fallback {
                                    if let Some(bid) = backup_id {
                                        if let Some(bb) = cfg_snap.llm_backends.iter().find(|b| b.id == bid).cloned() {
                                            let codes: Vec<String> = errors.iter().map(|e| e.code.clone()).collect();
                                            let _ = crate::guardrail::log_fallback_event(&pipeline.base, &backend_id, &bid, &aufgabe.modul, &codes).await;
                                            backend_id = bb.id.clone();
                                            model_str = bb.model.clone();
                                            used_fallback = true;
                                            guardrail_retries = 0;
                                            continue;  // retry with backup
                                        }
                                    }
                                }
                                // Real hard-fail
                                let codes: Vec<String> = errors.iter().map(|e| e.code.clone()).collect();
                                let msg = format!("Guardrail hard-fail: {}", codes.join(", "));
                                pipeline.log(&modul.name, Some(&aufgabe.id), LogTyp::Failed, &msg);
                                aufgabe.ergebnis = Some(format!("FAILED: {}", msg));
                                if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                                    pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error,
                                        &format!("Verschieben failed: {e}"));
                                }
                                return;
                            } else {
                                let feedback = crate::guardrail::synth_feedback_user_message(
                                    &errors, max_retries_for_backend, guardrail_retries,
                                );
                                messages.push(serde_json::json!({"role": "user", "content": feedback}));
                                guardrail_retries += 1;
                                continue;
                            }
                        }
                    }
                }
                // ── End guardrail ──────────────────────────────────────────

                // Tool-Call-Extraktion mit Schema-basiertem Parameter-Ordering:
                // wir holen zuerst den Namen, dann das required[]-Array des passenden
                // Tools, und geben das an parse_openai_tool_call_with_schema. Das
                // schließt den path_keys-Heuristik-Bypass (ein LLM hätte sonst durch
                // Nicht-Standard-Keys wie "inhalt"/"ziel" die Reihenfolge manipulieren
                // können — die Whitelist-Prüfung lief dann auf dem falsch zugeordneten
                // Parameter).
                let tool_call = if raw_data != serde_json::Value::Null {
                    // Namen zuerst ohne Schema ziehen, dann Schema lookup, dann richtig parsen
                    let tmp_name = tools::parse_openai_tool_call(&raw_data).map(|(n, _)| n);
                    match tmp_name {
                        Some(name) => {
                            let schema = tools::schema_required_for(&name, &modul, &py_mods_snap);
                            tools::parse_openai_tool_call_with_schema(&raw_data, schema.as_deref())
                        }
                        None => None,
                    }
                } else { None }.or_else(|| tools::parse_tool_call(&response));

                if let Some((tool_name, params)) = tool_call {
                    tool_round += 1;
                    pipeline.log(&modul.name, Some(&aufgabe.id), LogTyp::Info,
                        &format!("Tool call: {}({})", tool_name, params.join(", ")));

                    // Tool-Round im Idempotency-Key: LLM kann dasselbe Tool in einer
                    // Task mehrfach rufen (unterschiedliche Intent-Iterationen) — wir
                    // dürfen nur KOMPLETTE Task-Wiederholungen deduplicaten, nicht
                    // jeden Tool-Round. task_id + round macht den Key eindeutig pro
                    // Iteration.
                    let tool_task_id = format!("{}#r{}", aufgabe.id, tool_round);
                    let tool_result = exec_tool(&tool_name, &params, &aufgabe.modul, Some(&tool_task_id), pipeline, config, llm, py_modules, py_pool).await;
                    let status = if tool_result.0 { "SUCCESS" } else { "FAILED" };
                    pipeline.log(&modul.name, Some(&aufgabe.id),
                        if tool_result.0 { LogTyp::Success } else { LogTyp::Failed },
                        &format!("Tool {}: {} → {}", tool_name, status, util::safe_truncate(&tool_result.1, 100)));

                    let call_id = raw_data.pointer("/choices/0/message/tool_calls/0/id")
                        .and_then(|v| v.as_str()).unwrap_or("call_0").to_string();
                    messages.push(serde_json::json!({"role": "assistant", "content": serde_json::Value::Null,
                        "tool_calls": [{"id": &call_id, "type": "function", "function": {"name": &tool_name, "arguments": "{}"}}]}));
                    messages.push(serde_json::json!({"role": "tool", "tool_call_id": &call_id,
                        "content": format!("{}: {}", status, tool_result.1)}));

                    // History trimmen: alte Tool-Results kuerzen
                    let keep_full = 6;
                    if messages.len() > 2 + keep_full + 4 {
                        for i in 2..(messages.len().saturating_sub(keep_full)) {
                            if messages[i].get("role").and_then(|v| v.as_str()) == Some("tool") {
                                if let Some(content) = messages[i].get("content").and_then(|v| v.as_str()) {
                                    if content.len() > 100 {
                                        let short = format!("{}...[gekuerzt]", util::safe_truncate(content, 100));
                                        messages[i]["content"] = serde_json::json!(short);
                                    }
                                }
                            }
                        }
                    }
                    continue;
                } else {
                    final_answer = response;
                    break;
                }
            }
            Err(e) => {
                // LLM-Call ist fehlgeschlagen — die Reservation aus check_daily_budget
                // muss zurückgebucht werden, sonst akkumuliert sie. track_tokens wird
                // hier NICHT gerufen (keine Response), also muss release explizit.
                {
                    let cfg_live = config.read().await;
                    crate::web::release_reservation(&pipeline.store.pool, tokens, &cfg_live, &model_str).await;
                }
                aufgabe.retry_count += 1;
                if aufgabe.retry_count <= aufgabe.retry {
                    pipeline.log(&modul.name, Some(&aufgabe.id), LogTyp::Warning,
                        &format!("RETRY {}/{}: {}", aufgabe.retry_count, aufgabe.retry, e));
                    if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Erstellt) {
                        pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error, &format!("Verschieben failed: {e}"));
                    }
                } else {
                    aufgabe.ergebnis = Some(format!("FAILED: {e}"));
                    pipeline.log(&modul.name, Some(&aufgabe.id), LogTyp::Failed, &format!("FAILED: {e}"));
                    if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Failed) {
                        pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error, &format!("Verschieben failed: {e}"));
                    }
                }
                return;
            }
        }
    }

    aufgabe.ergebnis = Some(final_answer.clone());
    pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Success,
        &format!("SUCCESS: {}", util::safe_truncate(&final_answer, 100)));
    if let Err(e) = pipeline.verschieben(aufgabe, AufgabeStatus::Success) {
        pipeline.log("cycle", Some(&aufgabe.id), LogTyp::Error, &format!("Verschieben failed: {e}"));
    }
    // Route result back if zurueck_an is set
    if aufgabe.status == AufgabeStatus::Success || aufgabe.status == AufgabeStatus::Failed {
        let cfg = config.read().await;
        route_ergebnis(aufgabe, pipeline, &cfg);
    }
}

fn route_ergebnis(aufgabe: &Aufgabe, pipeline: &Pipeline, config: &AgentConfig) {
    let Some(ref zurueck) = aufgabe.zurueck_an else { return; };

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
    let (is_chat, target) = if let Some(t) = zurueck.strip_prefix("chat:") {
        (true, t)
    } else {
        (false, zurueck.as_str())
    };

    // Linking-Check nur für non-chat: Target muss verlinkt oder Selbst sein
    if !is_chat && aufgabe.modul != target {
        let source_modul = config.module.iter().find(|m| m.id == aufgabe.modul);
        if let Some(source) = source_modul {
            if !source.linked_modules.contains(&target.to_string()) {
                pipeline.log("routing", Some(&aufgabe.id), LogTyp::Warning,
                    &format!("Routing blocked: {} not linked to {}", aufgabe.modul, target));
                return;
            }
        }
    }

    let ergebnis = aufgabe.ergebnis.as_deref().unwrap_or("Kein Ergebnis");
    let payload = format!("[Ergebnis von {}]: {}", aufgabe.modul, ergebnis);
    let mut result_task = Aufgabe::direct("__chat_reply__", vec![payload], target, &aufgabe.modul, None, None);
    result_task.typ = crate::types::AufgabeTyp::ChatReply;
    result_task.anweisung = format!("[Ergebnis von {}]", aufgabe.modul);
    result_task.tool = None;
    let _ = pipeline.speichern(&result_task);
    pipeline.log("routing", Some(&aufgabe.id), LogTyp::Info,
        &format!("Ergebnis geroutet an {}{} (ChatReply)", if is_chat { "chat:" } else { "" }, target));
}

async fn exec_tool(
    tool_name: &str, params: &[String], modul_id: &str,
    task_id: Option<&str>,
    pipeline: &Arc<Pipeline>, config: &Arc<RwLock<AgentConfig>>,
    llm: &Arc<LlmRouter>, py_modules: &Arc<RwLock<Vec<crate::loader::PyModuleMeta>>>,
    py_pool: &Arc<crate::loader::PyProcessPool>,
) -> (bool, String) {
    let config_snapshot = config.read().await.clone();
    let py_mods = py_modules.read().await;
    tools::exec_tool_unified(tool_name, params, modul_id, task_id, pipeline, llm, &py_mods, py_pool, &config_snapshot).await
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
        assert!(evaluate_condition("contains:ERROR", "Task ERROR occurred", true));
        assert!(!evaluate_condition("contains:ERROR", "All good", true));
    }

    #[test]
    fn test_evaluate_condition_not_contains() {
        assert!(evaluate_condition("not_contains:FAIL", "SUCCESS done", true));
        assert!(!evaluate_condition("not_contains:FAIL", "FAIL happened", true));
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
}
