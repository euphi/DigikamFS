"""On-the-fly-Verkleinerung von JPEGs mit persistentem Datei-Cache.

Die Verkleinerung selbst übernimmt Pillow (schnell, keine Metadaten-Handhabung
nötig), alle Metadaten (EXIF inkl. Orientierung, IPTC, XMP, GPS, ICC-Profil,
...) werden danach per `exiftool` 1:1 von der Originaldatei auf die
verkleinerte Kopie übertragen. Das ist deutlich robuster als Pillows eigene,
sehr limitierte EXIF/XMP-Schreibunterstützung -- exiftool ist genau für
diesen "alle Metadaten von A nach B kopieren"-Anwendungsfall gebaut und wird
im Kern auch von digikam selbst (über libexiv2) für Metadaten verwendet.

Absichtlich NICHT per Pillow vor-rotiert (kein `exif_transpose`): die
Original-Pixel bleiben unangetastet in ihrer rohen Sensor-Orientierung, und
die Orientation-EXIF-Tag wird unverändert mitkopiert. Ein Viewer, der EXIF-
Orientierung respektiert, zeigt das verkleinerte Bild dadurch exakt so an
wie das Original -- inklusive korrekter Rotation.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from PIL import Image

log = logging.getLogger("digikamfs.cache")

EXIFTOOL_BIN = shutil.which("exiftool")

# Tags, die nach dem Resize nicht mehr stimmen und daher NICHT von der
# Originaldatei übernommen werden (die tatsächliche Pixelgröße steht ohnehin
# korrekt im JPEG-SOF-Marker und wird von jedem Leser inkl. exiftool selbst
# automatisch daraus bestimmt).
_EXCLUDE_TAGS = ["ExifIFD:ExifImageWidth", "ExifIFD:ExifImageHeight"]


class MetadataCopyError(RuntimeError):
    pass


def check_exiftool_available() -> None:
    if EXIFTOOL_BIN is None:
        raise MetadataCopyError(
            "'exiftool' wurde nicht gefunden. Bitte installieren, z.B. unter "
            "Debian/Ubuntu mit: sudo apt install libimage-exiftool-perl"
        )


def _copy_metadata(source_path: Path, target_path: Path) -> None:
    check_exiftool_available()
    cmd = [
        EXIFTOOL_BIN, "-q", "-q", "-overwrite_original",
        "-TagsFromFile", str(source_path), "-all:all",
    ]
    cmd += [f"--{tag}" for tag in _EXCLUDE_TAGS]
    cmd.append(str(target_path))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MetadataCopyError(
            f"exiftool-Metadatenkopie fehlgeschlagen ({source_path} -> {target_path}): "
            f"{result.stderr.strip()}"
        )


def cache_path_for(cache_root: Path, profile_name: str, source_path: Path) -> Path:
    # Hash des vollen Quellpfads verhindert Kollisionen bei gleichnamigen
    # Dateien aus unterschiedlichen Alben; der Klartext-Stamm bleibt im
    # Dateinamen für einfacheres manuelles Debugging im Cache-Verzeichnis.
    h = hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:16]
    safe_stem = source_path.stem.replace("/", "_")
    return cache_root / profile_name / f"{h}_{safe_stem}.jpg"


def is_cache_valid(cache_file: Path, source_mtime: float) -> bool:
    try:
        return cache_file.stat().st_mtime >= source_mtime
    except FileNotFoundError:
        return False


def ensure_cached(source_path: Path, cache_file: Path, max_dim: Optional[int], jpeg_quality: int) -> Path:
    """Stellt sicher, dass cache_file eine aktuelle, verkleinerte Version von
    source_path ist (inkl. Original-Metadaten), und gibt den Pfad zurück, aus
    dem gelesen werden soll.

    max_dim=None bedeutet Passthrough (Original-Profil): es wird direkt der
    Quellpfad zurückgegeben, ohne etwas zu cachen.
    """
    if max_dim is None:
        return source_path

    source_mtime = source_path.stat().st_mtime
    if is_cache_valid(cache_file, source_mtime):
        return cache_file

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = cache_file.with_name(
        f".tmp-{os.getpid()}-{threading.get_ident()}-{cache_file.name}"
    )
    try:
        with Image.open(source_path) as img:
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            img.save(tmp_file, "JPEG", quality=jpeg_quality, optimize=True)

        _copy_metadata(source_path, tmp_file)

        # mtime des Caches an die Quelle koppeln: so bleibt der obige
        # is_cache_valid()-Vergleich auch nach einem Prozess-Neustart korrekt.
        # Muss NACH exiftool passieren, da exiftool die mtime der Zieldatei
        # sonst auf "jetzt" setzt.
        os.utime(tmp_file, (source_mtime, source_mtime))
        os.replace(tmp_file, cache_file)  # atomar, keine kaputten Teil-Dateien für parallele Leser
    except Exception:
        tmp_file.unlink(missing_ok=True)
        raise
    return cache_file

