"""Reddit public HTML scraper via BeautifulSoup.

Uses old.reddit.com because the modern Reddit frontend often serves
verification pages to non-browser clients. This module only reads public pages;
it does not log in, solve challenges, or bypass rate limits.
"""

import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - runtime dependency guard
    BeautifulSoup = None


MODULE = {
    "name": "reddit_scraper",
    "description": "Sucht Reddit-Themen/Threads und zieht oeffentliche Kommentare via old.reddit.com + BeautifulSoup.",
    "version": "1.0",
    "settings": {
        "max_results": {"type": "number", "label": "Max Suchtreffer", "default": 15},
        "max_threads": {"type": "number", "label": "Max Threads bei Pull", "default": 3},
        "max_comments": {"type": "number", "label": "Max Kommentare pro Thread", "default": 30},
        "max_comment_depth": {"type": "number", "label": "Max Kommentar-Tiefe", "default": 4},
        "include_nsfw": {"type": "bool", "label": "NSFW einschliessen", "default": False},
        "request_delay_ms": {"type": "number", "label": "Pause zwischen Thread-Abrufen", "default": 800},
        "request_timeout_s": {"type": "number", "label": "HTTP Timeout Sekunden", "default": 20},
        "user_agent": {
            "type": "string",
            "label": "User-Agent",
            "default": "aistuff-reddit-research/1.0 public-html-reader",
        },
        "max_output_chars": {"type": "number", "label": "Max Ausgabezeichen", "default": 24000},
    },
    "tools": [
        {
            "name": "reddit_scraper.search",
            "description": (
                "Sucht Reddit-Threads. Param Suchtext oder JSON "
                "{\"query\":\"black hole\",\"subreddit\":\"askscience\",\"sort\":\"top\","
                "\"time\":\"month\",\"limit\":10}."
            ),
            "params": ["query_json"],
        },
        {
            "name": "reddit_scraper.thread",
            "description": (
                "Zieht einen Reddit-Thread inkl. Kommentaren. JSON "
                "{\"url\":\"https://old.reddit.com/r/.../comments/...\","
                "\"comment_limit\":30,\"comment_sort\":\"top\",\"max_depth\":3}."
            ),
            "params": ["thread_json"],
        },
        {
            "name": "reddit_scraper.pull",
            "description": (
                "Sucht ein Thema und zieht direkt die Top-Threads mit Kommentaren. JSON "
                "{\"query\":\"UFO disclosure\",\"threads\":3,\"comments_per_thread\":20,"
                "\"sort\":\"relevance\",\"time\":\"month\"}."
            ),
            "params": ["query_json"],
        },
        {
            "name": "reddit_scraper.parse_html",
            "description": "Parst eingefuegtes Reddit-HTML als search oder thread. JSON {html, mode:'search'|'thread', query?, url?}.",
            "params": ["html_json"],
        },
        {
            "name": "reddit_scraper.help",
            "description": "Zeigt Filter, Sortierung und Beispielaufrufe.",
            "params": [],
        },
    ],
}


OLD_BASE = "https://old.reddit.com"
SEARCH_SORTS = {"relevance", "hot", "top", "new", "comments"}
TIME_FILTERS = {"hour", "day", "week", "month", "year", "all"}
COMMENT_SORTS = {"confidence", "top", "new", "controversial", "old", "qa", "live"}


def handle_tool(tool_name, params, config):
    try:
        if BeautifulSoup is None:
            return fail("beautifulsoup4 fehlt. Installiere z.B. python3-bs4 oder beautifulsoup4 fuer den Agent-Python.")
        if tool_name == "reddit_scraper.search":
            return _search(params, config)
        if tool_name == "reddit_scraper.thread":
            return _thread(params, config)
        if tool_name == "reddit_scraper.pull":
            return _pull(params, config)
        if tool_name == "reddit_scraper.parse_html":
            return _parse_html_tool(params, config)
        if tool_name == "reddit_scraper.help":
            return ok(help_text())
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"Reddit Scraper Fehler: {exc}")


