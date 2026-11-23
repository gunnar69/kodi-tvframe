# kodi-tvframe – TV-Rahmen für Kodi

Kodi-Add-on **service.tvframe43** („4:3-Röhrenfernseher“): Es zeigt 4:3-Videos, Breitbild-Filme (2,35:1) und 2:1-Material in einem bildschirmfüllenden Fernsehgehäuse. Das Overlay ist ein PNG mit transparenter Mattscheibe. Zoom und vertikale Position werden aus der Mattscheibe berechnet, 16:9 bleibt unangetastet.

Dieses Repo enthält
- `addon/service.tvframe43/` – den Quellcode des Add-ons (ab Version 1.2.0),
- `designs/<id>/` – die **Design-Galerie**: zusätzliche Rahmen, die das Add-on herunterladen kann,
- `index.json` – die Liste aller Designs mit Prüfsummen (wird von `tools/build_index.py` erzeugt).

- `addon/repository.tvframe43/` – das Repository-Add-on „TV-Rahmen Repository“,
- `repo/` – das Kodi-Repository (addons.xml, Prüfsummen, Zips), erzeugt von `tools/build_repo.py`.

## Installation & automatische Updates

Am einfachsten über das **TV-Rahmen Repository**. Es wird einmal installiert, danach findet Kodi neue Versionen selbst. Gehostet wird es direkt aus diesem GitHub-Repo, es gibt keinen eigenen Server.

1. Die Repository-Zip herunterladen:
   https://raw.githubusercontent.com/gunnar69/kodi-tvframe/main/repo/zips/repository.tvframe43/repository.tvframe43-1.0.0.zip
   Sie kommt z. B. per USB-Stick oder Netzwerkfreigabe aufs Gerät oder einfach in den Download-Ordner.
2. In Kodi: *Einstellungen → Add-ons → Aus ZIP-Datei installieren* → `repository.tvframe43-1.0.0.zip` wählen. Eventuell muss vorher „Unbekannte Quellen“ erlaubt werden.
3. *Add-ons → Aus Repository installieren → TV-Rahmen Repository → Dienst-Add-ons* (bzw. Programm-Add-ons) → **4:3-Röhrenfernseher** → Installieren.
4. Updates: Unter *Einstellungen → System → Add-ons → Updates* „Updates automatisch installieren“ wählen, dann aktualisiert Kodi das Add-on selbst. Alternativ kann man unter *Add-ons → Updates prüfen* manuell nachsehen. Kodi prüft das Repository etwa einmal am Tag. Jedes Paket wird per sha256 geprüft.

Ein bereits per Zip installiertes service.tvframe43 bleibt erhalten. Sobald das Repository installiert ist, erscheint die neuere Version als Update, und die Einstellungen bleiben erhalten.

Ohne Repository geht es auch per Hand: die Zip unter `repo/zips/service.tvframe43/` laden und über *Aus ZIP-Datei installieren* einspielen. Dann gibt es allerdings keine automatischen Updates.

Danach den Rahmen wählen: *Add-on-Einstellungen → Rahmen auswählen (mit Vorschau)* oder während der Wiedergabe über die optional belegbare Taste.

### Neue Version veröffentlichen (für Maintainer)

1. In `addon/service.tvframe43/addon.xml` die `version` erhöhen und einen `<news>`-Eintrag ergänzen.
2. `python3 tools/build_repo.py` ausführen. Das erneuert `repo/addons.xml`, die Prüfsummen und die Zip. Ältere Zips werden aus `repo/` entfernt, sie bleiben aber in der Git-Historie.
3. `python3 tools/build_repo.py --check` und `python3 tools/build_index.py --check` müssen „OK“ melden. Die CI prüft das bei jedem Pull Request ebenfalls.
4. `repo/` zusammen mit dem Code committen und nach `main` pushen. raw.githubusercontent.com cacht etwa 5 Minuten.

