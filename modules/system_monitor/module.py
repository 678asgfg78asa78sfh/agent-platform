"""Liest laufende Prozesse und einfache Systemauslastung."""

import json
import os
import platform
import subprocess
import sys


MODULE = {
    "name": "system_monitor",
    "description": "Liest laufende Prozesse, CPU/RAM-Verbrauch und grundlegende Systemauslastung.",
    "version": "1.1",
    "settings": {
        "default_count": {"type": "number", "label": "Standardanzahl Prozesse", "default": 15},
    },
    "tools": [
        {
            "name": "system_monitor.sysinfo.processes",
            "description": "Zeigt die Top-Prozesse nach CPU-Verbrauch inklusive RAM, Laufzeit und Kommando.",
            "params": ["count"],
        }
    ],
}


def handle_tool(tool_name, params, config):
    if tool_name == "system_monitor.sysinfo.processes":
        count = _count_from_params(params, config)
        return {"success": True, "data": _process_report(count)}

    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}


def _count_from_params(params, config):
    raw = None
    if params:
        raw = params[0]
    if raw in (None, ""):
        raw = (config or {}).get("default_count", 15)
    try:
        return max(1, min(50, int(raw)))
    except Exception:
        return 15


def _process_report(count):
    processes = _ps_processes(count)
    source = "ps"
    if not processes:
        processes = _proc_processes(count)
        source = "/proc"

    lines = [
        "SYSTEM_MONITOR_PROCESS_REPORT",
        f"Host: {platform.node() or 'unknown'}",
        f"OS: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Load: {_load_average()}",
        _memory_summary(),
        "",
        f"Top Prozesse nach CPU ({source}, max {count})",
        "PID      USER          CPU%   MEM%   RSS_MB  ELAPSED      STAT CMD",
    ]

    for proc in processes[:count]:
        lines.append(
            f"{proc['pid']:<8} "
            f"{_truncate(proc['user'], 12):<12} "
            f"{proc['cpu']:>5.1f} "
            f"{proc['mem']:>6.1f} "
            f"{proc['rss_mb']:>8.1f} "
            f"{_truncate(proc['elapsed'], 12):<12} "
            f"{_truncate(proc['stat'], 5):<5} "
            f"{_truncate(proc['cmd'], 120)}"
        )

    if not processes:
        lines.append("Keine Prozessdaten gefunden.")
    return "\n".join(lines)


def _ps_processes(count):
    cmd = [
        "ps",
        "-eo",
        "pid=,ppid=,user=,pcpu=,pmem=,rss=,etime=,stat=,comm=,args=",
        "--sort=-pcpu",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except Exception:
        return []
    if result.returncode != 0:
        return []

    processes = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 9)
        if len(parts) < 9:
            continue
        pid, _ppid, user, cpu, mem, rss, elapsed, stat, comm = parts[:9]
        args = parts[9] if len(parts) > 9 else comm
        processes.append(
            {
                "pid": pid,
                "user": user,
                "cpu": _float(cpu),
                "mem": _float(mem),
                "rss_mb": _float(rss) / 1024.0,
                "elapsed": elapsed,
                "stat": stat,
                "cmd": args or comm,
            }
        )
    processes.sort(key=lambda item: item["cpu"], reverse=True)
    return processes[:count]


def _proc_processes(count):
    processes = []
    for entry in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not entry.isdigit():
            continue
        base = os.path.join("/proc", entry)
        try:
            status = _read_file(os.path.join(base, "status"))
            cmdline = _read_file(os.path.join(base, "cmdline")).replace("\x00", " ").strip()
        except Exception:
            continue
        name = _status_value(status, "Name") or entry
        user = _status_value(status, "Uid") or "?"
        rss_kb = _status_value(status, "VmRSS") or "0 kB"
        rss = _float(rss_kb.split()[0])
        processes.append(
            {
                "pid": entry,
                "user": user,
                "cpu": 0.0,
                "mem": 0.0,
                "rss_mb": rss / 1024.0,
                "elapsed": "?",
                "stat": _status_value(status, "State")[:5] or "?",
                "cmd": cmdline or name,
            }
        )
    processes.sort(key=lambda item: item["rss_mb"], reverse=True)
    return processes[:count]


def _load_average():
    try:
        one, five, fifteen = os.getloadavg()
        return f"{one:.2f} / {five:.2f} / {fifteen:.2f} (1m/5m/15m)"
    except Exception:
        return "nicht verfuegbar"


def _memory_summary():
    meminfo = _read_file("/proc/meminfo")
    total = _meminfo_kb(meminfo, "MemTotal")
    available = _meminfo_kb(meminfo, "MemAvailable")
    swap_total = _meminfo_kb(meminfo, "SwapTotal")
    swap_free = _meminfo_kb(meminfo, "SwapFree")
    if not total:
        return "RAM: nicht verfuegbar"
    used = max(0, total - available)
    pct = (used / total) * 100 if total else 0
    parts = [f"RAM: {_gb(used)} / {_gb(total)} genutzt ({pct:.1f}%)"]
    if swap_total:
        swap_used = max(0, swap_total - swap_free)
        swap_pct = (swap_used / swap_total) * 100
        parts.append(f"Swap: {_gb(swap_used)} / {_gb(swap_total)} genutzt ({swap_pct:.1f}%)")
    return " | ".join(parts)


def _read_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def _status_value(status, key):
    prefix = key + ":"
    for line in status.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _meminfo_kb(meminfo, key):
    value = _status_value(meminfo, key)
    if not value:
        return 0.0
    return _float(value.split()[0])


def _gb(kb):
    return f"{kb / 1024.0 / 1024.0:.1f} GB"


def _float(value):
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return 0.0


def _truncate(value, limit):
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


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
