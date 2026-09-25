from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .cache import MetadataCopyError, cache_path_for, check_exiftool_available, ensure_cached
from .config import Config, ConfigError, load_config
from .digikam_index import debug_dump, load_photos
from .tree import FileNode, Tree, build_tree

log = logging.getLogger("digikamfs")


def _check_prereqs(cfg: Config) -> None:
    if any(max_dim is not None for max_dim in cfg.profiles.values()):
        check_exiftool_available()


def _build_tree(cfg: Config) -> Tree:
    min_rating = min(cfg.star_levels)
    photos = load_photos(cfg.digikam_db, min_rating, cfg.min_status, cfg.image_category)
    root = build_tree(photos, cfg.profiles, cfg.star_levels, cfg.star_dir_format)
    return Tree(root)


def _cmd_debug_db(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    debug_dump(cfg.digikam_db)
    return 0


def _cmd_prewarm(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    _check_prereqs(cfg)
    t0 = time.time()
    tree = _build_tree(cfg)
    targets = [
        n for n in tree.inode_to_node.values()
        if isinstance(n, FileNode) and n.profile_max_dim is not None
    ]
    print(
        f"{len(targets)} Bilder würden für alle Verkleinerungs-Profile gecacht. "
        "Hinweis: das ist nicht mehr nötig für schnelles Browsen (getattr generiert "
        "nichts mehr) -- nur sinnvoll, wenn du gezielt VOR einer erwarteten Nutzung "
        "(z.B. Familienfeier am Fernseher per DLNA) vorwärmen willst."
    )
    errors = 0
    for i, node in enumerate(targets, start=1):
        cache_file = cache_path_for(cfg.cache_dir, node.profile_name, node.source_path)
        try:
            ensure_cached(node.source_path, cache_file, node.profile_max_dim, cfg.jpeg_quality)
        except Exception as exc:  # weiter machen, einzelne kaputte Dateien sollen nicht alles stoppen
            errors += 1
            log.error("Fehler bei %s: %s", node.source_path, exc)
        if i % 50 == 0 or i == len(targets):
            print(f"  {i}/{len(targets)}")
    print(f"Fertig in {time.time() - t0:.1f}s, {errors} Fehler")
    return 1 if errors else 0


def _cmd_mount(args: argparse.Namespace) -> int:
    # Lokal importiert: pyfuse3/trio werden nur für 'mount' benötigt, nicht
    # für 'prewarm' oder 'debug-db' auf Systemen ohne FUSE-Entwicklungspakete.
    import pyfuse3
    import trio

    from .fs import DigikamOperations, TreeHolder

    cfg = load_config(Path(args.config))
    _check_prereqs(cfg)
    mountpoint = Path(args.mountpoint)
    if not mountpoint.is_dir():
        print(f"Mountpoint existiert nicht oder ist kein Verzeichnis: {mountpoint}", file=sys.stderr)
        return 2

    tree = _build_tree(cfg)
    holder = TreeHolder(tree)
    ops = DigikamOperations(cfg, holder)

    fuse_options = set(pyfuse3.default_options)
    fuse_options.add("fsname=digikamfs")
    fuse_options.add("ro")
    if cfg.allow_other:
        fuse_options.add("allow_other")
    if args.debug:
        fuse_options.add("debug")

    pyfuse3.init(ops, str(mountpoint), fuse_options)

    async def refresh_loop() -> None:
        while True:
            await trio.sleep(cfg.refresh_interval_seconds)
            try:
                new_tree = await trio.to_thread.run_sync(_build_tree, cfg)
                holder.tree = new_tree
                log.info("Index neu aufgebaut (%d Inodes)", len(new_tree.inode_to_node))
            except Exception:
                log.exception("Fehler beim periodischen Neuaufbau des Index")

    async def run() -> None:
        async with trio.open_nursery() as nursery:
            ops.attach_nursery(nursery)
            nursery.start_soon(refresh_loop)
            await pyfuse3.main()
            # Sauberes Beenden: refresh_loop und evtl. noch laufende
            # Prefetch-Tasks abbrechen, statt auf sie zu warten.
            nursery.cancel_scope.cancel()

    try:
        trio.run(run)
    finally:
        pyfuse3.close(unmount=True)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="digikamfs")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-Logging aktivieren")
    sub = parser.add_subparsers(dest="command", required=True)

    mount_p = sub.add_parser("mount", help="Dateisystem mounten (blockiert im Vordergrund)")
    mount_p.add_argument("mountpoint")
    mount_p.add_argument("-c", "--config", required=True)
    mount_p.add_argument("-d", "--debug", action="store_true", help="FUSE-Debug-Ausgaben")
    mount_p.set_defaults(func=_cmd_mount)

    prewarm_p = sub.add_parser(
        "prewarm",
        help="Optional: Cache für alle Fotos vorab aufbauen (z.B. vor einer erwarteten Nutzungsspitze)",
    )
    prewarm_p.add_argument("-c", "--config", required=True)
    prewarm_p.set_defaults(func=_cmd_prewarm)

    debugdb_p = sub.add_parser("debug-db", help="status/category/rating-Werte aus der DB anzeigen")
    debugdb_p.add_argument("-c", "--config", required=True)
    debugdb_p.set_defaults(func=_cmd_debug_db)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        return args.func(args)
    except (ConfigError, MetadataCopyError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
