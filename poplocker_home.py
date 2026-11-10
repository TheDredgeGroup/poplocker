"""Pop Locker Home: the tools Claude uses to run the smart devices in the house.

Lights, plugs and switches that can be reached on the home network, with nothing extra to install:
Kasa (older firmware), WiZ, Shelly, Tasmota, Philips Hue, WLED, Govee (LAN control on), Yeelight (LAN control on)
and LIFX. Anything else - a TV, a projector, a garage door - can be added as a "command" device: a name for
each thing it can do and the program to run for it.

Three ways in, all doing the same things:
  python poplocker_home.py list | scan | on <name> | off <name> | brightness <name> 40 | color <name> red | ...
  python poplocker_home.py --mcp        the same tools for Claude (an MCP server on stdin/stdout)
  the schedule: steps saved with a time ("23:00", "sunset-20") run by themselves from the ears

Every change is read back from the device afterwards, so the answer says what really happened.
Everything about this house is in home/home.json in this folder (devices, schedule, where the house is).
"""
import colorsys, datetime, json, math, os, re, socket, struct, subprocess, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = os.path.join(HERE, "home")
HOME_FILE = os.path.join(HOME_DIR, "home.json")
LOG_FILE = os.path.join(HOME_DIR, "home.log")
TYPES = ("kasa", "wiz", "shelly", "tasmota", "hue", "wled", "govee", "yeelight", "lifx", "command")
LIGHT_ACTIONS = ("on", "off", "toggle", "brightness", "color", "temperature", "status")
COLORS = {"red": "#ff0000", "green": "#00ff00", "blue": "#0000ff", "white": "#ffffff", "yellow": "#ffff00",
          "orange": "#ff7f00", "purple": "#8000ff", "pink": "#ff40a0", "cyan": "#00ffff", "magenta": "#ff00ff",
          "teal": "#00b0a0", "lime": "#a0ff00", "amber": "#ffbf00", "gold": "#ffc800", "violet": "#9000ff",
          "turquoise": "#30d5c8", "lavender": "#b090ff", "warm white": "#ffb46b", "cool white": "#dce8ff"}
file_lock = threading.Lock()


class HomeError(Exception):
    """Something the person should be told in plain words."""


def log(text):
    try:
        os.makedirs(HOME_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


# ---- the house file -------------------------------------------------------------------------------------------
def load():
    try:
        with open(HOME_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("devices", [])
    data.setdefault("schedules", [])
    return data


def save(data):
    os.makedirs(HOME_DIR, exist_ok=True)
    tmp = HOME_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, HOME_FILE)


def find(target, data=None):
    """The devices a name means: one device, everything in a room, or "all". Names must match what's saved -
    Claude reads the list and picks; nothing here guesses."""
    devices = (data or load())["devices"]
    names = [t.strip() for t in target] if isinstance(target, list) else [str(target).strip()]
    out = []
    for name in names:
        key = name.casefold()
        hit = [d for d in devices if d["name"].casefold() == key]
        if not hit:
            hit = [d for d in devices if (d.get("room") or "").casefold() == key]
        if not hit and key in ("all", "everything"):
            hit = list(devices)
        if not hit:
            known = ", ".join(sorted(d["name"] for d in devices)) or "none yet"
            rooms = ", ".join(sorted({d["room"] for d in devices if d.get("room")}))
            raise HomeError(f'No device or room is called "{name}". Devices: {known}.' + (f" Rooms: {rooms}." if rooms else ""))
        out += [d for d in hit if d not in out]
    return out


# ---- colours --------------------------------------------------------------------------------------------------
def parse_color(value):
    """-> (r, g, b) from a colour name or #rrggbb."""
    v = str(value).strip().lower()
    v = COLORS.get(v, v)
    m = re.fullmatch(r"#?([0-9a-f]{6})", v)
    if not m:
        raise HomeError(f'"{value}" isn\'t a colour I know. Use a name ({", ".join(sorted(COLORS))}) or #rrggbb.')
    n = int(m.group(1), 16)
    return n >> 16, (n >> 8) & 255, n & 255


def hexcolor(r, g, b):
    return "#%02x%02x%02x" % (int(r) & 255, int(g) & 255, int(b) & 255)


def hsv(rgb):
    """-> hue 0-360, saturation 0-100, value 0-100"""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    return h * 360, s * 100, v * 100


def from_hsv(h, s, v=100):
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb((h % 360) / 360, s / 100, v / 100))


def number(value, low, high, what):
    try:
        n = int(round(float(str(value).strip().rstrip("%kK"))))
    except (TypeError, ValueError):
        raise HomeError(f"{what} needs a number from {low} to {high}.")
    if not low <= n <= high:
        raise HomeError(f"{what} goes from {low} to {high}.")
    return n


