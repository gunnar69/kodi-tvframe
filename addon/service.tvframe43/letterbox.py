# -*- coding: utf-8 -*-
"""service.tvframe43 (ab 1.3.0) - eingebrannte schwarze Balken (Letterbox) erkennen.

Problem: Filme in 2,35/2,39:1 liegen oft als volles 16:9-Bild vor, die Balken sind Teil des
Bildes. Kodi meldet dann 1,78 - ohne Messung bleibt der Film ohne Rahmen.

Messquellen (beide liefern ein "Zeilenprofil": je Bildzeile die Helligkeit (Luma 0-255) in
Gruppen von ~8 Spalten):
  1. "thumb"  - Kodi zieht per Software-Decoder Einzelbilder aus der Datei:
                image://video@<pfad>/            (1/3 der Laufzeit)
                image://video@<pfad>/?chapter=N  (Kapitelanfang N)
                Ergebnis landet als Baseline-JPEG (max. 720 px breit) im Texture-Cache und wird
                per xbmcvfs gelesen. Funktioniert unabhaengig vom Video-Renderer - auch mit
                MediaCodec (Surface) und HDR/Dolby Vision (Tonemapping -> Balken = RGB 0).
                Das JPEG wird NICHT voll dekodiert: fuer Zeilenmittelwerte reichen je 8x8-Block
                die 8 Koeffizienten mit horizontaler Frequenz 0 (siehe jpeg_row_profile()).
  2. "render" - xbmc.RenderCapture waehrend der Wiedergabe (BGRA). Liefert bei Renderern,
                die am Framebuffer vorbei zeichnen (Android MediaCodec Surface, Amlogic),
                KEINE Pixel (leerer Puffer) -> wird dann fuer diese Wiedergabe abgeschaltet.

Entscheidung: mehrere Proben, jede einzeln streng geprueft (tiefschwarz, symmetrisch, Bildkante
hell, nicht zu dunkel, Ergebnis rastet auf Standardwert ein), dann Konsens. Unsicher -> nichts.
"""
import json
import math
import os
import threading
import time

try:  # Kodi-Module nur im Add-on vorhanden; Analysefunktionen laufen auch ohne
    import xbmc
    import xbmcvfs
except Exception:  # pragma: no cover
    xbmc = xbmcvfs = None

# --- Schwellwerte (8 Bit, Vollbereich) ----------------------------------------------------
BLACK = 24          # Zeile gilt als Balken, wenn KEINE Spaltengruppe heller ist (HDR/DV-Schwarz
#                     nach Tonemapping 0-2, Limited-Range-Schwarz 16, JPEG-Rauschen/Ringing < 10)
BLACK_MEAN = 18     # ... und der Mittelwert der Balkenzone sehr dunkel ist (gleichmaessig schwarz;
#                     18 laesst Limited-Range-Schwarz 16 aus RenderCapture noch zu)
LIT = 40            # Zeile gilt als "Bild", wenn mindestens eine Gruppe so hell ist
EDGE_LIT = 30       # an der Balkenkante reicht etwas weniger (dunkle Bildkante, Balken <= 24)
EDGE_WIN = 0.03     # in diesem Anteil der Hoehe hinter der Balkenkante muss eine helle Zeile liegen
MIN_LIT_SHARE = 0.30  # mindestens so viele Inhaltszeilen muessen hell sein (sonst: dunkle Szene)
SYM_TOL = 0.012     # |oben - unten| <= max(2 px, 1,2 % der Hoehe)
MIN_BAR = 0.015     # Balken zusammen < 1,5 % der Hoehe -> "keine Balken"
STANDARD = (1.85, 2.00, 2.20, 2.35, 2.39, 2.40)
SNAP_TOL = 0.04     # Abstand zum naechsten Standardwert
CLUSTER_TOL = 0.06  # 2,35 / 2,39 / 2,40 gelten als dasselbe Format
MIN_AGREE_THUMB = 2   # so viele uebereinstimmende Einzelbilder braucht eine Entscheidung
MIN_AGREE_RENDER = 4  # live-Proben (Start-Logos, Titel) brauchen mehr
AGREE_SHARE = 0.75    # Anteil der gueltigen Proben, die zustimmen muessen
LAST_AGREE = 3        # Hysterese live: die letzten N gueltigen Proben muessen zustimmen

