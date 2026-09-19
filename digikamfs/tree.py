"""Virtueller Verzeichnisbaum: Profil -> Album-Pfad (mit Jahr) -> NSterne -> Datei."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

from .digikam_index import Photo


@dataclass
class DirNode:
    children: Dict[str, "Node"] = field(default_factory=dict)


@dataclass
class FileNode:
    source_path: Path
    profile_name: str
    profile_max_dim: Optional[int]   # None = Original, keine Verkleinerung
    orig_size: int
    fs_mtime: float        # tatsächliches Dateisystem-mtime -- NUR für Cache-Invalidierung
    capture_time: float    # Aufnahmedatum -- wird als Datei-mtime angezeigt


Node = Union[DirNode, FileNode]


def build_tree(photos: List[Photo], profiles: Dict[str, Optional[int]], star_levels: List[int]) -> DirNode:
    root = DirNode()

    by_album: Dict[str, List[Photo]] = {}
    for p in photos:
        by_album.setdefault(p.rel_dir, []).append(p)

    for profile_name, max_dim in profiles.items():
        profile_dir = DirNode()
        root.children[profile_name] = profile_dir

        for rel_dir, album_photos in by_album.items():
            segments = [s for s in rel_dir.split("/") if s]
            cur = profile_dir
            for seg in segments:
                nxt = cur.children.get(seg)
                if nxt is None:
                    nxt = DirNode()
                    cur.children[seg] = nxt
                cur = nxt

            for level in star_levels:
                matching = [p for p in album_photos if p.rating >= level]
                if not matching:
                    continue
                star_dir = DirNode()
                cur.children[f"{level}Sterne"] = star_dir
                for p in matching:
                    star_dir.children[p.filename] = FileNode(
                        source_path=p.abs_path,
                        profile_name=profile_name,
                        profile_max_dim=max_dim,
                        orig_size=p.size,
                        fs_mtime=p.fs_mtime,
                        capture_time=p.capture_time,
                    )
    return root


class Tree:
    """Weist jedem Knoten eine stabile Inode-Nummer zu (Hash des virtuellen
    Pfads). Stabil heißt: baut man den Baum neu auf (Refresh), bekommt
    derselbe Pfad wieder dieselbe Inode-Nummer -- wichtig, damit der
    Kernel-Dentry-Cache und offene Filehandles nicht durcheinanderkommen."""

    def __init__(self, root: DirNode):
        import pyfuse3  # lokal importiert, damit tree.py auch ohne FUSE-Paket testbar bleibt

        self._root_inode = pyfuse3.ROOT_INODE
        self.inode_to_node: Dict[int, Node] = {}
        self.inode_to_path: Dict[int, str] = {}
        self.path_to_inode: Dict[str, int] = {}
        self._assign(root, "", self._root_inode)

    def _inode_for_path(self, path: str) -> int:
        if path == "":
            return self._root_inode
        digest = hashlib.blake2b(path.encode("utf-8"), digest_size=8).digest()
        val = int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF
        if val in (0, self._root_inode):
            val += 2
        return val

    def _assign(self, node: Node, path: str, inode: int) -> None:
        self.inode_to_node[inode] = node
        self.inode_to_path[inode] = path
        self.path_to_inode[path] = inode
        if isinstance(node, DirNode):
            for name, child in node.children.items():
                child_path = f"{path}/{name}" if path else name
                child_inode = self._inode_for_path(child_path)
                self._assign(child, child_path, child_inode)
