# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import release_tools as rt


def project(version="1.2.3", changelog="## [Unreleased]\n\n## [1.2.3] - 2027-01-01\n\n### Added\n- thing\n\n## [1.2.2] - 2026-12-01\n- old\n"):
    d = Path(tempfile.mkdtemp())
    (d / "VERSION").write_text(version + "\n")
    (d / "CHANGELOG.md").write_text(changelog)
    return d


class ReleaseTools(unittest.TestCase):
    def test_matching_tag_passes(self):
        self.assertEqual(rt.check("v1.2.3", project()), "1.2.3")

    def test_tag_must_match_version(self):
        with self.assertRaises(SystemExit):
            rt.check("v1.2.4", project())

    def test_changelog_section_is_required(self):
        with self.assertRaises(SystemExit):
            rt.check("v2.0.0", project("2.0.0"))

    def test_date_placeholder_blocks_a_tagged_release(self):
        d = project("0.1.0", "## [0.1.0] - YYYY-MM-DD\n\n- first\n")
        self.assertEqual(rt.check(None, d), "0.1.0")        # fine while developing
        with self.assertRaises(SystemExit):
            rt.check("v0.1.0", d)                           # not fine when tagging

    def test_bad_version_is_rejected(self):
        with self.assertRaises(SystemExit):
            rt.read_version(project("v1.2"))

    def test_notes_stop_at_the_next_heading(self):
        body = rt.changelog_section("1.2.3", project())
        self.assertIn("- thing", body)
        self.assertNotIn("old", body)

    def test_repository_is_consistent_now(self):
        rt.check()          # the real VERSION and CHANGELOG.md must agree


if __name__ == "__main__":
    unittest.main()
