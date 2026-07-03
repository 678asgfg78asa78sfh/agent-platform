"""Tests fuer git_tools: Confinement, Arg-Validierung, Commit-Roundtrip, Redaction.

Laufen ohne Netz und ohne GitHub-Token: python3 -m pytest modules/git_tools/ -q
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import module as git_tools  # noqa: E402


def make_config(home):
    return {"home_dir": home, "command_timeout_s": 30}


def test_repo_outside_home_rejected():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as outside:
        res = git_tools.handle_tool("git_tools.status", ['{"repo": "%s"}' % outside], make_config(home))
        assert not res["success"]
        assert "nicht erlaubt" in res["data"]


def test_init_commit_log_roundtrip():
    with tempfile.TemporaryDirectory() as home:
        cfg = make_config(home)
        res = git_tools.handle_tool("git_tools.init", ["{}"], cfg)
        assert res["success"], res["data"]

        with open(os.path.join(home, "hello.py"), "w") as f:
            f.write("print('hi')\n")

        res = git_tools.handle_tool("git_tools.commit", ['{"message": "feat: hello"}'], cfg)
        assert res["success"], res["data"]

        res = git_tools.handle_tool("git_tools.log", ["{}"], cfg)
        assert res["success"] and "feat: hello" in res["data"]

        res = git_tools.handle_tool("git_tools.status", ["{}"], cfg)
        assert res["success"] and "working tree clean" in res["data"]


def test_commit_without_changes_fails_cleanly():
    with tempfile.TemporaryDirectory() as home:
        cfg = make_config(home)
        git_tools.handle_tool("git_tools.init", ["{}"], cfg)
        res = git_tools.handle_tool("git_tools.commit", ['{"message": "leer"}'], cfg)
        assert not res["success"]
        assert "Nichts zu committen" in res["data"]


def test_branch_name_injection_rejected():
    with tempfile.TemporaryDirectory() as home:
        cfg = make_config(home)
        git_tools.handle_tool("git_tools.init", ["{}"], cfg)
        for evil in ["-f", "--force", "; rm -rf /", "a b"]:
            res = git_tools.handle_tool(
                "git_tools.branch", ['{"action": "create", "name": "%s"}' % evil.replace('"', "")], cfg
            )
            assert not res["success"], f"haette abgelehnt werden muessen: {evil!r}"


def test_add_path_escape_rejected():
    with tempfile.TemporaryDirectory() as home:
        cfg = make_config(home)
        git_tools.handle_tool("git_tools.init", ["{}"], cfg)
        res = git_tools.handle_tool(
            "git_tools.commit", ['{"message": "x", "add": ["../../etc/passwd"]}'], cfg
        )
        assert not res["success"]


def test_push_disabled_by_setting():
    with tempfile.TemporaryDirectory() as home:
        cfg = make_config(home)
        cfg["allow_push"] = False
        git_tools.handle_tool("git_tools.init", ["{}"], cfg)
        res = git_tools.handle_tool("git_tools.push", ["{}"], cfg)
        assert not res["success"] and "deaktiviert" in res["data"]


def test_plain_dir_reports_kein_git_repo():
    """Nicht-Repo muss die 'Kein Git-Repo'-Meldung bekommen, nicht die Nested-Meldung."""
    with tempfile.TemporaryDirectory() as home:
        res = git_tools.handle_tool("git_tools.status", ["{}"], make_config(home))
        assert not res["success"]
        assert "Kein Git-Repo" in res["data"], res["data"]


def test_nested_dir_in_outer_repo_not_captured():
    """Home liegt IN einem fremden Repo: git add -A darf nie ins aeussere Repo stagen."""
    with tempfile.TemporaryDirectory() as outer:
        cfg_outer = make_config(outer)
        git_tools.handle_tool("git_tools.init", ["{}"], cfg_outer)
        sub = os.path.join(outer, "workspace")
        os.makedirs(sub)
        cfg_sub = make_config(sub)
        res = git_tools.handle_tool("git_tools.status", ["{}"], cfg_sub)
        assert not res["success"], res["data"]
        assert "innerhalb von" in res["data"]
        res = git_tools.handle_tool("git_tools.commit", ['{"message": "boese"}'], cfg_sub)
        assert not res["success"]


def test_hooks_do_not_execute():
    """Repo-eigene Hooks (z.B. aus geklontem Fremd-Repo) duerfen nie laufen."""
    with tempfile.TemporaryDirectory() as home:
        cfg = make_config(home)
        git_tools.handle_tool("git_tools.init", ["{}"], cfg)
        marker = os.path.join(home, "hook-ran")
        hook = os.path.join(home, ".git", "hooks", "pre-commit")
        with open(hook, "w") as f:
            f.write(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        os.chmod(hook, 0o755)
        with open(os.path.join(home, "a.txt"), "w") as f:
            f.write("x\n")
        res = git_tools.handle_tool("git_tools.commit", ['{"message": "test"}'], cfg)
        assert res["success"], res["data"]
        assert not os.path.exists(marker), "pre-commit-Hook wurde ausgefuehrt!"


def test_redact_credentials():
    assert git_tools.redact("https://user:ghp_secret123@github.com/x/y") == "https://***@github.com/x/y"
    assert git_tools.redact("https://ghp_tok@github.com/x") == "https://***@github.com/x"
    assert git_tools.redact("https://github.com/x/y") == "https://github.com/x/y"


def test_bare_string_param_is_repo():
    payload = git_tools.parse_payload(["myproject"])
    assert payload == {"repo": "myproject"}


def test_broken_json_gives_helpful_error():
    with tempfile.TemporaryDirectory() as home:
        res = git_tools.handle_tool("git_tools.status", ['{"repo": "x",}'], make_config(home))
        assert not res["success"]
        assert "JSON-Objekt" in res["data"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"OK {fn.__name__}")
    print(f"{len(fns)} Tests gruen")
