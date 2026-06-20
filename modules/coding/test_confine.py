"""
Test: coding module honors confine_home_only (SECURE-Compartments R5).

When confine_home_only=True is in the config, allow_project_root must be
treated as False: the workspace cannot widen to project_root, and absolute
paths outside the module home are rejected.

This is a direct unit test of `workspace_root`/`resolve_path` (module.py has a
`__main__` guard, so importing it does not start the stdin loop). The
`root2 != "/"` assertion fails without the confine_home_only gate.
"""
import importlib.util
import os
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "coding_mod", os.path.join(REPO, "modules", "coding", "module.py")
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def main():
    home = tempfile.mkdtemp()
    base = {"home_dir": home, "project_root": "/", "allow_project_root": True}

    # Baseline: WITHOUT confine, $PROJECT_ROOT widens the workspace to "/".
    root, err = m.workspace_root(dict(base), {"workspace": "$PROJECT_ROOT"})
    assert root == os.path.realpath("/"), f"baseline expected '/', got {root!r} (err={err!r})"

    # WITH confine_home_only: widening is disabled -> workspace stays inside home.
    cfg = dict(base)
    cfg["confine_home_only"] = True
    cfg["secure"] = "acme"
    root2, err2 = m.workspace_root(cfg, {"workspace": "$PROJECT_ROOT"})
    assert root2 != os.path.realpath("/"), f"confine breached: workspace widened to {root2!r}"
    assert m.is_within(root2, os.path.realpath(home)), f"{root2!r} not within home {home!r}"

    # An absolute path outside home is rejected by resolve_path.
    hroot, _ = m.workspace_root(cfg, {})  # no workspace -> home
    abs_path, perr = m.resolve_path("/etc/hostname", cfg, hroot)
    assert abs_path is None and perr, f"expected denial for /etc/hostname, got {abs_path!r}"

    print("OK confine_home_only: project-root widening disabled + out-of-home path denied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
