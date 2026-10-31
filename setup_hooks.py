"""Adds the voice window's two hooks to this user's Claude Code settings, keeping everything already there.
Stop: read each finished reply aloud. SessionStart: open the window when Claude opens."""
import json, os, sys

here = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
py = f"{here}/python/python.exe"          # the Python that comes with the app
hook = f"{here}/claude_voice_hook.py"
wanted = {
    "Stop": f'"{py}" "{hook}"',
    "SessionStart": f'"{py}" "{hook}" --open',
}
path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    with open(path, encoding="utf-8") as f:
        settings = json.load(f)
except (OSError, ValueError):
    settings = {}
hooks = settings.setdefault("hooks", {})
for event, command in wanted.items():
    groups = hooks.setdefault(event, [])
    # drop an older copy of our own hook (a reinstall), keep anyone else's
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if "claude_voice_hook.py" not in h.get("command", "")]
    groups[:] = [g for g in groups if g.get("hooks")]
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": 20}]})
with open(path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
print("Claude Code hooks added to", path)
