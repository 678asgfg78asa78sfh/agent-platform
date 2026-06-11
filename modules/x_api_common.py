import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


API_BASE_DEFAULT = "https://api.x.com"
RECENT_SEARCH_PATH = "/2/tweets/search/recent"
USAGE_TWEETS_PATH = "/2/usage/tweets"

POST_READ_USD = 0.005
USER_READ_USD = 0.010
SELF_SERVE_RECENT_QUERY_CHARS = 512

TWEET_FIELDS = ",".join(
    [
        "id",
        "text",
        "author_id",
        "created_at",
        "conversation_id",
        "entities",
        "lang",
        "public_metrics",
        "referenced_tweets",
        "source",
    ]
)

USER_FIELDS = "id,username,name,verified,verified_type,public_metrics"


def run_module(module, handler):
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            action = req.get("action")
            if action == "describe":
                print(json.dumps(module), flush=True)
            elif action == "handle_tool":
                result = handler(req.get("tool", ""), req.get("params", []), req.get("config", {}))
                print(json.dumps(result), flush=True)
            else:
                print(json.dumps({"error": f"Unknown action: {action}"}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), flush=True)


def ok(data):
    return {"success": True, "data": data}


def fail(data):
    return {"success": False, "data": data}


def load_runtime_config(config, module_dir):
    merged = {}
    local_path = os.path.join(module_dir, "config.json")
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8") as fh:
                local = json.load(fh)
            if isinstance(local, dict):
                for key, value in local.items():
                    if value not in ("", None, [], {}):
                        merged[key] = value
        except Exception:
            pass

    # Runtime config has already passed Rust-side vault alias resolution, so it
    # must override local fallback files that may still contain aliases.
    for key, value in dict(config or {}).items():
        if value not in ("", None, [], {}):
            merged[key] = value

    env_token = os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN")
    if env_token:
        merged["bearer_token"] = env_token
    return merged


def parse_payload(params, default_key="query"):
    if isinstance(params, dict):
        return dict(params)
    if not params:
        return {}

    raw = params[0]
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    raw = str(raw).strip()
    if not raw:
        return {}

    if raw.startswith("{") and raw.endswith("}"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {default_key: raw}


def cfg_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "ja", "on", "y"}:
        return True
    if text in {"0", "false", "no", "nein", "off", "n"}:
        return False
    return default


def cfg_int(value, default=0, min_value=None, max_value=None):
    try:
        num = int(float(str(value).strip()))
    except Exception:
        num = default
    if min_value is not None:
        num = max(min_value, num)
    if max_value is not None:
        num = min(max_value, num)
    return num


