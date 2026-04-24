"""System-Info Modul — liefert Infos über CPU, RAM, Disk, Netzwerk."""
import json, sys, os, platform

MODULE = {
    "name": "sysinfo",
    "description": "Zeigt System-Informationen (CPU, RAM, Disk, OS)",
    "version": "1.0",
    "settings": {},
    "tools": [
        {"name": "sysinfo.overview", "description": "Zeigt eine Uebersicht ueber das System (OS, CPU, RAM, Disk)", "params": []},
        {"name": "sysinfo.time", "description": "Zeigt das aktuelle Datum und die Uhrzeit", "params": []},
        {"name": "sysinfo.processes", "description": "Zeigt die Top-Prozesse nach CPU/RAM Verbrauch", "params": ["count"]},
    ],
}

def handle_tool(tool_name, params, config):
    if tool_name == "sysinfo.overview":
        return _overview()
    elif tool_name == "sysinfo.time":
        from datetime import datetime
        now = datetime.now()
        return {"success": True, "data": now.strftime("Datum: %d.%m.%Y, Uhrzeit: %H:%M:%S, Wochentag: %A")}
    elif tool_name == "sysinfo.processes":
        count = int(params[0]) if params else 10
        return _processes(count)
    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}

def _overview():
    try:
        info = []
        info.append(f"OS: {platform.system()} {platform.release()}")
        info.append(f"Hostname: {platform.node()}")
        info.append(f"Arch: {platform.machine()}")
        info.append(f"Python: {platform.python_version()}")

        # CPU
        try:
            with open("/proc/cpuinfo") as f:
                cpus = [l for l in f if l.startswith("model name")]
                if cpus:
                    info.append(f"CPU: {cpus[0].split(':')[1].strip()} ({len(cpus)} cores)")
        except Exception:
            pass

        # RAM
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
                total = int([l for l in lines if "MemTotal" in l][0].split()[1]) // 1024
                avail = int([l for l in lines if "MemAvailable" in l][0].split()[1]) // 1024
                used = total - avail
                info.append(f"RAM: {used}MB / {total}MB ({100*used//total}%)")
        except Exception:
            pass

        # Disk
        try:
            st = os.statvfs("/")
            total_gb = (st.f_blocks * st.f_frsize) / (1024**3)
            free_gb = (st.f_bavail * st.f_frsize) / (1024**3)
            used_gb = total_gb - free_gb
            info.append(f"Disk /: {used_gb:.1f}GB / {total_gb:.1f}GB ({100*used_gb/total_gb:.0f}%)")
        except Exception:
            pass

        # Load
        try:
            load1, load5, load15 = os.getloadavg()
            info.append(f"Load: {load1:.2f} / {load5:.2f} / {load15:.2f}")
        except Exception:
            pass

        # Uptime
        try:
            with open("/proc/uptime") as f:
                secs = float(f.read().split()[0])
                days = int(secs // 86400)
                hours = int((secs % 86400) // 3600)
                info.append(f"Uptime: {days}d {hours}h")
        except Exception:
            pass

        return {"success": True, "data": "\n".join(info)}
    except Exception as e:
        return {"success": False, "data": str(e)}

def _processes(count):
    try:
        import subprocess
        result = subprocess.run(
            ["ps", "aux", "--sort=-%mem"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")
        top = lines[:count+1]  # header + N lines
        return {"success": True, "data": "\n".join(top)}
    except Exception as e:
        return {"success": False, "data": str(e)}


# === stdin/stdout Interface fuer Rust-Core ===
if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            action = req.get("action", "")
            if action == "describe":
                print(json.dumps(MODULE), flush=True)
            elif action == "handle_tool":
                result = handle_tool(req["tool"], req.get("params", []), req.get("config", {}))
                print(json.dumps(result), flush=True)
            else:
                print(json.dumps({"error": f"Unknown action: {action}"}), flush=True)
        except Exception as e:
            print(json.dumps({"error": str(e)}), flush=True)
