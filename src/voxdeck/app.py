"""Vox Deck main window, tray icon and entry point."""
import argparse
import math
import os
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QColorDialog, QComboBox, QFileDialog,
                               QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QMainWindow,
                               QMenu, QMessageBox, QPushButton, QRadioButton, QSlider, QSystemTrayIcon, QVBoxLayout, QWidget)

from . import audio, bundle
from .board import COLORS
from .controller import Controller

STYLE = """
QMainWindow, QWidget#root { background: #15171c; }
QLabel { color: #d7dae0; }
QLabel#h { color: #8a90a0; font-size: 11px; letter-spacing: 2px; }
QLabel#title { font-size: 20px; font-weight: bold; color: #f2f3f5; }
QLabel#val { color: #aab0c0; }
QSlider::groove:horizontal { height: 4px; background: #2f3442; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #5865f2; border-radius: 2px; }
QSlider::handle:horizontal { background: #e6e8ec; width: 12px; margin: -5px 0; border-radius: 6px; }
QComboBox, QPushButton { background: #232731; color: #e6e8ec; border: 1px solid #2f3442;
    border-radius: 6px; padding: 6px 10px; }
QPushButton:hover { border-color: #5865f2; }
QPushButton:disabled { color: #5b6070; background: #1a1d24; }
QPushButton#preset:checked, QPushButton#upreset:checked { background: #5865f2; border-color: #7983f5; color: white; font-weight: bold; }
QPushButton#upreset { border-color: #3d5a80; color: #cfe0ff; }
QPushButton#power:checked { background: #1f7a45; border-color: #2ea35f; color: white; font-weight: bold; }
QPushButton#mute:checked { background: #8a2a2a; border-color: #b33a3a; color: white; }
QPushButton#pad { border: none; border-radius: 10px; font-size: 14px; font-weight: bold; color: white; }
QPushButton#pad:pressed { padding-top: 9px; }
QPushButton#add { background: #1a1d24; border: 2px dashed #353a48; color: #6d7385; font-size: 24px; border-radius: 10px; }
QCheckBox, QRadioButton { color: #d7dae0; spacing: 8px; }
QRadioButton::indicator { width: 12px; height: 12px; border-radius: 8px; border: 2px solid #5b6070; background: #15171c; }
QRadioButton::indicator:checked { border: 2px solid #c9cdfb; background: #5865f2; }
QMenu { background: #1b1e25; color: #e6e8ec; border: 1px solid #2f3442; padding: 4px; }
QMenu::item { padding: 5px 20px; border-radius: 4px; }
QMenu::item:selected { background: #5865f2; color: white; }
QMenu::item:disabled { color: #5b6070; }
QMenu::separator { height: 1px; background: #2f3442; margin: 4px 6px; }
QDialog, QMessageBox { background: #1b1e25; }
QPushButton::menu-indicator { subcontrol-position: right center; right: 6px; }
QFrame#line { color: #2a2e3a; }
"""


