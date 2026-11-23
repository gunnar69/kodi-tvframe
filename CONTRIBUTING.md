# Designs beitragen

Danke für dein Design! So kommt es in die Galerie:

1. Repo forken und einen Ordner `designs/<id>/` anlegen. Die `<id>` besteht nur aus Kleinbuchstaben, Ziffern, `-` und `_` (max. 48 Zeichen) und ist gleichzeitig der Ordnername.
2. Je Format ein PNG mit **1920×1080 Pixeln** anlegen. Die Mattscheibe ist dabei transparent (Alpha 0). Außerdem gehört zu jedem PNG eine Koordinaten-JSON (`canvas`, `screen`, `overlap`). Mindestens ein Format ist Pflicht:
   - `43` – 4:3-Videos (ca. 1,20–1,45)
   - `wide` – Breitbild 2,35:1 / 2,39:1
   - `w2` – 2:1 (ca. 1,90–2,08)
3. `preview.jpg` (480×270) und `design.json` anlegen. Das Format steht in der [README](README.md).
4. `python3 tools/build_index.py` ausführen. Das Skript prüft das Design und aktualisiert `index.json`. Mit `--check` prüft es nur.
5. Einen Pull Request mit dem neuen Ordner **und** der aktualisierten `index.json` stellen.

## Regeln

- Nur Dateien vom Typ `.png`, `.json` und `.jpg`. Einfache Dateinamen (`A–Z a–z 0–9 . _ -`), keine Unterordner. Jede Datei muss in `design.json` verwendet werden.
- Pro Datei höchstens 12 MB, pro Design höchstens 40 MB. Bitte trotzdem klein halten: PNGs optimieren, ein Rahmen ist meist unter 1,5 MB.
- Die Mattscheibe muss vollständig im Bild liegen, und `canvas` muss der PNG-Größe entsprechen.
- Bei einer Änderung `version` erhöhen. Das Add-on zeigt dann „Update verfügbar“.
- Rechte: Mit `"rights_confirmed": true` bestätigst du, dass du alle Bilder verbreiten darfst und sie unter der angegebenen `license` stehen. Keine Markenlogos, keine fremden Fotos ohne Erlaubnis.
- In Designs gibt es keinen Code. Das Add-on lädt ausschließlich Bilddaten und prüft jede Datei per sha256.

Für Änderungen am Add-on selbst (`addon/`) gilt: kleine, nachvollziehbare Pull Requests. Gespeicherte Einstellungswerte dürfen ihre Bedeutung nicht ändern.