# ---- talking to devices ---------------------------------------------------------------------------------------
def http(url, body=None, method=None, timeout=4):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read(200_000).decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def udp_ask(ip, port, payload, timeout=2.0, tries=3, listen=0):
    """Send a datagram and return the first answer from that address (None when there isn't one)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if listen:
            s.bind(("", listen))
        s.settimeout(timeout / tries)
        for _ in range(tries):
            s.sendto(payload, (ip, port))
            end = time.time() + timeout / tries
            while time.time() < end:
                try:
                    data, addr = s.recvfrom(65535)
                except socket.timeout:
                    break
                except OSError:
                    continue
                if addr[0] == ip:
                    return data
        return None
    finally:
        s.close()


def udp_tell(ip, port, payload, times=2):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(times):
            s.sendto(payload, (ip, port))
            time.sleep(0.05)
    finally:
        s.close()


# Each kind of device has state(dev) -> {"on", "brightness", "color", "temperature"} (what it knows of them) and
# change(dev, on, brightness, rgb, kelvin) where None means "leave it".

# ---- Kasa (the older, unencrypted protocol on port 9999) ------------------------------------------------------
def _kasa_scramble(data):
    key, out = 171, bytearray()
    for c in data:
        key ^= c
        out.append(key)
    return bytes(out)


def _kasa_clear(data):
    key, out = 171, bytearray()
    for c in data:
        out.append(key ^ c)
        key = c
    return bytes(out)


def _kasa(dev, request):
    if dev.get("child"):
        request = dict(request, context={"child_ids": [dev["child"]]})
    body = _kasa_scramble(json.dumps(request).encode("utf-8"))
    with socket.create_connection((dev["ip"], 9999), timeout=4) as s:
        s.sendall(struct.pack(">I", len(body)) + body)
        head = b""
        while len(head) < 4:
            more = s.recv(4 - len(head))
            if not more:
                raise HomeError("the device closed the connection")
            head += more
        want, data = struct.unpack(">I", head)[0], b""
        while len(data) < want:
            more = s.recv(want - len(data))
            if not more:
                break
            data += more
    return json.loads(_kasa_clear(data))


KASA_BULB = "smartlife.iot.smartbulb.lightingservice"


def kasa_state(dev):
    info = _kasa(dev, {"system": {"get_sysinfo": {}}})["system"]["get_sysinfo"]
    if "light_state" in info:
        ls = info["light_state"]
        src = ls if ls.get("on_off") else ls.get("dft_on_state", {})
        st = {"on": bool(ls.get("on_off")), "brightness": src.get("brightness")}
        if src.get("color_temp"):
            st["temperature"] = src["color_temp"]
        elif src.get("saturation"):
            st["color"] = hexcolor(*from_hsv(src.get("hue", 0), src.get("saturation", 0)))
        return st
    if dev.get("child"):
        for c in info.get("children", []):
            if c.get("id") == dev["child"]:
                return {"on": bool(c.get("state"))}
    st = {"on": bool(info.get("relay_state"))}
    if "brightness" in info:
        st["brightness"] = info["brightness"]
    return st


def kasa_change(dev, on, brightness, rgb, kelvin):
    info = _kasa(dev, {"system": {"get_sysinfo": {}}})["system"]["get_sysinfo"]
    if "light_state" in info:
        new = {"ignore_default": 1}
        if on is not None:
            new["on_off"] = int(on)
        if brightness is not None:
            new["brightness"] = max(1, brightness)
        if rgb is not None:
            h, s, _ = hsv(rgb)
            new.update(hue=round(h), saturation=round(s), color_temp=0)
        if kelvin is not None:
            new["color_temp"] = kelvin
        _kasa(dev, {KASA_BULB: {"transition_light_state": new}})
        return
    if rgb is not None or kelvin is not None:
        raise HomeError("it's a plug or switch - it has no colour")
    if brightness is not None:
        if "brightness" not in info:
            raise HomeError("it's a plug or switch - it can't dim")
        _kasa(dev, {"smartlife.iot.dimmer": {"set_brightness": {"brightness": max(1, brightness)}}})
    if on is not None:
        _kasa(dev, {"system": {"set_relay_state": {"state": int(on)}}})


# ---- WiZ ------------------------------------------------------------------------------------------------------
def _wiz(dev, method, params):
    answer = udp_ask(dev["ip"], 38899, json.dumps({"method": method, "params": params}).encode("utf-8"))
    if answer is None:
        raise HomeError("no answer")
    return json.loads(answer.decode("utf-8", "replace"))


def wiz_state(dev):
    r = _wiz(dev, "getPilot", {}).get("result", {})
    st = {"on": bool(r.get("state")), "brightness": r.get("dimming")}
    if r.get("temp"):
        st["temperature"] = r["temp"]
    elif "r" in r:
        st["color"] = hexcolor(r.get("r", 0), r.get("g", 0), r.get("b", 0))
    return st


def wiz_change(dev, on, brightness, rgb, kelvin):
    p = {}
    if on is not None:
        p["state"] = bool(on)
    if brightness is not None:
        p["dimming"] = max(10, brightness)
    if rgb is not None:
        p.update(r=rgb[0], g=rgb[1], b=rgb[2])
    if kelvin is not None:
        p["temp"] = kelvin
    r = _wiz(dev, "setPilot", p)
    if "error" in r:
        raise HomeError(str(r["error"].get("message", r["error"])))


# ---- Shelly ---------------------------------------------------------------------------------------------------
def _shelly_kind(dev):
    """-> (generation, "switch" or "light"), remembered on the device entry for next time."""
    if dev.get("gen") and dev.get("kind"):
        return dev["gen"], dev["kind"]
    base = f"http://{dev['ip']}"
    gen = int((http(base + "/shelly") or {}).get("gen", 1))
    ch = dev.get("channel", 0)
    if gen >= 2:
        status = http(base + "/rpc/Shelly.GetStatus")
        kind = "light" if f"light:{ch}" in status else "switch"
    else:
        try:
            http(base + f"/light/{ch}")
            kind = "light"
        except urllib.error.HTTPError:
            kind = "switch"
    dev["gen"], dev["kind"] = gen, kind
    return gen, kind


def shelly_state(dev):
    gen, kind = _shelly_kind(dev)
    base, ch = f"http://{dev['ip']}", dev.get("channel", 0)
    if gen >= 2:
        r = http(base + f"/rpc/{kind.capitalize()}.GetStatus?id={ch}")
        st = {"on": bool(r.get("output"))}
    else:
        r = http(base + f"/{'light' if kind == 'light' else 'relay'}/{ch}")
        st = {"on": bool(r.get("ison"))}
    if kind == "light" and "brightness" in r:
        st["brightness"] = r["brightness"]
    return st


def shelly_change(dev, on, brightness, rgb, kelvin):
    gen, kind = _shelly_kind(dev)
    if rgb is not None or kelvin is not None:
        raise HomeError("colour isn't supported on Shelly devices yet")
    if brightness is not None and kind != "light":
        raise HomeError("it's a switch - it can't dim")
    base, ch, q = f"http://{dev['ip']}", dev.get("channel", 0), {}
    if gen >= 2:
        q["id"] = ch
        if on is not None:
            q["on"] = "true" if on else "false"
        if brightness is not None:
            q["brightness"] = brightness
        http(base + f"/rpc/{kind.capitalize()}.Set?" + urllib.parse.urlencode(q))
    else:
        if on is not None:
            q["turn"] = "on" if on else "off"
        if brightness is not None:
            q["brightness"] = max(1, brightness)
        http(base + f"/{'light' if kind == 'light' else 'relay'}/{ch}?" + urllib.parse.urlencode(q))


# ---- Tasmota --------------------------------------------------------------------------------------------------
def _tasmota(dev, command):
    return http(f"http://{dev['ip']}/cm?cmnd=" + urllib.parse.quote(command))


def tasmota_state(dev):
    r = _tasmota(dev, "State")
    ch = dev.get("channel")
    power = r.get(f"POWER{ch}") if ch else r.get("POWER", r.get("POWER1"))
    st = {"on": power == "ON"}
    if "Dimmer" in r:
        st["brightness"] = r["Dimmer"]
    if r.get("CT") and str(r.get("Color", ""))[:6] in ("000000", ""):
        st["temperature"] = round(1_000_000 / r["CT"])
    elif re.fullmatch(r"[0-9A-Fa-f]{6,10}", str(r.get("Color", ""))):
        st["color"] = "#" + r["Color"][:6].lower()
    return st


def tasmota_change(dev, on, brightness, rgb, kelvin):
    ch, cmds = dev.get("channel") or "", []
    if rgb is not None:
        cmds.append("Color " + hexcolor(*rgb)[1:])
    if kelvin is not None:
        cmds.append(f"CT {max(153, min(500, round(1_000_000 / kelvin)))}")
    if brightness is not None:
        cmds.append(f"Dimmer {brightness}")
    if on is not None:
        cmds.append(f"Power{ch} {'On' if on else 'Off'}")
    r = _tasmota(dev, cmds[0] if len(cmds) == 1 else "Backlog " + "; ".join(cmds))
    if isinstance(r, dict) and r.get("Command") == "Unknown":
        raise HomeError("the device doesn't know that command (a plug has no dimmer or colour)")


# ---- Philips Hue (through the bridge; each light is its own device) --------------------------------------------
def _hue(dev, path="", body=None):
    r = http(f"http://{dev['ip']}/api/{dev['key']}/lights/{dev['light']}{path}", body, "PUT" if body is not None else None)
    errors = [e["error"].get("description", "error") for e in r if isinstance(e, dict) and "error" in e] if isinstance(r, list) else []
    if errors:
        raise HomeError("; ".join(errors))
    return r


def hue_state(dev):
    s = _hue(dev).get("state", {})
    if s.get("reachable") is False:
        raise HomeError("the bridge can't reach it (is it switched off at the wall?)")
    st = {"on": bool(s.get("on"))}
    if "bri" in s:
        st["brightness"] = max(1, round(s["bri"] / 254 * 100))
    if s.get("colormode") == "ct" and s.get("ct"):
        st["temperature"] = round(1_000_000 / s["ct"])
    elif s.get("colormode") == "hs":
        st["color"] = hexcolor(*from_hsv(s.get("hue", 0) / 65535 * 360, s.get("sat", 0) / 254 * 100))
    return st


def hue_change(dev, on, brightness, rgb, kelvin):
    b = {}
    if on is not None:
        b["on"] = bool(on)
    if brightness is not None:
        b["bri"] = max(1, round(brightness / 100 * 254))
    if rgb is not None:
        h, s, _ = hsv(rgb)
        b.update(hue=round(h / 360 * 65535) % 65536, sat=round(s / 100 * 254))
    if kelvin is not None:
        b["ct"] = max(153, min(500, round(1_000_000 / kelvin)))
    _hue(dev, "/state", b)


def hue_pair(bridge_ip, room=None):
    """Press the round button on the Hue bridge, then call this: it gets a key and adds every light."""
    r = http(f"http://{bridge_ip}/api", {"devicetype": "poplocker#home"})
    first = r[0] if isinstance(r, list) and r else {}
    if "success" not in first:
        why = first.get("error", {}).get("description", "no answer from a Hue bridge at that address")
        raise HomeError(f"The bridge said: {why}. Press the round button on top of the bridge, then try again within 30 seconds.")
    key = first["success"]["username"]
    lights = http(f"http://{bridge_ip}/api/{key}/lights")
    added = []
    with file_lock:
        data = load()
        for light_id, light in sorted(lights.items(), key=lambda kv: int(kv[0])):
            name = _free_name(data, light.get("name") or f"Hue light {light_id}")
            dev = {"name": name, "type": "hue", "ip": bridge_ip, "key": key, "light": light_id}
            if room:
                dev["room"] = room
            data["devices"].append(dev)
            added.append(name)
        save(data)
    log(f"hue bridge {bridge_ip} paired, {len(added)} lights added")
    return f"Paired with the Hue bridge and added {len(added)} lights: {', '.join(added) or 'none'}."


# ---- WLED -----------------------------------------------------------------------------------------------------
def wled_state(dev):
    r = http(f"http://{dev['ip']}/json/state")
    st = {"on": bool(r.get("on")), "brightness": max(1, round(r.get("bri", 0) / 255 * 100))}
    try:
        st["color"] = hexcolor(*r["seg"][0]["col"][0][:3])
    except (KeyError, IndexError, TypeError):
        pass
    return st


def wled_change(dev, on, brightness, rgb, kelvin):
    b = {}
    if on is not None:
        b["on"] = bool(on)
    if brightness is not None:
        b["bri"] = max(1, round(brightness / 100 * 255))
    if kelvin is not None:
        rgb = (255, 180, 107) if kelvin < 3500 else ((255, 228, 206) if kelvin < 5000 else (255, 255, 255))
    if rgb is not None:
        b["seg"] = [{"col": [list(rgb)]}]
    http(f"http://{dev['ip']}/json/state", b)


# ---- Govee (needs "LAN Control" switched on for the light in the Govee app) ------------------------------------
def _govee(cmd, data):
    return json.dumps({"msg": {"cmd": cmd, "data": data}}).encode("utf-8")


def govee_state(dev):
    answer = udp_ask(dev["ip"], 4003, _govee("devStatus", {}), listen=4002)
    if answer is None:
        raise HomeError("no answer (is LAN Control on for it in the Govee app?)")
    d = json.loads(answer.decode("utf-8", "replace"))["msg"]["data"]
    st = {"on": bool(d.get("onOff")), "brightness": d.get("brightness")}
    if d.get("colorTemInKelvin"):
        st["temperature"] = d["colorTemInKelvin"]
    elif isinstance(d.get("color"), dict):
        st["color"] = hexcolor(d["color"].get("r", 0), d["color"].get("g", 0), d["color"].get("b", 0))
    return st


def govee_change(dev, on, brightness, rgb, kelvin):
    ip = dev["ip"]
    if rgb is not None:
        udp_tell(ip, 4003, _govee("colorwc", {"color": dict(zip("rgb", rgb)), "colorTemInKelvin": 0}))
    if kelvin is not None:
        udp_tell(ip, 4003, _govee("colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": kelvin}))
    if brightness is not None:
        udp_tell(ip, 4003, _govee("brightness", {"value": max(1, brightness)}))
    if on is not None:
        udp_tell(ip, 4003, _govee("turn", {"value": int(on)}))


# ---- Yeelight (needs "LAN Control" switched on for the bulb in the Yeelight app) --------------------------------
def _yeelight(dev, method, params):
    with socket.create_connection((dev["ip"], 55443), timeout=4) as s:
        s.sendall((json.dumps({"id": 1, "method": method, "params": params}) + "\r\n").encode("utf-8"))
        s.settimeout(4)
        buf = b""
        while True:
            more = s.recv(4096)
            if not more:
                raise HomeError("the bulb closed the connection")
            buf += more
            for line in buf.split(b"\r\n")[:-1] if buf.endswith(b"\r\n") else buf.split(b"\r\n")[:-1]:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("id") == 1:
                    if "error" in r:
                        raise HomeError(str(r["error"].get("message", r["error"])))
                    return r.get("result", [])


def yeelight_state(dev):
    power, bright, rgb, ct, mode = (_yeelight(dev, "get_prop", ["power", "bright", "rgb", "ct", "color_mode"]) + [""] * 5)[:5]
    st = {"on": power == "on"}
    if str(bright).isdigit():
        st["brightness"] = int(bright)
    if str(mode) == "2" and str(ct).isdigit():
        st["temperature"] = int(ct)
    elif str(rgb).isdigit() and str(mode) in ("1", "3"):
        st["color"] = "#%06x" % int(rgb)
    return st


def yeelight_change(dev, on, brightness, rgb, kelvin):
    if on is False:
        _yeelight(dev, "set_power", ["off", "smooth", 300])
        return
    if on or brightness is not None or rgb is not None or kelvin is not None:
        _yeelight(dev, "set_power", ["on", "smooth", 300])      # the other settings are refused while it's off
    if rgb is not None:
        _yeelight(dev, "set_rgb", [max(1, (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]), "smooth", 300])
    if kelvin is not None:
        _yeelight(dev, "set_ct_abx", [max(1700, min(6500, kelvin)), "smooth", 300])
    if brightness is not None:
        _yeelight(dev, "set_bright", [max(1, brightness), "smooth", 300])


# ---- LIFX -----------------------------------------------------------------------------------------------------
def _lifx_packet(kind, payload=b"", want_answer=False):
    size = 36 + len(payload)
    frame = struct.pack("<HHI", size, 0x3400, 0x504C)          # for every light at this address; our own source id
    address = b"\0" * 8 + b"\0" * 6 + bytes([1 if want_answer else 0, 1])
    return frame + address + b"\0" * 8 + struct.pack("<HH", kind, 0) + payload


def _lifx_read(dev):
    """-> (hue, saturation, brightness, kelvin, power) as the light's own 0-65535 numbers"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.7)
        for _ in range(3):
            s.sendto(_lifx_packet(101, want_answer=True), (dev["ip"], 56700))
            end = time.time() + 0.7
            while time.time() < end:
                try:
                    data, addr = s.recvfrom(1024)
                except socket.timeout:
                    break
                if addr[0] == dev["ip"] and len(data) >= 48 and struct.unpack_from("<H", data, 32)[0] == 107:
                    h, sat, b, k, _, power = struct.unpack_from("<HHHHhH", data, 36)
                    return h, sat, b, k, power
    finally:
        s.close()
    raise HomeError("no answer")


