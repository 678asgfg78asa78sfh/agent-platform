# Agent Platform — Python Module

Jedes Modul ist ein Ordner mit einer `module.py` Datei.

## Struktur

```
modules/
  mein_modul/
    module.py      <- Pflicht: Standard-Interface
    ...            <- Optional: weitere Dateien
```

## module.py Format

```python
MODULE = {
    "name": "mein_modul",
    "description": "Was das Modul macht",
    "version": "1.0",
    "settings": {
        "key": {"type": "string|number|bool|list|text|password|select", "label": "Anzeigename", "default": "wert"},
    },
    "tools": [
        {"name": "mein_modul.aktion", "description": "Was es tut", "params": ["param1", "param2"]},
    ],
}

def handle_tool(tool_name: str, params: list, config: dict) -> dict:
    """Wird vom Agent aufgerufen wenn das LLM dieses Tool nutzt.
    Return: {"success": True/False, "data": "Ergebnis-Text"}
    """
    return {"success": True, "data": "OK"}
```

## Kommunikation

Der Rust-Core ruft module.py per stdin/stdout JSON auf:
- Input:  {"action": "handle_tool", "tool": "name", "params": [...], "config": {...}}
- Output: {"success": true, "data": "..."}

Oder fuer Metadaten:
- Input:  {"action": "describe"}
- Output: MODULE dict
```
