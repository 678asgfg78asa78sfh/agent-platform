// src/security.rs — Auth, path sanitization, SSRF protection, rate limiting,
// secret redaction. Central home for cross-cutting security concerns.

use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::RwLock;

use axum::{
    body::Body,
    extract::{ConnectInfo, State},
    http::{Request, StatusCode},
    middleware::Next,
    response::Response,
};

use crate::types::AgentConfig;

// ─── Path sanitization ────────────────────────────────

/// Reject IDs containing path traversal attempts. Returns sanitized id or None if dangerous.
pub fn safe_id(id: &str) -> Option<String> {
    if id.is_empty() || id.len() > 128 { return None; }
    if id.contains("..") || id.contains('/') || id.contains('\\') || id.contains('\0') {
        return None;
    }
    // Only allow alnum, dot, dash, underscore
    if !id.chars().all(|c| c.is_alphanumeric() || c == '.' || c == '-' || c == '_') {
        return None;
    }
    Some(id.to_string())
}

/// Validate a path segment used inside an already-scoped base directory.
/// Allows multi-segment relative paths (`sub/file.txt`) but blocks traversal.
pub fn safe_relative_path(p: &str) -> Option<String> {
    if p.is_empty() || p.len() > 1024 { return None; }
    if p.contains("..") || p.starts_with('/') || p.contains('\0') || p.contains('\\') {
        return None;
    }
    Some(p.to_string())
}

// ─── SSRF protection ──────────────────────────────────

/// Check if a URL resolves to a dangerous address (private, loopback, link-local, metadata).
/// Returns Err with reason if URL should be blocked.
pub fn validate_external_url(url: &str) -> Result<(), String> {
    if !url.starts_with("http://") && !url.starts_with("https://") {
        return Err(format!("nur http(s) erlaubt: {}", url));
    }

    let host = extract_host(url).ok_or_else(|| "keine Host-Komponente".to_string())?;

    if host.is_empty() {
        return Err("leerer Host".into());
    }
    let lower = host.to_lowercase();

    // Block obvious metadata hostnames
    let blocked_hosts = [
        "localhost", "localhost.localdomain", "metadata.google.internal",
        "metadata", "instance-data",
    ];
    if blocked_hosts.iter().any(|b| lower == *b) {
        return Err(format!("blockierter Host: {}", host));
    }

    // If it parses as IP, check ranges
    if let Ok(ip) = lower.parse::<IpAddr>() {
        if is_blocked_ip(&ip) {
            return Err(format!("blockierte IP: {}", ip));
        }
        return Ok(());
    }

    // For hostnames we do best-effort resolution check. Failure is non-fatal (DNS may be
    // unavailable in containers); reqwest will fail on connect anyway.
    if let Ok(addrs) = (host.as_str(), 80u16).to_socket_addrs_safe() {
        for addr in addrs {
            if is_blocked_ip(&addr) {
                return Err(format!("Host '{}' löst auf blockierte IP auf: {}", host, addr));
            }
        }
    }

    Ok(())
}

fn extract_host(url: &str) -> Option<String> {
    let rest = url.strip_prefix("http://").or_else(|| url.strip_prefix("https://"))?;
    let host_end = rest.find(|c: char| c == '/' || c == ':' || c == '?' || c == '#')
        .unwrap_or(rest.len());
    let host = &rest[..host_end];
    // Strip IPv6 brackets
    let host = host.trim_start_matches('[').trim_end_matches(']');
    if host.is_empty() { None } else { Some(host.to_string()) }
}

fn is_blocked_ip(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            let o = v4.octets();
            v4.is_loopback()
                || v4.is_private()
                || v4.is_link_local()
                || v4.is_broadcast()
                || v4.is_unspecified()
                || v4.is_multicast()
                // 169.254.169.254 is already link-local, but make explicit
                || (o[0] == 169 && o[1] == 254)
                // 100.64.0.0/10 CGNAT
                || (o[0] == 100 && (o[1] & 0xC0) == 64)
        }
        IpAddr::V6(v6) => {
            v6.is_loopback()
                || v6.is_unspecified()
                || v6.is_multicast()
                // fc00::/7 unique-local
                || (v6.segments()[0] & 0xfe00 == 0xfc00)
                // fe80::/10 link-local
                || (v6.segments()[0] & 0xffc0 == 0xfe80)
        }
    }
}

// Helper trait so we can call to_socket_addrs without borrowing issues
trait ToSocketAddrsSafe {
    fn to_socket_addrs_safe(self) -> std::io::Result<Vec<IpAddr>>;
}
impl ToSocketAddrsSafe for (&str, u16) {
    fn to_socket_addrs_safe(self) -> std::io::Result<Vec<IpAddr>> {
        use std::net::ToSocketAddrs;
        Ok(self.to_socket_addrs()?.map(|sa| sa.ip()).collect())
    }
}

// ─── Secret redaction ─────────────────────────────────

const REDACTED: &str = "***REDACTED***";

const SECRET_KEYS: &[&str] = &[
    "api_key", "password", "auth_token", "notify_token",
    "brave_api_key", "serper_api_key", "google_api_key", "grok_api_key",
    "api_auth_token",
];

