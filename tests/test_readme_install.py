import re
import unittest
from pathlib import Path

README = (Path(__file__).parents[1] / "README.md").read_text()


class ReadmeFastInstallTests(unittest.TestCase):
    """Regression: the fast-install block pinned version=0.3.10, a version that
    never had a GitHub Release, so its first curl was a 404 for everyone."""

    def block(self):
        return README.split("### Fast install (copy and paste)", 1)[1].split("```bash\n", 1)[1].split("```", 1)[0]

    def test_version_is_discovered_not_pinned(self):
        block = self.block()
        self.assertIsNone(re.search(r"^version=\d", block, re.M), "fast install must not pin a version")
        self.assertIn("api.github.com/repos/omribenami/Omarchy-AI/releases", block)
        self.assertIn("sort -V | tail -1", block)

    def test_downloads_release_assets_and_verifies_before_unpacking(self):
        block = self.block()
        self.assertIn("releases/download/v${version}", block)
        self.assertLess(block.index("sha256sum -c"), block.index("tar -xzf"))

    def test_tag_filter_skips_non_version_releases(self):
        pattern = self.block().split("grep -o '", 1)[1].split("'", 1)[0]
        sample = '"tag_name": "demo-media", "tag_name": "v0.4.1", "tag_name": "v0.10.0"'
        self.assertEqual(re.findall(pattern.replace("*\\.", "*\\."), sample), ['"tag_name": "v0.4.1"', '"tag_name": "v0.10.0"'])


if __name__ == "__main__":
    unittest.main()
