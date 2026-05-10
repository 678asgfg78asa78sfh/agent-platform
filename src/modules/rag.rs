use crate::security::safe_id;
use crate::tools::ToolResult;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::Path;
use std::sync::OnceLock;
use std::time::{Instant, SystemTime};
use tokio::sync::RwLock;

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
    "der", "die", "das", "und", "oder", "aber", "ist", "war", "sind", "waren", "ein", "eine",
    "einer", "eines", "einem", "einen", "den", "dem", "des", "mit", "von", "bei", "nach", "vor",
    "über", "unter", "zwischen", "durch", "ich", "du", "er", "sie", "es", "wir", "ihr", "sie",
    "mich", "dich", "ihn", "mir", "dir", "ihm", "uns", "euch", "ihnen", "nicht", "kein", "keine",
    "keiner", "auch", "noch", "schon", "nur", "auf", "was", "wer", "wo", "wie", "wann", "warum",
    // English
    "the", "and", "or", "but", "is", "was", "are", "were", "be", "been", "being", "a", "an", "in",
    "on", "at", "to", "for", "of", "with", "by", "from", "i", "you", "he", "she", "it", "we",
    "they", "me", "him", "her", "us", "them", "not", "no", "yes", "also", "only", "just", "so",
    "as", "that", "this", "what", "who", "where", "how", "when", "why",
];

fn is_stopword(word: &str) -> bool {
    STOPWORDS.contains(&word)
}

fn extract_keywords(text: &str) -> Vec<String> {
    text.split_whitespace()
        .map(|w| {
            w.to_lowercase()
                .trim_matches(|c: char| !c.is_alphanumeric())
                .to_string()
        })
        .filter(|w| w.len() > 2 && !is_stopword(w))
        .collect()
}

fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let dot: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
    let norm_a: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
    let norm_b: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm_a == 0.0 || norm_b == 0.0 {
        return 0.0;
    }
    dot / (norm_a * norm_b)
}

fn compact_rag_text(entry: &RagEntry) -> String {
    if entry.text.starts_with("DEEPDIVE_CRAWL_MANIFEST") {
        return compact_deepdive_manifest(entry);
    }
    if entry.text.starts_with("DEEPDIVE_CRAWL_NOTE") || entry.text.starts_with("DEEPDIVE_RAG_NOTE")
    {
        return compact_deepdive_text(entry);
    }
    if entry.text.starts_with("RSS_NEWS_NOTE") {
        return compact_rss_news_text(entry);
    }
    crate::util::safe_truncate(&entry.text, 1800).to_string()
}

fn compact_deepdive_manifest(entry: &RagEntry) -> String {
    let mut out = vec![format!(
        "rag_id: {}\nstored_at_utc: {}{}",
        entry.id,
        entry.timestamp,
        age_suffix(&entry.timestamp)
    )];
    for key in [
        "crawl_id",
        "crawl_started_at_utc",
        "captured_at_utc",
        "topic",
        "sources_fetched",
        "failed_count",
        "search_error_count",
    ] {
        if let Some(v) = line_value(&entry.text, key) {
            out.push(format!("{}: {}", key, v));
        }
    }
    if let Some(sources) = section_after(&entry.text, "sources:") {
        let mut sources = sources;
        for marker in [
            "\nfailures:",
            "\nsearch_errors:",
            "\ntool_trace:",
            "\ntrace:",
        ] {
            sources = sources.split(marker).next().unwrap_or(sources);
        }
        let sources = sources.trim();
        if !sources.is_empty() {
            out.push(format!(
                "sources:\n{}",
                crate::util::safe_truncate(sources, 1400)
            ));
            out.push(format!(
                "<quellen>\n{}\n</quellen>",
                crate::util::safe_truncate(sources, 2400)
            ));
        }
    }
    if let Some(tool_trace) = section_after(&entry.text, "tool_trace:") {
        let tool_trace = tool_trace
            .split("\ntrace:")
            .next()
            .unwrap_or(tool_trace)
            .trim();
        if !tool_trace.is_empty() {
            out.push(format!(
                "tool_trace_excerpt:\n{}",
                crate::util::safe_truncate(tool_trace, 1400)
            ));
        }
    }
    if let Some(trace) = section_after(&entry.text, "trace:") {
        out.push(format!(
            "trace_excerpt:\n{}",
            crate::util::safe_truncate(trace, 1400)
        ));
    }
    out.join("\n")
}