def _search(params, config):
    payload = parse_payload(params)
    query = first_text(payload, "query", "q", "topic", "text")
    if not query:
        return fail('Kein Query. Beispiel: reddit_scraper.search({"query":"black hole","limit":10})')

    limit = cfg_int(payload.get("limit", config.get("max_results", 15)), 15, 1, 100)
    url = build_search_url(query, payload, config)
    ok_fetch, body, err = fetch_url(url, config)
    if not ok_fetch:
        return fail(f"REDDIT_SEARCH_FAILED\nquery: {query}\nurl: {url}\nerror: {err}")

    results = parse_search_html(body, limit, cfg_bool(payload.get("include_subreddits"), False))
    text = format_search(query, url, results, payload)
    return ok(limit_output(text, config))


def _thread(params, config):
    payload = parse_payload(params)
    url = first_text(payload, "url", "thread_url", "permalink")
    if not url:
        thread_id = first_text(payload, "thread_id", "id")
        subreddit = first_text(payload, "subreddit", "sr")
        if thread_id and subreddit:
            url = f"{OLD_BASE}/r/{clean_subreddit(subreddit)}/comments/{thread_id}/"
    if not url:
        return fail("url oder {subreddit, thread_id} fehlt.")

    target = build_thread_url(url, payload)
    ok_fetch, body, err = fetch_url(target, config)
    if not ok_fetch:
        return fail(f"REDDIT_THREAD_FAILED\nurl: {target}\nerror: {err}")

    comment_limit = cfg_int(payload.get("comment_limit", payload.get("comments", config.get("max_comments", 30))), 30, 0, 500)
    max_depth = cfg_int(payload.get("max_depth", config.get("max_comment_depth", 4)), 4, 0, 20)
    min_score = cfg_int(payload.get("min_score", -10_000), -10_000, -1000000, 1000000)
    parsed = parse_thread_html(body, target, comment_limit, max_depth, min_score)
    return ok(limit_output(format_thread(parsed), config))


def _pull(params, config):
    payload = parse_payload(params)
    query = first_text(payload, "query", "q", "topic", "text")
    if not query:
        return fail('Kein Query. Beispiel: reddit_scraper.pull({"query":"UFO disclosure","threads":3})')

    search_limit = cfg_int(payload.get("search_limit", payload.get("limit", config.get("max_results", 15))), 15, 1, 100)
    thread_count = cfg_int(payload.get("threads", payload.get("max_threads", config.get("max_threads", 3))), 3, 1, 20)
    comments_per_thread = cfg_int(
        payload.get("comments_per_thread", payload.get("comment_limit", config.get("max_comments", 30))),
        30,
        0,
        300,
    )
    max_depth = cfg_int(payload.get("max_depth", config.get("max_comment_depth", 4)), 4, 0, 20)
    min_score = cfg_int(payload.get("min_score", -10_000), -10_000, -1000000, 1000000)

    search_url = build_search_url(query, payload, config)
    ok_fetch, body, err = fetch_url(search_url, config)
    if not ok_fetch:
        return fail(f"REDDIT_PULL_FAILED\nquery: {query}\nurl: {search_url}\nerror: {err}")

    results = parse_search_html(body, search_limit, False)
    threads = []
    errors = []
    delay = cfg_int(config.get("request_delay_ms", 800), 800, 0, 10000) / 1000.0
    for item in results[:thread_count]:
        if delay and threads:
            time.sleep(delay)
        target = build_thread_url(item.get("url", ""), payload)
        ok_thread, thread_body, thread_err = fetch_url(target, config)
        if not ok_thread:
            errors.append(f"{item.get('id','')}: {thread_err}")
            continue
        threads.append(parse_thread_html(thread_body, target, comments_per_thread, max_depth, min_score))

    text = format_pull(query, search_url, results, threads, errors, payload)
    return ok(limit_output(text, config))