def lifx_state(dev):
    h, s, b, k, power = _lifx_read(dev)
    st = {"on": power > 0, "brightness": max(1, round(b / 65535 * 100))}
    if s > 650:
        st["color"] = hexcolor(*from_hsv(h / 65535 * 360, s / 65535 * 100))
    else:
        st["temperature"] = k
    return st


def lifx_change(dev, on, brightness, rgb, kelvin):
    if brightness is not None or rgb is not None or kelvin is not None:
        h, s, b, k, _ = _lifx_read(dev)
        if rgb is not None:
            hh, ss, _ = hsv(rgb)
            h, s = round(hh / 360 * 65535) % 65536, round(ss / 100 * 65535)
        if kelvin is not None:
            s, k = 0, max(1500, min(9000, kelvin))
        if brightness is not None:
            b = max(655, round(brightness / 100 * 65535))
        udp_tell(dev["ip"], 56700, _lifx_packet(102, struct.pack("<BHHHHI", 0, h, s, b, k, 300)))
    if on is not None:
        udp_tell(dev["ip"], 56700, _lifx_packet(117, struct.pack("<HI", 65535 if on else 0, 300)))


DRIVERS = {"kasa": (kasa_state, kasa_change), "wiz": (wiz_state, wiz_change), "shelly": (shelly_state, shelly_change),
           "tasmota": (tasmota_state, tasmota_change), "hue": (hue_state, hue_change), "wled": (wled_state, wled_change),
           "govee": (govee_state, govee_change), "yeelight": (yeelight_state, yeelight_change),
           "lifx": (lifx_state, lifx_change)}


