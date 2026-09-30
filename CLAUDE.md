# CLAUDE.md: FlightWall project context

Handoff notes from the claude.ai chat where this project was designed and built (Sep 20–28, 2026). Read this before changing anything.

## What this is

A 64×64 HUB75 LED matrix display for Maxwell's kids that shows where Dada is. It's inspired by TheFlightWall (theflightwall.com) but is a standalone DIY build: Raspberry Pi 4 + Home Assistant data. It must be **fully automatic** (no manual steps) and **fun for kids** (big text, colors, "DADA" branding).

It is **not** part of Maxwell's separate ADS-B receiver project (Pi 5 with two displays). Don't mix them.

## Hardware (bought, assembled, working)

- **Raspberry Pi 4**, hostname `rpi-flightwall`, user `mk`, Raspberry Pi OS Lite 64-bit (Debian, kernel 6.18). The Pi 5 isn't supported by the LED library.
- **Waveshare RGB-Matrix-P3-64x64**: 3 mm pitch, 192×192 mm, 1/32 scan, 5V 4A, board marking `P3(2121)64X64-32S-M5.1`. The ribbon goes to the **left HUB75 header on the upper PCB** (input). The lower PCB's gold pads are unused footprints.
- **WatangTech / SEENGREAT RGB Matrix Adapter Board** (Pi/Pico HUB75 adapter). Its pinout matches hzeller's **default `regular` mapping**: E on GPIO 15, OE on GPIO 18 (hardware PWM, so no jumper mod needed), LAT GPIO 4, CLK GPIO 17. So `hardware_mapping = "regular"` and no `--led-gpio-mapping` flag. **No level shifters** (3.3V passes straight through); if there's sparkle or ghosting, raise `gpio_slowdown` (currently 4). There's probably no reverse-polarity protection. The RPI PWR jumper is ON, so the board powers the Pi.
- **Power:** a single 5V 4A supply, 5.5×2.1 mm barrel, center-positive, ETL-listed, into the adapter's DC jack. **Never plug USB-C into the Pi as well.** (A 2.5 mm-plug supply was rejected as a loose fit.)
- No soldering anywhere.

## Status as of Sep 28

- Hardware assembled; the panel powers on (showed the normal idle "two blue lines"); SSH works.
- `wall.py` was delivered and previewed (all screens render correctly via `--png`), but **it has not yet run against the real Home Assistant or real Flighty data.**
- **Blocker:** the Pi can't reach HA. HA is at `192.168.1.3:8123` on the home LAN. The Pi is temporarily behind a **UniFi Travel Router (UTR)** while Maxwell is away, on `192.168.129.57` (192.168.129.x subnet), backhauling home. `curl` to HA **times out**. A home firewall rule allowing the Pi's IP to reach 8123 didn't help, most likely because home sees the UTR's Teleport/VPN address, not the Pi's. The unverified next checks were `ping 192.168.1.1`, `traceroute -n 192.168.1.3`, and whether the UTR's upstream is also 192.168.1.x (a subnet clash). **Expected resolution:** it'll work once the display is installed at home on the home LAN.
- `homeassistant.local` probably won't resolve on the Pi. Use `"ha_url": "http://192.168.1.3:8123"`.
- Not yet confirmed: whether the system setup block (audio off, snd_bcm2835 blacklist, `isolcpus=3`, library build and pip install) and the systemd service were completed. Verify on the Pi before debugging anything else.
- Not yet confirmed: the real `person_entity`, the `flight_calendar` entity ID, and that Flighty Calendar Export → Google/iCloud calendar → HA integration is set up. `--check` lists the actual person and calendar entities if the configured ones are wrong.

## Repo layout

```
wall.py               the whole app (single file, ~1100 lines)
config.example.json   template; real config.json holds the HA token and is git-ignored
flightwall.service    systemd unit: runs /home/mk/flightwall/wall.py as root, Restart=always
docs/screens.png      preview of all screens
README.md             user-facing docs (install, config, troubleshooting)
.cache/               runtime cache (fonts, flags, geocode, airports) - git-ignored
```

