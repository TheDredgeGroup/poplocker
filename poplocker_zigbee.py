"""Pop Locker's Zigbee helper: keeps the Zigbee USB dongle connected and lets the home controller work Zigbee plugs.

It runs in the background and answers only on this PC (127.0.0.1:5056). The home controller starts it by itself the
first time a Zigbee device is used. It works with Silicon Labs dongles (SONOFF Dongle Plus MG24, SONOFF ZBDongle-E,
Home Assistant SkyConnect / ZBT-1 and others running Zigbee "EZSP" firmware). Windows needs the dongle's USB driver:
Device Manager > the dongle > Update driver > Search automatically (the CP210x driver comes from Windows Update).

  python poplocker_zigbee.py            run the helper (normally started by the home controller)
  python poplocker_zigbee.py --find     show which COM port the dongle is on
"""
import json, os, re, socket, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = os.path.join(HERE, "home")
PORT = 5056
LOG = os.path.join(HOME_DIR, "zigbee.log")
DB = os.path.join(HOME_DIR, "zigbee.db")

# USB ids of dongles with Silicon Labs radios (bellows talks to these)
DONGLES = {"VID_10C4&PID_EA60": "Silicon Labs CP210x (SONOFF Dongle Plus MG24 / ZBDongle-E)",
           "VID_1A86&PID_55D4": "SONOFF ZBDongle-E (CH9102)",
           "VID_10C4&PID_8A2A": "Home Assistant SkyConnect / ZBT-1"}


