use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio::io::AsyncWriteExt;
use tokio::io::{AsyncBufReadExt, BufReader};
use serde::{Deserialize, Serialize};

/// Beschreibung eines Python-Moduls (aus MODULE dict)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PyModuleMeta {
    pub name: String,
    pub description: String,
    pub version: String,
    #[serde(default)]
    pub settings: HashMap<String, serde_json::Value>,
    #[serde(default)]
    pub tools: Vec<PyToolDef>,
    #[serde(default)]
    pub path: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PyToolDef {
    pub name: String,
    pub description: String,
    pub params: Vec<String>,
}

/// Pool of persistent Python module processes. One entry per module name.
/// Each entry carries its own async Mutex so concurrent calls to different modules
/// run in parallel. Only same-module calls are serialized (required by stdio protocol).
pub struct PyProcessPool {
    registry: Mutex<HashMap<String, Arc<Mutex<Option<PyProcess>>>>>,
    max_idle_secs: u64,
}

struct PyProcess {
    stdin: tokio::process::ChildStdin,
    stdout: BufReader<tokio::process::ChildStdout>,
    child: tokio::process::Child,
    last_used: std::time::Instant,
    #[allow(dead_code)]
    module_path: PathBuf,
}

impl PyProcessPool {
    pub fn new(max_idle_secs: u64) -> Arc<Self> {
        Arc::new(Self {
            registry: Mutex::new(HashMap::new()),
            max_idle_secs,
        })
    }

    fn spawn_process(module_path: &Path) -> Result<PyProcess, String> {
        let mut child = tokio::process::Command::new("python3")
            .arg(module_path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| format!("Python spawn: {e}"))?;
        let stdin = child.stdin.take().ok_or("No stdin")?;
        let stdout = child.stdout.take().ok_or("No stdout")?;
        // WICHTIG: stderr muss kontinuierlich gelesen werden, sonst füllt sich der
        // Pipe-Buffer (typisch 64KB) und Python blockiert auf write(sys.stderr).
        // Wenn Python in einem Handler viel loggt oder eine Exception-Traceback
        // produziert, friert der Prozess sonst ein — stdin-write geht durch, aber
        // der Response kommt nie aus stdout raus. Background-Task liest stderr
        // zeilenweise und loggt via tracing::debug. Gemini-Finding (run 4).
        if let Some(stderr) = child.stderr.take() {
            let name = module_path.file_stem().and_then(|s| s.to_str())
                .unwrap_or("py").to_string();
            tokio::spawn(async move {
                use tokio::io::AsyncBufReadExt;
                let mut reader = BufReader::new(stderr).lines();
                while let Ok(Some(line)) = reader.next_line().await {
                    if !line.is_empty() {
                        tracing::debug!("[py:{}] {}", name, line);
                    }
                }
            });
        }
        Ok(PyProcess {
            stdin,
            stdout: BufReader::new(stdout),
            child,
            last_used: std::time::Instant::now(),
            module_path: module_path.to_path_buf(),
        })
    }

    /// Call a Python tool. Per-module mutex: other modules run concurrently.
    pub async fn call(
        &self,
        module_path: &Path,
        module_name: &str,
        tool_name: &str,
        params: &[String],
        config: &serde_json::Value,
    ) -> Result<(bool, String), String> {
        let slot = {
            let mut reg = self.registry.lock().await;
            reg.entry(module_name.to_string())
                .or_insert_with(|| Arc::new(Mutex::new(None)))
                .clone()
        };

        let mut guard = slot.lock().await;

        // Check if alive
        let needs_spawn = match guard.as_mut() {
            None => true,
            Some(proc) => matches!(proc.child.try_wait(), Ok(Some(_))),
        };
        if needs_spawn {
            *guard = Some(Self::spawn_process(module_path)?);
        }

        let proc = guard.as_mut().ok_or("process slot empty after spawn")?;
        proc.last_used = std::time::Instant::now();

        let request = serde_json::json!({
            "action": "handle_tool",
            "tool": tool_name,
            "params": params,
            "config": config,
        });
        let request_str = serde_json::to_string(&request)
            .map_err(|e| format!("serialize request: {e}"))? + "\n";

        if let Err(e) = proc.stdin.write_all(request_str.as_bytes()).await {
            *guard = None;
            return Err(format!("stdin write: {e}"));
        }
        if let Err(e) = proc.stdin.flush().await {
            *guard = None;
            return Err(format!("stdin flush: {e}"));
        }

        // Wir lesen mehrere Zeilen und überspringen alles was kein valid-JSON-
        // Objekt ist. Python-Module die `print("loading...")` während Init
        // machen, pollen sonst den ersten read_line und unser Pool-Eintrag
        // wäre für immer kaputt (Gemini-Finding Round-SQLite-1). Max 50
        // Non-JSON-Lines, danach Abbruch — das verhindert Infinite-Skipping
        // wenn das Modul nur Müll schickt.
        let result: serde_json::Value = {
            let mut parsed: Option<serde_json::Value> = None;
            let mut skipped = 0usize;
            loop {
                if skipped > 50 {
                    *guard = None;
                    return Err("Python IPC: >50 non-JSON lines, Modul spricht nicht das stdin/stdout-Protokoll".to_string());
                }
                let mut line = String::new();
                let res = tokio::time::timeout(
                    std::time::Duration::from_secs(30),
                    proc.stdout.read_line(&mut line),
                ).await;
                match res {
                    Ok(Ok(0)) => { *guard = None; return Err("Python process died".to_string()); }
                    Ok(Err(e)) => { *guard = None; return Err(format!("read_line: {e}")); }
                    Err(_) => { *guard = None; return Err("Python module timeout (30s)".to_string()); }
                    Ok(Ok(_)) => {}
                }
                let trimmed = line.trim();
                if trimmed.is_empty() { skipped += 1; continue; }
                // Nur Zeilen die wie JSON-Objekte aussehen parsen
                if !trimmed.starts_with('{') {
                    skipped += 1;
                    tracing::debug!("[py:{}] skipping non-JSON stdout: {}", module_name, crate::util::safe_truncate(trimmed, 120));
                    continue;
                }
                match serde_json::from_str::<serde_json::Value>(trimmed) {
                    Ok(v) => { parsed = Some(v); break; }
                    Err(_) => {
                        skipped += 1;
                        tracing::debug!("[py:{}] non-parseable stdout-line: {}", module_name, crate::util::safe_truncate(trimmed, 120));
                        continue;
                    }
                }
            }
            parsed.unwrap()  // loop above garantiert Some oder return Err
        };

        if let Some(err) = result.get("error").and_then(|v| v.as_str()) {
            return Err(format!("Python error: {err}"));
        }

        let success = result["success"].as_bool().unwrap_or(false);
        let data = result["data"].as_str().unwrap_or("").to_string();
        Ok((success, data))
    }

