"""Manueller End-to-End-Test ohne echten FUSE-Mount: DB -> Index -> Baum -> Cache."""
import shutil
import sqlite3
import subprocess
from pathlib import Path

from PIL import Image

from digikamfs.config import Config
from digikamfs.digikam_index import load_photos, debug_dump
from digikamfs.tree import build_tree, Tree, DirNode, FileNode
from digikamfs.cache import ensure_cached, cache_path_for, check_exiftool_available

check_exiftool_available()

BASE = Path("/tmp/digikamfs_test")
if BASE.exists():
    shutil.rmtree(BASE)
BASE.mkdir()

photo_root = BASE / "Bilder"
album1 = photo_root / "2026" / "2026-08-xx-SpanienSofi"
album2 = photo_root / "2025" / "2025-12-xx-Weihnachten"
album1.mkdir(parents=True)
album2.mkdir(parents=True)

# Echte JPEGs erzeugen (unterschiedliche Größen/Orientierungen)
def make_jpeg(path, size, color):
    Image.new("RGB", size, color=color).save(path, "JPEG")

make_jpeg(album1 / "IMG_0001.jpg", (4000, 3000), (200, 50, 50))   # 3 Sterne
make_jpeg(album1 / "IMG_0002.jpg", (4000, 3000), (50, 200, 50))   # 4 Sterne -- bekommt unten "echte" Handy-Metadaten
make_jpeg(album1 / "IMG_0003.jpg", (4000, 3000), (50, 50, 200))   # 2 Sterne -> darf nirgends auftauchen
make_jpeg(album2 / "IMG_0100.jpg", (4000, 3000), (200, 200, 50))  # 5 Sterne -> muss in 3 und 4 Sterne auftauchen
# Video simulieren (category=2) -- sollte trotz hoher Bewertung ignoriert werden
(album1 / "CLIP_0001.mp4").write_bytes(b"not a real video, just bytes")

# IMG_0002.jpg bekommt realistische Handy-Metadaten: Orientation=6 (Hochformat-
# Aufnahme, aber Sensor-Rohdaten liegen als 4000x3000 Querformat vor -- genau
# wie bei echten Smartphone-Fotos), plus GPS und XMP.
subprocess.run(
    [
        "exiftool", "-q", "-overwrite_original",
        "-Orientation#=6", "-Make=TestPhone",
        "-GPSLatitudeRef=N", "-GPSLatitude=52.5",
        "-XMP-dc:Subject=SpanienSofi", "-XMP-dc:Creator=Testfotograf",
        str(album1 / "IMG_0002.jpg"),
    ],
    check=True,
)

db_path = BASE / "digikam4.db"
conn = sqlite3.connect(db_path)
conn.executescript("""
CREATE TABLE AlbumRoots (id INTEGER PRIMARY KEY, specificPath TEXT);
CREATE TABLE Albums (id INTEGER PRIMARY KEY, albumRoot INTEGER, relativePath TEXT);
CREATE TABLE Images (id INTEGER PRIMARY KEY, album INTEGER, name TEXT, status INTEGER, category INTEGER);
CREATE TABLE ImageInformation (imageid INTEGER PRIMARY KEY, rating INTEGER, creationDate TEXT, digitizationDate TEXT);
""")
conn.execute("INSERT INTO AlbumRoots VALUES (1, ?)", (str(photo_root),))
conn.execute("INSERT INTO Albums VALUES (1, 1, '/2026/2026-08-xx-SpanienSofi')")
conn.execute("INSERT INTO Albums VALUES (2, 1, '/2025/2025-12-xx-Weihnachten')")

