# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0]

First public release.

### Added

- Read-only FUSE filesystem exposing digiKam albums as
  `<profile>/<year>/<album>/<N>Sterne/<file>`, filtered by star rating.
- Resolution profiles with lazy downscaling on first open, file cache with
  mtime-based invalidation and atomic writes.
- Metadata preservation (EXIF, GPS, IPTC, XMP) via `exiftool`.
- Capture date from the digiKam database as file timestamp.
- Background index refresh, stable inode numbers, prefetch of the next file.
- `star_dir_format` option to rename the rating directories, e.g. `{n}stars`.
- Commands `digikamfs mount`, `prewarm` and `debug-db`.
- `digikamfs-smb-shares` to generate one Samba share per resolution profile.
- systemd unit template in `contrib/`.
- CI (lint, pipeline test on Python 3.10, 3.12 and 3.13, packaging check) and a
  PyPI release workflow.

[Unreleased]: https://github.com/euphi/DigikamFS/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/euphi/DigikamFS/releases/tag/v0.1.0
