#!/usr/bin/env python3
"""
FlightWall - "Where's Dada?" display for a 64x64 HUB75 LED panel.

The screen is picked automatically, in this order:
  FLYING  a flight from the Flighty calendar is in progress: plane, route, progress bar
  LANDED  a flight into Denver landed in the last `landed_minutes`: "Dada landed 12 min ago"
  LEAVING any flight departs within `leaving_soon_hours` (from anywhere, home or away)
  HOME    the phone is in Denver: "Dada is home!"
  AWAY    flag + city, local time and weather there, countdown to the next flight home

Usage:
  sudo python3 wall.py           normal mode (reads Home Assistant)
  sudo python3 wall.py --demo    cycle through every screen with made-up data
  python3 wall.py --check        print what it reads from Home Assistant (no panel)
  python3 wall.py --png DIR      render the demo screens to PNG files (no panel)
"""
import argparse
import base64
import csv
import html
import hmac
import io
import colorsys
import json
import math
import os
import re
import secrets
import socket
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from PIL import BdfFontFile, Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache")
W = H = 64

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
DEFAULTS = {
    "ha_url": "http://homeassistant.local:8123",
    "ha_token": "",
    "person_entity": "person.maxwell",
    "flight_calendar": "calendar.flighty",
    "all_events_are_flights": False,
    "home": {"lat": 39.7392, "lon": -104.9903, "radius_km": 40,
             "airports": ["DEN"], "city_word": "denver", "tz": "America/Denver"},
    "dad_name": "DADA",
    "landed_minutes": 90,
    "lookahead_days": 90,
    "offline_after_s": 90,
    "leaving_soon_hours": 4,
    "airport_names": {},
    "temp_unit": "fahrenheit",
    "brightness": 60,
    "night_brightness": 15,
    "night_start": "20:30",
    "night_end": "06:30",
    "clock_24h": False,
    "rotate": 0,
    "location_every_s": 60,
    "calendar_every_s": 300,
    "weather_every_s": 600,
    "gpio_slowdown": 4,
    "web_password": "",
    "web_port": 8080,
    # temporary visitor override (settings page): times are home time, "YYYY-MM-DDTHH:MM"
    "visitor": {"enabled": False, "name": "", "location": "", "flight": "",
                "from": "", "to": "", "depart": "", "arrive": ""},
    "font_dir": "/home/mk/rpi-rgb-led-matrix/fonts",
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        with open(path) as fp:
            user = json.load(fp)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


CFG = load_config()
HOME_TZ = ZoneInfo(CFG["home"]["tz"])


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------------------
# Fonts (hzeller BDF fonts, converted once to PIL bitmap fonts)
# ----------------------------------------------------------------------------
class Font:
    def __init__(self, name):
        bdf = None
        for d in (CFG["font_dir"], os.path.join(HERE, "fonts"),
                  os.path.expanduser("~/rpi-rgb-led-matrix/fonts")):
            p = os.path.join(d, name + ".bdf")
            if os.path.exists(p):
                bdf = p
                break
        if not bdf:
            sys.exit(f"Font {name}.bdf not found - set font_dir in config.json")
        out = os.path.join(CACHE, "fonts", name)
        if not os.path.exists(out + ".pil"):
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(bdf, "rb") as fp:
                BdfFontFile.BdfFontFile(fp).save(out)
        self.pil = ImageFont.load(out + ".pil")
        # where the baseline sits relative to PIL's draw origin
        probe = Image.new("L", (40, 40))
        ImageDraw.Draw(probe).text((0, 0), "H", font=self.pil, fill=255)
        self.ascent = probe.getbbox()[3]

    def width(self, s):
        return int(self.pil.getlength(s))


FONTS = {}


def font(name):
    if name not in FONTS:
        FONTS[name] = Font(name)
    return FONTS[name]


# ----------------------------------------------------------------------------
# Drawing helpers
# ----------------------------------------------------------------------------
WHITE = (255, 255, 255)
GREY = (130, 130, 140)
DIM = (35, 35, 45)
CYAN = (0, 200, 255)
YELLOW = (255, 200, 0)
ORANGE = (255, 120, 0)
GREEN = (40, 230, 90)
PINK = (255, 60, 140)
RED = (235, 30, 40)
BLUE = (60, 130, 255)


def ascii_only(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if ord(c) < 128 and not unicodedata.combining(c))


def text(d, x, base, s, f, color):
    """Draw text with its baseline at `base`. Returns x after the text."""
    d.text((x, base - f.ascent), s, font=f.pil, fill=color)
    return x + f.width(s)


def ctext(d, base, s, f, color, x0=0, x1=W):
    x = x0 + (x1 - x0 - f.width(s)) // 2
    return text(d, x, base, s, f, color)


def rainbow(d, x, base, s, f, t, speed=0.25, sat=0.85):
    for i, ch in enumerate(s):
        r, g, b = colorsys.hsv_to_rgb((t * speed + i * 0.08) % 1.0, sat, 1.0)
        x = text(d, x, base, ch, f, (int(r * 255), int(g * 255), int(b * 255)))
    return x


def fit_font(s, width, names=("5x7", "4x6", "tom-thumb")):
    for n in names:
        if font(n).width(s) <= width:
            return font(n)
    return font(names[-1])


def degree(d, x, y, color):
    """Tiny 3x3 degree ring with its top-left at (x, y)."""
    for dx, dy in ((1, 0), (0, 1), (2, 1), (1, 2)):
        d.point((x + dx, y + dy), fill=color)


def sprite(img, x, y, rows, palette):
    for j, row in enumerate(rows):
        for i, c in enumerate(row):
            if c != "." and 0 <= x + i < W and 0 <= y + j < H:
                img.putpixel((x + i, y + j), palette[c])


PLANE = [
    "....X......",
    "....XX.....",
    "X...XXX....",
    "XX..XXXX...",
    "XXXXXXXXXXX",
    "XX..XXXX...",
    "X...XXX....",
    "....XX.....",
    "....X......",
]
PLANE_SMALL = [
    "..X....",
    "X.XX...",
    "XXXXXXX",
    "X.XX...",
    "..X....",
]
WIFI = [
    "..XXXXXXX..",
    ".X.......X.",
    "X..XXXXX..X",
    "..X.....X..",
    "....XXX....",
    "...X...X...",
    ".....X.....",
    ".....X.....",
]
HEART = [
    ".XX.XX.",
    "XXXXXXX",
    "XXXXXXX",
    ".XXXXX.",
    "..XXX..",
    "...X...",
]
HOUSE = [
    "......R......",
    ".....RRR.....",
    "....RRRRR....",
    "...RRRRRRR...",
    "..RRRRRRRRR..",
    ".RRRRRRRRRRR.",
    "..WWWWWWWWW..",
    "..WYYWWWDDW..",
    "..WYYWWWDDW..",
    "..WWWWWWDDW..",
]


# ---- weather icons (12x12, drawn at x, y) ----------------------------------
def _cloud(d, x, y, col, shade):
    d.ellipse((x + 0, y + 4, x + 5, y + 9), fill=col)
    d.ellipse((x + 3, y + 1, x + 9, y + 7), fill=col)
    d.ellipse((x + 6, y + 4, x + 11, y + 9), fill=col)
    d.rectangle((x + 2, y + 6, x + 9, y + 9), fill=col)
    d.line((x + 2, y + 9, x + 9, y + 9), fill=shade)


def _sun(d, x, y, r=3):
    cx, cy = x + 5, y + 5
    for k in range(8):
        a = k * math.pi / 4
        for dist in (r + 2, r + 3):
            d.point((round(cx + dist * math.cos(a)), round(cy + dist * math.sin(a))), fill=YELLOW)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ORANGE)
    d.ellipse((cx - r + 1, cy - r + 1, cx + r - 1, cy + r - 1), fill=YELLOW)


def _moon(img, x, y, size=10):
    m = Image.new("L", (size + 2, size + 2))
    md = ImageDraw.Draw(m)
    md.ellipse((0, 0, size, size), fill=255)
    md.ellipse((size * 0.35, -size * 0.2, size * 1.25, size * 0.7), fill=0)
    img.paste((255, 235, 150), (x, y), m)


def weather_icon(img, d, x, y, code, is_day):
    kind = weather_kind(code)
    if kind == "clear":
        _sun(d, x, y) if is_day else _moon(img, x + 1, y + 1)
    elif kind == "partly":
        if is_day:
            _sun(d, x - 1, y - 1, r=2)
        else:
            _moon(img, x, y, size=7)
        _cloud(d, x + 1, y + 3, (225, 225, 235), GREY)
    elif kind == "cloudy":
        _cloud(d, x, y + 1, (200, 200, 210), GREY)
    elif kind == "fog":
        for i, (a, b) in enumerate(((1, 9), (0, 11), (2, 10), (0, 8))):
            d.line((x + a, y + 2 + i * 3, x + b, y + 2 + i * 3), fill=GREY)
    elif kind in ("rain", "snow", "storm"):
        dark = kind == "storm"
        _cloud(d, x, y - 2, (110, 110, 125) if dark else (200, 200, 210), (70, 70, 80))
        if kind == "rain":
            for dx in (2, 5, 8):
                d.line((x + dx, y + 9, x + dx - 1, y + 11), fill=BLUE)
        elif kind == "snow":
            for dx, dy in ((2, 9), (5, 11), (8, 9), (10, 11)):
                d.point((x + dx, y + dy), fill=WHITE)
        else:
            d.line((x + 6, y + 6, x + 4, y + 9), fill=YELLOW)
            d.line((x + 4, y + 9, x + 7, y + 9), fill=YELLOW)
            d.line((x + 7, y + 9, x + 5, y + 12), fill=YELLOW)


