# DigikamFS

Virtuelles Nur-Lese-Dateisystem, das Alben und Sterne-Bewertungen direkt aus
der digiKam-SQLite-Datenbank liest und als Baum

```
<ExportRoot>/<Auflösungsprofil>/<Jahr>/<Album>/<N>Sterne/<Dateiname>
```

per FUSE bereitstellt. Fotos werden beim ersten Zugriff auf die konfigurierte
Auflösung herunterskaliert und danach als Datei gecacht. Der Mount kann
anschließend ganz normal per SMB/DLNA freigegeben oder als Kopierquelle
(rsync, Nextcloud-Client, ...) benutzt werden.

Videos und alles außer Standard-JPEGs (z.B. RAW) werden aktuell ignoriert.

*The English version of this documentation is available in
[README.md](README.md).*

## Installation

```bash
sudo apt install libfuse3-dev fuse3 pkg-config python3-venv libimage-exiftool-perl
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

Der Editable-Install liefert das Kommando `digikamfs` und macht das Paket
unabhängig vom Arbeitsverzeichnis importierbar -- worauf die systemd-Unit
weiter unten aufbaut. Ohne ihn funktionieren die folgenden Befehle weiterhin,
aber nur aus dem Projektverzeichnis heraus und in der Schreibweise
`python3 -m digikamfs`.

`exiftool` wird für alle Profile mit Verkleinerung benötigt und überträgt
EXIF/IPTC/XMP/GPS von der Originaldatei auf die verkleinerte Version. Wird
ausschließlich das `Original`-Passthrough-Profil genutzt, ist es nicht nötig
-- das Dateisystem prüft das beim Start und meldet klar, falls `exiftool`
fehlt und gebraucht würde.

## Vor dem ersten Einsatz: DB-Annahmen verifizieren

Der Code filtert Fotos über `Images.status = 1` (sichtbar, nicht
Papierkorb/versteckt) und `Images.category = 1` (Bild, nicht Video/Audio).
Diese Werte stimmen mit aktuellen digiKam-Versionen überein, sollten aber
einmal gegen die eigene DB geprüft werden, bevor man sich darauf verlässt:

```bash
python3 -m digikamfs debug-db -c config.yaml
```

Zeigt die tatsächlich vorkommenden `status`-, `category`- und
`rating`-Werte mit Anzahl. Falls die eigenen Werte abweichen, in
`config.yaml` `status_visible_value` / `category_image_value` entsprechend
setzen.

## Konfiguration

Siehe `config.example.yaml`, kopieren nach `config.yaml` und anpassen:

- `digikam_db`: Pfad zur digikam4.db
- `cache_dir`: wo die verkleinerten JPEGs abgelegt werden
- `profiles`: ein Ordner pro Auflösung auf oberster Ebene; `null` = Original
  ohne Verkleinerung (Passthrough, kein zusätzlicher Speicherbedarf)
- `star_levels`: welche `NSterne`-Ordner erzeugt werden (Standard `[3, 4]`)

## Benutzung

**Mounten** (blockiert im Vordergrund, für Dauerbetrieb siehe systemd-Unit
unten):

```bash
mkdir -p /srv/DigikamFS
python3 -m digikamfs mount /srv/DigikamFS -c config.yaml
```

Beenden mit Strg+C, oder von einem anderen Terminal:
`fusermount3 -u /srv/DigikamFS`

**Cache vorwärmen** (optional -- fürs normale Browsen nicht nötig, da
`getattr`/`ls -l` nie generieren, siehe unten. Nur sinnvoll, wenn gezielt vor
einer erwarteten Nutzungsspitze -- z.B. einer Familienfeier am DLNA-Fernseher
-- alles vorab gecacht werden soll):

```bash
python3 -m digikamfs prewarm -c config.yaml
```

## Funktionsweise / Design-Entscheidungen

- **Index im Speicher**: Die DB wird nicht bei jedem `ls` neu abgefragt,
  sondern einmalig beim Start und danach alle `refresh_interval_seconds`
  im Hintergrund neu eingelesen (Referenz-Swap, laufende Zugriffe werden
  nicht unterbrochen).
- **Stabile Inode-Nummern**: Jede Inode wird deterministisch aus dem Hash
  des virtuellen Pfads berechnet (nicht fortlaufend gezählt). Dadurch bleibt
  dieselbe Datei über einen Index-Refresh hinweg dieselbe Inode-Nummer –
  wichtig für Kernel-Caching und SMB-Clients. Bei sehr vielen Dateien ist
  eine Hash-Kollision theoretisch möglich, praktisch für eine private
  Fotosammlung aber vernachlässigbar.
- **Echter Cache, nur bei echtem Zugriff**: `getattr`/`readdir` (also reines
  Browsen/`ls -l`) generieren *nie* etwas -- sie prüfen nur billig, ob schon
  eine gültige Cache-Datei existiert, und melden sonst die Originalgröße als
  Schätzung. Verkleinert wird ausschließlich in `open()`, also wenn eine
  Datei tatsächlich gelesen/kopiert/angezeigt wird. Dadurch bleibt Browsen
  immer schnell und der Cache wächst nur mit dem, was wirklich genutzt wird
  -- nicht mit allem, was je in einem Ordner angezeigt wurde.
- **Prefetch**: Beim tatsächlichen Öffnen einer Datei wird im Hintergrund
  (ohne den aktuellen Zugriff zu verzögern) direkt die alphabetisch nächste
  Datei im selben `NSterne`-Ordner mit umgewandelt. Beim Durchklicken eines
  Albums liegt die jeweils nächste Datei damit meist schon fertig im Cache,
  bevor sie geöffnet wird. Läuft über eine eigene Hintergrund-Nursery und
  dedupliziert sich mit echten Zugriffen (kein doppeltes Resizing derselben
  Datei, falls beides gleichzeitig passiert).
- **Angezeigtes Datum = Aufnahmedatum**: Die Datei-Zeitstempel (mtime/atime/
  ctime), die über FUSE gemeldet werden, kommen aus `ImageInformation.
  creationDate` in der digiKam-DB (Fallback: `digitizationDate`, dann die
  tatsächliche Datei-mtime, falls beides fehlt) -- nicht aus dem
  Dateisystem-mtime der Originaldatei. Intern für die Cache-Invalidierung
  wird weiterhin die echte Datei-mtime verwendet (davon unabhängig), damit
  Änderungen an der Originaldatei zuverlässig erkannt werden.
- **Metadaten bleiben erhalten**: EXIF (inkl. Orientierung), GPS, IPTC und
  XMP werden nach dem Resize per `exiftool` unverändert von der Original- auf
  die verkleinerte Datei übertragen -- nur die (jetzt falschen) EXIF-eigenen
  Dimensions-Tags (`ExifImageWidth`/`Height`) werden dabei ausgeschlossen,
  die tatsächliche Pixelgröße steht ohnehin korrekt im JPEG selbst. Die
  Pixel werden dabei bewusst NICHT vorab gedreht -- die Orientation-Angabe
  bleibt unverändert, ein Viewer, der sie respektiert, zeigt das verkleinerte
  Bild also genau wie das Original an.
- **Größenschätzung vor dem ersten Öffnen**: Bis eine Datei zum ersten Mal
  geöffnet wurde, zeigt `ls -l` die Original-Dateigröße statt der späteren
  (kleineren) Zielgröße. Kopiertools lesen bis EOF und verlassen sich nicht
  blind auf die gemeldete Größe, das ist also unkritisch -- nur die Anzeige
  vor dem ersten Zugriff ist eine Schätzung nach oben.
- **Cache-Invalidierung**: Eine Cache-Datei gilt als gültig, wenn ihre
  mtime >= mtime der Originaldatei ist. Ändert sich das Original, wird beim
  nächsten Zugriff neu generiert.
- **Atomare Erzeugung**: Resize schreibt in eine temporäre Datei und
  benennt sie erst danach um (`os.replace`) – parallele Leser (mehrere
  SMB-Clients) bekommen nie eine kaputte Teil-Datei.

## Bekannte Grenzen

- Nur Standard-JPEGs werden verarbeitet; RAW-Dateien und Videos werden
  komplett ausgeblendet (laut Anforderung so gewollt).
- Es wird angenommen, dass digiKam-Ratings zuverlässig in der DB stehen. Das
  FS nutzt die DB, da dies deutlich schneller ist, als EXIF/XMP aus jeder
  Datei einzeln zu lesen.
- Kein Schreibzugriff: das FS ist bewusst read-only gemountet.

## Troubleshooting: minidlna/Samba "Permission denied"

FUSE-Mounts sind per Kernel-Default **ausschließlich für den mountenden
Benutzer** zugänglich -- unabhängig davon, welche Dateirechte gemeldet
werden. Läuft minidlna, smbd o.ä. unter einem eigenen Benutzer (z.B.
`minidlna`, nicht der User, der `digikamfs mount` ausgeführt hat), bekommt er
ohne weiteres Zutun ein `Permission denied`, selbst wenn `ls -la` beim
Mount-User `dr-xr-xr-x`/`-r--r--r--` zeigt. Das ist kein digikamfs-Bug,
sondern FUSE-Standardverhalten.

Fix (bereits standardmäßig aktiv, siehe `allow_other: true` in
`config.example.yaml`):

1. In der Config sicherstellen: `allow_other: true`
2. Falls `digikamfs mount` NICHT als root läuft (z.B. via systemd mit
   `User=benutzer`): einmalig in `/etc/fuse.conf` die Zeile
   ```
   user_allow_other
   ```
   einkommentieren (ohne führendes `#`). Läuft der Mount als root, ist dieser
   Schritt nicht nötig.
