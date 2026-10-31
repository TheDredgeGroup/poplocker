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


root = tk.Tk()
root.title("Pop Locker")
try:
    root.iconbitmap(os.path.join(HERE, "icon", "poplocker.ico"))
except tk.TclError:
    pass
root.attributes("-topmost", True)
root.configure(bg="#1e1e1e")
root.minsize(380, 160)
bar = tk.Frame(root, bg="#1e1e1e")
bar.pack(side=tk.TOP, fill=tk.X)
label = tk.Label(bar, text="", width=20, anchor="w", bg="#1e1e1e", fg="#CE93D8", font=("Arial", 10, "bold"))
label.pack(side=tk.LEFT, padx=(10, 4), pady=6)
# The reply's text, with the paragraph being read highlighted. Click a paragraph to read from it.
# Claude's ears (claude_voice_ears.py): mic on/off, Talk (no wake word needed), and what they last heard
EARS = os.path.join(HERE, "claude_voice_ears.py")
EARS_PY = os.path.join(HERE, "python", "pythonw.exe")      # the Python that comes with the app
ears_bar = tk.Frame(root, bg="#1e1e1e")
ears_bar.pack(side=tk.TOP, fill=tk.X)
ears_label = tk.Label(ears_bar, text="", anchor="w", bg="#1e1e1e", fg="#90A4AE", font=("Segoe UI", 9))
box = tk.Text(root, wrap="word", height=10, width=58, bg="#262626", fg="#d0d0d0", relief="flat", padx=10,
              pady=8, font=("Segoe UI", 10), cursor="hand2", spacing3=6)
box.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
box.tag_configure("cur", background="#5C3B66", foreground="#ffffff")
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
        label.config(text=f"\U0001f50a Claude  {i + 1} of {n}")
    elif n:
        label.config(text=f"\u2714 Done  ({n} paragraphs)")
    else:
        label.config(text="Waiting for Claude...")
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
    pause_btn.config(text="⏸ Pause")


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
    pause_btn.config(text="▶ Play")
    label.config(text=f"⏸ Paused  {i + 1} of {n}")


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
    off = os.path.exists(os.path.join(HERE, "ears_off"))
    mic_btn.config(text="\U0001f507 Mic off" if off else "\U0001f3a4 Mic on", bg="#555555" if off else "#1565C0")


def ears_watch():
    """What the ears last did, and any reader command they heard ("Hey Claude, pause")."""
    try:
        ears_label.config(text=open(os.path.join(HERE, "ears_status.txt"), encoding="utf-8").read()[:80])
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
                label.config(text=f"➡ Read on {elsewhere}")
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


buttons = {}
for text, cmd, bg in (("\u23ee Back", back, "#404040"), ("\u23f8 Pause", pause, "#2E7D32"),
                      ("\u23ed Skip", skip, "#404040"), ("\u23f9 Stop", stop, "#B71C1C")):
    buttons[cmd] = tk.Button(bar, text=text, command=cmd, bg=bg, fg="white", width=8,
                             font=("Arial", 10, "bold"))
    buttons[cmd].pack(side=tk.LEFT, padx=3, pady=6)
pause_btn = buttons[pause]
mic_btn = tk.Button(ears_bar, text="", command=mic_toggle, fg="white", width=10, font=("Arial", 9, "bold"))
mic_btn.pack(side=tk.LEFT, padx=(10, 3), pady=(0, 4))
tk.Button(ears_bar, text="\U0001f5e3 Talk", command=talk, bg="#6A1B9A", fg="white", width=8,
          font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=3, pady=(0, 4))


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
    wake_btn.config(text="\U0001f442 " + new)


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


voice_var = tk.StringVar(value=voice_name)
voice_menu = tk.OptionMenu(ears_bar, voice_var, *VOICES.keys(), command=pick_voice)
voice_menu.config(bg="#333333", fg="white", activebackground="#444444", activeforeground="white",
                  highlightthickness=0, font=("Arial", 9), width=14)
voice_menu["menu"].config(bg="#333333", fg="white")
voice_menu.pack(side=tk.LEFT, padx=3, pady=(0, 4))
wake_btn = tk.Button(ears_bar, text="\U0001f442 " + wake_word(), command=set_wake, bg="#333333", fg="white",
                     font=("Arial", 9))
wake_btn.pack(side=tk.LEFT, padx=3, pady=(0, 4))


def open_history():
    """The saved chats, searchable by keyword, date and project (chat_search.pyw)."""
    subprocess.Popen([EARS_PY, os.path.join(HERE, "chat_search.pyw")], cwd=HERE, creationflags=NO_WINDOW)


if os.path.exists(os.path.join(HERE, "chat_search.pyw")):
    tk.Button(ears_bar, text="\U0001f50e History", command=open_history, bg="#1565C0", fg="white",
              font=("Arial", 9)).pack(side=tk.LEFT, padx=3, pady=(0, 4))
ears_label.pack(side=tk.LEFT, padx=8, pady=(0, 4), fill=tk.X, expand=True)
show_mic()
keep_awake()
root.protocol("WM_DELETE_WINDOW", close)
root.update_idletasks()
root.geometry(f"+{root.winfo_screenwidth() - root.winfo_width() - 40}+{root.winfo_screenheight() - root.winfo_height() - 90}")
# start on the reply that's there now only if it's brand new; otherwise wait quietly for the next one
try:
    if os.path.exists(TEXT_FILE):
        import time
        if time.time() - os.path.getmtime(TEXT_FILE) > 15:
            state["mtime"] = os.path.getmtime(TEXT_FILE)
            with open(TEXT_FILE, encoding="utf-8") as f:
                state["paras"] = json.load(f)
            state["i"] = len(state["paras"])   # Back still reads the last reply
except (OSError, ValueError):
    pass
fill_text()
show()
root.after(100, watch)
start_ears()
root.after(1500, ears_watch)
root.mainloop()
