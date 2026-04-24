use crate::tools::ToolResult;
use crate::types::ModulSettings;

/// Simple HTML tag stripper - extracts readable text from HTML
fn strip_html(html: &str) -> String {
    let mut result = String::new();
    let mut in_tag = false;
    let mut in_script = false;
    let mut in_style = false;
    let lower = html.to_lowercase();
    let chars: Vec<char> = html.chars().collect();
    let lower_chars: Vec<char> = lower.chars().collect();

    let mut i = 0;
    while i < chars.len() {
        if !in_tag && chars[i] == '<' {
            // Check for script/style open
            let remaining: String = lower_chars[i..].iter().take(10).collect();
            if remaining.starts_with("<script") { in_script = true; }
            if remaining.starts_with("<style") { in_style = true; }
            in_tag = true;
        } else if in_tag && chars[i] == '>' {
            let remaining: String = lower_chars[i.saturating_sub(8)..=i].iter().collect();
            if remaining.contains("</script") { in_script = false; }
            if remaining.contains("</style") { in_style = false; }
            in_tag = false;
            // Add space after block elements
            if !result.ends_with(' ') && !result.ends_with('\n') {
                result.push(' ');
            }
        } else if !in_tag && !in_script && !in_style {
            result.push(chars[i]);
        }
        i += 1;
    }

    // Decode common HTML entities
    let result = result
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&nbsp;", " ");

    // Collapse whitespace
    let mut collapsed = String::new();
    let mut last_was_space = false;
    for c in result.chars() {
        if c.is_whitespace() {
            if !last_was_space {
                collapsed.push(' ');
                last_was_space = true;
            }
        } else {
            collapsed.push(c);
            last_was_space = false;
        }
    }
    collapsed.trim().to_string()
}

/// Raw HTTP GET - fetch any URL
pub async fn http_get(url: &str) -> ToolResult {
    if url.is_empty() {
        return ToolResult::fail("Keine URL angegeben".into());
    }
    if let Err(e) = crate::security::validate_external_url(url) {
        return ToolResult::fail(format!("DENIED: SSRF-Schutz: {}", e));
    }

    let client = build_client(15);
    match client.get(url).send().await {
        Ok(resp) => {
            let status = resp.status().as_u16();
            match resp.text().await {
                Ok(body) => {
                    let text = strip_html(&body);
                    let truncated = truncate(&text, 6000);
                    ToolResult::ok(format!("HTTP {} | {}", status, truncated))
                }
                Err(e) => ToolResult::fail(format!("Response lesen fehlgeschlagen: {}", e)),
            }
        }
        Err(e) => ToolResult::fail(format!("HTTP GET fehlgeschlagen: {}", e)),
    }
}

// ─── Web Search Engine ─────────────────────────────

/// Multi-source web search. Tries available engines in order.
/// Settings can configure: search_engine (duckduckgo|brave|serper|google|grok), api keys etc.
pub async fn search(settings: &ModulSettings, query: &str) -> ToolResult {
    if query.trim().is_empty() {
        return ToolResult::fail("Keine Suchanfrage".into());
    }

    let engine = settings.search_engine.as_deref().unwrap_or("duckduckgo");
    let _max_results = settings.max_results.unwrap_or(8) as usize;

    let result = match engine {
        "brave" => search_brave(settings, query).await,
        "serper" => search_serper(settings, query).await,
        "google" => search_google(settings, query).await,
        "grok" => search_grok(settings, query).await,
        _ => search_duckduckgo(query).await,
    };

    // Fallback to DuckDuckGo if primary fails
    match &result {
        r if !r.success && engine != "duckduckgo" => {
            tracing::warn!("Search engine '{}' failed, falling back to DuckDuckGo", engine);
            search_duckduckgo(query).await
        }
        _ => result,
    }
}

// ─── DuckDuckGo (free, no API key) ────────────────