# ---- "command" devices: anything a program on this PC can work --------------------------------------------------
def run_command(dev, action, value):
    actions = dev.get("actions") or {}
    if action not in actions:
        raise HomeError(f"it can do: {', '.join(sorted(actions)) or 'nothing yet'}")
    cmd = actions[action]
    if isinstance(cmd, list):
        if value is None and any("{value}" in str(a) for a in cmd):
            raise HomeError(f'"{action}" needs a value')
        cmd = [str(a).replace("{value}", str(value if value is not None else "")) for a in cmd]
        shell = False
    else:
        if value is None and "{value}" in cmd:
            raise HomeError(f'"{action}" needs a value')
        if value is not None and re.search(r'[&|<>^%!"`$;\r\n]', str(value)):
            raise HomeError("that value has characters that aren't allowed in a command")
        cmd, shell = cmd.replace("{value}", str(value if value is not None else "")), True
    try:
        p = subprocess.run(cmd, shell=shell, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=int(dev.get("timeout", 90)), cwd=dev.get("cwd") or None, stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise HomeError("its program didn't finish in time")
    except OSError as e:
        raise HomeError(f"its program couldn't be started: {e}")
    out = ((p.stdout or "").strip() or (p.stderr or "").strip())[-600:]
    if p.returncode != 0:
        raise HomeError(f"its program failed: {out or 'exit code ' + str(p.returncode)}")
    return out or "done"


# ---- doing things ---------------------------------------------------------------------------------------------
def describe(st):
    if "says" in st:
        return st["says"]
    parts = ["on" if st.get("on") else "off"]
    if st.get("on") and st.get("brightness") is not None:
        parts.append(f"{st['brightness']}%")
    if st.get("on") and st.get("color"):
        parts.append(st["color"])
    if st.get("on") and st.get("temperature"):
        parts.append(f"{st['temperature']}K")
    return " ".join(parts)


def read_state(dev):
    if dev["type"] == "command":
        if "status" in (dev.get("actions") or {}):
            return {"says": run_command(dev, "status", None)}
        raise HomeError("it can't be asked how it is")
    try:
        return DRIVERS[dev["type"]][0](dev)
    except HomeError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise HomeError(f"it didn't answer ({type(e).__name__}: {e})"[:160])


def act_one(dev, action, value=None):
    """Do one thing to one device. -> a sentence saying what really happened (read back from the device)."""
    name = dev["name"]
    try:
        if dev["type"] == "command":
            return f"{name}: {run_command(dev, action, value)}"
        if action not in LIGHT_ACTIONS:
            raise HomeError(f"it can do: {', '.join(LIGHT_ACTIONS)}")
        if action == "status":
            return f"{name}: {describe(read_state(dev))}"
        on = brightness = rgb = kelvin = None
        if action == "on":
            on = True
        elif action == "off":
            on = False
        elif action == "toggle":
            on = not read_state(dev).get("on")
        elif action == "brightness":
            brightness = number(value, 0, 100, "Brightness")
            on = brightness > 0
            brightness = brightness or None
        elif action == "color":
            rgb, on = parse_color(value), True
        elif action == "temperature":
            kelvin, on = number(value, 1500, 9000, "Colour temperature (in kelvin)"), True
        try:
            DRIVERS[dev["type"]][1](dev, on, brightness, rgb, kelvin)
        except HomeError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise HomeError(f"it didn't answer ({type(e).__name__}: {e})"[:160])
        # read it back: the answer is what the device says, not what was asked for
        after, wrong = None, ""
        for wait in (0.5, 1.2):
            time.sleep(wait)
            try:
                after = read_state(dev)
            except HomeError:
                continue
            wrong = ""
            if on is not None and after.get("on") is not on:
                wrong = f"still {'on' if after.get('on') else 'off'}"
            elif brightness is not None and after.get("brightness") is not None and abs(after["brightness"] - brightness) > 6:
                wrong = f"brightness is {after['brightness']}%, not {brightness}%"
            if not wrong:
                break
        if after is None:
            return f"{name}: sent, but it couldn't be read back to check."
        if wrong:
            return f"{name}: FAILED - asked for {action}{' ' + str(value) if value is not None else ''} but it's {wrong}."
        return f"{name}: {describe(after)} (checked)."
    except HomeError as e:
        return f"{name}: FAILED - {e}"
    finally:
        log(f"{name} {action} {'' if value is None else value}".rstrip())


def control(target, action, value=None):
    devices = find(target)
    action = str(action).strip().lower()
    if len(devices) == 1:
        lines = [act_one(devices[0], action, value)]
    else:
        with ThreadPoolExecutor(min(12, len(devices))) as ex:
            lines = list(ex.map(lambda d: act_one(d, action, value), devices))
    _remember_kinds(devices)
    return "\n".join(lines)


def _remember_kinds(devices):
    """Save what a Shelly turned out to be, so it isn't asked every time."""
    found = {d["name"]: (d["gen"], d["kind"]) for d in devices if d.get("type") == "shelly" and d.get("gen")}
    if not found:
        return
    with file_lock:
        data, changed = load(), False
        for d in data["devices"]:
            if d["name"] in found and (d.get("gen"), d.get("kind")) != found[d["name"]]:
                d["gen"], d["kind"] = found[d["name"]]
                changed = True
        if changed:
            save(data)


def list_devices(with_state=True):
    data = load()
    devices = data["devices"]
    if not devices:
        return "No devices have been added yet. Use scan to look for some, or add_device."

    def line(d):
        where = f" [{d['room']}]" if d.get("room") else ""
        if d["type"] == "command":
            return f"{d['name']}{where} - can: {', '.join(sorted(d.get('actions') or {}))}"
        if not with_state:
            return f"{d['name']}{where} - {d['type']} light/plug"
        try:
            return f"{d['name']}{where} - {d['type']}, {describe(read_state(d))}"
        except HomeError as e:
            return f"{d['name']}{where} - {d['type']}, NOT ANSWERING ({e})"
    with ThreadPoolExecutor(min(12, len(devices))) as ex:
        lines = list(ex.map(line, devices))
    return "\n".join(lines) + f"\nLights and plugs can: {', '.join(LIGHT_ACTIONS)}."


def _free_name(data, name):
    taken = {d["name"].casefold() for d in data["devices"]}
    base, n = name.strip() or "Device", 2
    name = base
    while name.casefold() in taken:
        name, n = f"{base} {n}", n + 1
    return name


def add_device(name, kind, ip="", room="", light="", key="", channel=None, child=""):
    kind = str(kind).strip().lower()
    if kind == "command":
        raise HomeError('A "command" device runs a program on this PC, so it is added by hand: put it in home/home.json '
                        '(see the README) or ask Claude in the normal Claude Code chat to add it.')
    if kind not in DRIVERS:
        raise HomeError(f"The kinds are: {', '.join(DRIVERS)}.")
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", str(ip).strip()):
        raise HomeError("It needs the device's address on the home network, like 192.168.1.50.")
    dev = {"name": str(name).strip(), "type": kind, "ip": str(ip).strip()}
    if not dev["name"]:
        raise HomeError("It needs a name.")
    if room:
        dev["room"] = str(room).strip()
    if kind == "hue":
        if not (light and key):
            raise HomeError("Hue lights are added with hue_pair (press the bridge's button first).")
        dev.update(light=str(light), key=str(key))
    if channel not in (None, ""):
        dev["channel"] = int(channel)
    if child:
        dev["child"] = str(child)
    with file_lock:
        data = load()
        if any(d["name"].casefold() == dev["name"].casefold() for d in data["devices"]):
            raise HomeError(f'There is already a device called "{dev["name"]}".')
        data["devices"].append(dev)
        save(data)
    log(f"added {dev['name']} ({kind} {dev['ip']})")
    try:
        return f'Added "{dev["name"]}". It answers: {describe(read_state(dev))}.'
    except HomeError as e:
        return f'Added "{dev["name"]}", but it is NOT answering yet ({e}).'


def change_device(name, new_name="", room=None):
    with file_lock:
        data = load()
        devs = [d for d in data["devices"] if d["name"].casefold() == str(name).strip().casefold()]
        if not devs:
            raise HomeError(f'No device is called "{name}".')
        d = devs[0]
        if new_name and new_name.strip().casefold() != d["name"].casefold():
            if any(o["name"].casefold() == new_name.strip().casefold() for o in data["devices"]):
                raise HomeError(f'There is already a device called "{new_name}".')
            for s in data["schedules"]:
                for step in s.get("steps", []):
                    if str(step.get("target", "")).casefold() == d["name"].casefold():
                        step["target"] = new_name.strip()
            d["name"] = new_name.strip()
        if room is not None:
            if str(room).strip():
                d["room"] = str(room).strip()
            else:
                d.pop("room", None)
        save(data)
    return f'It is now "{d["name"]}"' + (f' in room "{d["room"]}".' if d.get("room") else ".")


def remove_device(name):
    with file_lock:
        data = load()
        keep = [d for d in data["devices"] if d["name"].casefold() != str(name).strip().casefold()]
        if len(keep) == len(data["devices"]):
            raise HomeError(f'No device is called "{name}".')
        data["devices"] = keep
        save(data)
    log(f"removed {name}")
    return f'Removed "{name}".'


# ---- looking for devices on the network -------------------------------------------------------------------------
def my_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))        # nothing is sent: this only picks the network card that leads out
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _shout(port, payload, dest, me, wait=2.0, listen=0):
    """Send to everyone and collect the answers. -> [(address, data)]"""
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((me, 0))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(me))
        ear = s
        if listen:
            ear = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ear.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ear.bind((me, listen))
        ear.settimeout(0.3)
        for _ in range(2):
            s.sendto(payload, (dest, port))
        end = time.time() + wait
        while time.time() < end:
            try:
                data, addr = ear.recvfrom(65535)
                if addr[0] != me:
                    out.append((addr[0], data))
            except OSError:
                pass
        s.close()
        if listen:
            ear.close()
    except OSError:
        pass
    return out


