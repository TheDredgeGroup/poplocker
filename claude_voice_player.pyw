"""Pop Locker player - stays open all the time. Reads each new reply one paragraph at a
time in the Northern English Piper voice. Back goes to the paragraph before (all the way to the first) and
reads it, Skip goes to the next, Stop stops reading (the window stays). A new reply starts reading by itself."""
import ctypes, json, os, subprocess, sys, tempfile, threading, time, tkinter as tk, wave, winsound

HERE = os.path.dirname(os.path.abspath(__file__))
TEXT_FILE = os.path.join(HERE, "last_reply.json")
PID_FILE = os.path.join(HERE, "player.pid")
PIPER = os.path.join(HERE, "piper", "piper.exe")
VOICE_DIR = os.path.join(HERE, "voices")
# the voices to pick from (both CC-BY-SA 4.0 - fine to sell with a credit)
VOICES = {"Male - Northern": "en_GB-northern_english_male-medium.onnx",          # Northern English male
          "Female - Southern": "en_GB-southern_english_female-low.onnx"}         # Southern English female
SETTINGS = os.path.join(HERE, "player_settings.json")
DEFAULT_WAKE = "Hey Computer"
try:
    with open(SETTINGS) as _f:
        voice_name = json.load(_f).get("voice", "")
except (OSError, ValueError):
    voice_name = ""
if voice_name not in VOICES:
    voice_name = next(iter(VOICES))
VOICE = os.path.join(VOICE_DIR, VOICES[voice_name])
NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000

# one window only - a second copy (the Startup icon, the desktop icon and Claude's SessionStart hook can all start
# it) would read every reply twice, on top of itself
_one = ctypes.windll.kernel32.CreateMutexW(None, False, "ClaudeVoicePlayerWindow")
if ctypes.windll.kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
    sys.exit(0)
with open(PID_FILE, "w") as f:
    f.write(str(os.getpid()))

state = {"paras": [], "i": -1, "after": None, "mtime": 0.0, "reply": 0, "wavs": {},
         # what's playing now (reply, paragraph, ms into it when it started, when it started) and where Pause
         # stopped it (reply, paragraph, ms)
         "playing": None, "paused": None, "sound": None}
lock = threading.Lock()


LEAD_IN_MS = 400


def lead_in(path):
    """Put a moment of silence before the words. A TV or other HDMI speaker wakes up as the sound starts and
    swallowed the first words of every paragraph."""
    try:
        with wave.open(path) as f:
            params, frames = f.getparams(), f.readframes(f.getnframes())
        silence = b"\0" * (int(params.framerate * LEAD_IN_MS / 1000) * params.sampwidth * params.nchannels)
        with wave.open(path, "wb") as f:
            f.setparams(params)
            f.writeframes(silence + frames)
    except (OSError, wave.Error):
        pass


def make(reply, i):
    """The wav for paragraph i of reply number `reply` (made once, ahead of time when possible)."""
    with lock:
        if reply != state["reply"] or i >= len(state["paras"]) or i in state["wavs"]:
            return
        text = state["paras"][i]
        state["wavs"][i] = None   # being made
    out = os.path.join(tempfile.gettempdir(), f"claude_voice_{os.getpid()}_{reply}_{i}.wav")
    # below-normal priority: the next paragraph is made while this one plays, and on a laptop Piper using
    # every core made the sound break up
    p = subprocess.run([PIPER, "--model", VOICE, "--output_file", out], input=text, capture_output=True,
                       text=True, encoding="utf-8", creationflags=NO_WINDOW | BELOW_NORMAL)
    if p.returncode == 0 and os.path.exists(out):
        lead_in(out)
    with lock:
        if reply == state["reply"]:
            state["wavs"][i] = out if p.returncode == 0 and os.path.exists(out) else False


def clear_wavs():
    for w in state["wavs"].values():
        if w:
            try:
                os.remove(w)
            except OSError:
                pass
    state["wavs"] = {}


# sharp on high-DPI screens (a 4K at 150%): without this Windows stretches the whole window and every edge goes
# soft. "System aware": Tk can't redraw itself when moved to a screen with other scaling, so it's drawn for the main one
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except (OSError, AttributeError):
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (OSError, AttributeError):
        pass

