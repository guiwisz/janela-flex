#!/usr/bin/env python3
"""Janela Flex — editor de layouts de zonas.

Modo Dividir:  clique numa zona para dividi-la (Shift = divisão horizontal).
               Arraste uma linha divisória para redimensionar as zonas vizinhas.
Modo Desenhar: arraste para criar zonas livres (podem se sobrepor);
               arraste uma zona para movê-la, ou pelo canto inferior direito para redimensioná-la.
Ctrl+clique seleciona várias zonas; M mescla; Delete exclui; Ctrl+Z desfaz.
"""
import copy
import json
import math
import os
import re
import shutil
import sys
import unicodedata

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "janela-flex")
CONFIG_FILE = os.path.join(CONFIG_DIR, "layouts.json")
EPS = 1e-4
MIN_FRAC = 0.03
HANDLE_PX = 7

PRESETS = [
    ("Uma zona (tela inteira)", lambda: [z(0, 0, 1, 1)]),
    ("Metades", lambda: cols([1, 1])),
    ("Terços", lambda: cols([1, 1, 1])),
    ("Quartos", lambda: cols([1, 1, 1, 1])),
    ("Ultrawide 25 / 50 / 25", lambda: cols([1, 2, 1])),
    ("Foco central 20 / 60 / 20", lambda: cols([1, 3, 1])),
    ("2/3 + 1/3", lambda: cols([2, 1])),
    ("Linhas (cima / baixo)", lambda: rows([1, 1])),
    ("Grade 2 × 2", lambda: grid(2, 2)),
    ("Grade 3 × 2", lambda: grid(3, 2)),
    ("Prioridade (principal + 2 laterais)",
     lambda: [z(0, 0, 0.625, 1), z(0.625, 0, 0.375, 0.5), z(0.625, 0.5, 0.375, 0.5)]),
    ("Ultrawide: laterais divididas",
     lambda: [z(0, 0, .25, .5), z(0, .5, .25, .5), z(.25, 0, .5, 1), z(.75, 0, .25, .5), z(.75, .5, .25, .5)]),
]