def weather_kind(code):
    if code is None:
        return "cloudy"
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code >= 95:
        return "storm"
    return "rain"


# ---- flags -------------------------------------------------------------------
FLAG_W, FLAG_H = 16, 11
_flag_mem = {}
_flag_fail = {}


def flag_path(code):
    return os.path.join(CACHE, "flags", f"{code}.png")


def prefetch_flag(code):
    """Download a flag once (runs in the data thread, never while drawing)."""
    if not code or os.path.exists(flag_path(code)):
        return
    if time.time() - _flag_fail.get(code, 0) < 600:
        return
    try:
        r = requests.get(f"https://flagcdn.com/w80/{code}.png", timeout=10)
        r.raise_for_status()
        os.makedirs(os.path.dirname(flag_path(code)), exist_ok=True)
        with open(flag_path(code), "wb") as fp:
            fp.write(r.content)
        log("flag downloaded:", code)
    except Exception as e:
        _flag_fail[code] = time.time()
        log("flag download failed:", code, e)


def draw_flag(img, d, x, y, code):
    if not code:
        return
    if code not in _flag_mem and os.path.exists(flag_path(code)):
        src = Image.open(flag_path(code)).convert("RGB")
        scale = min(FLAG_W / src.width, FLAG_H / src.height)
        size = (max(1, round(src.width * scale)), max(1, round(src.height * scale)))
        _flag_mem[code] = src.resize(size, Image.LANCZOS)
    im = _flag_mem.get(code)
    if im:
        img.paste(im, (x + (FLAG_W - im.width) // 2, y + (FLAG_H - im.height) // 2))
    else:  # no image yet: a little box with the code
        label = code.split("-")[-1].upper()
        d.rectangle((x, y, x + FLAG_W - 1, y + FLAG_H - 1), outline=GREY)
        ctext(d, y + 8, label, font("4x6"), WHITE, x, x + FLAG_W)


# ----------------------------------------------------------------------------
# Data: Home Assistant, reverse geocoding, weather
# ----------------------------------------------------------------------------
def ha_get(path, params=None):
    r = requests.get(CFG["ha_url"].rstrip("/") + path, params=params, timeout=15,
                     headers={"Authorization": f"Bearer {CFG['ha_token']}"})
    r.raise_for_status()
    return r.json()


def km_between(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))


_geo_cache = None


def _geo_load():
    global _geo_cache
    if _geo_cache is None:
        try:
            with open(os.path.join(CACHE, "geocode.json")) as fp:
                _geo_cache = json.load(fp)
        except Exception:
            _geo_cache = {}
    return _geo_cache


def _geo_save():
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, "geocode.json"), "w") as fp:
        json.dump(_geo_cache, fp)


def _place(a):
    """(city, flag) from a Nominatim address block."""
    city = (a.get("city") or a.get("town") or a.get("village") or a.get("municipality")
            or a.get("hamlet") or a.get("county") or a.get("state") or "?")
    cc = a.get("country_code", "").lower()
    iso = a.get("ISO3166-2-lvl4", "").lower()
    return city, (iso if cc == "us" and iso.startswith("us-") else cc)


def forward_geocode(q):
    """(city, flag, lat, lon) for a place name like "Chicago" or "Lyon, France"."""
    _geo_load()
    key = "q:" + q.strip().lower()
    if key in _geo_cache:
        return tuple(_geo_cache[key])
    r = requests.get("https://nominatim.openstreetmap.org/search", timeout=15,
                     params={"q": q, "format": "jsonv2", "addressdetails": 1, "limit": 1,
                             "accept-language": "en"},
                     headers={"User-Agent": "FlightWall-LED/1.0 (personal home display)"})
    r.raise_for_status()
    hits = r.json()
    if not hits:
        raise ValueError(f"place not found: {q}")
    city, flag = _place(hits[0].get("address", {}))
    _geo_cache[key] = [city, flag, float(hits[0]["lat"]), float(hits[0]["lon"])]
    _geo_save()
    return tuple(_geo_cache[key])


def reverse_geocode(lat, lon):
    """(city, flag_code) for a position; US -> state flag (us-co), else country (jp)."""
    _geo_load()
    key = f"{lat:.2f},{lon:.2f}"
    if key in _geo_cache:
        return tuple(_geo_cache[key])
    r = requests.get("https://nominatim.openstreetmap.org/reverse", timeout=15,
                     params={"lat": lat, "lon": lon, "format": "jsonv2",
                             "zoom": 10, "accept-language": "en"},
                     headers={"User-Agent": "FlightWall-LED/1.0 (personal home display)"})
    r.raise_for_status()
    city, flag = _place(r.json().get("address", {}))
    _geo_cache[key] = [city, flag]
    _geo_save()
    return city, flag


def fetch_weather(lat, lon):
    d = requests.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,weather_code,is_day",
        "daily": "temperature_2m_max,temperature_2m_min", "forecast_days": 1,
        "temperature_unit": CFG["temp_unit"], "timezone": "auto"}).json()
    c, day = d["current"], d.get("daily", {})
    hi, lo = (day.get("temperature_2m_max") or [None])[0], (day.get("temperature_2m_min") or [None])[0]
    return {"temp": round(c["temperature_2m"]), "code": c["weather_code"],
            "hi": None if hi is None else round(hi), "lo": None if lo is None else round(lo),
            "is_day": bool(c["is_day"]), "tz": d["timezone"], "at": (lat, lon), "ts": time.time()}


# ---- flights from the calendar ------------------------------------------------
ROUTE = re.compile(r"\b([A-Z]{3})\s*(?:→|->|>|–|—|\bto\b)\s*([A-Z]{3})\b")
FLTNUM = re.compile(r"\b([A-Z]{2}|[A-Z]\d|\d[A-Z])\s?(\d{1,4})\b")


def _clean(s):
    return (s or "").replace("​", "").replace("⁠", "").replace(" ", " ")


def _dt(v):
    if "dateTime" not in v:
        return None  # all-day event, not a flight
    t = datetime.fromisoformat(v["dateTime"].replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=HOME_TZ)


def parse_flight(ev):
    start, end = _dt(ev.get("start", {})), _dt(ev.get("end", {}))
    if not start or not end or end <= start:
        return None
    summary = _clean(ev.get("summary"))
    blob = " ".join(_clean(ev.get(k)) for k in ("summary", "location", "description"))
    m = ROUTE.search(summary) or ROUTE.search(blob)
    if not m and not CFG["all_events_are_flights"] and not re.search(r"\bflight\b", blob, re.I):
        return None
    origin, dest = (m.group(1), m.group(2)) if m else (None, None)
    rest = summary[m.end():] if m and m.string is summary else summary
    n = FLTNUM.search(rest)
    home = (dest in CFG["home"]["airports"]) if dest else \
        bool(re.search(r"\bto\s+" + CFG["home"]["city_word"], blob, re.I))
    return {"start": start, "end": end, "origin": origin, "dest": dest,
            "number": f"{n.group(1)} {n.group(2)}" if n else "",
            "home": home, "summary": summary}


def fetch_flights():
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    events = ha_get(f"/api/calendars/{CFG['flight_calendar']}", {
        "start": (now - timedelta(days=2)).strftime(fmt),
        "end": (now + timedelta(days=CFG["lookahead_days"])).strftime(fmt)})
    flights = [f for f in (parse_flight(e) for e in events) if f]
    return sorted(flights, key=lambda f: f["start"])