fn compact_deepdive_text(entry: &RagEntry) -> String {
    let mut out = vec![format!(
        "rag_id: {}\nstored_at_utc: {}{}",
        entry.id,
        entry.timestamp,
        age_suffix(&entry.timestamp)
    )];
    for key in [
        "crawl_id",
        "captured_at_utc",
        "source_last_seen_utc",
        "topic",
        "source_url",
        "source_title",
        "source_depth",
        "page_role",
        "discovery_method",
        "discovery_reason",
        "parent_url",
        "source_type",
        "source_reliability",
        "relevance_score",
        "recency_label",
        "author",
        "publisher",
        "date_hints",
        "search_snippet",
    ] {
        if let Some(v) = line_value(&entry.text, key) {
            out.push(format!("{}: {}", key, v));
        }
    }

    out.push(deepdive_source_tag(entry));

    if let Some(passages) = section_after(&entry.text, "key_passages:") {
        let passages = passages
            .split("\nassessment_required:")
            .next()
            .unwrap_or(passages)
            .split("\ncausality_hints:")
            .next()
            .unwrap_or(passages)
            .trim();
        if !passages.is_empty() {
            out.push(format!(
                "key_passages:\n{}",
                crate::util::safe_truncate(passages, 900)
            ));
        }
    }

    if let Some(hints) = section_after(&entry.text, "causality_hints:") {
        let hints = hints
            .split("\nassessment_required:")
            .next()
            .unwrap_or(hints)
            .trim();
        if !hints.is_empty() {
            out.push(format!(
                "causality_hints:\n{}",
                crate::util::safe_truncate(hints, 900)
            ));
        }
    }

    let excerpt = if let Some(text) = section_after(&entry.text, "source_text:") {
        text
    } else if let Some(text) = section_after(&entry.text, "source_material:") {
        text
    } else {
        entry.text.as_str()
    };
    out.push(format!(
        "content_excerpt:\n{}",
        crate::util::safe_truncate(&collapse_ws(excerpt), 1400)
    ));
    out.join("\n")
}

fn compact_rss_news_text(entry: &RagEntry) -> String {
    let mut out = vec![format!(
        "rag_id: {}\nstored_at_utc: {}{}",
        entry.id,
        entry.timestamp,
        age_suffix(&entry.timestamp)
    )];
    for key in [
        "captured_at_utc",
        "source_last_seen_utc",
        "topic",
        "rss_item_id",
        "rss_source_id",
        "source_url",
        "source_title",
        "source_feed_url",
        "source_name",
        "source_type",
        "source_category",
        "source_language",
        "source_reliability",
        "source_alignment",
        "source_reach",
        "published_at_utc",
        "fetched_at_utc",
        "recency_label",
        "rss_score",
        "deepdive_next_step",
    ] {
        if let Some(v) = line_value(&entry.text, key) {
            out.push(format!("{}: {}", key, v));
        }
    }

    out.push(rss_source_tag(entry));

    if let Some(summary) = section_after(&entry.text, "source_summary:") {
        let summary = summary
            .split("\nassessment_required:")
            .next()
            .unwrap_or(summary)
            .trim();
        if !summary.is_empty() {
            out.push(format!(
                "summary_excerpt:\n{}",
                crate::util::safe_truncate(&collapse_ws(summary), 1000)
            ));
        }
    }
    out.join("\n")
}

fn rss_source_tag(entry: &RagEntry) -> String {
    let url = line_value(&entry.text, "source_url").unwrap_or_else(|| "(kein URL-Fundort)".into());
    let title = line_value(&entry.text, "source_title").unwrap_or_else(|| "(kein Titel)".into());
    let captured = line_value(&entry.text, "captured_at_utc").unwrap_or_default();
    let published = line_value(&entry.text, "published_at_utc").unwrap_or_default();
    let source = line_value(&entry.text, "source_name").unwrap_or_default();
    let reliability = line_value(&entry.text, "source_reliability").unwrap_or_default();
    let alignment = line_value(&entry.text, "source_alignment").unwrap_or_default();
    format!(
        "<quellen>\n- rag_id: {}\n  fundort: {}\n  titel: {}\n  quelle: {}\n  veroeffentlicht_utc: {}\n  abgerufen_utc: {}\n  serioesitaet: {}\n  ausrichtung: {}\n</quellen>",
        entry.id, url, title, source, published, captured, reliability, alignment
    )
}

fn deepdive_source_tag(entry: &RagEntry) -> String {
    let url = line_value(&entry.text, "source_url").unwrap_or_else(|| "(kein URL-Fundort)".into());
    let title = line_value(&entry.text, "source_title").unwrap_or_else(|| "(kein Titel)".into());
    let captured = line_value(&entry.text, "captured_at_utc").unwrap_or_default();
    let seen = line_value(&entry.text, "source_last_seen_utc").unwrap_or_default();
    let dates = line_value(&entry.text, "date_hints").unwrap_or_default();
    let crawl_id = line_value(&entry.text, "crawl_id").unwrap_or_default();
    format!(
        "<quellen>\n- rag_id: {}\n  crawl_id: {}\n  fundort: {}\n  titel: {}\n  abgerufen_utc: {}\n  source_last_seen_utc: {}\n  datumshinweise: {}\n</quellen>",
        entry.id, crawl_id, url, title, captured, seen, dates
    )
}

