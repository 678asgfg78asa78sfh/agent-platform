mod types;
mod pipeline;
mod cycle;
mod watchdog;
mod llm;
mod tools;
mod modules;
mod web;
mod security;
mod wizard;
pub mod loader;
pub mod util;
pub mod guardrail;
pub mod benchmark;
pub mod store;

use std::sync::Arc;
use std::path::PathBuf;
use tokio::sync::RwLock;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt().with_target(false).with_level(true).init();

    let base_dir = std::env::args().nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("./agent-data"));

    tracing::info!("Agent Platform startet | Daten: {}", base_dir.display());

    let pipeline_raw = pipeline::Pipeline::new(&base_dir).expect("Pipeline-Ordner erstellen fehlgeschlagen");
    pipeline_raw.deduplizieren();
    let pipeline = Arc::new(pipeline_raw);

    // Config laden — graceful bei korrupter/fehlender Config, mit Backup-Fallback.
    // save_config in web.rs rotiert bei jedem Save in .bak-1/.bak-2/.bak-3. Wenn die
    // aktuelle config.json nicht parst (korrupt, halb-geschrieben durch älteren non-
    // atomic Write vor dem Fix, manuelles Bearbeiten mit Syntax-Fehler), probieren wir
    // nacheinander die drei Backups bevor wir auf Default fallen. Default-Fallback
    // würde alle Module, LLM-Backends und Permissions löschen — das wäre Totalverlust.
    let config_path = base_dir.join("config.json");
    let load_cfg = |path: &std::path::Path| -> Result<types::AgentConfig, String> {
        let raw = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
        serde_json::from_str::<types::AgentConfig>(&raw).map_err(|e| e.to_string())
    };
    let config: types::AgentConfig = if config_path.exists() {
        match load_cfg(&config_path) {
            Ok(c) => c,
            Err(e) => {
                tracing::error!("Config korrupt: {} — versuche Backups", e);
                let backup = config_path.with_extension("json.corrupt");
                let _ = std::fs::copy(&config_path, &backup);
                // Backup-Ketten durchprobieren (bak-1 = jüngstes)
                let mut recovered: Option<types::AgentConfig> = None;
                for slot in 1..=3u8 {
                    let bak = config_path.with_extension(format!("json.bak-{}", slot));
                    if bak.exists() {
                        match load_cfg(&bak) {
                            Ok(c) => {
                                tracing::warn!("Config aus Backup slot {} wiederhergestellt — current war korrupt", slot);
                                recovered = Some(c);
                                break;
                            }
                            Err(e2) => {
                                tracing::warn!("Backup slot {} auch korrupt: {}", slot, e2);
                            }
                        }
                    }
                }
                recovered.unwrap_or_else(|| {
                    tracing::error!("Kein lesbares Backup gefunden — verwende Defaults (ALLE Module weg!)");
                    types::AgentConfig::default()
                })
            }
        }
    } else {
        let default = types::AgentConfig::default();
        if let Ok(json) = serde_json::to_string_pretty(&default) {
            // Atomic write — first-run config must be valid JSON or nothing
            let _ = util::atomic_write(&config_path, json.as_bytes());
        }
        default
    };
    let admin_port = config.web_port;
    let bind_address = config.bind_address.clone();

    // Chat-Module mit eigenen Ports sammeln
    let chat_ports: Vec<(String, u16)> = config.module.iter()
        .filter(|m| m.typ == "chat" && m.settings.port.is_some())
        .map(|m| (m.id.clone(), m.settings.port.unwrap()))
        .collect();

    // Startup warning: exposed without auth
    if bind_address == "0.0.0.0" && config.api_auth_token.as_deref().unwrap_or("").is_empty() {
        tracing::warn!("SECURITY: bind_address=0.0.0.0 OHNE api_auth_token — nicht-lokale Zugriffe werden verweigert");
    }

    let config = Arc::new(RwLock::new(config));

    // Wizard session dir init + periodic expired-session cleanup
    {
        let cfg_snap = config.read().await;
        if let Some(wcfg) = cfg_snap.wizard.clone() {
            drop(cfg_snap);
            if let Err(e) = wizard::ensure_dirs(&base_dir).await {
                tracing::warn!("Wizard: ensure_dirs failed: {}", e);
            }
            let data_root = base_dir.clone();
            let timeout = wcfg.session_timeout_secs;
            tokio::spawn(async move {
                let mut interval = tokio::time::interval(std::time::Duration::from_secs(60));
                loop {
                    interval.tick().await;
                    let n = wizard::cleanup_expired(&data_root, timeout).await;
                    if n > 0 {
                        tracing::info!("wizard: cleaned up {} expired session(s)", n);
                    }
                }
            });
        }
    }

    // Guardrail init + periodic retention cleanup
    {
        let gcfg = config.read().await.guardrail.clone().unwrap_or_default();
        if gcfg.enabled {
            if let Err(e) = guardrail::ensure_dirs(&base_dir).await {
                tracing::warn!("Guardrail: ensure_dirs failed: {}", e);
            }
            let retention = config.read().await.log_retention_days;
            let data_root_clone = base_dir.clone();
            tokio::spawn(async move {
                let mut interval = tokio::time::interval(std::time::Duration::from_secs(86400));
                loop {
                    interval.tick().await;
                    let n = guardrail::cleanup_old_events(&data_root_clone, retention).await;
                    if n > 0 {
                        tracing::info!("guardrail: removed {} expired event log(s)", n);
                    }
                }
            });
        }
    }

    // Python-Module entdecken
    let modules_dir = base_dir.parent().unwrap_or(&base_dir).join("modules");
    let discovered = loader::discover_modules(&modules_dir);
    if !discovered.is_empty() {
        tracing::info!("{} Python-Module geladen", discovered.len());
    }
    let py_modules = Arc::new(RwLock::new(discovered));
    let py_pool = crate::loader::PyProcessPool::new(300); // 5 min idle timeout

    let llm = Arc::new(llm::LlmRouter::new(config.clone()));
    // Token/cost tracker — shared between HTTP chat path AND scheduler-driven LLM calls
    // so `daily_budget_usd` is enforced consistently regardless of entry point.
    let tokens: web::TokenTracker = Arc::new(RwLock::new(web::TokenStats::default()));
    let orchestrator = cycle::Orchestrator::new(pipeline.clone(), config.clone(), llm.clone(), py_modules.clone(), py_pool.clone(), tokens.clone());
    let watchdog = watchdog::Watchdog::new(
        orchestrator.heartbeats.clone(),
        120,
        pipeline.clone(),
        orchestrator.busy.clone(),
        orchestrator.handles.clone(),
    );
    let rate_limit = {
        let cfg = config.read().await;
        security::RateLimiter::new(cfg.chat_rate_limit_per_min)
    };
    let wizard_rate = {
        let cfg = config.read().await;
        security::RateLimiter::new(cfg.wizard.as_ref().map(|w| w.rate_limit_per_min).unwrap_or(10))
    };

    // Periodic rate-limit bucket cleanup (stale IPs removed every 5 min)
    let rl_for_cleanup = rate_limit.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(300)).await;
            rl_for_cleanup.cleanup().await;
        }
    });

    let web_state = Arc::new(web::AppState {
        pipeline: pipeline.clone(),
        config: config.clone(),
        llm: llm.clone(),
        heartbeats: orchestrator.heartbeats.clone(),
        py_modules: py_modules.clone(),
        py_pool: py_pool.clone(),
        busy: orchestrator.busy.clone(),
        tokens: tokens.clone(),
        rate_limit: rate_limit.clone(),
        wizard_rate: wizard_rate.clone(),
        data_root: base_dir.clone(),
        config_path: config_path.clone(),
        wizard_turn_inflight: Arc::new(tokio::sync::Mutex::new(std::collections::HashSet::new())),
    });

    tracing::info!("Admin-UI: http://{}:{}", bind_address, admin_port);

    // Guardrail alert loop (5-min poll, checks valid-rate per backend/model)
    {
        let gcfg_snap = config.read().await.guardrail.clone().unwrap_or_default();
        if gcfg_snap.enabled && gcfg_snap.alert.enabled {
            let cfg_ref = config.clone();
            let data_root_clone = base_dir.clone();
            let state_clone = web_state.clone();
            let cooldown_map = guardrail::new_alert_cooldown_map();
            tokio::spawn(async move {
                let mut tick = tokio::time::interval(std::time::Duration::from_secs(300));
                loop {
                    tick.tick().await;
                    let gcfg = cfg_ref.read().await.guardrail.clone().unwrap_or_default();
                    if !gcfg.alert.enabled { continue; }
                    let breaches = guardrail::check_alert_threshold(&gcfg, &data_root_clone, &cooldown_map).await;
                    for (backend, model, pct, n) in breaches {
                        let msg = format!(
                            "Guardrail Alert: {}/{} valid-rate bei {:.1}% (letzten {} min, {} Calls)",
                            backend, model, pct, gcfg.alert.window_minutes, n
                        );
                        let notify_modul = match gcfg.alert.notify_backend_id.as_deref() {
                            Some(m) => m,
                            None => {
                                tracing::warn!("Guardrail alert — no notify_backend_id: {}", msg);
                                let _ = guardrail::log_alert_event(&data_root_clone, &backend, &model, pct, n).await;
                                continue;
                            }
                        };
                        let params = vec![msg.clone()];
                        let py_mods = state_clone.py_modules.read().await.clone();
                        let cfg_full = cfg_ref.read().await.clone();
                        let (ok, detail) = crate::tools::exec_tool_unified(
                            "notify.send", &params, notify_modul,
                            None,  // Guardrail-Alert: keine task_id → keine Idempotency (by design)
                            &state_clone.pipeline, &state_clone.llm,
                            &py_mods, &state_clone.py_pool, &cfg_full,
                        ).await;
                        tracing::info!("Guardrail alert → notify ok={} {}", ok, detail);
                        let _ = guardrail::log_alert_event(&data_root_clone, &backend, &model, pct, n).await;
                    }
                }
            });
        }
    }

    // Chat-Instanzen auf eigenen Ports starten
    for (modul_id, chat_port) in &chat_ports {
        let state = web_state.clone();
        let mid = modul_id.clone();
        let cp = *chat_port;
        let bind = bind_address.clone();
        tokio::spawn(async move {
            match tokio::net::TcpListener::bind(format!("{}:{}", bind, cp)).await {
                Ok(listener) => {
                    tracing::info!("Chat '{}' auf Port {}", mid, cp);
                    let app = web::chat_router(state, mid.clone())
                        .into_make_service_with_connect_info::<std::net::SocketAddr>();
                    let _ = axum::serve(listener, app).await;
                }
                Err(e) => {
                    tracing::error!("Chat '{}' Port {} belegt: {}", mid, cp, e);
                }
            }
        });
    }

    let listener = tokio::net::TcpListener::bind(format!("{}:{}", bind_address, admin_port))
        .await.expect("Admin-Port belegt");

    let app = web::router(web_state)
        .into_make_service_with_connect_info::<std::net::SocketAddr>();

    tokio::select! {
        _ = axum::serve(listener, app) => {},
        _ = orchestrator.run() => {},
        _ = watchdog.run() => {},
    }
}
