use crate::tools::ToolResult;
use std::path::{Path, PathBuf};

/// Bereinigt LLM-generierte Pfade: trimmt Whitespace und entfernt umschließende Quotes.
/// WICHTIG: darf NICHT relative in absolute Pfade umwandeln — das war ein
/// Traversal-Bypass (Sicherheitsfix): `./home/user/.ssh/id_rsa` wurde zu
/// `/home/user/.ssh/id_rsa`, und die allowed-Whitelist wurde dann unter dem
/// transformierten absoluten Pfad geprüft. Jetzt bleibt relativ relativ; die
/// Canonicalization in is_path_allowed übernimmt die Auflösung unter dem
/// Home-Verzeichnis bzw. CWD.
fn clean_path(path: &str) -> String {
    path.trim().trim_matches('"').trim_matches('\'').to_string()
}

/// True if `path` liegt innerhalb eines der `allowed`-Verzeichnisse.
/// Nutzt `Path::starts_with` statt String-Prefix — das verhindert
/// `/safe/../etc` Bypass durch String-Matching und auch Präfix-Kollisionen
/// wie `/safe` matched `/safe_evil`. Canonicalization löst Symlinks auf.
fn is_path_allowed(path: &str, allowed: &[&str]) -> bool {
    if allowed.is_empty() { return false; }
    let clean = clean_path(path);
    let target: PathBuf = Path::new(&clean).to_path_buf();

    // Für neue Dateien existiert der Zielpfad noch nicht — dann kanonisieren wir das
    // parent-Verzeichnis und hängen den Dateinamen zurück an.
    let canonical = match std::fs::canonicalize(&target) {
        Ok(p) => p,
        Err(_) => {
            match (target.parent(), target.file_name()) {
                (Some(parent), Some(name)) => {
                    match std::fs::canonicalize(parent) {
                        Ok(p) => p.join(name),
                        Err(_) => return false,
                    }
                }
                _ => return false,
            }
        }
    };

    // Component-based starts_with: kein String-Präfix-Match, also kein "/safe"
    // matched "/safe_evil"-Bypass. Jeder allowed-Pfad wird kanonisiert, damit
    // Symlinks/Relative-Pfade korrekt verglichen werden.
    allowed.iter().any(|a| {
        let allowed_canonical = std::fs::canonicalize(a).unwrap_or_else(|_| PathBuf::from(a));
        canonical.starts_with(&allowed_canonical)
    })
}

pub async fn read_file(path: &str, allowed: &[&str], max_size: usize) -> ToolResult {
    let path = clean_path(path);
    if !is_path_allowed(&path, allowed) {
        return ToolResult::fail(format!("DENIED: Pfad '{}' ist nicht in der Whitelist", path));
    }
    let path_owned = path.clone();
    let read = tokio::task::spawn_blocking(move || std::fs::read_to_string(&path_owned))
        .await
        .unwrap_or_else(|e| Err(std::io::Error::other(e.to_string())));
    match read {
        Ok(content) => {
            if content.len() <= max_size { return ToolResult::ok(content); }
            let mut end = max_size;
            while end > 0 && !content.is_char_boundary(end) { end -= 1; }
            ToolResult::ok(format!("{}... [abgeschnitten, {} bytes total]", &content[..end], content.len()))
        }
        Err(e) => ToolResult::fail(format!("Datei lesen fehlgeschlagen: {}", e)),
    }
}

pub async fn write_file(path: &str, content: &str, allowed: &[&str], allow_write: bool) -> ToolResult {
    let path = clean_path(path);
    if !allow_write {
        return ToolResult::fail("DENIED: Schreibzugriff ist für dieses Modul deaktiviert".into());
    }
    if !is_path_allowed(&path, allowed) {
        return ToolResult::fail(format!("DENIED: Pfad '{}' ist nicht in der Whitelist", path));
    }
    let path_owned = path.clone();
    let content_owned = content.to_string();
    let content_len = content.len();
    let write = tokio::task::spawn_blocking(move || std::fs::write(&path_owned, &content_owned))
        .await
        .unwrap_or_else(|e| Err(std::io::Error::other(e.to_string())));
    match write {
        Ok(_) => ToolResult::ok(format!("Datei geschrieben: {} ({} bytes)", path, content_len)),
        Err(e) => ToolResult::fail(format!("Datei schreiben fehlgeschlagen: {}", e)),
    }
}

