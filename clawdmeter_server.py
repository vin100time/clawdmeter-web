#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clawdmeter Web — affichage de l'usage Claude Code (vieux Safari + ESP32 OLED).

Adapte le daemon ESP32 "Clawdmeter" (HermannBjorgvin/Clawdmeter) en un petit
serveur web local. Aucune dependance externe : uniquement la lib standard Python.

- Lit le token OAuth Claude Code (~/.claude/.credentials.json, etc.)
- Interroge toutes les 60 s l'endpoint d'usage de Claude Code (/api/oauth/usage),
  simple GET qui ne consomme pas de quota. En secours : ancien appel Haiku +
  lecture des en-tetes de rate-limit.
- Sert sur le reseau local (port 8080) :
    /            index.html (iPad)
    /usage.json  etat complet
    /esp.json    version compacte, pre-formatee pour l'ecran de l'ESP32

Lance simplement :  python clawdmeter_server.py
"""

import json
import os
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# 0.0.0.0 : joignable par l'iPad et l'ESP32. Mettre CLAWDMETER_HOST=127.0.0.1
# pour n'ecouter qu'en local (si seule la barre Windows lit le serveur).
HOST = os.environ.get("CLAWDMETER_HOST", "0.0.0.0")
PORT = int(os.environ.get("CLAWDMETER_PORT", "8080"))
POLL_INTERVAL = 60          # secondes entre deux appels API
PROFILE_INTERVAL = 3600     # l'abonnement change rarement
API_BASE = "https://api.anthropic.com"
USAGE_URL = API_BASE + "/api/oauth/usage"
PROFILE_URL = API_BASE + "/api/oauth/profile"
MESSAGES_URL = API_BASE + "/v1/messages"

API_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.270",
}

API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}

HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"

# Chemins candidats pour les credentials Claude Code (ordre de priorite)
def credential_candidates():
    cands = []
    env_path = os.environ.get("CLAUDE_CREDENTIALS_PATH")
    if env_path:
        cands.append(Path(env_path))
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg_dir:
        cands.append(Path(cfg_dir) / ".credentials.json")
    home = Path.home()
    cands.append(home / ".claude" / ".credentials.json")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(Path(local) / "Claude" / ".credentials.json")
    appdata = os.environ.get("APPDATA")
    if appdata:
        cands.append(Path(appdata) / "Claude" / ".credentials.json")
    return cands


# Etat partage entre le thread de polling et le serveur web.
# Les champs courts (s, sr, st, ...) sont ceux lus par index.html.
STATE = {
    "s": 0, "sr": 0, "st": "unknown",
    "w": 0, "wr": 0, "wst": "unknown",
    "o": 0, "ore": 0, "ost": "unknown",
    "g": "unknown", "claim": "five_hour",
    "limits": [],       # [{kind, label, short, p, sev, severity, active, reset_min, reset}]
    "breakdown": [],    # [{key, label, short, p}] repartition de la semaine
    "spend": None,      # credits de depassement
    "plan": "",
    "source": "",       # "usage" ou "headers"
    "ok": False,
    "msg": "demarrage...",
    "updated": 0,
}
STATE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Lecture du token
# ---------------------------------------------------------------------------

def extract_access_token(blob):
    """Extrait l'accessToken depuis differentes formes de JSON."""
    try:
        data = json.loads(blob)
        if isinstance(data, dict):
            if isinstance(data.get("accessToken"), str):
                return data["accessToken"]
            oauth = data.get("claudeAiOauth")
            if isinstance(oauth, dict) and isinstance(oauth.get("accessToken"), str):
                return oauth["accessToken"]
    except (ValueError, TypeError):
        pass
    # Repli : regex
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    m = re.search(r"[A-Za-z0-9_\-.~+/=]{20,}", blob)
    if m:
        return m.group(0)
    return None


