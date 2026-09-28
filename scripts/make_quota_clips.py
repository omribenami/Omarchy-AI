"""Render the spoken alerts (quota and model fallback) as clips.

The repository ships them in English (voice/alerts/<stem>-en.ogg). Other
languages are rendered on each machine, in the user's language, by
omarchy_ai.core.alert_clips; this script can also do that by hand. Re-run
after changing the wording in omarchy_ai.core.quota.MESSAGES or
omarchy_ai.voice.fallback, or after changing gemini_model (a fallback
notice names it):

    .venv/bin/python scripts/make_quota_clips.py            # shipped English clips
    .venv/bin/python scripts/make_quota_clips.py --lang fr  # this machine, French

Existing clips are kept; delete one to render it again. Each take is kept
only after it is checked against a transcript of what was said.
"""
import argparse
import asyncio

from omarchy_ai.config import load_config
from omarchy_ai.core import alert_clips


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="en", help="ISO 639-1 code (default: en, the shipped clips)")
    args = parser.parse_args()
    config = load_config()
    dest = alert_clips.PACKAGED if args.lang == "en" else alert_clips.LOCAL
    todo = {stem: text for stem, text in alert_clips.texts(config).items()
            if not (dest / f"{stem}-{args.lang}.ogg").exists()}
    for path in asyncio.run(alert_clips.render(config, args.lang, todo, dest)):
        print(path)
    print(f"{len(todo)} to render; missing now: {sorted(alert_clips.missing(config, args.lang))}")


if __name__ == "__main__":
    main()
