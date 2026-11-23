# -*- coding: utf-8 -*-
"""service.tvframe43 - Design-Galerie (ab 1.2.0).

Laedt die Designliste (index.json) aus dem oeffentlichen GitHub-Repo gunnar69/kodi-tvframe und
installiert einzelne Designs in den Profilordner (addon_data/service.tvframe43/designs/<id>/).

Sicherheit: Es werden ausschliesslich Daten geladen (PNG/JSON/JPG), nie Code. Jede Datei muss
im Index mit Groesse und sha256 stehen; Dateinamen sind auf [A-Za-z0-9._-] ohne Pfadanteile
beschraenkt, Ziel ist immer direkt designs/<id>/. Heruntergeladen wird nach designs/.tmp-<id>/,
erst nach vollstaendiger Pruefung wird umbenannt. Das Modul nutzt keine Kodi-Funktionen, damit
es auch ausserhalb von Kodi testbar ist (Transport-Fallback ueber Kodi kann gesetzt werden).
"""
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import struct
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_URL = "https://github.com/gunnar69/kodi-tvframe"
BASE_URL = "https://raw.githubusercontent.com/gunnar69/kodi-tvframe/main/"
INDEX_URL = BASE_URL + "index.json"
USER_AGENT = "service.tvframe43-gallery/1.2"
TIMEOUT = 12

MAX_INDEX = 1024 * 1024             # index.json
MAX_FILE = 12 * 1024 * 1024         # je Datei
MAX_TOTAL = 40 * 1024 * 1024        # je Design
MAX_FILES = 16                      # je Design
MAX_PREVIEW = 2 * 1024 * 1024

FORMATS = ("43", "wide", "w2")
DESIGN_FILE = "design.json"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
EXTS = (".png", ".json", ".jpg")

# optional: zweiter Transportweg (z.B. Kodis eigenes curl ueber xbmcvfs), nur bei SSL-Problemen.
# Die Integritaet bleibt durch sha256 gesichert.
FALLBACK_GET = None