root = tk.Tk()
root.title("Pop Locker")
try:
    root.iconbitmap(default=os.path.join(HERE, "icon", "poplocker.ico"))    # default: the wake word box gets it too
except tk.TclError:
    pass
SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)


def px(n):
    """A size in pixels at 100% scaling -> pixels on this screen."""
    return int(round(n * SCALE))


root.attributes("-topmost", True)
root.minsize(px(300), px(230))
root.geometry(f"{px(330)}x{px(300)}")
# Claude's ears (claude_voice_ears.py): mic on/off, Talk (no wake word needed), and what they last heard
EARS = os.path.join(HERE, "claude_voice_ears.py")
EARS_PY = os.path.join(HERE, "python", "pythonw.exe")      # the Python that comes with the app

# ---- the look: charcoal fading to grey, rounded buttons all one blue -------------------------------------------
# Everything is drawn on one canvas: Tk's own buttons can't be rounded, and a gradient can't show behind them.
import tkinter.font as tkfont
TOP, MID, BOTTOM = (0x1c, 0x1c, 0x1c), (0x2b, 0x2b, 0x2b), (0x4a, 0x4a, 0x4a)
BLUE, BLUE_HOVER, BLUE_DOWN = "#2F7BD8", "#4890E8", "#2466B5"
PANEL = "#1f1f1f"
BTN_FONT = tkfont.Font(family="Segoe UI", size=9, weight="bold")
SMALL_FONT = tkfont.Font(family="Segoe UI", size=9)
TITLE_FONT = tkfont.Font(family="Segoe UI", size=10, weight="bold")
ICON_FONT = tkfont.Font(family="Segoe MDL2 Assets", size=10)       # Windows' own symbols (Windows 10 and 11)
EMOJI_FONT = tkfont.Font(family="Segoe UI Emoji", size=10)
I_BACK, I_PAUSE, I_PLAY, I_SKIP, I_STOP = "", "", "", "", ""
I_MIC, I_MIC_OFF, I_TALK, I_DOWN, I_SEARCH, I_VOLUME = "", "", "", "", "", ""
I_LOCK, I_DONE, I_SENT = "", "", ""
I_EAR = "\U0001f442"            # the icon font has no ear: the emoji one

cv = tk.Canvas(root, highlightthickness=0, bd=0, bg="#2b2b2b")
cv.pack(fill=tk.BOTH, expand=True)


def _mix(a, b, t):
    t = min(1.0, max(0.0, t))
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def shade(y, h):
    """The background colour at height y: charcoal at the top, grey at the bottom."""
    t = y / max(1, h - 1)
    return _mix(TOP, MID, t / 0.45) if t < 0.45 else _mix(MID, BOTTOM, (t - 0.45) / 0.55)


def _rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


_pictures = {}


def rounded_picture(w, h, r, color, top):
    """A rounded box with smooth edges, as a picture. Tk draws its own shapes with jagged edges, so the edge pixels
    are worked out here and blended with the background behind them (the fade at that height: `top` is where the
    box sits on the canvas). Kept, so the same box isn't worked out twice."""
    ch = cv.winfo_height()
    key = (w, h, r, color, top, ch)
    if key in _pictures:
        return _pictures[key]
    fg = _rgb(color)
    r = min(r, w / 2, h / 2)
    rows = []
    for j in range(h):
        cy = j + 0.5
        if r <= cy <= h - r:
            rows.append("{" + " ".join([color] * w) + "}")
            continue
        y_abs = top + j
        bg = _rgb(shade(y_abs - y_abs % 2, ch))        # the fade is drawn in 2-pixel bands

        def blend(a):
            if a >= 1:
                return color
            return "#%02x%02x%02x" % tuple(round(b + (f - b) * max(0.0, a)) for f, b in zip(fg, bg))
        ccy = r if cy < r else h - r
        middle = blend(r - abs(cy - ccy) + 0.5)        # the same all along the straight top or bottom edge
        row = []
        for i in range(w):
            cx = i + 0.5
            if r <= cx <= w - r:
                row.append(middle)
            else:
                ccx = r if cx < r else w - r
                row.append(blend(r - ((cx - ccx) ** 2 + (cy - ccy) ** 2) ** 0.5 + 0.5))
        rows.append("{" + " ".join(row) + "}")
    pic = tk.PhotoImage(width=w, height=h)
    pic.put(" ".join(rows))
    if len(_pictures) > 300:
        _pictures.clear()
    _pictures[key] = pic
    return pic


