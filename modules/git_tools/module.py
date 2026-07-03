"""Git & GitHub fuer den Coding-Flow: status/diff/commit/push lokal, PRs via GitHub-API.

Arbeitsteilung (bewusst so geschnitten):
  - coding.*    aendert Code (context -> patch -> run -> review)
  - editor.*    chirurgische Einzeldatei-Edits ohne Workflow
  - git_tools.* versioniert das Ergebnis (commit/branch/push/PR)

Sicherheit: kein shell=True, feste Subkommandos, validierte Branch-/Remote-
Namen (kein "-"-Prefix = keine Argument-Injection), Workspace-Confinement wie
im coding-Modul, Token/Credentials werden aus jeder Ausgabe redigiert.
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

MODULE = {
    "name": "git_tools",
    "description": (
        "Git-Versionskontrolle + GitHub-PRs fuer Coding-Aufgaben: status, diff, log, "
        "branch, commit, push, pull, pr_create, pr_list. Ergaenzt coding.* (Code aendern) "
        "und editor.* (Einzeldatei-Edits) um den Abschluss: committen, pushen, PR stellen."
    ),
    "version": "1.0",
    "settings": {
        "workspace_root": {"type": "string", "label": "Workspace Root leer=Coding-Home", "default": ""},
        "allow_project_root": {"type": "bool", "label": "Projektroot als Workspace erlauben", "default": False},
        "allow_push": {"type": "bool", "label": "git push erlauben", "default": True},
        "github_token": {"type": "string", "label": "GitHub Token leer=Vault-Eintrag 'github'", "default": ""},
        "command_timeout_s": {"type": "number", "label": "Git Timeout Sekunden", "default": 60},
        "max_output_kb": {"type": "number", "label": "Max Output KB pro Call", "default": 64},
    },
    "tools": [
        {
            "name": "git_tools.status",
            "description": "Repo-Status: Branch, Upstream, ahead/behind, geaenderte Dateien. JSON: {\"repo\": \"pfad\"} — repo optional, default Workspace-Root. Beispiel: git_tools.status({\"repo\": \"myproject\"})",
            "params": ["query_json"],
        },
        {
            "name": "git_tools.diff",
            "description": "Unified diff der Aenderungen. JSON: {repo?, staged?: bool, base?: \"HEAD~1\"|branch, path?}. Ohne base: Working-Tree vs HEAD. Beispiel: git_tools.diff({\"repo\": \"myproject\", \"staged\": false})",
            "params": ["query_json"],
        },
        {
            "name": "git_tools.log",
            "description": "Commit-Historie kompakt (hash, datum, subject). JSON: {repo?, limit?: 20, path?}.",
            "params": ["query_json"],
        },
        {
            "name": "git_tools.branch",
            "description": "Branches: {repo?, action: \"list\"|\"create\"|\"switch\", name?}. create wechselt direkt auf den neuen Branch. Beispiel: git_tools.branch({\"action\": \"create\", \"name\": \"feature/login-fix\"})",
            "params": ["query_json"],
        },
        {
            "name": "git_tools.commit",
            "description": "Staged + committet. JSON: {repo?, message, add?: \"all\"|[\"pfad1\",\"pfad2\"]}. add default \"all\" (= git add -A). Beispiel: git_tools.commit({\"message\": \"fix: null check in parser\", \"add\": [\"src/parser.py\"]})",
            "params": ["query_json"],
        },
        {
            "name": "git_tools.push",
            "description": "Pusht den aktuellen (oder angegebenen) Branch. JSON: {repo?, remote?: \"origin\", branch?, set_upstream?: bool}. Kein force-push moeglich.",
            "params": ["query_json"],
        },
        {
            "name": "git_tools.pull",
            "description": "Holt Remote-Stand. JSON: {repo?, remote?: \"origin\", rebase?: true}.",
            "params": ["query_json"],
        },
        {
            "name": "git_tools.init",
            "description": "Initialisiert ein neues Git-Repo im Workspace. JSON: {repo?, initial_branch?: \"main\"}.",
            "params": ["query_json"],
        },
        {
            "name": "git_tools.pr_create",
            "description": "Erstellt einen GitHub Pull Request fuer das origin-Repo. JSON: {repo?, title, body?, base?: \"main\", head?: aktueller Branch}. Braucht GitHub-Token (Vault 'github'). Beispiel: git_tools.pr_create({\"title\": \"Fix login timeout\", \"base\": \"main\"})",
            "params": ["query_json"],
        },
        {
            "name": "git_tools.pr_list",
            "description": "Listet GitHub Pull Requests des origin-Repos. JSON: {repo?, state?: \"open\"|\"closed\"|\"all\"}.",
            "params": ["query_json"],
        },
    ],
}

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
# Credentials in Remote-URLs (https://user:token@host oder https://token@host)
REDACT_RE = re.compile(r"(https?://)[^/@\s]+@")


def cfg_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "ja", "on"}
    if value is None:
        return default
    return bool(value)


def cfg_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def redact(text):
    return REDACT_RE.sub(r"\1***@", text or "")


def parse_payload(params):
    """Ein Param, JSON-Objekt mit benannten Keys. Nackter String = repo-Pfad."""
    raw = params[0] if params else ""
    if isinstance(raw, dict):
        return raw
    raw = str(raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {"_parse_error": raw}
    return {"repo": raw}


# ─── Workspace-Confinement (gleiche Regeln wie modules/coding) ───────────────

def home_dir(config):
    home = str(config.get("home_dir") or "").strip()
    if home:
        return os.path.realpath(os.path.abspath(home))
    # Default = Coding-Home, damit coding.* und git_tools.* denselben Baum sehen
    return os.path.realpath(os.path.abspath(os.path.join(os.getcwd(), "agent-data", "home", "coding")))


def is_within(path, root):
    path = os.path.realpath(os.path.abspath(path))
    root = os.path.realpath(os.path.abspath(root))
    return path == root or path.startswith(root + os.sep)


def effective_allow_project_root(config):
    if cfg_bool(config.get("confine_home_only"), False):
        return False
    return cfg_bool(config.get("allow_project_root"), False)


def resolve_repo(payload, config, create=False):
    raw = str(payload.get("repo") or config.get("workspace_root") or "").strip()
    home = home_dir(config)
    project = str(config.get("project_root") or "").strip()
    if not raw:
        raw = home
    if not os.path.isabs(raw):
        raw = os.path.join(home, raw)
    repo = os.path.realpath(os.path.abspath(raw))
    allowed = [home]
    if effective_allow_project_root(config) and project:
        allowed.append(os.path.realpath(os.path.abspath(project)))
    if not any(is_within(repo, a) for a in allowed):
        return None, f"Repo-Pfad nicht erlaubt: {repo}. Erlaubt unterhalb: {allowed}"
    if not os.path.isdir(repo):
        if create:
            # Nur fuer git_tools.init: neues Projektverzeichnis im Workspace
            # anlegen — der Pfad ist zu diesem Zeitpunkt bereits confined.
            os.makedirs(repo, exist_ok=True)
        else:
            return None, f"Verzeichnis existiert nicht: {repo}"
    return repo, None


def require_git_repo(repo, config):
    """Repo-Wurzel muss EXAKT `repo` sein.

    Wichtig: kein blosses `rev-parse --is-inside-work-tree` — das Coding-Home
    kann selbst innerhalb eines uebergeordneten Repos liegen (hier: agent/);
    dann wuerde `git add -A` im Home ins AEUSSERE Repo stagen. Der Toplevel-
    Vergleich deckt zugleich Worktrees und Submodule ab (.git als Datei).
    """
    # check=True: bei check=False landet gits stderr ("fatal: not a git
    # repository") im Rueckgabetext statt in err und wuerde unten als
    # "Toplevel" verglichen — Nicht-Repos bekaemen die falsche Meldung.
    top, err = run_git(repo, ["rev-parse", "--show-toplevel"], config, check=True)
    if err or not (top or "").strip():
        return f"Kein Git-Repo: {repo} (git_tools.init zum Anlegen nutzen)"
    if os.path.realpath(top.strip()) != os.path.realpath(repo):
        return (
            f"Kein eigenes Git-Repo: {repo} liegt innerhalb von {top.strip()} — "
            "git_tools.init nutzen oder den Repo-Pfad direkt angeben"
        )
    return None


def safe_name(value, what):
    name = str(value or "").strip()
    if not name:
        return None, f"{what} fehlt"
    if not SAFE_NAME_RE.match(name):
        return None, f"Ungueltiger {what}: {name!r} (erlaubt: A-Z a-z 0-9 . _ / -, kein '-' am Anfang)"
    return name, None


def run_git(repo, args, config, check=True):
    """Fuehrt git mit fester Arg-Liste aus. Kein Shell, kein Prompt, Output gekappt."""
    timeout = max(5, cfg_int(config.get("command_timeout_s"), 60))
    max_kb = max(4, cfg_int(config.get("max_output_kb"), 64))
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_AUTHOR_NAME", "Agent Platform")
    env.setdefault("GIT_AUTHOR_EMAIL", "agent@local")
    env.setdefault("GIT_COMMITTER_NAME", "Agent Platform")
    env.setdefault("GIT_COMMITTER_EMAIL", "agent@local")
    try:
        proc = subprocess.run(
            # hooksPath=/dev/null: Repo-eigene Hooks (potenziell aus einem
            # geklonten Fremd-Repo) duerfen hier nie Code ausfuehren.
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat", "-C", repo] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None, f"git {' '.join(args[:2])} Timeout nach {timeout}s"
    except FileNotFoundError:
        return None, "git ist auf dem System nicht installiert"
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr.strip() else "")
    out = redact(out.strip())
    if len(out) > max_kb * 1024:
        out = out[: max_kb * 1024] + f"\n... [gekappt bei {max_kb}KB]"
    if check and proc.returncode != 0:
        return None, f"git {' '.join(args[:2])} fehlgeschlagen (exit {proc.returncode}):\n{out}"
    return out, None


def ok(data):
    return {"success": True, "data": data}


def fail(msg):
    return {"success": False, "data": msg}


# ─── Git-Operationen ─────────────────────────────────────────────────────────

def git_status(payload, config):
    repo, err = resolve_repo(payload, config)
    if err:
        return fail(err)
    if (err := require_git_repo(repo, config)):
        return fail(err)
    head, _ = run_git(repo, ["branch", "--show-current"], config, check=False)
    track, _ = run_git(
        repo,
        ["for-each-ref", "--format=%(upstream:short) %(upstream:track)", "refs/heads/" + (head or "HEAD")],
        config,
        check=False,
    )
    files, err = run_git(repo, ["status", "--porcelain"], config)
    if err:
        return fail(err)
    lines = [f"repo: {repo}", f"branch: {head or '(detached)'}"]
    if track and track.strip():
        lines.append(f"upstream: {track.strip()}")
    lines.append(f"changed files ({len(files.splitlines()) if files else 0}):")
    lines.append(files if files else "  (working tree clean)")
    return ok("\n".join(lines))


def git_diff(payload, config):
    repo, err = resolve_repo(payload, config)
    if err:
        return fail(err)
    if (err := require_git_repo(repo, config)):
        return fail(err)
    args = ["diff"]
    if cfg_bool(payload.get("staged"), False):
        args.append("--cached")
    base = str(payload.get("base") or "").strip()
    if base:
        name, err = safe_name(base, "base")
        if err:
            return fail(err)
        args.append(name)
    if payload.get("path"):
        path = str(payload["path"]).strip()
        if not is_within(os.path.join(repo, path), repo):
            return fail(f"path ausserhalb des Repos: {path}")
        args += ["--", path]
    out, err = run_git(repo, args, config)
    if err:
        return fail(err)
    return ok(out if out else "(kein Diff — nichts geaendert)")


def git_log(payload, config):
    repo, err = resolve_repo(payload, config)
    if err:
        return fail(err)
    if (err := require_git_repo(repo, config)):
        return fail(err)
    limit = max(1, min(cfg_int(payload.get("limit"), 20), 200))
    args = ["log", f"-{limit}", "--date=short", "--pretty=format:%h %ad %an: %s"]
    if payload.get("path"):
        args += ["--", str(payload["path"]).strip()]
    out, err = run_git(repo, args, config, check=False)
    if err:
        return fail(err)
    return ok(out if out else "(keine Commits)")


def git_branch(payload, config):
    repo, err = resolve_repo(payload, config)
    if err:
        return fail(err)
    if (err := require_git_repo(repo, config)):
        return fail(err)
    action = str(payload.get("action") or "list").strip().lower()
    if action == "list":
        out, err = run_git(repo, ["branch", "-vv"], config)
        return fail(err) if err else ok(out or "(keine Branches)")
    name, err = safe_name(payload.get("name"), "Branch-Name")
    if err:
        return fail(err)
    if action == "create":
        out, err = run_git(repo, ["switch", "-c", name], config)
    elif action == "switch":
        out, err = run_git(repo, ["switch", name], config)
    else:
        return fail(f"Unbekannte action: {action} (list|create|switch)")
    return fail(err) if err else ok(out or f"Branch: {name}")


def git_commit(payload, config):
    repo, err = resolve_repo(payload, config)
    if err:
        return fail(err)
    if (err := require_git_repo(repo, config)):
        return fail(err)
    message = str(payload.get("message") or "").strip()
    if not message:
        return fail('Commit-Message fehlt. Beispiel: {"message": "fix: ..."}')
    add = payload.get("add", "all")
    if add == "all" or add is True:
        _, err = run_git(repo, ["add", "-A"], config)
        if err:
            return fail(err)
    elif isinstance(add, list):
        for p in add:
            p = str(p).strip()
            if not p or p.startswith("-") or not is_within(os.path.join(repo, p), repo):
                return fail(f"Ungueltiger add-Pfad: {p!r}")
            _, err = run_git(repo, ["add", "--", p], config)
            if err:
                return fail(err)
    staged, _ = run_git(repo, ["diff", "--cached", "--stat"], config, check=False)
    if not (staged or "").strip():
        return fail("Nichts zu committen (staging leer). Erst Dateien aendern oder add pruefen.")
    out, err = run_git(repo, ["commit", "-m", message], config)
    if err:
        return fail(err)
    return ok(f"{out}\n\nstaged gewesen:\n{staged}")


def git_push(payload, config):
    if not cfg_bool(config.get("allow_push"), True):
        return fail("git push ist per Modul-Setting deaktiviert (allow_push=false)")
    repo, err = resolve_repo(payload, config)
    if err:
        return fail(err)
    if (err := require_git_repo(repo, config)):
        return fail(err)
    remote, err = safe_name(payload.get("remote") or "origin", "Remote")
    if err:
        return fail(err)
    branch = str(payload.get("branch") or "").strip()
    if not branch:
        branch, _ = run_git(repo, ["branch", "--show-current"], config, check=False)
        branch = (branch or "").strip()
    name, err = safe_name(branch, "Branch")
    if err:
        return fail(err)
    args = ["push", remote, name]
    if cfg_bool(payload.get("set_upstream"), False):
        args = ["push", "-u", remote, name]
    out, err = run_git(repo, args, config)
    return fail(err) if err else ok(out or f"Gepusht: {remote}/{name}")


def git_pull(payload, config):
    repo, err = resolve_repo(payload, config)
    if err:
        return fail(err)
    if (err := require_git_repo(repo, config)):
        return fail(err)
    remote, err = safe_name(payload.get("remote") or "origin", "Remote")
    if err:
        return fail(err)
    args = ["pull", "--rebase" if cfg_bool(payload.get("rebase"), True) else "--ff-only", remote]
    out, err = run_git(repo, args, config)
    return fail(err) if err else ok(out or "Aktuell.")


def git_init(payload, config):
    repo, err = resolve_repo(payload, config, create=True)
    if err:
        return fail(err)
    if os.path.isdir(os.path.join(repo, ".git")):
        return fail(f"Ist bereits ein Git-Repo: {repo}")
    branch, err = safe_name(payload.get("initial_branch") or "main", "Branch")
    if err:
        return fail(err)
    out, err = run_git(repo, ["init", "-b", branch], config)
    return fail(err) if err else ok(out or f"Git-Repo initialisiert: {repo} ({branch})")


# ─── GitHub-API (PRs) ────────────────────────────────────────────────────────

def load_github_token(config):
    """Token: erst Modul-Setting, sonst Vault, sonst ~/.git-credentials.

    Bewusst OHNE Prozess-Cache: der Modul-Prozess lebt im Pool tagelang,
    ein gecachter Token wuerde Rotation/Config-Aenderungen verstecken.
    """
    direct = str(config.get("github_token") or "").strip()
    if direct:
        return direct
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "agent-data" / "config.json",
        Path("agent-data/config.json"),
    ]
    for path in candidates:
        try:
            if not path.exists():
                continue
            vault = json.loads(path.read_text(encoding="utf-8")).get("api_key_vault", [])
            for entry in vault:
                name = (str(entry.get("name", "")) + str(entry.get("provider", ""))).lower()
                if entry.get("id") == "github" or "github" in name:
                    secret = str(entry.get("secret") or "").strip()
                    if secret:
                        return secret
        except Exception:
            continue
    # Fallback: ~/.git-credentials (dort liegt der Push-Token ohnehin)
    try:
        for line in Path.home().joinpath(".git-credentials").read_text().splitlines():
            m = re.match(r"https://[^:]+:([^@]+)@github\.com", line.strip())
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def origin_owner_repo(repo, config):
    url, err = run_git(repo, ["remote", "get-url", "origin"], config)
    if err:
        return None, None, "Kein origin-Remote konfiguriert"
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", url.strip())
    if not m:
        return None, None, f"origin ist kein GitHub-Remote: {url.strip()}"
    return m.group(1), m.group(2), None


def github_request(method, api_path, token, body=None, timeout=30):
    req = urllib.request.Request(
        "https://api.github.com" + api_path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-platform-git-tools",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def pr_create(payload, config):
    repo, err = resolve_repo(payload, config)
    if err:
        return fail(err)
    if (err := require_git_repo(repo, config)):
        return fail(err)
    token = load_github_token(config)
    if not token:
        return fail("Kein GitHub-Token (Modul-Setting github_token oder Vault-Eintrag 'github')")
    owner, name, err = origin_owner_repo(repo, config)
    if err:
        return fail(err)
    title = str(payload.get("title") or "").strip()
    if not title:
        return fail('PR-Titel fehlt. Beispiel: {"title": "Fix login timeout"}')
    head = str(payload.get("head") or "").strip()
    if not head:
        head, _ = run_git(repo, ["branch", "--show-current"], config, check=False)
        head = (head or "").strip()
    base = str(payload.get("base") or "main").strip()
    for label, v in (("head", head), ("base", base)):
        if not SAFE_NAME_RE.match(v or ""):
            return fail(f"Ungueltiger {label}-Branch: {v!r}")
    if head == base:
        return fail(f"head == base ({head}) — erst einen Feature-Branch anlegen (git_tools.branch create)")
    try:
        pr = github_request(
            "POST",
            f"/repos/{owner}/{name}/pulls",
            token,
            {"title": title, "body": str(payload.get("body") or ""), "head": head, "base": base},
        )
        return ok(f"PR #{pr.get('number')} erstellt: {pr.get('html_url')}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        return fail(f"GitHub API HTTP {e.code}: {redact(detail)}")
    except Exception as e:
        return fail(f"GitHub API Fehler: {redact(str(e))}")


def pr_list(payload, config):
    repo, err = resolve_repo(payload, config)
    if err:
        return fail(err)
    if (err := require_git_repo(repo, config)):
        return fail(err)
    token = load_github_token(config)
    if not token:
        return fail("Kein GitHub-Token (Modul-Setting github_token oder Vault-Eintrag 'github')")
    owner, name, err = origin_owner_repo(repo, config)
    if err:
        return fail(err)
    state = str(payload.get("state") or "open").strip().lower()
    if state not in {"open", "closed", "all"}:
        state = "open"
    try:
        prs = github_request("GET", f"/repos/{owner}/{name}/pulls?state={state}&per_page=20", token)
        if not prs:
            return ok(f"Keine {state}-PRs in {owner}/{name}")
        lines = [f"#{p['number']} [{p['state']}] {p['title']} ({p['head']['ref']} -> {p['base']['ref']}) {p['html_url']}" for p in prs]
        return ok("\n".join(lines))
    except urllib.error.HTTPError as e:
        return fail(f"GitHub API HTTP {e.code}")
    except Exception as e:
        return fail(f"GitHub API Fehler: {redact(str(e))}")


# ─── Dispatch ────────────────────────────────────────────────────────────────

HANDLERS = {
    "git_tools.status": git_status,
    "git_tools.diff": git_diff,
    "git_tools.log": git_log,
    "git_tools.branch": git_branch,
    "git_tools.commit": git_commit,
    "git_tools.push": git_push,
    "git_tools.pull": git_pull,
    "git_tools.init": git_init,
    "git_tools.pr_create": pr_create,
    "git_tools.pr_list": pr_list,
}


def handle_tool(tool_name, params, config):
    handler = HANDLERS.get(tool_name)
    if not handler:
        return fail(f"Unbekanntes Tool: {tool_name}")
    payload = parse_payload(params if isinstance(params, list) else [params])
    if "_parse_error" in payload:
        return fail(
            "Parameter muss ein JSON-Objekt mit benannten Keys sein, z.B. "
            '{"repo": "myproject", "message": "fix: ..."}. Erhalten: ' + payload["_parse_error"][:200]
        )
    try:
        return handler(payload, config or {})
    except Exception as e:
        return fail(f"git_tools Fehler: {redact(str(e))}")


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
        except Exception as e:
            print(json.dumps({"error": str(e)}), flush=True)