class GalleryError(Exception):
    """code: offline, http, ssl, invalid, hash, path, size, cancel"""

    def __init__(self, code, detail=""):
        Exception.__init__(self, "%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


# --- Pruefungen -------------------------------------------------------------------

def valid_id(did):
    return isinstance(did, str) and bool(ID_RE.match(did))


def safe_name(name):
    """Nur ein nackter Dateiname mit erlaubter Endung (kein Pfad, kein '..')."""
    return (isinstance(name, str) and bool(NAME_RE.match(name)) and ".." not in name
            and name.endswith(EXTS))


def _text(v, default="", limit=160):
    if not isinstance(v, str):
        return default
    v = "".join(c for c in v if c >= " " and c != "\x7f").strip()
    return v[:limit] if v else default


def check_content(name, data):
    """Inhalt passt zur Endung (Magic Bytes / gueltiges JSON)."""
    if name.endswith(".png"):
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise GalleryError("invalid", "%s ist keine PNG-Datei" % name)
    elif name.endswith(".jpg"):
        if not data.startswith(b"\xff\xd8\xff"):
            raise GalleryError("invalid", "%s ist keine JPEG-Datei" % name)
    elif name.endswith(".json"):
        try:
            json.loads(data.decode("utf-8"))
        except Exception:
            raise GalleryError("invalid", "%s ist kein gueltiges JSON" % name)
    else:
        raise GalleryError("path", name)


def png_size(data):
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise GalleryError("invalid", "PNG ohne IHDR")
    return struct.unpack(">II", data[16:24])


def validate_frame(fr, name="frame"):
    """Koordinaten-JSON wie resources/frames/*.json -> bereinigtes dict."""
    if not isinstance(fr, dict):
        raise GalleryError("invalid", name)
    try:
        cw, ch = int(fr["canvas"][0]), int(fr["canvas"][1])
        sc = fr["screen"]
        x, y, w, h = (float(sc[k]) for k in ("x", "y", "w", "h"))
        ov = float(fr.get("overlap", 0) or 0)
    except Exception:
        raise GalleryError("invalid", "%s: canvas/screen fehlen" % name)
    if not (100 <= cw <= 8192 and 100 <= ch <= 8192):
        raise GalleryError("invalid", "%s: canvas %sx%s" % (name, cw, ch))
    if not (w > 0 and h > 0 and x >= 0 and y >= 0 and x + w <= cw and y + h <= ch):
        raise GalleryError("invalid", "%s: Mattscheibe ausserhalb des Bildes" % name)
    if not (0 <= ov <= 100):
        raise GalleryError("invalid", "%s: overlap" % name)
    out = {"canvas": [cw, ch], "screen": {"x": x, "y": y, "w": w, "h": h}, "overlap": ov}
    if "radius" in sc:
        try:
            out["screen"]["radius"] = float(sc["radius"])
        except Exception:
            pass
    for k in ("design_aspect", "zoom", "verticalshift"):
        try:
            if k in fr:
                out[k] = float(fr[k])
        except Exception:
            pass
    out["outside_black"] = bool(fr.get("outside_black", False))
    out["name"] = _text(fr.get("name"), name)
    return out


def validate_design(d, did, read):
    """design.json pruefen. read(name) -> bytes einer Datei des Designs (oder Exception)."""
    if not isinstance(d, dict) or d.get("id") != did:
        raise GalleryError("invalid", "design.json: id passt nicht zum Ordner")
    fmts = d.get("formats")
    if not isinstance(fmts, dict) or not fmts:
        raise GalleryError("invalid", "design.json: keine Formate")
    out = {"id": did, "name": _text(d.get("name"), did), "name_en": _text(d.get("name_en")),
           "author": _text(d.get("author"), "?", 80), "version": _text(d.get("version"), "1", 32),
           "description": _text(d.get("description"), "", 400),
           "description_en": _text(d.get("description_en"), "", 400),
           "license": _text(d.get("license"), "", 120), "formats": {}, "preview": None}
    for fmt, spec in fmts.items():
        if fmt not in FORMATS:
            continue                     # unbekannte (kuenftige) Formate ignorieren
        if not isinstance(spec, dict):
            raise GalleryError("invalid", "design.json: Format %s" % fmt)
        img, frm = spec.get("image"), spec.get("frame")
        if not (safe_name(img) and img.endswith(".png") and safe_name(frm) and frm.endswith(".json")):
            raise GalleryError("path", "design.json: Format %s" % fmt)
        try:
            frame = validate_frame(json.loads(read(frm).decode("utf-8")), frm)
            w, h = png_size(read(img))
        except GalleryError:
            raise
        except Exception as e:
            raise GalleryError("invalid", "Format %s: %s" % (fmt, e))
        if [w, h] != frame["canvas"]:
            raise GalleryError("invalid", "%s: %dx%d passt nicht zu canvas %s" % (img, w, h, frame["canvas"]))
        out["formats"][fmt] = {"image": img, "frame": frm}
        # ab 1.3.2 optional: "centered": {"image", "frame"} = Variante mit mittiger Mattscheibe
        # fuer Filme mit eingebrannten Balken. Fehlerhaft -> einfach ignorieren (Fallback).
        cen = spec.get("centered")
        if isinstance(cen, dict):
            ci, cf = cen.get("image"), cen.get("frame")
            try:
                if not (safe_name(ci) and ci.endswith(".png") and safe_name(cf) and cf.endswith(".json")):
                    raise GalleryError("path", "centered")
                cfr = validate_frame(json.loads(read(cf).decode("utf-8")), cf)
                if list(png_size(read(ci))) != cfr["canvas"]:
                    raise GalleryError("invalid", "centered canvas")
                out["formats"][fmt]["centered"] = {"image": ci, "frame": cf}
            except Exception:
                pass
    if not out["formats"]:
        raise GalleryError("invalid", "design.json: kein bekanntes Format")
    pv = d.get("preview")
    if pv is not None:
        if not (safe_name(pv) and pv.endswith((".jpg", ".png"))):
            raise GalleryError("path", "design.json: preview")
        out["preview"] = pv
    return out


def check_entry(e):
    """Ein Eintrag aus index.json -> bereinigtes dict (oder GalleryError)."""
    if not isinstance(e, dict) or not valid_id(e.get("id")):
        raise GalleryError("path", "ungueltige id %r" % (e.get("id") if isinstance(e, dict) else e))
    did = e["id"]
    files = e.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise GalleryError("invalid", "%s: Dateiliste" % did)
    prefix = "designs/%s/" % did
    clean, names, total = [], set(), 0
    for f in files:
        if not isinstance(f, dict):
            raise GalleryError("invalid", did)
        p, sha, size = f.get("path"), f.get("sha256"), f.get("size")
        if not isinstance(p, str) or not p.startswith(prefix) or not safe_name(p[len(prefix):]):
            raise GalleryError("path", "%s: %r" % (did, p))
        name = p[len(prefix):]
        if name in names:
            raise GalleryError("invalid", "%s: %s doppelt" % (did, name))
        if not isinstance(sha, str) or not SHA_RE.match(sha):
            raise GalleryError("invalid", "%s: sha256 von %s" % (did, name))
        if not isinstance(size, int) or isinstance(size, bool) or not (0 < size <= MAX_FILE):
            raise GalleryError("size", "%s: %s" % (did, name))
        total += size
        names.add(name)
        clean.append({"path": p, "name": name, "sha256": sha, "size": size})
    if DESIGN_FILE not in names:
        raise GalleryError("invalid", "%s: design.json fehlt" % did)
    if total > MAX_TOTAL:
        raise GalleryError("size", "%s: zusammen %d Bytes" % (did, total))
    pv = e.get("preview")
    pv_name = pv[len(prefix):] if isinstance(pv, str) and pv.startswith(prefix) else None
    if pv_name not in names or not pv_name.endswith((".jpg", ".png")):
        pv_name = None
    fmts = e.get("formats") if isinstance(e.get("formats"), list) else []
    return {"id": did, "name": _text(e.get("name"), did), "name_en": _text(e.get("name_en")),
            "author": _text(e.get("author"), "?", 80), "version": _text(e.get("version"), "1", 32),
            "description": _text(e.get("description"), "", 400),
            "description_en": _text(e.get("description_en"), "", 400),
            "formats": [f for f in FORMATS if f in fmts], "preview": pv_name, "files": clean}


def parse_index(data):
    """-> (eintraege, verworfene). Ungueltige Eintraege werden uebersprungen, nicht installiert."""
    try:
        idx = json.loads(data.decode("utf-8"))
    except Exception:
        raise GalleryError("invalid", "index.json ist kein JSON")
    if not isinstance(idx, dict) or not isinstance(idx.get("designs"), list):
        raise GalleryError("invalid", "index.json ohne designs-Liste")
    out, bad, seen = [], [], set()
    for e in idx["designs"]:
        try:
            c = check_entry(e)
        except GalleryError as ex:
            bad.append(str(ex))
            continue
        if c["id"] in seen:
            bad.append("%s doppelt" % c["id"])
            continue
        seen.add(c["id"])
        out.append(c)
    return out, bad


# --- Netzwerk ---------------------------------------------------------------------

def http_get(url, limit, timeout=TIMEOUT):
    if not isinstance(url, str) or not url.startswith("https://"):
        raise GalleryError("invalid", "nur https erlaubt")
    try:
        resp = urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)
        try:
            data = resp.read(limit + 1)
        finally:
            resp.close()
    except HTTPError as e:
        raise GalleryError("http", "HTTP %s" % getattr(e, "code", "?"))
    except URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLError):
            return _fallback(url, limit, e)
        raise GalleryError("offline", str(getattr(e, "reason", e)))
    except ssl.SSLError as e:
        return _fallback(url, limit, e)
    except (socket.timeout, OSError) as e:
        raise GalleryError("offline", str(e))
    if len(data) > limit:
        raise GalleryError("size", url.rsplit("/", 1)[-1])
    return bytes(data)


