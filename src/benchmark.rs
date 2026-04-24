// src/benchmark.rs — Runs a curated set of prompts against a configured LLM
// backend and reports tool-call quality.

use std::sync::Arc;
use tokio::sync::mpsc;

use crate::types::{AgentConfig, LlmBackend, BenchmarkCase, BenchmarkExpectation, BenchmarkResult, BenchmarkReport};
use crate::guardrail::{validate_response, ValidatorContext};

#[derive(serde::Deserialize)]
struct Suite {
    pub version: u32,
    pub cases: Vec<BenchmarkCase>,
}

pub fn load_suite() -> Result<Vec<BenchmarkCase>, String> {
    let raw = include_str!("../modules/templates/benchmark_prompts.json");
    let s: Suite = serde_json::from_str(raw).map_err(|e| e.to_string())?;
    let _ = s.version; // suppress warning
    Ok(s.cases)
}

#[derive(serde::Serialize, Clone)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum BenchmarkEvent {
    CaseStart { case_id: String, prompt: String, n: usize, of: usize },
    CaseResult { result: BenchmarkResult },
    Report { report: BenchmarkReport },
    Error { message: String },
}

/// Run the benchmark suite against `backend`. Uses `run_modul_id` as the calling
/// modul context for validation. Streams events via `tx`.
pub async fn run_benchmark(
    backend: LlmBackend,
    run_modul_id: String,
    cfg_snapshot: AgentConfig,
    py_modules: Vec<crate::loader::PyModuleMeta>,
    llm: Arc<crate::llm::LlmRouter>,
    tx: mpsc::Sender<BenchmarkEvent>,
) {
    let cases = match load_suite() {
        Ok(c) => c,
        Err(e) => { let _ = tx.send(BenchmarkEvent::Error { message: e }).await; return; }
    };
    let total = cases.len();
    let mut report = BenchmarkReport {
        backend: backend.id.clone(), model: backend.model.clone(),
        started_at: chrono::Utc::now().timestamp(),
        total_cases: total, passed: 0, failed: 0, denied: 0,
        total_latency_ms: 0, results: Vec::with_capacity(total),
    };

    let modul = match cfg_snapshot.module.iter().find(|m| m.id == run_modul_id) {
        Some(m) => m,
        None => {
            let _ = tx.send(BenchmarkEvent::Error {
                message: format!("run_modul_id '{}' not found in config", run_modul_id),
            }).await;
            return;
        }
    };

    let tools_json = crate::tools::tools_as_openai_json(modul, &py_modules);

    for (i, c) in cases.iter().enumerate() {
        let _ = tx.send(BenchmarkEvent::CaseStart {
            case_id: c.id.clone(), prompt: c.prompt.clone(), n: i + 1, of: total,
        }).await;

        let messages = vec![serde_json::json!({"role": "user", "content": c.prompt})];
        let t_start = std::time::Instant::now();
        let raw = match llm.chat_with_tools_adhoc(&backend, &messages, &tools_json).await {
            Ok((_text, raw)) => raw,
            Err(e) => {
                let r = BenchmarkResult {
                    case_id: c.id.clone(), prompt: c.prompt.clone(),
                    passed: false, actual_tool: None,
                    errors: vec![crate::types::ValidationError {
                        field: "network".into(), code: "backend_error".into(),
                        human_message_de: e,
                    }],
                    latency_ms: t_start.elapsed().as_millis() as u64,
                };
                report.failed += 1;
                report.total_latency_ms += r.latency_ms;
                let _ = tx.send(BenchmarkEvent::CaseResult { result: r.clone() }).await;
                report.results.push(r);
                continue;
            }
        };

        let vctx = ValidatorContext {
            modul_id: &run_modul_id,
            cfg: &cfg_snapshot,
            py_modules: &py_modules,
            last_user_msg: Some(&c.prompt),
            strict_mode: false,
        };
        let validated = validate_response(&raw, &vctx);
        let latency_ms = t_start.elapsed().as_millis() as u64;

        let (passed, actual_tool, errors) = match (&c.expected, &validated) {
            (BenchmarkExpectation::ToolCalled { tool_name }, Ok(calls)) => {
                let first = calls.first().map(|p| p.tool_name.clone());
                let ok = first.as_deref() == Some(tool_name.as_str());
                (ok, first, vec![])
            }
            (BenchmarkExpectation::NoToolCall, Ok(calls)) => (calls.is_empty(), calls.first().map(|p| p.tool_name.clone()), vec![]),
            (BenchmarkExpectation::Denied, Err(errs)) => {
                let denied = errs.iter().any(|e| e.code == "no_permission");
                (denied, None, errs.clone())
            }
            (_, Err(errs)) => (false, None, errs.clone()),
            (BenchmarkExpectation::Denied, Ok(_calls)) => (false, None, vec![]),
        };

        let is_denied_expected = matches!(c.expected, BenchmarkExpectation::Denied);
        if passed { report.passed += 1; } else { report.failed += 1; }
        if is_denied_expected && passed { report.denied += 1; }
        report.total_latency_ms += latency_ms;

        let r = BenchmarkResult {
            case_id: c.id.clone(), prompt: c.prompt.clone(),
            passed, actual_tool, errors, latency_ms,
        };
        let _ = tx.send(BenchmarkEvent::CaseResult { result: r.clone() }).await;
        report.results.push(r);
    }
    let _ = tx.send(BenchmarkEvent::Report { report }).await;
}

