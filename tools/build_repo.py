#!/usr/bin/env python3
"""Kodi-Repository unter repo/ aus den Add-ons in addon/ erzeugen bzw. pruefen.

  python3 tools/build_repo.py           # repo/ neu schreiben (Zips, addons.xml, Pruefsummen)
  python3 tools/build_repo.py --check   # nur pruefen: passt repo/ zum Stand in addon/? (CI)

Gehostet wird alles ueber raw.githubusercontent.com (Branch main), ohne eigenen Server.
Das Repository-Add-on (addon/repository.tvframe43) verweist auf:

  repo/addons.xml                      alle addon.xml-Inhalte (Kodi-Index)
  repo/addons.xml.sha256               sha256 von addons.xml (<checksum verify="sha256">)
  repo/addons.xml.md5                  md5 von addons.xml (nur fuer aeltere Werkzeuge)
  repo/zips/<id>/<id>-<version>.zip    Paket, oberste Ebene im Zip = Ordner <id>/
  repo/zips/<id>/<id>-<version>.zip.sha256   (<hashes>sha256</hashes>: Kodi holt <zip>.sha256)
  repo/zips/<id>/<asset>               icon/fanart usw. aus <assets> (Kodi: artdir/<id>/<pfad>)

Die Zips sind reproduzierbar (sortierte Eintraege, feste Zeitstempel und Rechte), ohne
__pycache__/*.pyc, Backups, versteckte Dateien und Testdateien. Je Add-on liegt nur die
aktuelle Version in repo/zips/ (aeltere Versionen bleiben in der Git-Historie).

Neue Version veroeffentlichen: Version in addon/<id>/addon.xml erhoehen (+ <news>),
build_repo.py ausfuehren, repo/ mit committen, nach main pushen.
"""
import hashlib
import io
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDONS = os.path.join(ROOT, "addon")
REPO = os.path.join(ROOT, "repo")
ZIPS = os.path.join(REPO, "zips")

ZIP_DATE = (2020, 1, 1, 0, 0, 0)          # fester Zeitstempel -> reproduzierbare Zips
ID_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SKIP_DIRS = {"__pycache__", "tests", "test"}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}


class BuildError(Exception):
    pass


def skip_file(name):
    return (name in SKIP_NAMES or name.startswith(".") or name.endswith((".pyc", ".pyo", "~", ".orig", ".rej", ".swp"))
            or ".bak" in name or re.match(r"^test_.*\.py$", name) or re.match(r"^.*_test\.py$", name))


def addon_files(folder):
    """Relative Pfade (mit /) aller Dateien, die ins Zip gehoeren, sortiert."""
    out = []
    for base, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for n in files:
            p = os.path.join(base, n)
            if skip_file(n):
                continue
            if os.path.islink(p):
                raise BuildError("%s: Symlinks sind im Add-on nicht erlaubt" % p)
            out.append(os.path.relpath(p, folder).replace(os.sep, "/"))
    return sorted(out)


def read_addon(folder):
    """addon.xml lesen und pruefen -> (id, version, xml-Text ohne Deklaration, Asset-Pfade)."""
    path = os.path.join(folder, "addon.xml")
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
        root = ET.fromstring(raw)
    except (UnicodeDecodeError, ET.ParseError) as e:
        raise BuildError("%s: kein gueltiges UTF-8-XML (%s)" % (path, e))
    aid, ver = root.get("id", ""), root.get("version", "")
    if root.tag != "addon" or not ID_RE.match(aid):
        raise BuildError("%s: <addon id=...> fehlt/ungueltig" % path)
    if aid != os.path.basename(folder):
        raise BuildError("%s: Ordnername muss der Add-on-ID '%s' entsprechen" % (path, aid))
    if not VERSION_RE.match(ver):
        raise BuildError("%s: Version '%s' ist nicht im Format X.Y.Z" % (path, ver))
    assets = []
    for el in root.iter("assets"):
        for a in el:
            if a.text and a.text.strip():
                rel = a.text.strip()
                if rel.startswith("/") or ".." in rel.split("/"):
                    raise BuildError("%s: Asset-Pfad '%s' ungueltig" % (path, rel))
                if not os.path.isfile(os.path.join(folder, rel)):
                    raise BuildError("%s: Asset '%s' fehlt" % (path, rel))
                assets.append(rel)
    body = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", text).rstrip() + "\n"
    return aid, ver, body, sorted(set(assets))


def make_zip(folder, aid):
    """Reproduzierbares Zip im Speicher -> (bytes, {arcname: inhalt})."""
    entries = {}
    for rel in addon_files(folder):
        with open(os.path.join(folder, rel), "rb") as f:
            entries["%s/%s" % (aid, rel)] = f.read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in sorted(entries):
            zi = zipfile.ZipInfo(name, ZIP_DATE)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zi.create_system = 3
            z.writestr(zi, entries[name], compresslevel=9)
    return buf.getvalue(), entries


