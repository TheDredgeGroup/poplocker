"""Pop Locker's ears: a mic with a wake word, and a door for your phone to talk into the Claude chat window.

Runs beside the voice player window (which starts it) in this folder's own Python (venv), with faster-whisper
and PyAudio. Two ways in, both ending the same way - the words are pasted into the Claude app's chat box and Enter
is pressed, like a person typing it:

  1. The PC mic: say "Hey Computer, ..." (or press the player's Talk button and just talk). Whisper runs on the CPU
     so the graphics card stays free. "Hey Computer, pause / skip / back / stop / play" work the reader
     instead of being sent.
  2. The iPhone: a Siri Shortcut posts {"text": ...} to http://<this PC>:5055/say with the key from
     ears_key.txt (anyone on the Wi-Fi could otherwise type into Claude). The answer comes back to the phone
     when Claude finishes within ~3 minutes, so Siri can read it; it always plays on the PC too.

Files it shares with the player (all in this folder): ears_off (mic not listening while it exists),
talk_now (record the next thing said as a message, no wake word), control.txt (reader commands for the player),
ears_status.txt (what the player shows under its buttons), ears.pid.
"""
import ctypes, ctypes.wintypes as wt, difflib, json, os, re, threading, time, winsound
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPLY_FILE = os.path.join(HERE, "last_reply.json")
KEY_FILE = os.path.join(HERE, "ears_key.txt")
PORT = 5055
SETTINGS = os.path.join(HERE, "player_settings.json")
DEFAULT_WAKE = "Hey Computer"
PHONE_MAX = 500          # characters of the answer sent back to the phone - the PC reads all of it
RATE, FRAME = 16000, 480          # 30 ms frames
# the "hey" is required: on its own, "Claude" turns up in normal talk and in replies being read out (a second PC heard
# the main PC reading "claude.ai" aloud and sent a message nobody said)
CLAUDE_WAKE = re.compile(r"^\W*(?:hey|hi|hay|okay|ok|a|yo)\W*(?:claude|cloud|claud|clod|clyde|klaud|clawed|claw|clyde)\b\W*(.*)$",
                         re.I | re.S)
CLAUDE_WORDS = {"hey", "hi", "hay", "okay", "ok", "a", "yo", "claude", "cloud", "claud", "clod", "clyde", "klaud",
                "clawed", "claw"}

# the wake word is set in the window and saved in
# player_settings.json; it's re-read whenever that file changes, so a new one works without a restart
wake = {"phrase": None, "words": [], "mtime": None}


def wake_phrase():
    try:
        mtime = os.path.getmtime(SETTINGS)
    except OSError:
        mtime = None
    if mtime != wake["mtime"] or wake["phrase"] is None:
        wake["mtime"] = mtime
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                phrase = (json.load(f).get("wake") or "").strip()
        except (OSError, ValueError):
            phrase = ""
        wake["phrase"] = phrase or DEFAULT_WAKE
        wake["words"] = re.sub(r"[^a-z0-9' ]", " ", wake["phrase"].lower()).split()
    return wake["phrase"]


def is_default_wake():
    return wake["words"] == ["hey", "claude"]


def hint():
    """Whisper's hint: it expects the wake word and the reader commands (without it "Hey Claude, pause" came out
    as "Hey Court Pools")."""
    p = wake_phrase()
    return f"{p}, pause. {p}, skip. {p}, stop."


GREETINGS = {"hey", "hi", "hay", "okay", "ok", "a", "yo", "hello"}


def alike(heard, want):
    """A wake-word word heard right, or close enough - Whisper writes "OK" for "okay", "Jarvus" for "Jarvis"."""
    return (heard in GREETINGS and want in GREETINGS) or difflib.SequenceMatcher(None, heard, want).ratio() >= 0.75


def wake_match(text):
    """What was said after the wake word, or None when it doesn't start with the wake word. A custom wake word
    allows Whisper's near misses (each word at least 75% alike)."""
    wake_phrase()
    if is_default_wake():
        m = CLAUDE_WAKE.match(text)
        return m.group(1).strip() if m else None
    heard = re.findall(r"[a-z0-9']+", text.lower())
    want = wake["words"]
    if len(heard) < len(want):
        return None
    if not all(alike(h, w) for h, w in zip(heard, want)):
        return None
    # cut the text after the last wake word, keeping its original wording
    m = re.match(r"^\W*" + r"\W+".join(r"[a-z0-9']+" for _ in want) + r"\b\W*(.*)$", text, re.I | re.S)
    return m.group(1).strip() if m else ""