def log(text):
    os.makedirs(HOME_DIR, exist_ok=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def find_dongle():
    """-> (COM port, what it is) or (None, why not). Reads Windows' device list; no extra modules needed."""
    try:
        with open(os.path.join(HOME_DIR, "home.json"), encoding="utf-8") as f:
            port = (json.load(f).get("zigbee") or {}).get("port")
        if port:
            return port, "set in home.json"
    except (OSError, ValueError):
        pass
    if os.name != "nt":
        return None, "only Windows is supported"
    ps = ("Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match '\\(COM\\d+\\)' } | "
          "ForEach-Object { $_.PNPDeviceID + '|' + $_.Name }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=20, creationflags=0x08000000).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"couldn't read the device list ({e})"
    for line in out.splitlines():
        dev_id, _, name = line.partition("|")
        for usb, what in DONGLES.items():
            if usb in dev_id.upper():
                m = re.search(r"\((COM\d+)\)", name)
                if m:
                    return m.group(1), what
    return None, ("no Zigbee dongle found. Plug it in; if Device Manager shows it with a yellow sign, "
                  "update its driver (Search automatically)")


# ---- the helper ---------------------------------------------------------------------------------------------------
class Zigbee:
    def __init__(self, port):
        import asyncio
        self.asyncio = asyncio
        self.port = port
        self.loop = asyncio.new_event_loop()
        self.app = None
        self.joined = []          # devices that joined while pairing was open
        self.ready = threading.Event()
        self.error = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.asyncio.set_event_loop(self.loop)
        wait = 5
        while True:
            try:
                self.loop.run_until_complete(self._start())
                self.error = None
                break
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                log(f"couldn't start the dongle on {self.port} (trying again in {wait} s): {self.error}")
                self.ready.set()
                time.sleep(wait)
                wait = min(wait * 2, 60)
                found = find_dongle()[0]          # it may have been plugged into another USB port
                if found:
                    self.port = found
        self.ready.set()
        self.loop.run_forever()

    async def _start(self):
        import zigpy.config as zc
        from bellows.zigbee.application import ControllerApplication
        cfg = {zc.CONF_DEVICE: {zc.CONF_DEVICE_PATH: self.port, zc.CONF_DEVICE_BAUDRATE: 115200,
                                zc.CONF_DEVICE_FLOW_CONTROL: "software"},
               zc.CONF_DATABASE: DB, zc.CONF_OTA: {zc.CONF_OTA_ENABLED: False}}
        self.app = await ControllerApplication.new(cfg, auto_form=True, start_radio=True)
        helper = self

        class Listener:
            def device_initialized(self, device):
                helper.joined.append(device)
                log(f"joined: {device.ieee} {device.manufacturer} {device.model}")
        self.app.add_listener(Listener())
        s = self.app.state
        log(f"dongle ready on {self.port}: {getattr(s.node_info, 'model', '')} firmware "
            f"{getattr(s.node_info, 'version', '')}, channel {s.network_info.channel}, {len(self.devices())} devices")

    def call(self, coro, timeout=60):
        return self.asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def devices(self):
        return [d for d in self.app.devices.values() if d.nwk != 0x0000]

    @staticmethod
    def cluster(dev, cluster_id):
        for ep_id, ep in dev.endpoints.items():
            if ep_id and cluster_id in ep.in_clusters:
                return ep.in_clusters[cluster_id]
        return None

    def describe(self, d):
        return {"ieee": str(d.ieee), "maker": d.manufacturer, "model": d.model,
                "can": "on/off" if self.cluster(d, 0x0006) else "nothing Pop Locker can work yet",
                "what": FRIENDLY.get(d.model or "", "")}

    def device(self, ieee):
        for d in self.devices():
            if str(d.ieee).lower() == str(ieee).lower():
                return d
        raise LookupError("that Zigbee device isn't joined to this dongle (pair it again)")

    async def _retry(self, make, what, tries=4):
        for n in range(1, tries + 1):
            try:
                return await make()
            except Exception as e:
                if n == tries:
                    raise RuntimeError(f"{what}: it didn't answer ({type(e).__name__})") from e
                await self.asyncio.sleep(1.5)

    async def state(self, ieee):
        d = self.device(ieee)
        oo = self.cluster(d, 0x0006)
        if oo is None:
            raise LookupError("it has no on/off")
        r = await self._retry(lambda: oo.read_attributes(["on_off"], allow_cache=False), "reading it")
        out = {"on": bool(r[0].get("on_off"))}
        for ep_id, ep in d.endpoints.items():       # power: Aqara plugs give watts on an Analog Input (0x000C)
            if ep_id and 0x000C in ep.in_clusters:
                try:
                    p = await self._retry(lambda: ep.in_clusters[0x000C].read_attributes(
                        ["present_value"], allow_cache=False), "reading power", tries=2)
                    if p[0].get("present_value") is not None:
                        out["power"] = round(float(p[0]["present_value"]), 1)
                except RuntimeError:
                    pass
                break
        return out

    async def set(self, ieee, on):
        d = self.device(ieee)
        oo = self.cluster(d, 0x0006)
        if oo is None:
            raise LookupError("it has no on/off")
        await self._retry(lambda: (oo.on() if on else oo.off()), "switching it")
        await self.asyncio.sleep(1.0)
        return await self.state(ieee)

    async def pair(self, seconds):
        start = len(self.joined)
        await self.app.permit(seconds)
        end = time.time() + seconds
        while time.time() < end:
            await self.asyncio.sleep(1)
            if len(self.joined) > start:
                await self.asyncio.sleep(5)       # let it finish describing itself
                break
        return [self.describe(d) for d in self.joined[start:]]


FRIENDLY = {"lumi.plug.maus01": "Aqara Smart Plug (US)", "lumi.plug": "Aqara Smart Plug", "lumi.plug.mmeu01": "Aqara Smart Plug (EU)",
            "lumi.plug.maeu01": "Aqara Smart Plug (EU)", "lumi.switch.n0agl1": "Aqara Single Switch Module T1",
            "S26R2ZB": "SONOFF Zigbee Smart Plug", "S40ZBTPB": "SONOFF Zigbee Smart Plug", "ZBMINI": "SONOFF ZBMINI switch"}


def serve():
    port, what = find_dongle()
    if not port:
        print(f"Zigbee: {what}")
        log(f"not started: {what}")
        return 1
    zb = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/devices":
                return self._send(404, {"error": "not here"})
            problem = trouble()
            if problem:
                return self._send(503, {"error": problem})
            self._send(200, {"port": zb.port, "devices": [zb.describe(d) for d in zb.devices()]})

        def do_POST(self):
            try:
                a = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                problem = trouble()
                if problem:
                    return self._send(503, {"error": problem})
                if self.path == "/state":
                    return self._send(200, zb.call(zb.state(a["ieee"])))
                if self.path == "/set":
                    out = zb.call(zb.set(a["ieee"], bool(a["on"])))
                    log(f"{a['ieee']} {'on' if a['on'] else 'off'} -> {out}")
                    return self._send(200, out)
                if self.path == "/pair":
                    secs = max(30, min(int(a.get("seconds", 120)), 254))
                    log(f"pairing open for {secs} s")
                    return self._send(200, {"joined": zb.call(zb.pair(secs), timeout=secs + 30)})
                self._send(404, {"error": "not here"})
            except (LookupError, RuntimeError, KeyError, ValueError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                log(f"error on {self.path}: {type(e).__name__}: {e}")
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def trouble():
        if zb.error:
            return f"the Zigbee dongle on {zb.port} isn't working: {zb.error}"
        if zb.app is None:
            return "starting: the Zigbee dongle is still connecting"
        return None

    class OnlyOne(ThreadingHTTPServer):
        # Python's server lets a second program take the same port on Windows (SO_REUSEADDR); ask for it exclusively,
        # so a second helper fails here and leaves the dongle alone
        allow_reuse_address = False

        def server_bind(self):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    try:
        server = OnlyOne(("127.0.0.1", PORT), Handler)      # only one helper: the one that gets the port
    except OSError:
        print("Zigbee helper is already running.")
        return 0
    zb = Zigbee(port)
    log(f"helper started, dongle {port} ({what})")
    print(f"Zigbee helper on 127.0.0.1:{PORT}, dongle {port} ({what})")
    server.serve_forever()


if __name__ == "__main__":
    if "--find" in sys.argv:
        print(*find_dongle())
        sys.exit(0)
    sys.exit(serve() or 0)