3. Mount neu starten (`systemctl restart digikamfs` bzw. den Prozess neu
   ausführen).

Danach: als der jeweils andere Benutzer (z.B. `sudo -u minidlna ls
/media/DigikamFS/QHD`) zum Test gegenprüfen, bevor minidlna/Samba selbst
neu gestartet wird.

Falls es danach immer noch nicht geht: prüfen, ob minidlna/smbd über
systemd-Hardening (`ProtectHome=`, `PrivateMounts=`, `ProtectSystem=strict`
o.ä.) in einer eigenen Mount-Namespace läuft, die den FUSE-Mount unter
Umständen gar nicht erst sieht -- das äußert sich dann eher als "kein
Verzeichnis gefunden" statt "Permission denied".

## Beispiel: Samba-Freigabe

```ini
[Fotos]
   path = /srv/DigikamFS
   read only = yes
   browseable = yes
   guest ok = no
```

Eine Freigabe pro Auflösungsprofil lässt sich auch automatisch aus den
Top-Level-Verzeichnissen des Mountpoints erzeugen:

```bash
sudo ./digikamfs/generate_smb_shares.py \
    --mount-point /srv/DigikamFS \
    --smb-user digikamfs \
    --smb-group digikamfs
```

Das Skript schreibt `/etc/samba/digikamfs-shares.conf` atomar; diese Datei
wird per `include` in die `smb.conf` eingebunden. Es muss erneut ausgeführt
werden, wenn Auflösungsprofile hinzugefügt oder entfernt werden.

