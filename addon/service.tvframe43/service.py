# -*- coding: utf-8 -*-
"""service.tvframe43 - 4:3- und Breitbild-Videos in einem Fernsehgehaeuse zeigen.

Erkennt das Seitenverhaeltnis des laufenden Videos:
  ~4:3   (Standard 1.20-1.45) -> 4:3-Rahmen
  ~2.35  (Standard 2.10-2.50) -> Breitbild-Rahmen (breiter Rahmen oben/unten)
  sonst  (16:9 usw.)          -> nichts
Ab 1.3.0: meldet Kodi ~16:9 (1.70-1.85), wird nach eingebrannten schwarzen Balken gesucht
(letterbox.py). Ein 2,39-Film im 16:9-Bild wird dann wie ein Breitbild-Film behandelt.
Das Overlay (PNG 1920x1080 mit transparenter Mattscheibe) liegt immer
bildschirmfuellend auf dem Vollbild-Videofenster (12005). Der Zoom wird aus
dem Mattscheiben-Rechteck berechnet (JSON neben dem PNG bzw. aus dem
Alphakanal einer eigenen Datei), sodass das Video die Mattscheibe exakt deckt.
"""
import json
import os
import re
import struct
import threading
import time
import zlib

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

import gallery
import letterbox

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
ADDON_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo("path"))
FRAMES_DIR = os.path.join(ADDON_PATH, "resources", "frames")
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
CACHE_FILE = os.path.join(PROFILE, "custom_frames.json")
DESIGNS_DIR = os.path.join(PROFILE, "designs")         # ab 1.2.0: Designs aus der Galerie
GALLERY_CACHE = os.path.join(PROFILE, "gallery")       # index.json + Vorschaubilder
LETTERBOX_FILE = os.path.join(PROFILE, "letterbox.json")  # ab 1.3.0: gemessene Balken je Datei
# Nur Videos, die Kodi als ~16:9 meldet, werden auf eingebrannte Balken untersucht
LB_MIN, LB_MAX = 1.70, 1.85

