"""Liest Alben, Dateien und Sterne-Bewertungen aus der digikam-SQLite-Datenbank."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("digikamfs.index")

# Diese Query verbindet drei digikam-Kerntabellen:
#   AlbumRoots      -- eine Zeile pro Sammlungs-Root (z.B. /srv/Bilder)
#   Albums          -- eine Zeile pro Ordner (relativePath, z.B. /2026/2026-08-xx-SpanienSofi)
#   Images          -- eine Zeile pro Datei
#   ImageInformation -- rating, Aufnahmedatum u.a., verknüpft über imageid
_QUERY = """
    SELECT ar.specificPath AS root,
           al.relativePath AS rel,
           im.name AS filename,
           ii.rating AS rating,
           ii.creationDate AS creation_date,
           ii.digitizationDate AS digitization_date
    FROM Images im
    JOIN Albums al ON im.album = al.id
    JOIN AlbumRoots ar ON al.albumRoot = ar.id
    LEFT JOIN ImageInformation ii ON ii.imageid = im.id
    WHERE im.status = ?
      AND im.category = ?
      AND ii.rating >= ?
"""


@dataclass(frozen=True)
class Photo:
    abs_path: Path
    rel_dir: str      # Album-Pfad ohne führenden/nachfolgenden Slash, z.B. "2026/2026-08-xx-SpanienSofi"
    filename: str
    rating: int
    size: int
    fs_mtime: float       # tatsächliches Dateisystem-mtime -- nur für Cache-Invalidierung
    capture_time: float   # Aufnahmedatum (aus digikam-DB, Fallback fs_mtime) -- für Anzeige


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    # mode=ro erlaubt parallelen lesenden Zugriff, auch während Digikam selbst
    # die Datenbank offen hält (WAL-Modus vorausgesetzt, digikam-Standard).
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_digikam_date(raw: Optional[str]) -> Optional[float]:
    """digikam speichert Datumswerte i.d.R. als 'YYYY-MM-DDTHH:MM:SS' (lokale
    Zeit, ohne Zeitzone). Robust gegen ein paar naheliegende Varianten;
    liefert None statt einer Exception, falls das Format doch abweicht --
    ein einzelnes komisches Datum soll nicht den ganzen Index-Aufbau killen."""
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidate).timestamp()
        except ValueError:
            continue
    log.warning("Konnte Datum nicht parsen, benutze Datei-mtime als Fallback: %r", raw)
    return None


def load_photos(db_path: Path, min_rating: int, status_visible: int, image_category: int) -> List[Photo]:
    """Lädt alle Fotos mit rating >= min_rating aus der digikam-DB.

    Gibt für jedes Foto das tatsächliche rating zurück (nicht nur ein Bool),
    damit der Baum-Aufbau die einzelnen Sterne-Stufen selbst bucketen kann.
    """
    conn = _connect_readonly(db_path)
    photos: List[Photo] = []
    skipped_missing = 0
    fallback_date_count = 0
    try:
        cursor = conn.execute(_QUERY, (status_visible, image_category, min_rating))
        for row in cursor:
            root = row["root"] or ""
            rel = row["rel"] or ""
            abs_path = Path(root) / rel.lstrip("/") / row["filename"]
            try:
                st = abs_path.stat()
            except OSError:
                # DB und Dateisystem sind (kurzzeitig) inkonsistent, z.B. weil
                # eine Datei außerhalb von Digikam gelöscht wurde. Überspringen
                # statt abzubrechen.
                skipped_missing += 1
                continue

            capture_time = _parse_digikam_date(row["creation_date"])
            if capture_time is None:
                capture_time = _parse_digikam_date(row["digitization_date"])
            if capture_time is None:
                capture_time = st.st_mtime
                fallback_date_count += 1

            photos.append(
                Photo(
                    abs_path=abs_path,
                    rel_dir=rel.strip("/"),
                    filename=row["filename"],
                    rating=int(row["rating"] or 0),
                    size=st.st_size,
                    fs_mtime=st.st_mtime,
                    capture_time=capture_time,
                )
            )
    finally:
        conn.close()

    if skipped_missing:
        log.warning("%d Einträge aus der DB übersprungen (Datei nicht gefunden)", skipped_missing)
    if fallback_date_count:
        log.info("%d Fotos ohne verwertbares Aufnahmedatum, Datei-mtime als Anzeigedatum benutzt", fallback_date_count)
    log.info("%d Fotos mit rating >= %d geladen", len(photos), min_rating)
    return photos


def debug_dump(db_path: Path) -> None:
    """Gibt Verteilungen von status/category/rating sowie Beispiel-Datumswerte
    aus, um die in config.py verwendeten Konstanten und das Datumsformat gegen
    die eigene DB zu verifizieren."""
    conn = _connect_readonly(db_path)
    try:
        print("Status-Werte in Images (status, Anzahl):")
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM Images GROUP BY status ORDER BY n DESC"):
            print(f"  {row['status']!r}: {row['n']}")

        print("\nKategorie-Werte in Images (category, Anzahl):")
        for row in conn.execute("SELECT category, COUNT(*) AS n FROM Images GROUP BY category ORDER BY n DESC"):
            print(f"  {row['category']!r}: {row['n']}")

        print("\nRating-Verteilung in ImageInformation (rating, Anzahl):")
        for row in conn.execute("SELECT rating, COUNT(*) AS n FROM ImageInformation GROUP BY rating ORDER BY rating"):
            print(f"  {row['rating']!r}: {row['n']}")

        print("\nBeispiel-Werte für creationDate (zur Formatprüfung, sollte 'YYYY-MM-DDTHH:MM:SS' sein):")
        for row in conn.execute(
            "SELECT creationDate FROM ImageInformation WHERE creationDate IS NOT NULL LIMIT 5"
        ):
            print(f"  {row['creationDate']!r}")
    finally:
        conn.close()