Wird geänderter Code ohne höhere Version gebaut, bricht `build_repo.py` ab, denn Kodi würde das Update sonst nie anbieten.

## Design-Galerie im Add-on

Im Auswahldialog öffnet **„Designs aus der Galerie…“** diese Galerie. Dafür lädt das Add-on `index.json` von `raw.githubusercontent.com`. Ein Design wird nur installiert, wenn jede Datei zu Größe und sha256 aus dem Index passt. Erlaubt sind nur `.png`, `.json` und `.jpg` mit einfachen Dateinamen. Abgelegt wird alles im Profilordner (`addon_data/service.tvframe43/designs/<id>/`). Es wird nie Code geladen oder ausgeführt.

Installierte Designs erscheinen danach in der normalen Rahmen-Liste, und zwar nur bei den Formaten, für die sie Dateien haben. Sie lassen sich dort aktualisieren und entfernen.

## Eigenes Design beitragen

Eigene Designs kommen per **Pull Request** dazu: Du legst einen neuen Ordner unter `designs/` an. Wie das genau geht, steht in [CONTRIBUTING.md](CONTRIBUTING.md). Kurzfassung:

```
designs/mein-design/
  design.json        Beschreibung (siehe unten)
  preview.jpg        Vorschau, 480×270
  mein_43.png        Rahmen 1920×1080 mit transparenter Mattscheibe (4:3)
  mein_43.json       Koordinaten der Mattscheibe
  mein_wide.png/json optional: Breitbild 2,35:1
  mein_2x1.png/json  optional: 2:1
```

`design.json`:

```json
{
 "schema": 1,
 "id": "mein-design",
 "name": "Mein Design",
 "name_en": "My design",
 "author": "Dein Name oder GitHub-Name",
 "version": "1.0.0",
 "description": "Kurze Beschreibung (Deutsch)",
 "description_en": "Short description (English)",
 "license": "CC-BY-4.0",
 "rights_confirmed": true,
 "preview": "preview.jpg",
 "formats": {
  "43":   {"image": "mein_43.png",   "frame": "mein_43.json"},
  "wide": {"image": "mein_wide.png", "frame": "mein_wide.json"},
  "w2":   {"image": "mein_2x1.png",  "frame": "mein_2x1.json"}
 }
}
```

Koordinaten-JSON je Format (wie `addon/service.tvframe43/resources/frames/*.json`):

```json
{"canvas": [1920, 1080], "screen": {"x": 304, "y": 10, "w": 1312, "h": 980, "radius": 6}, "overlap": 6}
```

`screen` ist das Rechteck der transparenten Mattscheibe in Pixeln. `overlap` gibt an, wie viele Pixel der Rahmen über das Video ragen darf. Danach `python3 tools/build_index.py` ausführen und `index.json` mit committen.

Optional (ab Add-on 1.3.2): `"centered": {"image": "mein_wide_c.png", "frame": "mein_wide_c.json"}` je Format. Gemeint ist eine Variante, bei der die Mattscheibe genau mittig im Bild sitzt (`y = (1080 - h) / 2`, gleiche Breite und Höhe). Das Add-on nutzt sie bei Filmen mit eingebrannten schwarzen Balken, denn dort liegt der Bildinhalt immer in der Mitte und lässt sich nicht verschieben. Fehlt die Variante, verschiebt das Add-on stattdessen den normalen Rahmen. Ältere Add-on-Versionen ignorieren das Feld.

## Rechte und Lizenz

- Der **Code** steht unter der MIT-Lizenz (siehe `LICENSE`).
- **Designs** tragen ihre eigene Lizenz im Feld `license`. Wer ein Design einreicht, bestätigt mit `"rights_confirmed": true`, dass er die Rechte an allen Bildern besitzt, also selbst erstellt oder passend lizenziert. Keine Markenlogos, keine fremden Fotos ohne Erlaubnis.
- Die Startdesigns von SARAH stehen unter CC0-1.0.