def z(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def cols(weights):
    tot, acc, out = sum(weights), 0, []
    for wt in weights:
        out.append(z(acc / tot, 0, wt / tot, 1))
        acc += wt
    return out


def rows(weights):
    return [z(c["y"], c["x"], c["h"], c["w"]) for c in cols(weights)]


def grid(nc, nr):
    return [z(c / nc, r / nr, 1 / nc, 1 / nr) for r in range(nr) for c in range(nc)]


def rnd(v):
    return round(v, 6)


def clean(zone):
    return {k: rnd(zone[k]) for k in ("x", "y", "w", "h")}


def slug(name, existing):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "layout"
    base, i = s, 2
    while s in existing:
        s = f"{base}-{i}"
        i += 1
    return s


def screen_size():
    disp = Gdk.Display.get_default()
    mon = disp.get_primary_monitor() or disp.get_monitor(0)
    g = mon.get_geometry()
    sc = mon.get_scale_factor()
    return g.width * sc, g.height * sc


# --------------------------------------------------------------------------
class Canvas(Gtk.DrawingArea):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
                        | Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_press)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("leave-notify-event", lambda *a: self.set_hover(None))
        self.hover = None        # (fx, fy, shift)
        self.drag = None         # dict descrevendo a operação em curso
        self.selection = set()

    # ---- coordenadas ----
    def frame(self):
        aw, ah = self.get_allocated_width(), self.get_allocated_height()
        sw, sh = self.app.screen
        pad = 24
        scale = min((aw - 2 * pad) / sw, (ah - 2 * pad) / sh)
        w, h = sw * scale, sh * scale
        return (aw - w) / 2, (ah - h) / 2, w, h

    def to_frac(self, x, y):
        ox, oy, w, h = self.frame()
        return (x - ox) / w, (y - oy) / h

    def snap(self, v, axis):
        n = self.app.snap_steps()
        if not n:
            return min(1, max(0, v))
        return min(1, max(0, round(v * n) / n))

    @property
    def zones(self):
        lay = self.app.current()
        return lay["zones"] if lay else []

    def zone_at(self, fx, fy):
        best, area = None, 9
        for i, zn in enumerate(self.zones):
            if zn["x"] <= fx < zn["x"] + zn["w"] and zn["y"] <= fy < zn["y"] + zn["h"]:
                a = zn["w"] * zn["h"]
                if a < area:
                    best, area = i, a
        return best

    def divider_at(self, px, py):
        """Retorna ('v', pos, (y1, y2)) ou ('h', pos, (x1, x2)) se o ponteiro está sobre uma divisória interna."""
        ox, oy, w, h = self.frame()
        fx, fy = self.to_frac(px, py)
        tx, ty = HANDLE_PX / w, HANDLE_PX / h
        for zn in self.zones:
            for edge in (zn["x"], zn["x"] + zn["w"]):
                if EPS < edge < 1 - EPS and abs(fx - edge) < tx and zn["y"] - EPS <= fy <= zn["y"] + zn["h"] + EPS:
                    seg = self.segment("v", edge, fy)
                    if seg:
                        return ("v", edge, seg)
            for edge in (zn["y"], zn["y"] + zn["h"]):
                if EPS < edge < 1 - EPS and abs(fy - edge) < ty and zn["x"] - EPS <= fx <= zn["x"] + zn["w"] + EPS:
                    seg = self.segment("h", edge, fx)
                    if seg:
                        return ("h", edge, seg)
        return None

    def segment(self, orient, pos, at):
        """Encontra o trecho contínuo da divisória em `pos` que passa por `at`
        e as zonas de cada lado (before = termina em pos, after = começa em pos)."""
        if orient == "v":
            lo, ln, so, sl = "x", "w", "y", "h"
        else:
            lo, ln, so, sl = "y", "h", "x", "w"
        lo_s, hi_s = at, at
        members = set()
        changed = True
        while changed:
            changed = False
            for i, zn in enumerate(self.zones):
                if i in members:
                    continue
                touches = abs(zn[lo] - pos) < EPS or abs(zn[lo] + zn[ln] - pos) < EPS
                if not touches:
                    continue
                a, b = zn[so], zn[so] + zn[sl]
                if a <= hi_s + EPS and b >= lo_s - EPS:
                    members.add(i)
                    lo_s, hi_s = min(lo_s, a), max(hi_s, b)
                    changed = True
        before = [i for i in members if abs(self.zones[i][lo] + self.zones[i][ln] - pos) < EPS]
        after = [i for i in members if abs(self.zones[i][lo] - pos) < EPS]
        if not before or not after:
            return None
        return {"before": before, "after": after, "range": (lo_s, hi_s)}

    # ---- desenho ----
    def on_draw(self, widget, cr):
        ox, oy, w, h = self.frame()
        sw, sh = self.app.screen
        # fundo do "monitor"
        cr.set_source_rgb(0.13, 0.14, 0.16)
        cr.rectangle(0, 0, self.get_allocated_width(), self.get_allocated_height())
        cr.fill()
        cr.set_source_rgb(0.07, 0.08, 0.09)
        rounded(cr, ox - 6, oy - 6, w + 12, h + 12, 10)
        cr.fill()
        cr.set_source_rgb(0.2, 0.22, 0.25)
        cr.rectangle(ox, oy, w, h)
        cr.fill()

        # grade de encaixe
        n = self.app.snap_steps()
        if n and n <= 48:
            cr.set_source_rgba(1, 1, 1, 0.05)
            cr.set_line_width(1)
            for i in range(1, n):
                x = ox + w * i / n
                y = oy + h * i / n
                cr.move_to(x, oy)
                cr.line_to(x, oy + h)
                cr.move_to(ox, y)
                cr.line_to(ox + w, y)
            cr.stroke()

        hover_zone = None
        if self.hover and not self.drag:
            hover_zone = self.zone_at(self.hover[0], self.hover[1])

        for i, zn in enumerate(self.zones):
            x, y = ox + zn["x"] * w, oy + zn["y"] * h
            zw, zh = zn["w"] * w, zn["h"] * h
            sel = i in self.selection
            if sel:
                cr.set_source_rgba(0.21, 0.52, 0.89, 0.55)
            elif i == hover_zone:
                cr.set_source_rgba(0.35, 0.38, 0.44, 0.95)
            else:
                cr.set_source_rgba(0.28, 0.30, 0.35, 0.92)
            rounded(cr, x + 3, y + 3, zw - 6, zh - 6, 8)
            cr.fill_preserve()
            if sel:
                cr.set_source_rgba(0.47, 0.71, 1, 1)
            else:
                cr.set_source_rgba(1, 1, 1, 0.25)
            cr.set_line_width(2)
            cr.stroke()

            # número + tamanho
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.select_font_face("Sans", 0, 1)
            fs = max(12, min(40, zh / 4, zw / 4))
            cr.set_font_size(fs)
            txt = str(i + 1)
            ext = cr.text_extents(txt)
            cr.move_to(x + zw / 2 - ext.width / 2 - ext.x_bearing, y + zh / 2 - fs * 0.15)
            cr.show_text(txt)
            info = f"{round(zn['w'] * sw)} × {round(zn['h'] * sh)}"
            pct = f"{zn['w'] * 100:.1f}% × {zn['h'] * 100:.1f}%"
            cr.select_font_face("Sans", 0, 0)
            for k, line in enumerate((info, pct)):
                cr.set_font_size(max(9, min(13, zw / 12)))
                e = cr.text_extents(line)
                if e.width < zw - 12 and zh > 70:
                    cr.set_source_rgba(1, 1, 1, 0.6)
                    cr.move_to(x + zw / 2 - e.width / 2, y + zh / 2 + fs * 0.45 + k * 16)
                    cr.show_text(line)
            if self.app.mode == "draw":
                cr.set_source_rgba(1, 1, 1, 0.35)
                cr.move_to(x + zw - 6, y + zh - 18)
                cr.line_to(x + zw - 6, y + zh - 6)
                cr.line_to(x + zw - 18, y + zh - 6)
                cr.close_path()
                cr.fill()

        # pré-visualização da divisão / divisória
        if self.hover and not self.drag:
            fx, fy, shift = self.hover
            px, py = ox + fx * w, oy + fy * h
            div = self.divider_at(px, py) if self.app.mode == "split" else None
            if div:
                self.draw_divider(cr, div, ox, oy, w, h, (0.47, 0.71, 1, 1))
            elif self.app.mode == "split" and hover_zone is not None:
                zn = self.zones[hover_zone]
                cr.set_source_rgba(0.47, 0.71, 1, 0.9)
                cr.set_line_width(2)
                cr.set_dash([6, 4])
                if shift:
                    yy = oy + self.snap(fy, "y") * h
                    cr.move_to(ox + zn["x"] * w + 4, yy)
                    cr.line_to(ox + (zn["x"] + zn["w"]) * w - 4, yy)
                else:
                    xx = ox + self.snap(fx, "x") * w
                    cr.move_to(xx, oy + zn["y"] * h + 4)
                    cr.line_to(xx, oy + (zn["y"] + zn["h"]) * h - 4)
                cr.stroke()
                cr.set_dash([])
        if self.drag and self.drag["kind"] == "divider":
            d = self.drag
            self.draw_divider(cr, (d["orient"], d["pos"], d["seg"]), ox, oy, w, h, (0.47, 0.71, 1, 1))
        if self.drag and self.drag["kind"] == "draw" and self.drag.get("rect"):
            rx, ry, rw, rh = self.drag["rect"]
            cr.set_source_rgba(0.21, 0.52, 0.89, 0.4)
            cr.rectangle(ox + rx * w, oy + ry * h, rw * w, rh * h)
            cr.fill_preserve()
            cr.set_source_rgba(0.47, 0.71, 1, 1)
            cr.set_line_width(2)
            cr.stroke()

    def draw_divider(self, cr, div, ox, oy, w, h, color):
        orient, pos, seg = div
        a, b = seg["range"]
        cr.set_source_rgba(*color)
        cr.set_line_width(4)
        if orient == "v":
            cr.move_to(ox + pos * w, oy + a * h)
            cr.line_to(ox + pos * w, oy + b * h)
        else:
            cr.move_to(ox + a * w, oy + pos * h)
            cr.line_to(ox + b * w, oy + pos * h)
        cr.stroke()

    # ---- eventos ----
    def set_hover(self, hv):
        self.hover = hv
        self.queue_draw()

    def update_cursor(self, px, py):
        win = self.get_window()
        if not win:
            return
        name = "default"
        if self.app.mode == "split":
            div = self.divider_at(px, py)
            if div:
                name = "col-resize" if div[0] == "v" else "row-resize"
            elif self.zone_at(*self.to_frac(px, py)) is not None:
                name = "crosshair"
        else:
            fx, fy = self.to_frac(px, py)
            i = self.zone_at(fx, fy)
            if i is not None:
                name = "se-resize" if self.near_corner(i, px, py) else "move"
            else:
                name = "crosshair"
        win.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), name))

    def near_corner(self, i, px, py):
        ox, oy, w, h = self.frame()
        zn = self.zones[i]
        cx, cy = ox + (zn["x"] + zn["w"]) * w, oy + (zn["y"] + zn["h"]) * h
        return abs(px - cx) < 18 and abs(py - cy) < 18

    def on_motion(self, widget, ev):
        fx, fy = self.to_frac(ev.x, ev.y)
        shift = bool(ev.state & Gdk.ModifierType.SHIFT_MASK)
        d = self.drag
        if d:
            if d["kind"] == "divider":
                self.move_divider(d, fx if d["orient"] == "v" else fy)
            elif d["kind"] == "draw":
                x1, y1 = d["start"]
                x2, y2 = self.snap(fx, "x"), self.snap(fy, "y")
                d["rect"] = (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
            elif d["kind"] == "move":
                zn = self.zones[d["index"]]
                ow, oh = zn["w"], zn["h"]
                nx = self.snap(d["orig"]["x"] + fx - d["start"][0], "x")
                ny = self.snap(d["orig"]["y"] + fy - d["start"][1], "y")
                zn["x"] = min(max(0, nx), 1 - ow)
                zn["y"] = min(max(0, ny), 1 - oh)
                d["moved"] = True
            elif d["kind"] == "resize":
                zn = self.zones[d["index"]]
                zn["w"] = max(MIN_FRAC, min(1 - zn["x"], self.snap(fx, "x") - zn["x"]))
                zn["h"] = max(MIN_FRAC, min(1 - zn["y"], self.snap(fy, "y") - zn["y"]))
                d["moved"] = True
            self.queue_draw()
            return
        self.update_cursor(ev.x, ev.y)
        self.set_hover((fx, fy, shift))

    def move_divider(self, d, v):
        v = self.snap(v, d["orient"])
        lo, ln = ("x", "w") if d["orient"] == "v" else ("y", "h")
        zs = self.zones
        min_v = max(d["orig"][i][lo] for i in d["seg"]["before"]) + MIN_FRAC
        max_v = min(d["orig"][i][lo] + d["orig"][i][ln] for i in d["seg"]["after"]) - MIN_FRAC
        v = min(max(v, min_v), max_v)
        for i in d["seg"]["before"]:
            zs[i][ln] = v - d["orig"][i][lo]
        for i in d["seg"]["after"]:
            end = d["orig"][i][lo] + d["orig"][i][ln]
            zs[i][lo] = v
            zs[i][ln] = end - v
        d["pos"] = v
        d["moved"] = True

    def on_press(self, widget, ev):
        self.grab_focus()
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return
        fx, fy = self.to_frac(ev.x, ev.y)
        idx = self.zone_at(fx, fy)
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(ev.state & Gdk.ModifierType.SHIFT_MASK)

        if ev.button == 3:
            if idx is not None and idx not in self.selection:
                self.selection = {idx}
            self.queue_draw()
            self.app.context_menu(ev, idx)
            return

        if ev.button != 1 or not self.app.current():
            return

        if ctrl:
            if idx is not None:
                self.selection ^= {idx}
                self.queue_draw()
            return

        if self.app.mode == "split":
            div = self.divider_at(ev.x, ev.y)
            if div:
                self.app.push_undo()
                self.drag = {"kind": "divider", "orient": div[0], "pos": div[1], "seg": div[2],
                             "orig": copy.deepcopy(self.zones), "moved": False}
                return
            if idx is not None:
                self.drag = {"kind": "click-split", "index": idx, "shift": shift, "at": (fx, fy)}
            else:
                self.drag = {"kind": "draw", "start": (self.snap(fx, "x"), self.snap(fy, "y")), "rect": None}
        else:
            if idx is not None:
                self.selection = {idx}
                self.app.push_undo()
                kind = "resize" if self.near_corner(idx, ev.x, ev.y) else "move"
                self.drag = {"kind": kind, "index": idx, "start": (fx, fy),
                             "orig": dict(self.zones[idx]), "moved": False}
            else:
                self.selection = set()
                self.drag = {"kind": "draw", "start": (self.snap(fx, "x"), self.snap(fy, "y")), "rect": None}
        self.queue_draw()

    def on_release(self, widget, ev):
        d, self.drag = self.drag, None
        if not d or ev.button != 1:
            return
        if d["kind"] == "click-split":
            fx, fy = d["at"]
            self.split(d["index"], "h" if d["shift"] else "v", fy if d["shift"] else fx)
        elif d["kind"] == "draw" and d.get("rect"):
            rx, ry, rw, rh = d["rect"]
            if rw >= MIN_FRAC and rh >= MIN_FRAC:
                self.app.push_undo()
                self.zones.append(z(rx, ry, rw, rh))
                self.selection = {len(self.zones) - 1}
                self.app.changed()
        elif d["kind"] in ("divider", "move", "resize"):
            if d.get("moved"):
                self.app.changed()
            else:
                self.app.drop_undo()
        self.queue_draw()

    # ---- operações ----
    def split(self, idx, orient, at=None, parts=2):
        zn = self.zones[idx]
        lo, ln = ("x", "w") if orient == "v" else ("y", "h")
        self.app.push_undo()
        if at is not None and parts == 2:
            cut = self.snap(at, lo)
            if not (zn[lo] + MIN_FRAC <= cut <= zn[lo] + zn[ln] - MIN_FRAC):
                cut = zn[lo] + zn[ln] / 2
            pieces = [(zn[lo], cut - zn[lo]), (cut, zn[lo] + zn[ln] - cut)]
        else:
            step = zn[ln] / parts
            pieces = [(zn[lo] + k * step, step) for k in range(parts)]
        new = []
        for start, length in pieces:
            nz = dict(zn)
            nz[lo], nz[ln] = start, length
            new.append(nz)
        self.zones[idx:idx + 1] = new
        self.selection = set()
        self.app.changed()

    def merge_selection(self):
        sel = sorted(self.selection)
        if len(sel) < 2:
            self.app.status("Selecione 2 ou mais zonas com Ctrl+clique para mesclar.")
            return
        zs = self.zones
        x1 = min(zs[i]["x"] for i in sel)
        y1 = min(zs[i]["y"] for i in sel)
        x2 = max(zs[i]["x"] + zs[i]["w"] for i in sel)
        y2 = max(zs[i]["y"] + zs[i]["h"] for i in sel)
        # zonas totalmente dentro do retângulo são absorvidas; as que cruzam a borda impedem a mesclagem
        inside, crossing = [], []
        for i, zn in enumerate(zs):
            inter = zn["x"] < x2 - EPS and zn["x"] + zn["w"] > x1 + EPS and zn["y"] < y2 - EPS and zn["y"] + zn["h"] > y1 + EPS
            if not inter:
                continue
            full = zn["x"] >= x1 - EPS and zn["y"] >= y1 - EPS and zn["x"] + zn["w"] <= x2 + EPS and zn["y"] + zn["h"] <= y2 + EPS
            (inside if full else crossing).append(i)
        if crossing and self.app.mode == "split":
            self.selection = set(sel) | set(crossing)
            self.queue_draw()
            self.app.status("A mesclagem cortaria outras zonas (em destaque). Inclua-as na seleção ou use o modo Desenhar.")
            return
        self.app.push_undo()
        absorbed = set(inside) | set(crossing)
        pos = sum(1 for i in range(min(absorbed)) if i not in absorbed)
        keep = [zn for i, zn in enumerate(zs) if i not in absorbed]
        keep.insert(pos, z(x1, y1, x2 - x1, y2 - y1))
        zs[:] = keep
        self.selection = {pos}
        self.app.changed()

    def delete_selection(self):
        if not self.selection:
            return
        self.app.push_undo()
        zs = self.zones
        zs[:] = [zn for i, zn in enumerate(zs) if i not in self.selection]
        self.selection = set()
        self.app.changed()

    def move_order(self, delta):
        if len(self.selection) != 1:
            return
        i = next(iter(self.selection))
        j = i + delta
        if not 0 <= j < len(self.zones):
            return
        self.app.push_undo()
        zs = self.zones
        zs[i], zs[j] = zs[j], zs[i]
        self.selection = {j}
        self.app.changed()


def rounded(cr, x, y, w, h, r):
    r = max(0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


# --------------------------------------------------------------------------
class Editor(Gtk.Window):
    SNAP_OPTIONS = [("Livre", 0), ("1/4", 4), ("1/6", 6), ("1/8", 8), ("1/12", 12),
                    ("1/16", 16), ("1/24", 24), ("1/48", 48), ("1%", 100)]

    def __init__(self):
        super().__init__(title="Janela Flex — Editor de layouts")
        self.set_default_size(1280, 760)
        self.screen = screen_size()
        self.mode = "split"
        self.undo_stack = []
        self.save_id = 0
        self.loading = False
        self._touched = set()
        self.load()

        hb = Gtk.HeaderBar(show_close_button=True, title="Janela Flex", subtitle=f"Monitor {self.screen[0]}×{self.screen[1]}")
        self.set_titlebar(hb)

        mode_box = Gtk.Box(spacing=0)
        mode_box.get_style_context().add_class("linked")
        self.btn_split = Gtk.RadioButton.new_with_label(None, "Dividir")
        self.btn_draw = Gtk.RadioButton.new_with_label_from_widget(self.btn_split, "Desenhar")
        for b in (self.btn_split, self.btn_draw):
            b.set_mode(False)
            b.connect("toggled", self.on_mode)
            mode_box.pack_start(b, False, False, 0)
        self.btn_split.set_tooltip_text("Clique numa zona para dividir (Shift = horizontal). Arraste as linhas para redimensionar.")
        self.btn_draw.set_tooltip_text("Arraste para criar zonas livres; arraste uma zona para mover; canto inferior direito redimensiona.")
        hb.pack_start(mode_box)

        hb.pack_start(Gtk.Label(label="  Encaixe:"))
        self.snap_combo = Gtk.ComboBoxText()
        for label, _ in self.SNAP_OPTIONS:
            self.snap_combo.append_text(label)
        self.snap_combo.set_active(6)
        self.snap_combo.connect("changed", lambda *a: self.canvas.queue_draw())
        hb.pack_start(self.snap_combo)

        self.btn_activate = Gtk.Button(label="Usar este layout")
        self.btn_activate.get_style_context().add_class("suggested-action")
        self.btn_activate.connect("clicked", self.on_activate)
        hb.pack_end(self.btn_activate)

        undo = Gtk.Button.new_from_icon_name("edit-undo-symbolic", Gtk.IconSize.BUTTON)
        undo.set_tooltip_text("Desfazer (Ctrl+Z)")
        undo.connect("clicked", lambda *a: self.undo())
        hb.pack_end(undo)

        paned = Gtk.Paned()
        self.add(paned)

        # barra lateral
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        side.set_size_request(270, -1)
        paned.pack1(side, False, False)

        side.pack_start(self.bold("Layouts"), False, False, 0)
        self.listbox = Gtk.ListBox()
        self.listbox.connect("row-selected", self.on_row_selected)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.add(self.listbox)
        side.pack_start(sc, True, True, 0)

        btns = Gtk.Box(spacing=4)
        new_btn = Gtk.MenuButton(label="Novo")
        menu = Gtk.Menu()
        for label, fn in PRESETS:
            it = Gtk.MenuItem(label=label)
            it.connect("activate", lambda _w, l=label, f=fn: self.new_layout(l, f()))
            menu.append(it)
        menu.show_all()
        new_btn.set_popup(menu)
        btns.pack_start(new_btn, True, True, 0)
        for label, cb in (("Duplicar", self.duplicate), ("Excluir", self.delete_layout)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda *a, c=cb: c())
            btns.pack_start(b, True, True, 0)
        side.pack_start(btns, False, False, 0)

        side.pack_start(self.bold("Nome"), False, False, 6)
        self.name_entry = Gtk.Entry()
        self.name_entry.connect("changed", self.on_rename)
        side.pack_start(self.name_entry, False, False, 0)

        side.pack_start(self.bold("Zona selecionada"), False, False, 6)
        grid_ = Gtk.Grid(column_spacing=6, row_spacing=4)
        self.spins = {}
        for r, (key, label) in enumerate((("x", "X %"), ("y", "Y %"), ("w", "Largura %"), ("h", "Altura %"))):
            grid_.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
            sp = Gtk.SpinButton.new_with_range(0, 100, 0.5)
            sp.set_digits(2)
            sp.connect("value-changed", self.on_spin, key)
            grid_.attach(sp, 1, r, 1, 1)
            self.spins[key] = sp
        side.pack_start(grid_, False, False, 0)

        order = Gtk.Box(spacing=4)
        for label, delta in (("◀ Ordem", -1), ("Ordem ▶", 1)):
            b = Gtk.Button(label=label)
            b.set_tooltip_text("Muda o número da zona (usado em Super+Alt+N e na ordem de Super+Alt+←/→)")
            b.connect("clicked", lambda *a, d=delta: self.canvas.move_order(d))
            order.pack_start(b, True, True, 0)
        side.pack_start(order, False, False, 0)

        help_ = Gtk.Label(xalign=0, wrap=True)
        help_.set_markup(
            "<small><b>Dividir:</b> clique numa zona divide na vertical; <b>Shift</b>+clique na horizontal. "
            "Arraste as linhas azuis para redimensionar.\n"
            "<b>Desenhar:</b> arraste no vazio para criar zonas (podem se sobrepor).\n"
            "<b>Ctrl</b>+clique seleciona várias · <b>M</b> mescla · <b>Del</b> exclui · "
            "<b>Ctrl+Z</b> desfaz · botão direito abre o menu.\n\n"
            "Ao arrastar janelas: encoste na borda da tela (ou segure <b>Shift</b>); "
            "botão direito ou Ctrl junta várias zonas.</small>")
        side.pack_start(help_, False, False, 6)

        self.status_label = Gtk.Label(xalign=0, wrap=True)
        self.status_label.get_style_context().add_class("dim-label")
        side.pack_start(self.status_label, False, False, 0)

        self.canvas = Canvas(self)
        paned.pack2(self.canvas, True, False)
        self.canvas.connect("button-release-event", lambda *a: self.refresh_spins())

        self.connect("key-press-event", self.on_key)
        self.refresh_list()
        self.show_all()

    # ---- util ----
    def bold(self, text):
        lbl = Gtk.Label(xalign=0)
        lbl.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>")
        return lbl

    def status(self, text):
        self.status_label.set_text(text)

    def snap_steps(self):
        i = self.snap_combo.get_active()
        return self.SNAP_OPTIONS[i][1] if i >= 0 else 0

    def current(self):
        if 0 <= self.cur < len(self.config["layouts"]):
            return self.config["layouts"][self.cur]
        return None

    # ---- arquivo ----
    def read_file(self):
        if not os.path.exists(CONFIG_FILE):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            shutil.copy(os.path.join(HERE, "defaults.json"), CONFIG_FILE)
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)

    def load(self):
        try:
            self.config = self.read_file()
        except Exception as e:  # arquivo corrompido: começa dos padrões
            print("erro ao ler config:", e, file=sys.stderr)
            with open(os.path.join(HERE, "defaults.json"), encoding="utf-8") as f:
                self.config = json.load(f)
        self.config.setdefault("activeByMonitor", {})
        self.config.setdefault("layouts", [])
        ids = [l["id"] for l in self.config["layouts"]]
        act = self.config.get("active")
        self.cur = ids.index(act) if act in ids else 0

    def save_now(self):
        self.save_id = 0
        try:
            disk = self.read_file()   # preserva o layout ativo trocado por atalho
            if "active" not in self._touched:
                self.config["active"] = disk.get("active", self.config.get("active"))
                self.config["activeByMonitor"] = disk.get("activeByMonitor", {})
        except Exception:
            pass
        self._touched = set()
        out = dict(self.config)
        out["layouts"] = [{**l, "zones": [clean(zn) for zn in l["zones"]]} for l in self.config["layouts"]]
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
        self.status("Salvo ✓ (a extensão já usa a versão nova)")
        return False

    def changed(self, touched=None):
        if touched:
            self._touched.add(touched)
        self.canvas.queue_draw()
        self.refresh_spins()
        self.update_row_labels()
        if self.save_id:
            GLib.source_remove(self.save_id)
        self.save_id = GLib.timeout_add(400, self.save_now)
        self.status("Salvando…")

    # ---- desfazer ----
    def push_undo(self):
        lay = self.current()
        if lay:
            self.undo_stack.append((self.cur, copy.deepcopy(lay["zones"])))
            del self.undo_stack[:-100]

    def drop_undo(self):
        if self.undo_stack:
            self.undo_stack.pop()

    def undo(self):
        if not self.undo_stack:
            self.status("Nada para desfazer.")
            return
        idx, zones = self.undo_stack.pop()
        if idx < len(self.config["layouts"]):
            self.config["layouts"][idx]["zones"] = zones
            if idx != self.cur:
                self.listbox.select_row(self.listbox.get_row_at_index(idx))
            self.canvas.selection = set()
            self.changed()

    # ---- lista de layouts ----
    def refresh_list(self):
        self.loading = True
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        for l in self.config["layouts"]:
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(xalign=0, margin=6)
            row.add(lbl)
            self.listbox.add(row)
        self.listbox.show_all()
        self.update_row_labels()
        self.loading = False
        row = self.listbox.get_row_at_index(self.cur)
        if row:
            self.listbox.select_row(row)

    def update_row_labels(self):
        act = self.config.get("active")
        for i, l in enumerate(self.config["layouts"]):
            row = self.listbox.get_row_at_index(i)
            if row:
                star = "★ " if l["id"] == act else "    "
                row.get_child().set_text(f"{star}{l['name']}  ({len(l['zones'])})")

    def on_row_selected(self, lb, row):
        if self.loading or row is None:
            return
        self.cur = row.get_index()
        self.canvas.selection = set()
        lay = self.current()
        self.loading = True
        self.name_entry.set_text(lay["name"] if lay else "")
        self.loading = False
        self.refresh_spins()
        self.canvas.queue_draw()

    def new_layout(self, name, zones):
        ids = {l["id"] for l in self.config["layouts"]}
        self.config["layouts"].append({"id": slug(name, ids), "name": name, "zones": zones})
        self.cur = len(self.config["layouts"]) - 1
        self.refresh_list()
        self.changed()
        self.name_entry.grab_focus()

    def duplicate(self):
        lay = self.current()
        if lay:
            self.new_layout(lay["name"] + " (cópia)", copy.deepcopy(lay["zones"]))

    def delete_layout(self):
        lay = self.current()
        if not lay:
            return
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL, text=f"Excluir o layout “{lay['name']}”?")
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        del self.config["layouts"][self.cur]
        self.undo_stack = []
        if self.config.get("active") == lay["id"] and self.config["layouts"]:
            self.config["active"] = self.config["layouts"][0]["id"]
            self._touched.add("active")
        self.cur = max(0, self.cur - 1)
        self.refresh_list()
        self.changed()

    def on_rename(self, entry):
        lay = self.current()
        if lay and not self.loading:
            lay["name"] = entry.get_text() or "Sem nome"
            self.changed()

    def on_activate(self, *a):
        lay = self.current()
        if lay:
            self.config["active"] = lay["id"]
            self.config["activeByMonitor"] = {}
            self.changed("active")
            self.status(f"“{lay['name']}” agora é o layout ativo.")

    def on_mode(self, btn):
        if btn.get_active():
            self.mode = "split" if btn is self.btn_split else "draw"
            self.canvas.queue_draw()

    # ---- spins da zona ----
    def refresh_spins(self):
        sel = self.canvas.selection
        lay = self.current()
        one = lay and len(sel) == 1 and next(iter(sel)) < len(lay["zones"])
        self.loading = True
        for k, sp in self.spins.items():
            sp.set_sensitive(bool(one))
            sp.set_value(lay["zones"][next(iter(sel))][k] * 100 if one else 0)
        self.loading = False

    def on_spin(self, sp, key):
        if self.loading:
            return
        sel = self.canvas.selection
        lay = self.current()
        if not lay or len(sel) != 1:
            return
        zn = lay["zones"][next(iter(sel))]
        self.push_undo()
        zn[key] = sp.get_value() / 100
        zn["w"] = max(MIN_FRAC, min(zn["w"], 1 - zn["x"]))
        zn["h"] = max(MIN_FRAC, min(zn["h"], 1 - zn["y"]))
        self.changed()

    # ---- menu e teclado ----
    def context_menu(self, ev, idx):
        menu = Gtk.Menu()

        def item(label, cb, sensitive=True):
            it = Gtk.MenuItem(label=label)
            it.set_sensitive(sensitive)
            it.connect("activate", lambda *a: cb())
            menu.append(it)

        c = self.canvas
        has = idx is not None
        item("Dividir na vertical (ao meio)", lambda: c.split(idx, "v"), has)
        item("Dividir na horizontal (ao meio)", lambda: c.split(idx, "h"), has)
        item("Dividir em 3 colunas", lambda: c.split(idx, "v", parts=3), has)
        item("Dividir em 3 linhas", lambda: c.split(idx, "h", parts=3), has)
        menu.append(Gtk.SeparatorMenuItem())
        item("Mesclar selecionadas (M)", c.merge_selection, len(c.selection) >= 2)
        item("Excluir (Del)", c.delete_selection, bool(c.selection))
        menu.show_all()
        menu.popup_at_pointer(ev)

    def on_key(self, w, ev):
        if self.name_entry.has_focus() or any(s.has_focus() for s in self.spins.values()):
            return False
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        k = Gdk.keyval_name(ev.keyval)
        if ctrl and k in ("z", "Z"):
            self.undo()
        elif k in ("m", "M"):
            self.canvas.merge_selection()
        elif k in ("Delete", "BackSpace"):
            self.canvas.delete_selection()
        elif k == "Escape":
            self.canvas.selection = set()
            self.canvas.queue_draw()
            self.refresh_spins()
        elif ctrl and k in ("s", "S"):
            self.save_now()
        else:
            return False
        return True


def main():
    win = Editor()
    win.connect("destroy", lambda *a: (win.save_id and win.save_now(), Gtk.main_quit()))
    Gtk.main()


if __name__ == "__main__":
    main()
