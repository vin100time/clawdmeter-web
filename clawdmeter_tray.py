#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clawdmeter Tray — usage Claude dans la barre des taches Windows.

Lit http://127.0.0.1:8080/usage.json (clawdmeter_server.py) et affiche :
  - une icone dessinee dans la zone de notification (le pourcentage de la
    limite la plus contraignante, en couleur), avec le detail au survol ;
  - une barre d'une ligne ancree juste au-dessus de la barre des taches de
    l'ecran choisi, toujours au premier plan.

La barre se recale en permanence : elle ne peut ni se perdre hors ecran, ni
passer derriere une autre fenetre. On la deplace en la glissant sur l'ecran
voulu, elle se raccroche alors a la barre des taches de cet ecran.

Ne depend que de ce qui est deja installe : Pillow, pywin32, tkinter.
Lancement :  pythonw clawdmeter_tray.py
"""

import json
import os
import queue
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import win32api
import win32con
import win32gui

SERVER = "http://127.0.0.1:8080"
USAGE_URL = SERVER + "/usage.json"
REFRESH_S = 30
ANCHOR_MS = 1500          # frequence de recalage de la barre
CONFIG = Path(os.environ.get("LOCALAPPDATA", ".")) / "Clawdmeter" / "widget.json"

# Palette, reprise de la page iPad
BG = "#14121a"
FG = "#f4f1ea"
DIM = "#8b8478"
ACCENT = "#ff9b3d"
BAR_BG = "#2a2733"
GREEN, ORANGE, RED, GRAY = "#4dd17a", "#ffb13d", "#ff4d4d", "#6f6a60"

MENU_TOGGLE, MENU_PAGE, MENU_REFRESH, MENU_QUIT = 1001, 1002, 1003, 1004
MENU_SCREEN, MENU_ALIGN = 1100, 1200          # +index
ALIGNS = ("gauche", "centre", "droite")
WM_TRAY = win32con.WM_USER + 20

STATE = {"ok": False, "msg": "demarrage...", "limits": [], "breakdown": [],
         "spend": None, "plan": ""}
LOCK = threading.Lock()


def color_for(p):
    if p >= 80:
        return RED
    if p >= 50:
        return ORANGE
    return GREEN


def snapshot():
    with LOCK:
        return json.loads(json.dumps(STATE))


def worst_limit(st):
    """Limite mise en avant : celle signalee par l'API, sinon la plus haute."""
    limits = st.get("limits") or []
    if not limits:
        return None
    for l in limits:
        if l.get("active"):
            return l
    return max(limits, key=lambda l: l.get("p", 0))


def monitors():
    """Zones de travail des ecrans : le bas exclut la barre des taches."""
    out = []
    for handle, _, _ in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(handle)
        out.append({"work": info["Work"], "full": info["Monitor"],
                    "primary": bool(info["Flags"] & 1)})
    out.sort(key=lambda m: (m["full"][0], m["full"][1]))
    return out or [{"work": (0, 0, 1280, 720), "full": (0, 0, 1280, 720), "primary": True}]


# ---------------------------------------------------------------------------
# Lecture du serveur
# ---------------------------------------------------------------------------

def fetch_loop(notify):
    while True:
        try:
            with urllib.request.urlopen(USAGE_URL, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            data.setdefault("ok", False)
        except Exception as e:  # noqa: BLE001
            data = {"ok": False, "msg": "serveur injoignable (%s)" % type(e).__name__}
        with LOCK:
            if data.get("ok"):
                STATE.update(data)
            else:
                STATE["ok"] = False
                STATE["msg"] = data.get("msg", "erreur")
        notify()
        time.sleep(REFRESH_S)


# ---------------------------------------------------------------------------
# Icone de la zone de notification
# ---------------------------------------------------------------------------

def load_font(size):
    for name in ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                pass
    return ImageFont.load_default()


def draw_icon(text, color):
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, 31, 31], radius=7, fill=(20, 18, 26, 255),
                        outline=color, width=2)
    font = load_font(21 if len(text) <= 2 else 15)
    box = d.textbbox((0, 0), text, font=font)
    d.text(((32 - (box[2] - box[0])) / 2 - box[0],
            (32 - (box[3] - box[1])) / 2 - box[1]), text, font=font, fill=color)
    return img


