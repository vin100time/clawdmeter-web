# clawdmeter-web

A tiny Claude usage meter for your desk. It reads **your own** Claude Code usage
limits and shows them in three places, from one small Python server:

- **a thin bar docked above the Windows taskbar**, plus a tray icon that draws the percentage;
- **a web page**, light enough for an old tablet (it was written for an iPad 4 running Safari 10);
- **an ESP32 + SSD1306 OLED**, for a standalone desk gadget.

No dependencies beyond the Python standard library for the server. No account, no
cloud, no telemetry: the only network call is to Anthropic, with the token Claude
Code already stores on your machine.

Inspired by [HermannBjorgvin/Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter),
an ESP32 daemon for the same purpose. This is the "web + PC" take on the idea.

```
| Claude | 5h : [####------]  12% | 7j : [#######---]  36% | Fable : [########--] 42%* |
```

## What it shows

| Metric | Example |
| --- | --- |
| Session limit (5 h) | `12%`, resets in `3h40` |
| Weekly limit (all models) | `36%`, resets in `4j06h` |
| Weekly limit per model | `Fable 42%` — appears only if your account has one |
| Which limit binds first | the `*` marker, as reported by the API |
| Week split by surface | `Claude Code 96%`, `Chats 4%`, `Cowork 0%` |
| Extra-usage credits | shown only when enabled on your account |
| Plan | `Max 20x`, `Pro`, … |

## How it works

```
~/.claude/.credentials.json          OAuth token, read locally on every poll
            |
            v
  clawdmeter_server.py  ---- GET ---->  api.anthropic.com/api/oauth/usage
            |                           (falls back to rate-limit headers)
            |  HTTP, port 8080
    +-------+--------+------------------+
    |                |                  |
 index.html      /usage.json        /esp.json
 (browser)       (Windows bar)      (ESP32 firmware)
```

The data comes from `GET /api/oauth/usage`, the endpoint behind Claude Code's own
`/usage` command. It is a plain read: **it does not consume any of your quota**.

> ⚠️ That endpoint is **undocumented and internal**. Anthropic can change it
> without notice. When it fails, the server falls back to the older method: a
> 1-token request to Haiku, reading the `anthropic-ratelimit-unified-*` response
> headers. You then lose the per-model limit and the weekly split, but the main
> gauges keep working.

## Requirements

- An active Claude subscription with **Claude Code signed in on this machine**
  (the token must exist in `~/.claude/.credentials.json`).
- **Python 3.9+** for the server — standard library only.
- For the Windows bar: **Pillow** and **pywin32** (`pip install pillow pywin32`).
- For the ESP32: **arduino-cli**, ESP32 core 3.x, and the libraries
  *Adafruit SSD1306*, *Adafruit GFX*, *Adafruit BusIO*, *ArduinoJson 7*.

The server itself runs on Windows, Linux and macOS. The docked bar is
Windows-only; elsewhere, use the web page.

## Quick start

```bash
git clone https://github.com/vin100time/clawdmeter-web
cd clawdmeter-web
python clawdmeter_server.py
```

It prints the addresses to open:

```
 Clawdmeter Web demarre
 Sur l'iPad, ouvre l'une de ces adresses dans Safari :
    http://192.168.1.10:8080/
 ESP32 : http://<ip>:8080/esp.json
```

Open that URL in any browser and you are done. The server polls Anthropic every
60 s; the page refreshes every 10 s.

## The Windows bar

```bash
pythonw clawdmeter_tray.py
```

- The bar docks itself **just above the taskbar** of one screen and re-asserts its
  position every 1.5 s, so it cannot end up off-screen or behind another window.
- **Drag it** onto any screen: on release it re-docks to that screen's taskbar,
  aligned left, centre or right depending on where you dropped it. The choice is
  remembered in `%LOCALAPPDATA%\Clawdmeter\widget.json`.
- **Right-click the tray icon** for: show/hide, pick screen, pick alignment, open
  the web page, refresh now, quit. **Hover it** for the full detail.
- The icon shows the binding limit as a drawn number: green below 50 %, orange
  below 80 %, red above.
- It never takes focus and stays out of Alt+Tab.

> Windows 11 hides new tray icons by default. If you do not see it, click the `^`
> arrow next to the clock and drag the icon onto the taskbar.

**Start it at login:** press <kbd>Win</kbd>+<kbd>R</kbd>, run `shell:startup`, and
put a shortcut to `pythonw.exe "…\clawdmeter_tray.py"` in that folder. Do the same
for `clawdmeter_server.py` so the data source starts too.

## The ESP32 display