fn age_suffix(ts: &str) -> String {
    DateTime::parse_from_rfc3339(ts)
        .ok()
        .map(|dt| {
            let age = Utc::now().signed_duration_since(dt.with_timezone(&Utc));
            if age.num_minutes() < 120 {
                format!(" (age: {} min)", age.num_minutes().max(0))
            } else if age.num_hours() < 72 {
                format!(" (age: {} h)", age.num_hours())
            } else {
                format!(" (age: {} d)", age.num_days())
            }
        })
        .unwrap_or_default()
}

fn line_value(text: &str, key: &str) -> Option<String> {
    let prefix = format!("{}:", key);
    text.lines()
        .find_map(|line| line.strip_prefix(&prefix).map(|v| v.trim().to_string()))
        .filter(|v| !v.is_empty())
}

fn section_after<'a>(text: &'a str, marker: &str) -> Option<&'a str> {
    let start = text.find(marker)? + marker.len();
    let rest = text[start..].trim();
    if marker == "source_text:" {
        if let Some(end) = rest.find("\nsource_links:") {
            return Some(rest[..end].trim());
        }
    }
    Some(rest)
}

fn collapse_ws(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn crawl_id_from_query(query: &str) -> Option<String> {
    query
        .split_whitespace()
        .find(|part| {
            part.starts_with("dd-")
                && part.len() <= 64
                && part
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        })
        .map(|s| s.trim_matches(|c: char| c == ',' || c == ';').to_string())
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
            if cached.dir_mtime == current_mtime && cached.loaded_at.elapsed().as_secs() < 60 {
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
    })
    .await
    .unwrap_or_default();

    let mut c = cache().write().await;
    c.insert(
        pool.to_string(),
        CachedIndex {
            entries: entries.clone(),
            loaded_at: Instant::now(),
            dir_mtime: current_mtime,
        },
    );
    entries
}

/// Store text in RAG pool. If embedding is provided, store it alongside.
pub async fn speichern(
    base: &Path,
    pool: &str,
    text: &str,
    embedding: Option<Vec<f32>>,
    embed_model: Option<String>,
) -> ToolResult {
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
            ToolResult::ok(format!(
                "Im RAG Pool '{}' gespeichert (id: {})",
                pool,
                &id[..8]
            ))
        }
        Err(e) => ToolResult::fail(format!("RAG speichern fehlgeschlagen: {}", e)),
    }
}

/// Search RAG pool. Vector search first (if query_embedding provided), keyword fallback.
pub async fn suchen(
    base: &Path,
    pool: &str,
    query: &str,
    query_embedding: Option<&[f32]>,
) -> ToolResult {
    if query.trim().is_empty() {
        return ToolResult::fail("Keine Suchanfrage angegeben".into());
    }

    let loaded_entries = load_all_entries(base, pool).await;
    let entries: Vec<RagEntry> = if let Some(crawl_id) = crawl_id_from_query(query) {
        loaded_entries
            .into_iter()
            .filter(|entry| entry.text.contains(&crawl_id))
            .collect()
    } else {
        loaded_entries
    };
    let query_keywords = extract_keywords(query);

    // Vector search if embedding available
    if let Some(qvec) = query_embedding {
        let mut results: Vec<(f32, &RagEntry)> = entries
            .iter()
            .filter_map(|entry| {
                entry
                    .embedding
                    .as_ref()
                    .map(|evec| (cosine_similarity(qvec, evec), entry))
            })
            .filter(|(score, _)| *score > 0.3)
            .collect();

        // Python modules such as deepdive/rss write RAG files directly and do
        // not have precomputed embeddings. Keep keyword hits in the result set
        // so fresh source notes remain visible when vector search is enabled.
        for (score, entry) in keyword_ranked_entries(&entries, &query_keywords) {
            if !results.iter().any(|(_, existing)| existing.id == entry.id) {
                results.push((score, entry));
            }
        }

        results.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

        if !results.is_empty() {
            let top: Vec<String> = results
                .iter()
                .take(5)
                .map(|(score, entry)| {
                    format!(
                        "[{:.0}% match]\n{}",
                        display_score(*score),
                        compact_rag_text(entry)
                    )
                })
                .collect();
            return ToolResult::ok(format!(
                "RAG Ergebnisse ({} gefunden, hybrid search):\n{}",
                results.len(),
                top.join("\n\n")
            ));
        }
    }

    // Keyword fallback
    let mut results = keyword_ranked_entries(&entries, &query_keywords);
    results.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

    if results.is_empty() {
        ToolResult::ok(format!(
            "Keine Ergebnisse im RAG Pool '{}' fuer: {}",
            pool, query
        ))
    } else {
        let top: Vec<String> = results
            .iter()
            .take(5)
            .map(|(score, entry)| {
                format!(
                    "[{:.0}% match]\n{}",
                    display_score(*score),
                    compact_rag_text(entry)
                )
            })
            .collect();
        ToolResult::ok(format!(
            "RAG Ergebnisse ({} gefunden, keyword search):\n{}",
            results.len(),
            top.join("\n\n")
        ))
    }
}

