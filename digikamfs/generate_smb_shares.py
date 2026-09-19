#!/usr/bin/env python3
"""
generate_smb_shares.py

Erzeugt aus den Top-Level-Verzeichnissen des DigikamFS-Mountpoints
(je eines pro ResolutionProfile) Samba-Share-Definitionen und schreibt
sie atomar nach /etc/samba/digikamfs-shares.conf. Diese Datei wird über
"include" in smb.conf eingebunden.

Muss erneut ausgeführt werden, wenn Auflösungsprofile in der DigikamFS-
Config hinzugefügt oder entfernt werden.

Aufruf (als root, wegen Schreibzugriff auf /etc/samba/):
    sudo ./generate_smb_shares.py \\
        --mount-point /mnt/digikamfs \\
        --smb-user digikamfs \\
        --smb-group digikamfs
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys

SHARE_TEMPLATE = """
[{share_name}]
   comment      = Digikam Fotos - Profil "{profile}"
   path         = {path}
   browseable   = yes
   read only    = yes
   guest ok     = no
   valid users  = @{smb_group}
   force user   = {smb_user}
   force group  = {smb_group}
"""


def discover_profiles(mount_point: str) -> list[str]:
    if not os.path.ismount(mount_point):
        print(f"Warnung: {mount_point} ist aktuell nicht gemountet.", file=sys.stderr)
    try:
        entries = sorted(
            e for e in os.listdir(mount_point)
            if os.path.isdir(os.path.join(mount_point, e)) and not e.startswith(".")
        )
    except FileNotFoundError:
        sys.exit(f"Mountpoint {mount_point} existiert nicht.")
    return entries


def sanitize_share_name(profile: str) -> str:
    # Samba-Sharenamen: keine Leerzeichen/Sonderzeichen
    return "DigikamFS-" + "".join(c if c.isalnum() else "_" for c in profile)


def build_config(mount_point: str, profiles: list[str], smb_user: str, smb_group: str) -> str:
    header = (
        "# Automatisch generiert von generate_smb_shares.py -- NICHT manuell editieren\n"
        f"# Stand: {datetime.datetime.now().isoformat(timespec='seconds')}\n"
    )
    blocks = [
        SHARE_TEMPLATE.format(
            share_name=sanitize_share_name(p),
            profile=p,
            path=os.path.join(mount_point, p),
            smb_user=smb_user,
            smb_group=smb_group,
        )
        for p in profiles
    ]
    return header + "\n".join(blocks) + "\n"


def reload_samba() -> None:
    try:
        subprocess.run(["smbcontrol", "all", "reload-config"], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(
            f"Hinweis: smbd konnte nicht automatisch neu geladen werden ({exc}). "
            f"Bitte manuell: systemctl reload smbd",
            file=sys.stderr,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mount-point", default="/media/DigikamFS")
    parser.add_argument("--smb-user", default="digikamfs")
    parser.add_argument("--smb-group", default="digikamfs")
    parser.add_argument("--output", default="/etc/samba/digikamfs-shares.conf")
    parser.add_argument("--no-reload", action="store_true")
    args = parser.parse_args()

    profiles = discover_profiles(args.mount_point)
    if not profiles:
        sys.exit(f"Keine Auflösungsprofile unter {args.mount_point} gefunden.")

    config = build_config(args.mount_point, profiles, args.smb_user, args.smb_group)

    # Atomarer Schreibvorgang, analog zu cache.py
    tmp_path = args.output + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(config)
    os.replace(tmp_path, args.output)

    print(f"{len(profiles)} Freigabe(n) nach {args.output} geschrieben: {', '.join(profiles)}")

    if not args.no_reload:
        reload_samba()


if __name__ == "__main__":
    main()