def scan():
    """Look for devices on the home network that aren't saved yet. Only asks; changes nothing."""
    me = my_address()
    everyone = me.rsplit(".", 1)[0] + ".255"
    found, jobs = {}, []          # (kind, ip, child) -> description

    def note(kind, ip, what, child=""):
        found.setdefault((kind, ip, child), what)

    def kasa():
        for ip, d in _shout(9999, _kasa_scramble(b'{"system":{"get_sysinfo":{}}}'), everyone, me):
            try:
                info = json.loads(_kasa_clear(d))["system"]["get_sysinfo"]
            except (ValueError, KeyError):
                continue
            kids = info.get("children") or []
            if kids:
                for c in kids:
                    note("kasa", ip, f'"{c.get("alias")}" on the strip "{info.get("alias")}" ({info.get("model")})', c.get("id", ""))
            else:
                note("kasa", ip, f'"{info.get("alias")}" ({info.get("model")})')

    def kasa_new():
        for ip, d in _shout(20002, bytes.fromhex("020000010000000000000000463cb5d3"), everyone, me):
            if not any(k[1] == ip for k in found):
                found[("tplink-new", ip, "")] = ("a newer Kasa/Tapo device - it only talks with the TP-Link account "
                                                 "password, which Pop Locker doesn't support yet")

    def wiz():
        for ip, d in _shout(38899, b'{"method":"getSystemConfig","params":{}}', everyone, me):
            try:
                r = json.loads(d.decode("utf-8", "replace")).get("result", {})
            except ValueError:
                r = {}
            note("wiz", ip, f'WiZ light ({r.get("moduleName", "unknown model")})')

    def lifx():
        for ip, d in _shout(56700, struct.pack("<HHI", 36, 0x3400, 0x504C) + b"\0" * 14 + bytes([1, 1]) + b"\0" * 8
                            + struct.pack("<HH", 101, 0), everyone, me):
            if len(d) >= 88 and struct.unpack_from("<H", d, 32)[0] == 107:
                label = d[48:80].split(b"\0")[0].decode("utf-8", "replace")
                note("lifx", ip, f'LIFX light "{label}"')

    def yeelight():
        msg = b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1982\r\nMAN: "ssdp:discover"\r\nST: wifi_bulb\r\n\r\n'
        for ip, d in _shout(1982, msg, "239.255.255.250", me):
            t = d.decode("utf-8", "replace")
            m = re.search(r"(?im)^name:\s*(.*)$", t)
            model = re.search(r"(?im)^model:\s*(.*)$", t)
            note("yeelight", ip, f'Yeelight {model.group(1).strip() if model else ""} "{m.group(1).strip() if m else ""}"')

    def govee():
        for ip, d in _shout(4001, _govee("scan", {"account_topic": "reserve"}), "239.255.255.250", me, listen=4002):
            try:
                sku = json.loads(d.decode("utf-8", "replace"))["msg"]["data"].get("sku", "")
            except (ValueError, KeyError):
                sku = ""
            note("govee", ip, f"Govee light {sku}")

    def hue_ssdp():
        msg = b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n'
        for ip, d in _shout(1900, msg, "239.255.255.250", me, wait=3.0):
            if b"ipbridge" in d.lower() or b"hue-bridgeid" in d.lower():
                note("hue-bridge", ip, "Philips Hue bridge - press its button, then use hue_pair")

    def web(ip):
        try:
            with socket.create_connection((ip, 80), timeout=0.6):
                pass
        except OSError:
            return
        for path, kind in (("/shelly", "shelly"), ("/json/info", "wled"), ("/cm?cmnd=Status%200", "tasmota"),
                           ("/api/config", "hue-bridge")):
            try:
                r = http(f"http://{ip}{path}", timeout=1.5)
            except (OSError, ValueError):
                continue
            if not isinstance(r, dict):
                continue
            if kind == "shelly" and ("mac" in r and ("type" in r or "gen" in r or "app" in r)):
                return note("shelly", ip, f'Shelly {r.get("app") or r.get("type") or ""} "{r.get("name") or r.get("id") or ""}"')
            if kind == "wled" and r.get("brand") == "WLED":
                return note("wled", ip, f'WLED strip "{r.get("name", "")}"')
            if kind == "tasmota" and "Status" in r:
                return note("tasmota", ip, f'Tasmota "{(r["Status"].get("DeviceName") or "")}"')
            if kind == "hue-bridge" and "bridgeid" in r:
                return note("hue-bridge", ip, f'Philips Hue bridge "{r.get("name", "")}" - press its button, then use hue_pair')

    def neighbours():
        try:
            out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10,
                                 creationflags=0x08000000 if os.name == "nt" else 0).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        prefix = me.rsplit(".", 1)[0] + "."
        return sorted({m for m in re.findall(r"(\d+\.\d+\.\d+\.\d+)", out)
                       if m.startswith(prefix) and not m.endswith((".255", ".0")) and m != me})

    with ThreadPoolExecutor(40) as ex:
        jobs = [ex.submit(f) for f in (kasa, wiz, lifx, yeelight, govee, hue_ssdp)]
        jobs += [ex.submit(web, ip) for ip in neighbours()]
        for j in jobs:
            j.result()
    kasa_new()
    saved = {(d["type"], d.get("ip"), d.get("child", "")) for d in load()["devices"]}
    hue_saved = {d.get("ip") for d in load()["devices"] if d["type"] == "hue"}
    new, old = [], 0
    for (kind, ip, child), what in sorted(found.items(), key=lambda kv: tuple(int(p) for p in kv[0][1].split("."))):
        if (kind, ip, child) in saved or (kind == "hue-bridge" and ip in hue_saved):
            old += 1
            continue
        extra = f", child {child}" if child else ""
        new.append(f"{kind} at {ip}{extra}: {what}")
    head = f"Found {len(new)} new device{'s' if len(new) != 1 else ''}" + (f" ({old} already saved)" if old else "") + "."
    if not new:
        head += (" Nothing that Pop Locker can work answered on this network. Lights that only work through their "
                 "maker's cloud app (most Tuya / Smart Life ones) don't answer here.")
    return "\n".join([head] + new)


