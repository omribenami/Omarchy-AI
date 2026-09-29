# Marketplace listing for Omarchy AI

The Omarchy plugin marketplace (plugins.omarchy.org, repository
[omacom/omarchy-plugin-marketplace](https://github.com/omacom/omarchy-plugin-marketplace))
lists one Quattro plugin per GitHub repository, with `manifest.json` at the
repository root. This monorepo's desktop UI is eight plugins under
`quickshell/plugins/`. The public listing is the front door for the full
[Omarchy AI](https://github.com/omribenami/Omarchy-AI) assistant. Its README
(`templates/README.md`) is that front door: the product pitch, features, and
the demo videos from the Omarchy AI README. Catalog cards use the manifest
`description` plus `preview.png`. The repository still contains one plugin,
the settings bar widget `omarchy-ai.settings`. `omarchy plugin add` does not
install the daemon. The plugin id stays `omarchy-ai.settings` so the live page
<https://omarchyplugins.com/plugin.html?id=omarchy-ai.settings> keeps
working.

`scripts/marketplace-plugin/assemble.py` builds the listing tree:

- every file in `quickshell/plugins/omarchy-ai.settings/` at the tree root,
  including `preview.png`. That still is a frame from
  `docs/media/desktop-demo.mp4` (the primary README demo). The marketplace
  shows it on catalog cards and does not host video; the listing README
  embeds the demo videos instead. `sync.sh` needs no extra preview path:
  `assemble.py` already copies every plugin file to the listing root.
- `templates/README.md` and `templates/LICENSE` at the tree root
- `manifest.json` id, name, kinds, and entry points copied as-is; `author`
  set to `Omri Ben Ami`

`scripts/marketplace-plugin/seed/` is that tree, checked in so the listing
repository can be created before the GitHub Action has a token. Regenerate
it whenever the plugin or the templates change:

```bash
python3 scripts/marketplace-plugin/assemble.py scripts/marketplace-plugin/seed
```

`tests/test_marketplace_plugin_sync.py` fails when `seed/` drifts from the
assembler.

## Create the public listing repository

This checkout's GitHub token cannot create repositories under `omribenami`
(`Resource not accessible by integration (createRepository)`). The name
`omribenami/omarchy-ai-settings` was free when that was checked. If it is
taken, use `omribenami/Omarchy-AI-plugin`, change `LISTING_REPO` in
`listing.env`, and regenerate `seed/`.

From a machine logged in as `omribenami` (`gh auth login`, `repo` scope):

```bash
gh repo create omribenami/omarchy-ai-settings \
  --public \
  --description "Omarchy AI voice assistant. This marketplace entry is the settings panel; install the full assistant from Omarchy-AI releases."

workdir="$(mktemp -d)"
git init -b main "$workdir"
cp -a scripts/marketplace-plugin/seed/. "$workdir/"
git -C "$workdir" add -A
git -C "$workdir" -c user.name="Omri Ben Ami" -c user.email="omribenami@users.noreply.github.com" \
  commit -m "Seed omarchy-ai.settings marketplace listing"
git -C "$workdir" remote add origin https://github.com/omribenami/omarchy-ai-settings.git
git -C "$workdir" push -u origin main
```

An empty repository is enough if you would rather let the Action publish
the first commit. After this workflow is on `main` and the secret below
exists, run **Sync marketplace plugin** with `workflow_dispatch`. The sync
script pushes `main` when the remote has no `main` yet.

## Secret

On **omribenami/Omarchy-AI** (Settings → Secrets and variables → Actions),
add a repository secret:

| Name | Value |
| --- | --- |
| `MARKETPLACE_PLUGIN_SYNC_TOKEN` | PAT or fine-grained token with **contents:write** on the listing repo |

Fine-grained token: resource owner `omribenami`, repository access only
`omarchy-ai-settings`, permission **Contents: Read and write**. A classic
PAT needs `public_repo` for a public listing repo (or `repo` if the listing
is private while you are setting it up).

The workflow reads that secret and passes it to `scripts/marketplace-plugin/sync.sh`.
If the secret is missing or blank, the job prints an error and exits
without creating a listing commit. It does not fall back to `GITHUB_TOKEN`.

## What the Action syncs

`.github/workflows/sync-marketplace-plugin.yml` runs when:

- `main` receives a push that changes
  `quickshell/plugins/omarchy-ai.settings/**`,
  `scripts/marketplace-plugin/**`, or the workflow file itself
- someone starts it with **workflow_dispatch**

Other paths in this monorepo do not start a sync. The script assembles a
clean tree, and it commits only when that tree differs from the listing
`main`. The commit message includes the Omarchy-AI SHA. The push
fast-forwards `main`. It does not force-push: `omarchy plugin update`
fast-forwards the installed checkout, and a rewritten `main` would strand
those installs.

## Marketplace submission

The listing is already published as `omarchy-ai.settings`
([omacom/omarchy-plugin-marketplace#9079](https://github.com/omacom/omarchy-plugin-marketplace/issues/9079),
approved and verified). Do not open a second submission. Changing the
plugin id would break that page. Suggested metadata, matching the manifest
category: category `System`, tags `ai`, `bar`, `quickshell`.

Title on the live issue: `[Plugin]: Omarchy AI Settings`. The product name
in the manifest is now `Omarchy AI`. A verification request for a newer
listing SHA should use that name and these maintainer notes:

```markdown
The marketplace page is the front door for the full Omarchy AI assistant.
The listing README has the product features and demo videos. Catalog cards
use the manifest description and preview.png. Install the assistant from
https://github.com/omribenami/Omarchy-AI releases (`install.sh`).
`omarchy plugin add` still installs only the settings bar widget and does
not run the daemon. The panel resolves `omarchy-ai-settings` at runtime
and, when the assistant is absent, explains that install. It does not
depend on an install-time path rewrite. Standard installation can be
requested for a listing commit that contains this lookup; this repository
does not apply that marketplace label.
```

Later sync commits move the listing `main` ahead of the approved snapshot.
The marketplace shows that as an unverified update until a
plugin-verification request is filed for the new SHA, as described in that
repository's `SUBMISSION.md`. The original checklist remains true: the
repository is public, install and remove are documented, the license and
the external daemon dependency are documented, and the panel does not
overwrite user configuration without an explicit action.