/// Walk a JSON value and replace any field whose name is in SECRET_KEYS with REDACTED
/// (only if the original value is a non-empty string).
pub fn redact_secrets(value: &mut serde_json::Value) {
    match value {
        serde_json::Value::Object(map) => {
            for (k, v) in map.iter_mut() {
                if SECRET_KEYS.contains(&k.as_str()) {
                    if let serde_json::Value::String(s) = v {
                        if !s.is_empty() {
                            *v = serde_json::Value::String(REDACTED.into());
                        }
                    }
                } else {
                    redact_secrets(v);
                }
            }
        }
        serde_json::Value::Array(arr) => {
            for v in arr.iter_mut() {
                redact_secrets(v);
            }
        }
        _ => {}
    }
}

/// When saving config from the frontend, any field that came back as REDACTED must
/// be restored from the existing on-disk config so we don't wipe real secrets.
pub fn restore_redacted(incoming: &mut serde_json::Value, existing: &serde_json::Value) {
    match (incoming, existing) {
        (serde_json::Value::Object(in_map), serde_json::Value::Object(ex_map)) => {
            for (k, v) in in_map.iter_mut() {
                if SECRET_KEYS.contains(&k.as_str()) {
                    if let serde_json::Value::String(s) = v {
                        if s == REDACTED || s.is_empty() {
                            if let Some(real) = ex_map.get(k) {
                                if let serde_json::Value::String(real_s) = real {
                                    if !real_s.is_empty() {
                                        *v = real.clone();
                                    }
                                }
                            }
                        }
                    }
                } else if let Some(ex_child) = ex_map.get(k) {
                    restore_redacted(v, ex_child);
                }
            }
        }
        (serde_json::Value::Array(in_arr), serde_json::Value::Array(ex_arr)) => {
            // Match by id/name if objects, else index-aligned
            for (i, in_item) in in_arr.iter_mut().enumerate() {
                let ex_match = if let (Some(id), true) = (
                    in_item.get("id").and_then(|v| v.as_str()),
                    in_item.is_object(),
                ) {
                    ex_arr.iter().find(|e| e.get("id").and_then(|v| v.as_str()) == Some(id))
                } else {
                    ex_arr.get(i)
                };
                if let Some(ex_child) = ex_match {
                    restore_redacted(in_item, ex_child);
                }
            }
        }
        _ => {}
    }
}

// ─── Auth middleware ──────────────────────────────────

pub struct AuthState {
    pub config: Arc<tokio::sync::RwLock<AgentConfig>>,
}

/// Axum middleware: check Bearer token for /api/* routes.
/// - If api_auth_token configured: require matching header.
/// - If not configured: only allow 127.0.0.1 / ::1 connections.
pub async fn auth_middleware(
    State(state): State<Arc<AuthState>>,
    ConnectInfo(addr): ConnectInfo<std::net::SocketAddr>,
    req: Request<Body>,
    next: Next,
) -> Result<Response, StatusCode> {
    let path = req.uri().path();

    // Public: static assets, favicon
    if path == "/favicon.ico"
        || path == "/"
        || path.starts_with("/chat/")
    {
        return Ok(next.run(req).await);
    }

    if !path.starts_with("/api/") {
        return Ok(next.run(req).await);
    }

    let cfg = state.config.read().await;
    let configured_token = cfg.api_auth_token.clone();
    drop(cfg);

    // No token configured → localhost-only
    if configured_token.as_deref().unwrap_or("").is_empty() {
        let ip = addr.ip();
        if ip.is_loopback() {
            return Ok(next.run(req).await);
        }
        return Err(StatusCode::UNAUTHORIZED);
    }

    let expected = configured_token.unwrap();
    let header_ok = req.headers()
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(|t| constant_time_eq(t.as_bytes(), expected.as_bytes()))
        .unwrap_or(false);

    // Also allow token in query string for streaming endpoints that can't set headers
    let query_ok = req.uri().query()
        .and_then(|q| {
            q.split('&')
                .find_map(|p| p.strip_prefix("token="))
                .map(|t| constant_time_eq(t.as_bytes(), expected.as_bytes()))
        })
        .unwrap_or(false);

    if header_ok || query_ok {
        Ok(next.run(req).await)
    } else {
        Err(StatusCode::UNAUTHORIZED)
    }
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() { return false; }
    let mut result: u8 = 0;
    for (x, y) in a.iter().zip(b.iter()) {
        result |= x ^ y;
    }
    result == 0
}

// ─── Rate limiting ────────────────────────────────────

/// Simple per-IP token bucket. Not distributed, not perfect, but prevents trivial abuse.
pub struct RateLimiter {
    buckets: RwLock<HashMap<IpAddr, Bucket>>,
    per_minute: u32,
}

struct Bucket {
    tokens: f64,
    last_refill: Instant,
}

impl RateLimiter {
    pub fn new(per_minute: u32) -> Arc<Self> {
        Arc::new(Self {
            buckets: RwLock::new(HashMap::new()),
            per_minute,
        })
    }

