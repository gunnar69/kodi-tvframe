# -*- coding: utf-8 -*-
"""service.tvframe43 - Rahmen-Auswahl mit Vorschaubildern.

Aufruf: RunScript(service.tvframe43,select) - aus den Einstellungen (Button), ueber das
Programme-Menue oder eine optionale Fernbedienungstaste (eigene Keymap).
Ab 1.1.4 ist der Stil je Format (4:3, 2.35:1, 2:1) getrennt waehlbar; erster Eintrag ist
"Ohne Rahmen". Waehrend der Wiedergabe gilt die Liste dem Format des laufenden Videos und es wird
live umgeschaltet: Der Dialog bleibt offen, bis man ihn mit Zurueck schliesst. Ohne Wiedergabe
(oder bei 16:9) wird zuerst das Format gewaehlt.
Ab 1.2.0: "Designs aus der Galerie..." laedt weitere Rahmen aus github.com/gunnar69/kodi-tvframe
(nur Daten, sha256-geprueft, Ablage im Profilordner designs/<id>/). Installierte Designs stehen
danach in der Stil-Liste der Formate, fuer die sie Dateien haben (Wert 14 + gallery_<format>).
Aufruf direkt: RunScript(service.tvframe43,gallery).
"""
import os
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo("path"))
THUMBS = os.path.join(ADDON_PATH, "resources", "thumbs")

from service import (DISPLAY_ORDER, STYLE_CUSTOM, STYLE_NONE, STYLES, STYLE_SETTING, FORMATS,
                     STYLE_GALLERY, GALLERY_SETTING, DESIGNS_DIR, GALLERY_CACHE,
                     has_format, migrate_settings, video_ratio, Service)
import gallery as G

WINDOW_HOME = 10000
PROP_PING = "tvframe43.ping"
THUMB_NONE = os.path.join(THUMBS, "none.jpg")
THUMB_GALLERY = os.path.join(THUMBS, "gallery.jpg")
GALLERY_ENTRY = "gallery"           # Sondereintrag "Designs aus der Galerie..." in den Listen
DL = "dl:"                          # Listen-Schluessel fuer installierte Designs: "dl:<id>"
FMT_LABEL = {"43": 32360, "wide": 32361, "w2": 32362}
# ausserhalb von 4:3 teilen sich manche Stile dieselbe Datei -> nur einmal zeigen, neutraler Name
SHARED_LABEL = {0: 32144, 3: 32145, 12: 32146}


def tr(i):
    return ADDON.getLocalizedString(i)


def trf(i, arg):
    """Uebersetzung mit %s; robust, falls eine Uebersetzung den Platzhalter nicht enthaelt."""
    t = tr(i)
    return t % arg if t.count("%s") == 1 else "%s %s" % (t, arg)


def english():
    try:
        return not xbmc.getLanguage(xbmc.ISO_639_1).lower().startswith("de")
    except Exception:
        return False


def design_name(d):
    return (d.get("name_en") if english() else "") or d.get("name") or d.get("id", "?")


def installed_designs():
    try:
        return G.list_installed(DESIGNS_DIR)
    except Exception as e:
        xbmc.log("[service.tvframe43] Galerie-Designs nicht lesbar: %s" % e, xbmc.LOGWARNING)
        return []


def style_label(idx, fmt="43"):
    if isinstance(idx, str):
        if idx == GALLERY_ENTRY:
            return tr(32312)
        d = G.load_design(DESIGNS_DIR, idx[len(DL):])
        return design_name(d) if d else tr(32403)
    if idx == STYLE_GALLERY:
        return tr(32147)
    if idx == STYLE_NONE:
        return tr(32304)
    if fmt != "43":
        # z.B. 1 (LG CX mit Fuss) hat bei Breitbild dieselbe Datei wie 0 -> gleicher Name
        idx = next((i for i in styles_for(fmt) if is_current(i, idx, fmt)), idx)
        if idx in SHARED_LABEL:
            return tr(SHARED_LABEL[idx])
    return tr(32130 + idx) if idx <= 11 else tr(32142 + idx - 12)


def style_thumb(idx):
    if isinstance(idx, str) or idx == STYLE_GALLERY:
        d = G.load_design(DESIGNS_DIR, idx[len(DL):]) if isinstance(idx, str) and idx.startswith(DL) else None
        if d and d.get("preview"):
            return os.path.join(d["dir"], d["preview"])
        return THUMB_GALLERY
    return THUMB_NONE if idx == STYLE_NONE else os.path.join(THUMBS, "style%d.jpg" % idx)


