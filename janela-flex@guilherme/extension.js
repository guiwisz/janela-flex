// Janela Flex — zonas de janelas personalizadas para o Cinnamon.
const GLib = imports.gi.GLib;
const Gio = imports.gi.Gio;
const St = imports.gi.St;
const Meta = imports.gi.Meta;
const Clutter = imports.gi.Clutter;
const Main = imports.ui.main;
const Settings = imports.ui.settings;
const Util = imports.misc.util;

const UUID = "janela-flex@guilherme";
const CONFIG_DIR = GLib.build_filenamev([GLib.get_user_config_dir(), "janela-flex"]);
const CONFIG_FILE = GLib.build_filenamev([CONFIG_DIR, "layouts.json"]);
const POLL_MS = 25;
const EDGE_PX = 12;   // distância da borda que ativa o modo "edge"

let extension = null;

// O Muffin emite (display, window, op) ou (display, screen, window, op) conforme a versão.
function grabArgs(args) {
    const win = args.find(a => a instanceof Meta.Window) || null;
    const op = args.find(a => typeof a === "number");
    return [win, op];
}

function log_(msg) { global.log(`[janela-flex] ${msg}`); }

class JanelaFlex {
    constructor(meta) {
        this.meta = meta;
        this.config = null;
        this.overlay = null;
        this.zoneActors = [];
        this.dragWindow = null;
        this.pollId = 0;
        this.monitorIndex = -1;
        this.anchorZone = -1;
        this.selected = [];      // índices das zonas destacadas
        this.selectedRect = null;
        this.savedEdgeTiling = null;
        this.osd = null;
        this.osdTimeout = 0;

        this.settings = new Settings.ExtensionSettings(this, UUID);
        this.settings.bind("activation", "activation");
        this.settings.bind("gap", "gap");
        this.settings.bind("disable-native-tiling", "disableNativeTiling");
        this.settings.bind("zone-number-keys", "zoneNumberKeys", () => this._bindKeys());
        for (const k of ["key-next", "key-prev", "key-cycle-layout", "key-editor"])
            this.settings.bind(k, k.replace(/-(\w)/g, (_, c) => c.toUpperCase()), () => this._bindKeys());

        this.muffinSettings = new Gio.Settings({ schema_id: "org.cinnamon.muffin" });

        this._ensureConfig();
        this._loadConfig();
        this._watchConfig();

        this.grabBeginId = global.display.connect("grab-op-begin", (...args) => this._onGrabBegin(...grabArgs(args)));
        this.grabEndId = global.display.connect("grab-op-end", (...args) => this._onGrabEnd(...grabArgs(args)));
        this._bindKeys();
    }

    destroy() {
        global.display.disconnect(this.grabBeginId);
        global.display.disconnect(this.grabEndId);
        this._stopPolling();
        this._hideOverlay();
        this._restoreNative();
        this._unbindKeys();
        if (this.monitor) this.monitor.cancel();
        if (this.reloadId) GLib.source_remove(this.reloadId);
        this._hideOsd();
        this.settings.finalize();
    }

    // ---------- Configuração ----------
    _ensureConfig() {
        if (GLib.file_test(CONFIG_FILE, GLib.FileTest.EXISTS)) return;
        GLib.mkdir_with_parents(CONFIG_DIR, 0o755);
        const src = Gio.File.new_for_path(GLib.build_filenamev([this.meta.path, "defaults.json"]));
        src.copy(Gio.File.new_for_path(CONFIG_FILE), Gio.FileCopyFlags.NONE, null, null);
    }

    _loadConfig() {
        try {
            const [ok, bytes] = GLib.file_get_contents(CONFIG_FILE);
            if (!ok) return;
            const cfg = JSON.parse(new TextDecoder().decode(bytes));
            if (!Array.isArray(cfg.layouts)) throw new Error("layouts ausente");
            cfg.activeByMonitor = cfg.activeByMonitor || {};
            this.config = cfg;
        } catch (e) {
            log_(`erro ao ler ${CONFIG_FILE}: ${e}`);
            if (!this.config) this.config = { layouts: [], activeByMonitor: {} };
        }
    }