CACHE_MAX = 400     # Eintraege im Zwischenspeicher (aelteste fliegen raus)
UNSURE_RETRY = 2    # so oft wird bei "unsicher" neu gemessen, danach nicht mehr

# Video-Laufzeit, ab der gemessen wird (Trailer/Clips unter 10 Min. lohnen nicht)
MIN_DURATION = 600
# Pausen im Messablauf (Sekunden; Tests setzen sie auf 0)
PAUSE_THUMB = 1.0     # zwischen zwei Einzelbildern (Software-Decoder entlasten)
RENDER_START = 5.0    # vor der ersten live-Probe
PAUSE_RENDER = 5.0    # zwischen live-Proben
RENDER_MAX = 24       # live-Proben hoechstens (~2 Minuten)


# --- Zeilenprofile ---------------------------------------------------------------------------

def bgra_row_profile(buf, w, h, group=8):
    """BGRA-Puffer (RenderCapture) -> Zeilenprofil: je Zeile Liste der Luma-Mittel je `group` Spalten."""
    buf = bytes(buf)
    if w <= 0 or h <= 0 or len(buf) < w * h * 4:
        return None
    rows = []
    stride = w * 4
    for y in range(h):
        r = buf[y * stride:(y + 1) * stride]
        b, g, rr = r[0::4], r[1::4], r[2::4]
        row = []
        for x0 in range(0, w, group):
            x1 = min(w, x0 + group)
            n = x1 - x0
            row.append((0.299 * sum(rr[x0:x1]) + 0.587 * sum(g[x0:x1]) + 0.114 * sum(b[x0:x1])) / n)
        rows.append(row)
    return rows


class JpegError(ValueError):
    pass


# natuerliche Position (zeile*8+spalte) je Zickzack-Index
_ZIGZAG = (0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27,
           20, 13, 6, 7, 14, 21, 28, 35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
           58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63)
# Zickzack-Index der Koeffizienten F(v, 0) (vertikale Frequenz v, horizontale 0)
_COL0 = tuple(_ZIGZAG.index(8 * v) for v in range(8))
_COL0_SET = {k: v for v, k in enumerate(_COL0)}
# Zeilenmittel eines 8x8-Blocks:  m(y) = 128 + C(0)/4 * sum_v C(v) F(v,0) cos((2y+1) v pi / 16)
# (die Summe ueber x eliminiert alle Terme mit u != 0, sum_x cos((2x+1) 0) = 8)
_BASIS = [[(1.0 / math.sqrt(2) / 4.0) * ((1.0 / math.sqrt(2)) if v == 0 else 1.0) *
           math.cos((2 * y + 1) * v * math.pi / 16.0) for v in range(8)] for y in range(8)]


def _huff_table(counts, symbols):
    """-> Liste mit 65536 Eintraegen (symbol, laenge) fuer 16-Bit-Vorschau."""
    table = [None] * 65536
    code, k = 0, 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            if k >= len(symbols):
                raise JpegError("DHT kaputt")
            span = 1 << (16 - length)
            start = code << (16 - length)
            if start + span > 65536:
                raise JpegError("DHT kaputt")
            table[start:start + span] = [(symbols[k], length)] * span
            code += 1
            k += 1
        code <<= 1
    return table


