"""Generate the plugin's PNG icons (needs PySide6). Run from the repo root."""
import os
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
from PySide6.QtCore import Qt, QRectF

app = QGuiApplication([])
out = Path(__file__).parent / "xyz.arrowstorm.voxdeck.sdPlugin" / "icons"


def key(name, bg, label, size=144):
    for suffix, s in (("", size // 2), ("@2x", size)):
        im = QImage(s, s, QImage.Format.Format_ARGB32)
        im.fill(Qt.GlobalColor.transparent)
        p = QPainter(im)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(bg))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(0, 0, s, s), s * .12, s * .12)
        p.setPen(QColor("white"))
        f = QFont("sans-serif")
        f.setBold(True)
        f.setPixelSize(int(s * .2))
        p.setFont(f)
        p.drawText(QRectF(0, 0, s, s), Qt.AlignmentFlag.AlignCenter, label)
        p.end()
        im.save(str(out / f"{name}{suffix}.png"))


key("key_clip", "#2980b9", "CLIP")
key("key_preset", "#2a2e3a", "VOICE")
key("key_preset_active", "#5865f2", "VOICE\nACTIVE")
key("key_on", "#1f7a45", "FX ON")
key("key_off", "#4a4f5c", "FX OFF")
key("key_live", "#2a2e3a", "MIC\nLIVE")
key("key_muted", "#8a2a2a", "MIC\nMUTED")
key("key_stop", "#8a2a2a", "STOP")
for name, s in (("plugin", 256), ("action", 20)):
    for suffix, k in (("", 1), ("@2x", 2)):
        im = QImage(s * k, s * k, QImage.Format.Format_ARGB32)
        im.fill(Qt.GlobalColor.transparent)
        p = QPainter(im)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor("#5865f2") if name == "plugin" else QColor("white"))
        p.setPen(Qt.PenStyle.NoPen)
        n = s * k
        if name == "plugin":
            p.drawRoundedRect(QRectF(0, 0, n, n), n * .2, n * .2)
            p.setBrush(QColor("white"))
        for i, h in enumerate((.3, .6, .8, .5, .25)):
            w = n * .1
            p.drawRoundedRect(QRectF(n * .15 + i * n * .17, n / 2 - n * h / 2, w, n * h), w / 2, w / 2)
        p.end()
        im.save(str(out / f"{name}{suffix}.png"))