class Tip:
    """The words for a symbol, shown when the mouse rests on its button."""
    win, job = None, None

    @classmethod
    def later(cls, text):
        cls.hide()
        cls.job = root.after(450, lambda: cls._show(text() if callable(text) else text))

    @classmethod
    def _show(cls, text):
        cls.job = None
        w = cls.win = tk.Toplevel(root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        tk.Label(w, text=text, bg="#111111", fg="#ffffff", font=SMALL_FONT, padx=px(8), pady=px(3)).pack()
        w.update_idletasks()
        # above the pointer and kept inside the window's width (it sits in the screen's bottom right corner)
        x = min(root.winfo_pointerx() - px(10), root.winfo_rootx() + root.winfo_width() - w.winfo_reqwidth())
        x = max(x, root.winfo_rootx())
        w.geometry(f"+{x}+{root.winfo_pointery() - w.winfo_reqheight() - px(12)}")

    @classmethod
    def hide(cls):
        if cls.job:
            root.after_cancel(cls.job)
            cls.job = None
        if cls.win:
            cls.win.destroy()
            cls.win = None


class RoundButton:
    """A rounded blue button drawn on the canvas: a symbol, words, or both. `tip` (words, or a function giving
    them) says what it does when the mouse rests on it."""
    H = px(28)

    def __init__(self, icon, text, command, tip, icon_font=ICON_FONT, round_=False, icon_after=False):
        self.icon, self.text, self.command, self.tip = icon, text, command, tip
        self.icon_font, self.round, self.icon_after = icon_font, round_, icon_after
        self.box, self.inside, self.pressed, self.items = None, False, False, None

    def width(self):
        if self.round:
            return self.H
        w = px(26)
        if self.icon:
            w += self.icon_font.measure(self.icon)
        if self.text:
            w += BTN_FONT.measure(self.text) + (px(6) if self.icon else 0)
        return w

    def place(self, x, y, w=None):
        self.box = (int(x), int(y), int(w or self.width()), self.H)
        if self.items is None:
            # only the picture takes the mouse; the symbol and words on it are "disabled" so they don't
            self.items = {
                "shape": cv.create_image(0, 0, anchor="nw", tags="chrome"),
                "icon": cv.create_text(0, 0, anchor="w", fill="white", disabledfill="white", state="disabled"),
                "text": cv.create_text(0, 0, anchor="w", fill="white", disabledfill="white", state="disabled",
                                       font=BTN_FONT)}
            s = self.items["shape"]
            cv.tag_bind(s, "<Enter>", self._enter)
            cv.tag_bind(s, "<Leave>", self._leave)
            cv.tag_bind(s, "<ButtonPress-1>", self._down)
            cv.tag_bind(s, "<ButtonRelease-1>", self._up)
        self.draw()

    def paint(self):
        x, y, w, h = self.box
        color = BLUE_DOWN if self.pressed else (BLUE_HOVER if self.inside else BLUE)
        cv.itemconfig(self.items["shape"], image=rounded_picture(w, h, h / 2, color, y))

    def draw(self):
        if not self.items:
            return
        x, y, w, h = self.box
        it = self.items
        cv.coords(it["shape"], x, y)
        self.paint()
        cy = y + h / 2
        iw = self.icon_font.measure(self.icon) if self.icon else 0
        tw = BTN_FONT.measure(self.text) if self.text else 0
        gap = px(6) if self.icon and self.text else 0
        left = x + (w - iw - gap - tw) / 2
        cv.itemconfig(it["icon"], text=self.icon or "", font=self.icon_font)
        icon_x, text_x = (left + tw + gap, left) if self.icon_after else (left, left + iw + gap)
        cv.coords(it["icon"], icon_x, cy)
        cv.itemconfig(it["text"], text=self.text or "")
        cv.coords(it["text"], text_x, cy)

    def config(self, icon=None, text=None, tip=None):
        if icon is not None:
            self.icon = icon
        if text is not None:
            self.text = text
        if tip is not None:
            self.tip = tip
        self.draw()

    def _enter(self, _e):
        self.inside = True
        self.paint()
        Tip.later(self.tip)

    def _leave(self, _e):
        self.inside = self.pressed = False
        self.paint()
        Tip.hide()

    def _down(self, _e):
        self.pressed = True
        self.paint()

    def _up(self, _e):
        Tip.hide()
        self.pressed = False
        self.paint()
        if self.inside:
            self.command()


# the title, what's being read (top right), and what the ears last did (the bottom line)
title_icon = cv.create_text(px(12), px(16), anchor="w", text=I_LOCK, font=ICON_FONT, fill=BLUE)
title_text = cv.create_text(px(32), px(16), anchor="w", text="Pop Locker", font=TITLE_FONT, fill="#ffffff")
status_text = cv.create_text(0, px(16), anchor="e", text="", font=SMALL_FONT, fill="#9aa4ad")
status_icon = cv.create_text(0, px(16), anchor="e", text="", font=ICON_FONT, fill="#9aa4ad")
ears_text = cv.create_text(px(14), 0, anchor="w", text="", font=SMALL_FONT, fill="#b0b8c0")
ears_said = {"text": ""}
panel = cv.create_image(0, 0, anchor="nw")


def set_status(icon, text):
    cv.itemconfig(status_icon, text=icon)
    cv.itemconfig(status_text, text=text)
    place_status()


def place_status():
    w = cv.winfo_width()
    cv.coords(status_text, w - px(12), px(16))
    bbox = cv.bbox(status_text)
    cv.coords(status_icon, (bbox[0] if bbox and cv.itemcget(status_text, "text") else w - px(12)) - px(5), px(16))


def set_ears(text):
    """The bottom line, cut short with ... to fit the window."""
    ears_said["text"] = text
    room = cv.winfo_width() - px(28)
    if SMALL_FONT.measure(text) > room > 20:
        while text and SMALL_FONT.measure(text + "...") > room:
            text = text[:-1]
        text = text.rstrip() + "..."
    cv.itemconfig(ears_text, text=text)


# The reply's text, with the paragraph being read highlighted. Click a paragraph to read from it.
box = tk.Text(cv, wrap="word", bg=PANEL, fg="#c9c9c9", relief="flat", bd=0, highlightthickness=0, padx=px(6),
              pady=px(4), font=("Segoe UI", 9), cursor="hand2", spacing3=px(6), selectbackground=BLUE)
box_win = cv.create_window(0, 0, anchor="nw", window=box)
box.tag_configure("cur", background=BLUE, foreground="#ffffff")
box.config(state="disabled")


def fill_text():
    box.config(state="normal")
    box.delete("1.0", "end")
    for n, p in enumerate(state["paras"]):
        box.insert("end", p, (f"p{n}",))
        box.insert("end", "\n\n")
    box.config(state="disabled")


def highlight():
    box.tag_remove("cur", "1.0", "end")
    i = state["i"]
    if 0 <= i < len(state["paras"]):
        rng = box.tag_ranges(f"p{i}")
        if rng:
            box.tag_add("cur", rng[0], rng[1])
            box.see(rng[0])


def show():
    n, i = len(state["paras"]), state["i"]
    if 0 <= i < n:
        set_status(I_VOLUME, f"{i + 1} of {n}")
    elif n:
        set_status(I_DONE, f"Done, {n} paragraph{'s' if n != 1 else ''}")
    else:
        set_status("", "Waiting for Claude")
    highlight()


def clicked(event):
    for name in box.tag_names(f"@{event.x},{event.y}"):
        if name.startswith("p") and name[1:].isdigit():
            play(int(name[1:]))
            return


box.bind("<Button-1>", clicked)


def mci(command):
    """Windows' media player commands. It says when a sound has really finished (winsound can't), and its stop
    works at once."""
    buf = ctypes.create_unicode_buffer(128)
    ctypes.windll.winmm.mciSendStringW(command, buf, 128, None)
    return buf.value


def halt():
    if state["after"]:
        root.after_cancel(state["after"])
        state["after"] = None
    state["sound"] = None
    mci("stop voice")
    mci("close voice")
    state["playing"] = None


def unpause():
    state["paused"] = None
    pause_btn.config(icon=I_PAUSE, text="Pause")


def play(i):
    """Read paragraph i (and carry on to the next ones)."""
    halt()
    unpause()
    n = len(state["paras"])
    if i >= n:
        state["i"] = n          # finished - Back reads the last paragraph
        show()
        return
    state["i"] = max(0, i)
    i = state["i"]
    show()
    reply = state["reply"]
    if i not in state["wavs"] or state["wavs"][i] is None:
        threading.Thread(target=make, args=(reply, i), daemon=True).start()
        state["after"] = root.after(150, lambda: wait_ready(reply, i))
        return
    start_audio(reply, i)


def wait_ready(reply, i):
    state["after"] = None
    if reply != state["reply"] or state["i"] != i:
        return
    if state["wavs"].get(i) is None:
        state["after"] = root.after(150, lambda: wait_ready(reply, i))
        return
    start_audio(reply, i)


def start_audio(reply, i, from_ms=0):
    asleep_for = wake_speaker()
    if asleep_for:             # the speaker was asleep - give it time to wake before the first word
        state["after"] = root.after(int(asleep_for * 1000), lambda: start_audio(reply, i, from_ms))
        return
    w = state["wavs"].get(i)
    threading.Thread(target=make, args=(reply, i + 1), daemon=True).start()   # next one ready in time
    if not w:
        play(i + 1)
        return
    with wave.open(w) as f:
        rate, total = f.getframerate(), f.getnframes()
        if from_ms > 0:   # resuming after Pause: play a copy that starts there
            f.setpos(min(total, int(from_ms / 1000 * rate)))
            frames, params = f.readframes(total), f.getparams()
            w = os.path.join(tempfile.gettempdir(), f"claude_voice_{os.getpid()}_resume.wav")
            with wave.open(w, "wb") as out:
                out.setparams(params)
                out.writeframes(frames)
    if from_ms > 0:
        lead_in(w)
    import time
    state["playing"] = (reply, i, from_ms, time.monotonic())
    # the next paragraph starts when this one has really finished playing. Going by the file's length cut
    # paragraphs short on the TV, whose sound starts late
    mci("close voice")
    mci(f'open "{w}" type waveaudio alias voice')
    mci("play voice")
    state["sound"] = w
    state["after"] = root.after(200, lambda: when_done(w, i))


def when_done(sound, i):
    state["after"] = None
    if state["sound"] != sound:              # stopped, skipped or paused meanwhile
        return
    if mci("status voice mode") == "playing":
        state["after"] = root.after(150, lambda: when_done(sound, i))
        return
    mci("close voice")
    state["sound"] = None
    state["after"] = root.after(250, lambda: play(i + 1))     # a short breath between paragraphs


def pause():
    """Pause where it is; press again to carry on from the same spot."""
    import time
    if state["paused"]:
        reply, i, at = state["paused"]
        unpause()
        if reply == state["reply"] and 0 <= i < len(state["paras"]):
            state["i"] = i
            show()
            if state["wavs"].get(i):
                start_audio(reply, i, at)
            else:
                play(i)
        return
    i, n = state["i"], len(state["paras"])
    if not (0 <= i < n):          # nothing being read - Play starts from the top
        if n:
            play(0)
        return
    at = 0
    if state["playing"] and state["playing"][1] == i:
        _, _, from_ms, started = state["playing"]
        at = from_ms + int((time.monotonic() - started) * 1000)
    halt()
    state["paused"] = (state["reply"], i, max(0, at - 300))   # back up a hair so no word is clipped
    pause_btn.config(icon=I_PLAY, text="Play")
    set_status(I_PAUSE, f"Paused, {i + 1} of {n}")


def back():
    if state["paras"]:
        play(state["i"] - 1 if state["i"] > 0 else 0)


def skip():
    if state["paras"] and state["i"] < len(state["paras"]):
        play(state["i"] + 1)


KEEP_AWAKE = os.path.join(tempfile.gettempdir(), "claude_voice_keep_awake.wav")


AWAKE_AFTER_S = 60      # the speaker stays awake this long after reading, so a back-and-forth never chops
WAKE_UP_S = 3           # a sleeping TV needs this long - the first words chopped for a few seconds


def wake_speaker():
    """Start the near-silent loop now (if it isn't running). -> seconds until the speaker is surely awake."""
    import random
    if not state.get("awake"):
        if not os.path.exists(KEEP_AWAKE):
            with wave.open(KEEP_AWAKE, "wb") as f:     # 2 s of the faintest hiss - not dead digital zero
                f.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                f.writeframes(b"".join(random.choice((b"\x01\x00", b"\xff\xff")) for _ in range(44100)))
        winsound.PlaySound(KEEP_AWAKE, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
        state["awake"] = True
        state["awake_at"] = time.monotonic()
    return max(0.0, state["awake_at"] + WAKE_UP_S - time.monotonic())


def keep_awake():
    """Play a near-silent loop while it's reading, and a minute after. A TV or dock speaker puts its sound to
    sleep the moment nothing plays and chops the words while waking up again - with YouTube playing at the same
    time every word came through."""
    busy = bool(state["sound"] or state["after"])          # a paragraph playing, or the next one being made
    if busy:
        state["last_busy"] = time.monotonic()
    want = busy or time.monotonic() - state.get("last_busy", 0) < AWAKE_AFTER_S
    if want:
        wake_speaker()
    elif state.get("awake"):
        winsound.PlaySound(None, 0)
        state["awake"] = False
    root.after(500, keep_awake)


def stop():
    halt()
    unpause()
    if state["paras"]:
        state["i"] = len(state["paras"])
    show()


def ears_running():
    """The process in ears.pid is alive AND is a Python - Windows reuses process numbers, and a stale
    ears.pid that now belongs to another program kept the ears from ever being started again."""
    try:
        pid = int(open(os.path.join(HERE, "ears.pid")).read())
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        name = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        got_name = ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, name, ctypes.byref(size))
        ctypes.windll.kernel32.CloseHandle(h)
        # a Python at all - not the folder: a venv's pythonw.exe starts the real Python somewhere else
        ours = got_name and os.path.basename(name.value).lower() in ("python.exe", "pythonw.exe")
        return code.value == 259 and bool(ours)          # STILL_ACTIVE
    except (OSError, ValueError):
        return False


def start_ears():
    if not ears_running() and os.path.exists(EARS_PY):
        subprocess.Popen([EARS_PY, EARS], cwd=HERE, creationflags=NO_WINDOW)


def mic_toggle():
    off = os.path.join(HERE, "ears_off")
    if os.path.exists(off):
        os.remove(off)
    else:
        open(off, "w").close()
    show_mic()


def talk():
    open(os.path.join(HERE, "talk_now"), "w").close()
    start_ears()


def show_mic():
    mic_btn.config(icon=I_MIC_OFF if os.path.exists(os.path.join(HERE, "ears_off")) else I_MIC)


def mic_tip():
    if os.path.exists(os.path.join(HERE, "ears_off")):
        return "Mic is off. Click to listen for the wake word"
    return f"Mic is on, listening for “{wake_word()}”. Click to turn it off"


def ears_watch():
    """What the ears last did, and any reader command they heard ("Hey Claude, pause")."""
    try:
        said = open(os.path.join(HERE, "ears_status.txt"), encoding="utf-8").read().strip()
        if said != ears_said["text"]:
            set_ears(said)
    except OSError:
        pass
    c = os.path.join(HERE, "control.txt")
    if os.path.exists(c):
        try:
            cmd = open(c).read().strip()
            os.remove(c)
        except OSError:
            cmd = ""
        if cmd == "pause" and not state["paused"]:
            pause()
        elif cmd == "play":
            if state["paused"] or not (0 <= state["i"] < len(state["paras"])):
                pause()
        elif cmd in ("skip", "back", "stop"):
            {"skip": skip, "back": back, "stop": stop}[cmd]()
    if not ears_running():
        start_ears()        # came back after a crash - checked every few seconds below
        root.after(5000, ears_watch)
        return
    root.after(400, ears_watch)


def hand_over(paras):
    """If this reply answers a message sent from another PC (the ears there asked for it - route.json), send it
    there to be read instead of reading it here. -> where it went, or None to read it here.
    Only that one reply goes: the chat file can't tell a message typed here from one sent there, so a lasting
    route sent replies to typing on this PC over to the other PC too. Each message from there asks again."""
    path = os.path.join(HERE, "route.json")
    try:
        with open(path) as f:
            route = json.load(f)
        os.remove(path)
        if time.time() - route.get("at", 0) > 2 * 3600 or not route.get("to"):
            return None
        import urllib.request
        key = open(os.path.join(HERE, "ears_key.txt")).read().strip()
        req = urllib.request.Request(route["to"].rstrip("/") + "/read",
                                     data=json.dumps({"key": key, "paras": paras}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=4).read()
        return route["to"].split("//")[-1].split(":")[0]
    except Exception:
        return None          # no route, or that PC is off - read it here


def watch():
    """A new reply from the hook: read it from the top."""
    try:
        m = os.path.getmtime(TEXT_FILE)
        if m != state["mtime"]:
            state["mtime"] = m
            with open(TEXT_FILE, encoding="utf-8") as f:
                paras = json.load(f)
            halt()
            with lock:
                clear_wavs()
                state["reply"] += 1
                state["paras"] = paras
            fill_text()
            elsewhere = hand_over(paras)
            if elsewhere:
                state["i"] = len(paras)          # not read here; Back still reads it
                show()
                set_status(I_SENT, f"Read on {elsewhere}")
            else:
                play(0)
    except (OSError, ValueError):
        pass
    root.after(500, watch)


def close():
    halt()
    clear_wavs()
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    root.destroy()


def save_setting(key, value):
    """Change one setting in player_settings.json and keep the others (the ears read the wake word from it)."""
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        settings = {}
    settings[key] = value
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except OSError:
        pass


def wake_word():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f).get("wake") or DEFAULT_WAKE
    except (OSError, ValueError):
        return DEFAULT_WAKE


def set_wake():
    """Change the wake word. The ears pick it up without a restart."""
    from tkinter import simpledialog
    new = simpledialog.askstring("Wake word", "What do you say to start a message?\n"
                                 "Two words work best, e.g. Hey Jarvis. Leave it empty for Hey Computer.",
                                 initialvalue=wake_word(), parent=root)
    if new is None:          # Cancel
        return
    new = " ".join(new.split()) or DEFAULT_WAKE
    save_setting("wake", new)


def pick_voice(name):
    """Switch voice; the paragraph being read starts again in the new one."""
    global VOICE
    VOICE = os.path.join(VOICE_DIR, VOICES[name])
    save_setting("voice", name)
    reading = 0 <= state["i"] < len(state["paras"]) and not state["paused"]
    halt()
    with lock:
        clear_wavs()
        state["reply"] += 1          # any wav still being made in the old voice is thrown away
    if state["paused"]:              # Play then starts that paragraph over in the new voice
        state["paused"] = (state["reply"], state["paused"][1], 0)
    elif reading:
        play(state["i"])


def short_voice(name):
    """ "Male - Northern" -> "Northern", for the button."""
    return name.split(" - ")[-1]


voice_var = tk.StringVar(value=voice_name)
voice_popup = tk.Menu(root, tearoff=0, bg="#2b2b2b", fg="white", activebackground=BLUE, activeforeground="white",
                      selectcolor="white", font=SMALL_FONT)


def choose_voice(name):
    voice_btn.config(text=short_voice(name))
    layout()
    pick_voice(name)


for _name in VOICES:
    voice_popup.add_radiobutton(label=_name, variable=voice_var, value=_name, command=lambda n=_name: choose_voice(n))


def open_voices():
    x, y, w, h = voice_btn.box
    voice_popup.tk_popup(cv.winfo_rootx() + int(x), cv.winfo_rooty() + int(y + h + 2))


def open_history():
    """The saved chats, searchable by keyword, date and project (chat_search.pyw)."""
    subprocess.Popen([EARS_PY, os.path.join(HERE, "chat_search.pyw")], cwd=HERE, creationflags=NO_WINDOW)


# the reader's buttons (top row) and the ears' buttons (bottom row)
back_btn = RoundButton(I_BACK, "", back, "Back: read the paragraph before", round_=True)
pause_btn = RoundButton(I_PAUSE, "Pause", pause, "Pause, or carry on from where it stopped")
skip_btn = RoundButton(I_SKIP, "", skip, "Skip to the next paragraph", round_=True)
stop_btn = RoundButton(I_STOP, "", stop, "Stop reading", round_=True)
mic_btn = RoundButton(I_MIC, "", mic_toggle, mic_tip, round_=True)
talk_btn = RoundButton(I_TALK, "Talk", talk, "Talk: send the next thing you say, no wake word needed")
voice_btn = RoundButton(I_DOWN, short_voice(voice_name), open_voices, "The reading voice", icon_after=True)
wake_btn = RoundButton(I_EAR, "", set_wake, lambda: f"Wake word: “{wake_word()}”. Click to change it",
                       icon_font=EMOJI_FONT, round_=True)
history_btn = RoundButton(I_SEARCH, "", open_history, "History: search your saved chats", round_=True)
bottom_row = [mic_btn, talk_btn, voice_btn, wake_btn]
if os.path.exists(os.path.join(HERE, "chat_search.pyw")):
    bottom_row.append(history_btn)


def layout(_event=None):
    """Draw the background and put everything in place for the window's size."""
    w, h = cv.winfo_width(), cv.winfo_height()
    if w < px(100) or h < px(100):
        return
    gap, side, bh = px(6), px(12), RoundButton.H
    cv.delete("bg")
    for y in range(0, h, 2):
        cv.create_line(0, y + 1, w, y + 1, fill=shade(y, h), width=2, tags="bg")
    cv.tag_lower("bg")
    place_status()
    # top row: Back, Pause (the wide one), Skip, Stop
    y = px(32)
    pause_w = w - 2 * side - 3 * bh - 3 * gap
    back_btn.place(side, y)
    pause_btn.place(side + bh + gap, y, pause_w)
    skip_btn.place(side + bh + 2 * gap + pause_w, y)
    stop_btn.place(w - side - bh, y)
    # the reply, on a darker rounded panel
    top, bottom = y + bh + px(10), h - px(64)
    cv.coords(panel, side, top)
    cv.itemconfig(panel, image=rounded_picture(w - 2 * side, bottom - top, px(10), PANEL, top))
    cv.coords(box_win, side + px(6), top + px(6))
    cv.itemconfig(box_win, width=max(10, w - 2 * side - px(12)), height=max(10, bottom - top - px(12)))
    # bottom row: Mic, Talk, voice, wake word, History; then what the ears last did
    x, y = side, h - px(58)
    for b in bottom_row:
        b.place(x, y)
        x += b.width() + gap
    cv.coords(ears_text, side + px(2), h - px(15))
    set_ears(ears_said["text"])


_pending = {"job": None}


def _resized(_event):
    if _pending["job"]:
        root.after_cancel(_pending["job"])
    _pending["job"] = root.after(30, layout)


cv.bind("<Configure>", _resized)
show_mic()
keep_awake()
root.protocol("WM_DELETE_WINDOW", close)
root.update_idletasks()
try:        # Windows' dark title bar, to go with the charcoal (Windows 10 2004 and later; ignored elsewhere)
    ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(int(root.wm_frame(), 16)), 20,
                                               ctypes.byref(ctypes.c_int(1)), 4)
except (OSError, ValueError, AttributeError):
    pass
root.geometry(f"+{root.winfo_screenwidth() - root.winfo_width() - 40}+{root.winfo_screenheight() - root.winfo_height() - 90}")
# the window starts empty: an old reply is never shown again. Only one written in the last 15 seconds (the reply
# that opened the window) is read.
try:
    if os.path.exists(TEXT_FILE):
        import time
        if time.time() - os.path.getmtime(TEXT_FILE) > 15:
            state["mtime"] = os.path.getmtime(TEXT_FILE)
except (OSError, ValueError):
    pass
fill_text()
show()
root.after(100, watch)
start_ears()
root.after(1500, ears_watch)
root.mainloop()