# ---- connectivity ------------------------------------------------------------
def check_network():
    """('ok' | 'no_internet' | 'no_network', pi_ip)"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 53))  # no packet is sent; just asks for a route
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        return "no_network", None
    if ip.startswith("127.") or ip == "0.0.0.0":
        return "no_network", None
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            socket.create_connection((host, port), timeout=4).close()
            return "ok", ip
        except OSError:
            pass
    return "no_internet", ip


def classify_ha_error(e):
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = e.response.status_code
        if code in (401, 403):
            return "ha_auth"
        if code == 404:
            return "ha_entity"
    return "ha_down"  # DNS failure, refused, timeout, 5xx


# ---- airport code -> city name (OurAirports, downloaded once) ----------------
AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
AIRPORT_FIXES = {  # airports whose "municipality" isn't the city people say
    "NRT": "Tokyo", "HND": "Tokyo", "KIX": "Osaka", "ICN": "Seoul", "GMP": "Seoul",
    "IAD": "Washington", "DCA": "Washington", "BWI": "Baltimore", "EWR": "Newark",
    "LGA": "New York", "JFK": "New York", "CDG": "Paris", "ORY": "Paris",
    "LHR": "London", "LGW": "London", "STN": "London", "MXP": "Milan", "FCO": "Rome",
    "PEK": "Beijing", "PKX": "Beijing", "PVG": "Shanghai", "DXB": "Dubai",
    "BER": "Berlin", "MUC": "Munich", "YYZ": "Toronto", "YVR": "Vancouver",
    "MEX": "Mexico City", "SJD": "Los Cabos", "CUN": "Cancun", "DFW": "Dallas",
    "IAH": "Houston", "MCO": "Orlando", "FLL": "Fort Lauderdale", "SEA": "Seattle",
    "EGE": "Vail", "ASE": "Aspen", "HDN": "Steamboat", "COS": "Colorado Springs",
}
_airports = None


def _airports_path():
    return os.path.join(CACHE, "airports.json")


def prefetch_airports():
    """Build {IATA: city} from OurAirports once; a few MB, cached forever."""
    if os.path.exists(_airports_path()):
        return True
    try:
        r = requests.get(AIRPORTS_URL, timeout=90)
        r.raise_for_status()
        table = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            code = (row.get("iata_code") or "").strip().upper()
            city = (row.get("municipality") or "").strip()
            if len(code) != 3 or not city:
                continue
            big = row.get("type") in ("large_airport", "medium_airport")
            if code not in table or big:
                table[code] = city.split(",")[-1].strip()  # "Ota, Tokyo" -> "Tokyo"
        os.makedirs(CACHE, exist_ok=True)
        with open(_airports_path(), "w") as fp:
            json.dump(table, fp)
        log(f"airport names downloaded ({len(table)} airports)")
        return True
    except Exception as e:
        log("airport names download failed:", e)
        return False


def airport_city(code):
    global _airports
    if not code:
        return None
    if code in CFG["airport_names"]:
        return CFG["airport_names"][code]
    if code in AIRPORT_FIXES:
        return AIRPORT_FIXES[code]
    if _airports is None and os.path.exists(_airports_path()):
        with open(_airports_path()) as fp:
            _airports = json.load(fp)
    return (_airports or {}).get(code)


# ---- background updater ------------------------------------------------------
STATE = {"visitor": {"q": None, "loc": None, "wx": None}, "loc": None, "wx": None, "home_wx": None, "flights": [],
         "net": {"status": "unknown", "ip": None, "fail_since": None},
         "ha": {"status": "unknown", "fail_since": None, "error": ""},
         "cal_error": ""}
LOCK = threading.Lock()
REFRESH = threading.Event()  # set by the settings page: re-fetch everything now


def _set_status(key, status, **extra):
    with LOCK:
        cur = STATE[key]
        bad = status not in ("ok", "unknown")
        if bad and cur["status"] != status:
            log(f"{key} status: {status}", extra.get("error", ""))
        elif not bad and cur["status"] not in ("ok", "unknown"):
            log(f"{key} status: back to ok")
        cur["fail_since"] = (cur["fail_since"] or time.time()) if bad else None
        cur["status"] = status
        cur.update(extra)


def update_location():
    st = ha_get(f"/api/states/{CFG['person_entity']}")
    a = st.get("attributes", {})
    lat, lon = a.get("latitude"), a.get("longitude")
    hm = CFG["home"]
    if lat is None or lon is None:
        loc = {"state": st["state"], "lat": hm["lat"], "lon": hm["lon"], "city": "DENVER",
               "flag": "us-co", "at_home": st["state"] == "home"}
    else:
        city, flag = reverse_geocode(lat, lon)
        near = km_between(lat, lon, hm["lat"], hm["lon"]) <= hm["radius_km"]
        loc = {"state": st["state"], "lat": lat, "lon": lon, "city": city, "flag": flag,
               "at_home": near or st["state"] == "home"}
    prefetch_flag(loc["flag"])
    return loc


def visitor_plan(now=None):
    """The visitor's flight from the settings, or None if it's off, incomplete, or over
    (landed more than `landed_minutes` ago)."""
    v = CFG["visitor"]
    if not v.get("enabled"):
        return None
    try:
        start = datetime.fromisoformat(v["depart"]).replace(tzinfo=HOME_TZ)
        end = datetime.fromisoformat(v["arrive"]).replace(tzinfo=HOME_TZ)
    except (KeyError, TypeError, ValueError):
        return None
    if end <= start or (now and now >= end + timedelta(minutes=CFG["landed_minutes"])):
        return None
    origin, dest = (v.get("from") or "").upper() or None, (v.get("to") or "").upper() or None
    return {"start": start, "end": end, "origin": origin, "dest": dest,
            "number": (v.get("flight") or "").upper(), "home": dest in CFG["home"]["airports"],
            "summary": "", "name": ascii_only(v.get("name") or "").upper() or "GUEST", "visitor": True}


def visitor_query():
    """Where the visitor is before the flight: the setting, else the departure airport's city."""
    v = CFG["visitor"]
    origin = (v.get("from") or "").upper()
    return (v.get("location") or "").strip() or airport_city(origin) or origin


def update_visitor():
    """Geocode the visitor's place (once per name) and fetch its weather."""
    q = visitor_query()
    if not q:
        return
    with LOCK:
        cur = dict(STATE["visitor"])
    loc = cur["loc"] if cur["q"] == q else None
    if loc is None:
        city, flag, lat, lon = forward_geocode(q)
        loc = {"city": city, "flag": flag, "lat": lat, "lon": lon, "at_home": False}
        prefetch_flag(flag)
        log("visitor location:", q, "->", city, flag)
    wx = fetch_weather(loc["lat"], loc["lon"])
    with LOCK:
        STATE["visitor"] = {"q": q, "loc": loc, "wx": wx}


def visitor_snapshot(now):
    """What render() needs for the visitor, or None."""
    f = visitor_plan(now)
    if not f:
        return None
    with LOCK:
        v = dict(STATE["visitor"])
    fresh = v["q"] == visitor_query()
    return {"flight": f, "loc": v["loc"] if fresh else None, "wx": v["wx"] if fresh else None}


def data_loop():
    due = {"net": 0, "loc": 0, "cal": 0, "wx": 0, "hwx": 0, "apt": 0, "vis": 0}
    while True:
        now = time.time()
        if REFRESH.is_set():
            REFRESH.clear()
            due.update(net=0, loc=0, cal=0, wx=0, hwx=0, vis=0)
        if now >= due["apt"]:
            due["apt"] = float("inf") if prefetch_airports() else now + 3600
        if now >= due["net"]:
            st, ip = check_network()
            _set_status("net", st, ip=ip)
            due["net"] = now + 30
        if now >= due["loc"]:
            try:
                loc = update_location()
                _set_status("ha", "ok", error="")
                with LOCK:
                    old = STATE["loc"]
                    STATE["loc"] = loc
                if old is None or km_between(old["lat"], old["lon"], loc["lat"], loc["lon"]) > 10:
                    due["wx"] = 0  # moved: refresh weather now
                    log("location:", loc["city"], loc["flag"], "home" if loc["at_home"] else "away")
                due["loc"] = now + CFG["location_every_s"]
            except Exception as e:
                _set_status("ha", classify_ha_error(e), error=str(e)[:120])
                due["loc"] = now + 30
        if now >= due["cal"]:
            try:
                fl = fetch_flights()
                with LOCK:
                    STATE["flights"] = fl
                    STATE["cal_error"] = ""
                due["cal"] = now + CFG["calendar_every_s"]
            except Exception as e:
                log("calendar error:", e)
                with LOCK:
                    STATE["cal_error"] = str(e)[:120]
                due["cal"] = now + 60
        if now >= due["wx"]:
            with LOCK:
                loc = STATE["loc"]
            lat, lon = (loc["lat"], loc["lon"]) if loc else (CFG["home"]["lat"], CFG["home"]["lon"])
            try:
                wx = fetch_weather(lat, lon)
                with LOCK:
                    STATE["wx"] = wx
                due["wx"] = now + CFG["weather_every_s"]
            except Exception as e:
                log("weather error:", e)
                due["wx"] = now + 60
        if visitor_plan(datetime.now(timezone.utc)):
            with LOCK:
                moved = STATE["visitor"]["q"] != visitor_query()
            if now >= due["vis"] or moved:
                try:
                    update_visitor()
                    due["vis"] = now + CFG["weather_every_s"]
                except Exception as e:
                    log("visitor update error:", e)
                    due["vis"] = now + 120
        if now >= due["hwx"]:  # Denver weather, for the offline screen
            try:
                hw = fetch_weather(CFG["home"]["lat"], CFG["home"]["lon"])
                with LOCK:
                    STATE["home_wx"] = hw
                due["hwx"] = now + 900
            except Exception:
                due["hwx"] = now + 120
        time.sleep(2)


# ----------------------------------------------------------------------------
# Deciding what to show
# ----------------------------------------------------------------------------
def pick_mode(snap, now):
    flights = snap["flights"]
    for f in flights:
        if f["start"] <= now < f["end"]:
            return "flying", f
    recent = [f for f in flights if f["home"] and
              f["end"] <= now < f["end"] + timedelta(minutes=CFG["landed_minutes"])]
    if recent:
        return "landed", recent[-1]
    soon = timedelta(hours=CFG["leaving_soon_hours"])
    for f in flights:  # any booked flight, wherever it leaves from and wherever GPS says he is
        if now < f["start"] <= now + soon:
            return "leaving", f
    v = snap.get("visitor")  # temporary override from the settings page
    if v:
        f = v["flight"]
        if f["start"] <= now < f["end"]:
            return "flying", f
        if f["end"] <= now < f["end"] + timedelta(minutes=CFG["landed_minutes"]):
            return "landed", f
        if now < f["start"]:
            return "visitor", v
    if snap.get("offline"):
        return "offline", snap["offline"]
    if snap["loc"] and snap["loc"]["at_home"]:
        return "home", None
    return "away", None


def offline_reason(net, ha, cal_error, now_ts):
    """(line1, line2, hint) for the offline screen, or None if all is well enough."""
    grace = CFG["offline_after_s"]
    ip = net.get("ip") or ""
    if net["status"] == "no_network":
        if now_ts - (net["fail_since"] or now_ts) >= grace:
            return ("NO NETWORK", "CONNECTIVITY", "CHECK WIFI")
        return None
    if ha["status"] in ("ok", "unknown"):
        return None
    if now_ts - (ha["fail_since"] or now_ts) < grace:
        return None
    if ha["status"] == "ha_auth":
        return ("HA TOKEN", "REJECTED", ip)
    if ha["status"] == "ha_entity":
        return ("HA PERSON", "NOT FOUND", ip)
    if net["status"] == "no_internet":
        return ("NO INTERNET", "CONNECTIVITY", "HA DOWN TOO")
    return ("NO HA", "CONNECTIVITY", ip)