def _fallback(url, limit, err):
    if FALLBACK_GET is None:
        raise GalleryError("ssl", str(err))
    try:
        data = FALLBACK_GET(url)
    except Exception as e:
        raise GalleryError("ssl", "%s / %s" % (err, e))
    if not data:
        raise GalleryError("ssl", str(err))
    if len(data) > limit:
        raise GalleryError("size", url.rsplit("/", 1)[-1])
    return bytes(data)


def _write(path, data):
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def fetch_index(cache_dir, url=INDEX_URL):
    """Designliste laden. Netz zuerst, bei Fehler die zuletzt gespeicherte Liste.
    -> (eintraege, aus_cache, fehler_oder_None). Ohne Netz und ohne Cache: GalleryError."""
    cache = os.path.join(cache_dir, "index.json")
    try:
        data = http_get(url, MAX_INDEX)
        entries, _bad = parse_index(data)
        try:
            _write(cache, data)
        except Exception:
            pass
        return entries, False, None
    except GalleryError as err:
        if os.path.isfile(cache):
            try:
                with open(cache, "rb") as f:
                    entries, _bad = parse_index(f.read())
                return entries, True, err
            except Exception:
                pass
        raise


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def preview_file(cache_dir, entry, base=BASE_URL):
    """Vorschaubild (sha256-geprueft) in den Cache laden -> lokaler Pfad oder None."""
    name = entry.get("preview")
    f = next((x for x in entry["files"] if x["name"] == name), None) if name else None
    if not f or f["size"] > MAX_PREVIEW:
        return None
    path = _inside(os.path.realpath(_ensure(os.path.join(cache_dir, "previews"))),
                   "%s%s" % (entry["id"], os.path.splitext(name)[1]))
    try:
        if os.path.isfile(path) and sha256_file(path) == f["sha256"]:
            return path
        data = http_get(base + f["path"], f["size"])
        if hashlib.sha256(data).hexdigest() != f["sha256"]:
            return None
        check_content(name, data)
        _write(path, data)
        return path
    except Exception:
        return None