## Beispiel: systemd-Service für Dauerbetrieb

`/etc/systemd/system/digikamfs.service`:

Eine fertige Vorlage dieser Unit liegt unter
[contrib/digikamfs.service](contrib/digikamfs.service).

```ini
[Unit]
Description=DigikamFS (read-only FUSE view of digiKam albums)
After=local-fs.target
# Samba erst starten, wenn der Mount steht (schadet nicht, wenn smb.service aus ist).
Before=smb.service

[Service]
Type=simple
User=benutzer
Group=benutzer
ExecStart=/pfad/zu/venv/bin/digikamfs mount /srv/DigikamFS -c /pfad/zu/config.yaml
ExecStop=/usr/bin/fusermount3 -u /srv/DigikamFS
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

Zwei Punkte sind dabei wichtig:

- `ExecStart` nutzt das Konsolenskript `digikamfs` aus dem venv, das
  `pip install -e .` voraussetzt (siehe Installation). Die Modul-Schreibweise
  `python3 -m digikamfs` findet das Paket nur über das aktuelle Verzeichnis,
  und systemd startet Dienste in `/` -- sie braucht daher zusätzlich ein
  `WorkingDirectory=/pfad/zum/projekt`.
- Sandbox-Optionen wie `ProtectHome=`, `ProtectSystem=strict` oder
  `PrivateMounts=` dürfen **nicht** ergänzt werden. Sie legen den Dienst in
  eine eigene Mount-Namespace, wodurch der FUSE-Mount für smbd/minidlna
  unsichtbar wird -- der im Troubleshooting beschriebene Fall, der sich
  meist als "kein Verzeichnis gefunden" äußert.

Aktivieren und kontrollieren:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now digikamfs
journalctl -u digikamfs -f
```

Für den nächtlichen Cache-Vorlauf zusätzlich einen Cron-/systemd-Timer auf
`python3 -m digikamfs prewarm -c config.yaml` einrichten.

## Tests

`test_pipeline.py` baut eine synthetische digiKam-DB samt echten Test-JPEGs
auf und prüft die komplette Pipeline (DB-Query, Baumaufbau, Sterne-Filterung,
Video-Ausschluss, Resize, Cache-Hit, Inode-Stabilität) ohne echten Mount:

```bash
python3 test_pipeline.py
```

Wurde bereits gegen einen echten FUSE-Mount verifiziert (readdir, getattr,
open/read, `cp`) – das Projekt ist also funktionsfähig, nicht nur
theoretisch korrekt.

## Lizenz

MIT -- siehe [LICENSE](LICENSE).