fn keyword_ranked_entries<'a>(
    entries: &'a [RagEntry],
    query_keywords: &[String],
) -> Vec<(f32, &'a RagEntry)> {
    let mut results: Vec<(f32, &RagEntry)> = vec![];
    for entry in entries {
        let matches = query_keywords
            .iter()
            .filter(|qk| {
                entry.keywords.iter().any(|rk| rk.contains(qk.as_str()))
                    || entry.text.to_lowercase().contains(qk.as_str())
            })
            .count();
        if matches > 0 {
            let mut score = matches as f32 / query_keywords.len().max(1) as f32;
            if entry.text.starts_with("DEEPDIVE_CRAWL_MANIFEST") {
                score += 0.25;
            }
            if entry.text.starts_with("RSS_NEWS_NOTE") {
                score += 0.15;
            }
            results.push((score, entry));
        }
    }
    results
}

fn display_score(score: f32) -> f32 {
    (score * 100.0).clamp(0.0, 100.0)
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

    #[test]
    fn test_crawl_id_from_query() {
        assert_eq!(
            crawl_id_from_query("dd-20260507T011624Z-5f8430b0 Friedrich Merz").as_deref(),
            Some("dd-20260507T011624Z-5f8430b0")
        );
        assert!(crawl_id_from_query("Friedrich Merz").is_none());
    }

    #[test]
    fn test_compact_rss_news_text_keeps_source_assessment() {
        let entry = RagEntry {
            id: "rss-rag-1".into(),
            timestamp: "2026-05-09T10:00:00Z".into(),
            keywords: vec![],
            embedding: None,
            embedding_model: None,
            text: [
                "RSS_NEWS_NOTE",
                "captured_at_utc: 2026-05-09T10:00:00Z",
                "source_url: https://example.test/news/1",
                "source_title: Energie Politik Beschluss",
                "source_name: Example News",
                "source_reliability: hoch",
                "source_alignment: neutral",
                "published_at_utc: 2026-05-09T09:00:00Z",
                "source_summary:",
                "Die Regierung beschliesst neue Energie Regeln.",
            ]
            .join("\n"),
        };

        let compact = compact_rss_news_text(&entry);
        assert!(compact.contains("source_reliability: hoch"));
        assert!(compact.contains("source_alignment: neutral"));
        assert!(compact.contains("<quellen>"));
        assert!(compact.contains("summary_excerpt:"));
    }

    #[tokio::test]
    async fn test_hybrid_search_returns_rss_notes_without_embedding() {
        let tmp = tempfile::tempdir().unwrap();
        let pool = "hybrid-rss";
        let stored = speichern(
            tmp.path(),
            pool,
            "unrelated embedded note",
            Some(vec![1.0, 0.0]),
            Some("test-embed".into()),
        )
        .await;
        assert!(stored.success);

        let entry = RagEntry {
            id: "rss-direct".into(),
            timestamp: chrono::Utc::now().to_rfc3339(),
            keywords: extract_keywords("Energie Politik Example News"),
            embedding: None,
            embedding_model: None,
            text: [
                "RSS_NEWS_NOTE",
                "captured_at_utc: 2026-05-09T10:00:00Z",
                "source_url: https://example.test/news/1",
                "source_title: Energie Politik Beschluss",
                "source_name: Example News",
                "source_reliability: hoch",
                "source_alignment: neutral",
                "source_summary:",
                "Die Regierung beschliesst neue Energie Regeln.",
            ]
            .join("\n"),
        };
        let dir = rag_dir(tmp.path(), pool);
        let json = serde_json::to_string_pretty(&entry).unwrap();
        std::fs::write(dir.join("rss-direct.json"), json).unwrap();

        let result = suchen(tmp.path(), pool, "energie politik", Some(&[1.0, 0.0])).await;
        assert!(result.success);
        assert!(result.data.contains("hybrid search"));
        assert!(
            result
                .data
                .contains("source_title: Energie Politik Beschluss")
        );
    }
}