    pub async fn check(&self, ip: IpAddr) -> bool {
        if self.per_minute == 0 { return true; }
        let now = Instant::now();
        let per_sec = self.per_minute as f64 / 60.0;
        let capacity = self.per_minute as f64;

        let mut buckets = self.buckets.write().await;
        let bucket = buckets.entry(ip).or_insert(Bucket {
            tokens: capacity,
            last_refill: now,
        });

        let elapsed = now.duration_since(bucket.last_refill).as_secs_f64();
        bucket.tokens = (bucket.tokens + elapsed * per_sec).min(capacity);
        bucket.last_refill = now;

        if bucket.tokens >= 1.0 {
            bucket.tokens -= 1.0;
            true
        } else {
            false
        }
    }

    pub async fn cleanup(&self) {
        let now = Instant::now();
        let mut buckets = self.buckets.write().await;
        buckets.retain(|_, b| now.duration_since(b.last_refill).as_secs() < 600);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_safe_id_basic() {
        assert_eq!(safe_id("chat.roland"), Some("chat.roland".into()));
        assert_eq!(safe_id("abc-123_ok"), Some("abc-123_ok".into()));
    }

    #[test]
    fn test_safe_id_rejects_traversal() {
        assert_eq!(safe_id(".."), None);
        assert_eq!(safe_id("../etc"), None);
        assert_eq!(safe_id("foo/bar"), None);
        assert_eq!(safe_id("foo\\bar"), None);
        assert_eq!(safe_id("foo\0bar"), None);
        assert_eq!(safe_id(""), None);
    }

    #[test]
    fn test_safe_relative_path() {
        assert!(safe_relative_path("sub/file.txt").is_some());
        assert!(safe_relative_path("../etc").is_none());
        assert!(safe_relative_path("/etc").is_none());
        assert!(safe_relative_path("sub/../../etc").is_none());
    }

    #[test]
    fn test_ssrf_blocks_localhost() {
        assert!(validate_external_url("http://localhost/x").is_err());
        assert!(validate_external_url("http://127.0.0.1/x").is_err());
        assert!(validate_external_url("http://10.0.0.1/x").is_err());
        assert!(validate_external_url("http://192.168.1.1/x").is_err());
        assert!(validate_external_url("http://169.254.169.254/x").is_err());
        assert!(validate_external_url("http://[::1]/x").is_err());
    }

    #[test]
    fn test_ssrf_allows_public() {
        assert!(validate_external_url("https://example.com/x").is_ok());
        assert!(validate_external_url("https://8.8.8.8/x").is_ok());
    }

    #[test]
    fn test_ssrf_rejects_non_http() {
        assert!(validate_external_url("file:///etc/passwd").is_err());
        assert!(validate_external_url("ftp://example.com").is_err());
    }

    #[test]
    fn test_redact_secrets() {
        let mut v = serde_json::json!({
            "name": "test",
            "api_key": "secret123",
            "llm_backends": [{"id": "x", "api_key": "abc", "url": "http://x"}],
        });
        redact_secrets(&mut v);
        assert_eq!(v["api_key"], REDACTED);
        assert_eq!(v["llm_backends"][0]["api_key"], REDACTED);
        assert_eq!(v["name"], "test");
    }

    #[test]
    fn test_redact_skips_empty() {
        let mut v = serde_json::json!({"api_key": ""});
        redact_secrets(&mut v);
        assert_eq!(v["api_key"], "");
    }

    #[test]
    fn test_restore_redacted() {
        let existing = serde_json::json!({
            "api_key": "real_secret",
            "llm_backends": [{"id": "a", "api_key": "real_a"}, {"id": "b", "api_key": "real_b"}],
        });
        let mut incoming = serde_json::json!({
            "api_key": "***REDACTED***",
            "llm_backends": [{"id": "b", "api_key": "***REDACTED***"}, {"id": "a", "api_key": "new_a"}],
        });
        restore_redacted(&mut incoming, &existing);
        assert_eq!(incoming["api_key"], "real_secret");
        // Matched by id, not index
        assert_eq!(incoming["llm_backends"][0]["api_key"], "real_b");
        assert_eq!(incoming["llm_backends"][1]["api_key"], "new_a");
    }

    #[test]
    fn test_constant_time_eq() {
        assert!(constant_time_eq(b"abc", b"abc"));
        assert!(!constant_time_eq(b"abc", b"abd"));
        assert!(!constant_time_eq(b"abc", b"abcd"));
    }

    #[test]
    fn test_restore_redacted_empty_to_existing() {
        let existing = serde_json::json!({"api_key": "real_secret"});
        let mut incoming = serde_json::json!({"api_key": ""});
        restore_redacted(&mut incoming, &existing);
        assert_eq!(incoming["api_key"], "real_secret");
    }

    #[test]
    fn test_restore_redacted_empty_with_empty_existing_stays_empty() {
        let existing = serde_json::json!({"api_key": ""});
        let mut incoming = serde_json::json!({"api_key": ""});
        restore_redacted(&mut incoming, &existing);
        assert_eq!(incoming["api_key"], "");
    }
}
