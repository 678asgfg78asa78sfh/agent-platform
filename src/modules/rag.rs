use crate::tools::ToolResult;
use crate::security::safe_id;
use std::path::Path;
use std::collections::HashMap;
use std::sync::OnceLock;
use std::time::{Instant, SystemTime};
use tokio::sync::RwLock;
use chrono::Utc;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
struct RagEntry {
    id: String,
    text: String,
    timestamp: String,
    keywords: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    embedding: Option<Vec<f32>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    embedding_model: Option<String>,
}

fn rag_dir(base: &Path, pool: &str) -> std::path::PathBuf {
    let safe_pool = safe_id(pool).unwrap_or_else(|| "shared".to_string());
    let dir = base.join("rag").join(&safe_pool);
    std::fs::create_dir_all(&dir).ok();
    dir
}

/// Common German + English stopwords. Filtered before indexing so "der", "ist", "und"
/// don't match every entry.
const STOPWORDS: &[&str] = &[
    // German
    "der", "die", "das", "und", "oder", "aber", "ist", "war", "sind", "waren",
    "ein", "eine", "einer", "eines", "einem", "einen", "den", "dem", "des",
    "mit", "von", "bei", "nach", "vor", "über", "unter", "zwischen", "durch",
    "ich", "du", "er", "sie", "es", "wir", "ihr", "sie", "mich", "dich", "ihn",
    "mir", "dir", "ihm", "uns", "euch", "ihnen",
    "nicht", "kein", "keine", "keiner", "auch", "noch", "schon", "nur", "auf",
    "was", "wer", "wo", "wie", "wann", "warum",
    // English
    "the", "and", "or", "but", "is", "was", "are", "were", "be", "been", "being",
    "a", "an", "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
    "not", "no", "yes", "also", "only", "just", "so", "as", "that", "this",
    "what", "who", "where", "how", "when", "why",
];

fn is_stopword(word: &str) -> bool {
    STOPWORDS.contains(&word)
}

fn extract_keywords(text: &str) -> Vec<String> {
    text.split_whitespace()
        .map(|w| w.to_lowercase().trim_matches(|c: char| !c.is_alphanumeric()).to_string())
        .filter(|w| w.len() > 2 && !is_stopword(w))
        .collect()
}

fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() || a.is_empty() { return 0.0; }
    let dot: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
    let norm_a: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
    let norm_b: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm_a == 0.0 || norm_b == 0.0 { return 0.0; }
    dot / (norm_a * norm_b)
}

struct CachedIndex {
    entries: Vec<RagEntry>,
    loaded_at: Instant,
    dir_mtime: Option<SystemTime>,
}

fn cache() -> &'static RwLock<HashMap<String, CachedIndex>> {
    static CACHE: OnceLock<RwLock<HashMap<String, CachedIndex>>> = OnceLock::new();
    CACHE.get_or_init(|| RwLock::new(HashMap::new()))
}

fn invalidate_cache(pool: &str) {
    // Best-effort sync wipe using try_write
    if let Ok(mut c) = cache().try_write() {
        c.remove(pool);
    }
}

fn dir_mtime(dir: &Path) -> Option<SystemTime> {
    std::fs::metadata(dir).ok().and_then(|m| m.modified().ok())
}

async fn load_all_entries(base: &Path, pool: &str) -> Vec<RagEntry> {
    let dir = rag_dir(base, pool);
    let current_mtime = dir_mtime(&dir);

    // Cached path
    {
        let c = cache().read().await;
        if let Some(cached) = c.get(pool) {
            // Invalidate if dir mtime changed or cache older than 60s
            if cached.dir_mtime == current_mtime
                && cached.loaded_at.elapsed().as_secs() < 60
            {
                return cached.entries.clone();
            }
        }
    }

    // Load from disk
    let dir_owned = dir.clone();
    let entries: Vec<RagEntry> = tokio::task::spawn_blocking(move || {
        let mut entries = vec![];
        if let Ok(files) = std::fs::read_dir(&dir_owned) {
            for file in files.flatten() {
                if file.path().extension().is_some_and(|e| e == "json") {
                    if let Ok(content) = std::fs::read_to_string(file.path()) {
                        if let Ok(entry) = serde_json::from_str::<RagEntry>(&content) {
                            entries.push(entry);
                        }
                    }
                }
            }
        }
        entries
    }).await.unwrap_or_default();

    let mut c = cache().write().await;
    c.insert(pool.to_string(), CachedIndex {
        entries: entries.clone(),
        loaded_at: Instant::now(),
        dir_mtime: current_mtime,
    });
    entries
}

