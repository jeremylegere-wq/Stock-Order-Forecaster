#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Small helpers for releases (standard library only).

  python scripts/release_tools.py check [TAG]   verify VERSION, CHANGELOG.md and (optionally) a tag agree
  python scripts/release_tools.py notes VERSION print the CHANGELOG section for VERSION (used as release notes)

The authoritative version is the single line in the VERSION file. Tags are "v" + that version, e.g. v0.2.0.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def read_version(root: Path = ROOT) -> str:
    v = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not SEMVER.match(v):
        raise SystemExit(f"VERSION must look like 1.2.3 (or 1.2.3-rc.1), found {v!r}")
    return v


def changelog_section(version: str, root: Path = ROOT) -> str:
    """The text under '## [version]' up to the next '## ' heading. Empty string if missing."""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def check(tag: str | None = None, root: Path = ROOT) -> str:
    version = read_version(root)
    if tag is not None and tag != f"v{version}":
        raise SystemExit(f"Tag {tag!r} does not match VERSION: expected 'v{version}'. "
                         "Change VERSION and CHANGELOG.md in a commit first, then tag that commit.")
    if not changelog_section(version, root):
        raise SystemExit(f"CHANGELOG.md has no '## [{version}]' section with content. Add release notes before tagging.")
    if tag is not None and f"## [{version}] - YYYY-MM-DD" in (root / "CHANGELOG.md").read_text(encoding="utf-8"):
        raise SystemExit(f"CHANGELOG.md still has the date placeholder for {version}. Replace YYYY-MM-DD with the release date.")
    return version


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "check" and len(argv) <= 3:
        v = check(argv[2] if len(argv) == 3 else None)
        print(f"OK: version {v}")
        return 0
    if len(argv) == 3 and argv[1] == "notes":
        body = changelog_section(argv[2])
        if not body:
            raise SystemExit(f"No CHANGELOG.md section for {argv[2]}")
        print(body)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
