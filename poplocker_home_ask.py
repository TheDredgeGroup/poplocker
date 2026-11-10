"""Pop Locker Home: hands a sentence about the house to Claude and gets a short spoken answer back.

Claude is run by itself for this (no window, no open chat needed), in the home/ folder, and can use ONLY the home
tools in poplocker_home.py - no files, no programs, no web. That matters because the sentence may come from a
phone, a sensor or a timer through the door, not from someone sitting at the PC.

It needs Claude Code's command line, signed in once (run "claude" in a terminal and type /login). Until then
NotReady is raised and the ears type the request into the open Claude chat instead.

  python poplocker_home_ask.py "turn the porch light off"     try it by hand
"""
import glob, json, os, re, subprocess, sys, threading, time

import poplocker_home as home

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "player_settings.json")
REPLY_FILE = os.path.join(HERE, "last_reply.json")
RECENT_FILE = os.path.join(home.HOME_DIR, "recent.json")
MCP_FILE = os.path.join(home.HOME_DIR, "mcp.json")
REMEMBER_MINUTES, REMEMBER_TURNS = 15, 4      # "make it warmer" works after "turn on the lamp"; then it's forgotten
one_at_a_time = threading.Lock()

RULES = """You are the voice of this house's smart-home controller. A person spoke (or a timer or sensor sent) the \
request below; your answer is read out loud by a speaker.

- Use the home tools to do what was asked. Call list_devices first if you aren't sure of a device's or room's exact \
name, then act. Pick the device or room that the person plainly means; ask only when it's a real toss-up.
- Say what really happened, from what the tools answered. If a tool answer says FAILED, say that it didn't work and \
why, in plain words. Never say something is done unless a tool answer shows it.
- Answer in one or two short spoken sentences. No lists, no markdown, no symbols, no code, no addresses.
- You have only the home tools. If asked for anything else, say that this is the home controller and it's a \
question for the main Claude chat.
- It is now {now}."""


class NotReady(Exception):
    """Claude's command line isn't there or isn't signed in - the message says which, in plain words."""


def setting(name, default=""):
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f).get(name) or default
    except (OSError, ValueError):
        return default


def find_claude():
    """Claude Code's command line: a path set in the settings, on PATH, in the usual install places, or the copy
    that comes inside the Claude desktop app (newest version)."""
    import shutil
    given = setting("claude_path")
    if given and os.path.isfile(given):
        return given
    for name in ("claude", "claude.exe", "claude.cmd"):
        hit = shutil.which(name)
        if hit:
            return hit
    home_dir = os.path.expanduser("~")
    for p in (os.path.join(home_dir, ".local", "bin", "claude.exe"), os.path.join(home_dir, ".local", "bin", "claude"),
              os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")):
        if os.path.isfile(p):
            return p
    inside = glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Claude-Data", "*", "claude-code", "*", "*", "claude.exe"))
    inside += glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Claude-Data", "*", "claude-code", "*", "claude.exe"))

    def version(p):
        m = re.search(r"claude-code[\\/](\d+)\.(\d+)\.(\d+)", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    return max(inside, key=version) if inside else None


def _python():
    """python.exe (not the windowless pythonw.exe) to run the tools with."""
    exe = sys.executable
    plain = os.path.join(os.path.dirname(exe), "python.exe")
    return plain if os.path.basename(exe).lower().startswith("pythonw") and os.path.isfile(plain) else exe


def _recent():
    try:
        with open(RECENT_FILE, encoding="utf-8") as f:
            turns = json.load(f)
    except (OSError, ValueError):
        return []
    cutoff = time.time() - REMEMBER_MINUTES * 60
    return [t for t in turns if isinstance(t, dict) and t.get("at", 0) > cutoff][-REMEMBER_TURNS:]


def _remember(asked, answered):
    turns = _recent() + [{"at": time.time(), "asked": asked, "answered": answered}]
    try:
        with open(RECENT_FILE, "w", encoding="utf-8") as f:
            json.dump(turns[-REMEMBER_TURNS:], f)
    except OSError:
        pass


def speak(text):
    """Have the Pop Locker window read it out (the same file Claude's normal replies arrive in)."""
    paras = [p.strip() for p in str(text).split("\n") if p.strip()]
    if not paras:
        return
    tmp = REPLY_FILE + ".home.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(paras, f)
    os.replace(tmp, REPLY_FILE)


def ask(words, source="voice", say=True, timeout=120):
    """-> Claude's short answer after it has done what was asked. Raises NotReady when Claude can't be run."""
    exe = find_claude()
    if not exe:
        raise NotReady("Claude Code's command line isn't installed on this PC.")
    os.makedirs(home.HOME_DIR, exist_ok=True)
    with open(MCP_FILE, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"home": {"command": _python(), "args": [os.path.join(HERE, "poplocker_home.py"), "--mcp"]}}}, f)
    rules = RULES.format(now=time.strftime("%A %d %B %Y, %H:%M"))
    recent = _recent()
    if recent:
        rules += "\n\nThe last few things said to you, oldest first (for words like \"it\" and \"again\"):\n" + "\n".join(
            f"- Asked: {t['asked']}\n  You answered: {t['answered']}" for t in recent)
    if source != "voice":
        rules += f"\n\nThis request came from: {source}."
    cmd = [exe, "-p", str(words), "--model", setting("home_model", "haiku"), "--output-format", "json",
           "--mcp-config", MCP_FILE, "--strict-mcp-config", "--tools", "", "--allowedTools", "mcp__home",
           "--settings", '{"disableAllHooks": true}', "--append-system-prompt", rules]
    started = time.time()
    with one_at_a_time:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                               cwd=home.HOME_DIR, stdin=subprocess.DEVNULL,
                               creationflags=0x08000000 if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            home.log(f"ask timed out: {words[:80]}")
            return _done(words, "Sorry, that took too long and I gave up.", say, started, keep=False)
        except OSError as e:
            raise NotReady(f"Claude's command line couldn't be started: {e}")
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {}
    text = str(out.get("result") or "").strip()
    if out.get("is_error") or not text:
        why = text or (p.stderr or "").strip()[-300:] or f"exit code {p.returncode}"
        home.log(f"ask failed: {why[:300]}")
        if re.search(r"not logged in|/login|invalid api key|authentication", why, re.I):
            raise NotReady("Claude's command line isn't signed in. Run claude in a terminal and type /login, once.")
        return _done(words, "Sorry, the home controller hit a problem and couldn't do that.", say, started, keep=False)
    home.log(f"asked ({source}): {words[:120]} -> {text[:200]}")
    return _done(words, text, say, started)


def _done(words, text, say, started, keep=True):
    if keep:
        _remember(words, text)
    if say:
        try:
            # if a hook already handed this answer to the reader, don't have it read twice
            if os.path.getmtime(REPLY_FILE) <= started:
                speak(text)
        except OSError:
            speak(text)
    return text


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    try:
        print(ask(" ".join(sys.argv[1:]), say=False))
    except NotReady as e:
        print(e)
        sys.exit(1)