On the Pi, the app lives in `~/flightwall/` and the LED library in `~/rpi-rgb-led-matrix/` (fonts at `/home/mk/rpi-rgb-led-matrix/fonts`).

## How wall.py works

- **Rendering:** every frame is drawn into a 64×64 PIL image, then `canvas.SetImage()` + `SwapOnVSync()` at about 10 fps. hzeller BDF fonts are converted once to PIL bitmap fonts (cached in `.cache/fonts`). Helpers: `text()` draws by **baseline** and returns the x after the text; `ctext`, `rainbow`, `fit_font`; `fit_text()` tries 5x7, then 4x6, then a scrolling marquee; `sprite()` draws pixel art from string rows (PLANE, PLANE_SMALL, HEART, HOUSE, WIFI); `weather_icon()` draws procedural 12×12 icons (sun, moon, partly, cloudy, fog, rain, snow, storm) from Open-Meteo WMO codes plus `is_day`.
- **`render(snap, now, t)` is pure:** a snapshot dict plus a UTC datetime plus an animation time goes in, an image comes out. That makes `--png` and `--demo` work with fake data.
- **Data thread (`data_loop`):**
  - network check every 30 s (`check_network`: UDP route trick, then TCP to 1.1.1.1:443 / 8.8.8.8:53)
  - HA person location every 60 s
  - HA calendar every 5 min
  - weather for the current location every 10 min, or immediately if he moved more than 10 km
  - Denver weather every 15 min (for the offline screen)
  - OurAirports download once
- **Shared `STATE`** is guarded by `LOCK`. HA status tracking uses `_set_status` with `fail_since` timestamps; errors are classified as `ha_down` / `ha_auth` (401/403) / `ha_entity` (404).
- **Location:** `/api/states/<person>` lat/lon go through Nominatim reverse geocoding (zoom 10, `accept-language=en`, cached per 0.01°). The flag code is `us-xx` for US states (from `ISO3166-2-lvl4`), otherwise the country code. Flags come from `https://flagcdn.com/w80/<code>.png` and are resized to 16×11. "Home" means within 40 km of the Denver coordinates, or the HA person state is `home`.
- **Flights:** `/api/calendars/<entity>?start&end` (2 days back to 90 days ahead). `parse_flight` strips U+200B, U+2060 and U+00A0 (Flighty inserts these) and matches `XXX→YYY` / `->` / `to`, plus a flight number like `UA 2211`. A flight is "home" if the destination is in `home.airports` (`["DEN"]`), or the text says "to Denver". Event start = departure, end = arrival.
- **Airport → city:** OurAirports `airports.csv` becomes a cached `{IATA: municipality}` table (keeping the last comma part, e.g. "Ota, Tokyo" → "Tokyo"). `AIRPORT_FIXES` overrides odd municipalities (NRT→Tokyo, EGE→Vail…), and the `airport_names` config key overrides those. Unknown codes fall back to showing the code.

### Screen priority (`pick_mode`)