def jpeg_row_profile(data):
    """Baseline-JPEG -> Zeilenprofil der Luminanz (je Zeile: Mittel je 8er-Spaltengruppe).

    Kein vollstaendiges Dekodieren: es werden alle Huffman-Symbole gelesen, aber pro Y-Block
    nur die 8 Koeffizienten F(v,0) ausgewertet -> exakte Zeilenmittel je 8 Spalten.
    Progressive/arithmetische JPEGs -> JpegError (Kodis Texture-Cache schreibt Baseline)."""
    data = bytes(data)
    if data[:2] != b"\xff\xd8":
        raise JpegError("kein JPEG")
    pos = 2
    qt, dc_t, ac_t = {}, {}, {}
    frame = None
    restart = 0
    n = len(data)
    while pos < n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9:
            break
        ln = (data[pos] << 8) | data[pos + 1]
        seg = data[pos + 2:pos + ln]
        pos += ln
        if marker == 0xDB:                       # DQT
            p = 0
            while p < len(seg):
                pq, tq = seg[p] >> 4, seg[p] & 15
                p += 1
                if pq:
                    qt[tq] = [(seg[p + 2 * i] << 8) | seg[p + 2 * i + 1] for i in range(64)]
                    p += 128
                else:
                    qt[tq] = list(seg[p:p + 64])
                    p += 64
        elif marker in (0xC0, 0xC1):             # SOF0/SOF1 (Baseline/erweitert, Huffman)
            if seg[0] != 8:
                raise JpegError("nur 8 Bit")
            h, w, nc = (seg[1] << 8) | seg[2], (seg[3] << 8) | seg[4], seg[5]
            comps = []
            for i in range(nc):
                cid, hv, tq = seg[6 + 3 * i], seg[7 + 3 * i], seg[8 + 3 * i]
                comps.append({"id": cid, "h": hv >> 4, "v": hv & 15, "tq": tq})
            frame = {"w": w, "h": h, "comps": comps}
        elif marker in (0xC2, 0xC3) or 0xC5 <= marker <= 0xCF and marker not in (0xC8, 0xCC):
            raise JpegError("JPEG-Typ %02X nicht unterstuetzt (progressiv/arithmetisch)" % marker)
        elif marker == 0xC4:                     # DHT
            p = 0
            while p < len(seg):
                tc, th = seg[p] >> 4, seg[p] & 15
                counts = list(seg[p + 1:p + 17])
                total = sum(counts)
                syms = list(seg[p + 17:p + 17 + total])
                (ac_t if tc else dc_t)[th] = _huff_table(counts, syms)
                p += 17 + total
        elif marker == 0xDD:                     # DRI
            restart = (seg[0] << 8) | seg[1]
        elif marker == 0xDA:                     # SOS
            if frame is None:
                raise JpegError("SOS vor SOF")
            ns = seg[0]
            scomps = []
            for i in range(ns):
                cid, t = seg[1 + 2 * i], seg[2 + 2 * i]
                c = next((c for c in frame["comps"] if c["id"] == cid), None)
                if c is None:
                    raise JpegError("unbekannte Komponente")
                scomps.append((c, t >> 4, t & 15))
            # Entropie-Daten bis zum naechsten Nicht-RST-Marker
            end = pos
            while True:
                end = data.find(b"\xff", end)
                if end < 0 or end + 1 >= n:
                    end = n
                    break
                nxt = data[end + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    end += 2
                    continue
                break
            return _decode_scan(data[pos:end], frame, scomps, qt, dc_t, ac_t, restart)
    raise JpegError("keine Bilddaten")


def _decode_scan(ecs, frame, scomps, qt, dc_t, ac_t, restart):
    comps = frame["comps"]
    hmax = max(c["h"] for c in comps)
    vmax = max(c["v"] for c in comps)
    w, h = frame["w"], frame["h"]
    mcux = (w + 8 * hmax - 1) // (8 * hmax)
    mcuy = (h + 8 * vmax - 1) // (8 * vmax)
    luma = comps[0]
    if len(scomps) != len(comps):
        raise JpegError("nicht verschachtelte Scans nicht unterstuetzt")
    for c, td, ta in scomps:
        if td not in dc_t or ta not in ac_t or c["tq"] not in qt:
            raise JpegError("Tabelle fehlt")
    # Segmente an RST-Markern trennen, Stopfbytes entfernen
    segments = []
    start = 0
    i = 0
    while True:
        i = ecs.find(b"\xff", i)
        if i < 0 or i + 1 >= len(ecs):
            break
        if 0xD0 <= ecs[i + 1] <= 0xD7:
            segments.append(ecs[start:i])
            start = i + 2
        i += 2
    segments.append(ecs[start:])
    segments = [s.replace(b"\xff\x00", b"\xff") + b"\x00\x00\x00\x00" for s in segments]

    bw = mcux * luma["h"]                      # Y-Bloecke je Blockzeile
    bh = mcuy * luma["v"]
    # Zeilensummen je Y-Block: out[blockzeile][blockspalte] = 8 Zeilenmittel
    out = [[None] * bw for _ in range(bh)]
    q = qt[luma["tq"]]
    qcol = [q[k] for k in _COL0]
    total_mcu = mcux * mcuy
    seg_i = 0
    data = segments[0]
    bitpos = 0
    preds = [0] * len(scomps)
    plan = [(ci, c is luma, dc_t[td], ac_t[ta], c["h"], c["v"]) for ci, (c, td, ta) in enumerate(scomps)]
    basis = _BASIS
    col0 = _COL0_SET
    for m in range(total_mcu):
        if restart and m and m % restart == 0:
            seg_i += 1
            if seg_i >= len(segments):
                raise JpegError("RST fehlt")
            data = segments[seg_i]
            bitpos = 0
            preds = [0] * len(scomps)
        my, mx = divmod(m, mcux)
        for ci, is_y, dct, act, ch, cv in plan:
            for by in range(cv):
                for bx in range(ch):
                    # --- DC
                    p = bitpos >> 3
                    if p + 3 > len(data):
                        raise JpegError("Daten zu kurz")
                    peek = (((data[p] << 16) | (data[p + 1] << 8) | data[p + 2]) >> (8 - (bitpos & 7))) & 0xFFFF
                    e = dct[peek]
                    if e is None:
                        raise JpegError("Huffman-Code ungueltig")
                    s, ln = e
                    bitpos += ln
                    diff = 0
                    if s:
                        p = bitpos >> 3
                        v = (((data[p] << 16) | (data[p + 1] << 8) | data[p + 2]) >> (8 - (bitpos & 7))) & 0xFFFF
                        v >>= 16 - s
                        bitpos += s
                        diff = v if v >= (1 << (s - 1)) else v - (1 << s) + 1
                    preds[ci] += diff
                    coefs = [0] * 8 if is_y else None
                    if is_y:
                        coefs[0] = preds[ci]
                    # --- AC
                    k = 1
                    while k < 64:
                        p = bitpos >> 3
                        peek = (((data[p] << 16) | (data[p + 1] << 8) | data[p + 2]) >> (8 - (bitpos & 7))) & 0xFFFF
                        e = act[peek]
                        if e is None:
                            raise JpegError("Huffman-Code ungueltig")
                        rs, ln = e
                        bitpos += ln
                        r, s = rs >> 4, rs & 15
                        if s == 0:
                            if r == 15:
                                k += 16
                                continue
                            break
                        k += r
                        p = bitpos >> 3
                        v = (((data[p] << 16) | (data[p + 1] << 8) | data[p + 2]) >> (8 - (bitpos & 7))) & 0xFFFF
                        v >>= 16 - s
                        bitpos += s
                        if is_y and k in col0:
                            coefs[col0[k]] = v if v >= (1 << (s - 1)) else v - (1 << s) + 1
                        k += 1
                    if is_y:
                        f = [coefs[i] * qcol[i] for i in range(8)]
                        out[my * cv + by][mx * ch + bx] = [128.0 + sum(bv[i] * f[i] for i in range(8)) for bv in basis]
    # in Zeilen umsortieren, auf echte Bildgroesse zuschneiden
    cols = (w + 7) // 8
    rows = []
    for y in range(h):
        brow = out[y >> 3]
        yy = y & 7
        rows.append([min(255.0, max(0.0, brow[x][yy])) for x in range(cols)])
    return rows


# --- Auswertung einer Probe ---------------------------------------------------------------

def analyse_profile(rows, reported_ratio):
    """Eine Probe auswerten.

    -> dict mit "kind": "bars" (eingebrannte Balken, "ratio" = gemessen, eingerastet),
                        "full" (Bild reicht bis an die Kante, keine Balken),
                        "dark" / "edge" / "asym" / "odd" / "empty" (unbrauchbar, "why").
    Ergebnis-Verhaeltnis = gemeldetes Verhaeltnis * Hoehe / (Hoehe - Balken oben - Balken unten)
    (entspricht Breite / Bildhoehe ohne Balken, aber unabhaengig von nicht-quadratischen
    Vorschaubildern wie 704x400)."""
    if not rows:
        return {"kind": "empty", "why": "keine Pixel"}
    h = len(rows)
    mx = [max(r) for r in rows]
    mean = [sum(r) / len(r) for r in rows]
    top = 0
    while top < h and mx[top] <= BLACK:
        top += 1
    if top >= h:
        return {"kind": "dark", "why": "ganzes Bild schwarz"}
    bot = 0
    while bot < h and mx[h - 1 - bot] <= BLACK:
        bot += 1
    res = {"top": top, "bottom": bot, "h": h}
    content = range(top, h - bot)
    lit = sum(1 for y in content if mx[y] >= LIT)
    if lit < MIN_LIT_SHARE * len(content):
        res.update(kind="dark", why="zu dunkel (%d/%d helle Zeilen)" % (lit, len(content)))
        return res
    win = max(2, int(round(EDGE_WIN * h)))
    if not any(mx[y] >= EDGE_LIT for y in range(top, min(h, top + win))) or \
            not any(mx[y] >= EDGE_LIT for y in range(max(0, h - bot - win), h - bot)):
        res.update(kind="edge", why="Bildkante dunkel - Balkengrenze unsicher")
        return res
    bars = [y for y in range(top)] + [h - 1 - y for y in range(bot)]
    if bars and sum(mean[y] for y in bars) / len(bars) > BLACK_MEAN:
        res.update(kind="edge", why="Balken nicht gleichmaessig schwarz")
        return res
    if top + bot < MIN_BAR * h:
        res.update(kind="full", ratio=round(reported_ratio, 3))
        return res
    if abs(top - bot) > max(2, SYM_TOL * h):
        res.update(kind="asym", why="Balken unsymmetrisch (oben %d, unten %d)" % (top, bot))
        return res
    raw = reported_ratio * h / float(h - top - bot)
    snap = min(STANDARD, key=lambda s: abs(s - raw))
    res["raw"] = round(raw, 3)
    if abs(snap - raw) > SNAP_TOL:
        res.update(kind="odd", why="%.3f passt zu keinem Standardformat" % raw)
        return res
    res.update(kind="bars", ratio=snap)
    return res


def decide(samples, min_agree, last_agree=0):
    """Konsens ueber mehrere Proben.

    -> ("bars", ratio) | ("full", None) | (None, grund)
    Nur gueltige Proben ("bars"/"full") zaehlen. Entscheidung, wenn mindestens `min_agree`
    Proben dasselbe sagen UND das mindestens AGREE_SHARE aller gueltigen Proben sind
    (+ optional die letzten `last_agree` gueltigen Proben zustimmen = Hysterese)."""
    valid = [s for s in samples if s.get("kind") in ("bars", "full")]
    if not valid:
        return None, "keine gueltige Probe (%d)" % len(samples)
    fulls = [s for s in valid if s["kind"] == "full"]
    bars = sorted(s["ratio"] for s in valid if s["kind"] == "bars")
    best, members = None, []
    for r in bars:
        cl = [x for x in bars if abs(x - r) <= CLUSTER_TOL]
        if len(cl) > len(members):
            best, members = r, cl
    need = max(min_agree, int(math.ceil(AGREE_SHARE * len(valid))))

    def tail_ok(pred):
        if not last_agree:
            return True
        tail = valid[-last_agree:]
        return len(tail) >= last_agree and all(pred(s) for s in tail)

    if members and len(members) >= need:
        med = members[len(members) // 2]
        if tail_ok(lambda s: s["kind"] == "bars" and abs(s["ratio"] - med) <= CLUSTER_TOL):
            return "bars", med
    if len(fulls) >= need and tail_ok(lambda s: s["kind"] == "full"):
        return "full", None
    return None, "kein Konsens (%d Balken %s, %d ohne, %d ungueltig)" % (
        len(bars), "/".join("%.2f" % b for b in bars), len(fulls), len(samples) - len(valid))


# --- Zwischenspeicher -------------------------------------------------------------------------

def cache_key(path, duration):
    """Dateiname (ohne Ordner, Gross/Klein egal) + Laufzeit in Sekunden."""
    name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    return "%s|%d" % (name, int(round(duration or 0)))


class LetterboxCache(object):
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = None

    def _load(self):
        if self.data is None:
            try:
                with open(self.path) as f:
                    d = json.load(f)
                self.data = d if isinstance(d, dict) else {}
            except Exception:
                self.data = {}
        return self.data

    def get(self, key):
        with self.lock:
            return self._load().get(key)

    def put(self, key, entry):
        with self.lock:
            d = self._load()
            entry = dict(entry, t=int(time.time()))
            d[key] = entry
            if len(d) > CACHE_MAX:
                for k in sorted(d, key=lambda k: d[k].get("t", 0))[:len(d) - CACHE_MAX]:
                    del d[k]
            try:
                folder = os.path.dirname(self.path)
                if folder and not os.path.isdir(folder):
                    os.makedirs(folder)
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(d, f)
                os.replace(tmp, self.path)
            except Exception:
                pass
            return entry


# --- Messablauf (Kodi) ---------------------------------------------------------------------

def kodi_url_encode(s):
    """Wie Kodis URIUtils::URLEncode: alphanumerisch und -_.!()~ bleiben, Rest %xx (klein)."""
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        if (c.isascii() and c.isalnum()) or c in "-_.!()~":
            out.append(c)
        else:
            out.append("%%%02x" % b)
    return "".join(out)


def thumb_url(path, chapter=0):
    url = "image://video@%s/" % kodi_url_encode(path)
    return url + ("?chapter=%d" % chapter) if chapter else url


def measurable_path(path):
    """Nur echte Dateien (lokal, smb, nfs, ...). Live-TV, Streams, Plugins: nicht."""
    p = (path or "").lower()
    if not p or p.startswith(("pvr://", "http://", "https://", "plugin://", "upnp://", "rtmp", "rtsp://",
                              "udp://", "mms", "dvd://", "bluray://", "iso9660://")):
        return False
    return True


def thumb_chapters(count, max_samples=4):
    """Kapitel fuer Einzelbilder: Kapitel 1 (Start-Logos) auslassen, gleichmaessig verteilt."""
    if count < 2:
        return []
    cands = list(range(2, count + 1))
    if len(cands) <= max_samples:
        return cands
    step = (len(cands) - 1) / float(max_samples - 1)
    return sorted(set(cands[int(round(i * step))] for i in range(max_samples)))


class Detector(object):
    """Eine Messung fuer eine Datei (Hintergrund-Thread). Ergebnis in .result:
       ("bars", ratio) / ("full", None) / (None, grund); .done wenn fertig."""

    def __init__(self, path, reported, duration, chapters, cache, on_done, log,
                 use_render=True, render_allowed=None, rpc=None):
        self.path, self.reported, self.duration, self.chapters = path, reported, duration, chapters
        self.cache, self.on_done, self.log = cache, on_done, log
        self.use_render = use_render
        self.render_allowed = render_allowed or (lambda: True)
        self.rpc = rpc
        self.key = cache_key(path, duration)
        self.samples = []
        self.result = (None, "laeuft")
        self.done = False
        self.stop_flag = threading.Event()
        self.thread = None

    # -- Steuerung
    def start(self):
        self.thread = threading.Thread(target=self._run, name="tvframe43-letterbox")
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.stop_flag.set()

    def _wait(self, s):
        return self.stop_flag.wait(s)

    # -- Quellen
    def _thumb_sample(self, chapter):
        url = thumb_url(self.path, chapter)
        existed = self._texture_ids(url)
        t0 = time.time()
        try:
            f = xbmcvfs.File(url)
            try:
                data = f.readBytes()
            finally:
                f.close()
        except Exception as e:
            return {"kind": "empty", "why": "Einzelbild nicht lesbar: %s" % e, "src": "thumb%d" % chapter}
        dt = time.time() - t0
        if not existed:  # selbst erzeugte Kapitelbilder wieder aus Kodis Texture-Cache entfernen
            if chapter:
                for tid in self._texture_ids(url):
                    self.rpc("Textures.RemoveTexture", {"textureid": tid})
        if not data:
            return {"kind": "empty", "why": "kein Einzelbild (Vorschaubilder aus Videos aus?)", "src": "thumb%d" % chapter}
        try:
            rows = jpeg_row_profile(data)
        except Exception as e:
            return {"kind": "empty", "why": "Einzelbild nicht auswertbar: %s" % e, "src": "thumb%d" % chapter}
        s = analyse_profile(rows, self.reported)
        s["src"] = "thumb%d" % chapter
        s["secs"] = round(dt, 1)
        return s

    def _texture_ids(self, url):
        if not self.rpc:
            return []
        res = self.rpc("Textures.GetTextures", {"filter": {"field": "url", "operator": "is", "value": url},
                                                "properties": ["url"]}) or {}
        return [t.get("textureid") for t in res.get("textures") or [] if t.get("textureid") is not None]

    def _render_sample(self, rc, w, h):
        try:
            rc.capture(w, h)
            buf = rc.getImage(1000)
        except Exception as e:
            return {"kind": "empty", "why": "RenderCapture-Fehler: %s" % e, "src": "render"}
        if not buf or len(buf) < w * h * 4:
            return {"kind": "empty", "why": "RenderCapture ohne Pixel", "src": "render"}
        rows = bgra_row_profile(buf, w, h)
        s = analyse_profile(rows, self.reported)
        s["src"] = "render"
        return s

    def _note(self, s):
        self.samples.append(s)
        if s["kind"] in ("bars", "full"):
            self.log("Balken-Messung Probe %d (%s): %s %s (oben %s, unten %s, roh %s)" % (
                len(self.samples), s.get("src"), s["kind"], s.get("ratio"), s.get("top"), s.get("bottom"),
                s.get("raw", "-")))
        else:
            self.log("Balken-Messung Probe %d (%s): verworfen - %s" % (len(self.samples), s.get("src"), s.get("why")))

    def _finish(self, result, final=True):
        self.result = result
        kind, val = result
        prev = self.cache.get(self.key) or {}
        if kind:
            entry = {"state": kind, "ratio": val or 0, "n": len(self.samples)}
        else:
            entry = {"state": "unsure", "tries": int(prev.get("tries", 0)) + 1, "n": len(self.samples)}
        if final and not self.stop_flag.is_set():
            self.cache.put(self.key, entry)
        self.log("Balken-Messung Ergebnis fuer %s: %s (%s)" % (
            self.key, kind or "unsicher", ("%.2f" % val) if isinstance(val, float) else val))
        self.done = True
        try:
            self.on_done(self)
        except Exception:
            pass

    # -- Ablauf
    def _run(self):
        try:
            self._run_inner()
        except Exception as e:  # pragma: no cover
            self.log("Balken-Messung abgebrochen: %s" % e)
            self.done = True

    def _run_inner(self):
        t0 = time.time()
        # 1) Einzelbilder aus der Datei (Software-Decoder, Renderer-unabhaengig)
        chs = [0] + thumb_chapters(self.chapters)
        self.log("Balken-Messung startet: %s, gemeldet %.3f, Laufzeit %d s, Kapitel %d -> Einzelbilder %s" % (
            self.key, self.reported, self.duration, self.chapters, chs))
        for ch in chs:
            if self.stop_flag.is_set():
                return
            self._note(self._thumb_sample(ch))
            r = decide(self.samples, MIN_AGREE_THUMB)
            if r[0] and len([s for s in self.samples if s["kind"] in ("bars", "full")]) >= 3:
                break  # 3 gueltige, einig -> genug Decoder-Last
            if self._wait(PAUSE_THUMB):
                return
        result = decide(self.samples, MIN_AGREE_THUMB)
        if result[0]:
            self.log("Balken-Messung: Einzelbilder einig nach %.0f s" % (time.time() - t0))
            return self._finish(result)
        self.log("Balken-Messung: Einzelbilder ohne Entscheidung (%s)" % result[1])
        # 2) live per RenderCapture (falls der Renderer Pixel liefert)
        if not self.use_render or xbmc is None:
            return self._finish(result)
        rc = None
        try:
            rc = xbmc.RenderCapture()
        except Exception:
            rc = None
        empties, live = 0, 0
        if self._wait(RENDER_START):
            return
        while rc is not None and live < RENDER_MAX and not self.stop_flag.is_set():
            if not self.render_allowed():
                if self._wait(max(PAUSE_RENDER / 2.0, 0.01) if PAUSE_RENDER else 0):
                    return
                continue
            s = self._render_sample(rc, 320, 180)
            if s["kind"] == "empty":
                empties += 1
                if empties >= 3:
                    self.log("Balken-Messung: RenderCapture liefert keine Pixel (Renderer zeichnet am "
                             "Framebuffer vorbei, z.B. MediaCodec Surface) - live-Messung aus")
                    break
            else:
                empties = 0
                live += 1
                self._note(s)
                result = decide(self.samples, MIN_AGREE_RENDER, last_agree=LAST_AGREE)
                if result[0]:
                    return self._finish(result)
            if self._wait(PAUSE_RENDER):
                return
        return self._finish(decide(self.samples, MIN_AGREE_RENDER, last_agree=LAST_AGREE))