# ---- the schedule -----------------------------------------------------------------------------------------------
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def sun_times(day, lat, lon):
    """-> (sunrise, sunset) as local times on that day, or (None, None) where the sun doesn't rise or set."""
    n = day.toordinal() - 730120                      # days since noon on 1 Jan 2000
    j = n - lon / 360
    m = math.radians((357.5291 + 0.98560028 * j) % 360)
    c = 1.9148 * math.sin(m) + 0.02 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)
    transit = j + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    decl = math.asin(math.sin(lam) * math.sin(math.radians(23.4397)))
    phi = math.radians(lat)
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(decl)) / (math.cos(phi) * math.cos(decl))
    if not -1 <= cos_w <= 1:
        return None, None
    w = math.degrees(math.acos(cos_w)) / 360
    noon2000 = datetime.datetime(2000, 1, 1, 12, tzinfo=datetime.timezone.utc)

    def local(days):
        return (noon2000 + datetime.timedelta(days=days)).astimezone().replace(tzinfo=None)
    return local(transit - w), local(transit + w)


def parse_when(when):
    """"23:00", "7:30am", "sunset", "sunrise+15", "sunset-20" -> ("clock", minutes) or ("sunset", offset minutes)"""
    w = str(when).strip().lower().replace(" ", "")
    m = re.fullmatch(r"(sunrise|sunset)(?:([+-])(\d{1,3}))?", w)
    if m:
        return m.group(1), int(m.group(3) or 0) * (-1 if m.group(2) == "-" else 1)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", w)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3):
            if not 1 <= h <= 12:
                raise HomeError("That isn't a time.")
            h = h % 12 + (12 if m.group(3) == "pm" else 0)
        if h <= 23 and mi <= 59:
            return "clock", h * 60 + mi
    raise HomeError('The time is like "23:00", "7:30am", "sunset" or "sunset-20" (minutes before).')