def ping():
    """Zaehler statt Zeitstempel: zwei schnelle Auswahlen ergeben sicher verschiedene Werte."""
    w = xbmcgui.Window(WINDOW_HOME)
    try:
        n = int(w.getProperty(PROP_PING) or 0) + 1
    except ValueError:
        n = int(time.time())
    w.setProperty(PROP_PING, str(n))


def current_style(fmt):
    try:
        return int(float(xbmcaddon.Addon().getSetting(STYLE_SETTING[fmt]) or 0))
    except Exception:
        return 0


def current_key(fmt):
    """Wie current_style, aber Galerie-Designs als "dl:<id>"."""
    idx = current_style(fmt)
    if idx == STYLE_GALLERY:
        return DL + (xbmcaddon.Addon().getSetting(GALLERY_SETTING[fmt]) or "")
    return idx


def entries_for(fmt):
    """Ohne Rahmen + eingebaute Stile + installierte Galerie-Designs mit Datei fuer fmt + Galerie."""
    dl = [DL + d["id"] for d in installed_designs() if fmt in d["formats"]]
    return [STYLE_NONE] + styles_for(fmt) + dl + [GALLERY_ENTRY]


def stem(idx, fmt):
    if idx == STYLE_CUSTOM or not has_format(idx, fmt):
        return None
    return STYLES[idx][FORMATS.index(fmt)]


def styles_for(fmt):
    """Waehlbare Stile fuer ein Format in Anzeigereihenfolge; gleiche Datei nur einmal."""
    out, seen = [], set()
    for i in DISPLAY_ORDER:
        if not has_format(i, fmt):
            continue
        st = stem(i, fmt)
        if st is not None:
            if st in seen:
                continue
            seen.add(st)
        out.append(i)
    return out


def is_current(idx, cur, fmt):
    if idx == cur:
        return True
    if isinstance(idx, str) or isinstance(cur, str):
        return False
    st = stem(idx, fmt)
    return st is not None and st == stem(cur, fmt)


def playing_file(player):
    try:
        return player.getPlayingFile() if player.isPlayingVideo() else ""
    except Exception:
        return ""


def playing_format(player):
    """Format des laufenden Videos ("43"/"wide"/"w2") oder None (z.B. 16:9)."""
    if not playing_file(player):
        return None
    try:
        ratio, _ = video_ratio()
        return Service.classify(ratio) if ratio else None
    except Exception:
        return None


def make_item(label, label2, thumb, active):
    li = xbmcgui.ListItem(label, label2)
    li.setArt({"thumb": thumb, "icon": thumb})
    if active:
        try:
            li.select(True)
        except Exception:
            pass
    return li


def heading(fmt):
    t, name = tr(32350), tr(FMT_LABEL[fmt])
    return t % name if "%s" in t else "%s %s" % (t, name)


def choose_style(fmt, live):
    """Stil-Liste fuer ein Format. live=True: Dialog bleibt offen, jede Wahl wird sofort uebernommen.
    Rueckgabe True, wenn etwas gespeichert wurde."""
    changed = False
    pre = None
    while True:
        cur = current_key(fmt)
        entries = entries_for(fmt)
        items = [make_item(style_label(i, fmt), entry_label2(i, cur, fmt),
                           style_thumb(i), is_current(i, cur, fmt)) for i in entries]
        if pre is None or pre >= len(entries):
            pre = next((n for n, i in enumerate(entries) if is_current(i, cur, fmt)), 0)
        sel = xbmcgui.Dialog().select(heading(fmt), items, preselect=pre, useDetails=True)
        pre = None
        if sel < 0:
            return changed
        val = entries[sel]
        if val == GALLERY_ENTRY:
            if gallery_dialog():
                changed = True
            pre = len(entries_for(fmt)) - 1
            continue
        if isinstance(val, str):
            # installiertes Galerie-Design: erst die ID, dann den Stil-Wert 14 speichern
            xbmcaddon.Addon().setSetting(GALLERY_SETTING[fmt], val[len(DL):])
            xbmcaddon.Addon().setSetting(STYLE_SETTING[fmt], str(STYLE_GALLERY))
            changed = True
            ping()
            if not live:
                return changed
            continue
        if val == STYLE_CUSTOM and not xbmcaddon.Addon().getSetting("custom_" + fmt):
            path = xbmcgui.Dialog().browse(1, tr(32102 if fmt == "43" else 32103 if fmt == "wide" else 32106),
                                           "files", ".png")
            if not path:
                continue
            xbmcaddon.Addon().setSetting("custom_" + fmt, path)
        xbmcaddon.Addon().setSetting(STYLE_SETTING[fmt], str(val))
        changed = True
        ping()
        if not live:
            return changed


