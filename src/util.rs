// src/util.rs — Shared utilities used across modules

use crate::types::{AgentConfig, ModulConfig, ModulIdentity};
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
    let backend_identity = config.llm_backends.iter()
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

        let handles: Vec<_> = (0..20).map(|i| {
            let p = p.clone();
            let content: Vec<u8> = format!("content-from-writer-{:03}", i).into_bytes();
            std::thread::spawn(move || {
                atomic_write(&p, &content).unwrap();
            })
        }).collect();
        for h in handles { h.join().unwrap(); }

        // Nach allen Threads: genau eine Datei, genau ein vollständiger Inhalt
        // (keine abgeschnittene oder leere Datei).
        let contents = std::fs::read(&path).unwrap();
        assert!(contents.starts_with(b"content-from-writer-"),
                "datei muss vollständigen content eines writers haben, nicht fragment: {:?}", contents);
        // Keine .tmp.* Leichen im Verzeichnis
        let files: Vec<_> = std::fs::read_dir(dir.path()).unwrap().flatten().collect();
        let tmp_count = files.iter().filter(|f| {
            f.file_name().to_string_lossy().contains(".tmp.")
        }).count();
        assert_eq!(tmp_count, 0, "keine .tmp.* Leichen erwartet");
    }
}
