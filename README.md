# DigikamFS

A virtual read-only filesystem that reads albums and star ratings directly
from the digiKam SQLite database and exposes them via FUSE as a tree:

```
<ExportRoot>/<ResolutionProfile>/<Year>/<Album>/<N>Sterne/<Filename>
```

(`Sterne` is German for "stars"; the rating directories are named literally,
e.g. `3Sterne`.)

Photos are downscaled to the configured resolution on first access and cached
as files afterwards. The mount can then be shared via SMB/DLNA or used as a
copy source (rsync, Nextcloud client, ...) like any ordinary directory.

Videos and everything other than standard JPEGs (RAW, for example) are
currently ignored.

*Eine deutsche Fassung dieser Dokumentation liegt unter
[README.de.md](README.de.md).*

## Installation

```bash
sudo apt install libfuse3-dev fuse3 pkg-config python3-venv libimage-exiftool-perl
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`exiftool` is required for every profile that performs downscaling; it copies
EXIF/IPTC/XMP/GPS data from the original file to the resized version. It is
not needed when only the `Original` passthrough profile is used -- this is
checked at startup, and a clear error is reported if `exiftool` is missing
while being required.

## Before first use: verifying the database assumptions

Photos are filtered by `Images.status = 1` (visible, neither trashed nor
hidden) and `Images.category = 1` (image, not video or audio). These values
match current digiKam versions, but they should be verified against the local
database before being relied upon:

```bash
python3 -m digikamfs debug-db -c config.yaml
```

The command reports the `status`, `category` and `rating` values that actually
occur, together with their counts. If the local values differ, set
`status_visible_value` / `category_image_value` accordingly in `config.yaml`.

## Configuration

See `config.example.yaml`; copy it to `config.yaml` and adjust:

- `digikam_db`: path to `digikam4.db`
- `cache_dir`: location of the downscaled JPEGs
- `profiles`: one top-level directory per resolution; `null` means the
  original without downscaling (passthrough, no additional storage required)
- `star_levels`: which `NSterne` directories are created (default `[3, 4]`)

## Usage

**Mounting** (blocks in the foreground; see the systemd unit below for
permanent operation):

```bash
mkdir -p /srv/DigikamFS
python3 -m digikamfs mount /srv/DigikamFS -c config.yaml
```

Terminate with Ctrl+C, or from another terminal:
`fusermount3 -u /srv/DigikamFS`

**Warming the cache** (optional -- not required for normal browsing, since
`getattr`/`ls -l` never generate anything, see below; useful only when
everything should be cached ahead of a known usage peak, such as a family
gathering in front of a DLNA television):

```bash
python3 -m digikamfs prewarm -c config.yaml
```

## How it works / design decisions

- **In-memory index**: the database is not queried on every `ls`. It is read
  once at startup and reloaded in the background every
  `refresh_interval_seconds` (reference swap; accesses in flight are not
  interrupted).
- **Stable inode numbers**: each inode is derived deterministically from the
  hash of the virtual path rather than being counted upwards. A file therefore
  keeps the same inode number across an index refresh -- important for kernel
  caching and SMB clients. With very large collections a hash collision is
  theoretically possible, but negligible in practice for a private photo
  library.
- **A real cache, filled only on real access**: `getattr`/`readdir` -- that
  is, plain browsing and `ls -l` -- *never* generate anything. They only
  perform a cheap check for an existing valid cache file and otherwise report
  the original size as an estimate. Downscaling happens exclusively in
  `open()`, i.e. when a file is actually read, copied or displayed. Browsing
  therefore stays fast, and the cache grows only with what is really used
  rather than with everything that has ever been listed in a directory.
- **Prefetch**: when a file is actually opened, the alphabetically next file
  in the same `NSterne` directory is converted in the background, without
  delaying the current access. When browsing through an album, the next file
  is usually already cached before it is opened. This runs in a dedicated
  background nursery and deduplicates against real accesses, so the same file
  is never resized twice concurrently.
- **Reported date = capture date**: the file timestamps (mtime/atime/ctime)
  reported over FUSE come from `ImageInformation.creationDate` in the digiKam
  database (falling back to `digitizationDate`, and then to the actual file
  mtime if both are missing) rather than from the filesystem mtime of the
  original. Internally, cache invalidation still uses the real file mtime,
  independently of this, so modifications to the original are detected
  reliably.
- **Metadata is preserved**: EXIF (including orientation), GPS, IPTC and XMP
  are transferred unchanged from the original to the resized file by
  `exiftool` after the resize. Only the now-incorrect EXIF dimension tags
  (`ExifImageWidth`/`Height`) are excluded; the actual pixel size is recorded
  correctly in the JPEG itself anyway. The pixels are deliberately *not*
  rotated beforehand -- the orientation tag remains unchanged, so a viewer
  that honours it displays the resized image exactly like the original.
- **Size estimate before first open**: until a file has been opened for the
  first time, `ls -l` reports the original file size instead of the smaller
  target size. Copy tools read until EOF and do not rely blindly on the
  reported size, so this is uncritical -- only the display before first access
  is an upper-bound estimate.
- **Cache invalidation**: a cache file is considered valid while its mtime is
  greater than or equal to the mtime of the original. If the original changes,
  the file is regenerated on next access.
- **Atomic creation**: the resize writes to a temporary file and renames it
  only afterwards (`os.replace`), so concurrent readers (multiple SMB clients)
  never see a truncated file.

## Known limitations

- Only standard JPEGs are processed; RAW files and videos are hidden entirely
  (intentional, as per the requirements).
- digiKam ratings are assumed to be stored reliably in the database. The
  database is used rather than per-file EXIF/XMP because it is considerably
  faster.
- No write access: the filesystem is deliberately mounted read-only.

## Troubleshooting: minidlna/Samba "Permission denied"

By kernel default, FUSE mounts are accessible **only to the user who mounted
them**, regardless of the file permissions being reported. If minidlna, smbd
or a similar service runs under its own user (`minidlna`, for example, rather
than the user who ran `digikamfs mount`), it receives a "Permission denied"
without further configuration, even though `ls -la` shows
`dr-xr-xr-x`/`-r--r--r--` for the mounting user. This is standard FUSE
behaviour, not a digikamfs bug.

Fix (enabled by default, see `allow_other: true` in `config.example.yaml`):

1. Make sure the configuration contains `allow_other: true`.
2. If `digikamfs mount` does *not* run as root (for example via systemd with
   `User=someuser`), uncomment the line
   ```
   user_allow_other
   ```
   in `/etc/fuse.conf` once (removing the leading `#`). This step is
   unnecessary when the mount runs as root.
