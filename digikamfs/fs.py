"""FUSE-Operationen für DigikamFS (read-only)."""

from __future__ import annotations

import errno
import logging
import os
import stat as stat_module
import time
from pathlib import Path
from typing import Dict, Optional, Set

import pyfuse3
import trio

from .cache import cache_path_for, ensure_cached, is_cache_valid
from .config import Config
from .tree import DirNode, FileNode, Tree

log = logging.getLogger("digikamfs.fs")


class TreeHolder:
    """Hält die aktuell gültige Tree-Instanz; wird beim periodischen Refresh
    einfach ausgetauscht (Referenz-Swap), ohne dass laufende FUSE-Requests
    unterbrochen werden."""

    def __init__(self, tree: Tree):
        self.tree = tree


def _resolve_cache_path(cfg: Config, node: FileNode) -> Path:
    return cache_path_for(cfg.cache_dir, node.profile_name, node.source_path)


class DigikamOperations(pyfuse3.Operations):
    supports_dot_lookup = False

    def __init__(self, cfg: Config, holder: TreeHolder):
        super().__init__()
        self.cfg = cfg
        self.holder = holder
        self._open_fds: Dict[int, int] = {}
        self._next_fh = 0
        self._mount_time_ns = time.time_ns()
        self._uid = os.getuid()
        self._gid = os.getgid()
        self._background_nursery: Optional[trio.Nursery] = None
        # Verhindert, dass zwei gleichzeitige Opens (oder ein Prefetch und ein
        # echter Zugriff) dieselbe Datei parallel zweimal umwandeln.
        self._inflight: Set[str] = set()

    def attach_nursery(self, nursery: trio.Nursery) -> None:
        """Wird einmal beim Start aus cli.py aufgerufen, damit open()
        Prefetch-Vorgänge im Hintergrund starten kann, ohne den eigentlichen
        Lesezugriff zu blockieren."""
        self._background_nursery = nursery

    @property
    def tree(self) -> Tree:
        return self.holder.tree

    # -- Hilfsfunktionen -------------------------------------------------

    def _dir_attrs(self, inode: int) -> pyfuse3.EntryAttributes:
        entry = pyfuse3.EntryAttributes()
        entry.st_mode = stat_module.S_IFDIR | 0o555
        entry.st_size = 0
        entry.st_nlink = 2
        entry.st_ino = inode
        entry.st_uid = self._uid
        entry.st_gid = self._gid
        entry.st_atime_ns = self._mount_time_ns
        entry.st_mtime_ns = self._mount_time_ns
        entry.st_ctime_ns = self._mount_time_ns
        entry.entry_timeout = 60
        entry.attr_timeout = 60
        return entry

    async def _file_attrs(self, inode: int, node: FileNode) -> pyfuse3.EntryAttributes:
        entry = pyfuse3.EntryAttributes()
        entry.st_mode = stat_module.S_IFREG | 0o444
        entry.st_nlink = 1
        entry.st_ino = inode
        entry.st_uid = self._uid
        entry.st_gid = self._gid

        # WICHTIG: getattr/readdir erzeugen NIEMALS den Cache. Das hier ist
        # ein reiner Lesecheck (billig), damit reines Durchsuchen/`ls -l`
        # nicht bei jeder Datei ein Resize auslöst und den Cache mit Dingen
        # füllt, die nie wirklich geöffnet wurden. Erzeugt wird ausschließlich
        # in open(), also bei echtem Lesezugriff.
        cached_now = False
        if node.profile_max_dim is None:
            size = node.orig_size
        else:
            cache_file = _resolve_cache_path(self.cfg, node)
            if is_cache_valid(cache_file, node.fs_mtime):
                size = cache_file.stat().st_size
                cached_now = True
            else:
                # Noch nicht gecacht: Originalgröße als Schätzung melden.
                # Downscaling+Rekompression ist bei Fotos praktisch immer
                # kleiner als das Original, d.h. die Schätzung ist eine
                # sichere Obergrenze; Kopiertools lesen ohnehin bis EOF und
                # verlassen sich nicht blind auf die gemeldete Größe.
                size = node.orig_size

        entry.st_size = size
        # Angezeigtes Datum = Aufnahmedatum (aus digikam-DB), NICHT das
        # tatsächliche Dateisystem-mtime der Quelldatei (das dient nur intern
        # der Cache-Invalidierung, siehe node.fs_mtime / is_cache_valid).
        mtime_ns = int(node.capture_time * 1e9)
        entry.st_atime_ns = mtime_ns
        entry.st_mtime_ns = mtime_ns
        entry.st_ctime_ns = mtime_ns
        entry.entry_timeout = 60
        # Solange nur eine Schätzung vorliegt, Kernel-Attribut-Cache kurz
        # halten, damit die echte Größe nach dem ersten Öffnen zeitnah in
        # anderen Listings (z.B. einem zweiten SMB-Client) ankommt.
        entry.attr_timeout = 60 if cached_now else 5
        return entry

    async def _ensure_cached_tracked(self, node: FileNode) -> Path:
        """ensure_cached, aber dedupliziert: falls ein anderer Task (typischerweise
        ein Prefetch) bereits an genau dieser Datei arbeitet, wird gewartet statt
        doppelt zu resizen."""
        cache_file = _resolve_cache_path(self.cfg, node)
        if is_cache_valid(cache_file, node.fs_mtime):
            return cache_file
        key = str(cache_file)
        while key in self._inflight:
            await trio.sleep(0.05)
            if is_cache_valid(cache_file, node.fs_mtime):
                return cache_file
        self._inflight.add(key)
        try:
            return await trio.to_thread.run_sync(
                ensure_cached, node.source_path, cache_file,
                node.profile_max_dim, self.cfg.jpeg_quality,
            )
        finally:
            self._inflight.discard(key)

    async def _prefetch_next(self, inode: int) -> None:
        """Wird nach einem echten open() im Hintergrund gestartet: wandelt die
        alphabetisch nächste Datei im selben (NSterne-)Ordner schon mal um,
        damit sie beim Weiterklicken/-kopieren bereits fertig im Cache liegt."""
        try:
            path = self.tree.inode_to_path.get(inode)
            if not path or "/" not in path:
                return
            parent_path, _, name = path.rpartition("/")
            parent_inode = self.tree.path_to_inode.get(parent_path)
            parent_node = self.tree.inode_to_node.get(parent_inode) if parent_inode is not None else None
            if not isinstance(parent_node, DirNode):
                return
            siblings = sorted(parent_node.children.keys())
            try:
                idx = siblings.index(name)
            except ValueError:
                return
            if idx + 1 >= len(siblings):
                return  # letzte Datei im Ordner -- nichts mehr zum Vorausladen
            next_node = parent_node.children[siblings[idx + 1]]
            if not isinstance(next_node, FileNode) or next_node.profile_max_dim is None:
                return
            cache_file = _resolve_cache_path(self.cfg, next_node)
            if is_cache_valid(cache_file, next_node.fs_mtime):
                return
            log.debug("Prefetch: %s", next_node.source_path)
            await self._ensure_cached_tracked(next_node)
        except Exception:
            log.exception("Prefetch fehlgeschlagen")

    # -- FUSE-Operationen --------------------------------------------------

    async def lookup(self, parent_inode: int, name: bytes, ctx=None) -> pyfuse3.EntryAttributes:
        name_str = name.decode("utf-8")
        parent_path = self.tree.inode_to_path.get(parent_inode)
        if parent_path is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        child_path = f"{parent_path}/{name_str}" if parent_path else name_str
        inode = self.tree.path_to_inode.get(child_path)
        if inode is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return await self.getattr(inode, ctx)

    async def getattr(self, inode: int, ctx=None) -> pyfuse3.EntryAttributes:
        node = self.tree.inode_to_node.get(inode)
        if node is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if isinstance(node, DirNode):
            return self._dir_attrs(inode)
        return await self._file_attrs(inode, node)

    async def opendir(self, inode: int, ctx=None) -> int:
        node = self.tree.inode_to_node.get(inode)
        if not isinstance(node, DirNode):
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    async def readdir(self, fh: int, start_id: int, token) -> None:
        node = self.tree.inode_to_node.get(fh)
        if not isinstance(node, DirNode):
            return
        parent_path = self.tree.inode_to_path[fh]
        names = sorted(node.children.keys())
        for i, name in enumerate(names):
            if i < start_id:
                continue
            child_path = f"{parent_path}/{name}" if parent_path else name
            child_inode = self.tree.path_to_inode[child_path]
            attr = await self.getattr(child_inode)
            if not pyfuse3.readdir_reply(token, name.encode("utf-8"), attr, i + 1):
                break

    async def releasedir(self, fh: int) -> None:
        return None

    async def open(self, inode: int, flags: int, ctx=None) -> pyfuse3.FileInfo:
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise pyfuse3.FUSEError(errno.EROFS)
        node = self.tree.inode_to_node.get(inode)
        if node is None or isinstance(node, DirNode):
            raise pyfuse3.FUSEError(errno.ENOENT)

        if node.profile_max_dim is None:
            real_path = node.source_path
        else:
            real_path = await self._ensure_cached_tracked(node)

        fd = os.open(real_path, os.O_RDONLY)
        self._next_fh += 1
        fh = self._next_fh
        self._open_fds[fh] = fd

        # Prefetch der alphabetisch nächsten Datei im Hintergrund anstoßen --
        # ohne auf sie zu warten, damit der aktuelle open() sofort zurückkehrt.
        if node.profile_max_dim is not None and self._background_nursery is not None:
            self._background_nursery.start_soon(self._prefetch_next, inode)

        return pyfuse3.FileInfo(fh=fh, keep_cache=True)

    async def read(self, fh: int, off: int, size: int) -> bytes:
        fd = self._open_fds.get(fh)
        if fd is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        return os.pread(fd, size, off)

    async def release(self, fh: int) -> None:
        fd = self._open_fds.pop(fh, None)
        if fd is not None:
            os.close(fd)

    async def statfs(self, ctx=None) -> pyfuse3.StatvfsData:
        s = pyfuse3.StatvfsData()
        s.f_bsize = 4096
        s.f_frsize = 4096
        s.f_blocks = 0
        s.f_bfree = 0
        s.f_bavail = 0
        s.f_files = len(self.tree.inode_to_node)
        s.f_ffree = 0
        s.f_favail = 0
        s.f_namemax = 255
        return s