def _parse_html_tool(params, config):
    payload = parse_payload(params, "html")
    body = first_text(payload, "html", "body", "content")
    if not body:
        return fail("html fehlt.")
    mode = first_text(payload, "mode", "type") or detect_html_mode(body)
    if mode == "search":
        limit = cfg_int(payload.get("limit", config.get("max_results", 15)), 15, 1, 100)
        query = first_text(payload, "query", "q") or "(pasted search html)"
        results = parse_search_html(body, limit, cfg_bool(payload.get("include_subreddits"), False))
        return ok(limit_output(format_search(query, "(pasted html)", results, payload), config))
    comment_limit = cfg_int(payload.get("comment_limit", config.get("max_comments", 30)), 30, 0, 500)
    max_depth = cfg_int(payload.get("max_depth", config.get("max_comment_depth", 4)), 4, 0, 20)
    parsed = parse_thread_html(body, first_text(payload, "url") or "(pasted html)", comment_limit, max_depth, -10_000)
    return ok(limit_output(format_thread(parsed), config))


def fetch_url(url, config):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": str(config.get("user_agent") or MODULE["settings"]["user_agent"]["default"]),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8,de;q=0.6",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg_int(config.get("request_timeout_s"), 20, 5, 60)) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if looks_blocked(body):
                return False, "", "Reddit lieferte Verification/Login/Block-Seite. Kein Bypass im Modul."
            return True, body, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in {401, 403, 429}:
            return False, "", f"HTTP {exc.code}: Reddit blockt oder rate-limitiert den Abruf."
        return False, "", f"HTTP {exc.code}: {truncate(strip_tags(body), 500)}"
    except Exception as exc:
        return False, "", f"HTTP Fehler: {exc}"


def build_search_url(query, payload, config):
    subreddit = clean_subreddit(first_text(payload, "subreddit", "sr"))
    sort = normalize_choice(payload.get("sort"), SEARCH_SORTS, "relevance")
    time_filter = normalize_choice(payload.get("time") or payload.get("t"), TIME_FILTERS, "all")
    search_query = query
    include_nsfw = cfg_bool(payload.get("include_nsfw", config.get("include_nsfw")), False)
    if not include_nsfw and "nsfw:" not in search_query.lower():
        search_query = f"{search_query} nsfw:no"

    params = {"q": search_query, "sort": sort, "t": time_filter}
    if subreddit:
        params["restrict_sr"] = "on"
        path = f"/r/{subreddit}/search/"
    else:
        path = "/search/"
    return OLD_BASE + path + "?" + urllib.parse.urlencode(params)


def build_thread_url(url, payload):
    if not url:
        return ""
    url = html.unescape(url.strip())
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        url = urllib.parse.urljoin(OLD_BASE, url)
        parsed = urllib.parse.urlparse(url)
    netloc = "old.reddit.com" if "reddit.com" in parsed.netloc else parsed.netloc
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    sort = normalize_choice(payload.get("comment_sort") or payload.get("comments_sort") or payload.get("sort_comments"), COMMENT_SORTS, "")
    if sort:
        query["sort"] = sort
    query.setdefault("limit", "500")
    return urllib.parse.urlunparse((parsed.scheme or "https", netloc, parsed.path, "", urllib.parse.urlencode(query), ""))


def parse_search_html(body, limit, include_subreddits=False):
    soup = BeautifulSoup(body, "html.parser")
    out = []
    for node in soup.select(".search-result"):
        classes = set(node.get("class") or [])
        is_subreddit = "search-result-subreddit" in classes
        if is_subreddit and not include_subreddits:
            continue
        title_node = node.select_one(".search-title")
        if not title_node:
            continue
        url = normalize_reddit_url(title_node.get("href", ""))
        title = clean_text(title_node.get_text(" ", strip=True))
        meta = clean_text((node.select_one(".search-result-meta") or node).get_text(" ", strip=True))
        comments_node = node.select_one("a.search-comments")
        body_node = node.select_one(".search-result-body")
        subreddit_node = node.select_one(".search-subreddit-link")
        out.append(
            {
                "id": node.get("data-fullname", ""),
                "kind": "subreddit" if is_subreddit else "thread",
                "title": title,
                "url": url,
                "subreddit": clean_text(subreddit_node.get_text(" ", strip=True)) if subreddit_node else parse_subreddit(meta, url),
                "author": parse_author(meta),
                "score": parse_score(meta),
                "comments": parse_comments_count(clean_text(comments_node.get_text(" ", strip=True)) if comments_node else meta),
                "age": parse_age(meta),
                "flair": clean_text((node.select_one(".search-linkflairlabel") or {}).get("title", "") if node.select_one(".search-linkflairlabel") else ""),
                "snippet": clean_text(body_node.get_text(" ", strip=True)) if body_node else "",
                "meta": meta,
            }
        )
        if len(out) >= limit:
            break
    return out