def _words(s):
    return re.sub(r"[^a-z' ]+", " ", s.lower()).split()


def echo_of_reply(heard):
    """True when what the mic heard is the window reading Claude's reply aloud (a reply that says "Hey Claude,
    stop" would otherwise send itself back as a message)."""
    try:
        if time.time() - os.path.getmtime(REPLY_FILE) > 900:
            return False
        with open(REPLY_FILE, encoding="utf-8") as f:
            reply = " " + " ".join(_words(" ".join(json.load(f)))) + " "
    except (OSError, ValueError, TypeError):
        return False
    words = _words(heard)
    return len(words) >= 3 and " " + " ".join(words) + " " in reply


CONTROLS = {"pause": "pause", "paws": "pause", "skip": "skip", "next": "skip", "back": "back", "stop": "stop",
            "be quiet": "stop", "shut up": "stop", "play": "play", "resume": "play", "continue": "play",
            "keep going": "play", "go on": "play"}

with open(os.path.join(HERE, "ears.pid"), "w") as f:
    f.write(str(os.getpid()))
if not os.path.exists(KEY_FILE):
    import secrets    # plain words: easy to type into a phone shortcut (a mixed-case key got mistyped)
    words = ["river", "tiger", "maple", "rocket", "cactus", "harbor", "pepper", "falcon", "meadow", "copper",
             "walnut", "canyon", "sunset", "violet", "marble", "badger", "lemon", "comet", "willow", "ember"]
    with open(KEY_FILE, "w") as f:
        f.write("-".join(secrets.choice(words) for _ in range(3)) + f"-{secrets.randbelow(90) + 10}")
KEY = open(KEY_FILE).read().strip()

send_lock = threading.Lock()