def degraded(net, ha, cal_error):
    """Small problems that don't need the offline screen (shown as a corner dot)."""
    return net["status"] == "no_internet" or ha["status"] not in ("ok", "unknown") or bool(cal_error)


def next_home_flight(snap, now):
    for f in snap["flights"]:
        if f["home"] and f["start"] > now:
            return f
    return None


def clock(dt):
    """("5:30", "PM"), or ("17:30", "") with clock_24h."""
    if CFG["clock_24h"]:
        return dt.strftime("%H:%M"), ""
    return dt.strftime("%I:%M").lstrip("0"), dt.strftime("%p")


def clock_width(hm, big, ap, small, gap):
    return big.width(hm) + (gap + small.width(ap) if ap else 0)


# ----------------------------------------------------------------------------
# Screens
# ----------------------------------------------------------------------------
def fit_text(img, d, x0, x1, base, parts, t, center=False):
    """Draw [(text, color), ...] between x0 and x1: 5x7, else 4x6, else scrolling 5x7."""
    s = "".join(p for p, _ in parts)
    avail = x1 - x0
    for name in ("5x7", "4x6"):
        f = font(name)
        if f.width(s) <= avail:
            x = x0 + ((avail - f.width(s)) // 2 if center else 0)
            for p, c in parts:
                x = text(d, x, base, p, f, c)
            return
    f = font("5x7")  # too long even in 4x6: marquee
    strip_w = f.width(s) + 12
    top = base - f.ascent - 2
    strip = Image.new("RGB", (strip_w * 2, f.ascent + 4))
    sd = ImageDraw.Draw(strip)
    for start in (0, strip_w):
        x = start
        for p, c in parts:
            x = text(sd, x, f.ascent + 2, p, f, c)
    off = int(t * 10) % strip_w
    img.paste(strip.crop((off, 0, off + avail, strip.height)), (x0, top))


def draw_header(img, d, loc, t):
    """Flag at left, city name to the right (scrolls if it's long)."""
    city = ascii_only(loc["city"]).upper() if loc else "..."
    fit_text(img, d, 20, W - 1, 9, [(city, CYAN)], t)
    draw_flag(img, d, 1, 1, loc.get("flag") if loc else None)


def dest_name(f):
    city = airport_city(f["dest"])
    return ascii_only(city).upper() if city else (f["dest"] or "???")


def draw_route(img, d, f, plane_color):
    codes = font("7x13B")
    o, de = f["origin"] or "???", f["dest"] or "???"
    text(d, 2, 21, o, codes, WHITE)
    text(d, W - 2 - codes.width(de), 21, de, codes, GREEN if f["home"] else WHITE)
    sprite(img, (W - 11) // 2, 11, PLANE, {"X": plane_color})


def draw_temp(img, d, x, base, temp, f):
    x = text(d, x, base, str(temp), f, WHITE)
    degree(d, x + 1, base - f.ascent + (1 if f.ascent > 8 else 0), WHITE)
    return x + 4


def draw_countdown(img, d, f, now, t):
    """Bottom block (rows 48-63): days until Dada (or the visitor) arrives, or arrival time."""
    arr = f["end"].astimezone(HOME_TZ)
    days = (arr.date() - now.astimezone(HOME_TZ).date()).days
    name = f.get("name") or CFG["dad_name"]
    if days >= 2:
        big = font("9x15B")
        num = str(days)
        nf = font("5x7") if big.width(num) + 3 + font("5x7").width(name + "!") <= W - 2 else font("4x6")
        right = [("DAYS TIL", font("5x7")), (name + "!", nf)]
        rw = max(ff.width(s) for s, ff in right)
        x = (W - (big.width(num) + 3 + rw)) // 2
        x = text(d, x, 62, num, big, YELLOW) + 3
        text(d, x, 55, right[0][0], right[0][1], GREY)
        rainbow(d, x, 63, right[1][0], right[1][1], t)
    else:
        word = ("HERE" if f["home"] else "LANDS") if f.get("visitor") else "HOME"
        label = f"{word} TODAY!" if days <= 0 else f"{word} TOMORROW"
        ctext(d, 53, label, fit_font(label, 62, ("5x7", "4x6")), YELLOW if days <= 0 else CYAN)
        hm, ap = clock(arr)
        big = font("7x13B")
        tiny = font("4x6")
        x = (W - clock_width(hm, big, ap, tiny, 2)) // 2
        x = text(d, x, 64, hm, big, WHITE) + 2
        text(d, x, 64, ap, tiny, GREY)


def screen_away(img, d, snap, now, t, nxt=None):
    loc, wx = snap["loc"], snap["wx"]
    tz = ZoneInfo(wx["tz"]) if wx else HOME_TZ
    local = now.astimezone(tz)
    draw_header(img, d, loc, t)

    # local time, big
    hm, ap = clock(local)
    big, tiny = font("9x18B"), font("4x6")
    x = (W - clock_width(hm, big, ap, tiny, 1)) // 2
    x = text(d, x, 27, hm, big, WHITE) + 1
    text(d, x, 20, ap, tiny, GREY)

    # weather: icon + temperature
    if wx:
        f = font("7x13")
        tw = f.width(str(wx["temp"])) + 4
        x = (W - (12 + 3 + tw)) // 2
        weather_icon(img, d, x, 31, wx["code"], wx["is_day"])
        draw_temp(img, d, x + 15, 42, wx["temp"], f)

    d.line((6, 45, W - 7, 45), fill=DIM)
    nxt = nxt or next_home_flight(snap, now)
    if nxt:
        draw_countdown(img, d, nxt, now, t)
    else:
        ctext(d, 58, local.strftime("%a %b %d").upper().replace(" 0", " "), font("5x7"), GREY)


def screen_visitor(img, d, snap, now, t, v):
    """Temporary override: the visitor's place, time and weather, counting down to their flight."""
    f = v["flight"]
    screen_away(img, d, {"loc": v["loc"], "wx": v["wx"]}, now, t, nxt=f)


def screen_home(img, d, snap, now, t):
    local = now.astimezone(HOME_TZ)
    big = font("9x18B")
    name = CFG["dad_name"]
    x = ctext(d, 15, name, big, WHITE)
    pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(t * 4))
    col = tuple(int(c * pulse) for c in PINK)
    xl = (W - big.width(name)) // 2
    sprite(img, xl - 10, 5, HEART, {"X": col})
    sprite(img, x + 3, 5, HEART, {"X": col})
    msg = "IS HOME!"
    f = font("7x13B")
    rainbow(d, (W - f.width(msg)) // 2, 29, msg, f, t)

    # everything sits a little high so the bottom rows stay empty (frame edge)
    d.line((6, 33, W - 7, 33), fill=DIM)
    wx = snap["wx"]
    hilo = wx and wx.get("hi") is not None and wx.get("lo") is not None
    base = 46 if hilo else 50  # clock row moves up to make room for HI / LO
    hm, _ = clock(local)
    ww = 12 + 2 + font("5x7").width(str(wx["temp"])) + 4 if wx else 0
    for name in ("7x13", "6x10", "5x7"):  # biggest clock that fits next to the weather
        f = font(name)
        total = f.width(hm) + (5 + ww if wx else 0)
        if total <= W - 2:
            break
    x = text(d, (W - total) // 2, base, hm, f, WHITE)
    if wx:
        x += 5
        weather_icon(img, d, x, base - 11, wx["code"], wx["is_day"])
        draw_temp(img, d, x + 14, base, wx["temp"], font("5x7"))
    if hilo:
        draw_hilo(img, d, 56, wx)


def draw_hilo(img, d, base, wx):
    """'HI 78°  LO 52°' centred on one line: small labels, 5x7 numbers."""
    lab, num = font("4x6"), font("5x7")
    for deg in (True, False):  # drop the degree rings if it's too wide (e.g. -12 / -25)
        items = [("HI", lab, ORANGE, 2), (str(wx["hi"]), num, WHITE, 6),
                 ("LO", lab, CYAN, 2), (str(wx["lo"]), num, WHITE, 0)]
        w = sum(f.width(t) + gap for t, f, _, gap in items) + (8 if deg else 0)
        if w <= W - 2:
            break
    x = (W - w) // 2
    for i, (t, f, c, gap) in enumerate(items):
        x = text(d, x, base, t, f, c)
        if i in (1, 3) and deg:
            degree(d, x + 1, base - num.ascent, WHITE)
            x += 4
        x += gap


def screen_flying(img, d, snap, now, t, f):
    total = (f["end"] - f["start"]).total_seconds()
    p = max(0.0, min(1.0, (now - f["start"]).total_seconds() / total))
    small, tiny = font("5x7"), font("4x6")
    msg = (f.get("name") or CFG["dad_name"]) + " IS FLYING"
    tf = fit_font(msg, W, ("4x6", "tom-thumb"))
    rainbow(d, (W - tf.width(msg)) // 2, 6, msg, tf, t)

    draw_route(img, d, f, CYAN)

    # progress bar
    y, x0, x1 = 29, 4, W - 5
    for x in range(x0, x1 + 1, 2):
        d.point((x, y), fill=DIM)
    xp = x0 + round((x1 - x0) * p)
    d.line((x0, y, xp, y), fill=CYAN)
    d.ellipse((x0 - 1, y - 1, x0 + 1, y + 1), fill=WHITE)
    d.ellipse((x1 - 1, y - 1, x1 + 1, y + 1), fill=GREEN if f["home"] else WHITE)
    sprite(img, min(xp, x1 - 6) - 3, y - 2, PLANE_SMALL, {"X": YELLOW})

    left = f["end"] - now
    mins = max(0, int(left.total_seconds() // 60))
    ctext(d, 42, f"{mins // 60}H {mins % 60:02d}M LEFT" if mins >= 60 else f"{mins} MIN LEFT",
          small, WHITE)
    d.line((6, 46, W - 7, 46), fill=DIM)
    if f["home"]:
        msg = "ON THE WAY!" if f.get("visitor") else "COMING HOME!"
        rainbow(d, (W - small.width(msg)) // 2, 55, msg, small, t)
        hm, ap = clock(f["end"].astimezone(HOME_TZ))
        ctext(d, 63, f"LANDS {hm} {ap}".strip(), tiny, GREY)
    else:
        fit_text(img, d, 1, W - 1, 55, [("TO ", GREY), (dest_name(f), CYAN)], t, center=True)
        if f["number"]:
            ctext(d, 63, f["number"], tiny, GREY)


def screen_leaving(img, d, snap, now, t, f):
    """A booked flight takes off within `leaving_soon_hours`."""
    msg = (f.get("name") or CFG["dad_name"]) + " FLIES SOON"
    tf = fit_font(msg, W, ("4x6", "tom-thumb"))
    rainbow(d, (W - tf.width(msg)) // 2, 6, msg, tf, t)
    draw_route(img, d, f, GREY)
    if f["home"]:
        msg = "COMING HOME!"
        rainbow(d, (W - font("5x7").width(msg)) // 2, 32, msg, font("5x7"), t)
    else:
        fit_text(img, d, 1, W - 1, 32, [("TO ", GREY), (dest_name(f), CYAN)], t, center=True)
    d.line((6, 36, W - 7, 36), fill=DIM)

    mins = max(0, math.ceil((f["start"] - now).total_seconds() / 60))
    left = f"{mins // 60}H {mins % 60:02d}M" if mins >= 60 else f"{mins} MIN"
    big = font("9x15B") if font("9x15B").width(left) <= W - 2 else font("7x13B")
    ctext(d, 52, left, big, YELLOW)
    hm, ap = clock(f["start"].astimezone(HOME_TZ))
    label = f"TAKEOFF {hm} {ap}".strip()
    ctext(d, 62, label, fit_font(label, W - 2, ("4x6", "tom-thumb")), GREY)


def screen_landed(img, d, snap, now, t, f):
    mins = int((now - f["end"]).total_seconds() // 60)
    # plane touching down on a runway
    for x in range(2, W - 2, 6):
        d.line((x, 15, x + 2, 15), fill=GREY)
    bob = 1 if int(t * 2) % 2 else 0
    sprite(img, 8 + int(t * 6) % 40, 5 + bob, PLANE, {"X": CYAN})
    name = f.get("name") or CFG["dad_name"]
    ctext(d, 33, name, fit_font(name, W - 2, ("9x18B", "7x13B", "5x7")), WHITE)
    msg = "LANDED!"
    rainbow(d, (W - font("7x13B").width(msg)) // 2, 47, msg, font("7x13B"), t)
    ctext(d, 59, "JUST NOW!" if mins < 1 else f"{mins} MIN AGO", font("5x7"), YELLOW)


def screen_offline(img, d, snap, now, t, reason):
    l1, l2, hint = reason
    pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(t * 2.5))
    col = tuple(int(c * pulse) for c in ORANGE)
    # icon with a red slash: wifi for network problems, house for Home Assistant
    if l1.startswith(("NO NETWORK", "NO INTERNET")):
        sprite(img, 1, 2, WIFI, {"X": GREY})
        d.line((1, 11, 12, 0), fill=RED)
    else:
        sprite(img, 0, 1, HOUSE, {"R": (0, 150, 220), "W": GREY, "Y": GREY, "D": GREY})
        d.line((1, 11, 12, 0), fill=RED)
    tiny = font("4x6")
    text(d, 15, 6, l1, tiny, col)
    text(d, 15, 12, l2, tiny, col)

    local = now.astimezone(HOME_TZ)
    hm, ap = clock(local)
    big = font("9x18B")
    x = (W - clock_width(hm, big, ap, tiny, 1)) // 2
    x = text(d, x, 29, hm, big, WHITE) + 1
    text(d, x, 22, ap, tiny, GREY)

    wx = snap.get("home_wx")
    if wx and time.time() - wx.get("ts", time.time()) > 3600:
        wx = None  # too old to trust (network has been down a while)
    if wx:
        f = font("7x13")
        tw = f.width(str(wx["temp"])) + 4
        x = (W - (12 + 3 + tw)) // 2
        weather_icon(img, d, x, 32, wx["code"], wx["is_day"])
        draw_temp(img, d, x + 15, 43, wx["temp"], f)

    d.line((6, 46, W - 7, 46), fill=DIM)
    ctext(d, 55, local.strftime("%a %b %d").upper().replace(" 0", " "), font("5x7"), GREY)
    if hint:
        ctext(d, 63, hint, fit_font(hint, 62, ("4x6", "tom-thumb")), (90, 90, 100))


def render(snap, now, t):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    mode, f = pick_mode(snap, now)
    if mode == "flying":
        screen_flying(img, d, snap, now, t, f)
    elif mode == "landed":
        screen_landed(img, d, snap, now, t, f)
    elif mode == "leaving":
        screen_leaving(img, d, snap, now, t, f)
    elif mode == "offline":
        screen_offline(img, d, snap, now, t, f)
    elif mode == "visitor":
        screen_visitor(img, d, snap, now, t, f)
    elif mode == "home":
        screen_home(img, d, snap, now, t)
    else:
        screen_away(img, d, snap, now, t)
    if mode != "offline" and snap.get("degraded") and int(t * 2) % 2:
        d.rectangle((62, 0, 63, 1), fill=ORANGE)  # blinking dot: something is partly down
    return img


def brightness_for(now):
    local = now.astimezone(HOME_TZ).strftime("%H:%M")
    a, b = CFG["night_start"], CFG["night_end"]
    night = (local >= a or local < b) if a > b else (a <= local < b)
    return CFG["night_brightness"] if night else CFG["brightness"]


# ----------------------------------------------------------------------------
# Demo scenarios (fake data, used by --demo and --png)
# ----------------------------------------------------------------------------
def demo_scenarios():
    now = datetime(2026, 9, 25, 17, 30, tzinfo=HOME_TZ).astimezone(timezone.utc)

    def fl(o, de, dep_h, dur_h, num="UA 1234"):
        s = now + timedelta(hours=dep_h)
        return {"start": s, "end": s + timedelta(hours=dur_h), "origin": o, "dest": de,
                "number": num, "home": de in CFG["home"]["airports"], "summary": ""}

    def loc(city, flag, home=False):
        return {"state": "home" if home else "not_home", "lat": 0, "lon": 0,
                "city": city, "flag": flag, "at_home": home}

    def visitor(name, o, de, dep_h, dur_h, place, flag, wx_, num="WN 383"):
        f = dict(fl(o, de, dep_h, dur_h, num), name=name, visitor=True)
        return {"flight": f, "loc": {"city": place, "flag": flag, "lat": 0, "lon": 0, "at_home": False},
                "wx": wx_}

    def wx(temp, code, day, tz, hi=None, lo=None):
        return {"temp": temp, "code": code, "is_day": day, "tz": tz, "hi": hi, "lo": lo}

    return [
        ("away_tokyo_5days", {"loc": loc("Tokyo", "jp"), "wx": wx(71, 0, False, "Asia/Tokyo"),
                              "flights": [fl("HND", "DEN", 24 * 5 - 3, 11, "UA 138")]}, now),
        ("away_sf_tomorrow", {"loc": loc("San Francisco", "us-ca"),
                              "wx": wx(64, 2, True, "America/Los_Angeles"),
                              "flights": [fl("SFO", "DEN", 22, 2.3, "UA 552")]}, now),
        ("away_chicago_today", {"loc": loc("Chicago", "us-il"), "wx": wx(58, 61, True, "America/Chicago"),
                                "flights": [fl("ORD", "DEN", 4.2, 1.5, "UA 2211")]}, now),
        ("away_london_no_flight", {"loc": loc("London", "gb"), "wx": wx(49, 71, False, "Europe/London"),
                                   "flights": []}, now),
        ("away_long_city_name", {"loc": loc("Rancho Santa Margarita", "us-ca"),
                                 "wx": wx(88, 95, True, "America/Los_Angeles"),
                                 "flights": [fl("SNA", "DEN", 24 * 12, 2.2)]}, now),
        ("flying_home", {"loc": loc("Chicago", "us-il"), "wx": None,
                         "flights": [fl("ORD", "DEN", -1.6, 2.6, "UA 2211")]}, now),
        ("flying_away", {"loc": loc("Denver", "us-co"), "wx": None,
                         "flights": [fl("DEN", "LHR", -2.5, 9.5, "BA 218")]}, now),
        ("landed", {"loc": loc("Denver", "us-co", True), "wx": None,
                    "flights": [fl("ORD", "DEN", -2.8, 2.6, "UA 2211")]}, now),
        ("home", {"loc": loc("Denver", "us-co", True),
                  "wx": wx(72, 1, True, "America/Denver", 78, 49), "flights": []}, now),
        ("home_cold", {"loc": loc("Denver", "us-co", True),
                       "wx": wx(-4, 73, True, "America/Denver", 12, -11), "flights": []}, now),
        ("offline_no_ha", {"loc": None, "wx": None, "flights": [],
                           "home_wx": wx(72, 1, True, "America/Denver"),
                           "offline": ("NO HA", "CONNECTIVITY", "192.168.129.57")}, now),
        ("offline_no_network", {"loc": None, "wx": None, "flights": [],
                                "home_wx": wx(72, 1, True, "America/Denver"),
                                "offline": ("NO NETWORK", "CONNECTIVITY", "CHECK WIFI")}, now),
        ("offline_bad_token", {"loc": None, "wx": None, "flights": [], "home_wx": None,
                               "offline": ("HA TOKEN", "REJECTED", "192.168.129.57")}, now),
        ("degraded_away", {"loc": loc("Chicago", "us-il"), "wx": wx(58, 3, True, "America/Chicago"),
                           "flights": [], "degraded": True}, now),
        ("leaving_soon", {"loc": loc("Denver", "us-co", True), "wx": None,
                          "flights": [fl("DEN", "LHR", 2.75, 9.5, "BA 218")]}, now),
        ("leaving_home_from_mdw", {"loc": loc("Denver", "us-co", True), "wx": None,
                                   "flights": [fl("MDW", "DEN", 1.5, 2.7, "WN 383")]}, now),
        ("leaving_soon_40min", {"loc": loc("Denver", "us-co", True), "wx": None,
                                "flights": [fl("DEN", "SFO", 0.66, 2.4, "UA 552")]}, now),
        ("flying_elsewhere", {"loc": loc("London", "gb"), "wx": None,
                              "flights": [fl("LHR", "CDG", -0.4, 1.3, "AF 1681")]}, now),
        ("visitor_waiting", {"loc": loc("Denver", "us-co", True), "wx": None, "flights": [],
                             "visitor": visitor("GRAMMY", "MDW", "DEN", 24 * 3 + 1, 2.7, "Chicago", "us-il",
                                                wx(55, 61, True, "America/Chicago"))}, now),
        ("visitor_lands_tomorrow", {"loc": loc("Denver", "us-co", True), "wx": None, "flights": [],
                           "visitor": visitor("GRANDPA", "LHR", "DEN", 1, 9.9, "London", "gb",
                                              wx(48, 3, False, "Europe/London"), "BA 219")}, now),
        ("visitor_flying", {"loc": loc("Denver", "us-co", True), "wx": None, "flights": [],
                            "visitor": visitor("GRAMMY", "MDW", "DEN", -1.2, 2.7, "Chicago", "us-il", None)}, now),
        ("visitor_landed", {"loc": loc("Denver", "us-co", True), "wx": None, "flights": [],
                            "visitor": visitor("GRAMMY", "MDW", "DEN", -3, 2.7, "Chicago", "us-il", None)}, now),
        ("visitor_but_dada_flying", {"loc": loc("Chicago", "us-il"), "wx": None,
                                     "flights": [fl("ORD", "DEN", -1.0, 2.6, "UA 2211")],
                                     "visitor": visitor("GRAMMY", "MDW", "DEN", 24, 2.7, "Chicago", "us-il",
                                                        None)}, now),
        ("flying_long_name", {"loc": loc("Chicago", "us-il"), "wx": None,
                              "flights": [fl("ORD", "SLC", -1.0, 3.2, "DL 1402")]}, now),
    ]


def led_preview(img, scale=8):
    """Upscale with round 'LED' dots so the PNG looks like the real panel."""
    out = Image.new("RGB", (W * scale, H * scale), (8, 8, 10))
    d = ImageDraw.Draw(out)
    for y in range(H):
        for x in range(W):
            c = img.getpixel((x, y))
            if c == (0, 0, 0):
                c = (18, 18, 22)
            d.ellipse((x * scale + 1, y * scale + 1, x * scale + scale - 2, y * scale + scale - 2), fill=c)
    return out


# ----------------------------------------------------------------------------
# Panel output
# ----------------------------------------------------------------------------
class Panel:
    def __init__(self):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions
        o = RGBMatrixOptions()
        o.rows, o.cols = 64, 64
        o.hardware_mapping = "regular"
        o.gpio_slowdown = CFG["gpio_slowdown"]
        o.brightness = CFG["brightness"]
        o.drop_privileges = False
        self.m = RGBMatrix(options=o)
        self.c = self.m.CreateFrameCanvas()

    def show(self, img, brightness):
        if brightness <= 0:  # the library ignores 0, so blank the frame instead
            img, brightness = Image.new("RGB", (W, H)), 1
        self.m.brightness = brightness
        if CFG["rotate"]:
            img = img.rotate(-CFG["rotate"])  # clockwise, for a panel mounted sideways/upside down
        self.c.SetImage(img)
        self.c = self.m.SwapOnVSync(self.c)


# ----------------------------------------------------------------------------
# Settings web page (http://<pi-ip>:8080, only if web_password is set)
# ----------------------------------------------------------------------------
LAST = {"img": None, "snap": None}  # latest frame, for the page's live preview

# (key, label, kind, extra, help); dotted keys live inside "home"
SETTINGS = [
    ("Home Assistant", [
        ("ha_url", "HA URL", "text", None, "e.g. http://192.168.1.3:8123"),
        ("ha_token", "HA token", "secret", None, "long-lived access token; leave blank to keep"),
        ("person_entity", "Person entity", "text", None, "e.g. person.maxwell"),
        ("flight_calendar", "Flight calendar", "text", None, "e.g. calendar.flighty"),
        ("all_events_are_flights", "Every calendar event is a flight", "bool", None, ""),
    ]),
    ("Screens", [
        ("dad_name", "Name on screen", "text", None, ""),
        ("leaving_soon_hours", '"Flies soon" starts (hours before takeoff)', "float", (0, 168), "any flight, from anywhere"),
        ("landed_minutes", '"Landed" stays up (minutes after landing)', "int", (0, 1440), ""),
        ("lookahead_days", "Countdown looks ahead (days)", "int", (1, 365), ""),
        ("offline_after_s", "Offline screen after (seconds of failure)", "int", (10, 3600), ""),
        ("clock_24h", "24-hour clock", "bool", None, ""),
        ("temp_unit", "Temperature", "choice", [("fahrenheit", "°F"), ("celsius", "°C")], ""),
        ("airport_names", "Airport name overrides", "dict", None, "one per line, e.g. XNA=Bentonville"),
    ]),
    ("Home", [
        ("home.lat", "Latitude", "float", (-90, 90), ""),
        ("home.lon", "Longitude", "float", (-180, 180), ""),
        ("home.radius_km", "Counts as home within (km)", "float", (0.1, 500), ""),
        ("home.airports", "Home airports", "list", None, "comma separated, e.g. DEN"),
        ("home.city_word", "Home city word", "text", None, 'matches "to denver" in events without codes'),
        ("home.tz", "Time zone", "tz", None, "e.g. America/Denver"),
    ]),
    ("Display", [
        ("brightness", "Brightness", "int", (1, 100), ""),
        ("night_brightness", "Night brightness", "int", (0, 100), "0 turns the panel off at night"),
        ("night_start", "Night starts", "time", None, "HH:MM, home time"),
        ("night_end", "Night ends", "time", None, "HH:MM, home time"),
        ("rotate", "Rotate", "choice", [(0, "0°"), (90, "90°"), (180, "180°"), (270, "270°")], "clockwise"),
        ("gpio_slowdown", "GPIO slowdown", "int", (0, 5), "raise if you see sparkle/ghosting"),
    ]),
    ("Visitor (temporary)", [
        ("visitor.enabled", "Show a visitor", "bool", None,
         "replaces the home/away screen until their flight lands; Dada's own flights still come first"),
        ("visitor.name", "Name", "text", None, "e.g. GRAMMY"),
        ("visitor.location", "Where they are now", "text", None, "e.g. Chicago; blank = the departure airport's city"),
        ("visitor.flight", "Flight number", "text", None, "e.g. WN 383 (optional)"),
        ("visitor.from", "From airport", "iata", None, "e.g. MDW"),
        ("visitor.to", "To airport", "iata", None, "e.g. DEN"),
        ("visitor.depart", "Departs", "datetime", None, f"home time ({CFG['home']['tz']})"),
        ("visitor.arrive", "Lands", "datetime", None, f"home time ({CFG['home']['tz']})"),
    ]),
    ("Polling", [
        ("location_every_s", "Location every (s)", "int", (10, 3600), ""),
        ("calendar_every_s", "Calendar every (s)", "int", (30, 86400), ""),
        ("weather_every_s", "Weather every (s)", "int", (60, 86400), ""),
    ]),
    ("This page", [
        ("web_password", "Password", "secret", None, "leave blank to keep"),
        ("web_port", "Port", "int", (1, 65535), ""),
    ]),
]
RESTART_KEYS = {"gpio_slowdown", "home.tz", "web_port"}  # everything else applies live
CSRF = secrets.token_urlsafe(16)


def _cfg_get(cfg, key):
    for part in key.split("."):
        cfg = cfg.get(part) if isinstance(cfg, dict) else None
    return cfg


def _cfg_set(cfg, key, value):
    *path, last = key.split(".")
    for part in path:
        cfg = cfg.setdefault(part, {})
    cfg[last] = value


def parse_settings(form):
    """Form fields -> ({key: value}, [errors]). Blank secrets are left out (kept)."""
    out, errors = {}, []
    for _, fields in SETTINGS:
        for key, label, kind, extra, _ in fields:
            raw = form.get(key, [""])[0].strip()
            try:
                if kind == "bool":
                    out[key] = raw == "on"
                elif kind == "secret":
                    if raw:
                        out[key] = raw
                elif kind in ("int", "float"):
                    v = int(raw) if kind == "int" else float(raw)
                    if extra and not extra[0] <= v <= extra[1]:
                        raise ValueError(f"must be {extra[0]}-{extra[1]}")
                    out[key] = v
                elif kind == "time":
                    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", raw):
                        raise ValueError("use HH:MM, e.g. 20:30")
                    out[key] = raw
                elif kind == "choice":
                    match = [v for v, _ in extra if str(v) == raw]
                    if not match:
                        raise ValueError("pick one of the options")
                    out[key] = match[0]
                elif kind == "list":
                    out[key] = [x.strip().upper() for x in raw.split(",") if x.strip()]
                elif kind == "dict":
                    table = {}
                    for line in raw.splitlines():
                        if line.strip():
                            k, sep, v = line.partition("=")
                            if not sep or not k.strip() or not v.strip():
                                raise ValueError(f"bad line {line!r}, use CODE=City")
                            table[k.strip().upper()] = v.strip()
                    out[key] = table
                elif kind == "tz":
                    ZoneInfo(raw)
                    out[key] = raw
                elif kind == "iata":
                    if raw and not re.fullmatch(r"[A-Za-z]{3}", raw):
                        raise ValueError("use a 3-letter airport code, e.g. DEN")
                    out[key] = raw.upper()
                elif kind == "datetime":
                    if raw:
                        datetime.fromisoformat(raw)  # "2026-10-24T14:05" from the browser
                    out[key] = raw[:16]
                else:
                    if not raw and key in ("ha_url", "person_entity", "flight_calendar", "dad_name"):
                        raise ValueError("can't be empty")
                    out[key] = raw
            except Exception as e:
                errors.append(f"{label}: {e}")
    if out.get("visitor.enabled") and not errors:  # the visitor needs a complete flight
        missing = [k.split(".")[1] for k in ("visitor.name", "visitor.from", "visitor.to",
                                              "visitor.depart", "visitor.arrive") if not out.get(k)]
        if missing:
            errors.append("Visitor: fill in " + ", ".join(missing))
        elif out["visitor.arrive"] <= out["visitor.depart"]:
            errors.append("Visitor: landing has to be after departure")
    return out, errors


def save_settings(changes):
    """Write the changes into config.json (keeping its other keys and owner) and into CFG.
    Returns True if a restart is needed for them to take effect."""
    global HOME_TZ
    path = os.path.join(HERE, "config.json")
    try:
        with open(path) as fp:
            disk = json.load(fp)
        st = os.stat(path)
    except FileNotFoundError:
        disk, st = {}, None
    restart = False
    for key, v in changes.items():
        if _cfg_get(CFG, key) != v:
            restart |= key in RESTART_KEYS
        _cfg_set(disk, key, v)
        _cfg_set(CFG, key, v)
    tmp = path + ".tmp"
    with open(tmp, "w") as fp:
        json.dump(disk, fp, indent=2)
        fp.write("\n")
    os.chmod(tmp, 0o600)  # it holds the HA token
    if st:
        try:
            os.chown(tmp, st.st_uid, st.st_gid)  # stay editable by mk, not just root
        except OSError:
            pass
    os.replace(tmp, path)
    HOME_TZ = ZoneInfo(CFG["home"]["tz"])
    REFRESH.set()
    return restart


def restart_self():
    log("restarting (settings page)")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _status_rows():
    snap, now = LAST["snap"], datetime.now(timezone.utc)
    if not snap:
        return [("Status", "starting up")]
    with LOCK:
        net, ha, cal_err = dict(STATE["net"]), dict(STATE["ha"]), STATE["cal_error"]
    loc, wx = snap["loc"], snap["wx"]
    rows = [("Screen now", pick_mode(snap, now)[0].upper()),
            ("Location", f"{loc['city']} ({'home' if loc['at_home'] else 'away'})" if loc else "unknown"),
            ("Network", f"{net['status']}, Pi is {net.get('ip') or '?'}"),
            ("Home Assistant", ha["status"] + (f" - {ha['error']}" if ha.get("error") else "")),
            ("Calendar", cal_err or "ok")]
    if wx:
        hl = f", high {wx['hi']} / low {wx['lo']}" if wx.get("hi") is not None else ""
        rows.append(("Weather", f"{wx['temp']}°{hl}"))
    v = snap.get("visitor")
    if v:
        f = v["flight"]
        when = lambda t: f"{t.astimezone(HOME_TZ):%a %b %d %I:%M %p}"
        place = v["loc"]["city"] if v["loc"] else visitor_query() + " (looking up...)"
        if now < f["start"]:
            state = f"in {place}, flies {when(f['start'])}"
        elif now < f["end"]:
            state = f"flying, lands {when(f['end'])}"
        else:
            state = "landed"
        rows.append(("Visitor", f"{f['name']}: {state}"))
    elif CFG["visitor"].get("enabled"):
        rows.append(("Visitor", "finished - you can switch it off"))
    upcoming = [f for f in snap["flights"] if f["end"] > now][:3]
    for f in upcoming:
        rows.append(("Flight", f"{f['start'].astimezone(HOME_TZ):%a %b %d %I:%M %p}  "
                               f"{f['origin'] or '?'} → {f['dest'] or '?'}  {f['number']}"
                               f"{'  (home)' if f['home'] else ''}"))
    if not upcoming:
        rows.append(("Flights", "none booked"))
    return rows


PAGE_CSS = """
:root{color-scheme:light dark;--bg:#f4f4f6;--card:#fff;--fg:#1b1b1f;--mut:#6b6b75;--line:#ddd;--acc:#0a84ff}
@media (prefers-color-scheme:dark){:root{--bg:#111114;--card:#1c1c21;--fg:#eee;--mut:#9a9aa5;--line:#333}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:16px}h1{font-size:22px;margin:4px 0 12px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 14px}
h2{font-size:15px;margin:0 0 8px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.preview{display:block;width:100%;max-width:384px;margin:0 auto;border-radius:6px;image-rendering:pixelated}
table{width:100%;border-collapse:collapse}td{padding:3px 0;vertical-align:top}td:first-child{color:var(--mut);width:38%}
label{display:block;margin:10px 0 2px;font-weight:600}.help{color:var(--mut);font-size:13px}
input[type=text],input[type=password],input[type=number],input[type=datetime-local],select,textarea{width:100%;padding:8px;font:inherit;
 border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
textarea{min-height:70px}.check{display:flex;gap:8px;align-items:center;font-weight:600;margin:12px 0 2px}
button{font:inherit;font-weight:600;padding:10px 16px;border:0;border-radius:8px;background:var(--acc);color:#fff}
button.alt{background:var(--line);color:var(--fg)}.msg{padding:10px 12px;border-radius:8px;margin:0 0 14px}
.ok{background:#1f8f4a22;border:1px solid #1f8f4a}.err{background:#d0302f22;border:1px solid #d0302f}
.bar{display:flex;gap:10px;flex-wrap:wrap;position:sticky;bottom:0;padding:10px 0;background:var(--bg)}
"""


def settings_page(msg="", errors=(), form=None):
    """The page; after a rejected save, `form` (what was submitted) refills the fields."""
    e = html.escape
    out = ["<!doctype html><html><head><meta charset=utf-8>",
           "<meta name=viewport content='width=device-width,initial-scale=1'>",
           f"<title>FlightWall</title><style>{PAGE_CSS}</style></head><body><main>",
           "<h1>FlightWall</h1>"]
    if msg:
        out.append(f"<div class='msg ok'>{e(msg)}</div>")
    if errors:
        out.append("<div class='msg err'>Not saved:<br>" + "<br>".join(e(x) for x in errors) + "</div>")
    out.append("<section><h2>Live</h2><img class=preview id=pv src='panel.png' alt='panel preview'>"
               "<table>" + "".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in _status_rows())
               + "</table></section>")
    out.append(f"<form method=post action='save'><input type=hidden name=csrf value='{CSRF}'>")
    for group, fields in SETTINGS:
        out.append(f"<section><h2>{e(group)}</h2>")
        for key, label, kind, extra, hlp in fields:
            v = _cfg_get(CFG, key)
            raw = form.get(key, [""])[0] if form is not None else None
            if raw is not None and kind == "bool":
                v = raw == "on"
            name = e(key)
            if kind == "bool":
                out.append(f"<label class=check><input type=checkbox name='{name}'"
                           f"{' checked' if v else ''}> {e(label)}</label>")
            else:
                out.append(f"<label for='{name}'>{e(label)}</label>")
                if kind == "secret":
                    ph = "set (leave blank to keep)" if v else "not set"
                    out.append(f"<input type=password id='{name}' name='{name}' placeholder='{ph}' "
                               "autocomplete=new-password>")
                elif kind == "choice":
                    pick = raw if raw is not None else str(v)
                    opts = "".join(f"<option value='{e(str(o))}'{' selected' if str(o) == pick else ''}>{e(t)}</option>"
                                   for o, t in extra)
                    out.append(f"<select id='{name}' name='{name}'>{opts}</select>")
                elif kind == "dict":
                    txt = raw if raw is not None else "\n".join(f"{k}={c}" for k, c in (v or {}).items())
                    out.append(f"<textarea id='{name}' name='{name}'>{e(txt)}</textarea>")
                else:
                    shown = raw if raw is not None else (", ".join(v) if kind == "list"
                                                         else ("" if v is None else str(v)))
                    typ = {"int": "number' step='any", "float": "number' step='any",
                           "datetime": "datetime-local"}.get(kind, "text")
                    out.append(f"<input type='{typ}' id='{name}' name='{name}' value='{e(shown)}'>")
            if hlp:
                out.append(f"<div class=help>{e(hlp)}</div>")
        out.append("</section>")
    out.append("<div class=bar><button>Save</button></form>"
               f"<form method=post action='restart'><input type=hidden name=csrf value='{CSRF}'>"
               "<button class=alt>Restart display</button></form></div>")
    out.append("<script>setInterval(()=>{document.getElementById('pv').src='panel.png?'+Date.now()},2000)"
               "</script></main></body></html>")
    return "".join(out)


class SettingsHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _authed(self):
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
            except Exception:
                pw = ""
            if hmac.compare_digest(pw.encode(), str(CFG["web_password"]).encode()):
                return True
            time.sleep(1)  # slow down guessing
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="FlightWall"')
        self.end_headers()
        return False

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authed():
            return
        path = urlparse(self.path).path
        if path == "/panel.png":
            img = LAST["img"] or Image.new("RGB", (W, H))
            buf = io.BytesIO()
            led_preview(img, 6).save(buf, "PNG")
            return self._send(buf.getvalue(), "image/png")
        if path == "/":
            return self._send(settings_page())
        self._send("not found", "text/plain", 404)

    def do_POST(self):
        if not self._authed():
            return
        n = min(int(self.headers.get("Content-Length") or 0), 100_000)
        form = parse_qs(self.rfile.read(n).decode(), keep_blank_values=True)
        if not hmac.compare_digest(form.get("csrf", [""])[0], CSRF):
            return self._send("Stale page - reload and try again.", "text/plain", 403)
        path = urlparse(self.path).path
        if path == "/restart":
            self._send(settings_page("Restarting... reload this page in about 10 seconds."))
            threading.Timer(1, restart_self).start()
        elif path == "/save":
            changes, errors = parse_settings(form)
            if errors:
                return self._send(settings_page(errors=errors, form=form))
            try:
                restart = save_settings(changes)
            except Exception as e:
                return self._send(settings_page(errors=[f"couldn't write config.json: {e}"]))
            log("settings saved from the web page")
            if restart:
                self._send(settings_page("Saved. Restarting to apply... reload in about 10 seconds."))
                threading.Timer(1, restart_self).start()
            else:
                self._send(settings_page("Saved and applied."))
        else:
            self._send("not found", "text/plain", 404)


def start_web():
    if not CFG["web_password"]:
        log("settings page off (set web_password in config.json to turn it on)")
        return
    try:
        srv = ThreadingHTTPServer(("", int(CFG["web_port"])), SettingsHandler)
    except OSError as e:
        log("settings page couldn't start:", e)
        return
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _, ip = check_network()
    log(f"settings page on http://{ip or '<pi-ip>'}:{CFG['web_port']}/")


# ----------------------------------------------------------------------------
# --check: step-by-step connectivity diagnostic
# ----------------------------------------------------------------------------
def diagnose():
    def ok(m):
        print("  \u2713 " + m)

    def bad(m, fix=None):
        print("  \u2717 " + m + (f"\n      fix: {fix}" if fix else ""))

    print("\nNETWORK")
    st, ip = check_network()
    if st == "no_network":
        bad("no network connection (no IP address or route)",
            "check WiFi with: nmcli dev status   /   sudo nmtui")
        return
    ok(f"connected, Pi's IP is {ip}")
    if st == "ok":
        ok("internet reachable")
    else:
        bad("internet NOT reachable (local network only)", "check the router / modem")

    print("\nHOME ASSISTANT")
    base = CFG["ha_url"].rstrip("/")
    u = urlparse(base)
    if not u.hostname:
        bad(f"ha_url looks wrong: {base!r}", "should look like http://192.168.1.10:8123")
        return
    try:
        addr = socket.gethostbyname(u.hostname)
        ok(f"{u.hostname} resolves to {addr}")
    except OSError:
        bad(f"can't resolve {u.hostname}",
            "use HA's IP address in ha_url instead, e.g. http://192.168.1.10:8123")
        return
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        socket.create_connection((addr, port), timeout=5).close()
        ok(f"port {port} is open")
    except OSError as e:
        bad(f"can't connect to {addr}:{port} ({e})", "is HA running, and is the port right?")
        return
    hdr = {"Authorization": f"Bearer {CFG['ha_token']}"}
    try:
        r = requests.get(base + "/api/", headers=hdr, timeout=10)
    except Exception as e:
        bad(f"HTTP request failed: {e}", "http vs https mismatch in ha_url?")
        return
    if r.status_code in (401, 403):
        bad("token rejected (HTTP %d)" % r.status_code,
            "HA > your profile > Security > Long-lived access tokens > Create, paste into config.json")
        return
    if not r.ok:
        bad(f"unexpected HTTP {r.status_code} from /api/")
        return
    ok("API reachable, token accepted")

    loc = None
    try:
        states = requests.get(base + "/api/states", headers=hdr, timeout=15).json()
        people = [x["entity_id"] for x in states if x["entity_id"].startswith("person.")]
    except Exception:
        people = []
    try:
        st_ = ha_get(f"/api/states/{CFG['person_entity']}")
        a = st_.get("attributes", {})
        has_gps = a.get("latitude") is not None
        ok(f"{CFG['person_entity']}: state '{st_['state']}', GPS {'yes' if has_gps else 'NO'}")
        if not has_gps:
            bad("person has no GPS position",
                "Settings > People > you > add your iPhone's device tracker")
    except Exception as e:
        bad(f"{CFG['person_entity']} not found ({classify_ha_error(e)})",
            f"person entities in your HA: {', '.join(people) or 'none'}")

    try:
        cals = [c["entity_id"] for c in ha_get("/api/calendars")]
    except Exception:
        cals = []
    if CFG["flight_calendar"] in cals:
        ok(f"{CFG['flight_calendar']} found")
    else:
        bad(f"{CFG['flight_calendar']} not found",
            f"calendars in your HA: {', '.join(cals) or 'none'}")

    fl = []
    prefetch_airports()
    try:
        fl = fetch_flights()
        ok(f"{len(fl)} flight(s) found (2 days back to {CFG['lookahead_days']} ahead):")
        for f in fl:
            print(f"      {f['start'].astimezone(HOME_TZ):%a %b %d %I:%M%p} -> "
                  f"{f['end'].astimezone(HOME_TZ):%I:%M%p}  {f['origin']}->{f['dest']} "
                  f"({airport_city(f['dest']) or '?'}) "
                  f"{f['number']:8} home={f['home']}  [{f['summary']}]")
    except Exception as e:
        bad(f"couldn't read flights: {str(e)[:100]}")

    print("\nINTERNET SERVICES")
    try:
        loc = update_location()
        ok(f"location: {loc['city']} (flag {loc['flag']}), "
           f"{'HOME' if loc['at_home'] else 'away'}")
    except Exception as e:
        bad(f"location lookup failed: {str(e)[:100]}")
    try:
        w = fetch_weather(*((loc["lat"], loc["lon"]) if loc else (CFG["home"]["lat"], CFG["home"]["lon"])))
        ok(f"weather: {w['temp']} deg (hi {w['hi']} / lo {w['lo']}), code {w['code']}, timezone {w['tz']}")
    except Exception as e:
        bad(f"weather failed: {str(e)[:100]}")

    now = datetime.now(timezone.utc)
    vf = visitor_plan(now)
    if vf:
        print(f"\nVISITOR\n  {vf['name']} {vf['origin']}->{vf['dest']} {vf['number']}, "
              f"departs {vf['start'].astimezone(HOME_TZ):%a %b %d %I:%M %p}, "
              f"lands {vf['end'].astimezone(HOME_TZ):%I:%M %p} (waiting in {visitor_query()})")
    print("\nSCREEN NOW:", pick_mode({"loc": loc, "flights": fl, "wx": None}, now)[0].upper(), "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="cycle all screens with fake data")
    ap.add_argument("--check", action="store_true", help="print Home Assistant data and exit")
    ap.add_argument("--png", metavar="DIR", help="render demo screens to PNGs and exit")
    args = ap.parse_args()

    if args.png:
        os.makedirs(args.png, exist_ok=True)
        for name, snap, now in demo_scenarios():
            for tt in (0.0, 1.3):
                img = render(snap, now, tt)
                led_preview(img).save(os.path.join(args.png, f"{name}{'' if tt == 0 else '_b'}.png"))
        print("wrote", args.png)
        return

    if args.check:
        diagnose()
        return

    panel = Panel()
    t0 = time.time()
    if args.demo:
        scen = demo_scenarios()
        for _, snap, _ in scen:
            if snap["loc"]:
                prefetch_flag(snap["loc"]["flag"])
        while True:
            t = time.time() - t0
            name, snap, now = scen[int(t // 8) % len(scen)]
            panel.show(render(snap, now + timedelta(seconds=t % 8), t), CFG["brightness"])
            time.sleep(0.1)

    threading.Thread(target=data_loop, daemon=True).start()
    start_web()
    while True:
        now = datetime.now(timezone.utc)
        with LOCK:
            net, ha, cal_err = dict(STATE["net"]), dict(STATE["ha"]), STATE["cal_error"]
            snap = {"loc": STATE["loc"], "wx": STATE["wx"], "home_wx": STATE["home_wx"],
                    "flights": list(STATE["flights"]),
                    "offline": offline_reason(net, ha, cal_err, time.time()),
                    "degraded": degraded(net, ha, cal_err)}
        snap["visitor"] = visitor_snapshot(now)
        try:
            img = render(snap, now, time.time() - t0)
        except Exception as e:  # never let a drawing bug kill the display
            log("render error:", e)
            img = Image.new("RGB", (W, H))
        LAST["img"], LAST["snap"] = img, snap
        panel.show(img, brightness_for(now))
        time.sleep(0.1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