# creationDate absichtlich weit weg von der tatsächlichen Datei-mtime (die ist
# "jetzt", beim Testlauf erzeugt) -- so lässt sich zweifelsfrei prüfen, dass
# wirklich das Aufnahmedatum und nicht die Datei-mtime anzeigt wird.
# IMG_0001 bekommt bewusst KEIN Datum (NULL), um den Fallback auf fs_mtime zu testen.
rows = [
    # iid, album, name, status, category, rating, creation_date, digitization_date
    (1, 1, "IMG_0001.jpg", 1, 1, 3, None, None),
    (2, 1, "IMG_0002.jpg", 1, 1, 4, "2026-08-20T10:15:00", "2026-08-20T10:15:00"),
    (3, 1, "IMG_0003.jpg", 1, 1, 2, "2026-08-20T10:20:00", None),
    (4, 1, "CLIP_0001.mp4", 1, 2, 5, "2026-08-20T10:25:00", None),  # Video -> muss ignoriert werden
    (5, 2, "IMG_0100.jpg", 1, 1, 5, "2025-12-24T18:00:00", None),
]
for iid, album, name, status, category, rating, creation_date, digitization_date in rows:
    conn.execute("INSERT INTO Images VALUES (?,?,?,?,?)", (iid, album, name, status, category))
    conn.execute(
        "INSERT INTO ImageInformation VALUES (?,?,?,?)",
        (iid, rating, creation_date, digitization_date),
    )
conn.commit()
conn.close()

print("=== debug_dump ===")
debug_dump(db_path)

cfg = Config(
    digikam_db=db_path,
    cache_dir=BASE / "cache",
    profiles={"Original": None, "QHD": 2560},
    star_levels=[3, 4],
    jpeg_quality=85,
    refresh_interval_seconds=300,
    min_status=1,
    image_category=1,
    allow_other=False,
)

print("\n=== load_photos ===")
photos = load_photos(cfg.digikam_db, min(cfg.star_levels), cfg.min_status, cfg.image_category)
for p in photos:
    print(" ", p.rel_dir, p.filename, "rating=", p.rating, "capture_time=", p.capture_time)
assert len(photos) == 3, f"erwartet 3 Fotos (2-Sterne-Bild und Video raus), bekommen {len(photos)}"

print("\n=== Aufnahmedatum vs. Datei-mtime ===")
from datetime import datetime
by_name = {p.filename: p for p in photos}

img2 = by_name["IMG_0002.jpg"]
expected_ts = datetime.fromisoformat("2026-08-20T10:15:00").timestamp()
assert img2.capture_time == expected_ts, f"Aufnahmedatum falsch geparst: {img2.capture_time} != {expected_ts}"
assert abs(img2.capture_time - img2.fs_mtime) > 3600, "capture_time sollte sich deutlich von der (aktuellen) Datei-mtime unterscheiden"
print(f"IMG_0002.jpg: creationDate korrekt geparst und weicht wie erwartet von fs_mtime ab (Differenz {abs(img2.capture_time - img2.fs_mtime):.0f}s)")

img1 = by_name["IMG_0001.jpg"]
assert img1.capture_time == img1.fs_mtime, "Ohne creationDate/digitizationDate muss auf fs_mtime zurückgefallen werden"
print("IMG_0001.jpg (kein Datum in DB): Fallback auf fs_mtime greift korrekt")

img100 = by_name["IMG_0100.jpg"]
expected_ts_100 = datetime.fromisoformat("2025-12-24T18:00:00").timestamp()
assert img100.capture_time == expected_ts_100
print("IMG_0100.jpg: creationDate korrekt geparst")

print("\n=== build_tree + Tree (Inodes) ===")
root = build_tree(photos, cfg.profiles, cfg.star_levels)
tree = Tree(root)
print(f"{len(tree.inode_to_node)} Inodes gesamt")

def ls(path=""):
    inode = tree.path_to_inode[path]
    node = tree.inode_to_node[inode]
    assert isinstance(node, DirNode)
    return sorted(node.children.keys())

print("root:", ls(""))
print("Original:", ls("Original"))
print("QHD:", ls("QHD"))
print("QHD/2026:", ls("QHD/2026"))
print("QHD/2026/2026-08-xx-SpanienSofi:", ls("QHD/2026/2026-08-xx-SpanienSofi"))
print("QHD/2026/2026-08-xx-SpanienSofi/3Sterne:", ls("QHD/2026/2026-08-xx-SpanienSofi/3Sterne"))
print("QHD/2026/2026-08-xx-SpanienSofi/4Sterne:", ls("QHD/2026/2026-08-xx-SpanienSofi/4Sterne"))
print("QHD/2025/2025-12-xx-Weihnachten/3Sterne:", ls("QHD/2025/2025-12-xx-Weihnachten/3Sterne"))
print("QHD/2025/2025-12-xx-Weihnachten/4Sterne:", ls("QHD/2025/2025-12-xx-Weihnachten/4Sterne"))