def fire_time(s, day, data):
    """When schedule s goes off on that day (a datetime), or None when it doesn't that day."""
    days = s.get("days", "daily")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(days)):
        if days != day.isoformat():
            return None
    elif days != "daily" and DAYS[day.weekday()] not in days:
        return None
    kind, n = parse_when(s["when"])
    midnight = datetime.datetime.combine(day, datetime.time())
    if kind == "clock":
        return midnight + datetime.timedelta(minutes=n)
    loc = data.get("location")
    if not loc:
        return None
    rise, sets = sun_times(day, loc["lat"], loc["lon"])
    base = rise if kind == "sunrise" else sets
    return base + datetime.timedelta(minutes=n) if base else None


def add_schedule(when, steps=None, days="daily", ask="", note=""):
    kind, _ = parse_when(when)
    if isinstance(days, list):
        days = [str(d).strip().lower()[:3] for d in days]
    else:
        text = str(days).strip().lower()
        if text in ("", "daily", "every day", "everyday"):
            days = "daily"
        elif text in ("weekdays",):
            days = DAYS[:5]
        elif text in ("weekends",):
            days = DAYS[5:]
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            datetime.date.fromisoformat(text)
            days = text
        else:
            days = [d.strip()[:3] for d in re.split(r"[,\s]+", text) if d.strip()]
    if isinstance(days, list) and (not days or any(d not in DAYS for d in days)):
        raise HomeError('Days are "daily", "weekdays", "weekends", a list like ["mon","wed"], or one date like "2026-12-24".')
    steps = steps or []
    if not steps and not ask:
        raise HomeError("It needs steps (device actions) or something to ask Claude at that time.")
    with file_lock:
        data = load()
        for step in steps:
            find(step.get("target", ""), data)                 # the names must exist now
            if not step.get("action"):
                raise HomeError("Each step needs an action.")
        if kind != "clock" and not data.get("location"):
            raise HomeError("Sunrise and sunset need to know where the house is: use set_location first "
                            "(ask the person for their town if you don't know it).")
        new_id = max([s.get("id", 0) for s in data["schedules"]] + [0]) + 1
        s = {"id": new_id, "when": str(when).strip().lower().replace(" ", ""), "days": days}
        if steps:
            s["steps"] = [{k: v for k, v in step.items() if k in ("target", "action", "value") and v not in (None, "")}
                          for step in steps]
        if ask:
            s["ask"] = str(ask)
        if note:
            s["note"] = str(note)
        # a time that has already gone by today waits for its next day
        today = datetime.date.today()
        at = fire_time(s, today, data)
        if at and at <= datetime.datetime.now():
            s["last"] = today.isoformat()
        data["schedules"].append(s)
        save(data)
    log(f"schedule {new_id} added: {s}")
    return f"Saved as schedule {new_id}: {describe_schedule(s, data)}"


def describe_schedule(s, data=None):
    days = s.get("days", "daily")
    days = days if isinstance(days, str) else ", ".join(days)
    what = "; ".join(f"{st.get('target')} {st.get('action')}{' ' + str(st['value']) if 'value' in st else ''}"
                     for st in s.get("steps", []))
    if s.get("ask"):
        what = (what + "; " if what else "") + f'ask Claude: "{s["ask"]}"'
    line = f"{s['when']} {days} - {what}"
    if data is not None and parse_when(s["when"])[0] != "clock":
        at = fire_time(s, datetime.date.today(), data)
        if at:
            line += f" (today that's {at.strftime('%H:%M')})"
    return line


def list_schedules():
    data = load()
    if not data["schedules"]:
        return "Nothing is scheduled."
    return "\n".join(f"{s['id']}: {describe_schedule(s, data)}" for s in data["schedules"])


def remove_schedule(schedule_id):
    with file_lock:
        data = load()
        keep = [s for s in data["schedules"] if str(s.get("id")) != str(schedule_id).strip()]
        if len(keep) == len(data["schedules"]):
            raise HomeError(f"There is no schedule {schedule_id}.")
        data["schedules"] = keep
        save(data)
    log(f"schedule {schedule_id} removed")
    return f"Schedule {schedule_id} removed."


def set_location(lat, lon, place=""):
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HomeError("Latitude is -90 to 90 and longitude -180 to 180 (west is negative).")
    with file_lock:
        data = load()
        data["location"] = {"lat": lat, "lon": lon, "place": str(place)}
        save(data)
    rise, sets = sun_times(datetime.date.today(), lat, lon)
    if not rise:
        return "Saved. The sun doesn't rise or set there today."
    return f"Saved{' as ' + place if place else ''}. Today the sun rises at {rise.strftime('%H:%M')} and sets at {sets.strftime('%H:%M')}."


LATE_MINUTES = 10        # a time missed by more than this (the PC was off or asleep) is skipped, not run late


def due_schedules(now=None):
    """The schedules whose time has come: marks them done for today and returns them."""
    now = now or datetime.datetime.now()
    today = now.date()
    due = []
    with file_lock:
        data = load()
        changed = False
        for s in list(data["schedules"]):
            if s.get("last") == today.isoformat():
                continue
            try:
                at = fire_time(s, today, data)
            except HomeError:
                continue
            if not at or at > now:
                continue
            s["last"] = today.isoformat()
            changed = True
            if (now - at).total_seconds() <= LATE_MINUTES * 60:
                due.append(dict(s))
            else:
                log(f"schedule {s.get('id')} skipped: its time ({at.strftime('%H:%M')}) went by while this PC was off")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(s.get("days"))):
                data["schedules"].remove(s)             # a one-time schedule is finished
        if changed:
            save(data)
    return due


def run_schedule(s, ask=None):
    """Run a schedule's steps now. `ask` is how to hand a sentence to Claude (the ears pass it in)."""
    out = []
    for step in s.get("steps", []):
        try:
            out.append(control(step.get("target", ""), step.get("action", ""), step.get("value")))
        except HomeError as e:
            out.append(f"FAILED - {e}")
    for line in out:
        log(f"schedule {s.get('id')}: {line}")
    if s.get("ask") and ask:
        ask(s["ask"])
    return "\n".join(out)


def run_scheduler(ask=None, stop=None):
    """Runs for ever (in the ears): every 20 seconds, does what's due."""
    while not (stop and stop.is_set()):
        try:
            for s in due_schedules():
                threading.Thread(target=run_schedule, args=(s, ask), daemon=True).start()
        except Exception as e:                # the schedule must keep going whatever one entry does
            log(f"scheduler: {e!r}")
        time.sleep(20)