def entry_label2(i, cur, fmt):
    if is_current(i, cur, fmt):
        return tr(32301)
    if i == GALLERY_ENTRY:
        return tr(32402)
    if isinstance(i, str):
        d = G.load_design(DESIGNS_DIR, i[len(DL):])
        return trf(32404, d["author"]) if d else ""
    return ""


def choose_format():
    """Ohne passendes laufendes Video: erst Format, dann Stil. Zurueck beendet.
    Letzter Eintrag: Designs aus der Galerie."""
    pre = 0
    while True:
        items = []
        for fmt in FORMATS:
            cur = current_key(fmt)
            items.append(make_item(tr(FMT_LABEL[fmt]), style_label(cur, fmt), style_thumb(cur), False))
        items.append(make_item(tr(32312), tr(32402), THUMB_GALLERY, False))
        sel = xbmcgui.Dialog().select(tr(32300), items, preselect=pre, useDetails=True)
        if sel < 0:
            return
        pre = sel
        if sel == len(FORMATS):
            gallery_dialog()
            continue
        choose_style(FORMATS[sel], live=False)


# --- Galerie ------------------------------------------------------------------------

ERR_TEXT = {"offline": 32420, "http": 32421, "invalid": 32422, "hash": 32423, "path": 32424,
            "size": 32425, "ssl": 32426, "cancel": 32427}


def error_text(e):
    t = tr(ERR_TEXT.get(getattr(e, "code", ""), 32422))
    detail = getattr(e, "detail", "") or ""
    if "%s" in t:
        return t % detail
    return t + ("\n[%s]" % detail if detail and getattr(e, "code", "") in ("invalid", "path", "size") else "")


def _kodi_get(url):
    """Zweiter Transportweg ueber Kodis curl (nur bei SSL-Problemen von Python)."""
    f = xbmcvfs.File(url)
    try:
        return bytes(f.readBytes())
    finally:
        f.close()


def notify(msg, icon=None):
    try:
        xbmcgui.Dialog().notification(tr(32400), msg, icon or THUMB_GALLERY, 4000)
    except Exception:
        pass


def fmt_names(fmts):
    return ", ".join(tr(FMT_LABEL[f]) for f in FORMATS if f in fmts)


def reset_usage(did):
    """Formate, die ein entferntes Design nutzen, auf den Standardstil (0) zuruecksetzen."""
    a = xbmcaddon.Addon()
    hit = False
    for fmt in FORMATS:
        if current_style(fmt) == STYLE_GALLERY and a.getSetting(GALLERY_SETTING[fmt]) == did:
            a.setSetting(STYLE_SETTING[fmt], "0")
            a.setSetting(GALLERY_SETTING[fmt], "")
            hit = True
    return hit


def used_by(did):
    a = xbmcaddon.Addon()
    return [f for f in FORMATS if current_style(f) == STYLE_GALLERY and a.getSetting(GALLERY_SETTING[f]) == did]


def install_with_progress(entry):
    dlg = xbmcgui.DialogProgress()
    dlg.create(tr(32400), tr(32417))

    def progress(n, total, name):
        dlg.update(int(100 * n / max(1, total)), "%s\n%s" % (tr(32417), name))
        return not dlg.iscanceled()
    try:
        return G.install(entry, DESIGNS_DIR, progress=progress)
    finally:
        dlg.close()