#[cfg(test)]
mod path_tests {
    use super::*;

    #[test]
    fn test_clean_path_no_absolute_expansion() {
        // Used to turn "./home/user" into "/home/user" — that was the bypass.
        assert_eq!(clean_path("./home/user/.ssh/id_rsa"), "./home/user/.ssh/id_rsa");
        assert_eq!(clean_path("./tmp/foo"), "./tmp/foo");
    }

    #[test]
    fn test_clean_path_strips_quotes_and_whitespace() {
        assert_eq!(clean_path("  \"/tmp/x\"  "), "/tmp/x");
        assert_eq!(clean_path("'  /home/a  '"), "  /home/a  ");  // only outer quotes
    }

    #[test]
    fn test_is_path_allowed_prefix_collision_blocked() {
        let dir = tempfile::tempdir().unwrap();
        let safe = dir.path().join("safe");
        let evil = dir.path().join("safe_evil");
        std::fs::create_dir(&safe).unwrap();
        std::fs::create_dir(&evil).unwrap();
        let file_in_evil = evil.join("file.txt");
        std::fs::write(&file_in_evil, b"x").unwrap();

        let safe_str = safe.to_string_lossy().to_string();
        let allowed = [safe_str.as_str()];
        // String-prefix would match /safe against /safe_evil — component-match must not.
        assert!(!is_path_allowed(&file_in_evil.to_string_lossy(), &allowed));
    }

    #[test]
    fn test_is_path_allowed_inside_permitted() {
        let dir = tempfile::tempdir().unwrap();
        let safe = dir.path().join("home");
        std::fs::create_dir(&safe).unwrap();
        let file_in_safe = safe.join("ok.txt");
        std::fs::write(&file_in_safe, b"x").unwrap();

        let safe_str = safe.to_string_lossy().to_string();
        let allowed = [safe_str.as_str()];
        assert!(is_path_allowed(&file_in_safe.to_string_lossy(), &allowed));
    }

    #[test]
    fn test_is_path_allowed_empty_list_denies() {
        assert!(!is_path_allowed("/tmp/x", &[]));
    }

    #[test]
    fn test_is_path_allowed_traversal_blocked() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        std::fs::create_dir(&home).unwrap();
        std::fs::write(home.join("a"), b"a").unwrap();

        // Existing file outside home
        let outside = dir.path().join("outside.txt");
        std::fs::write(&outside, b"x").unwrap();

        let home_str = home.to_string_lossy().to_string();
        let allowed = [home_str.as_str()];
        // Canonicalized outside path must not match home prefix
        assert!(!is_path_allowed(&outside.to_string_lossy(), &allowed));
    }
}

pub async fn list_dir(path: &str, allowed: &[&str]) -> ToolResult {
    let path = clean_path(path);
    if !is_path_allowed(&path, allowed) {
        return ToolResult::fail(format!("DENIED: Pfad '{}' ist nicht in der Whitelist", path));
    }
    let path_owned = path.clone();
    let result = tokio::task::spawn_blocking(move || -> std::io::Result<Vec<String>> {
        let entries = std::fs::read_dir(&path_owned)?;
        Ok(entries
            .filter_map(|e| e.ok())
            .map(|e| {
                let ft = if e.path().is_dir() { "DIR " } else { "FILE" };
                format!("{} {}", ft, e.path().display())
            })
            .collect())
    })
    .await
    .unwrap_or_else(|e| Err(std::io::Error::other(e.to_string())));
    match result {
        Ok(list) if list.is_empty() => ToolResult::ok("Verzeichnis ist leer".into()),
        Ok(list) => ToolResult::ok(list.join("\n")),
        Err(e) => ToolResult::fail(format!("Verzeichnis lesen fehlgeschlagen: {}", e)),
    }
}