Hardware: any ESP32 dev board with an SSD1306 128×64 I²C OLED. A two-colour panel
(top 16 px yellow) looks best, since the header row is designed for it.

```bash
cp esp32/clawdmeter_oled/secrets.example.h esp32/clawdmeter_oled/secrets.h
# edit secrets.h: Wi-Fi SSID, password, and your PC's address
arduino-cli compile --fqbn esp32:esp32:esp32 -u -p COM5 esp32/clawdmeter_oled
```

Wiring is the standard I²C pair (SDA 21, SCL 22). The firmware scans the common
pin pairs at boot, so most boards work unchanged. The display shows the focused
limit as a large percentage in the yellow band, and cycles one page per limit
(plus a week-split page) every 6 s.

Useful constants at the top of the sketch: `OLED_CONTRAST` (8 is dim, ~207 is the
library default), `FETCH_MS`, `PAGE_MS`, `OLED_PINS`.

## JSON endpoints

`GET /usage.json` — full state, used by the web page and the Windows bar:

```json
{
  "ok": true, "msg": "ok", "plan": "Max 20x", "source": "usage",
  "limits": [
    {"kind": "session", "label": "Session 5h", "short": "5h", "p": 12,
     "sev": 0, "active": false, "reset_min": 220, "reset": "3h40"}
  ],
  "breakdown": [{"key": "claude_code", "short": "Code", "p": 96}],
  "spend": {"enabled": false, "used": 0.0, "limit": 100.0, "currency": "EUR", "p": 0},
  "s": 12, "sr": 220, "st": "allowed", "w": 36, "claim": "seven_day", "updated": 1789393200
}
```

The short keys (`s`, `sr`, `st`, `w`, `wr`, `wst`, `o`, `ost`, `g`, `claim`) are kept
for the original web page. `sev` is 0 normal, 1 warning, 2 blocked.

`GET /esp.json` — the same data pre-formatted for a microcontroller, ~320 bytes, so
the firmware only has to draw it: `L` is the limit list (`n` name, `p` percent,
`r` reset, `s` severity, `a` active), `B` the week split, `X` the credits, `age`
the seconds since the last successful poll.

## Configuration

| Where | Setting |
| --- | --- |
| `clawdmeter_server.py` | `PORT` (8080), `POLL_INTERVAL` (60 s), or the `CLAWDMETER_HOST` / `CLAWDMETER_PORT` environment variables |
| `clawdmeter_tray.py` | `REFRESH_S`, bar geometry `H` / `BAR_W` / `FONT` / `MARGIN` |
| `clawdmeter_oled.ino` | `FETCH_MS`, `PAGE_MS`, `OLED_CONTRAST`, `OLED_PINS` |

## Privacy and security

- **Your token stays on your machine.** It is read from the Claude Code credential
  file at each poll and sent only to `api.anthropic.com`, which is where Claude
  Code sends it anyway. It is never written to disk by this project, never logged,
  and never exposed by the HTTP endpoints.
- **The server listens on all interfaces with no authentication.** Anyone on your
  network can read your usage percentages (not your token). That is deliberate, so
  a tablet or an ESP32 can reach it. If you only use the Windows bar, restrict it:
  `CLAWDMETER_HOST=127.0.0.1 python clawdmeter_server.py`.
- **`secrets.h` is git-ignored** because it holds your Wi-Fi password. Only
  `secrets.example.h` is tracked.
- Only your own account's usage is ever requested. Nothing is sent anywhere else.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `token Claude introuvable` | Claude Code has never signed in on this machine, or stores its credentials elsewhere. Set `CLAUDE_CREDENTIALS_PATH` to the file. |
| `token expire` | Start Claude Code once so it refreshes the token. |
| Page says `serveur injoignable` | The server is not running, or a firewall blocks port 8080. |
| Browser shows the page but no numbers | Check `/usage.json` directly; `msg` explains the failure. |
| Bar shows `hors ligne` | Same as above — the bar reads `127.0.0.1:8080`. |
| ESP32 shows `Serveur injoignable` with its own IP | The board is on a different subnet from that PC address. Use the IP your PC owns on the board's subnet, in `SERVER_URL` / `SERVER_URL_ALT`. |
| ESP32 shows `OLED introuvable` | Wrong I²C pins; add your pair to `OLED_PINS`. |
| No serial port when flashing | Often a charge-only USB cable: the board's LED lights but Windows reports "device descriptor request failed". Change cable, then port. |

## Credits

- Idea and name: [HermannBjorgvin/Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter).
- Not affiliated with, endorsed by, or supported by Anthropic. "Claude" is their
  trademark. This project only reads your own account's usage, through an
  endpoint that may change at any time.

MIT licensed — see [LICENSE](LICENSE).