/// Store text in RAG pool. If embedding is provided, store it alongside.
pub async fn speichern(base: &Path, pool: &str, text: &str, embedding: Option<Vec<f32>>, embed_model: Option<String>) -> ToolResult {
    if text.trim().is_empty() {
        return ToolResult::fail("Kein Text zum Speichern angegeben".into());
    }

    let dir = rag_dir(base, pool);
    let id = uuid::Uuid::new_v4().to_string();
    let entry = RagEntry {
        id: id.clone(),
        text: text.to_string(),
        timestamp: Utc::now().to_rfc3339(),
        keywords: extract_keywords(text),
        embedding,
        embedding_model: embed_model,
    };

    let path = dir.join(format!("{}.json", id));
    let json = match serde_json::to_string_pretty(&entry) {
        Ok(j) => j,
        Err(e) => return ToolResult::fail(format!("RAG serialisieren fehlgeschlagen: {}", e)),
    };
    let write_result = tokio::task::spawn_blocking(move || std::fs::write(&path, json))
        .await
        .unwrap_or_else(|e| Err(std::io::Error::other(e.to_string())));
    match write_result {
        Ok(_) => {
            invalidate_cache(pool);
            ToolResult::ok(format!("Im RAG Pool '{}' gespeichert (id: {})", pool, &id[..8]))
        }
        Err(e) => ToolResult::fail(format!("RAG speichern fehlgeschlagen: {}", e)),
    }
}

/// Search RAG pool. Vector search first (if query_embedding provided), keyword fallback.
pub async fn suchen(base: &Path, pool: &str, query: &str, query_embedding: Option<&[f32]>) -> ToolResult {
    if query.trim().is_empty() {
        return ToolResult::fail("Keine Suchanfrage angegeben".into());
    }

    let entries = load_all_entries(base, pool).await;

    // Vector search if embedding available
    if let Some(qvec) = query_embedding {
        let mut results: Vec<(f32, &RagEntry)> = entries.iter()
            .filter_map(|entry| {
                entry.embedding.as_ref().map(|evec| {
                    (cosine_similarity(qvec, evec), entry)
                })
            })
            .filter(|(score, _)| *score > 0.3)
            .collect();

        results.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

        if !results.is_empty() {
            let top: Vec<String> = results.iter().take(5).map(|(score, entry)| {
                format!("[{:.0}% match] {}", score * 100.0, entry.text)
            }).collect();
            return ToolResult::ok(format!("RAG Ergebnisse ({} gefunden, vector search):\n{}", results.len(), top.join("\n\n")));
        }
    }

    // Keyword fallback
    let query_keywords = extract_keywords(query);
    let mut results: Vec<(f32, &RagEntry)> = vec![];

    for entry in &entries {
        let matches = query_keywords.iter()
            .filter(|qk| {
                entry.keywords.iter().any(|rk| rk.contains(qk.as_str()))
                || entry.text.to_lowercase().contains(qk.as_str())
            })
            .count();
        if matches > 0 {
            let score = matches as f32 / query_keywords.len().max(1) as f32;
            results.push((score, entry));
        }
    }

    results.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

    if results.is_empty() {
        ToolResult::ok(format!("Keine Ergebnisse im RAG Pool '{}' fuer: {}", pool, query))
    } else {
        let top: Vec<String> = results.iter().take(5).map(|(score, entry)| {
            format!("[{:.0}% match] {}", score * 100.0, entry.text)
        }).collect();
        ToolResult::ok(format!("RAG Ergebnisse ({} gefunden, keyword search):\n{}", results.len(), top.join("\n\n")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cosine_identical() {
        let a = vec![1.0, 0.0, 0.0];
        assert!((cosine_similarity(&a, &a) - 1.0).abs() < 0.001);
    }

    #[test]
    fn test_cosine_orthogonal() {
        let a = vec![1.0, 0.0, 0.0];
        let b = vec![0.0, 1.0, 0.0];
        assert!(cosine_similarity(&a, &b).abs() < 0.001);
    }

    #[test]
    fn test_cosine_opposite() {
        let a = vec![1.0, 0.0];
        let b = vec![-1.0, 0.0];
        assert!((cosine_similarity(&a, &b) - (-1.0)).abs() < 0.001);
    }

    #[test]
    fn test_cosine_empty() {
        assert_eq!(cosine_similarity(&[], &[]), 0.0);
    }

    #[test]
    fn test_cosine_length_mismatch() {
        assert_eq!(cosine_similarity(&[1.0], &[1.0, 2.0]), 0.0);
    }

    #[test]
    fn test_extract_keywords_basic() {
        let kw = extract_keywords("Hello World Rust programming");
        assert!(kw.contains(&"hello".to_string()));
        assert!(kw.contains(&"world".to_string()));
        assert!(kw.contains(&"rust".to_string()));
    }

    #[test]
    fn test_extract_keywords_filters_short() {
        let kw = extract_keywords("I am a ok");
        // "I", "am", "a", "ok" are all <= 2 chars
        assert!(kw.is_empty() || kw.iter().all(|w| w.len() > 2));
    }
}
