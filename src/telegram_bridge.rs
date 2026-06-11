use crate::llm::LlmRouter;
use crate::loader::{PyModuleMeta, PyProcessPool};
use crate::pipeline::Pipeline;
use crate::types::{AgentConfig, LogTyp};
use std::collections::HashSet;
use std::sync::Arc;
use tokio::sync::{Mutex, RwLock};

pub async fn run(
    config: Arc<RwLock<AgentConfig>>,
    pipeline: Arc<Pipeline>,
    llm: Arc<LlmRouter>,
    py_modules: Arc<RwLock<Vec<PyModuleMeta>>>,
    py_pool: Arc<PyProcessPool>,
) {
    pipeline.log(
        "telegram_bridge",
        None,
        LogTyp::Info,
        "Telegram Bridge gestartet",
    );

    let active: Arc<Mutex<HashSet<String>>> = Arc::new(Mutex::new(HashSet::new()));

    loop {
        let module_ids = {
            let cfg = config.read().await;
            cfg.module
                .iter()
                .filter(|m| m.typ == "telegram_bot")
                .filter(|m| {
                    m.settings
                        .extra
                        .get("enabled")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(true)
                })
                .map(|m| m.id.clone())
                .collect::<Vec<_>>()
        };

        for module_id in module_ids {
            let mut guard = active.lock().await;
            if guard.contains(&module_id) {
                continue;
            }
            guard.insert(module_id.clone());
            drop(guard);

            let cfg_ref = config.clone();
            let pipeline_ref = pipeline.clone();
            let llm_ref = llm.clone();
            let py_modules_ref = py_modules.clone();
            let py_pool_ref = py_pool.clone();
            let active_ref = active.clone();
            tokio::spawn(async move {
                telegram_module_loop(
                    module_id.clone(),
                    cfg_ref,
                    pipeline_ref,
                    llm_ref,
                    py_modules_ref,
                    py_pool_ref,
                )
                .await;
                active_ref.lock().await.remove(&module_id);
            });
        }

        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    }
}

async fn telegram_module_loop(
    module_id: String,
    config: Arc<RwLock<AgentConfig>>,
    pipeline: Arc<Pipeline>,
    llm: Arc<LlmRouter>,
    py_modules: Arc<RwLock<Vec<PyModuleMeta>>>,
    py_pool: Arc<PyProcessPool>,
) {
    pipeline.log(
        &module_id,
        None,
        LogTyp::Info,
        "Telegram Bot Poll-Loop aktiv",
    );

    loop {
        let (exists, enabled, interval_ms) = {
            let cfg = config.read().await;
            match cfg.module.iter().find(|m| m.id == module_id) {
                Some(m) => {
                    let enabled = m
                        .settings
                        .extra
                        .get("enabled")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(true);
                    (
                        true,
                        enabled,
                        m.scheduler_interval_ms.unwrap_or(2000).max(500),
                    )
                }
                None => (false, false, 2000),
            }
        };
        if !exists || !enabled {
            pipeline.log(
                &module_id,
                None,
                LogTyp::Info,
                "Telegram Bot Poll-Loop beendet",
            );
            return;
        }

        let cfg_snapshot = config.read().await.clone();
        let py_mods = py_modules.read().await.clone();
        let (ok, detail) = crate::tools::exec_tool_unified(
            "telegram_bot.poll",
            &[],
            &module_id,
            None,
            &pipeline,
            &llm,
            &py_mods,
            &py_pool,
            &cfg_snapshot,
            None,
        )
        .await;

        if !ok {
            pipeline.log(
                &module_id,
                None,
                LogTyp::Warning,
                &format!(
                    "Telegram poll fehlgeschlagen: {}",
                    crate::util::safe_truncate(&detail, 160)
                ),
            );
            tokio::time::sleep(std::time::Duration::from_secs(10)).await;
        } else if !detail.contains("updates=0") {
            pipeline.log(
                &module_id,
                None,
                LogTyp::Info,
                &crate::util::safe_truncate(&detail, 160),
            );
        }

        tokio::time::sleep(std::time::Duration::from_millis(interval_ms)).await;
    }
}