def status(text):
    try:
        with open(os.path.join(HERE, "ears_status.txt"), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def log(text):
    try:
        with open(os.path.join(HERE, "ears.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def control(cmd):
    with open(os.path.join(HERE, "control.txt"), "w") as f:
        f.write(cmd)


# ---- typing into the Claude app -------------------------------------------------------------------------------
user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
user32.FindWindowW.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
kernel32.GlobalAlloc.restype = wt.HANDLE
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wt.HANDLE]
kernel32.GlobalUnlock.argtypes = [wt.HANDLE]   # without this a 64-bit handle overflowed and the paste died
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
VK_CONTROL, VK_V, VK_RETURN, VK_MENU = 0x11, 0x56, 0x0D, 0x12
# window sizes and mouse positions in real screen pixels on EVERY monitor (e.g. a 4K at 150% + a 1080p at 100%):
# per-monitor aware, or Windows rescales positions on the screen whose scaling differs and the click misses
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
except Exception:
    user32.SetProcessDPIAware()


def _prompt_box(h, seconds=3):
    """Centre of the Claude app's message box, from the accessibility tree. -> (x, y) or None.
    Asked on a side thread with a time limit: the accessibility call has none, and it has been seen
    to hang for good inside the Claude app - every phone message after it queued up and timed
    out. A stuck lookup is left behind and the usual spot is clicked instead."""
    found = []
    t = threading.Thread(target=lambda: found.append(_ask_prompt_box(h)), daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        log(f"send: the Claude app didn't say where its message box is within {seconds}s - clicking the usual spot")
    return found[0] if found else None


def _ask_prompt_box(h):
    try:
        import comtypes
        comtypes.CoInitialize()          # this runs on its own thread
    except Exception:
        pass
    try:
        from pywinauto import Desktop
        win = Desktop(backend="uia").window(handle=h)
        for e in win.descendants(control_type="Edit"):
            if (e.element_info.name or "") == "Prompt":
                r = e.rectangle()
                if r.width() > 20 and r.height() > 5:
                    return (r.left + min(r.width() // 2, 200), (r.top + r.bottom) // 2)
    except Exception:
        pass
    return None


def _click_message_box(h):
    """Click inside the chat's message box, so the paste can't land on another control (it sometimes
    hit the "New" chat button). The box sits centred low in the window: ~47% across, ~70 px (at 100%
    scaling) up from the bottom - measured on a 4K screen with the sidebar and side panel open."""
    r = wt.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    try:
        scale = user32.GetDpiForWindow(h) / 96.0
    except Exception:
        scale = 1.0
    x = r.left + int((r.right - r.left) * 0.47)
    y = r.bottom - int(70 * scale)
    # better: ask Windows' accessibility tree where the box really is - the app exposes it as an Edit named
    # "Prompt". The fixed spot above missed it with an artifact open (the chat moves to the left half).
    box = _prompt_box(h)
    if box:
        x, y = box
    # nothing on top of the box (the voice window stays on top - dragged there, the click would hit it instead)
    user32.WindowFromPoint.restype = wt.HWND
    user32.WindowFromPoint.argtypes = [wt.POINT]
    user32.GetAncestor.restype = wt.HWND
    under = user32.WindowFromPoint(wt.POINT(x, y))
    if not under or user32.GetAncestor(under, 2) != h:      # GA_ROOT
        return False
    old = wt.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    user32.SetCursorPos(x, y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)    # left down
    user32.mouse_event(0x0004, 0, 0, 0, 0)    # left up
    time.sleep(0.15)
    # put the mouse back - unless it was over Claude's own window: moving it back onto the sidebar button there
    # made the app slide the closed sidebar open , so then it stays in the message box
    if not (r.left <= old.x < r.right and r.top <= old.y < r.bottom):
        user32.SetCursorPos(old.x, old.y)
    return True


def _claude_window():
    """The Claude desktop app's main window (title exactly "Claude", owned by claude.exe)."""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def each(h, _):
        if user32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, buf, 256)
            if buf.value == "Claude":
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                hp = kernel32.OpenProcess(0x1000, False, pid.value)
                name = ctypes.create_unicode_buffer(512)
                size = wt.DWORD(512)
                if hp and kernel32.QueryFullProcessImageNameW(hp, 0, name, ctypes.byref(size)):
                    if name.value.lower().endswith("claude.exe"):
                        found.append(h)
                if hp:
                    kernel32.CloseHandle(hp)
        return True

    user32.EnumWindows(each, 0)
    return found[0] if found else None


def _set_clipboard(text):
    data = (text + "\0").encode("utf-16-le")
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("clipboard busy")
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0042, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(13, h)     # CF_UNICODETEXT
    finally:
        user32.CloseClipboard()


def _key(vk, up=False):
    user32.keybd_event(vk, 0, 2 if up else 0, 0)


def send_to_claude(text):
    """Paste text into the Claude app's message box and press Enter. -> "" or what went wrong."""
    # one paste at a time - but never wait forever behind one that's stuck
    if not send_lock.acquire(timeout=20):
        log("send: still busy with the last message")
        return "Still busy with the last message - try again in a moment."
    try:
        return _send(text)
    finally:
        send_lock.release()


def _send(text):
    h = _claude_window()
    if not h:
        return "The Claude app isn't open."
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)       # restore
    # Windows only lets the foreground app hand over the foreground - a tap of Alt makes it allow it
    _key(VK_MENU); _key(VK_MENU, True)
    user32.SetForegroundWindow(h)
    time.sleep(0.4)
    if user32.GetForegroundWindow() != h:
        return "Couldn't bring the Claude window to the front."
    if not _click_message_box(h):
        return "Something's covering Claude's message box - nothing was sent."
    _set_clipboard(text)
    _key(VK_CONTROL); _key(VK_V); _key(VK_V, True); _key(VK_CONTROL, True)
    time.sleep(0.3)
    _key(VK_RETURN); _key(VK_RETURN, True)
    return ""


def wait_for_reply(since, seconds):
    """Claude's spoken reply if it arrives within `seconds` (the Stop hook rewrites last_reply.json)."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            if os.path.getmtime(REPLY_FILE) > since:
                time.sleep(0.5)
                with open(REPLY_FILE, encoding="utf-8") as f:
                    return "\n\n".join(json.load(f))
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return None


# ---- answers come back where you asked (a message sent through a second PC is read out loud on that PC, not
# on the main one). Claude runs on ONE PC (the main PC); other PCs only show the chat
# through Remote Control, so their Stop hook never fires. A PC that isn't the main one has main_pc.txt (the
# main PC's door, e.g. http://192.168.1.46:5055): after it types a message in, it asks the main PC to send the
# next replies here (/route). The main PC's window then hands each reply over (/read) instead of reading it.
# Talking through the main PC itself clears the route.
ROUTE_FILE = os.path.join(HERE, "route.json")
ROUTE_HOURS = 2


def _post(url, obj, timeout=4):
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _my_door():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))      # picks the network card that reaches the home network
        return f"http://{s.getsockname()[0]}:{PORT}"
    finally:
        s.close()


def claim_replies():
    """Ask the main PC to read Claude's replies here (only on a PC with main_pc.txt)."""
    try:
        main = open(os.path.join(HERE, "main_pc.txt")).read().strip().rstrip("/")
    except OSError:
        try:
            os.remove(ROUTE_FILE)     # this IS the main PC: replies are read here again
        except OSError:
            pass
        return
    try:
        _post(f"{main}/route", {"key": KEY, "to": _my_door()})
    except Exception as e:
        status(f"⚠ Couldn't reach the main PC: {e}"[:80])


def wake_words():
    """Words that can sit around a reader command without making it a message: the wake word, please, now."""
    wake_phrase()
    return {"please", "now"} | set(wake["words"]) | (CLAUDE_WORDS if is_default_wake() else set())


def control_in(said):
    """The reader command in a short sentence made only of wake words and commands, else None.
    2026-10-29: "stop. Hey Claude, stop. Hey Claude." was typed to Claude instead of stopping the reader -
    only a lone exact word used to count."""
    text = " " + re.sub(r"[^a-z ]", " ", said.lower()) + " "
    found = []
    for phrase in sorted(CONTROLS, key=len, reverse=True):     # "keep going" before "go on"
        while f" {phrase} " in text:
            found.append((text.index(f" {phrase} "), CONTROLS[phrase]))
            text = text.replace(f" {phrase} ", " ", 1)
    allowed = wake_words()
    if not found or any(w not in allowed and not any(alike(w, a) for a in wake["words"]) for w in text.split()):
        return None              # anything else in it - it's a real message
    return max(found)[1]         # the last command said wins


def handle_words(said, from_phone=False):
    """A message for Claude, or a reader command. -> what to tell the person."""
    words = said.strip()
    cmd = control_in(words)
    if cmd:
        control(cmd)
        status(f"✔ {cmd}")
        return f"OK, {cmd}."
    problem = send_to_claude(words)
    status(f"⚠ {problem}" if problem else f"→ Sent: {words[:60]}")
    if not problem:
        threading.Thread(target=claim_replies, daemon=True).start()
    return problem


# ---- the iPhone door ------------------------------------------------------------------------------------------
class Door(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        # every knock on the door, so "request timed out" on the phone can be traced (2026-10-29); the key in
        # the address (/say?key=...) is never written down
        line = re.sub(r"([?&]key=)[^&\s\"]*", r"\1<key>", fmt % a)
        try:
            with open(os.path.join(HERE, "ears.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.client_address[0]} {line}\n")
        except OSError:
            pass

    def _answer(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._answer(200, {"status": "Claude's ears are on"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/say", "/route", "/read"):
            return self._answer(404, {"response": "Not here."})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            data = {}
        if path != "/say":
            if data.get("key") != KEY:
                return self._answer(403, {"response": "Wrong key."})
            if path == "/route":      # another PC: "read Claude's replies over here"
                with open(ROUTE_FILE, "w") as f:
                    json.dump({"to": data.get("to", ""), "at": time.time()}, f)
                self.log_message("replies now go to %s", data.get("to"))
                return self._answer(200, {"response": "ok"})
            paras = [p for p in data.get("paras", []) if isinstance(p, str)]   # /read: a reply to read here
            if paras:
                tmp = REPLY_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(paras, f)
                os.replace(tmp, REPLY_FILE)
            return self._answer(200, {"response": "ok"})
        self.log_message("POST /say fields=%s", sorted(data))
        from urllib.parse import parse_qs, urlparse
        in_url = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]   # http://...:5055/say?key=...
        if KEY not in (data.get("key"), self.headers.get("X-Key"), in_url):
            return self._answer(403, {"response": "Wrong key."})
        text = (data.get("text") or "").strip()
        if not text:
            return self._answer(200, {"response": "I didn't hear anything."})
        sent_at = time.time()
        status(f"\U0001f4f1 {text[:60]}")
        try:
            problem = handle_words(text, from_phone=True)
        except Exception as e:      # always answer the phone, never just drop it
            problem = f"Couldn't type it into Claude: {e}"
            status(f"⚠ {problem[:70]}")
            self.log_message("send failed: %r", e)
        if problem or text.lower().strip(" .!") in CONTROLS:
            return self._answer(200, {"response": problem or "Done."})
        # answer the phone at once (2026-10-29: waiting for Claude's reply gave "network timeout" on the iPhone);
        # a shortcut can still send {"wait": seconds} to hear the answer
        wait = data.get("wait", 0)
        reply = wait_for_reply(sent_at, float(wait)) if wait else None
        if reply and len(reply) > PHONE_MAX:    # long answers choke the phone
            cut = reply[:PHONE_MAX]
            end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
            reply = (cut[:end + 1] if end > 150 else cut.rsplit(" ", 1)[0] + "...") + " The rest is on the PC."
        self._answer(200, {"response": reply or "Sent to Claude."})


def serve():
    ThreadingHTTPServer(("0.0.0.0", PORT), Door).serve_forever()


# ---- the PC mic -----------------------------------------------------------------------------------------------
def mic_loop():
    import numpy as np
    import pyaudio
    from faster_whisper import WhisperModel
    status("Loading the speech model...")
    model = WhisperModel(os.path.join(HERE, "models", "base.en"), device="cpu", compute_type="int8")

    def transcribe(audio):
        # the hint makes it expect the wake word and reader commands - without it "Hey Claude, pause" came out
        # "A-Claud Pause" / "Hey Court Pools" (tested 2026-10-29); ordinary speech isn't changed by it
        segments, _ = model.transcribe(audio, language="en", beam_size=1, hotwords=wake_phrase(),
                                       initial_prompt=hint())
        return "".join(s.text for s in segments).strip()
    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True, frames_per_buffer=FRAME)
    floor, voiced, silent, frames = 200.0, 0, 0, []
    expecting = False      # heard "Hey Claude" on its own - the next thing said (within 10 s) is the message
    idle_shown = None
    while True:
        talk_now = os.path.exists(os.path.join(HERE, "talk_now"))
        if os.path.exists(os.path.join(HERE, "ears_off")) and not talk_now:
            if idle_shown != "off":
                status("\U0001f507 Mic off")
                idle_shown = "off"
            stream.read(FRAME, exception_on_overflow=False)
            frames, voiced, silent = [], 0, 0
            continue
        showing = "talk" if talk_now else "on " + wake_phrase()      # shows a new wake word as soon as it's set
        if not frames and idle_shown != showing:
            idle_shown = showing
            status("\U0001f3a4 Talk now..." if talk_now else f"\U0001f3a4 Say “{wake_phrase()}”")
        chunk = stream.read(FRAME, exception_on_overflow=False)
        level = float(np.sqrt(np.mean(np.frombuffer(chunk, np.int16).astype(np.float32) ** 2)))
        speech = level > max(floor * 3.0, 350)
        if not speech and not frames:
            floor = floor * 0.98 + level * 0.02       # follows the room's background noise
        if speech:
            voiced += 1
            silent = 0
            frames.append(chunk)
        elif frames:
            silent += 1
            frames.append(chunk)
        if frames and not voiced >= 3 and silent > 10:            # a click, not speech
            frames, voiced, silent = [], 0, 0
            continue
        long_enough = len(frames) * FRAME / RATE
        if frames and (silent > 30 or long_enough > 25):           # ~0.9 s of quiet ends what was said
            audio = np.frombuffer(b"".join(frames), np.int16).astype(np.float32) / 32768.0
            frames, voiced, silent = [], 0, 0
            idle_shown = None
            text = transcribe(audio)
            if not text:
                continue
            if expecting and time.time() > expecting:      # "Hey Claude" and then nothing for 10 s
                expecting = False
            if talk_now or expecting:
                try:
                    os.remove(os.path.join(HERE, "talk_now"))
                except OSError:
                    pass
                expecting = False
                rest = wake_match(text)
                handle_words(text if rest is None else rest)
                continue
            rest = wake_match(text)
            if rest is None or echo_of_reply(text):
                continue                 # not for Claude (or it's the reader talking)
            if rest:
                handle_words(rest)
            else:
                expecting = time.time() + 10
                winsound.Beep(880, 120)
                status("\U0001f3a4 Listening...")
                idle_shown = "listening"


threading.Thread(target=serve, daemon=True).start()
try:
    mic_loop()
except Exception as e:     # no mic / no Whisper - the iPhone door still works
    status(f"⚠ Mic: {e}")
    while True:
        time.sleep(3600)
