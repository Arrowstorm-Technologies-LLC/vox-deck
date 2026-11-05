"""Vox Deck main window, tray icon and entry point."""
import argparse
import signal
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QFileDialog,
                               QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QMainWindow,
                               QMenu, QPushButton, QSlider, QSystemTrayIcon, QVBoxLayout, QWidget)

from . import audio
from .board import COLORS
from .controller import Controller

STYLE = """
QMainWindow, QWidget#root { background: #15171c; }
QLabel { color: #d7dae0; }
QLabel#h { color: #8a90a0; font-size: 11px; letter-spacing: 2px; }
QLabel#title { font-size: 20px; font-weight: bold; color: #f2f3f5; }
QComboBox, QPushButton { background: #232731; color: #e6e8ec; border: 1px solid #2f3442;
    border-radius: 6px; padding: 6px 10px; }
QPushButton:hover { border-color: #5865f2; }
QPushButton:disabled { color: #5b6070; background: #1a1d24; }
QPushButton#preset:checked { background: #5865f2; border-color: #7983f5; color: white; font-weight: bold; }
QPushButton#power:checked { background: #1f7a45; border-color: #2ea35f; color: white; font-weight: bold; }
QPushButton#mute:checked { background: #8a2a2a; border-color: #b33a3a; color: white; }
QPushButton#pad { border: none; border-radius: 10px; font-size: 14px; font-weight: bold; color: white; }
QPushButton#pad:pressed { padding-top: 9px; }
QPushButton#add { background: #1a1d24; border: 2px dashed #353a48; color: #6d7385; font-size: 24px; border-radius: 10px; }
QCheckBox { color: #d7dae0; }
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

        v.addWidget(header("Voice presets"))
        self.pgrid = QGridLayout()
        self.pgrid.setSpacing(6)
        v.addLayout(self.pgrid)
        self.pbtn = {}
        for i, (pid, p) in enumerate(ctl.presets.items()):
            b = QPushButton(p["name"])
            b.setObjectName("preset")
            b.setCheckable(True)
            tip = p.get("description", "")
            if not p["available"]:
                b.setEnabled(False)
                tip += "\nNeeds: " + ", ".join(p["missing"])
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, pid=pid: ctl.set_preset(pid))
            self.pgrid.addWidget(b, i // 5, i % 5)
            self.pbtn[pid] = b

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
        ctl.changed.connect(self.refresh)
        self.refresh()
        self.resize(780, 560)

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
        ok = st["engine"]["mix"] and (st["muted"] or st["engine"]["voice"])
        self.status.setText(("Virtual mic: Voice Mix" if ok else "Audio engine not running") +
                            (f"   playing {st['playing']}" if st["playing"] else ""))

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
        QTimer.singleShot(600, lambda: (win.grab().save(a.screenshot), app.quit()))
        return app.exec()
    tray = QSystemTrayIcon(make_icon(), app)
    menu = QMenu()
    menu.addAction("Show", win.show)
    act = QAction("Effects on", menu, checkable=True)
    act.triggered.connect(lambda: ctl.set_enabled(act.isChecked()))
    menu.addAction(act)
    sub = menu.addMenu("Preset")
    for pid, p in ctl.presets.items():
        if p["available"]:
            sub.addAction(p["name"], lambda pid=pid: ctl.set_preset(pid))
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
