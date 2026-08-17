#!/usr/bin/env python3
"""Checks on the add-on's own files, rather than on the bridge.

Lives under tests/bridge so it rides the fast CI job: standard library only, no
Docker and no DDEV, a few milliseconds. tests/test.bats would build a whole DDEV
project first, which is a lot of machinery for reading three files.
"""
import os
import re
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", ".."))

# Each entry: the file, and a pattern whose first group is the pinned version. The
# patterns are deliberately narrow — the Dockerfile also *mentions* v0.10.0 and
# v0.10.5 in comments explaining why the archive name and checksum handling changed,
# so anything looser matches prose and fails for no reason.
PINS = {
    "codebase-memory-build/Dockerfile.codebase-memory":
        r"^ARG CBM_VERSION=(v[0-9]+\.[0-9]+\.[0-9]+)\s*$",
    "docker-compose.codebase-memory.yaml":
        r"^\s*CBM_VERSION:\s*\$\{CBM_VERSION:-(v[0-9]+\.[0-9]+\.[0-9]+)\}\s*$",
    "README.md":
        r"^\|\s*`CBM_VERSION`\s*\|\s*`(v[0-9]+\.[0-9]+\.[0-9]+)`",
}


class TestPinnedVersionIsConsistent(unittest.TestCase):
    """The pinned codebase-memory-mcp release is written in three places.

    The compose value is what actually takes effect; the Dockerfile default covers a
    direct `docker build`; the README tells the reader what they will get. Nothing
    ties them together, and the README drifted on the first bump — silently, because
    a stale docs table breaks no test and no build.
    """

    def _extract(self, relative_path, pattern):
        path = os.path.join(REPO_ROOT, relative_path)
        self.assertTrue(os.path.isfile(path), "missing file: %s" % relative_path)
        with open(path, encoding="utf-8") as handle:
            found = re.findall(pattern, handle.read(), re.M)
        self.assertEqual(
            len(found), 1,
            "expected exactly one CBM_VERSION declaration in %s, found %d — has the "
            "line been reworded? update the pattern in this test alongside it"
            % (relative_path, len(found)))
        return found[0]

    def test_all_three_declarations_agree(self):
        versions = {path: self._extract(path, pattern)
                    for path, pattern in PINS.items()}
        distinct = set(versions.values())
        self.assertEqual(
            len(distinct), 1,
            "the pinned version disagrees across files: %s" % (
                ", ".join("%s=%s" % (os.path.basename(p), v)
                          for p, v in sorted(versions.items()))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