def gallery_dialog():
    """Designliste aus dem Repo zeigen; installieren / aktualisieren / entfernen.
    Rueckgabe True, wenn sich etwas an den installierten Designs geaendert hat."""
    G.FALLBACK_GET = _kodi_get
    try:
        entries, cached, err = G.fetch_index(GALLERY_CACHE)
    except G.GalleryError as e:
        xbmc.log("[service.tvframe43] Galerie nicht ladbar: %s" % e, xbmc.LOGWARNING)
        local = installed_designs()
        if not local:
            xbmcgui.Dialog().ok(tr(32400), error_text(e))
            return False
        notify(tr(32419))
        entries, cached = [], True
    else:
        if cached:
            xbmc.log("[service.tvframe43] Galerie offline (%s), gespeicherte Liste" % err, xbmc.LOGWARNING)
            notify(tr(32428))
    previews = {}
    changed = False
    pre = 0
    while True:
        local = {d["id"]: d for d in installed_designs()}
        rows = [("remote", e) for e in entries] + \
               [("local", d) for i, d in sorted(local.items()) if i not in set(e["id"] for e in entries)]
        if not rows:
            xbmcgui.Dialog().ok(tr(32400), tr(32429))
            return changed
        items, states = [], []
        for kind, e in rows:
            if kind == "remote":
                state = G.local_state(DESIGNS_DIR, e)
                fmts = e["formats"]
                if e["id"] not in previews and not cached:
                    previews[e["id"]] = G.preview_file(GALLERY_CACHE, e)
                thumb = previews.get(e["id"])
            else:
                state, fmts = "local", list(e["formats"])
                thumb = None
            if not thumb and e["id"] in local and local[e["id"]].get("preview"):
                thumb = os.path.join(local[e["id"]]["dir"], local[e["id"]]["preview"])
            st = {"none": 32407, "same": 32405, "update": 32406, "local": 32408}[state]
            label2 = " · ".join(x for x in (trf(32404, e["author"]), "v" + e["version"],
                                             fmt_names(fmts), tr(st)) if x)
            items.append(make_item(design_name(e), label2, thumb or THUMB_GALLERY, state in ("same", "update", "local")))
            states.append(state)
        sel = xbmcgui.Dialog().select(tr(32400) + (" (%s)" % tr(32428) if cached else ""), items,
                                      preselect=min(pre, len(rows) - 1), useDetails=True)
        if sel < 0:
            return changed
        pre = sel
        kind, e = rows[sel]
        state = states[sel]
        actions = {"none": ["install"], "update": ["install", "remove"], "same": ["install", "remove"],
                   "local": ["remove"]}[state]
        if state == "local" or cached:
            actions = [a for a in actions if a != "install"] or actions
        labels = {"install": tr({"none": 32410, "update": 32411}.get(state, 32412)), "remove": tr(32413)}
        desc = (e.get("description_en") if english() else "") or e.get("description") or ""
        if len(actions) == 1 and actions[0] == "install":
            if not xbmcgui.Dialog().yesno(design_name(e), "%s\n%s" % (desc, trf(32418, fmt_names(e["formats"])))):
                continue
            act = "install"
        else:
            a = xbmcgui.Dialog().select(design_name(e), [labels[x] for x in actions])
            if a < 0:
                continue
            act = actions[a]
        if act == "install":
            if cached:
                xbmcgui.Dialog().ok(tr(32400), tr(32420))
                continue
            try:
                d = install_with_progress(e)
            except G.GalleryError as ex:
                xbmc.log("[service.tvframe43] Installation %s fehlgeschlagen: %s" % (e["id"], ex), xbmc.LOGWARNING)
                if ex.code != "cancel":
                    xbmcgui.Dialog().ok(tr(32400), error_text(ex))
                continue
            changed = True
            xbmc.log("[service.tvframe43] Galerie-Design installiert: %s v%s" % (d["id"], d["version"]), xbmc.LOGINFO)
            notify(trf(32415, design_name(d)))
            if used_by(d["id"]):
                ping()
        elif act == "remove":
            if not xbmcgui.Dialog().yesno(tr(32400), trf(32414, design_name(e))):
                continue
            if G.remove(DESIGNS_DIR, e["id"]):
                changed = True
                if reset_usage(e["id"]):
                    ping()
                xbmc.log("[service.tvframe43] Galerie-Design entfernt: %s" % e["id"], xbmc.LOGINFO)
                notify(trf(32416, design_name(e)))


def choose(player=None):
    player = player or xbmc.Player()
    try:
        migrate_settings()
    except Exception as e:
        xbmc.log("[service.tvframe43] Stil-Uebernahme fehlgeschlagen: %s" % e, xbmc.LOGWARNING)
    fmt = playing_format(player)
    if fmt:
        choose_style(fmt, live=True)
    else:
        choose_format()


def diag():
    import sys as _s
    win = xbmcgui.Window(12005)
    found = []
    for cid in list(range(1, 200)) + list(range(3000, 3400)):
        try:
            c = win.getControl(cid)
        except Exception:
            continue
        found.append("%d:%s" % (cid, type(c).__name__))
    xbmc.log("[service.tvframe43] DIAG controls 12005: %s" % ", ".join(found), xbmc.LOGINFO)
    if len(_s.argv) > 2 and _s.argv[2] == "purge":
        n = 0
        for cid in range(3000, 3400):
            try:
                c = win.getControl(cid)
            except Exception:
                continue
            if isinstance(c, xbmcgui.ControlImage):
                win.removeControl(c); n += 1
        xbmc.log("[service.tvframe43] DIAG purge removed %d" % n, xbmc.LOGINFO)


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "diag":
        diag()
    elif len(_sys.argv) > 1 and _sys.argv[1] == "gallery":
        gallery_dialog()
    else:
        choose()