assert ls("QHD/2026/2026-08-xx-SpanienSofi/3Sterne") == ["IMG_0001.jpg", "IMG_0002.jpg"]
assert ls("QHD/2026/2026-08-xx-SpanienSofi/4Sterne") == ["IMG_0002.jpg"]
assert ls("QHD/2025/2025-12-xx-Weihnachten/3Sterne") == ["IMG_0100.jpg"]
assert ls("QHD/2025/2025-12-xx-Weihnachten/4Sterne") == ["IMG_0100.jpg"]
assert "CLIP_0001.mp4" not in str(tree.path_to_inode.keys())

# Stabilität der Inode-Nummern über einen Rebuild hinweg prüfen
tree2 = Tree(build_tree(photos, cfg.profiles, cfg.star_levels))
p = "QHD/2026/2026-08-xx-SpanienSofi/3Sterne/IMG_0001.jpg"
assert tree.path_to_inode[p] == tree2.path_to_inode[p], "Inode-Nummer nicht stabil über Rebuild!"
print("\nInode-Stabilität über Rebuild: OK")

print("\n=== Resize/Cache + Metadaten-Erhaltung ===")
node = tree.inode_to_node[tree.path_to_inode["QHD/2026/2026-08-xx-SpanienSofi/4Sterne/IMG_0002.jpg"]]
assert isinstance(node, FileNode)
cache_file = cache_path_for(cfg.cache_dir, node.profile_name, node.source_path)
result_path = ensure_cached(node.source_path, cache_file, node.profile_max_dim, cfg.jpeg_quality)
with Image.open(result_path) as img:
    print("Originalgröße: 4000x3000 -> verkleinert auf:", img.size)
    assert max(img.size) <= 2560, "Verkleinerung hat max_dim nicht eingehalten"
    assert img.size == (2560, 1920), f"unerwartete Zielgröße: {img.size}"

def exif_tag(path, tag):
    out = subprocess.run(
        ["exiftool", "-s3", f"-{tag}", str(path)],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()

print("Metadaten-Vergleich Original vs. Cache:")
for tag in ["Orientation", "Make", "GPSLatitudeRef", "XMP-dc:Subject", "XMP-dc:Creator"]:
    src_val = exif_tag(node.source_path, tag)
    cache_val = exif_tag(result_path, tag)
    print(f"  {tag}: Original={src_val!r}  Cache={cache_val!r}")
    assert src_val == cache_val, f"Metadaten-Tag {tag} ging beim Resize verloren/veraendert!"

# Die alten Pixel-Dimensions-Tags dürfen NICHT unverändert übernommen worden
# sein (sonst stünden dort die falschen, alten 4000x3000-Werte).
stale_dim = exif_tag(result_path, "ExifImageWidth")
assert stale_dim in ("", "2560"), f"veraltetes ExifImageWidth wurde übernommen: {stale_dim!r}"
print("Keine veralteten Dimensions-Tags übernommen: OK")
print("Metadaten (EXIF-Orientation, Make, GPS, XMP) bleiben erhalten: OK")

# Cache-Hit beim zweiten Aufruf (mtime-Vergleich)
mtime_before = result_path.stat().st_mtime
result_path2 = ensure_cached(node.source_path, cache_file, node.profile_max_dim, cfg.jpeg_quality)
assert result_path2.stat().st_mtime == mtime_before, "Cache wurde unnötig neu erzeugt"
print("Cache-Hit beim zweiten Aufruf: OK")

# Original-Profil: Passthrough, kein Cache-File
orig_node = tree.inode_to_node[tree.path_to_inode["Original/2026/2026-08-xx-SpanienSofi/3Sterne/IMG_0001.jpg"]]
result = ensure_cached(orig_node.source_path, cache_path_for(cfg.cache_dir, "Original", orig_node.source_path), orig_node.profile_max_dim, cfg.jpeg_quality)
assert result == orig_node.source_path, "Original-Profil sollte direkt den Quellpfad liefern"
print("Original-Profil Passthrough: OK")

print("\nALLE TESTS BESTANDEN")
