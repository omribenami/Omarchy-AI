"""The first-run guide stays true for v0.15.0 and the listing page."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()
GUIDE = (ROOT / "docs" / "getting-started.md").read_text()
LISTING = (
    ROOT / "scripts" / "marketplace-plugin" / "templates" / "README.md"
).read_text()


class GettingStartedDocsTests(unittest.TestCase):
    def test_readme_leads_with_the_gateway_key_steps(self):
        start = README.index("## Getting started")
        self.assertLess(start, README.index("**Omarchy AI** is an agentic"))
        section = README[start : README.index("**Omarchy AI** is an agentic")]
        self.assertIn("docs/getting-started.md", section)
        self.assertIn("omarchy pkg add omarchy-ai", section)
        self.assertIn("https://github.com/omacom/omarchy-pkgs/pull/1196", section)
        self.assertIn("Gateway voice", section)
        self.assertIn("Vercel AI Gateway key", section)
        self.assertIn("set-vercel-gateway-api-key", section)
        self.assertIn("vercel-ai-gateway-key", section)
        self.assertIn("ai-gateway%2Fapi-keys", section)

    def test_guide_covers_install_key_voice_phone_logs_and_uninstall(self):
        for phrase in (
            "omarchy pkg add omarchy-ai",
            "omacom/omarchy-pkgs/pull/1196",
            "OmaStore",
            "bash install.sh",
            "systemctl --user enable --now omarchy-ai.service",
            "Gateway voice",
            "Vercel AI Gateway key",
            "set-vercel-gateway-api-key",
            "set provider omarchy",
            "~/.config/omarchy-ai/vercel-ai-gateway-key",
            "0600",
            "https://vercel.com/docs/ai-gateway/pricing",
            "Create key",
            "omachy",
            "Super + Ctrl + `",
            "Enable phone access",
            "Pair a phone",
            "journalctl --user -u omarchy-ai.service",
            "omarchy pkg drop omarchy-ai",
            "bash scripts/uninstall.sh",
            "OpenAI Live",
            "Gemini Live",
        ):
            self.assertIn(phrase, GUIDE, phrase)

    def test_listing_template_links_to_the_guide(self):
        self.assertLess(
            LISTING.index("## Getting started"), LISTING.index("## Features")
        )
        self.assertIn(
            "https://github.com/omribenami/Omarchy-AI/blob/main/docs/getting-started.md",
            LISTING,
        )
        self.assertIn("Gateway voice", LISTING)
        self.assertIn("Vercel AI Gateway key", LISTING)
        self.assertIn("omarchy-ai.settings", LISTING)


if __name__ == "__main__":
    unittest.main()