# Reihenfolge = Werte der Einstellung "style2" (ab 1.1.1; die alte Einstellung "style"
# aus 1.1.0 wird ignoriert, weil sich die Bedeutung der Indizes geaendert hat).
# Je Eintrag: (Datei-Stamm 4:3, Datei-Stamm Breitbild 2.35[, Datei-Stamm 2:1]) in resources/frames/.
# Ohne 2:1-Eintrag bleibt 2:1-Material (Format "w2") wie bisher unangetastet.
STYLES = [
    ("lg_cx_43_nofoot", "lg_cx_wide", "lg_cx_wide_2x1"),   # 0 LG CX, 4:3 ohne Fuss (Standard)
    ("lg_cx_43", "lg_cx_wide", "lg_cx_wide_2x1"),          # 1 LG CX, 4:3 mit Fuss
    ("wood50v3_43", "wood50v3_wide"),       # 2 50er Holztruhe (neu)
    ("flatA_43", "flat_wide"),              # 3 Flach A
    ("flatB_43", "flat_wide"),              # 4 Flach B
    ("wood50_43", "wood50_wide"),           # 5 (alt) 50er Holztruhe 1.1.0
    ("space60_43", "space60_wide"),         # 6 (alt)
    ("orange70_43", "orange70_wide"),       # 7 (alt)
    ("silver80_43", "silver80_wide"),       # 8 (alt)
    ("minimal_43", "minimal_wide"),         # 9 (alt)
    ("monitor80_43", "monitor80_wide"),     # 10 (alt)
    None,                                   # 11 eigene Datei (Platzhalter)
    ("lg_cx_body_fabric", "lg_cx_wide_body", "lg_cx_wide_body_2x1"),   # 12 LG CX Gehaeuse, Stoff (ab 1.1.2)
    ("lg_cx_body_metal", "lg_cx_wide_body", "lg_cx_wide_body_2x1"),    # 13 LG CX Gehaeuse, Metall (ab 1.1.2)
    None,                                   # 14 Design aus der Galerie (ab 1.2.0, Platzhalter)
]
STYLE_CUSTOM = 11                           # eigene Datei
# Ab 1.2.0: Wert 14 = Design aus der Galerie; welches, steht je Format in gallery_43/_wide/_w2
# (Design-ID). So bleibt style_* ein Integer aus der Options-Liste und Kodi setzt das Dropdown
# nicht zurueck; alle bisherigen Werte behalten ihre Bedeutung.
STYLE_GALLERY = 14
GALLERY_SETTING = {"43": "gallery_43", "wide": "gallery_wide", "w2": "gallery_w2"}
# Reihenfolge im Auswahldialog / Dropdown (Werte wie oben; neue Stile hinten angehaengt,
# damit gespeicherte Werte aus 1.1.1 ihre Bedeutung behalten)
DISPLAY_ORDER = [0, 1, 12, 13, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
STYLE_NONE = -1                             # "ohne Rahmen" (ab 1.1.4, je Format waehlbar)

# Ab 1.1.4: Stil getrennt je Format. Einmalige Uebernahme aus "style2" (Marker "styles_migrated").
STYLE_SETTING = {"43": "style_43", "wide": "style_wide", "w2": "style_w2"}
MIGRATED = "styles_migrated"


def has_format(idx, fmt):
    """Hat Stil `idx` eine Rahmen-Datei fuer `fmt`? Eigene Datei: immer waehlbar (Datei je Format).
    Galerie-Designs (14) werden separat je Design behandelt (gallery.list_installed)."""
    if idx == STYLE_CUSTOM:
        return True
    if idx == STYLE_GALLERY:
        return False
    if not (0 <= idx < len(STYLES)) or not STYLES[idx]:
        return False
    return ("43", "wide", "w2").index(fmt) < len(STYLES[idx])


def migrate_settings(addon=None):
    """style2 (bis 1.1.3, global) -> style_43/style_wide/style_w2. Fuer 2:1 nur, wenn der Stil
    einen 2:1-Rahmen hat, sonst "ohne Rahmen" (entspricht dem bisherigen Verhalten)."""
    addon = addon or xbmcaddon.Addon()
    try:
        if addon.getSettingBool(MIGRATED):
            return False
    except Exception:
        pass
    try:
        old = int(float(addon.getSetting("style2") or 0))
    except Exception:
        old = 0
    if not (0 <= old < len(STYLES)) or (old != STYLE_CUSTOM and not STYLES[old]):
        old = 0
    addon.setSetting("style_43", str(old))
    addon.setSetting("style_wide", str(old))
    w2 = old if (old != STYLE_CUSTOM and has_format(old, "w2")) else STYLE_NONE
    addon.setSetting("style_w2", str(w2))
    try:
        addon.setSettingBool(MIGRATED, True)
    except Exception:
        addon.setSetting(MIGRATED, "true")
    xbmc.log("[service.tvframe43] Stil-Einstellungen uebernommen aus style2=%d (2:1 -> %d)" % (old, w2), xbmc.LOGINFO)
    return True


def style_for(fmt):
    """Gewaehlter Stil fuer ein Format (STYLE_NONE = ohne Rahmen)."""
    try:
        return int(float(xbmcaddon.Addon().getSetting(STYLE_SETTING[fmt]) or 0))
    except Exception:
        return 0

# Kommunikation mit dem Auswahl-Skript (default.py) ueber Eigenschaften des Home-Fensters
WINDOW_HOME = 10000
PROP_PING = "tvframe43.ping"         # geaendert -> Dienst bewertet das laufende Video neu
PROP_OFF = "tvframe43.off_for"       # nur noch zum Aufraeumen: 1.1.3 schaltete damit pro Datei ab
PROP_MEASURED = "tvframe43.measured" # ab 1.3.0: "<datei>|<verhaeltnis>" bei eingebrannten Balken (fuer default.py)

# Optionale Fernbedienungstaste: eigene Keymap-Datei (nur diese wird geschrieben/geloescht)
KEYMAP_FILE = "special://userdata/keymaps/tvframe43.xml"
_RUN = "RunScript(service.tvframe43,select)"
HOTKEYS = {
    1: '<keyboard><red>%s</red></keyboard><remote><red>%s</red></remote>' % (_RUN, _RUN),
    2: '<keyboard><menu mod="longpress">%s</menu></keyboard><remote><menu mod="longpress">%s</menu></remote>' % (_RUN, _RUN),
    3: ('<keyboard><return mod="longpress">%s</return><enter mod="longpress">%s</enter></keyboard>'
        '<remote><select mod="longpress">%s</select></remote>') % (_RUN, _RUN, _RUN),
}


def keymap_xml(choice):
    body = HOTKEYS.get(choice)
    if not body:
        return None
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!-- angelegt von service.tvframe43 (Einstellung '
            '"Taste fuer Rahmen-Auswahl"); wird beim Abschalten wieder entfernt -->\n'
            '<keymap><FullscreenVideo>%s</FullscreenVideo></keymap>\n' % body)


def apply_keymap():
    """Eigene Keymap schreiben/entfernen. Fremde Keymaps werden nie angefasst."""
    try:
        choice = int(setting_num("hotkey", 0))
    except Exception:
        choice = 0
    path = xbmcvfs.translatePath(KEYMAP_FILE)
    want = keymap_xml(choice)
    have = None
    if xbmcvfs.exists(path):
        try:
            with xbmcvfs.File(path) as f:
                have = f.read()
        except Exception:
            have = ""
    if want == have or (want is None and have is None):
        return False
    if want is None:
        xbmcvfs.delete(path)
        log("Keymap entfernt: %s" % path)
    else:
        folder = os.path.dirname(path)
        if not xbmcvfs.exists(folder + "/"):
            xbmcvfs.mkdirs(folder)
        with xbmcvfs.File(path, "w") as f:
            f.write(want)
        log("Keymap geschrieben (%s): %s" % (choice, path))
    xbmc.executebuiltin("Action(reloadkeymaps)")
    return True
# JSON-Feld "outside_black" (z.B. LG CX): Flaeche ausserhalb des virtuellen Geraets ist
# bewusst RGB 0 - auf OLED verschmilzt sie mit dem echten Rahmen. Keine Sonderbehandlung noetig.
FORMATS = ("43", "wide", "w2")
SCREEN_ASPECT = 16.0 / 9.0
CUSTOM_OVERLAP = 4          # px (bezogen auf 1080p), die der Rahmen bei eigener Datei ueberdeckt

WINDOW_FULLSCREEN_VIDEO = 12005
GRACE_SECONDS = 20.0        # so lange wird unser Zoom notfalls erneut gesetzt
MAX_REAPPLY = 4
TICK = 0.5
# Solange Kodi Pause-/Info-/OSD-Anzeigen im Vollbild zeigt, Rahmen ausblenden (1.1.3)
OSD_CONDITION = ("Player.Paused | Player.ShowInfo | Window.IsVisible(videoosd) | "
                 "Window.IsVisible(fullscreeninfo) | Window.IsVisible(seekbar)")


def log(msg, level=xbmc.LOGINFO):
    xbmc.log("[%s] %s" % (ADDON_ID, msg), level)


def dbg(msg):
    if setting_bool("debug"):
        log(msg)


def setting_bool(key, default=False):
    try:
        return xbmcaddon.Addon().getSettingBool(key)
    except Exception:
        return default


def setting_num(key, default):
    try:
        v = xbmcaddon.Addon().getSetting(key)
        return float(str(v).replace(",", ".")) if v not in (None, "") else default
    except Exception:
        return default


def setting_str(key):
    try:
        return xbmcaddon.Addon().getSetting(key) or ""
    except Exception:
        return ""


def rpc(method, params=None):
    req = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        req["params"] = params
    try:
        resp = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
    except Exception as e:  # pragma: no cover
        log("JSON-RPC %s fehlgeschlagen: %s" % (method, e), xbmc.LOGWARNING)
        return None
    if "error" in resp:
        dbg("JSON-RPC %s Fehler: %s" % (method, resp["error"]))
        return None
    return resp.get("result")


_SKIN_RES = {}


def skin_resolution():
    """Koordinatensystem des Skins (Python-Controls auf Skin-Fenstern nutzen es)."""
    skin = xbmc.getSkinDir()
    if skin not in _SKIN_RES:
        _SKIN_RES[skin] = _read_skin_resolution()
    return _SKIN_RES[skin]


def _read_skin_resolution():
    try:
        path = xbmcvfs.translatePath("special://skin/addon.xml")
        with xbmcvfs.File(path) as f:
            xml = f.read()
        res = re.findall(r"<res\b[^>]*>", xml)
        pick = None
        for r in res:
            if 'default="true"' in r:
                pick = r
                break
        pick = pick or (res[0] if res else None)
        if pick:
            w = int(re.search(r'width="(\d+)"', pick).group(1))
            h = int(re.search(r'height="(\d+)"', pick).group(1))
            return w, h
    except Exception as e:
        dbg("Skin-Aufloesung nicht lesbar: %s" % e)
    return 1920, 1080


def to_float(s):
    try:
        v = float(str(s).replace(",", "."))
        return v if v > 0 else None
    except Exception:
        return None


def video_ratio():
    """Anzeige-Seitenverhaeltnis (DAR) des laufenden Videos, robust ermittelt."""
    try:
        r = to_float(xbmc.RenderCapture().getAspectRatio())
        if r and 0.5 < r < 4.0:
            return r, "render"
    except Exception:
        pass
    r = to_float(xbmc.getInfoLabel("VideoPlayer.VideoAspect"))
    if r and 0.5 < r < 4.0:
        return r, "infolabel"
    res = rpc("Player.GetProperties", {"playerid": 1, "properties": ["currentvideostream"]})
    try:
        vs = res["currentvideostream"]
        w, h = float(vs.get("width") or 0), float(vs.get("height") or 0)
        if w > 0 and h > 0:
            return w / h, "stream"
    except Exception:
        pass
    return None, None


# --- Geometrie ----------------------------------------------------------------

def compute_view(frame, ratio, container=None):
    """Zoom + verticalshift (siehe compute_layout, ohne Rahmen-Versatz)."""
    z, sh, _ = compute_layout(frame, ratio, container)
    return z, sh


def compute_layout(frame, ratio, container=None):
    """Zoom, verticalshift und Rahmen-Versatz, damit ein Video mit DAR `ratio` die Mattscheibe deckt.

    Kodi zeigt bei Zoom 1.0 das Video eingepasst und mittig: schmaler als der
    Bildschirm -> volle Hoehe, breiter -> volle Breite. Custom-Zoom skaliert das
    zentriert. Alle Masse als Anteil des Bildschirms (unabhaengig von Aufloesung).
    Rueckgabe (zoom, shift, oy): oy = Verschiebung des Rahmen-Overlays als Anteil der
    Bildschirmhoehe (+ = nach unten), 0 ausser bei eingebrannten Balken.

    `container` (ab 1.3.0): Seitenverhaeltnis, das Kodi skaliert, wenn es vom Bildinhalt
    abweicht - bei eingebrannten Balken 1.778 (16:9-Bild), `ratio` = gemessener Inhalt.

    Rechnung (A = 16/9 Bildschirm, z = Zoom, Anteile der Bildschirmhoehe/-breite):
      Container bei Zoom 1:   B_c = 1,          H_c = A / c        (c >= A)
      Inhalt (ohne Balken):   B   = B_c,        H   = H_c * c / r = A / r
      -> Breite/Hoehe des INHALTS sind bei echtem 2,35-Container (c = r = 2,35) und bei
         2,35 im 16:9-Bild (c = 1,778, r = 2,35) identisch, also auch derselbe Zoom
         z = max(need_w / B, need_h / H).
      Versatz: Kodi (CBaseRenderer::CalcDestRect) verschiebt den CONTAINER um
         |s| <= 1:  s * bb,  bb = max(0, (1 - z*H_c) / 2)   (schwarzer Rand des Containers)
         |s| >  1:  zusaetzlich (|s| - 1) * sR -> Container ragt aus dem Bildschirm.
    1.3.1: Beim 16:9-Container ist z*H_c ~ 1 -> bb ~ 0; die Mattscheibe liesse sich nur mit
      |s| > 1 erreichen. Das rechnet Kodi zwar richtig, aber auf der Box (Android, MediaCodec
      Surface, Dolby Vision) bleibt das Bild dann einfach mittig (Befund Oblivion 23.11.2026).
      Deshalb bei eingebrannten Balken: Video nur innerhalb des Bildschirms verschieben
      (|s| <= 1) und den Rest ueber das Rahmen-Overlay ausgleichen - der Rahmen wandert
      dorthin, wo der Bildinhalt tatsaechlich liegt. Fuer c == r unveraendert.
    1.3.2: Der verschobene Rahmen schnitt unten Standfuss/Soundbar ab. Deshalb gibt es je Stil
      (soweit vorhanden) eine Variante "<stamm>_c" (Feld "centered" im JSON bzw. in design.json),
      erzeugt mit _build/make_centered.py: gleiche Mattscheibe w x h, aber y = (H - h) / 2, also
      Mitte exakt auf der Bildmitte (dy ~ 0). Dann gilt hier: shift = 0, oy = 0, und der Zoom ist
      derselbe wie oben (Breite/Hoehe des Inhalts haengen nicht vom Container ab). Beispiel LG CX
      Gehaeuse (Mattscheibe 1868 x 788, Design ~2,37:1) im 1080p-Raster:
        need_w = 1880/1920 = 0,9792   need_h = 800/1080 = 0,7407
        2,39 im 16:9-Bild: Inhalt 1920 x 803 bei Zoom 1 -> z = max(0,9792, 0,7407/0,7439) = 0,9958
                          -> Inhalt 1912 x 800, mittig y 140..940; Mattscheibe 146..934 (+6 Ueberlappung)
        2,35 / 2,40 analog (z 0,9792 bzw. 0,9999), 2,2: z = 0,9792, Inhalt 855 hoch -> oben/unten
        je ~33 px unter Rahmen + deckend schwarzem Bereich ueber/unter dem Gehaeuse (outside_black).
      Ein Rahmen je Format reicht also; der Zoom gleicht die Seitenverhaeltnisse 2,2..2,40 (bzw.
      1,92..2,05 beim 2:1-Rahmen) aus. Echte Breitbild-Container und 4:3 nutzen weiter die
      normalen Rahmen (dort verschiebt Kodi das Bild korrekt per verticalshift).
    """
    cw, ch = float(frame["canvas"][0]), float(frame["canvas"][1])
    sc = frame["screen"]
    ov = float(frame.get("overlap", 0))
    need_w = (sc["w"] + 2 * ov) / cw
    need_h = (sc["h"] + 2 * ov) / ch
    cont = container or ratio
    if cont <= SCREEN_ASPECT:
        cbw, cbh = cont / SCREEN_ASPECT, 1.0
    else:
        cbw, cbh = 1.0, SCREEN_ASPECT / cont
    if ratio >= cont:        # Inhalt niedriger als Container (Balken oben/unten) bzw. gleich
        bw, bh = cbw, cbh * cont / ratio
    else:                    # Inhalt schmaler (Balken links/rechts) - derzeit nicht genutzt
        bw, bh = cbw * ratio / cont, cbh
    zoom = max(need_w / bw, need_h / bh)
    # vertikaler Versatz der Mattscheibe gegenueber der Bildmitte (Anteil, + = nach unten)
    dy = ((sc["y"] + sc["h"] / 2.0) - ch / 2.0) / ch
    shift, oy = 0.0, 0.0
    if abs(dy) > 0.002:
        vh = cbh * zoom      # Hoehe des CONTAINERS (Kodi rechnet den Versatz damit)
        if abs(cont - ratio) < 1e-6:
            # Kodi: verticalshift verschiebt um shift * (freie Hoehe)/2
            free = 1.0 - vh
            shift = (2.0 * dy / free) if free > 0.01 else 2.0 * dy
            # Kodi (CalcDestRect): |shift| <= 1 verschiebt innerhalb der Balken, darueber
            # wird anders gerechnet -> auf den linearen Bereich begrenzen
            shift = max(-1.0, min(1.0, shift))
        else:
            bb = max(0.0, (1.0 - vh) / 2.0)
            if bb > 0.002:
                shift = max(-1.0, min(1.0, dy / bb))
            # Rest (Mattscheibe weiter vom Bildinhalt entfernt als der Container-Rand
            # erlaubt) -> Rahmen verschieben: Mattscheibenmitte auf die Inhaltsmitte
            oy = -(dy - shift * bb)
            if abs(oy) < 0.5 / ch:
                oy = 0.0
    return round(zoom, 4), round(shift, 4), round(oy, 5)


# --- PNG-Alpha lesen (eigene Datei, ohne PIL) ------------------------------------

def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def png_alpha(data):
    """-> (breite, hoehe, alpha_zeilen[list of bytes]) fuer 8-Bit-PNG mit Alpha."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("keine PNG-Datei")
    pos, idat, trns = 8, [], None
    w = h = depth = ctype = interlace = None
    while pos < len(data):
        ln, typ = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif typ == b"IDAT":
            idat.append(chunk)
        elif typ == b"tRNS":
            trns = chunk
        elif typ == b"IEND":
            break
    if depth != 8 or interlace:
        raise ValueError("nur 8 Bit, nicht interlaced (Tiefe %s, interlace %s)" % (depth, interlace))
    bpp = {6: 4, 4: 2, 2: 3, 0: 1, 3: 1}.get(ctype)
    if bpp is None:
        raise ValueError("Farbtyp %s nicht unterstuetzt" % ctype)
    if ctype in (2, 0) or (ctype == 3 and not trns):
        raise ValueError("PNG hat keinen Alphakanal")
    raw = zlib.decompress(b"".join(idat))
    stride = w * bpp
    # Filter wirken byteweise mit Abstand bpp -> der Alphakanal haengt nur von
    # Alpha-Bytes ab. Es reicht, diesen einen Kanal zu entfiltern (4x schneller).
    off = {6: 3, 4: 1, 3: 0}[ctype]
    tab = None
    if ctype == 3:
        tab = bytes(trns[:256]) + b"\xff" * (256 - len(trns[:256]))
    prev = bytearray(w)
    rows = []
    p = 0
    rng = range(1, w)
    for _ in range(h):
        f = raw[p]
        cur = bytearray(raw[p + 1 + off:p + 1 + stride:bpp])
        p += 1 + stride
        if f == 1:
            for i in rng:
                cur[i] = (cur[i] + cur[i - 1]) & 255
        elif f == 2:
            cur = bytearray((c + u) & 255 for c, u in zip(cur, prev))
        elif f == 3:
            cur[0] = (cur[0] + (prev[0] >> 1)) & 255
            for i in rng:
                cur[i] = (cur[i] + ((cur[i - 1] + prev[i]) >> 1)) & 255
        elif f == 4:
            cur[0] = (cur[0] + prev[0]) & 255
            for i in rng:
                cur[i] = (cur[i] + _paeth(cur[i - 1], prev[i], prev[i - 1])) & 255
        rows.append(bytes(cur).translate(tab) if tab else bytes(cur))
        prev = cur
    return w, h, rows


def find_screen(w, h, rows, thresh=128, step=4):
    """Groesste zusammenhaengende transparente Flaeche -> Rechteck (x, y, w, h)."""
    gw, gh = (w + step - 1) // step, (h + step - 1) // step
    grid = [[rows[min(h - 1, gy * step)][min(w - 1, gx * step)] < thresh for gx in range(gw)] for gy in range(gh)]
    seen = [[False] * gw for _ in range(gh)]
    best = None
    for sy in range(gh):
        for sx in range(gw):
            if not grid[sy][sx] or seen[sy][sx]:
                continue
            stack = [(sx, sy)]
            seen[sy][sx] = True
            n, x0, y0, x1, y1 = 0, sx, sy, sx, sy
            while stack:
                x, y = stack.pop()
                n += 1
                x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < gw and 0 <= ny < gh and grid[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            if best is None or n > best[0]:
                best = (n, x0, y0, x1, y1)
    if best is None or best[0] < 50:
        raise ValueError("keine transparente Mattscheibe gefunden")
    _, x0, y0, x1, y1 = best
    # Feinabgleich in voller Aufloesung entlang der Mittellinien
    cy = min(h - 1, ((y0 + y1) * step) // 2)
    cx = min(w - 1, ((x0 + x1) * step) // 2)
    row = rows[cy]
    l = r = cx
    while l > 0 and row[l - 1] < thresh:
        l -= 1
    while r < w - 1 and row[r + 1] < thresh:
        r += 1
    t = b = cy
    while t > 0 and rows[t - 1][cx] < thresh:
        t -= 1
    while b < h - 1 and rows[b + 1][cx] < thresh:
        b += 1
    return l, t, r - l + 1, b - t + 1


def analyse_custom(path):
    with xbmcvfs.File(path) as f:
        data = f.readBytes()
    data = bytes(data)
    w, h, rows = png_alpha(data)
    x, y, sw, shh = find_screen(w, h, rows)
    k = 1080.0 / h
    return {"id": "custom", "name": "Eigene Datei", "image": path, "canvas": [w, h],
            "screen": {"x": x, "y": y, "w": sw, "h": shh}, "overlap": CUSTOM_OVERLAP / k}


class FrameStore(object):
    """Liefert die Rahmen-Beschreibung je Format; eigene Dateien werden gecacht."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = self._load_cache()

    def _load_cache(self):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cache(self):
        try:
            if not os.path.isdir(PROFILE):
                os.makedirs(PROFILE)
            with open(CACHE_FILE, "w") as f:
                json.dump(self.cache, f)
        except Exception as e:
            dbg("Cache nicht schreibbar: %s" % e)

    @staticmethod
    def _key(path):
        st = xbmcvfs.Stat(path)
        return "%s|%s|%s" % (path, st.st_size(), st.st_mtime())

    def custom(self, path):
        if not path or not xbmcvfs.exists(path):
            return None
        with self.lock:
            key = self._key(path)
            hit = self.cache.get(key)
            if hit is None:
                t0 = time.time()
                try:
                    hit = analyse_custom(path)
                except Exception as e:
                    log("Eigene Datei %s unbrauchbar: %s" % (path, e), xbmc.LOGWARNING)
                    hit = {"error": str(e)}
                log("Eigene Datei analysiert in %.1f s: %s" % (time.time() - t0, hit.get("screen", hit.get("error"))))
                self.cache = {k: v for k, v in self.cache.items() if not k.startswith(path + "|")}
                self.cache[key] = hit
                self._save_cache()
            return None if "error" in hit else hit

    def template(self, name):
        try:
            with open(os.path.join(FRAMES_DIR, name + ".json")) as f:
                meta = json.load(f)
            meta["image"] = os.path.join(FRAMES_DIR, meta.get("image", name + ".png"))
            return meta
        except Exception as e:
            log("Vorlage %s fehlt: %s" % (name, e), xbmc.LOGERROR)
            return None

    @staticmethod
    def gallery(did, fmt):
        """Installiertes Galerie-Design (Profilordner designs/<id>/), gleiche Struktur wie template()."""
        fr = gallery.frame_for(DESIGNS_DIR, did, fmt)
        if fr is None:
            log("Galerie-Design '%s' fuer %s nicht installiert oder ohne dieses Format" % (did, fmt),
                xbmc.LOGWARNING)
        return fr

    def centered(self, frame, fmt):
        """1.3.2: Variante mit mittiger Mattscheibe fuer eingebrannte Balken, sonst None.
        Vorlagen: Feld "centered" im JSON (Datei-Stamm, z.B. lg_cx_wide_body_c); Galerie:
        formats.<fmt>.centered in design.json; eigene Dateien: keine."""
        if not frame:
            return None
        fid = str(frame.get("id") or "")
        if fid.startswith("dl:"):
            if not frame.get("centered"):
                return None
            return gallery.frame_for(DESIGNS_DIR, fid[3:].split("/")[0], fmt, centered=True)
        name = frame.get("centered")
        if not name or not isinstance(name, str):
            return None
        return self.template(name)

    def get(self, fmt):
        idx = style_for(fmt)
        if idx == STYLE_NONE:
            return None          # "ohne Rahmen" fuer dieses Format
        if idx == STYLE_CUSTOM:
            return self.custom(setting_str("custom_" + fmt))
        if idx == STYLE_GALLERY:
            return self.gallery(setting_str(GALLERY_SETTING[fmt]), fmt)
        pair = STYLES[idx] if 0 <= idx < len(STYLES) and STYLES[idx] else STYLES[0]
        k = FORMATS.index(fmt)
        if k >= len(pair):
            return None          # Stil hat fuer dieses Format keinen Rahmen
        return self.template(pair[k])

    def prefetch(self):
        """Eigene Dateien im Hintergrund vorab analysieren (z.B. nach Einstellungsaenderung)."""
        for fmt in FORMATS:
            if style_for(fmt) != STYLE_CUSTOM:
                continue
            p = setting_str("custom_" + fmt)
            if p:
                threading.Thread(target=self.custom, args=(p,)).start()


class Overlay(object):
    def __init__(self):
        # 1.1.3: EIN Fenster-Objekt fuer add/remove/getControl - Kodi entfernt nur Controls, die
        # dasselbe xbmcgui.Window-Objekt angelegt hat; mit neuem Objekt schlaegt removeControl
        # still fehl und der Rahmen bleibt liegen
        self.win = xbmcgui.Window(WINDOW_FULLSCREEN_VIDEO)
        self.ctrl = None
        self.ctrl_id = None
        self.image = None
        self.offset = 0          # 1.3.1: senkrechter Versatz in Skin-Pixeln (+ = nach unten)

    def _attached(self):
        if self.ctrl_id is None:
            return False
        try:
            self.win.getControl(self.ctrl_id)
            return True
        except Exception:
            return False

    def show(self, image, oy=0.0):
        """`oy`: Versatz als Anteil der Bildschirmhoehe (1.3.1, eingebrannte Balken)."""
        sw, sh = skin_resolution()
        offset = int(round(oy * sh))
        if self.image == image and self.offset == offset and self._attached():
            return
        self.hide()
        if not xbmcvfs.exists(image):
            log("Overlay fehlt: %s" % image, xbmc.LOGERROR)
            return
        try:
            ctrl = xbmcgui.ControlImage(0, offset, sw, sh, image, aspectRatio=0)
            self.win.addControl(ctrl)
            self.ctrl, self.ctrl_id, self.image, self.offset = ctrl, ctrl.getId(), image, offset
            log("Overlay eingeblendet: %s (Skin %dx%d, id %s%s)" % (
                os.path.basename(image), sw, sh, self.ctrl_id, (", Versatz %+d px" % offset) if offset else ""))
        except Exception as e:
            log("Overlay konnte nicht eingeblendet werden: %s" % e, xbmc.LOGWARNING)

    def set_visible(self, visible):
        # Rahmen nur ausblenden (nicht entfernen), z.B. solange Kodis Pause-/Info-Anzeige
        # im Vollbild-Fenster liegt - sonst verdeckt der Rahmen Vorschaubild und Infos (1.1.3)
        if self.ctrl is not None and visible != getattr(self, "_visible", True):
            try:
                self.ctrl.setVisible(visible)
                self._visible = visible
                dbg("Overlay %s" % ("sichtbar" if visible else "ausgeblendet (Info/Pause)"))
            except Exception:
                pass

    def hide(self):
        self._visible = True
        if self.ctrl is not None:
            try:
                self.win.removeControl(self.ctrl)
            except Exception as e:
                log("Rahmen entfernen fehlgeschlagen: %s" % e, xbmc.LOGWARNING)
            log("Overlay entfernt")
        self.ctrl = None
        self.ctrl_id = None
        self.image = None
        self.offset = 0


class TVFramePlayer(xbmc.Player):
    def __init__(self, service):
        xbmc.Player.__init__(self)
        self.service = service

    def onAVStarted(self):
        self.service.request("evaluate")

    def onAVChange(self):
        self.service.request("evaluate")

    def onPlayBackStarted(self):
        self.service.request("newitem")

    def onPlayBackStopped(self):
        self.service.request("stop")

    def onPlayBackEnded(self):
        self.service.request("stop")

    def onPlayBackError(self):
        self.service.request("stop")


class TVFrameMonitor(xbmc.Monitor):
    def __init__(self, service):
        xbmc.Monitor.__init__(self)
        self.service = service

    def onSettingsChanged(self):
        self.service.request("settings")


class Service(object):
    def __init__(self):
        self.monitor = TVFrameMonitor(self)
        self.player = TVFramePlayer(self)
        self.overlay = Overlay()
        self.frames = FrameStore()
        self.pending = []
        self.last_ping = xbmcgui.Window(WINDOW_HOME).getProperty(PROP_PING)
        # 1.3.0: Balken-Messung - Zwischenspeicher und laufende Messung (unabhaengig von
        # reset_state(), weil deactivate() waehrend der Messung aufgerufen wird)
        self.lb_cache = letterbox.LetterboxCache(LETTERBOX_FILE)
        self.detector = None
        self.lb_logged = None
        self.reset_state()

    def reset_state(self):
        self.active = False          # aktuelles Video hat Rahmen-Format und wir sind zustaendig
        self.fmt = None              # "43" / "wide" / "w2"
        self.frame = None
        self.target_zoom = None
        self.target_shift = 0.0
        self.target_oy = 0.0         # 1.3.1: Rahmen-Versatz (Anteil Bildschirmhoehe)
        self.applied_sig = None      # 1.3.1: zuletzt angewandtes Ergebnis (gegen Rueckkopplung)
        self.applied_at = 0.0
        self.reapplied = 0
        self.saved_viewmode = None
        self.eval_tries = 0
        self.evaluating = False

    def request(self, what):
        self.pending.append(what)

    # --- Ansicht -------------------------------------------------------------
    def get_viewmode(self):
        return rpc("Player.GetViewMode")

    def set_view(self):
        rpc("Player.SetViewMode", {"viewmode": {"zoom": self.target_zoom, "pixelratio": 1.0,
                                                 "verticalshift": self.target_shift,
                                                 "nonlinearstretch": False}})

    def is_ours(self, vm):
        return bool(vm) and self.target_zoom is not None and \
            str(vm.get("viewmode")) == "custom" and abs(float(vm.get("zoom", 0)) - self.target_zoom) < 0.011 and \
            ("verticalshift" not in vm or abs(float(vm.get("verticalshift") or 0) - self.target_shift) < 0.02)

    def restore_viewmode(self):
        vm = self.get_viewmode()
        if not self.is_ours(vm):
            return
        sv = self.saved_viewmode or {"viewmode": "normal"}
        # 1.1.3: Kodi merkt sich die Ansicht pro Datei - die "gesicherte" custom-Ansicht stammt
        # dann oft von uns selbst (frueherer Stil). Verkleinert/verschoben -> auf normal zurueck.
        if str(sv.get("viewmode")) == "custom" and (
                abs(float(sv.get("zoom", 1) or 1) - 1.0) > 0.01 or abs(float(sv.get("verticalshift", 0) or 0)) > 0.01):
            sv = {"viewmode": "normal"}
        if str(sv.get("viewmode")) == "custom":
            rpc("Player.SetViewMode", {"viewmode": {k: sv[k] for k in
                                                    ("zoom", "pixelratio", "verticalshift", "nonlinearstretch") if k in sv}})
        else:
            rpc("Player.SetViewMode", {"viewmode": sv.get("viewmode", "normal")})
        log("Ansicht zurueckgesetzt auf %s" % sv.get("viewmode"))

    # --- Logik ---------------------------------------------------------------
    def eligible(self):
        if not self.player.isPlayingVideo():
            return False
        if not setting_bool("include_livetv", False) and (
                xbmc.getCondVisibility("Pvr.IsPlayingTv") or xbmc.getCondVisibility("Pvr.IsPlayingRadio")):
            return False
        return True

    @staticmethod
    def classify(ratio):
        if setting_num("min_ratio", 1.20) <= ratio <= setting_num("max_ratio", 1.45):
            return "43"
        if setting_bool("enable_wide", True) and \
                setting_num("wide_min", 2.10) <= ratio <= setting_num("wide_max", 2.50):
            return "wide"
        if setting_bool("enable_w2", True) and \
                setting_num("w2_min", 1.90) <= ratio <= setting_num("w2_max", 2.08):
            return "w2"
        return None

    # --- Eingebrannte Balken (1.3.0) ---------------------------------------------
    def stop_detector(self):
        if self.detector is not None:
            self.detector.stop()
            self.detector = None
        self.lb_logged = None
        xbmcgui.Window(WINDOW_HOME).clearProperty(PROP_MEASURED)

    def _detector_done(self, det):
        # aus dem Mess-Thread: nur vormerken, ausgewertet wird im Haupt-Takt
        if det is self.detector:
            self.request("letterbox")

    def _render_allowed(self):
        """Live-Proben nur, wenn das Video unverzerrt und ohne Anzeigen darueber laeuft."""
        if self.active or not self.player.isPlayingVideo() or xbmc.getCondVisibility(OSD_CONDITION):
            return False
        vm = self.get_viewmode() or {}
        try:
            return str(vm.get("viewmode")) in ("normal", "original", "custom") and \
                abs(float(vm.get("zoom", 1) or 1) - 1.0) < 0.01 and abs(float(vm.get("verticalshift", 0) or 0)) < 0.01
        except Exception:
            return False

    def letterbox_ratio(self, reported):
        """Gemessenes Seitenverhaeltnis des Bildinhalts bei eingebrannten Balken, sonst None.
        Startet bei Bedarf eine Messung im Hintergrund (Ergebnis kommt spaeter per "letterbox")."""
        if not setting_bool("detect_bars", True) or not (LB_MIN <= reported <= LB_MAX):
            return None
        try:
            path = self.player.getPlayingFile()
            dur = float(self.player.getTotalTime() or 0)
        except Exception:
            return None
        if not letterbox.measurable_path(path) or xbmc.getCondVisibility("Pvr.IsPlayingTv"):
            return None
        if dur < letterbox.MIN_DURATION:
            if self.lb_logged != path:
                self.lb_logged = path
                log("Balken-Messung: Laufzeit %d s zu kurz oder unbekannt - keine Messung" % dur)
            return None
        key = letterbox.cache_key(path, dur)
        win = xbmcgui.Window(WINDOW_HOME)
        hit = self.lb_cache.get(key)
        if hit and hit.get("state") in ("bars", "full") or \
                (hit and hit.get("state") == "unsure" and int(hit.get("tries", 0)) >= letterbox.UNSURE_RETRY):
            if self.lb_logged != key:
                self.lb_logged = key
                log("Balken-Messung aus Zwischenspeicher: %s -> %s %s" % (key, hit.get("state"), hit.get("ratio") or ""))
            if hit.get("state") == "bars" and hit.get("ratio"):
                r = float(hit["ratio"])
                win.setProperty(PROP_MEASURED, "%s|%.3f" % (path, r))
                return r
            win.clearProperty(PROP_MEASURED)
            return None
        det = self.detector
        if det is not None and det.key == key:
            if det.done and det.result[0] == "bars":
                win.setProperty(PROP_MEASURED, "%s|%.3f" % (path, det.result[1]))
                return det.result[1]
            return None          # Messung laeuft noch bzw. ergab nichts
        if det is not None:
            det.stop()
        try:
            chapters = int(xbmc.getInfoLabel("Player.ChapterCount") or 0)
        except Exception:
            chapters = 0
        self.detector = letterbox.Detector(path, reported, dur, chapters, self.lb_cache, self._detector_done,
                                           lambda m: log(m), render_allowed=self._render_allowed, rpc=rpc)
        self.lb_logged = key
        log("Gemeldet %.3f (16:9) - suche eingebrannte Balken im Hintergrund, bis dahin kein Rahmen" % reported)
        self.detector.start()
        return None

    def reset_foreign_zoom(self, fmt, ratio, container=None):
        """1.1.4, "ohne Rahmen": Kodi merkt sich die Ansicht pro Datei. Lief die Datei frueher mit
        Rahmen, startet sie mit unserem alten Zoom. Nur wenn die Ansicht exakt zu einem unserer
        Rahmen passt, auf normal zuruecksetzen - fremde Zoom-Einstellungen bleiben unberuehrt."""
        vm = self.get_viewmode()
        if not vm or str(vm.get("viewmode")) != "custom":
            return
        try:
            z, sh = float(vm.get("zoom", 1) or 1), float(vm.get("verticalshift", 0) or 0)
        except Exception:
            return
        cands = []
        override = setting_num("zoom_override_" + fmt, 0.0)
        if override > 0:
            cands.append((round(min(1.0, max(0.3, override)), 4), None))
        k = FORMATS.index(fmt)
        for st in set(s[k] for s in STYLES if s and k < len(s)):
            fr = self.frames.template(st)
            for f in (fr, self.frames.centered(fr, fmt)):     # 1.3.2: auch mittige Variante
                if f:
                    cands.append(compute_view(f, ratio, container))
        try:
            for d in gallery.list_installed(DESIGNS_DIR):
                fr = gallery.frame_for(DESIGNS_DIR, d["id"], fmt)
                for f in (fr, self.frames.centered(fr, fmt)):
                    if f:
                        cands.append(compute_view(f, ratio, container))
        except Exception:
            pass
        legacy = container is not None and abs(container - ratio) > 1e-6
        for zc, shc in cands:
            # 1.3.0 hat bei eingebrannten Balken |verticalshift| > 1 gesetzt (gleicher Zoom)
            if abs(z - zc) < 0.011 and (shc is None or abs(sh - shc) < 0.02 or (legacy and abs(sh) > 1.0)):
                rpc("Player.SetViewMode", {"viewmode": "normal"})
                log("Ohne Rahmen: alte Rahmen-Ansicht (Zoom %.4f) auf normal zurueckgesetzt" % z)
                return

    def evaluate(self):
        if not setting_bool("enabled", True) or not self.eligible():
            self.deactivate(restore=self.active)
            return True
        ratio, src = video_ratio()
        if ratio is None:
            return False  # spaeter nochmal
        container = ratio
        lb = self.letterbox_ratio(ratio)
        lb_msg = None
        if lb:
            # 1.3.0: eingebrannte Balken - Format nach Bildinhalt, Zoom/Versatz fuer 16:9-Container
            lb_msg = "Gemeldet %.3f, eingebrannte Balken gemessen -> Bildinhalt %.3f" % (ratio, lb)
            ratio, src = lb, src + "+balken"
        fmt = self.classify(ratio)
        if fmt and style_for(fmt) == STYLE_NONE:
            # derselbe Aufraeumpfad wie frueher "Rahmen aus": Overlay ueber das eine Overlay-Objekt
            # entfernen, eigene Ansicht zuruecknehmen
            log("Seitenverhaeltnis %.3f (%s) -> %s, ohne Rahmen gewaehlt" % (ratio, src, fmt))
            was_active = self.active
            self.deactivate(restore=was_active)
            if not was_active and self.player.isPlayingVideo():
                self.reset_foreign_zoom(fmt, ratio, container)
            return True
        frame = self.frames.get(fmt) if fmt else None
        if fmt == "w2" and frame is None:
            fmt = None           # 2:1 nur bei Stilen mit eigenem 2:1-Rahmen
        if fmt and frame is None:
            log("Seitenverhaeltnis %.3f (%s) -> %s, aber kein Rahmen verfuegbar" % (ratio, src, fmt))
        if not frame:
            if not fmt:
                log("Seitenverhaeltnis %.3f (%s) -> nichts zu tun" % (ratio, src))
            self.deactivate(restore=self.active)
            return True
        if lb and abs(container - ratio) > 1e-6:
            # 1.3.2: eingebrannte Balken -> Rahmenvariante mit mittiger Mattscheibe (Bild bleibt
            # mittig, nichts wird verschoben). Ohne Variante: Verhalten von 1.3.1 (Rahmen-Versatz).
            cen = self.frames.centered(frame, fmt)
            if cen:
                frame = cen
            elif lb_msg:
                lb_msg += " (keine mittige Rahmenvariante - Rahmen wird verschoben)"
        zoom, shift, oy = compute_layout(frame, ratio, container)
        override = setting_num("zoom_override_" + fmt, 0.0)
        if override > 0:
            zoom = round(min(1.0, max(0.3, override)), 4)
        vm = self.get_viewmode()
        # 1.3.1: Kodi meldet jedes SetViewMode per onAVChange zurueck -> ohne diese Pruefung
        # wurde alle 0,6 s neu bewertet und neu gesetzt. Unveraendert und noch unsere Ansicht
        # -> nichts tun.
        sig = (fmt, frame.get("id"), frame.get("image"), zoom, shift, oy)
        if self.active and sig == self.applied_sig and self.is_ours(vm):
            return True
        if lb_msg:
            log(lb_msg)
        log("Seitenverhaeltnis %.3f (%s) -> Rahmen %s/%s, Zoom %.4f, Versatz %.4f%s%s" % (
            ratio, src, frame.get("id"), fmt, zoom, shift,
            (", Rahmen %+.4f" % oy) if oy else "", " (manuell)" if override > 0 else ""))
        if not self.active and vm and not self.is_ours(vm):
            self.saved_viewmode = vm
        if self.fmt and self.fmt != fmt:
            self.overlay.hide()
        self.active = True
        self.fmt, self.frame = fmt, frame
        self.target_zoom, self.target_shift, self.target_oy = zoom, shift, oy
        self.applied_sig = sig
        self.set_view()
        self.applied_at = time.time()
        self.reapplied = 0
        return True

    def deactivate(self, restore=False):
        self.overlay.hide()
        if restore and self.player.isPlayingVideo():
            self.restore_viewmode()
        self.reset_state()

    def tick(self):
        ping = xbmcgui.Window(WINDOW_HOME).getProperty(PROP_PING)
        if ping != self.last_ping:
            self.last_ping = ping
            if self.player.isPlayingVideo():
                self.request("evaluate")
        while self.pending:
            what = self.pending.pop(0)
            if what == "stop":
                self.stop_detector()
                self.deactivate(restore=False)
            elif what == "newitem":
                self.stop_detector()
                self.overlay.hide()
            elif what == "letterbox":
                # Messung fertig -> neu bewerten (Rahmen ggf. nachtraeglich einblenden)
                if self.player.isPlayingVideo():
                    self.evaluating = True
                    self.eval_tries = 0
            elif what in ("evaluate", "settings"):
                if what == "settings":
                    self.frames.prefetch()
                    apply_keymap()
                    if not self.player.isPlayingVideo():
                        continue
                self.evaluating = True
                self.eval_tries = 0

        if self.evaluating:
            self.eval_tries += 1
            if self.evaluate() or self.eval_tries >= 20:
                self.evaluating = False

        if self.detector is not None and not self.player.isPlayingVideo():
            self.stop_detector()
        if not self.active:
            return
        if not self.player.isPlayingVideo():
            self.deactivate(restore=False)
            return
        vm = self.get_viewmode()
        ours = self.is_ours(vm)
        if not ours and vm is not None and time.time() - self.applied_at < GRACE_SECONDS \
                and self.reapplied < MAX_REAPPLY:
            self.reapplied += 1
            log("Ansicht wurde ueberschrieben (%s) - setze Zoom erneut" % vm.get("viewmode"))
            self.set_view()
            return
        # Window.IsActive(fullscreenvideo) ist auch wahr, wenn ein Dialog (z.B. die Rahmen-Auswahl)
        # darueber liegt -> Umschalten wird sofort sichtbar
        fullscreen = xbmcgui.getCurrentWindowId() == WINDOW_FULLSCREEN_VIDEO or \
            xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)")
        want = ours and setting_bool("overlay", True) and fullscreen
        if want:
            if xbmc.getCondVisibility(OSD_CONDITION):
                # 1.1.3: Rahmen komplett entfernen (setVisible reichte bei Confluence nicht) - sonst
                # liegt er ueber Poster/Infos des Vollbild-OSD und schneidet sie an
                if self.overlay.ctrl is not None:
                    log("Info/Pause-Anzeige aktiv - Rahmen ausgeblendet")
                    self.overlay.hide()
            else:
                self.overlay.show(self.frame["image"], self.target_oy)
        elif not ours:
            self.overlay.hide()

    def run(self):
        log("gestartet (Version %s)" % ADDON.getAddonInfo("version"))
        try:
            migrate_settings()
        except Exception as e:
            log("Stil-Uebernahme fehlgeschlagen: %s" % e, xbmc.LOGWARNING)
        # "Rahmen aus" aus 1.1.3 gibt es nicht mehr; eine haengengebliebene Eigenschaft wuerde
        # sonst nichts mehr bewirken - trotzdem wegraeumen
        xbmcgui.Window(WINDOW_HOME).clearProperty(PROP_OFF)
        self.frames.prefetch()
        try:
            apply_keymap()
        except Exception as e:
            log("Keymap nicht anwendbar: %s" % e, xbmc.LOGWARNING)
        if self.player.isPlayingVideo():
            self.request("evaluate")
        while not self.monitor.abortRequested():
            try:
                self.tick()
            except Exception as e:
                log("Fehler im Ablauf: %s" % e, xbmc.LOGERROR)
            if self.monitor.waitForAbort(TICK):
                break
        try:
            self.stop_detector()
            self.deactivate(restore=self.active)
        except Exception:
            pass
        log("beendet")


def remove_orphan_overlays():
    """1.1.3: Wird der Dienst hart beendet (Update, Deaktivieren), bleiben seine per addControl
    angelegten Rahmen im Vollbild-Fenster haengen ("Geister-Rahmen"). Beim Start alle
    Python-Bildcontrols (IDs ab 3000) dort entfernen."""
    removed = 0
    try:
        win = xbmcgui.Window(WINDOW_FULLSCREEN_VIDEO)
    except Exception:
        return
    for cid in range(3000, 3400):
        try:
            c = win.getControl(cid)
        except Exception:
            continue
        if isinstance(c, xbmcgui.ControlImage):
            try:
                win.removeControl(c)
                removed += 1
            except Exception:
                pass
    if removed:
        log("%d alte Rahmen-Reste entfernt" % removed)


if __name__ == "__main__":
    remove_orphan_overlays()
    Service().run()