def first_text(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def compact_text(text, limit=280):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def validate_query(query, config):
    max_chars = cfg_int(
        config.get("max_query_chars"),
        SELF_SERVE_RECENT_QUERY_CHARS,
        min_value=1,
        max_value=4096,
    )
    if len(query) > max_chars:
        return f"Query ist {len(query)} Zeichen lang, Limit hier: {max_chars}. Kuerzen oder weniger Operatoren nutzen."
    return ""


def normalize_lang(value):
    text = str(value or "").strip()
    if not text or text.lower() in {"all", "any", "none", "-"}:
        return ""
    if re.match(r"^[a-z]{2}(-[A-Z]{2})?$", text):
        return text
    return ""


def append_operator(query, operator):
    if not operator:
        return query
    if re.search(r"(?<!\S)" + re.escape(operator) + r"(?!\S)", query):
        return query
    return f"{query} {operator}".strip()


def build_search_query(base_query, payload, config, default_include_replies=False):
    query = str(base_query or "").strip()
    if not query:
        return "", "Kein Suchbegriff. Beispiel: black hole oder {\"query\":\"black hole\",\"max_results\":20}"

    lang = normalize_lang(payload.get("lang", config.get("lang", "")))
    if lang and "lang:" not in query:
        query = append_operator(query, f"lang:{lang}")

    include_retweets = cfg_bool(payload.get("include_retweets", config.get("include_retweets")), False)
    if not include_retweets and "is:retweet" not in query:
        query = append_operator(query, "-is:retweet")

    include_replies = cfg_bool(
        payload.get("include_replies", config.get("include_replies")),
        default_include_replies,
    )
    if not include_replies and "is:reply" not in query and "conversation_id:" not in query:
        query = append_operator(query, "-is:reply")

    if cfg_bool(payload.get("only_replies"), False) and "is:reply" not in query:
        query = append_operator(query, "is:reply")

    if cfg_bool(payload.get("require_links", config.get("require_links")), False):
        query = append_operator(query, "has:links")

    if cfg_bool(payload.get("has_images"), False):
        query = append_operator(query, "has:images")

    from_user = first_text(payload, "from_user", "from")
    if from_user:
        from_user = from_user.lstrip("@")
        query = append_operator(query, f"from:{from_user}")

    mention_user = first_text(payload, "mention_user", "mentions_user")
    if mention_user:
        mention_user = mention_user.lstrip("@")
        query = append_operator(query, f"@{mention_user}")

    err = validate_query(query, config)
    if err:
        return "", err
    return query, ""


def build_time_params(payload, config):
    params = {}
    for key in ("start_time", "end_time", "since_id", "until_id", "next_token", "pagination_token"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            params[key] = str(value).strip()

    if "start_time" not in params:
        since_hours = cfg_int(payload.get("since_hours", config.get("default_since_hours", 0)), 0)
        if since_hours > 0:
            since_hours = min(since_hours, 168)
            start = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            params["start_time"] = start.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return params


def rate_headers(headers):
    lower = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    reset = lower.get("x-rate-limit-reset", "")
    reset_text = reset
    if reset and reset.isdigit():
        try:
            reset_text = datetime.fromtimestamp(int(reset), timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            pass
    return {
        "limit": lower.get("x-rate-limit-limit", ""),
        "remaining": lower.get("x-rate-limit-remaining", ""),
        "reset": reset_text,
    }


def api_get(path, params, config, module_dir):
    cfg = load_runtime_config(config, module_dir)
    token = str(cfg.get("bearer_token") or cfg.get("api_key") or "").strip()
    if not token:
        return False, None, {}, "X Bearer Token fehlt. In den Modul-Settings `bearer_token` setzen oder ENV `X_BEARER_TOKEN` nutzen."

    base = str(cfg.get("api_base") or API_BASE_DEFAULT).rstrip("/")
    timeout = cfg_int(cfg.get("request_timeout_s"), 20, min_value=5, max_value=60)
    query = urllib.parse.urlencode(params or {}, doseq=False)
    url = f"{base}{path}"
    if query:
        url += "?" + query

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "agent-x-modules/1.0",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            return True, data, dict(resp.headers), ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            data = {"raw": raw[:1200]}
        return False, data, dict(exc.headers), format_api_error(exc.code, data)
    except Exception as exc:
        return False, None, {}, f"X API Fehler: {exc}"


def format_api_error(code, data):
    parts = [f"X API HTTP {code}"]
    errors = []
    if isinstance(data, dict):
        if isinstance(data.get("errors"), list):
            errors.extend(data.get("errors") or [])
        if isinstance(data.get("error"), dict):
            errors.append(data["error"])
        elif data.get("title") or data.get("detail"):
            errors.append(data)
    for err in errors[:3]:
        if not isinstance(err, dict):
            continue
        msg = " / ".join(
            str(err.get(key, "")).strip()
            for key in ("title", "detail", "reason")
            if str(err.get(key, "")).strip()
        )
        if msg:
            parts.append(msg)
    if len(parts) == 1 and isinstance(data, dict) and data.get("raw"):
        parts.append(str(data["raw"])[:500])
    return ": ".join(parts)


def search_recent(query, payload, config, module_dir, include_authors=False):
    cfg = load_runtime_config(config, module_dir)
    max_results = cfg_int(
        payload.get("max_results", cfg.get("max_results", 20)),
        20,
        min_value=10,
        max_value=100,
    )
    max_pages = cfg_int(payload.get("pages", cfg.get("max_pages", 1)), 1, min_value=1, max_value=3)
    sort_order = str(payload.get("sort_order", cfg.get("sort_order", "recency")) or "recency").strip()
    if sort_order not in {"recency", "relevancy"}:
        sort_order = "recency"

    params = {
        "query": query,
        "max_results": max_results,
        "sort_order": sort_order,
        "tweet.fields": TWEET_FIELDS,
    }
    params.update(build_time_params(payload, cfg))
    if include_authors:
        params["expansions"] = "author_id"
        params["user.fields"] = USER_FIELDS

    all_posts = []
    includes_users = {}
    partial_errors = []
    last_headers = {}
    meta = {}
    next_token = params.pop("next_token", params.pop("pagination_token", None))

    for _ in range(max_pages):
        page_params = dict(params)
        if next_token:
            page_params["next_token"] = next_token
        success, data, headers, error = api_get(RECENT_SEARCH_PATH, page_params, cfg, module_dir)
        last_headers = headers
        if not success:
            return False, data, headers, error
        if not isinstance(data, dict):
            return False, data, headers, "Unerwartete X API Antwort."

        for post in data.get("data") or []:
            if isinstance(post, dict):
                all_posts.append(post)
        for user in ((data.get("includes") or {}).get("users") or []):
            if isinstance(user, dict) and user.get("id"):
                includes_users[str(user["id"])] = user
        if isinstance(data.get("errors"), list):
            partial_errors.extend(data.get("errors") or [])
        meta = data.get("meta") or {}
        next_token = meta.get("next_token")
        if not next_token:
            break

    deduped = []
    seen = set()
    for post in all_posts:
        post_id = str(post.get("id", ""))
        if post_id and post_id not in seen:
            deduped.append(post)
            seen.add(post_id)

    result = {
        "query": query,
        "posts": deduped,
        "users": includes_users,
        "meta": meta,
        "headers": rate_headers(last_headers),
        "partial_errors": partial_errors,
        "include_authors": include_authors,
        "estimated_cost": estimate_cost(deduped, includes_users if include_authors else {}),
    }
    return True, result, last_headers, ""


def estimate_cost(posts, users):
    post_count = len(posts or [])
    user_count = len(users or {})
    total = post_count * POST_READ_USD + user_count * USER_READ_USD
    return {
        "posts": post_count,
        "users": user_count,
        "post_read_usd": round(post_count * POST_READ_USD, 4),
        "user_read_usd": round(user_count * USER_READ_USD, 4),
        "total_usd": round(total, 4),
    }


def usage_tweets(config, module_dir):
    success, data, headers, error = api_get(USAGE_TWEETS_PATH, {}, config, module_dir)
    if not success:
        return fail(error)

    lines = ["X_USAGE_TWEETS"]
    rate = rate_headers(headers)
    if rate.get("remaining"):
        lines.append(f"rate_limit_remaining: {rate['remaining']} reset: {rate.get('reset', '')}")

    data_obj = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(data_obj, dict):
        return ok("\n".join(lines + [json.dumps(data, ensure_ascii=True)[:3000]]))

    project_id = data_obj.get("project_id", "")
    project_cap = data_obj.get("project_cap", "")
    if project_id or project_cap:
        lines.append(f"project_id: {project_id} project_cap_posts: {project_cap}")

    total_posts = 0
    for day in data_obj.get("daily_project_usage") or []:
        date = day.get("date", "")
        day_total = 0
        apps = []
        for entry in day.get("usage") or []:
            consumed = cfg_int(entry.get("tweets_consumed", 0), 0)
            day_total += consumed
            app_id = entry.get("app_id", "")
            apps.append(f"{app_id}:{consumed}" if app_id else str(consumed))
        total_posts += day_total
        lines.append(f"{date}: posts_consumed={day_total} apps={', '.join(apps)}")

    lines.append(f"estimated_post_read_cost_from_usage: ${round(total_posts * POST_READ_USD, 4)}")
    return ok("\n".join(lines))


def index_users(result):
    return result.get("users") or {}


def record_from_post(post, users=None):
    users = users or {}
    user = users.get(str(post.get("author_id"))) or {}
    metrics = post.get("public_metrics") or {}
    urls = extract_urls(post)
    score = engagement_score(metrics)
    username = user.get("username") or ""
    return {
        "id": str(post.get("id", "")),
        "created_at": str(post.get("created_at", "")),
        "author_id": str(post.get("author_id", "")),
        "username": username,
        "author_name": user.get("name") or "",
        "verified": user.get("verified"),
        "lang": post.get("lang") or "",
        "conversation_id": str(post.get("conversation_id", "")),
        "text": compact_text(post.get("text", ""), 700),
        "metrics": metrics,
        "urls": urls,
        "score": score,
        "url": post_url(post, username),
        "referenced_tweets": post.get("referenced_tweets") or [],
        "source": post.get("source") or "",
    }


def extract_urls(post):
    urls = []
    entities = post.get("entities") or {}
    for item in entities.get("urls") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("unwound_url") or item.get("expanded_url") or item.get("url")
        if value and value not in urls:
            urls.append(value)
    return urls


def post_url(post, username=""):
    post_id = str(post.get("id", ""))
    if username:
        return f"https://x.com/{username}/status/{post_id}"
    return f"https://x.com/i/web/status/{post_id}"


def engagement_score(metrics):
    if not isinstance(metrics, dict):
        return 0
    return (
        cfg_int(metrics.get("like_count", 0), 0)
        + 2 * cfg_int(metrics.get("reply_count", 0), 0)
        + 3 * cfg_int(metrics.get("retweet_count", 0), 0)
        + 3 * cfg_int(metrics.get("quote_count", 0), 0)
    )


def metrics_text(metrics):
    if not isinstance(metrics, dict):
        return "likes=0 replies=0 reposts=0 quotes=0"
    return (
        f"likes={metrics.get('like_count', 0)} "
        f"replies={metrics.get('reply_count', 0)} "
        f"reposts={metrics.get('retweet_count', 0)} "
        f"quotes={metrics.get('quote_count', 0)}"
    )


def source_domain(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def cost_line(estimated):
    if not isinstance(estimated, dict):
        return ""
    return (
        "estimated_x_api_cost: "
        f"${estimated.get('total_usd', 0)} "
        f"(posts={estimated.get('posts', 0)} x ${POST_READ_USD}, "
        f"users={estimated.get('users', 0)} x ${USER_READ_USD})"
    )


def rate_line(rate):
    if not isinstance(rate, dict) or not any(rate.values()):
        return ""
    return (
        "rate_limit: "
        f"limit={rate.get('limit', '')} "
        f"remaining={rate.get('remaining', '')} "
        f"reset={rate.get('reset', '')}"
    ).strip()