def tooltip_text(st):
    if not st.get("ok"):
        return ("Clawdmeter - %s" % st.get("msg", "hors ligne"))[:127]
    lines = ["Claude" + (" - " + st["plan"] if st.get("plan") else "")]
    for l in st.get("limits", []):
        reset = " (%s)" % l["reset"] if l.get("reset") else ""
        lines.append("%s %d%%%s%s" % (l["label"], l["p"],
                                      " <<" if l.get("active") else "", reset))
    return "\n".join(lines)[:127]


class Tray(threading.Thread):
    """Icone de notification : fenetre cachee + boucle de messages Windows."""

    def __init__(self, commands):
        super().__init__(daemon=True)
        self.commands = commands
        self.hwnd = None
        self.hicon = None
        self.dirty = threading.Event()
        self.running = True
        self.screens = 1
        self.icon_path = Path(tempfile.gettempdir()) / "clawdmeter_tray.ico"

    def run(self):
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = "ClawdmeterTray"
        wc.lpfnWndProc = self.wnd_proc
        win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindow(wc.lpszClassName, "Clawdmeter", 0,
                                          0, 0, 0, 0, 0, 0, 0, None)
        self.update_icon(add=True)
        while self.running:
            win32gui.PumpWaitingMessages()
            if self.dirty.is_set():
                self.dirty.clear()
                self.update_icon()
            time.sleep(0.15)

    def nid(self, tip):
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        return (self.hwnd, 0, flags, WM_TRAY, self.hicon, tip)

    def update_icon(self, add=False):
        st = snapshot()
        l = worst_limit(st)
        if st.get("ok") and l:
            text, color = str(l["p"]), color_for(l["p"])
        else:
            text, color = "--", GRAY
        draw_icon(text, color).save(self.icon_path, sizes=[(32, 32)])
        old = self.hicon
        self.hicon = win32gui.LoadImage(0, str(self.icon_path), win32con.IMAGE_ICON,
                                        32, 32, win32con.LR_LOADFROMFILE)
        action = win32gui.NIM_ADD if add else win32gui.NIM_MODIFY
        win32gui.Shell_NotifyIcon(action, self.nid(tooltip_text(st)))
        if old:
            win32gui.DestroyIcon(old)

    def wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam == win32con.WM_LBUTTONDBLCLK:
                self.commands.put(("toggle", None))
            elif lparam in (win32con.WM_RBUTTONUP, win32con.WM_LBUTTONUP):
                self.popup_menu()
        elif msg == win32con.WM_COMMAND:
            self.dispatch(win32gui.LOWORD(wparam))
        elif msg == win32con.WM_DESTROY:
            self.remove()
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def dispatch(self, cmd):
        simple = {MENU_TOGGLE: "toggle", MENU_PAGE: "page",
                  MENU_REFRESH: "refresh", MENU_QUIT: "quit"}
        if cmd in simple:
            self.commands.put((simple[cmd], None))
        elif MENU_SCREEN <= cmd < MENU_SCREEN + 16:
            self.commands.put(("screen", cmd - MENU_SCREEN))
        elif MENU_ALIGN <= cmd < MENU_ALIGN + len(ALIGNS):
            self.commands.put(("align", ALIGNS[cmd - MENU_ALIGN]))

    def popup_menu(self):
        mons = monitors()
        screens = win32gui.CreatePopupMenu()
        for i, m in enumerate(mons):
            l, t, r, b = m["full"]
            label = "Ecran %d - %dx%d%s" % (i + 1, r - l, b - t,
                                            " (principal)" if m["primary"] else "")
            win32gui.AppendMenu(screens, win32con.MF_STRING, MENU_SCREEN + i, label)
        aligns = win32gui.CreatePopupMenu()
        for i, name in enumerate(ALIGNS):
            win32gui.AppendMenu(aligns, win32con.MF_STRING, MENU_ALIGN + i, name.capitalize())

        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_TOGGLE, "Afficher / masquer la barre")
        win32gui.AppendMenu(menu, win32con.MF_POPUP, screens, "Ancrer sur l'ecran")
        win32gui.AppendMenu(menu, win32con.MF_POPUP, aligns, "Position")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_PAGE, "Ouvrir la page web")
        win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_REFRESH, "Actualiser maintenant")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_QUIT, "Quitter")

        x, y = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(self.hwnd)
        win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_BOTTOMALIGN,
                                x, y, 0, self.hwnd, None)
        win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)

    def remove(self):
        self.running = False
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Barre ancree
# ---------------------------------------------------------------------------