    _saveConfig() {
        try {
            const data = JSON.stringify(this.config, null, 2);
            GLib.file_set_contents(CONFIG_FILE, data);
        } catch (e) { log_(`erro ao salvar: ${e}`); }
    }

    _watchConfig() {
        const dir = Gio.File.new_for_path(CONFIG_DIR);
        this.monitor = dir.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
        this.monitor.connect("changed", (m, file, other) => {
            const names = [file, other].filter(f => f).map(f => f.get_basename());
            if (!names.includes("layouts.json")) return;
            if (this.reloadId) GLib.source_remove(this.reloadId);
            this.reloadId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 150, () => {
                this.reloadId = 0;
                this._loadConfig();
                return GLib.SOURCE_REMOVE;
            });
        });
    }

    _layoutFor(monitorIndex) {
        const layouts = this.config.layouts;
        if (!layouts.length) return null;
        const id = this.config.activeByMonitor[String(monitorIndex)] || this.config.active;
        return layouts.find(l => l.id === id) || layouts[0];
    }

    // ---------- Geometria ----------
    _workArea(monitorIndex) {
        const ws = global.workspace_manager.get_active_workspace();
        return ws.get_work_area_for_monitor(monitorIndex);
    }

    _zoneRect(zone, wa) {
        const g = this.gap, h = Math.floor(g / 2), eps = 0.001;
        const x1 = wa.x + Math.round(zone.x * wa.width);
        const y1 = wa.y + Math.round(zone.y * wa.height);
        const x2 = wa.x + Math.round((zone.x + zone.w) * wa.width);
        const y2 = wa.y + Math.round((zone.y + zone.h) * wa.height);
        const l = zone.x < eps ? g : h;
        const t = zone.y < eps ? g : h;
        const r = zone.x + zone.w > 1 - eps ? g : h;
        const b = zone.y + zone.h > 1 - eps ? g : h;
        return { x: x1 + l, y: y1 + t, w: Math.max(50, x2 - x1 - l - r), h: Math.max(50, y2 - y1 - t - b) };
    }

    _rawRect(zone, wa) {
        return {
            x: wa.x + zone.x * wa.width, y: wa.y + zone.y * wa.height,
            w: zone.w * wa.width, h: zone.h * wa.height,
        };
    }

    _monitorAt(x, y) {
        const mons = Main.layoutManager.monitors;
        for (let i = 0; i < mons.length; i++) {
            const m = mons[i];
            if (x >= m.x && x < m.x + m.width && y >= m.y && y < m.y + m.height) return i;
        }
        return Main.layoutManager.primaryIndex;
    }

    _zoneAt(layout, wa, x, y) {
        // Zona menor que contém o ponteiro (permite zonas sobrepostas).
        let best = -1, bestArea = Infinity;
        layout.zones.forEach((z, i) => {
            const r = this._rawRect(z, wa);
            if (x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h) {
                const a = r.w * r.h;
                if (a < bestArea) { best = i; bestArea = a; }
            }
        });
        return best;
    }

    // ---------- Arrastar ----------
    _isMoveOp(op) {
        return op === Meta.GrabOp.MOVING || op === Meta.GrabOp.KEYBOARD_MOVING;
    }

    _onGrabBegin(win, op) {
        if (!win || !this._isMoveOp(op)) return;
        if (win.get_window_type() !== Meta.WindowType.NORMAL) return;
        this.dragWindow = win;
        this.anchorZone = -1;
        this.selected = [];
        this.selectedRect = null;
        this.edgeArmed = false;
        // Nos modos sem tecla, o encaixe nativo competiria pelas bordas desde o início.
        if (this.activation === "edge" || this.activation === "always") this._suspendNative();
        this._stopPolling();
        this.pollId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POLL_MS, () => this._poll());
    }

    _activationHeld(mask, x, y) {
        switch (this.activation) {
            case "always": return true;
            case "edge": {
                if (mask & Clutter.ModifierType.SHIFT_MASK) return true;
                if (!this.edgeArmed) {
                    const m = Main.layoutManager.monitors[this._monitorAt(x, y)];
                    if (x - m.x < EDGE_PX || y - m.y < EDGE_PX ||
                        m.x + m.width - 1 - x < EDGE_PX || m.y + m.height - 1 - y < EDGE_PX)
                        this.edgeArmed = true;
                }
                return this.edgeArmed;
            }
            case "ctrl": return (mask & Clutter.ModifierType.CONTROL_MASK) !== 0;
            case "alt": return (mask & Clutter.ModifierType.MOD1_MASK) !== 0;
            default: return (mask & Clutter.ModifierType.SHIFT_MASK) !== 0;
        }
    }

    _spanHeld(mask) {
        if (mask & Clutter.ModifierType.BUTTON3_MASK) return true;
        if (this.activation !== "ctrl" && (mask & Clutter.ModifierType.CONTROL_MASK)) return true;
        return false;
    }

    _poll() {
        if (!this.dragWindow) { this.pollId = 0; return GLib.SOURCE_REMOVE; }
        const [x, y, mask] = global.get_pointer();

        if (!this._activationHeld(mask, x, y)) {
            this._hideOverlay();
            this.selected = [];
            this.selectedRect = null;
            this.anchorZone = -1;
            return GLib.SOURCE_CONTINUE;
        }

        const mon = this._monitorAt(x, y);
        const layout = this._layoutFor(mon);
        if (!layout || !layout.zones.length) return GLib.SOURCE_CONTINUE;
        const wa = this._workArea(mon);

        if (!this.overlay || this.monitorIndex !== mon || this.overlayLayout !== layout) {
            this._showOverlay(mon, layout, wa);
            this.anchorZone = -1;
        }

        const zi = this._zoneAt(layout, wa, x, y);
        if (zi < 0) {
            this._setSelection(layout, wa, [], null);
            return GLib.SOURCE_CONTINUE;
        }

        if (this._spanHeld(mask)) {
            if (this.anchorZone < 0) this.anchorZone = this.selected.length ? this.selected[0] : zi;
            this._setSelection(layout, wa, ...this._spanSelection(layout, this.anchorZone, zi));
        } else {
            this.anchorZone = -1;
            this._setSelection(layout, wa, [zi], { ...layout.zones[zi] });
        }
        return GLib.SOURCE_CONTINUE;
    }

    _spanSelection(layout, a, b) {
        // Retângulo envolvente das duas zonas, expandido até cobrir inteiramente
        // todas as zonas que ele toca.
        const za = layout.zones[a], zb = layout.zones[b];
        let r = {
            x1: Math.min(za.x, zb.x), y1: Math.min(za.y, zb.y),
            x2: Math.max(za.x + za.w, zb.x + zb.w), y2: Math.max(za.y + za.h, zb.y + zb.h),
        };
        const eps = 0.0005;
        let sel = [];
        for (let iter = 0; iter < 20; iter++) {
            sel = [];
            let changed = false;
            layout.zones.forEach((z, i) => {
                const inter = z.x < r.x2 - eps && z.x + z.w > r.x1 + eps && z.y < r.y2 - eps && z.y + z.h > r.y1 + eps;
                if (!inter) return;
                sel.push(i);
                const nr = {
                    x1: Math.min(r.x1, z.x), y1: Math.min(r.y1, z.y),
                    x2: Math.max(r.x2, z.x + z.w), y2: Math.max(r.y2, z.y + z.h),
                };
                if (nr.x1 < r.x1 - eps || nr.y1 < r.y1 - eps || nr.x2 > r.x2 + eps || nr.y2 > r.y2 + eps) changed = true;
                r = nr;
            });
            if (!changed) break;
        }
        return [sel, { x: r.x1, y: r.y1, w: r.x2 - r.x1, h: r.y2 - r.y1 }];
    }

    _setSelection(layout, wa, sel, rect) {
        this.selected = sel;
        this.selectedRect = rect;
        this.zoneActors.forEach((a, i) => {
            if (sel.includes(i)) a.add_style_class_name("jflex-zone-hover");
            else a.remove_style_class_name("jflex-zone-hover");
        });
    }

    _onGrabEnd(win, op) {
        const target = this.dragWindow;
        const rect = this.overlay ? this.selectedRect : null;
        const mon = this.monitorIndex;
        this.dragWindow = null;
        this._stopPolling();
        this._hideOverlay();
        this._restoreNative();
        if (!target || !rect) return;
        // Espera o Muffin terminar a operação antes de reposicionar.
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 30, () => {
            this._placeWindow(target, rect, mon);
            return GLib.SOURCE_REMOVE;
        });
    }

    _stopPolling() {
        if (this.pollId) { GLib.source_remove(this.pollId); this.pollId = 0; }
    }

    // ---------- Overlay ----------
    _showOverlay(mon, layout, wa) {
        this._hideOverlay();
        this._suspendNative();
        this.monitorIndex = mon;
        this.overlayLayout = layout;
        this.overlay = new St.Widget({ reactive: false, x: 0, y: 0 });
        this.zoneActors = layout.zones.map((z, i) => {
            const r = this._zoneRect(z, wa);
            const actor = new St.Bin({
                style_class: "jflex-zone", reactive: false,
                x: r.x, y: r.y, width: r.w, height: r.h,
                x_align: St.Align.MIDDLE, y_align: St.Align.MIDDLE,
            });
            actor.set_child(new St.Label({ text: String(i + 1), style_class: "jflex-zone-label" }));
            this.overlay.add_child(actor);
            return actor;
        });
        Main.uiGroup.add_child(this.overlay);
    }

    _hideOverlay() {
        if (this.overlay) {
            this.overlay.destroy();
            this.overlay = null;
            this.overlayLayout = null;
            this.zoneActors = [];
        }
    }

    _suspendNative() {
        if (this.savedEdgeTiling !== null || !this.disableNativeTiling) return;
        if (!this.muffinSettings.get_boolean("edge-tiling")) return;
        this.savedEdgeTiling = true;
        this.muffinSettings.set_boolean("edge-tiling", false);
    }

    _restoreNative() {
        if (this.savedEdgeTiling === null) return;
        this.muffinSettings.set_boolean("edge-tiling", this.savedEdgeTiling);
        this.savedEdgeTiling = null;
    }

    // ---------- Posicionar ----------
    _placeWindow(win, zone, mon) {
        if (!win || win.is_fullscreen()) return;
        if (win.get_maximized()) win.unmaximize(Meta.MaximizeFlags.BOTH);
        if (win.get_monitor() !== mon) win.move_to_monitor(mon);
        const r = this._zoneRect(zone, this._workArea(mon));
        win.move_resize_frame(false, r.x, r.y, r.w, r.h);
        // Algumas janelas ignoram a primeira chamada logo após o unmaximize.
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 60, () => {
            const f = win.get_frame_rect();
            if (f.x !== r.x || f.y !== r.y || f.width !== r.w || f.height !== r.h)
                win.move_resize_frame(false, r.x, r.y, r.w, r.h);
            return GLib.SOURCE_REMOVE;
        });
    }

    _currentZoneIndex(win, layout, wa) {
        const f = win.get_frame_rect();
        let best = -1, bestDist = Infinity;
        layout.zones.forEach((z, i) => {
            const r = this._zoneRect(z, wa);
            const d = Math.abs(r.x - f.x) + Math.abs(r.y - f.y) + Math.abs(r.w - f.width) + Math.abs(r.h - f.height);
            if (d < bestDist) { bestDist = d; best = i; }
        });
        return bestDist < 40 ? best : -1;
    }

    _moveFocused(delta, absolute) {
        const win = global.display.get_focus_window();
        if (!win || win.get_window_type() !== Meta.WindowType.NORMAL) return;
        const mon = win.get_monitor();
        const layout = this._layoutFor(mon);
        if (!layout || !layout.zones.length) return;
        const n = layout.zones.length;
        let idx;
        if (absolute !== undefined) {
            if (absolute >= n) return;
            idx = absolute;
        } else {
            const cur = this._currentZoneIndex(win, layout, this._workArea(mon));
            idx = cur < 0 ? (delta > 0 ? 0 : n - 1) : (cur + delta + n) % n;
        }
        this._placeWindow(win, layout.zones[idx], mon);
    }

    _cycleLayout() {
        const layouts = this.config.layouts;
        if (!layouts.length) return;
        const win = global.display.get_focus_window();
        const mon = win ? win.get_monitor() : Main.layoutManager.primaryIndex;
        const cur = this._layoutFor(mon);
        const next = layouts[(layouts.indexOf(cur) + 1) % layouts.length];
        if (Main.layoutManager.monitors.length > 1) this.config.activeByMonitor[String(mon)] = next.id;
        else this.config.active = next.id;
        this._saveConfig();
        this._showOsd(`Layout: ${next.name}`, mon);
    }

    _showOsd(text, mon) {
        this._hideOsd();
        const m = Main.layoutManager.monitors[mon] || Main.layoutManager.primaryMonitor;
        this.osd = new St.Label({ text, style_class: "jflex-osd" });
        Main.uiGroup.add_child(this.osd);
        const [, nw] = this.osd.get_preferred_width(-1);
        const [, nh] = this.osd.get_preferred_height(-1);
        this.osd.set_position(Math.round(m.x + (m.width - nw) / 2), Math.round(m.y + m.height * 0.8 - nh / 2));
        this.osdTimeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1200, () => {
            this.osdTimeout = 0;
            this._hideOsd();
            return GLib.SOURCE_REMOVE;
        });
    }

    _hideOsd() {
        if (this.osdTimeout) { GLib.source_remove(this.osdTimeout); this.osdTimeout = 0; }
        if (this.osd) { this.osd.destroy(); this.osd = null; }
    }

    // ---------- Atalhos ----------
    _bindKeys() {
        this._unbindKeys();
        const add = (name, key, cb) => {
            if (!key) return;
            Main.keybindingManager.addHotKey(name, key, cb);
            this.boundKeys.push(name);
        };
        add("jflex-next", this.keyNext, () => this._moveFocused(1));
        add("jflex-prev", this.keyPrev, () => this._moveFocused(-1));
        add("jflex-cycle", this.keyCycleLayout, () => this._cycleLayout());
        add("jflex-editor", this.keyEditor, () => this.openEditor());
        if (this.zoneNumberKeys)
            for (let i = 1; i <= 9; i++)
                add(`jflex-zone-${i}`, `<Super><Alt>${i}`, () => this._moveFocused(0, i - 1));
    }

    _unbindKeys() {
        for (const name of this.boundKeys || []) Main.keybindingManager.removeHotKey(name);
        this.boundKeys = [];
    }

    openEditor() {
        const editor = GLib.build_filenamev([this.meta.path, "editor.py"]);
        Util.spawn(["python3", editor]);
    }
}

function init(meta) {
    extension = { meta };
}

function enable() {
    try {
        extension.instance = new JanelaFlex(extension.meta);
    } catch (e) {
        global.logError(`[janela-flex] falha ao ativar: ${e}\n${e.stack}`);
    }
    // Callbacks usados pelos botões em settings-schema.json.
    return {
        openEditor: () => extension.instance && extension.instance.openEditor(),
        get instance() { return extension.instance; },
    };
}

function disable() {
    if (extension.instance) {
        extension.instance.destroy();
        extension.instance = null;
    }
}