#[derive(serde::Serialize, Clone)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum CompareEvent {
    SideStart { side: String, backend: String, model: String },
    SideCaseResult { side: String, result: crate::types::BenchmarkResult },
    Report { report: crate::types::BenchmarkCompareReport },
    Error { message: String },
}

pub async fn run_compare(
    backend_a: crate::types::LlmBackend,
    backend_b: crate::types::LlmBackend,
    run_modul_id: String,
    cfg_snapshot: crate::types::AgentConfig,
    py_modules: Vec<crate::loader::PyModuleMeta>,
    llm: std::sync::Arc<crate::llm::LlmRouter>,
    tx: tokio::sync::mpsc::Sender<CompareEvent>,
) {
    let _ = tx.send(CompareEvent::SideStart {
        side: "A".into(), backend: backend_a.id.clone(), model: backend_a.model.clone(),
    }).await;
    let _ = tx.send(CompareEvent::SideStart {
        side: "B".into(), backend: backend_b.id.clone(), model: backend_b.model.clone(),
    }).await;

    let (tx_a, mut rx_a) = tokio::sync::mpsc::channel::<BenchmarkEvent>(64);
    let (tx_b, mut rx_b) = tokio::sync::mpsc::channel::<BenchmarkEvent>(64);

    let cfg_a = cfg_snapshot.clone();
    let py_a = py_modules.clone();
    let llm_a = llm.clone();
    let modul_a = run_modul_id.clone();
    let a_handle = tokio::spawn(async move {
        run_benchmark(backend_a, modul_a, cfg_a, py_a, llm_a, tx_a).await;
    });
    let cfg_b = cfg_snapshot.clone();
    let py_b = py_modules.clone();
    let llm_b = llm.clone();
    let modul_b = run_modul_id.clone();
    let b_handle = tokio::spawn(async move {
        run_benchmark(backend_b, modul_b, cfg_b, py_b, llm_b, tx_b).await;
    });

    let tx_fwd_a = tx.clone();
    let collect_a = tokio::spawn(async move {
        let mut report: Option<crate::types::BenchmarkReport> = None;
        while let Some(ev) = rx_a.recv().await {
            match ev {
                BenchmarkEvent::CaseResult { result } => {
                    let _ = tx_fwd_a.send(CompareEvent::SideCaseResult { side: "A".into(), result }).await;
                }
                BenchmarkEvent::Report { report: r } => { report = Some(r); }
                _ => {}
            }
        }
        report
    });
    let tx_fwd_b = tx.clone();
    let collect_b = tokio::spawn(async move {
        let mut report: Option<crate::types::BenchmarkReport> = None;
        while let Some(ev) = rx_b.recv().await {
            match ev {
                BenchmarkEvent::CaseResult { result } => {
                    let _ = tx_fwd_b.send(CompareEvent::SideCaseResult { side: "B".into(), result }).await;
                }
                BenchmarkEvent::Report { report: r } => { report = Some(r); }
                _ => {}
            }
        }
        report
    });

    let _ = a_handle.await;
    let _ = b_handle.await;
    let ra = collect_a.await.ok().flatten();
    let rb = collect_b.await.ok().flatten();

    match (ra, rb) {
        (Some(a), Some(b)) => {
            let mut winners = Vec::new();
            for r_a in &a.results {
                if let Some(r_b) = b.results.iter().find(|x| x.case_id == r_a.case_id) {
                    let w = match (r_a.passed, r_b.passed) {
                        (true, false) => "A",
                        (false, true) => "B",
                        _ => "tie",
                    };
                    winners.push((r_a.case_id.clone(), w.to_string()));
                }
            }
            let report = crate::types::BenchmarkCompareReport {
                report_a: a, report_b: b, winner_per_case: winners,
            };
            let _ = tx.send(CompareEvent::Report { report }).await;
        }
        _ => {
            let _ = tx.send(CompareEvent::Error { message: "one or both benchmark reports missing".into() }).await;
        }
    }
}