def make_icon() -> QIcon:
    pm = QPixmap(128, 128)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#5865f2"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(8, 8, 112, 112, 26, 26)
    p.setBrush(QColor("white"))
    for i, h in enumerate((30, 62, 84, 50, 24)):
        p.drawRoundedRect(20 + i * 20, 64 - h // 2, 12, h, 5, 5)
    p.end()
    return QIcon(pm)


def header(text):
    l = QLabel(text.upper())
    l.setObjectName("h")
    return l


class LevelMeter(QWidget):
    """Horizontal peak meter, -60..0 dBFS, with a short peak hold. Painted from values the
    controller already has; no audio passes through the GUI."""
    LO = -60.0

    def __init__(self, label):
        super().__init__()
        self.label = label
        self.db = None
        self.hold, self.hold_t = self.LO, 0
        self.setMinimumSize(200, 22)
        self.setMaximumHeight(22)

    def set_level(self, db, now_ms):
        self.db = db
        if db is not None and db >= self.hold:
            self.hold, self.hold_t = db, now_ms
        elif now_ms - self.hold_t > 900:
            self.hold = max(self.LO, self.hold - 1.5)
        self.update()

    def frac(self, db):
        return max(0.0, min(1.0, (db - self.LO) / -self.LO))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        lw = 74
        p.setPen(QColor("#8a90a0"))
        p.drawText(0, 0, lw, self.height(), Qt.AlignmentFlag.AlignVCenter, self.label)
        bx, bw, bh = lw, self.width() - lw - 58, self.height() - 8
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#232731"))
        p.drawRoundedRect(bx, 4, bw, bh, 4, 4)
        if self.db is not None:
            f = self.frac(self.db)
            col = "#2ea35f" if self.db < -12 else ("#d68910" if self.db < -3 else "#c0392b")
            p.setBrush(QColor(col))
            if f > 0:
                p.drawRoundedRect(bx, 4, max(4, int(bw * f)), bh, 4, 4)
            hx = bx + int(bw * self.frac(self.hold))
            p.setBrush(QColor("#e6e8ec"))
            if self.hold > self.LO:
                p.drawRect(min(hx, bx + bw - 2), 4, 2, bh)
        p.setPen(QColor("#d7dae0" if self.db is not None else "#5b6070"))
        txt = "--" if self.db is None else ("-inf" if self.db <= self.LO else f"{self.db:.0f} dB")
        p.drawText(bx + bw + 6, 0, 52, self.height(), Qt.AlignmentFlag.AlignVCenter, txt)
        p.end()


def fmt_value(spec, v):
    step = float(spec.get("step") or 0.01)
    dec = 0 if step >= 1 else min(3, max(0, -math.floor(math.log10(step) + 1e-9)))
    unit = spec.get("unit", "")
    sep = "" if unit in ("x", "%", ":1") else " "
    return f"{v:.{dec}f}{sep}{unit}" if unit else f"{v:.{dec}f}"


class TuneSlider(QWidget):
    """One tunable control: label, slider (in steps), value text."""

    def __init__(self, win, spec):
        super().__init__()
        self.win, self.spec = win, spec
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.name = QLabel(spec["name"])
        self.name.setFixedWidth(118)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.n = max(1, round((spec["max"] - spec["min"]) / spec["step"]))
        self.slider.setRange(0, self.n)
        self.val = QLabel()
        self.val.setObjectName("val")
        self.val.setFixedWidth(64)
        self.val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        h.addWidget(self.name)
        h.addWidget(self.slider, 1)
        h.addWidget(self.val)
        self.set_value(spec["value"])
        self.slider.valueChanged.connect(self.moved)

    def to_value(self, i):
        return self.spec["min"] + (self.spec["max"] - self.spec["min"]) * i / self.n

    def set_value(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(round((v - self.spec["min"]) / (self.spec["max"] - self.spec["min"]) * self.n))
        self.slider.blockSignals(False)
        self.val.setText(fmt_value(self.spec, v))

    def moved(self, i):
        v = self.win.ctl.set_param(self.spec["id"], self.to_value(i), self.win.tune_pid)
        self.val.setText(fmt_value(self.spec, v))


class ImportDialog(QDialog):
    """Shows what a bundle holds and asks how to bring it in. Nothing is overwritten unless the
    user picks a replace option (and confirms replacing the board)."""

    def __init__(self, parent, info, board_pads):
        super().__init__(parent)
        self.setWindowTitle("Import")
        self.board_pads = board_pads
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.setSpacing(7)
        n, b = len(info["presets"]), info["board"]
        parts = []
        if n:
            parts.append(f"{n} preset{'s' if n != 1 else ''}: " + ", ".join(p["name"] for p in info["presets"][:6])
                         + (" ..." if n > 6 else ""))
        if b:
            parts.append(f"a soundboard with {b['clips']} sound{'s' if b['clips'] != 1 else ''}")
        lab = QLabel("This file has " + (" and ".join(parts) if parts else "nothing to import") + ".")
        lab.setWordWrap(True)
        v.addWidget(lab)
        self.pmode = self.bmode = None
        if info["conflicts"]:
            v.addWidget(header("Presets you already have"))
            by_id = {p["id"]: p["name"] for p in info["presets"]}
            names = ", ".join(by_id.get(i, i) for i in info["conflicts"])
            self.pmode = self._choices(v, [("rename", f"Keep both ({names}): the imported copies get new names"),
                                           ("replace", "Replace my presets with the imported ones"),
                                           ("skip", "Skip those presets")])
        if info["builtin_ids"]:
            note = QLabel("Presets named like a built-in are imported under a new id (built-ins stay read-only).")
            note.setWordWrap(True)
            v.addWidget(note)
        if b:
            v.addWidget(header("Soundboard"))
            self.bmode = self._choices(v, [("append", f"Add the {b['clips']} pads to my board"),
                                           ("replace", f"Replace my board ({board_pads} pads; my sound files are kept)"),
                                           ("skip", "Do not import the board")])
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        ok = bb.addButton("Import", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setEnabled(bool(parts))
        bb.accepted.connect(self.confirm)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    @staticmethod
    def _choices(layout, options):
        group = QButtonGroup(layout)
        for i, (key, text) in enumerate(options):
            r = QRadioButton(text)
            r.setProperty("key", key)
            r.setChecked(i == 0)
            group.addButton(r)
            layout.addWidget(r)
        return group

    @staticmethod
    def _key(group, default):
        return group.checkedButton().property("key") if group else default

    def preset_mode(self):
        return self._key(self.pmode, "rename")

    def board_mode(self):
        return self._key(self.bmode, "skip")

    def confirm(self):
        if self.board_mode() == "replace" and self.board_pads and QMessageBox.question(
                self, "Replace soundboard", f"Replace your soundboard ({self.board_pads} pads) with the imported one?"
                " Your sound files stay where they are.") != QMessageBox.StandardButton.Yes:
            return
        self.accept()


class Pad(QPushButton):
    def __init__(self, win, idx, clip):
        super().__init__(clip["name"])
        self.win, self.idx = win, idx
        self.setObjectName("pad")
        self.setMinimumSize(110, 80)
        self.setStyleSheet(f"background: {clip.get('color', COLORS[idx % len(COLORS)])};")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.menu)
        self.clicked.connect(lambda: win.ctl.play(idx))
        self.setToolTip(clip["path"])

    def menu(self, pos):
        m = QMenu(self)
        a_ren, a_col, a_del = m.addAction("Rename"), m.addAction("Colour"), m.addAction("Remove")
        act = m.exec(self.mapToGlobal(pos))
        clip = self.win.ctl.board.clips[self.idx]
        if act == a_ren:
            t, ok = QInputDialog.getText(self, "Rename", "Name", text=clip["name"])
            if ok and t:
                clip["name"] = t
        elif act == a_col:
            c = QColorDialog.getColor(QColor(clip.get("color", "#2980b9")), self)
            if c.isValid():
                clip["color"] = c.name()
        elif act == a_del:
            del self.win.ctl.board.clips[self.idx]
        else:
            return
        self.win.ctl.board.save()
        self.win.rebuild_board()


class Window(QMainWindow):
    COLS = 5

    def __init__(self, ctl: Controller):
        super().__init__()
        self.ctl = ctl
        self.setWindowTitle("Vox Deck")
        self.setWindowIcon(make_icon())
        self.setAcceptDrops(True)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(10)

        top = QHBoxLayout()
        t = QLabel("Vox Deck")
        t.setObjectName("title")
        top.addWidget(t)
        top.addStretch()
        imp = QPushButton("Import...")
        imp.setToolTip("Bring in presets and/or a soundboard from a .voxdeck file")
        imp.clicked.connect(self.import_file)
        top.addWidget(imp)
        exp = QPushButton("Export")
        exp.setToolTip("Save presets and/or the soundboard (with its sounds) to a .voxdeck file")
        em = QMenu(exp)
        em.addAction("Soundboard and my presets...", lambda: self.export_file(True, "user"))
        em.addAction("Soundboard only...", lambda: self.export_file(True, "none"))
        em.addAction("My presets only...", lambda: self.export_file(False, "user"))
        em.addAction("Current preset...", lambda: self.export_file(False, [self.ctl.s["preset"]]))
        exp.setMenu(em)
        top.addWidget(exp)
        top.addSpacing(12)
        self.power = QPushButton("Effects ON")
        self.power.setObjectName("power")
        self.power.setCheckable(True)
        self.power.clicked.connect(lambda: ctl.set_enabled(self.power.isChecked()))
        self.mute = QPushButton("Mic muted")
        self.mute.setObjectName("mute")
        self.mute.setCheckable(True)
        self.mute.clicked.connect(lambda: ctl.set_muted(self.mute.isChecked()))
        top.addWidget(self.power)
        top.addWidget(self.mute)
        v.addLayout(top)

        row = QHBoxLayout()
        row.addWidget(QLabel("Microphone"))
        self.mic = QComboBox()
        self.mic.setMinimumWidth(320)
        row.addWidget(self.mic)
        row.addStretch()
        self.status = QLabel()
        row.addWidget(self.status)
        v.addLayout(row)

        mrow = QHBoxLayout()
        self.m_mic = LevelMeter("Mic in")
        self.m_out = LevelMeter("Voice Mix")
        mrow.addWidget(self.m_mic, 1)
        mrow.addSpacing(18)
        mrow.addWidget(self.m_out, 1)
        v.addLayout(mrow)
        self.mtimer = QTimer(self)
        self.mtimer.timeout.connect(self.poll_meters)
        self.clock = QElapsedTimer()
        self.clock.start()

        v.addWidget(header("Voice presets"))
        self.pgrid = QGridLayout()
        self.pgrid.setSpacing(6)
        v.addLayout(self.pgrid)
        self.pbtn = {}
        self.rebuild_presets()
        ctl.presets_changed.connect(self.rebuild_presets)

        trow = QHBoxLayout()
        self.tune_head = header("Tune")
        trow.addWidget(self.tune_head)
        trow.addStretch()
        self.tune_save = QPushButton("Save as preset...")
        self.tune_save.setToolTip("Keep these settings as a new preset of your own")
        self.tune_save.clicked.connect(self.save_preset)
        trow.addWidget(self.tune_save)
        self.tune_reset = QPushButton("Reset")
        self.tune_reset.setToolTip("Back to the preset's own values")
        self.tune_reset.clicked.connect(lambda: ctl.reset_params(self.tune_pid))
        trow.addWidget(self.tune_reset)
        v.addLayout(trow)
        self.tgrid = QGridLayout()
        self.tgrid.setHorizontalSpacing(28)
        self.tgrid.setVerticalSpacing(6)
        v.addLayout(self.tgrid)
        self.tune_pid, self.tune_sig, self.tsliders = None, None, {}

        line = QFrame()
        line.setObjectName("line")
        line.setFrameShape(QFrame.Shape.HLine)
        v.addWidget(line)
        v.addWidget(header("Soundboard  (drop audio files here, right-click a pad to edit)"))
        self.bgrid = QGridLayout()
        self.bgrid.setSpacing(8)
        v.addLayout(self.bgrid)
        v.addStretch()

        bot = QHBoxLayout()
        bot.addWidget(QLabel("Board volume"))
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setFixedWidth(180)
        self.vol.valueChanged.connect(lambda x: ctl.set_opt("board_volume", x / 100))
        bot.addWidget(self.vol)
        self.monitor = QCheckBox("Hear clips myself")
        self.monitor.toggled.connect(lambda x: ctl.set_opt("monitor", x))
        bot.addWidget(self.monitor)
        bot.addStretch()
        stop = QPushButton("Stop all")
        stop.clicked.connect(ctl.stop_all)
        bot.addWidget(stop)
        v.addLayout(bot)

        self.fill_mics()
        self.mic.activated.connect(lambda: ctl.set_mic(self.mic.currentData()))
        self.rebuild_board()
        ctl.board_changed.connect(self.rebuild_board)
        ctl.changed.connect(self.refresh)
        self.refresh()
        self.resize(800, 680)

    def fill_mics(self):
        self.mic.blockSignals(True)
        self.mic.clear()
        cur = self.ctl.s["mic"]
        mics = audio.list_mics()
        if cur and cur not in [n for n, _ in mics]:
            mics.append((cur, cur))
        for n, d in mics:
            self.mic.addItem(d, n)
        self.mic.setCurrentIndex(max(0, self.mic.findData(cur)))
        self.mic.blockSignals(False)

    def rebuild_board(self):
        while self.bgrid.count():
            self.bgrid.takeAt(0).widget().deleteLater()
        for i, c in enumerate(self.ctl.board.clips):
            self.bgrid.addWidget(Pad(self, i, c), i // self.COLS, i % self.COLS)
        n = len(self.ctl.board.clips)
        add = QPushButton("+")
        add.setObjectName("add")
        add.setMinimumSize(110, 80)
        add.clicked.connect(self.add_clips)
        self.bgrid.addWidget(add, n // self.COLS, n % self.COLS)

    def add_clips(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add sounds", "", "Audio (*.wav *.flac *.ogg *.mp3 *.opus)")
        self.add_files(files)

    def add_files(self, files):
        b = self.ctl.board
        for f in files:
            b.clips.append({"name": Path(f).stem[:18], "path": f, "color": COLORS[len(b.clips) % len(COLORS)]})
        b.save()
        self.rebuild_board()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_files([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def refresh(self):
        st = self.ctl.status()
        self.power.setChecked(st["enabled"])
        self.power.setText("Effects ON" if st["enabled"] else "Effects OFF")
        self.mute.setChecked(st["muted"])
        self.mute.setText("Mic MUTED" if st["muted"] else "Mic live")
        for pid, b in self.pbtn.items():
            b.setChecked(pid == st["preset"])
        self.vol.blockSignals(True)
        self.vol.setValue(int(st["board_volume"] * 100))
        self.vol.blockSignals(False)
        self.monitor.blockSignals(True)
        self.monitor.setChecked(st["monitor"])
        self.monitor.blockSignals(False)
        self.refresh_tune()
        ok = st["engine"]["mix"] and (st["muted"] or st["engine"]["voice"])
        self.status.setText(("Virtual mic: Voice Mix" if ok else "Audio engine not running") +
                            (f"   playing {st['playing']}" if st["playing"] else ""))

    def rebuild_presets(self):
        while self.pgrid.count():
            self.pgrid.takeAt(0).widget().deleteLater()
        self.pbtn = {}
        for i, (pid, p) in enumerate(self.ctl.presets.items()):
            b = QPushButton(p["name"])
            b.setObjectName("upreset" if p["user"] else "preset")
            b.setCheckable(True)
            tip = p.get("description", "")
            if p["user"]:
                tip += "\nYour preset. Right-click to rename, export or delete."
            if not p["available"]:
                b.setEnabled(False)
                tip += "\nNeeds: " + ", ".join(p["missing"])
            b.setToolTip(tip.strip())
            b.clicked.connect(lambda _=False, pid=pid: self.ctl.set_preset(pid))
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos, pid=pid, b=b: self.preset_menu(pid, b, pos))
            self.pgrid.addWidget(b, i // 5, i % 5)
            self.pbtn[pid] = b
        self.tune_sig = None
        if hasattr(self, "tgrid"):
            self.refresh()

    def preset_menu(self, pid, button, pos):
        p = self.ctl.presets.get(pid)
        if not p:
            return
        m = QMenu(self)
        if p["user"]:
            a_ren, a_del = m.addAction("Rename..."), m.addAction("Delete...")
        else:
            a_ren = a_del = None
            m.addAction("Built-in presets are read-only").setEnabled(False)
        self.preset_menu_extra(m, pid)
        act = m.exec(button.mapToGlobal(pos))
        try:
            if act is not None and act == a_ren:
                t, ok = QInputDialog.getText(self, "Rename preset", "Name", text=p["name"])
                if ok and t.strip():
                    self.ctl.rename_preset(pid, t)
            elif act is not None and act == a_del:
                if QMessageBox.question(self, "Delete preset", f"Delete your preset \"{p['name']}\"?") \
                        == QMessageBox.StandardButton.Yes:
                    self.ctl.delete_preset(pid)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "Vox Deck", str(e))

    def preset_menu_extra(self, menu, pid):
        menu.addSeparator()
        menu.addAction("Export...", lambda: self.export_file(False, [pid]))

    def export_file(self, board, presets):
        if presets == "user" and not board and not any(p["user"] for p in self.ctl.presets.values()):
            QMessageBox.information(self, "Export", "You have no presets of your own yet. Use \"Save as preset...\" first.")
            return
        base = "vox-deck-board" if board else (presets[0] if isinstance(presets, list) else "vox-deck-presets")
        path, _ = QFileDialog.getSaveFileName(self, "Export", str(Path.home() / (base + bundle.SUFFIX)),
                                              "Vox Deck file (*.voxdeck)")
        if not path:
            return
        if not path.endswith(bundle.SUFFIX):
            path += bundle.SUFFIX
        try:
            rep = self.ctl.export(path, board, presets)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "Export", f"Export failed: {e}")
            return
        msg = f"Saved {rep['presets']} preset(s)" + (f" and {rep['clips']} sound(s)" if board else "") + f" to {path}."
        if rep["skipped"]:
            msg += "\n\nLeft out (file missing or not audio): " + ", ".join(rep["skipped"])
        QMessageBox.information(self, "Export", msg)

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import", str(Path.home()),
                                              "Vox Deck file (*.voxdeck *.zip)")
        if not path:
            return
        try:
            info = self.ctl.inspect(path)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "Import", f"This file can not be imported: {e}")
            return
        dlg = ImportDialog(self, info, len(self.ctl.board.clips))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            rep = self.ctl.import_bundle(path, dlg.preset_mode(), dlg.board_mode())
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "Import", f"Import failed, nothing was changed: {e}")
            return
        bits = []
        if rep["presets_added"] or rep["presets_replaced"]:
            bits.append(f"{len(rep['presets_added'])} preset(s) added, {len(rep['presets_replaced'])} replaced")
        if rep["presets_skipped"]:
            bits.append(f"{len(rep['presets_skipped'])} skipped")
        if rep["clips"]:
            bits.append(f"{rep['clips']} pad(s) {'replaced the board' if rep['board'] == 'replace' else 'added'}")
        QMessageBox.information(self, "Import", ("Imported: " + "; ".join(bits)) if bits else "Nothing was imported.")

    def save_preset(self):
        pid = self.ctl.s["preset"]
        p = self.ctl.presets[pid]
        suggestion = p["name"] if p["user"] else f"My {p['name']}"
        t, ok = QInputDialog.getText(self, "Save as preset",
                                     f"Name for a new preset with the current {p['name']} settings",
                                     text=suggestion)
        if ok and t.strip():
            try:
                self.ctl.save_preset(t)
            except (ValueError, OSError) as e:
                QMessageBox.warning(self, "Vox Deck", str(e))

    def refresh_tune(self):
        pid = self.ctl.s["preset"]
        params = self.ctl.params(pid)
        sig = (pid, tuple(t["id"] for t in params))
        if sig != self.tune_sig:
            while self.tgrid.count():
                self.tgrid.takeAt(0).widget().deleteLater()
            self.tune_pid, self.tune_sig, self.tsliders = pid, sig, {}
            for i, t in enumerate(params):
                w = TuneSlider(self, t)
                self.tsliders[t["id"]] = w
                self.tgrid.addWidget(w, i // 2, i % 2)
        else:
            for t in params:
                w = self.tsliders[t["id"]]
                if not w.slider.isSliderDown():
                    w.set_value(t["value"])
        name = self.ctl.presets[pid]["name"]
        off = "" if self.ctl.s["enabled"] else "  (effects off: applies when on)"
        self.tune_head.setText(f"TUNE  {name.upper()}{off}")
        self.tune_reset.setEnabled(bool(self.ctl.s["tuning"].get(pid)))

    def poll_meters(self):
        lv, now = self.ctl.levels(), self.clock.elapsed()
        self.m_mic.set_level(lv["mic"], now)
        self.m_out.set_level(lv["out"], now)

    def showEvent(self, e):
        super().showEvent(e)
        self.ctl.set_meter_window(True)
        self.mtimer.start(40)

    def hideEvent(self, e):
        super().hideEvent(e)
        self.mtimer.stop()
        self.ctl.set_meter_window(False)

    def closeEvent(self, e):
        if QSystemTrayIcon.isSystemTrayAvailable():
            e.ignore()
            self.hide()
        else:
            e.accept()
            QApplication.quit()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vox-deck-gui")
    ap.add_argument("--minimized", action="store_true", help="start in the tray")
    ap.add_argument("--headless", action="store_true", help="no window or tray, audio and socket only")
    ap.add_argument("--screenshot", metavar="PNG", help="render the window to a PNG and exit (for testing)")
    a = ap.parse_args(argv)
    if a.headless:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no display needed
    app = QApplication(sys.argv[:1])
    app.setApplicationName("vox-deck")
    app.setDesktopFileName("vox-deck")
    app.setQuitOnLastWindowClosed(False)
    ctl = Controller()
    ctl.start()
    app.aboutToQuit.connect(ctl.shutdown)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    tick = QTimer()  # lets Python signal handlers run
    tick.start(300)
    tick.timeout.connect(lambda: None)
    if a.headless:
        return app.exec()
    app.setStyleSheet(STYLE)
    win = Window(ctl)
    if a.screenshot:
        win.show()
        QTimer.singleShot(1500, lambda: (win.grab().save(a.screenshot), app.quit()))
        return app.exec()
    tray = QSystemTrayIcon(make_icon(), app)
    menu = QMenu()
    menu.addAction("Show", win.show)
    act = QAction("Effects on", menu, checkable=True)
    act.triggered.connect(lambda: ctl.set_enabled(act.isChecked()))
    menu.addAction(act)
    sub = menu.addMenu("Preset")

    def fill_presets():
        sub.clear()
        for pid, p in ctl.presets.items():
            if p["available"]:
                sub.addAction(p["name"], lambda pid=pid: ctl.set_preset(pid))
    fill_presets()
    ctl.presets_changed.connect(fill_presets)
    menu.addAction("Stop all clips", ctl.stop_all)
    menu.addSeparator()
    menu.addAction("Quit", app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: win.show() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
    ctl.changed.connect(lambda: (act.setChecked(ctl.s["enabled"]),
                                 tray.setToolTip(f"Vox Deck: {ctl.status()['preset_name']}"
                                                 + ("" if ctl.s["enabled"] else " (bypassed)"))))
    act.setChecked(ctl.s["enabled"])
    tray.show()
    if not a.minimized or not tray.isVisible():
        win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