# --- Installation im Profilordner ---------------------------------------------------

def _ensure(folder):
    if not os.path.isdir(folder):
        os.makedirs(folder)
    return folder


def _inside(root, name):
    """root/name, garantiert direkt in root (kein Traversal, keine Symlinks hinaus)."""
    p = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(p) != root or not name or os.sep in name or (os.altsep and os.altsep in name):
        raise GalleryError("path", name)
    return p


def install(entry, designs_dir, progress=None, base=BASE_URL):
    """Design herunterladen, pruefen und nach designs_dir/<id>/ legen (atomar ersetzt).
    progress(n, gesamt, dateiname) -> False bricht ab. -> bereinigtes design-dict."""
    entry = check_entry(entry)          # nochmals pruefen (auch fuer Eintraege aus dem Cache)
    did = entry["id"]
    root = os.path.realpath(_ensure(designs_dir))
    target = _inside(root, did)
    tmp = _inside(root, ".tmp-" + did)
    old = _inside(root, ".old-" + did)
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        blobs = {}
        n_files = len(entry["files"])
        for n, f in enumerate(entry["files"]):
            if progress is not None and progress(n, n_files, f["name"]) is False:
                raise GalleryError("cancel")
            data = http_get(base + f["path"], f["size"])
            if len(data) != f["size"] or hashlib.sha256(data).hexdigest() != f["sha256"]:
                raise GalleryError("hash", f["name"])
            check_content(f["name"], data)
            blobs[f["name"]] = data

        def read(name):
            if name not in blobs:
                raise GalleryError("invalid", "%s fehlt im Index" % name)
            return blobs[name]

        design = validate_design(json.loads(read(DESIGN_FILE).decode("utf-8")), did, read)
        if design["preview"] and design["preview"] not in blobs:
            design["preview"] = None
        os.makedirs(tmp)
        for name, data in blobs.items():
            with open(_inside(tmp, name), "wb") as fh:
                fh.write(data)
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(target):
            os.rename(target, old)
        os.rename(tmp, target)
        shutil.rmtree(old, ignore_errors=True)
        if progress is not None:
            progress(n_files, n_files, "")
        return design
    except GalleryError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise GalleryError("invalid", str(e))