def parse_thread_html(body, url, comment_limit, max_depth, min_score):
    soup = BeautifulSoup(body, "html.parser")
    link = soup.select_one("div.thing.link") or soup.select_one("div.link")
    post = parse_post(link, url) if link else {"url": url, "title": "", "selftext": ""}
    comments = []
    for node in soup.select("div.comment"):
        parsed = parse_comment(node)
        if not parsed.get("text"):
            continue
        if parsed.get("depth", 0) > max_depth:
            continue
        score = parsed.get("score")
        if isinstance(score, int) and score < min_score:
            continue
        comments.append(parsed)
        if len(comments) >= comment_limit:
            break
    post["comments_returned"] = len(comments)
    post["more_comments_markers"] = len(soup.select(".morecomments"))
    return {"url": url, "post": post, "comments": comments}


def parse_post(node, fallback_url):
    title_node = node.select_one("a.title") if node else None
    body_node = node.select_one(".usertext-body") if node else None
    comments_node = node.select_one("a.comments") if node else None
    return {
        "id": node.get("data-fullname", "") if node else "",
        "title": clean_text(title_node.get_text(" ", strip=True)) if title_node else "",
        "url": normalize_reddit_url(node.get("data-permalink", "") or fallback_url) if node else fallback_url,
        "external_url": normalize_reddit_url(title_node.get("href", "")) if title_node else "",
        "subreddit": f"r/{node.get('data-subreddit', '')}" if node and node.get("data-subreddit") else "",
        "author": node.get("data-author", "") if node else "",
        "score": parse_int(node.get("data-score")) if node else None,
        "comments": parse_int(node.get("data-comments-count")) if node else parse_comments_count(clean_text(comments_node.get_text(" ", strip=True)) if comments_node else ""),
        "selftext": clean_text(body_node.get_text(" ", strip=True)) if body_node else "",
        "nsfw": bool(node and node.get("data-nsfw") == "true"),
    }


def parse_comment(node):
    entry = node.find("div", class_="entry")
    body_node = entry.find("div", class_="usertext-body") if entry else node.find("div", class_="usertext-body")
    author_node = entry.select_one("a.author") if entry else node.select_one("a.author")
    score_node = entry.select_one("span.score.unvoted") if entry else node.select_one("span.score.unvoted")
    permalink_node = entry.select_one("a.bylink") if entry else node.select_one("a.bylink")
    score = parse_int(score_node.get("title")) if score_node and score_node.get("title") else parse_score(clean_text(entry.get_text(" ", strip=True)) if entry else "")
    return {
        "id": node.get("data-fullname", ""),
        "author": node.get("data-author", "") or (clean_text(author_node.get_text(" ", strip=True)) if author_node else ""),
        "score": score,
        "depth": comment_depth(node),
        "age": parse_age(clean_text(entry.get_text(" ", strip=True)) if entry else ""),
        "permalink": normalize_reddit_url(permalink_node.get("href", "")) if permalink_node else "",
        "text": clean_text(body_node.get_text(" ", strip=True)) if body_node else "",
    }


def format_search(query, url, results, payload):
    lines = [
        "REDDIT_SEARCH",
        f"query: {query}",
        f"url: {url}",
        f"results: {len(results)}",
        f"sort: {payload.get('sort', 'relevance')} time: {payload.get('time', payload.get('t', 'all'))}",
        "",
        "THREADS",
    ]
    if not results:
        lines.append("Keine Threads gefunden.")
        return "\n".join(lines)
    for idx, item in enumerate(results, 1):
        lines.append(
            f"{idx}. score={fmt_num(item.get('score'))} comments={fmt_num(item.get('comments'))} "
            f"age={item.get('age','')} {item.get('subreddit','')} author={item.get('author','')}"
        )
        lines.append(f"   title: {item.get('title','')}")
        if item.get("flair"):
            lines.append(f"   flair: {item['flair']}")
        if item.get("snippet"):
            lines.append(f"   snippet: {truncate(item['snippet'], 500)}")
        lines.append(f"   url: {item.get('url','')}")
    return "\n".join(lines)