async fn search_duckduckgo(query: &str) -> ToolResult {
    let client = build_client(10);
    let url = format!("https://lite.duckduckgo.com/lite/?q={}", urlencod(query));

    let resp = match client.get(&url)
        .header("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
        .send().await {
        Ok(r) => r,
        Err(e) => return ToolResult::fail(format!("DuckDuckGo Fehler: {}", e)),
    };

    let html = match resp.text().await {
        Ok(t) => t,
        Err(e) => return ToolResult::fail(format!("DuckDuckGo Response Fehler: {}", e)),
    };

    // Parse DuckDuckGo Lite results
    // DDG Lite wraps links as: href="//duckduckgo.com/l/?uddg=https%3A%2F%2F..." class='result-link'>Title</a>
    // Then snippets in: class="result-snippet">text</td>
    let mut results = Vec::new();
    let mut pos = 0;

    while let Some(link_start) = html[pos..].find("class='result-link'") {
        let abs = pos + link_start;

        // Search backwards from class='result-link' to find href="..."
        let search_back = &html[abs.saturating_sub(2000)..abs];
        if let Some(href_pos) = search_back.rfind("href=\"") {
            let href_start = abs.saturating_sub(2000) + href_pos + 6;
            if let Some(href_end) = html[href_start..abs].find('"') {
                let raw_url = &html[href_start..href_start + href_end];

                // Extract actual URL from DDG redirect: uddg=https%3A%2F%2F...
                let real_url = if let Some(uddg_pos) = raw_url.find("uddg=") {
                    let encoded = &raw_url[uddg_pos + 5..];
                    let encoded = encoded.split('&').next().unwrap_or(encoded);
                    urldecod(encoded)
                } else if raw_url.starts_with("http") {
                    raw_url.to_string()
                } else {
                    pos = abs + 20;
                    continue;
                };

                // Get the link text (title)
                if let Some(gt) = html[abs..].find('>') {
                    let title_start = abs + gt + 1;
                    if let Some(title_end) = html[title_start..].find("</a>") {
                        let title = strip_html(&html[title_start..title_start + title_end]);

                        // Find snippet after this result
                        let snippet_search = &html[title_start..];
                        let snippet = if let Some(sn_pos) = snippet_search.find("class=\"result-snippet\"") {
                            let sn_abs = title_start + sn_pos;
                            if let Some(sn_gt) = html[sn_abs..].find('>') {
                                let sn_start = sn_abs + sn_gt + 1;
                                if let Some(sn_end) = html[sn_start..].find("</") {
                                    strip_html(&html[sn_start..sn_start + sn_end.min(500)])
                                } else { String::new() }
                            } else { String::new() }
                        } else { String::new() };

                        // Skip DDG internal and sponsored links
                        if !real_url.contains("duckduckgo.com") && !real_url.contains("duck.co")
                           && !real_url.contains("bing.com/aclick") && !title.is_empty() {
                            results.push(format!("{}\n  {}\n  {}", title.trim(), real_url, snippet.trim()));
                        }
                    }
                }
            }
        }
        pos = abs + 20;
        if results.len() >= 8 { break; }
    }

    if results.is_empty() {
        ToolResult::ok(format!("DuckDuckGo: Keine Ergebnisse für '{}'", query))
    } else {
        ToolResult::ok(format!("DuckDuckGo Ergebnisse für '{}':\n\n{}", query, results.join("\n\n")))
    }
}

// ─── Brave Search (free tier: 2000/month) ─────────

async fn search_brave(settings: &ModulSettings, query: &str) -> ToolResult {
    let api_key = match settings.brave_api_key.as_deref() {
        Some(k) if !k.is_empty() => k,
        _ => return ToolResult::fail("Brave Search: brave_api_key nicht konfiguriert".into()),
    };

    let client = build_client(10);
    let url = format!("https://api.search.brave.com/res/v1/web/search?q={}&count=8", urlencod(query));

    let resp = match client.get(&url)
        .header("X-Subscription-Token", api_key)
        .header("Accept", "application/json")
        .send().await {
        Ok(r) => r,
        Err(e) => return ToolResult::fail(format!("Brave Search Fehler: {}", e)),
    };

    let data: serde_json::Value = match resp.json().await {
        Ok(d) => d,
        Err(e) => return ToolResult::fail(format!("Brave Search Parse Fehler: {}", e)),
    };

    let results: Vec<String> = data["web"]["results"].as_array()
        .map(|arr| arr.iter().take(8).map(|r| {
            format!("{}\n  {}\n  {}",
                r["title"].as_str().unwrap_or(""),
                r["url"].as_str().unwrap_or(""),
                r["description"].as_str().unwrap_or(""))
        }).collect())
        .unwrap_or_default();

    if results.is_empty() {
        ToolResult::ok(format!("Brave: Keine Ergebnisse für '{}'", query))
    } else {
        ToolResult::ok(format!("Brave Ergebnisse für '{}':\n\n{}", query, results.join("\n\n")))
    }
}

// ─── Serper.dev (Google results, free: 2500/month) ─

async fn search_serper(settings: &ModulSettings, query: &str) -> ToolResult {
    let api_key = match settings.serper_api_key.as_deref() {
        Some(k) if !k.is_empty() => k,
        _ => return ToolResult::fail("Serper: serper_api_key nicht konfiguriert".into()),
    };

    let client = build_client(10);
    let body = serde_json::json!({"q": query, "num": 8});

    let resp = match client.post("https://google.serper.dev/search")
        .header("X-API-KEY", api_key)
        .header("Content-Type", "application/json")
        .json(&body)
        .send().await {
        Ok(r) => r,
        Err(e) => return ToolResult::fail(format!("Serper Fehler: {}", e)),
    };

    let data: serde_json::Value = match resp.json().await {
        Ok(d) => d,
        Err(e) => return ToolResult::fail(format!("Serper Parse Fehler: {}", e)),
    };

    let results: Vec<String> = data["organic"].as_array()
        .map(|arr| arr.iter().take(8).map(|r| {
            format!("{}\n  {}\n  {}",
                r["title"].as_str().unwrap_or(""),
                r["link"].as_str().unwrap_or(""),
                r["snippet"].as_str().unwrap_or(""))
        }).collect())
        .unwrap_or_default();

    if results.is_empty() {
        ToolResult::ok(format!("Serper: Keine Ergebnisse für '{}'", query))
    } else {
        ToolResult::ok(format!("Google (via Serper) Ergebnisse für '{}':\n\n{}", query, results.join("\n\n")))
    }
}

// ─── Google Custom Search (free: 100/day) ──────────

async fn search_google(settings: &ModulSettings, query: &str) -> ToolResult {
    let api_key = match settings.google_api_key.as_deref() {
        Some(k) if !k.is_empty() => k,
        _ => return ToolResult::fail("Google: google_api_key nicht konfiguriert".into()),
    };
    let cx = match settings.google_cx.as_deref() {
        Some(c) if !c.is_empty() => c,
        _ => return ToolResult::fail("Google: google_cx (Search Engine ID) nicht konfiguriert".into()),
    };

    let client = build_client(10);
    let url = format!(
        "https://www.googleapis.com/customsearch/v1?key={}&cx={}&q={}&num=8",
        api_key, cx, urlencod(query)
    );

    let resp = match client.get(&url).send().await {
        Ok(r) => r,
        Err(e) => return ToolResult::fail(format!("Google Search Fehler: {}", e)),
    };

    let data: serde_json::Value = match resp.json().await {
        Ok(d) => d,
        Err(e) => return ToolResult::fail(format!("Google Parse Fehler: {}", e)),
    };

    let results: Vec<String> = data["items"].as_array()
        .map(|arr| arr.iter().take(8).map(|r| {
            format!("{}\n  {}\n  {}",
                r["title"].as_str().unwrap_or(""),
                r["link"].as_str().unwrap_or(""),
                r["snippet"].as_str().unwrap_or(""))
        }).collect())
        .unwrap_or_default();

    if results.is_empty() {
        ToolResult::ok(format!("Google: Keine Ergebnisse für '{}'", query))
    } else {
        ToolResult::ok(format!("Google Ergebnisse für '{}':\n\n{}", query, results.join("\n\n")))
    }
}

// ─── Grok / xAI (uses Grok's web search capability) ─

async fn search_grok(settings: &ModulSettings, query: &str) -> ToolResult {
    let api_key = match settings.grok_api_key.as_deref() {
        Some(k) if !k.is_empty() => k,
        _ => return ToolResult::fail("Grok: grok_api_key nicht konfiguriert".into()),
    };

    let client = build_client(30);
    let body = serde_json::json!({
        "model": "grok-3-mini",
        "messages": [
            {"role": "system", "content": "Du bist ein Web-Recherche-Assistent. Suche nach der Anfrage und gib die wichtigsten Ergebnisse als strukturierte Liste zurück. Pro Ergebnis: Titel, URL, kurze Beschreibung."},
            {"role": "user", "content": format!("Suche im Web nach: {}", query)}
        ],
        "search_mode": "auto"
    });

    let resp = match client.post("https://api.x.ai/v1/chat/completions")
        .header("Authorization", format!("Bearer {}", api_key))
        .header("Content-Type", "application/json")
        .json(&body)
        .send().await {
        Ok(r) => r,
        Err(e) => return ToolResult::fail(format!("Grok Fehler: {}", e)),
    };

    let data: serde_json::Value = match resp.json().await {
        Ok(d) => d,
        Err(e) => return ToolResult::fail(format!("Grok Parse Fehler: {}", e)),
    };

    let content = data["choices"][0]["message"]["content"].as_str().unwrap_or("");
    if content.is_empty() {
        ToolResult::fail("Grok: Leere Antwort".into())
    } else {
        ToolResult::ok(format!("Grok Web Search für '{}':\n\n{}", query, truncate(content, 4000)))
    }
}

// ─── Helpers ───────────────────────────────────────

fn build_client(timeout_secs: u64) -> reqwest::Client {
    // SSRF-Schutz auf jedem Redirect-Hop: der initiale URL wird von
    // validate_external_url vor dem send() gecheckt, aber ein bösartiger
    // Server kann via 302 auf localhost/169.254/interne IPs umleiten und
    // das würde durchgehen — der Policy-Callback validiert JEDEN Hop und
    // bricht die Kette ab wenn ein non-public-Target erreicht wird.
    // GPT-Finding Run SQLite-8.
    let ssrf_policy = reqwest::redirect::Policy::custom(|attempt| {
        if attempt.previous().len() >= 5 {
            return attempt.error("too many redirects (SSRF-Schutz)");
        }
        let url = attempt.url().as_str();
        if let Err(e) = crate::security::validate_external_url(url) {
            return attempt.error(format!("redirect blocked (SSRF-Schutz): {}", e));
        }
        attempt.follow()
    });
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(timeout_secs))
        .redirect(ssrf_policy)
        .build()
        .unwrap_or_else(|_| reqwest::Client::new())
}

