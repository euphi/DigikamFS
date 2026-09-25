# Contributing to DigikamFS

Thanks for your interest! Bug reports, ideas and pull requests are all
welcome &ndash; in English or German.

## Reporting bugs

Please use the [bug report template](https://github.com/euphi/DigikamFS/issues/new/choose)
and include:

- the output of `digikamfs debug-db -c config.yaml` (it shows which
  status/category/rating values your digiKam database uses),
- the relevant log lines (`digikamfs -v mount ...` or
  `journalctl -u digikamfs`),
- your versions of DigikamFS, digiKam, the distribution and Python.

## Development setup

```bash
sudo apt install libfuse3-dev fuse3 pkg-config python3-venv libimage-exiftool-perl
python3 -m venv venv && . venv/bin/activate
pip install -e ".[dev]"
```

Before opening a pull request, run the same checks as CI:

```bash
ruff check .
python test_pipeline.py
```

`test_pipeline.py` builds a synthetic digiKam database with real JPEGs and
needs neither a real digiKam installation nor a FUSE mount. If you change
behaviour, please extend it.

## Guidelines

- Keep the filesystem read-only: DigikamFS must never write to the digiKam
  database or to the original photos.
- Browsing (`getattr`, `readdir`) must stay cheap &ndash; expensive work
  belongs in `open()`.
- Update both `README.md` and `README.de.md` when behaviour or options
  change, and add an entry to `CHANGELOG.md` under "Unreleased".
