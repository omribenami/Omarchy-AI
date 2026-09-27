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


class ReadmeVideoTests(unittest.TestCase):
    """2026-09-27: asked to replace the demo video, the assistant and two
    Task Runtime runs wrote `![](docs/media/desktop-demo.mp4)` and
    `<video src="docs/media/desktop-demo.mp4">`. GitHub strips both (checked
    with `gh api .../readme` as HTML: an empty <p>), and one run swapped the
    Phone session video instead. A README video must be a GitHub attachment,
    uploaded by dragging the mp4 into README.md in github.com's editor; git
    cannot create one (see knowledge/arch-operations.md)."""

    ATTACHMENT = re.compile(r"^https://github\.com/user-attachments/assets/[0-9a-f-]{36}$", re.M)

    def test_videos_are_github_attachments_not_repo_files(self):
        self.assertNotRegex(README, r"<video\b", "GitHub strips <video src=...> pointing into the repo")
        self.assertNotRegex(README, r"\]\([^)]*\.(mp4|webm|mov)\)", "a linked video file does not play on GitHub")

    def test_the_three_demo_videos_are_still_there(self):
        self.assertEqual(len(self.ATTACHMENT.findall(README)), 3, "main demo, phone session, phone bridge")