1. **flying**: a calendar flight in progress. Shows "DADA IS FLYING", route, progress bar, time left. Flights home show "COMING HOME!" plus landing time (Denver time); others show "TO <CITY>" plus the flight number.
2. **landed**: a home flight ended less than `landed_minutes` (90) ago: "DADA LANDED! N MIN AGO".
3. **leaving**: a flight from DEN departs within `leaving_soon_hours` (4): "DADA FLIES SOON", "TO <CITY>", countdown, takeoff time.
4. **offline**: after `offline_after_s` (90 s) of failure. Shows Denver time and weather plus a message: NO NETWORK / NO INTERNET / NO HA CONNECTIVITY / HA TOKEN REJECTED / HA PERSON NOT FOUND, with a hint line (CHECK WIFI or the Pi's IP). Weather more than 1 hour old is hidden.
5. **home**: "DADA IS HOME!" with pulsing hearts and a rainbow, plus Denver time and weather.
6. **away**: flag + city (scrolls if long), big local time, weather icon + temperature, and a countdown to the next flight home. Days are counted as calendar days in Denver time: "N DAYS TIL DADA!" for 2 or more days, then "HOME TOMORROW" / "HOME TODAY!" plus the arrival time. If nothing is booked, the date is shown instead.

Screens 1–3 use the **cached calendar**, so they keep working through an HA outage. A **blinking orange 2×2 dot** in the top-right corner means a partial problem (internet down while HA is OK, a calendar error, or HA failing but still within the grace period).

Other behavior: night dimming (brightness 60 → 15 from 20:30 to 06:30 Denver time); 12-hour clock (`clock_24h` switches it); `rotate` turns the whole image for sideways/upside-down mounting; poll intervals are `location_every_s` / `calendar_every_s` / `weather_every_s`; all text is uppercase ASCII (`ascii_only`).

### 64×64 layout budget

- **Away:** header rows 0–11 (flag at 1,1; city at x=20, baseline 9); time 9x18B at baseline 27; weather at y=31; divider at y=45; countdown in rows 47–63.
- Everything must fit in 64 px. Check width with `font(name).width(s)`, and preview with `--png` after any layout change.

## Commands

- `sudo python3 wall.py`: normal mode (what the service runs).
- `python3 wall.py --check`: step-by-step diagnostic: network, internet, HA DNS/port/token, person entity + GPS, calendar entity, parsed flights with destination cities, geocoding, weather, which screen it would show now.
- `sudo python3 wall.py --demo`: cycles all demo scenarios, 8 s each. Stop the service first (`sudo systemctl stop flightwall`).
- `python3 wall.py --png DIR`: renders every demo scenario as a dotted-LED PNG at 8× scale, at t=0 and t=1.3. **This is the development loop** and needs no panel: set `font_dir` to a clone of hzeller's `fonts/`. Flags and airports need network access; for offline previews, drop stand-in PNGs in `.cache/flags/<code>.png` and a small `.cache/airports.json`.
- Service: `sudo systemctl restart flightwall`; logs: `journalctl -u flightwall -f`.
- Deploy from the laptop: `scp wall.py mk@<pi-ip>:~/flightwall/ && ssh mk@<pi-ip> "sudo systemctl restart flightwall"`.

### One-time Pi setup (from the chat)

```bash
sudo apt install -y git build-essential python3-dev python-dev-is-python3 python3-pil python3-requests cython3
sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' /boot/firmware/config.txt
echo "blacklist snd_bcm2835" | sudo tee /etc/modprobe.d/blacklist-rgb-matrix.conf
sudo sed -i '1 s/$/ isolcpus=3/' /boot/firmware/cmdline.txt   # run once only
git clone https://github.com/hzeller/rpi-rgb-led-matrix
make -C rpi-rgb-led-matrix/examples-api-use
cd rpi-rgb-led-matrix && sudo pip3 install . --break-system-packages
# panel test:
sudo ~/rpi-rgb-led-matrix/examples-api-use/demo -D0 --led-rows=64 --led-cols=64 --led-slowdown-gpio=4
```

## Conventions and preferences

- Keep it a **single file** with no dependencies beyond `requests`, Pillow and the `rgbmatrix` binding.
- Never let the display die: `render()` errors are caught and logged, and data-fetch failures keep the last good values.
- Downloads (flags, airports, geocode) happen only in the data thread, never while drawing.
- Test every visual change with `--png` and actually look at the output before shipping. Add a demo scenario for any new screen.
- Keep `config.json` (the token) out of git. New config keys need a default in `DEFAULTS` plus an entry in `config.example.json` and the README's config table.
- Maxwell is technically strong (HA, ESPHome, UniFi, Docker/Unraid, published a HACS integration), so skip the basics. He uses Flighty to track flights and runs UniFi networking.

## Open ideas, not yet requested

- Verify the parser against real Flighty events once HA is reachable, and adjust `ROUTE` if needed.
- Tune `gpio_slowdown` down (2–3) if the panel is clean.
- A "landed in <city>" screen for non-Denver arrivals.
- An enclosure or frame for wall mounting.
