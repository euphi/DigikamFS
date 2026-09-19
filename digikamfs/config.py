"""Konfiguration für DigikamFS: Laden und Validieren der YAML-Konfigdatei."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    digikam_db: Path
    cache_dir: Path
    profiles: Dict[str, Optional[int]]   # Profilname -> maximale Kantenlänge in px (None = Original/passthrough)
    star_levels: List[int]               # z.B. [3, 4]
    jpeg_quality: int
    refresh_interval_seconds: int
    min_status: int
    image_category: int
    allow_other: bool


def load_config(path: Path) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    def require(key: str):
        if key not in raw:
            raise ConfigError(f"Pflichtfeld '{key}' fehlt in der Konfiguration ({path})")
        return raw[key]

    digikam_db = Path(require("digikam_db")).expanduser()
    if not digikam_db.exists():
        raise ConfigError(f"digikam_db nicht gefunden: {digikam_db}")

    cache_dir = Path(require("cache_dir")).expanduser()

    profiles_raw = require("profiles")
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise ConfigError("'profiles' muss ein nicht-leeres Mapping sein, z.B. {QHD: 2560}")
    profiles: Dict[str, Optional[int]] = {}
    for name, value in profiles_raw.items():
        if value is not None and (not isinstance(value, int) or value <= 0):
            raise ConfigError(f"Profil '{name}': Wert muss null (Original) oder eine positive Zahl sein")
        profiles[str(name)] = value

    star_levels = raw.get("star_levels", [3, 4])
    if not isinstance(star_levels, list) or not all(isinstance(x, int) for x in star_levels):
        raise ConfigError("'star_levels' muss eine Liste von Ganzzahlen sein, z.B. [3, 4]")
    star_levels = sorted(set(star_levels))

    jpeg_quality = int(raw.get("jpeg_quality", 85))
    refresh_interval_seconds = int(raw.get("refresh_interval_seconds", 300))

    # Digikam-Statuscode für "sichtbar" (nicht papierkorb/versteckt) und
    # Kategorie-Code für "Bild" (nicht Video/Audio). Diese Werte stimmen mit
    # aktuellen Digikam-Versionen überein, wurden aber nicht aus offizieller
    # Doku übernommen -- vor dem produktiven Einsatz bitte einmal mit
    # `digikamfs debug-db -c config.yaml` gegen die eigene DB verifizieren.
    min_status = int(raw.get("status_visible_value", 1))
    image_category = int(raw.get("category_image_value", 1))

    # Standardmäßig an, weil der Sinn dieses Projekts (Freigabe per SMB/DLNA)
    # praktisch immer bedeutet, dass ein ANDERER Prozess/User (Samba, minidlna,
    # ...) auf den Mount zugreifen muss -- ein FUSE-Mount ist ohne diese
    # Option per Kernel-Default NUR für den mountenden User sichtbar, egal
    # welche Dateirechte gemeldet werden. Siehe README, Abschnitt
    # "minidlna/Samba: Permission denied".
    allow_other = bool(raw.get("allow_other", True))

    return Config(
        digikam_db=digikam_db,
        cache_dir=cache_dir,
        profiles=profiles,
        star_levels=star_levels,
        jpeg_quality=jpeg_quality,
        refresh_interval_seconds=refresh_interval_seconds,
        min_status=min_status,
        image_category=image_category,
        allow_other=allow_other,
    )
