# Marketplace listing for `omarchy-ai.settings`

The Omarchy plugin marketplace (plugins.omarchy.org, repository
[omacom/omarchy-plugin-marketplace](https://github.com/omacom/omarchy-plugin-marketplace))
lists one Quattro plugin per GitHub repository, with `manifest.json` at the
repository root. This monorepo's desktop UI is eight plugins under
`quickshell/plugins/`. The public face is the settings bar widget,
`omarchy-ai.settings`. The voice assistant stays in
[Omarchy-AI](https://github.com/omribenami/Omarchy-AI).

`scripts/marketplace-plugin/assemble.py` builds the listing tree:

- every file in `quickshell/plugins/omarchy-ai.settings/` at the tree root
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
  --description "Omarchy shell bar widget for Omarchy AI settings"

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

Do not open the marketplace issue until the listing repository is public
and you have approved the body below. Automated validation is not a
security review. Suggested listing metadata, matching the manifest
category: category `System`, tags `ai`, `bar`, `quickshell`. The plugin id
stays `omarchy-ai.settings` (outside the reserved `omarchy.*` namespace) so
local installs and the marketplace listing stay the same plugin.

Title: `[Plugin]: Omarchy AI Settings`

```markdown
### Repository URL

https://github.com/omribenami/omarchy-ai-settings

### Category

System

### Tags

ai, bar, quickshell

### Suggest a missing tag

_No response_

### Maintainer notes

Marketplace install is the settings bar widget only. The voice assistant
is installed from https://github.com/omribenami/Omarchy-AI releases.

### Submission checklist

- [x] The repository is public and contains installation and removal instructions.
- [x] I have documented the plugin license and any external dependencies.
- [x] I confirm that I own or have permission to submit this plugin and its preview assets.
- [x] The plugin does not overwrite user configuration without explicit consent.
- [x] I understand that approval is for listing and is not a security review.
```

File it only after those five checklist lines are true, with the GitHub CLI
against `omacom/omarchy-plugin-marketplace` as described in that
repository's `SUBMISSION.md`. Later sync commits move the listing `main`
ahead of the approved snapshot. The marketplace shows that as an unverified
update until you file a plugin-verification request for the new SHA.