def zip_entries(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: z.read(i.filename) for i in z.infolist() if not i.is_dir()}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def collect():
    """Soll-Zustand von repo/: {relpfad: bytes}, Zip-Inhalte, Versionen."""
    if not os.path.isdir(ADDONS):
        raise BuildError("Ordner addon/ fehlt")
    files, zips, versions, bodies = {}, {}, {}, []
    for aid in sorted(os.listdir(ADDONS)):
        folder = os.path.join(ADDONS, aid)
        if not os.path.isfile(os.path.join(folder, "addon.xml")):
            continue
        aid, ver, body, assets = read_addon(folder)
        versions[aid] = ver
        bodies.append(body)
        zname = "zips/%s/%s-%s.zip" % (aid, aid, ver)
        data, entries = make_zip(folder, aid)
        files[zname] = data
        zips[zname] = entries
        files[zname + ".sha256"] = ("%s  %s-%s.zip\n" % (sha256(data), aid, ver)).encode()
        for rel in assets:
            with open(os.path.join(folder, rel), "rb") as f:
                files["zips/%s/%s" % (aid, rel)] = f.read()
    if not bodies:
        raise BuildError("keine Add-ons in addon/ gefunden")
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n'
           + "".join(bodies) + "</addons>\n").encode("utf-8")
    root = ET.fromstring(xml)                      # muss parsebar bleiben
    if [a.get("id") for a in root.findall("addon")] != sorted(versions):
        raise BuildError("addons.xml: Add-on-Liste inkonsistent")
    files["addons.xml"] = xml
    files["addons.xml.sha256"] = ("%s  addons.xml\n" % sha256(xml)).encode()
    files["addons.xml.md5"] = ("%s  addons.xml\n" % hashlib.md5(xml).hexdigest()).encode()
    return files, zips, versions


def existing():
    out = {}
    if os.path.isdir(REPO):
        for base, _dirs, names in os.walk(REPO):
            for n in names:
                p = os.path.join(base, n)
                out[os.path.relpath(p, REPO).replace(os.sep, "/")] = p
    return out


def read(p):
    with open(p, "rb") as f:
        return f.read()


def check(files, zips):
    errors = []
    have = existing()
    for rel in sorted(set(files) | set(have)):
        if rel not in have:
            errors.append("repo/%s fehlt" % rel)
            continue
        if rel not in files:
            errors.append("repo/%s ist ueberzaehlig (alte Version?)" % rel)
            continue
        cur = read(have[rel])
        if rel in zips:
            # Inhalt vergleichen statt Bytes (andere zlib-Version in der CI ergibt andere Kompression)
            try:
                ent = zip_entries(cur)
            except zipfile.BadZipFile:
                errors.append("repo/%s ist kein gueltiges Zip" % rel)
                continue
            if ent != zips[rel]:
                diff = sorted(n for n in set(ent) | set(zips[rel]) if ent.get(n) != zips[rel].get(n))
                errors.append("repo/%s passt nicht zu addon/ (%s) - bei geaendertem Code Version "
                              "in addon.xml erhoehen und build_repo.py ausfuehren"
                              % (rel, ", ".join(diff[:5]) + (" ..." if len(diff) > 5 else "")))
            side = have.get(rel + ".sha256")
            if side and read(side).split()[0].decode() != sha256(cur):
                errors.append("repo/%s.sha256 passt nicht zur Zip-Datei" % rel)
        elif rel.endswith(".zip.sha256"):
            continue                               # oben gegen die echte Zip-Datei geprueft
        elif cur != files[rel]:
            errors.append("repo/%s ist nicht aktuell" % rel)
    return errors


def build(files, zips, versions, allow_same):
    have = existing()
    # Schutz: gleicher Versionsstand mit anderem Inhalt -> Kodi wuerde das Update nie sehen
    for rel, entries in zips.items():
        if rel in have and not allow_same:
            try:
                same = zip_entries(read(have[rel])) == entries
            except zipfile.BadZipFile:
                same = False
            if not same:
                raise BuildError("repo/%s existiert schon mit anderem Inhalt. Version in addon.xml "
                                 "erhoehen (sonst sieht Kodi kein Update) oder --allow-same-version"
                                 % rel)
    for rel, p in have.items():
        if rel not in files:
            os.remove(p)
            print("entfernt  repo/%s" % rel)
    for rel, data in sorted(files.items()):
        p = os.path.join(REPO, *rel.split("/"))
        if rel in zips and rel in have and zip_entries(read(have[rel])) == zips[rel]:
            continue                               # unveraendert -> Bytes (und Hash) behalten
        if rel.endswith(".zip.sha256"):
            continue                               # wird nach dem Zip geschrieben
        if rel in have and read(have[rel]) == data:
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        print("geschrieben repo/%s" % rel)
    for rel in zips:                               # .sha256 immer zur tatsaechlichen Zip-Datei
        p = os.path.join(REPO, *rel.split("/"))
        line = ("%s  %s\n" % (sha256(read(p)), os.path.basename(rel))).encode()
        sp = p + ".sha256"
        if not os.path.isfile(sp) or read(sp) != line:
            with open(sp, "wb") as f:
                f.write(line)
            print("geschrieben repo/%s.sha256" % rel)
    for base, dirs, names in os.walk(ZIPS, topdown=False):
        if base != ZIPS and not dirs and not names:
            shutil.rmtree(base)
    print("OK: " + ", ".join("%s %s" % kv for kv in sorted(versions.items())))


def main():
    try:
        files, zips, versions = collect()
        if "--check" in sys.argv:
            errors = check(files, zips)
            if errors:
                print("FEHLER (repo/ ist nicht aktuell - python3 tools/build_repo.py ausfuehren):")
                for e in errors:
                    print("  - " + e)
                return 1
            print("OK: repo/ passt zu addon/ (" + ", ".join("%s %s" % kv for kv in sorted(versions.items())) + ")")
            return 0
        build(files, zips, versions, "--allow-same-version" in sys.argv)
        return 0
    except BuildError as e:
        print("FEHLER: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