def format_thread(parsed):
    post = parsed.get("post") or {}
    comments = parsed.get("comments") or []
    lines = [
        "REDDIT_THREAD",
        f"url: {parsed.get('url','')}",
        f"title: {post.get('title','')}",
        f"subreddit: {post.get('subreddit','')} author: {post.get('author','')} score: {fmt_num(post.get('score'))} comments_total: {fmt_num(post.get('comments'))}",
        f"comments_returned: {len(comments)} more_markers: {post.get('more_comments_markers', 0)}",
    ]
    if post.get("selftext"):
        lines.append("selftext: " + truncate(post.get("selftext", ""), 1200))
    lines.append("")
    lines.append("COMMENTS")
    if not comments:
        lines.append("Keine Kommentare extrahiert.")
        return "\n".join(lines)
    for idx, comment in enumerate(comments, 1):
        lines.append(
            f"{idx}. score={fmt_num(comment.get('score'))} depth={comment.get('depth', 0)} "
            f"age={comment.get('age','')} author={comment.get('author','')}"
        )
        lines.append(f"   text: {truncate(comment.get('text',''), 900)}")
        if comment.get("permalink"):
            lines.append(f"   url: {comment['permalink']}")
    return "\n".join(lines)


def format_pull(query, search_url, search_results, threads, errors, payload):
    lines = [
        "REDDIT_PULL",
        f"query: {query}",
        f"search_url: {search_url}",
        f"search_results: {len(search_results)}",
        f"threads_fetched: {len(threads)}",
        f"sort: {payload.get('sort', 'relevance')} time: {payload.get('time', payload.get('t', 'all'))}",
    ]
    if errors:
        lines.append("partial_errors:")
        lines.extend(f"- {err}" for err in errors[:8])
    for idx, parsed in enumerate(threads, 1):
        post = parsed.get("post") or {}
        lines.extend(
            [
                "",
                f"THREAD {idx}",
                f"title: {post.get('title','')}",
                f"subreddit: {post.get('subreddit','')} author: {post.get('author','')} score: {fmt_num(post.get('score'))} comments_total: {fmt_num(post.get('comments'))}",
                f"url: {parsed.get('url','')}",
            ]
        )
        if post.get("selftext"):
            lines.append("selftext: " + truncate(post.get("selftext", ""), 800))
        lines.append("comments:")
        comments = parsed.get("comments") or []
        if not comments:
            lines.append("- Keine Kommentare extrahiert.")
            continue
        for cidx, comment in enumerate(comments, 1):
            indent = "  " + ("  " * min(comment.get("depth", 0), 4))
            lines.append(
                f"{indent}- score={fmt_num(comment.get('score'))} depth={comment.get('depth',0)} "
                f"author={comment.get('author','')}: {truncate(comment.get('text',''), 600)}"
            )
    return "\n".join(lines)


def help_text():
    return "\n".join(
        [
            "REDDIT_SCRAPER_HELP",
            "tools: reddit_scraper.search, reddit_scraper.thread, reddit_scraper.pull, reddit_scraper.parse_html",
            "search filters: query, subreddit, sort=relevance|hot|top|new|comments, time=hour|day|week|month|year|all, limit, include_nsfw",
            "thread filters: url oder subreddit+thread_id, comment_limit, comment_sort=confidence|top|new|controversial|old|qa, max_depth, min_score",
            "pull: kombiniert Suche + Thread-Kommentare, z.B. threads=3 und comments_per_thread=20",
            "safety: nutzt nur oeffentliche old.reddit.com HTML-Seiten; kein Login, kein CAPTCHA/Verification-Bypass.",
            'example search: {"query":"black hole","subreddit":"askscience","sort":"top","time":"month","limit":10}',
            'example pull: {"query":"UFO disclosure","threads":3,"comments_per_thread":25,"sort":"relevance","time":"month"}',
        ]
    )