fn urldecod(s: &str) -> String {
    let mut bytes: Vec<u8> = Vec::new();
    let raw = s.as_bytes();
    let mut i = 0;
    while i < raw.len() {
        if raw[i] == b'%' && i + 2 < raw.len() {
            if let Ok(byte) = u8::from_str_radix(
                std::str::from_utf8(&raw[i+1..i+3]).unwrap_or(""), 16
            ) {
                bytes.push(byte);
                i += 3;
                continue;
            }
        }
        if raw[i] == b'+' {
            bytes.push(b' ');
        } else {
            bytes.push(raw[i]);
        }
        i += 1;
    }
    String::from_utf8(bytes.clone()).unwrap_or_else(|_| String::from_utf8_lossy(&bytes).to_string())
}

fn urlencod(s: &str) -> String {
    let mut result = String::new();
    for byte in s.as_bytes() {
        match *byte {
            b' ' => result.push('+'),
            b if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) => result.push(b as char),
            b => result.push_str(&format!("%{:02X}", b)),
        }
    }
    result
}

fn truncate(s: &str, max: usize) -> String {
    if s.len() <= max { return s.to_string(); }
    let mut end = max;
    while end > 0 && !s.is_char_boundary(end) { end -= 1; }
    format!("{}... [abgeschnitten, {} chars total]", &s[..end], s.len())
}