    /// Clean up idle processes
    pub async fn cleanup_idle(&self) {
        let now = std::time::Instant::now();
        let mut reg = self.registry.lock().await;
        let mut to_remove = vec![];
        for (name, slot) in reg.iter() {
            if let Ok(guard) = slot.try_lock() {
                match guard.as_ref() {
                    None => to_remove.push(name.clone()),
                    Some(proc) => {
                        let idle = now.duration_since(proc.last_used).as_secs();
                        if idle > self.max_idle_secs {
                            to_remove.push(name.clone());
                        }
                    }
                }
            }
        }
        for name in to_remove {
            reg.remove(&name);
            tracing::info!("Python pool: removed idle/dead process '{}'", name);
        }
    }
}

/// Entdeckt alle Python-Module im modules/ Verzeichnis
pub fn discover_modules(modules_dir: &Path) -> Vec<PyModuleMeta> {
    let mut modules = vec![];

    let entries = match std::fs::read_dir(modules_dir) {
        Ok(e) => e,
        Err(_) => return modules,
    };

    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() { continue; }

        let module_py = path.join("module.py");
        if !module_py.exists() { continue; }

        // Module-Metadaten per "describe" Action holen
        match describe_module_sync(&module_py) {
            Ok(mut meta) => {
                meta.path = module_py;
                tracing::info!("Python-Modul entdeckt: {} v{} ({} tools)", meta.name, meta.version, meta.tools.len());
                modules.push(meta);
            }
            Err(e) => {
                tracing::warn!("Python-Modul in {:?} fehlerhaft: {}", path, e);
            }
        }
    }

    modules
}

/// Holt MODULE-Metadaten synchron per subprocess
fn describe_module_sync(module_py: &Path) -> Result<PyModuleMeta, String> {
    let output = std::process::Command::new("python3")
        .arg(module_py)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .and_then(|mut child| {
            if let Some(ref mut stdin) = child.stdin {
                use std::io::Write;
                stdin.write_all(b"{\"action\":\"describe\"}\n").ok();
            }
            child.wait_with_output()
        })
        .map_err(|e| format!("Python starten fehlgeschlagen: {}", e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("Python Fehler: {}", stderr));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let first_line = stdout.lines().next().unwrap_or("");
    serde_json::from_str::<PyModuleMeta>(first_line)
        .map_err(|e| format!("JSON parse fehlgeschlagen: {} — Output: {}", e, first_line))
}

/// Fuehrt einen Tool-Call in einem Python-Modul aus (async)
pub async fn call_python_tool(
    module_py: &Path,
    tool_name: &str,
    params: &[String],
    config: &serde_json::Value,
) -> Result<(bool, String), String> {
    let request = serde_json::json!({
        "action": "handle_tool",
        "tool": tool_name,
        "params": params,
        "config": config,
    });

    let request_str = serde_json::to_string(&request)
        .map_err(|e| format!("serialize request: {e}"))? + "\n";

    let mut child = tokio::process::Command::new("python3")
        .arg(module_py)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("Python starten fehlgeschlagen: {}", e))?;

    // Request schreiben
    if let Some(ref mut stdin) = child.stdin {
        stdin.write_all(request_str.as_bytes()).await
            .map_err(|e| format!("stdin write: {}", e))?;
        stdin.shutdown().await.ok(); // EOF senden
    }

    // Timeout: 30 Sekunden
    let output = match tokio::time::timeout(
        std::time::Duration::from_secs(30),
        child.wait_with_output()
    ).await {
        Ok(result) => result.map_err(|e| format!("Python Prozess Fehler: {}", e))?,
        Err(_) => {
            // child wird hier gedroppt → kill_on_drop killt den Prozess
            return Err("Python-Modul Timeout (30s) — Prozess gekillt".to_string());
        }
    };

    let stdout = String::from_utf8_lossy(&output.stdout);
    let first_line = stdout.lines().next().unwrap_or("{}");

    let result: serde_json::Value = serde_json::from_str(first_line)
        .map_err(|e| format!("Python Response parse: {} — Raw: {}", e, first_line))?;

    if let Some(err) = result.get("error").and_then(|v| v.as_str()) {
        return Err(format!("Python-Modul Fehler: {}", err));
    }

    let success = result["success"].as_bool().unwrap_or(false);
    let data = result["data"].as_str().unwrap_or("").to_string();

    Ok((success, data))
}
