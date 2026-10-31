"""Claude Code Stop hook: read Claude's last reply out loud in the Northern English voice.

Pulls only the spoken-worthy prose out of the reply - no code blocks, tables, inline code, file paths, URLs,
commands or symbols - splits it into paragraphs, and starts the player window (claude_voice_player.pyw) with
its Skip paragraph / Stop buttons. Returns at once so Claude is never held up.
Off switch: a file named OFF in this folder (or tell Claude "voice off").
"""
import json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYER = os.path.join(HERE, "claude_voice_player.pyw")
TEXT_FILE = os.path.join(HERE, "last_reply.json")
PID_FILE = os.path.join(HERE, "player.pid")
MAX_PARAGRAPHS = 12


def last_reply(transcript_path):
    """All of Claude's text blocks written since the last real message the user sent (by time - the file
    isn't always in writing order)."""
    last_prompt, last_action, texts = "", "", []
    with open(transcript_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message") or {}
            content = m.get("content")
            ts = d.get("timestamp") or ""
            if d.get("type") == "user":
                # a real new message has a promptSource - tool results and the notes tools add mid-reply are
                # also stored as "user" entries and must not count
                if d.get("promptSource") and not d.get("isMeta"):
                    last_prompt = max(last_prompt, ts)
            elif d.get("type") == "assistant" and isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        texts.append((ts, c.get("text", "")))
                    elif c.get("type") == "tool_use":
                        last_action = max(last_action, ts)
    # only the final answer: what Claude wrote after his last tool call of this reply (not the
    # "checking this, testing that" notes in between)
    start = max(last_prompt, last_action if last_action > last_prompt else "")
    return "\n\n".join(t for ts, t in sorted(texts) if ts > start and t.strip())


def speakable(text):
    """Prose only, as a list of paragraphs."""
    text = re.sub(r"```.*?```", "\n\n", text, flags=re.S)                  # code blocks
    out, short = [], []
    for para in re.split(r"\n\s*\n", text):
        lines = []
        for line in para.splitlines():
            s = line.strip()
            if not s or s.startswith("|") or re.fullmatch(r"[-=*_|: ]{3,}", s):   # tables, rules
                continue
            lines.append(s)
        p = " ".join(lines)
        p = re.sub(r"`[^`]*`", "", p)                                      # inline code, names, commands
        p = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", p)                     # markdown links -> their words
        p = re.sub(r"https?://\S+", "", p)
        p = re.sub(r"\b[A-Za-z]:\\[^\s,;)]*", "", p)                       # Windows paths
        p = re.sub(r"(?<![\w])[~.]?/[\w.\-/]+", "", p)                     # unix-style paths
        p = re.sub(r"\S+\.(py|js|json|bat|ps1|md|txt|wav|onnx|docx|html|log|jsonl)\b", "", p)
        p = re.sub(r"^#+\s*", "", p)
        p = re.sub(r"^\s*([-*]|\d+\.)\s+", "", p)
        p = re.sub(r"[*_#>~|`]+", "", p)
        p = re.sub(r"[^\x20-\x7E‘-”–—…]", " ", p)  # emoji and odd symbols
        p = re.sub(r"\(\s*\)", "", p)
        p = re.sub(r"\s+([,.;:!?])", r"\1", p)
        p = re.sub(r"\s{2,}", " ", p).strip(" -:;,")
        if len(re.findall(r"[A-Za-z]{2,}", p)) >= 3:                       # real words, not leftovers
            out.append(p)
        elif re.search(r"[A-Za-z]{2,}", p):
            short.append(p)
    # a short answer that's the whole reply still gets read (a one-line
    # "It's 11:05 AM." used to be dropped, and nothing was read)
    return (out or short)[:MAX_PARAGRAPHS]


def player_running() -> bool:
    try:
        with open(PID_FILE) as f:
            pid = f.read().strip()
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                             creationflags=0x08000000).stdout
        return "pythonw" in out.lower() or "python" in out.lower()
    except (OSError, ValueError):
        return False


def open_player():
    """Start the voice window if it isn't open (it then waits for Claude's next reply)."""
    if player_running():
        return
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    subprocess.Popen([pythonw if os.path.exists(pythonw) else sys.executable, PLAYER, TEXT_FILE],
                     creationflags=0x00000008 | 0x00000200, close_fds=True)


def save_chat(path, cwd):
    """Save this chat to the claude_chats database in the background, so Claude isn't held up."""
    saver = os.path.join(HERE, "claude_chat_save.py")
    if not os.path.exists(os.path.join(HERE, "db.json")):
        return
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    subprocess.Popen([pythonw if os.path.exists(pythonw) else sys.executable, saver, path, cwd or ""],
                     creationflags=0x00000008 | 0x00000200, close_fds=True)


def main():
    if os.path.exists(os.path.join(HERE, "OFF")):
        return
    if "--open" in sys.argv:   # SessionStart hook: open the window when Claude opens
        open_player()
        return
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    path = data.get("transcript_path")
    if not path or not os.path.exists(path):
        return
    save_chat(path, data.get("cwd"))
    paras = speakable(last_reply(path))
    if not paras:
        return
    # the player window stays open and picks up the new reply by itself (it watches TEXT_FILE)
    tmp = TEXT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(paras, f)
    os.replace(tmp, TEXT_FILE)
    if player_running():
        return
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    subprocess.Popen([pythonw if os.path.exists(pythonw) else sys.executable, PLAYER, TEXT_FILE],
                     creationflags=0x00000008 | 0x00000200, close_fds=True)   # detached, own group


if __name__ == "__main__":
    main()
