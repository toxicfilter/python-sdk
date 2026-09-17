"""What the package promises about itself, rather than what it does.

Every failure here is silent for the person who hits it: a missing ``py.typed`` means a
consumer's type checker ignores every annotation in the client and nobody is told, and a
classifier list that lies about the Python versions is a promise no test would otherwise
check.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PackagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = (ROOT / "pyproject.toml").read_text()

    def test_the_typing_marker_is_shipped(self) -> None:
        # PEP 561: the file has to exist AND be declared as package data, or it is simply
        # left out of the wheel and the annotations are invisible again.
        self.assertTrue((ROOT / "toxicfilter" / "py.typed").exists())
        self.assertIn('toxicfilter = ["py.typed"]', self.manifest)
        self.assertIn("Typing :: Typed", self.manifest)

    def test_every_python_version_claimed_is_one_the_ci_runs(self) -> None:
        claimed = set(re.findall(r'Programming Language :: Python :: (3\.\d+)"', self.manifest))
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
        tested = set(re.findall(r"'(3\.\d+)'", workflow))

        self.assertTrue(claimed)
        self.assertEqual(claimed, tested, "the classifiers and the CI matrix disagree")

    def test_the_floor_is_the_lowest_version_tested(self) -> None:
        floor = re.search(r'requires-python = ">=(3\.\d+)"', self.manifest).group(1)
        tested = sorted(re.findall(r"'(3\.\d+)'", (ROOT / ".github" / "workflows" / "tests.yml").read_text()),
                        key=lambda v: [int(p) for p in v.split(".")])

        self.assertEqual(floor, tested[0], "requires-python claims a version the CI never runs")

    def test_the_readme_and_the_licence_travel_with_it(self) -> None:
        self.assertIn('readme = "README.md"', self.manifest)
        self.assertTrue((ROOT / "LICENSE").exists())
        self.assertIn("MIT", self.manifest)

    def test_the_art_is_there_and_the_readme_shows_it(self) -> None:
        for name in ("banner.svg", "banner.png", "og.svg", "og.png"):
            self.assertTrue((ROOT / "art" / name).exists(), f"art/{name} is missing")

        readme = (ROOT / "README.md").read_text()

        # Absolute, because PyPI does not rewrite a relative image path and the banner is
        # the first thing on the package page.
        self.assertIn("https://raw.githubusercontent.com/toxicfilter/python-sdk/main/art/banner.png", readme)

    def test_it_declares_no_dependencies(self) -> None:
        # The whole argument for this client: cURL-free, requests-free, nothing to conflict
        # with what the host application already pins.
        self.assertIn("dependencies = []", self.manifest)


if __name__ == "__main__":
    unittest.main()