# ---- the tools Claude sees (MCP) ----------------------------------------------------------------------------------
def _tool(name, description, props=None, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(required)}}


S, N = {"type": "string"}, {"type": "number"}
TOOLS = [
    _tool("list_devices", "Every saved device with its room, what it can do, and how it is right now (on/off, brightness, "
          "colour). Call this first when you aren't sure of a device's or room's exact name."),
    _tool("control", "Do something to a device, to every device in a room, or to \"all\". Lights and plugs take the actions "
          "on, off, toggle, brightness (value 0-100), color (value: a colour name or #rrggbb), temperature (value in kelvin, "
          "2700 warm - 6500 cool) and status. A command device takes the actions list_devices shows for it. The answer is "
          "read back from the device: it says what really happened, including FAILED.",
          {"target": {"description": "A device name, a room name, \"all\", or a list of names",
                      "anyOf": [S, {"type": "array", "items": S}]},
           "action": S, "value": {"description": "For brightness, color, temperature, or a command action that needs one",
                                  "anyOf": [S, N]}}, ["target", "action"]),
    _tool("scan", "Look on the home network for lights, plugs and bridges that aren't saved yet. Changes nothing. Takes "
          "about five seconds."),
    _tool("add_device", "Save a device that scan found (or whose address the person gives), under a name the person will "
          "say. Kinds: kasa, wiz, shelly, tasmota, wled, govee, yeelight, lifx. Hue lights are added with hue_pair instead.",
          {"name": S, "kind": S, "ip": S, "room": S, "channel": {"type": "integer", "description": "For a device with several "
           "switches: which one (Shelly counts from 0, Tasmota from 1)"}, "child": {"type": "string", "description": "For "
           "one socket of a Kasa power strip: the child id scan showed"}}, ["name", "kind", "ip"]),
    _tool("hue_pair", "Connect to a Philips Hue bridge and add all its lights. The person must press the round button on "
          "the bridge first (within the last 30 seconds).", {"bridge_ip": S, "room": S}, ["bridge_ip"]),
    _tool("change_device", "Rename a device or put it in a room (room \"\" takes it out of its room).",
          {"name": S, "new_name": S, "room": S}, ["name"]),
    _tool("remove_device", "Forget a saved device.", {"name": S}, ["name"]),
    _tool("add_schedule", "Have something happen by itself at a time. `when` is a clock time (\"23:00\", \"7:30am\") or "
          "\"sunrise\" / \"sunset\", with minutes before or after as \"sunset-20\" / \"sunrise+15\". `days` is \"daily\", "
          "\"weekdays\", \"weekends\", a list like [\"mon\",\"fri\"], or one date \"2026-12-24\" for a one-time. `steps` are "
          "device actions that run directly at that time (instant, and they work without the internet). Use `ask` only "
          "when the moment needs thinking: that sentence is handed to Claude then.",
          {"when": S, "days": {"anyOf": [S, {"type": "array", "items": S}]},
           "steps": {"type": "array", "items": {"type": "object", "properties": {"target": S, "action": S,
                     "value": {"anyOf": [S, N]}}, "required": ["target", "action"]}},
           "ask": S, "note": S}, ["when"]),
    _tool("list_schedules", "Everything that's scheduled, with each one's number."),
    _tool("remove_schedule", "Cancel a schedule by its number.", {"schedule_id": {"type": "integer"}}, ["schedule_id"]),
    _tool("set_location", "Where the house is, for sunrise and sunset times. Give the town's latitude and longitude "
          "(west and south are negative).", {"lat": N, "lon": N, "place": S}, ["lat", "lon"]),
]


def call_tool(name, a):
    if name == "list_devices":
        return list_devices()
    if name == "control":
        return control(a["target"], a["action"], a.get("value"))
    if name == "scan":
        return scan()
    if name == "add_device":
        return add_device(a["name"], a["kind"], a.get("ip", ""), a.get("room", ""), channel=a.get("channel"),
                          child=a.get("child", ""))
    if name == "hue_pair":
        return hue_pair(a["bridge_ip"], a.get("room"))
    if name == "change_device":
        return change_device(a["name"], a.get("new_name", ""), a.get("room"))
    if name == "remove_device":
        return remove_device(a["name"])
    if name == "add_schedule":
        return add_schedule(a["when"], a.get("steps"), a.get("days", "daily"), a.get("ask", ""), a.get("note", ""))
    if name == "list_schedules":
        return list_schedules()
    if name == "remove_schedule":
        return remove_schedule(a["schedule_id"])
    if name == "set_location":
        return set_location(a["lat"], a["lon"], a.get("place", ""))
    raise HomeError(f"There is no tool called {name}.")


def mcp_server():
    """Model Context Protocol over stdin/stdout: one JSON message per line."""
    stdin = open(sys.stdin.fileno(), encoding="utf-8", newline="\n", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False)

    def send(obj):
        stdout.write(json.dumps(obj) + "\n")
        stdout.flush()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method", "")
        if mid is None:
            continue                               # a notification: nothing to answer
        if method == "initialize":
            version = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": version, "capabilities": {"tools": {}},
                                                          "serverInfo": {"name": "poplocker-home", "version": "1.0"}}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                text, bad = call_tool(p.get("name", ""), p.get("arguments") or {}), False
            except HomeError as e:
                text, bad = str(e), True
            except Exception as e:
                text, bad = f"The tool broke: {type(e).__name__}: {e}", True
                log(f"tool {p.get('name')} broke: {e!r}")
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": bad}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}})


# ---- by hand ------------------------------------------------------------------------------------------------------
def main(argv):
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(__doc__)
        print("Commands: list, scan, schedules, <action> <name> [value]  (actions: " + ", ".join(LIGHT_ACTIONS) + ",\n"
              "or a command device's own), add <name> <kind> <ip> [room], remove <name>, hue_pair <bridge ip>, --mcp")
        return 0
    cmd = argv[0].lower()
    try:
        if cmd == "--mcp":
            mcp_server()
        elif cmd == "list":
            print(list_devices())
        elif cmd == "scan":
            print(scan())
        elif cmd == "schedules":
            print(list_schedules())
        elif cmd == "add":
            print(add_device(argv[1], argv[2], argv[3], argv[4] if len(argv) > 4 else ""))
        elif cmd == "remove":
            print(remove_device(argv[1]))
        elif cmd == "hue_pair":
            print(hue_pair(argv[1]))
        elif len(argv) >= 2:
            out = control(argv[1], cmd, argv[2] if len(argv) > 2 else None)
            print(out)
            return 1 if "FAILED" in out else 0
        else:
            print("Which device? Try: python poplocker_home.py list")
            return 2
    except HomeError as e:
        print(e)
        return 1
    except IndexError:
        print("Something's missing from that command. Try: python poplocker_home.py help")
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv[1:]))
