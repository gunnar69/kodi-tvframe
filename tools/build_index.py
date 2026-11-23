#!/usr/bin/env python3
"""index.json aus designs/<id>/design.json erzeugen bzw. pruefen.

  python3 tools/build_index.py           # index.json neu schreiben
  python3 tools/build_index.py --check   # nur pruefen: Designs gueltig + index.json aktuell?

Die Pruefung nutzt dieselben Regeln wie das Add-on (addon/service.tvframe43/gallery.py):
Ordnername = id, nur Dateien .png/.json/.jpg mit einfachen Namen, keine Unterordner/Symlinks,
jede Datei von design.json referenziert, PNG-Groesse = canvas der Koordinaten-JSON,
Mattscheibe innerhalb des Bildes, Groessenlimits.
"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "addon", "service.tvframe43"))
import gallery as G  # noqa: E402  (reines Python, keine Kodi-Module)

DESIGNS = os.path.join(ROOT, "designs")
INDEX = os.path.join(ROOT, "index.json")


def fail(errors, did, msg):
    errors.append("%s: %s" % (did, msg))


def build_entry(did, errors):
    folder = os.path.join(DESIGNS, did)
    if not G.valid_id(did):
        fail(errors, did, "Ordnername ungueltig (erlaubt: a-z 0-9 - _, max. 48 Zeichen, Anfang a-z/0-9)")
        return None
    names = sorted(os.listdir(folder))
    for n in names:
        p = os.path.join(folder, n)
        if os.path.islink(p) or not os.path.isfile(p):
            fail(errors, did, "%s: nur normale Dateien erlaubt (keine Ordner/Symlinks)" % n)
        elif not G.safe_name(n):
            fail(errors, did, "%s: unzulaessiger Dateiname/Typ (nur .png .json .jpg, [A-Za-z0-9._-])" % n)
    if errors:
        return None
    if G.DESIGN_FILE not in names:
        fail(errors, did, "design.json fehlt")
        return None
    if len(names) > G.MAX_FILES:
        fail(errors, did, "zu viele Dateien (%d > %d)" % (len(names), G.MAX_FILES))

    def read(name):
        if name not in names:
            raise G.GalleryError("invalid", "%s fehlt" % name)
        with open(os.path.join(folder, name), "rb") as f:
            return f.read()

    try:
        for n in names:
            G.check_content(n, read(n))
        raw = json.loads(read(G.DESIGN_FILE).decode("utf-8"))
        d = G.validate_design(raw, did, read)
    except (G.GalleryError, ValueError) as e:
        fail(errors, did, str(e))
        return None
    for k in ("name", "author", "version", "description", "license"):
        if not isinstance(raw.get(k), str) or not raw[k].strip():
            fail(errors, did, "design.json: Feld '%s' fehlt" % k)
    if raw.get("rights_confirmed") is not True:
        fail(errors, did, "design.json: \"rights_confirmed\": true fehlt (Rechte an den Bildern bestaetigen)")
    if not d["preview"] or not d["preview"].endswith(".jpg"):
        fail(errors, did, "preview.jpg fehlt (480x270 empfohlen)")
    elif os.path.getsize(os.path.join(folder, d["preview"])) > G.MAX_PREVIEW:
        fail(errors, did, "Vorschaubild zu gross")
    used = {G.DESIGN_FILE, d["preview"]}
    for fmt, spec in d["formats"].items():
        used.update((spec["image"], spec["frame"]))
        # ab Add-on 1.3.2 optional: Variante mit mittiger Mattscheibe (eingebrannte Balken)
        cen = (raw.get("formats", {}).get(fmt) or {}).get("centered")
        if cen is not None:
            if "centered" not in spec:
                fail(errors, did, "design.json: Format %s: \"centered\" ungueltig (image/frame .png/.json, "
                     "Dateien vorhanden, canvas passt)" % fmt)
            else:
                used.update((spec["centered"]["image"], spec["centered"]["frame"]))
    for n in names:
        if n not in used:
            fail(errors, did, "%s wird von design.json nicht verwendet" % n)
    files = []
    for n in names:
        data = read(n)
        if len(data) > G.MAX_FILE:
            fail(errors, did, "%s groesser als %d MB" % (n, G.MAX_FILE // 1048576))
        files.append({"path": "designs/%s/%s" % (did, n), "size": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()})
    entry = {"id": did, "name": d["name"], "author": d["author"], "version": d["version"],
             "description": d["description"], "license": d["license"],
             "preview": "designs/%s/%s" % (did, d["preview"]) if d["preview"] else None,
             "formats": [f for f in G.FORMATS if f in d["formats"]], "files": files}
    if d["name_en"]:
        entry["name_en"] = d["name_en"]
    if d["description_en"]:
        entry["description_en"] = d["description_en"]
    try:
        G.check_entry(entry)
    except G.GalleryError as e:
        fail(errors, did, str(e))
    return entry


def build():
    errors, entries = [], []
    for did in sorted(os.listdir(DESIGNS)):
        if os.path.isdir(os.path.join(DESIGNS, did)):
            e = build_entry(did, errors)
            if e:
                entries.append(e)
        else:
            fail(errors, did, "unter designs/ sind nur Ordner erlaubt")
    index = {"schema": 1, "repo": G.REPO_URL, "designs": entries}
    return json.dumps(index, ensure_ascii=False, indent=1, sort_keys=False) + "\n", errors


def main():
    text, errors = build()
    if errors:
        print("FEHLER:")
        for e in errors:
            print("  - " + e)
        return 1
    n = len(json.loads(text)["designs"])
    if "--check" in sys.argv:
        try:
            with open(INDEX, encoding="utf-8") as f:
                have = f.read()
        except OSError:
            have = ""
        if have != text:
            print("index.json ist nicht aktuell - bitte 'python3 tools/build_index.py' ausfuehren")
            return 1
        print("OK: %d Designs gueltig, index.json aktuell" % n)
        return 0
    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(text)
    print("index.json geschrieben: %d Designs" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