3. Restart the mount (`systemctl restart digikamfs`, or restart the process).

Afterwards, verify as the other user (for example
`sudo -u minidlna ls /media/DigikamFS/QHD`) before restarting minidlna or
Samba themselves.

If access still fails, check whether minidlna/smbd runs in its own mount
namespace due to systemd hardening (`ProtectHome=`, `PrivateMounts=`,
`ProtectSystem=strict` and similar), which may hide the FUSE mount entirely.
That case usually presents as "no such directory" rather than "Permission
denied".

## Example: Samba share

```ini
[Fotos]
   path = /srv/DigikamFS
   read only = yes
   browseable = yes
   guest ok = no
```

One share per resolution profile can also be generated automatically from the
top-level directories of the mountpoint:

```bash
sudo ./digikamfs/generate_smb_shares.py \
    --mount-point /srv/DigikamFS \
    --smb-user digikamfs \
    --smb-group digikamfs
```

The script writes `/etc/samba/digikamfs-shares.conf` atomically; that file is
meant to be pulled into `smb.conf` via `include`. It has to be re-run whenever
resolution profiles are added to or removed from the configuration.

## Example: systemd service for permanent operation

`/etc/systemd/system/digikamfs.service`:

```ini
[Unit]
Description=DigikamFS
After=network.target

[Service]
Type=simple
ExecStart=/path/to/venv/bin/python3 -m digikamfs mount /srv/DigikamFS -c /path/to/config.yaml
ExecStop=/bin/fusermount3 -u /srv/DigikamFS
Restart=on-failure
User=someuser

[Install]
WantedBy=multi-user.target
```

For a nightly cache warm-up, add a cron job or systemd timer running
`python3 -m digikamfs prewarm -c config.yaml`.

## Tests

`test_pipeline.py` builds a synthetic digiKam database together with real test
JPEGs and exercises the complete pipeline (database query, tree construction,
star filtering, video exclusion, resize, cache hit, inode stability) without
an actual mount:

```bash
python3 test_pipeline.py
```

The project has also been verified against a real FUSE mount (readdir,
getattr, open/read, `cp`), so it is known to work in practice rather than
being only theoretically correct.

## License

MIT -- see [LICENSE](LICENSE).