def remove(designs_dir, did):
    """Installiertes Design loeschen (nur designs_dir/<id>/)."""
    if not valid_id(did) or not os.path.isdir(designs_dir):
        return False
    link = os.path.join(designs_dir, did)
    if os.path.islink(link):
        return False                    # nie einem Symlink hinterher loeschen
    try:
        target = _inside(os.path.realpath(designs_dir), did)
    except GalleryError:
        return False
    if not os.path.isdir(target):
        return False
    shutil.rmtree(target)
    return True


def load_design(designs_dir, did):
    """Installiertes Design lesen und pruefen -> dict mit 'dir' oder None."""
    if not valid_id(did) or not os.path.isdir(designs_dir):
        return None
    try:
        root = os.path.realpath(designs_dir)
        folder = _inside(root, did)
        if not os.path.isdir(folder):
            return None

        def read(name):
            if not safe_name(name):
                raise GalleryError("path", name)
            with open(_inside(folder, name), "rb") as f:
                return f.read(MAX_FILE + 1)

        d = validate_design(json.loads(read(DESIGN_FILE).decode("utf-8")), did, read)
        d["dir"] = folder
        if d["preview"] and not os.path.isfile(os.path.join(folder, d["preview"])):
            d["preview"] = None
        return d
    except Exception:
        return None


def list_installed(designs_dir):
    if not os.path.isdir(designs_dir):
        return []
    out = []
    for did in sorted(os.listdir(designs_dir)):
        d = load_design(designs_dir, did) if valid_id(did) else None
        if d:
            out.append(d)
    return out


def frame_for(designs_dir, did, fmt, centered=False):
    """Rahmen-Beschreibung eines installierten Designs fuer ein Format (wie FrameStore.template).
    `centered` (1.3.2): Variante fuer eingebrannte Balken, None wenn das Design keine hat."""
    d = load_design(designs_dir, did)
    if not d or fmt not in d["formats"]:
        return None
    spec = d["formats"][fmt]
    if centered:
        spec = spec.get("centered")
        if not spec:
            return None
    try:
        with open(os.path.join(d["dir"], spec["frame"]), "rb") as f:
            meta = validate_frame(json.loads(f.read().decode("utf-8")), spec["frame"])
    except Exception:
        return None
    meta["id"] = "dl:%s/%s%s" % (did, fmt, "/c" if centered else "")
    meta["image"] = os.path.join(d["dir"], spec["image"])
    if not centered and spec.get("centered"):
        meta["centered"] = True       # Variante vorhanden (frame_for(..., centered=True))
    return meta


def local_state(designs_dir, entry):
    """'none' (nicht installiert), 'same' (identisch mit Index) oder 'update'."""
    try:
        entry = check_entry(entry)
    except GalleryError:
        return "update"
    d = load_design(designs_dir, entry["id"])
    if not d:
        return "none"
    try:
        have = set(n for n in os.listdir(d["dir"]))
        want = set(f["name"] for f in entry["files"])
        if have != want:
            return "update"
        for f in entry["files"]:
            if sha256_file(os.path.join(d["dir"], f["name"])) != f["sha256"]:
                return "update"
    except Exception:
        return "update"
    return "same"