def normalize_reddit_url(url):
    if not url:
        return ""
    url = html.unescape(url)
    if url.startswith("/"):
        return urllib.parse.urljoin(OLD_BASE, url)
    parsed = urllib.parse.urlparse(url)
    if "reddit.com" in parsed.netloc:
        return urllib.parse.urlunparse((parsed.scheme or "https", "old.reddit.com", parsed.path, "", parsed.query, ""))
    return url


def looks_blocked(body):
    text = body[:8000].lower()
    return any(
        marker in text
        for marker in (
            "please wait for verification",
            "blocked due to a network policy",
            "whoa there, pardner",
            "our cdn was unable to reach our servers",
            "login-required",
            "captcha",
        )
    )


def detect_html_mode(body):
    text = body[:20000]
    if "search-result-listing" in text or "search-result" in text:
        return "search"
    return "thread"


def parse_author(meta):
    match = re.search(r"\bby\s+([A-Za-z0-9_\-\[\]{}]+)", meta)
    return match.group(1) if match else ""


def parse_subreddit(meta, url):
    match = re.search(r"\bto\s+(r/[A-Za-z0-9_]+)", meta)
    if match:
        return match.group(1)
    match = re.search(r"/r/([^/]+)/", url or "")
    return f"r/{match.group(1)}" if match else ""


def parse_age(meta):
    match = re.search(r"submitted\s+(.+?)\s+by\b", meta)
    if match:
        return match.group(1).strip()
    match = re.search(r"(\d+\s+(?:minute|hour|day|month|year)s?\s+ago)", meta)
    return match.group(1) if match else ""


def parse_score(text):
    match = re.search(r"([\d,.]+)\s+points?", text or "", re.I)
    return parse_int(match.group(1)) if match else None


def parse_comments_count(text):
    match = re.search(r"([\d,.]+)\s+comments?", text or "", re.I)
    return parse_int(match.group(1)) if match else None


def parse_int(value):
    if value in (None, ""):
        return None
    text = str(value).strip().lower().replace(",", "")
    multiplier = 1
    if text.endswith("k"):
        multiplier = 1000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except Exception:
        return None


def comment_depth(node):
    depth = 0
    parent = node.parent
    while parent is not None:
        if getattr(parent, "name", None) == "div" and "child" in (parent.get("class") or []):
            depth += 1
        parent = parent.parent
    return max(0, depth - 1)


def clean_subreddit(value):
    value = clean_text(value)
    value = value[2:] if value.lower().startswith("r/") else value
    return re.sub(r"[^A-Za-z0-9_]", "", value)


def normalize_choice(value, allowed, default):
    value = str(value or "").strip().lower()
    return value if value in allowed else default


def parse_payload(params, default_key="query"):
    if isinstance(params, dict):
        return dict(params)
    if not params:
        return {}
    raw = params[0]
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {default_key: text}


def first_text(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def cfg_int(value, default, min_value=None, max_value=None):
    try:
        num = int(float(str(value)))
    except Exception:
        num = default
    if min_value is not None:
        num = max(min_value, num)
    if max_value is not None:
        num = min(max_value, num)
    return num


def cfg_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on"}


def clean_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def strip_tags(text):
    return re.sub(r"<[^>]+>", " ", str(text or ""))


def truncate(text, limit):
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def fmt_num(value):
    return "n/a" if value in (None, "") else str(value)


def limit_output(text, config):
    text = str(text or "")
    limit = cfg_int(config.get("max_output_chars", 24000), 24000, 2000, 80000)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def ok(data):
    return {"success": True, "data": data}


def fail(data):
    return {"success": False, "data": data}


if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            if req.get("action") == "describe":
                print(json.dumps(MODULE), flush=True)
            elif req.get("action") == "handle_tool":
                result = handle_tool(req["tool"], req.get("params", []), req.get("config", {}))
                print(json.dumps(result), flush=True)
            else:
                print(json.dumps({"error": f"Unknown action: {req.get('action')}"}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