class Bar:
    H = 32        # hauteur de la ligne (barre des taches Windows : 48 px)
    BAR_W = 44    # longueur des jauges
    BAR_H = 10    # epaisseur des jauges
    GAP = 9       # espace autour des separateurs
    FONT = 9      # taille du texte
    MARGIN = 8    # retrait par rapport au bord de l'ecran

    def __init__(self, root):
        self.root = root
        self.visible = True
        self.monitor = 0
        self.align = "droite"
        self.width = 420
        self.dragging = False

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BG)
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=1,
                                highlightbackground="#3a3440",
                                width=self.width, height=self.H)
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self.start_drag)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.end_drag)
        self.canvas.bind("<Button-3>", lambda e: self.hide())
        self.load()
        root.update_idletasks()
        self.setup_styles()

    # --- styles Windows : hors Alt+Tab, ne prend jamais le focus ---
    def hwnd(self):
        wid = self.root.winfo_id()
        parent = win32gui.GetParent(wid)
        return parent or wid

    def setup_styles(self):
        try:
            h = self.hwnd()
            ex = win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(h, win32con.GWL_EXSTYLE,
                                   ex | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE)
        except Exception:  # noqa: BLE001
            pass

    # --- configuration ---
    def load(self):
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        self.monitor = int(cfg.get("monitor", 0))
        self.align = cfg.get("align", "droite")
        if self.align not in ALIGNS:
            self.align = "droite"
        if cfg.get("visible") is False:
            self.hide()

    def save(self):
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps({"monitor": self.monitor, "align": self.align,
                                          "visible": self.visible}), encoding="utf-8")
        except OSError:
            pass

    def set_monitor(self, index):
        self.monitor = max(0, index)
        self.anchor()
        self.save()

    def set_align(self, align):
        if align in ALIGNS:
            self.align = align
            self.anchor()
            self.save()

    # --- ancrage au-dessus de la barre des taches ---
    def anchor(self):
        if not self.visible or self.dragging:
            return
        mons = monitors()
        m = mons[min(self.monitor, len(mons) - 1)]
        left, _, right, bottom = m["work"]
        w, h = self.width + 2, self.H + 2
        if self.align == "gauche":
            x = left + self.MARGIN
        elif self.align == "centre":
            x = left + (right - left - w) // 2
        else:
            x = right - w - self.MARGIN
        y = bottom - h                      # bas de la zone de travail = haut de la barre
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        try:
            win32gui.SetWindowPos(self.hwnd(), win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                                  win32con.SWP_NOACTIVATE)
        except Exception:  # noqa: BLE001
            self.root.attributes("-topmost", True)

    # --- glisser vers un autre ecran ---
    def start_drag(self, event):
        self.dragging = True
        self.offset = (event.x_root - self.root.winfo_x(),
                       event.y_root - self.root.winfo_y())

    def on_drag(self, event):
        self.root.geometry("+%d+%d" % (event.x_root - self.offset[0],
                                       event.y_root - self.offset[1]))

    def end_drag(self, event):
        """Au lacher : on se raccroche a la barre des taches de l'ecran vise."""
        self.dragging = False
        x, y = event.x_root, event.y_root
        for i, m in enumerate(monitors()):
            left, top, right, bottom = m["full"]
            if left <= x < right and top <= y < bottom:
                self.monitor = i
                third = (right - left) / 3
                self.align = ("gauche" if x < left + third else
                              "centre" if x < left + 2 * third else "droite")
                break
        self.anchor()
        self.save()

    def toggle(self):
        self.hide() if self.visible else self.show()

    def show(self):
        self.visible = True
        self.root.deiconify()
        self.setup_styles()
        self.anchor()
        self.save()

    def hide(self):
        self.visible = False
        self.root.withdraw()
        self.save()

    # --- dessin : | Claude | 5h [jauge] % | 7j ... | ---
    def label(self, x, text, fill, bold=False):
        item = self.canvas.create_text(x, self.H / 2, text=text, anchor="w", fill=fill,
                                       font=("Segoe UI", self.FONT, "bold" if bold else "normal"))
        return self.canvas.bbox(item)[2]

    def separator(self, x):
        self.canvas.create_line(x, 7, x, self.H - 7, fill="#3a3440")
        return x + self.GAP

    def draw(self):
        st = snapshot()
        c = self.canvas
        c.delete("all")
        online = bool(st.get("ok"))
        mid, half = self.H / 2, self.BAR_H / 2

        x = self.label(self.GAP, "Claude", ACCENT, bold=True) + self.GAP
        limits = st.get("limits") or []

        if not limits:
            x = self.separator(x)
            x = self.label(x, st.get("msg", "?"), DIM) + self.GAP
        for l in limits:
            x = self.separator(x)
            x = self.label(x, l["short"] + " :", FG if online else DIM) + 5
            c.create_rectangle(x, mid - half, x + self.BAR_W, mid + half,
                               fill=BAR_BG, outline="")
            fill = int(self.BAR_W * min(100, max(0, l["p"])) / 100)
            if fill > 0:
                c.create_rectangle(x, mid - half, x + fill, mid + half,
                                   fill=color_for(l["p"]) if online else GRAY, outline="")
            x += self.BAR_W + 5
            mark = "*" if l.get("active") else ""
            x = self.label(x, "%d%%%s" % (l["p"], mark), FG if online else DIM,
                           bold=True) + self.GAP
        x = self.separator(x) - self.GAP + 2

        self.width = x
        c.configure(width=x, height=self.H)
        self.anchor()


# ---------------------------------------------------------------------------

def main():
    commands = queue.Queue()
    root = tk.Tk()
    root.title("Clawdmeter")
    tray = Tray(commands)
    bar = Bar(root)
    tray.start()

    threading.Thread(target=fetch_loop, args=(lambda: commands.put(("draw", None)),),
                     daemon=True).start()

    def pump():
        try:
            while True:
                cmd, arg = commands.get_nowait()
                if cmd == "draw":
                    bar.draw()
                    tray.dirty.set()
                elif cmd == "toggle":
                    bar.toggle()
                elif cmd == "screen":
                    bar.set_monitor(arg)
                elif cmd == "align":
                    bar.set_align(arg)
                elif cmd == "page":
                    os.startfile(SERVER)
                elif cmd == "refresh":
                    tray.dirty.set()
                elif cmd == "quit":
                    tray.remove()
                    root.destroy()
                    return
        except queue.Empty:
            pass
        root.after(200, pump)

    def keep_anchored():
        bar.anchor()
        root.after(ANCHOR_MS, keep_anchored)

    bar.draw()
    root.after(200, pump)
    root.after(ANCHOR_MS, keep_anchored)
    root.mainloop()


if __name__ == "__main__":
    main()
