# Agent Platform — Python Modules

Each module is a directory containing a `module.py` file.

## Structure

```
modules/
  my_module/
    module.py      <- Required: standard interface
    ...            <- Optional: additional files
```

## module.py Format

```python
MODULE = {
    "name": "my_module",
    "description": "What the module does",
    "version": "1.0",
    "settings": {
        "key": {"type": "string|number|bool|list|text|password|select", "label": "Display name", "default": "value"},
    },
    "tools": [
        {"name": "my_module.action", "description": "What it does", "params": ["param1", "param2"]},
    ],
}

def handle_tool(tool_name: str, params: list, config: dict) -> dict:
    """Called by the agent when the LLM uses this tool.
    Return: {"success": True/False, "data": "result text"}
    """
    return {"success": True, "data": "OK"}
```

## Communication

The Rust core invokes `module.py` via JSON over stdin/stdout:
- Input:  `{"action": "handle_tool", "tool": "name", "params": [...], "config": {...}}`
- Output: `{"success": true, "data": "..."}`

Or for metadata:
- Input:  `{"action": "describe"}`
- Output: the `MODULE` dict

## Tips

- Put secrets (API keys, passwords) behind `"type": "password"` so the web UI redacts them in API responses.
- Emit compact `data` (the LLM pays tokens to read it) and use `success: false` with an error string when something fails — the agent will surface it as a task error.
- Do not spin up long-running threads inside `handle_tool`; the process is pooled and reused, and leaked threads will outlive a single tool call.