def read_token():
    """Relit le token a chaque appel (Claude Code peut le rafraichir)."""
    for path in credential_candidates():
        try:
            if path.is_file():
                return extract_access_token(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return None


def api_get(url, token):
    headers = dict(API_HEADERS)
    headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Mise en forme
# ---------------------------------------------------------------------------

def ascii_only(text):
    """L'ecran de l'ESP32 n'a pas d'accents : 'Deja' plutot que 'Déjà'."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    return text.encode("ascii", "ignore").decode("ascii")


def minutes_until(iso):
    if not iso:
        return 0
    try:
        ts = datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return 0
    mins = (ts - time.time()) / 60.0
    return int(round(mins)) if mins > 0 else 0


def short_duration(mins):
    """42m, 1h12, 5j22h"""
    if mins <= 0:
        return ""
    if mins < 60:
        return "%dm" % mins
    if mins < 24 * 60:
        return "%dh%02d" % (mins // 60, mins % 60)
    return "%dj%02dh" % (mins // 1440, (mins % 1440) // 60)


def severity_level(severity):
    """0 normal, 1 attention, 2 bloque/critique."""
    s = (severity or "normal").lower()
    if s in ("normal", "ok", "allowed", "none", ""):
        return 0
    if "warn" in s or "elevated" in s:
        return 1
    return 2


LEGACY_STATUS = {0: "allowed", 1: "allowed_warning", 2: "rejected"}
LEGACY_CLAIM = {"session": "five_hour", "weekly_all": "seven_day"}
BREAKDOWN_SHORT = {"claude_code": "Code", "chat": "Chat", "cowork": "Cowrk", "other": "Autre"}


def limit_labels(entry):
    kind = entry.get("kind") or ""
    if kind == "session":
        return "Session 5h", "5h"
    if kind == "weekly_all":
        return "Semaine 7j", "7j"
    scope = entry.get("scope") or {}
    name = ""
    for key in ("model", "surface"):
        part = scope.get(key)
        if isinstance(part, dict):
            name = part.get("display_name") or part.get("id") or ""
        elif isinstance(part, str):
            name = part
        if name:
            break
    name = ascii_only(name) or kind
    if kind.startswith("weekly"):
        return "Semaine " + name, name[:5]
    return name, name[:5]


# ---------------------------------------------------------------------------
# Source 1 : endpoint d'usage (celui de /usage dans Claude Code)
# ---------------------------------------------------------------------------

def parse_usage(raw):
    limits = []
    for entry in raw.get("limits") or []:
        label, short = limit_labels(entry)
        mins = minutes_until(entry.get("resets_at"))
        sev = severity_level(entry.get("severity"))
        limits.append({
            "kind": entry.get("kind") or "",
            "label": label,
            "short": short,
            "p": int(round(float(entry.get("percent") or 0))),
            "sev": sev,
            "severity": entry.get("severity") or "",
            "active": bool(entry.get("is_active")),
            "reset_min": mins,
            "reset": short_duration(mins),
        })

    # Repli si la liste generique est absente : anciens blocs five_hour / seven_day
    if not limits:
        for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
            block = raw.get(key)
            if isinstance(block, dict):
                label, short = limit_labels({"kind": kind})
                mins = minutes_until(block.get("resets_at"))
                limits.append({
                    "kind": kind, "label": label, "short": short,
                    "p": int(round(float(block.get("utilization") or 0))),
                    "sev": 0, "severity": "", "active": kind == "session",
                    "reset_min": mins, "reset": short_duration(mins),
                })

    breakdown = []
    bd = raw.get("seven_day_breakdown") or {}
    for row in bd.get("rows") or []:
        key = row.get("key") or ""
        label = ascii_only(row.get("display_name") or key)
        breakdown.append({
            "key": key,
            "label": label,
            "short": BREAKDOWN_SHORT.get(key, label[:5]),
            "p": int(round(float(row.get("percent") or 0))),
        })

    spend = None
    sp = raw.get("spend")
    if isinstance(sp, dict):
        def money(obj):
            if not isinstance(obj, dict):
                return None
            return (obj.get("amount_minor") or 0) / float(10 ** (obj.get("exponent") or 0))
        used = money(sp.get("used"))
        limit = money(sp.get("limit"))
        currency = (sp.get("used") or sp.get("limit") or {}).get("currency") or ""
        spend = {
            "enabled": bool(sp.get("enabled")),
            "used": used,
            "limit": limit,
            "currency": currency,
            "p": int(round(float(sp.get("percent") or 0))),
            "sev": severity_level(sp.get("severity")),
            "disabled_reason": sp.get("disabled_reason") or "",
        }

    result = {
        "limits": limits,
        "breakdown": breakdown,
        "spend": spend,
        "source": "usage",
        "ok": True,
        "msg": "ok",
    }

    # Champs historiques pour index.html
    by_kind = dict((l["kind"], l) for l in limits)
    for prefix, kind in (("s", "session"), ("w", "weekly_all")):
        l = by_kind.get(kind)
        if l:
            result[prefix] = l["p"]
            result[prefix + "r"] = l["reset_min"]
            result["st" if prefix == "s" else "wst"] = LEGACY_STATUS[l["sev"]]
    if spend and spend["enabled"]:
        result.update({"o": spend["p"], "ore": 0, "ost": LEGACY_STATUS[spend["sev"]]})
    else:
        # desactive (ex : plus de credits) n'est pas un blocage
        result.update({"o": 0, "ore": 0, "ost": "disabled"})
    worst = max([l["sev"] for l in limits] or [0])
    result["g"] = LEGACY_STATUS[worst]
    active = [l for l in limits if l["active"]]
    result["claim"] = LEGACY_CLAIM.get(active[0]["kind"], "") if active else ""
    return result


def plan_label(profile):
    account = profile.get("account") or {}
    org = profile.get("organization") or {}
    tier = (org.get("rate_limit_tier") or "").lower()
    m = re.search(r"(\d+)x", tier)
    if "max" in tier and m:
        return "Max " + m.group(1) + "x"
    if account.get("has_claude_max"):
        return "Max"
    if account.get("has_claude_pro"):
        return "Pro"
    return ascii_only(org.get("organization_type") or "")


# ---------------------------------------------------------------------------
# Source 2 (secours) : appel Haiku + en-tetes de rate-limit
# ---------------------------------------------------------------------------

def parse_headers(headers):
    now = time.time()

    def hdr(name, default="0"):
        v = headers.get(name)
        return v if v is not None else default

    def reset_minutes(reset_ts):
        try:
            r = float(reset_ts)
        except (ValueError, TypeError):
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util):
        try:
            return int(round(float(util) * 100))
        except (ValueError, TypeError):
            return 0

    result = {
        "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
        "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
        "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
        "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
        "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
        "wst": hdr("anthropic-ratelimit-unified-7d-status", "unknown"),
        "o": pct(hdr("anthropic-ratelimit-unified-overage-utilization")),
        "ore": reset_minutes(hdr("anthropic-ratelimit-unified-overage-reset")),
        "ost": hdr("anthropic-ratelimit-unified-overage-status", "unknown"),
        "g": hdr("anthropic-ratelimit-unified-status", "unknown"),
        "claim": hdr("anthropic-ratelimit-unified-representative-claim", "five_hour"),
    }
    claim = result["claim"]
    result["limits"] = [
        {"kind": "session", "label": "Session 5h", "short": "5h", "p": result["s"],
         "sev": 0 if result["st"] == "allowed" else 2, "severity": result["st"],
         "active": claim == "five_hour", "reset_min": result["sr"],
         "reset": short_duration(result["sr"])},
        {"kind": "weekly_all", "label": "Semaine 7j", "short": "7j", "p": result["w"],
         "sev": 0 if result["wst"] == "allowed" else 2, "severity": result["wst"],
         "active": claim == "seven_day", "reset_min": result["wr"],
         "reset": short_duration(result["wr"])},
    ]
    result["breakdown"] = []
    result["spend"] = None
    result["source"] = "headers"
    return result


def poll_headers(token):
    headers = dict(API_HEADERS)
    headers["Authorization"] = "Bearer " + token
    data = json.dumps(API_BODY).encode("utf-8")
    req = urllib.request.Request(MESSAGES_URL, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = parse_headers(resp.headers)
            result["ok"] = True
            result["msg"] = "ok (en-tetes)"
            return result
    except urllib.error.HTTPError as e:
        # Meme sur une erreur HTTP, les en-tetes de rate-limit sont presents
        result = parse_headers(e.headers or {})
        if e.headers and e.headers.get("anthropic-ratelimit-unified-5h-utilization") is not None:
            result["ok"] = True
            result["msg"] = "ok (en-tetes, HTTP %d)" % e.code
            return result
        if e.code == 401:
            return {"ok": False, "msg": "token expire (relance Claude Code)"}
        return {"ok": False, "msg": "HTTP %d" % e.code}
    except urllib.error.URLError as e:
        return {"ok": False, "msg": "reseau: %s" % e.reason}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": "erreur: %s" % e}


# ---------------------------------------------------------------------------
# Boucle de polling
# ---------------------------------------------------------------------------

def poll_once():
    token = read_token()
    if not token:
        return {"ok": False, "msg": "token Claude introuvable"}
    try:
        return parse_usage(api_get(USAGE_URL, token))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "msg": "token expire (relance Claude Code)"}
        print("[usage] HTTP %d, repli sur les en-tetes" % e.code)
    except urllib.error.URLError as e:
        return {"ok": False, "msg": "reseau: %s" % e.reason}
    except Exception as e:  # noqa: BLE001  (format inattendu, etc.)
        print("[usage] %s, repli sur les en-tetes" % e)
    return poll_headers(token)


def refresh_plan():
    token = read_token()
    if not token:
        return
    try:
        label = plan_label(api_get(PROFILE_URL, token))
    except Exception as e:  # noqa: BLE001
        print("[profil] %s" % e)
        return
    with STATE_LOCK:
        STATE["plan"] = label


def poll_loop():
    last_profile = 0
    while True:
        if time.time() - last_profile > PROFILE_INTERVAL:
            refresh_plan()
            last_profile = time.time()
        result = poll_once()
        result["updated"] = int(time.time())
        with STATE_LOCK:
            if result.get("ok"):
                STATE.update(result)
            else:
                # on garde les dernieres valeurs connues mais on signale l'etat
                STATE["ok"] = False
                STATE["msg"] = result.get("msg", "erreur")
                STATE["updated"] = result["updated"]
            summary = " ".join("%s=%d%%" % (l["short"], l["p"]) for l in STATE["limits"])
        status = "OK" if result.get("ok") else "KO"
        print("[%s] %s  %s  %s" % (time.strftime("%H:%M:%S"), status, summary, STATE.get("msg")))
        time.sleep(POLL_INTERVAL)


def esp_payload():
    """Vue compacte pour l'ESP32 : tout est deja formate, le firmware ne fait qu'afficher."""
    with STATE_LOCK:
        st = json.loads(json.dumps(STATE))
    payload = {
        "ok": 1 if st["ok"] else 0,
        "msg": ascii_only(st["msg"])[:21],
        "age": int(time.time() - st["updated"]) if st["updated"] else -1,
        "plan": st["plan"],
        "L": [{"n": l["short"], "p": l["p"], "r": l["reset"], "s": l["sev"], "a": 1 if l["active"] else 0}
              for l in st["limits"]],
        "B": [{"n": b["short"], "p": b["p"]} for b in st["breakdown"]],
    }
    sp = st.get("spend")
    if sp:
        payload["X"] = {
            "on": 1 if sp["enabled"] else 0,
            "u": "%.2f" % (sp["used"] or 0),
            "m": "%.0f" % (sp["limit"] or 0),
            "c": sp["currency"],
            "p": sp["p"],
            "s": sp["sev"],
        }
    return payload


# ---------------------------------------------------------------------------
# Serveur HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                body = INDEX_HTML.read_bytes()
            except OSError:
                body = b"<h1>index.html introuvable</h1>"
                self._send(500, body, "text/html; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/usage.json":
            with STATE_LOCK:
                body = json.dumps(STATE).encode("utf-8")
            self._send(200, body, "application/json")
        elif path == "/esp.json":
            body = json.dumps(esp_payload(), separators=(",", ":")).encode("utf-8")
            self._send(200, body, "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):
        pass  # silence


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def main():
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 52)
    print(" Clawdmeter Web demarre")
    print("=" * 52)
    print(" Sur l'iPad, ouvre l'une de ces adresses dans Safari :")
    for ip in lan_ips() or ["<ip-de-ton-pc>"]:
        print("    http://%s:%d/" % (ip, PORT))
    print(" ESP32 : http://<ip>:%d/esp.json" % PORT)
    print("")
    print(" (Ctrl+C pour arreter)")
    print("=" * 52)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArret.")


if __name__ == "__main__":
    main()
