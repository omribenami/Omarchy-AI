# Getting started

Save a Vercel AI Gateway key before the first conversation. Omarchy AI uses
that one key for speech, replies, and Jev's desktop, browser, and image
decisions. This page matches Omarchy AI 0.15.0 and the current `main`
branch.

OpenAI Live and Gemini Live are optional. They are at the end of this page.

## 1. Install

Pick one route. The package pull request notes that OmaStore refuses an
install that would shadow `/usr/bin/omarchy-ai`.

| Route | What you run | What it does |
| --- | --- | --- |
| Omarchy package | `omarchy pkg add omarchy-ai` | Installs a system copy under `/opt/omarchy-ai`, puts the commands on `PATH`, and installs a disabled user service. It does not run `install.sh`. |
| OmaStore | Open **Omarchy AI** in the store | Unpacks the latest stable GitHub Release into your account. The first launch runs `install.sh`, enables the user service, and opens a conversation. |
| Release archive | `bash install.sh` | Unpacks a GitHub Release in a folder you keep. You enable the user service yourself. |

All three need an x86-64 Omarchy desktop and a network connection during
install. None of them ships an API key.

### Omarchy package

This package is proposed in
[omacom/omarchy-pkgs#1196](https://github.com/omacom/omarchy-pkgs/pull/1196).
`omarchy pkg add omarchy-ai` works after that pull request is merged and
published. Until then, use OmaStore or the release archive.

```bash
omarchy pkg add omarchy-ai
```

The package installs:

- the program under `/opt/omarchy-ai`
- `omarchy-ai`, `omarchy-ai-settings`, `omarchy-ai-task`,
  `omarchy-ai-tool`, `omarchy-ai-dashboard`, and `omarchy-ai-vault` on
  `PATH`
- a user service at `/usr/lib/systemd/user/omarchy-ai.service`
- a desktop entry named **Omarchy AI**

The service stays disabled. The package does not install the bar panel or
the **Super + `** shortcut. Those come from the scripts below, after you
save the Gateway key in [step 3](#3-enter-the-key-in-settings).

### OmaStore

[OmaStore](https://github.com/KitsuneSemCalda/OmaStore) lists this app from
`omastore.toml` on the default branch. It installs Omarchy AI 0.12.1 or
newer. Version 0.12.0 has no launcher, so the store skips it.

The store unpacks the release archive and runs `bin/omarchy-ai`. It does
not run `install.sh` itself. The first launch of a new tree does:

1. Run `install.sh` (missing Arch packages, `uv`, the Python environment,
   wake word, desktop plugins, and the user service file).
2. Run `systemctl --user enable --now omarchy-ai.service`.
3. Open a conversation.

A terminal window stays open while that happens. Missing packages may ask
for your sudo password. When setup finishes, the terminal says Omarchy AI
is running.

Keep the unpacked directory. The service runs from it.

### Release archive

On an Omarchy desktop, the copy-paste block in the
[README fast install](../README.md#fast-install-copy-and-paste) downloads
the newest stable `vX.Y.Z` release, checks its `.sha256`, unpacks it, runs
`install.sh`, and enables the user service.

To install a file you already downloaded:

```bash
sha256sum -c omarchy-ai-<version>-linux-x86_64.tar.gz.sha256
tar -xzf omarchy-ai-<version>-linux-x86_64.tar.gz
cd omarchy-ai-<version>-linux-x86_64
bash install.sh
systemctl --user enable --now omarchy-ai.service
```

`install.sh` installs missing runtime packages, creates the Python
environment, copies the `omachy` wake word to
`~/.config/omarchy-ai/wake_models/`, installs the desktop plugins, and
writes `~/.config/systemd/user/omarchy-ai.service`. It leaves an existing
config and any existing key files in place. It prints the `systemctl`
command and does not start the service. The command above does.

Keep the unpacked directory. The service runs from it. A git checkout uses
the same `bash install.sh` and the same `systemctl` command.

## 2. Create a Vercel AI Gateway key

You need a [Vercel account](https://vercel.com/signup). The key belongs to
a Vercel team.

1. Open the [AI Gateway API Keys page](https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai-gateway%2Fapi-keys&title=AI+Gateway+API+Keys).
   In the dashboard that is **your team → AI Gateway → API Keys**.
   This [link opens the create dialog](https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai-gateway%2Fapi-keys%3FshowCreateKeyModal%3Dtrue&title=AI+Gateway+API+Keys).
2. Click **Create key**.
3. Name the key and select **Create API Key**.
4. Copy the key immediately. Vercel cannot show it again.

Vercel documents this on
[AI Gateway API Keys](https://vercel.com/docs/ai-gateway/authentication-and-byok/api-keys)
and [Getting Started with AI Gateway](https://vercel.com/docs/ai-gateway/getting-started).

**Credits.** AI Gateway bills token use from AI Gateway Credits at the
provider's list price, with no markup
([pricing](https://vercel.com/docs/ai-gateway/pricing)). To use the free
credits, [add a payment method](https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%3Fmodal%3Dadd-credit-card)
to the team. Free credits cover a subset of models and start on the first
Gateway request. Other models need purchased credits. Buying credits moves
the team to the paid tier, and the monthly free credit stops. Omarchy AI
calls Gateway models for speech, replies, and Jev decisions, so add credits
when a model you use is outside the free tier.

## 3. Enter the key in Settings

The installed default conversation model is **OpenAI Live**. That model
needs a separate OpenAI key. For the Gateway key alone, switch the
conversation model to **Gateway voice**.

### In the panel

`install.sh` and OmaStore place an **Omarchy-AI** icon on the right side of
the bar. Click it. The panel title is **Omachy**.

The Omarchy package does not place that icon. Run this once from a
terminal:

```bash
bash /opt/omarchy-ai/scripts/install-plugins.sh
bash /opt/omarchy-ai/scripts/install-keybinding.sh
```

The first command installs the bar panel and the other desktop plugins.
The second binds **Super + `** (voice) and **Super + Ctrl + `** (typing).
If the icon is still missing, run:

```bash
omarchy bar move omarchy-ai.settings --section right
```

In the panel, under **AI PROVIDER & ACCESS**:

1. Set **Conversation model** to **Gateway voice**.
2. Paste the key into the password field whose placeholder is **Vercel AI
   Gateway key**. You can also click **Paste**.
3. Click **Save key**. The line under the field changes to "Vercel AI
   Gateway key saved." The status line says "Gateway key saved — restart
   to apply."
4. Click **Apply saved changes**. If the service is stopped, that button
   says **Start assistant**.

A saved key changes the button to **Edit key**.

### From the terminal

The same two writes, without the panel. Pipe the key on stdin. Do not pass
it as an argument.

Release archive, OmaStore tree, or a git checkout (run this inside that
directory):

```bash
printf '%s' 'YOUR_KEY' | .venv/bin/omarchy-ai-settings set-vercel-gateway-api-key
.venv/bin/omarchy-ai-settings set provider omarchy
.venv/bin/omarchy-ai-settings restart
```

Omarchy package (`omarchy-ai-settings` is on `PATH`):

```bash
printf '%s' 'YOUR_KEY' | omarchy-ai-settings set-vercel-gateway-api-key
omarchy-ai-settings set provider omarchy
omarchy-ai-settings restart
```

The command rejects an empty value, a value shorter than 12 characters, or
a value that contains a space. The reply is one line of JSON. On success
it includes `"vercel_gateway_api_key": {"set": true, ...}` and it does not
print the key.

### Where the key is stored

The key file is `~/.config/omarchy-ai/vercel-ai-gateway-key`, mode `0600`.
`XDG_CONFIG_HOME` is honored when it is set. The key is not written into
`config.yaml`, and it is not loaded into the systemd unit environment.

**Conversation model** is `provider: omarchy` in
`~/.config/omarchy-ai/config.yaml`. The daemon reads that file on startup,
so the restart in the steps above is required.

## 4. Check that the key works

Saving the file does not call Vercel. The listening status only means the
wake-word process is up.

1. Open the panel. The Gateway line says the key is saved. After the
   restart, the status line says **Ready · listening for your wake word**.
2. Confirm the file from a terminal. This prints `set` and the path, not
   the key:

   ```bash
   omarchy-ai-settings get
   ```

   On an archive or OmaStore install, use
   `.venv/bin/omarchy-ai-settings get` from that directory.
3. Say **omachy**. You should hear the wake chime. Ask "what time is it?"
   or "turn the volume up." Say **bye** to hang up.
4. Watch the log while you do it:

   ```bash
   journalctl --user -u omarchy-ai.service -f
   ```

   A good start contains `omarchy-ai ready` and
   `wake word detected, starting live session`. A bad key contains
   `Gateway request failed`. A missing key contains
   `Vercel AI Gateway key is not configured`.

On the Omarchy package, run `omarchy-ai` once before that spoken check.
That command copies the `omachy` wake model into your config, starts the
service, and opens a conversation. **Apply saved changes** restarts the
service and does not copy the model. If the service was already started,
run `omarchy-ai`, then `systemctl --user restart omarchy-ai.service`.
Then enable it for later logins:

```bash
systemctl --user enable --now omarchy-ai.service
```

OmaStore and the README fast install already enable the service.

## 5. Wake word, voice, and typing

The wake word is **omachy** (oh-MAH-chee). The model file is
`~/.config/omarchy-ai/wake_models/omachy.onnx`. The daemon listens locally
and connects to Vercel only after you wake it.

- Say **omachy**, wait for the chime, then speak. Say **bye**, **goodbye**,
  **stop**, or **that's all** to hang up.
- Press **Super + `** to start a voice conversation without the wake word.
  The Omarchy package has this shortcut after
  `scripts/install-keybinding.sh`. You can also run
  `omarchy-ai-settings activate`.
- Press **Super + Ctrl + `** to type. The chat panel answers in text and
  stays quiet. **Esc** closes it. The same command is
  `omarchy-ai-settings chat-toggle`.

In the panel's **Appearance** section, **Text chat** can stay **On key** or
switch to **Always on screen**.

## 6. Open settings again

Click the **Omarchy-AI** bar icon, or run `omarchy-ai-settings get` to read
the current settings as JSON.

The panel sections are **Voice**, **Connections**, **People**, and
**Appearance**. **AI PROVIDER & ACCESS** stays at the top. **Apply saved
changes** restarts the user service. The panel refuses that restart while
a conversation is still open. Say **bye**, then click it again.

## 7. Pair a phone

Phone access is off until you turn it on.

1. Open settings → **Connections** → **PHONE BRIDGE**.
2. Turn on **Enable phone access**.
3. Click **Apply saved changes**.
4. Click **Pair a phone**.
5. Scan the QR code with the phone's camera. The code expires in 5
   minutes. The panel says "Scan with the phone's camera — expires in 5
   minutes."

The phone opens an HTTPS page served by this computer on port **8766**.
The certificate is the one this computer created for its LAN address, and
for its Tailscale address when Tailscale is up. Accept that certificate
on the phone.

The phone and the computer need to be on the same LAN, or on the same
tailnet with TCP 8766 allowed. If a firewall is on, allow your LAN subnet
to reach TCP 8766. After Tailscale connects, restart
`omarchy-ai.service` so the certificate includes the tailnet name, then
pair again.

**Revoke All** signs out every paired phone. An **Approval PIN** lets a
paired phone approve a waiting task. It is not your login password.

## 8. First things to try

Wake it, then try one short request at a time:

- "What time is it?"
- "Turn the volume up."
- "Switch to workspace 2."
- "Open a terminal."
- Press **Super + Ctrl + `** and type "What is on this workspace?"

Say **bye** when you are done. Risky actions, such as installs, deletes,
and root, wait for your approval. You can ignore those on the first run.

## 9. See that it is running, and where the logs are

```bash
systemctl --user status omarchy-ai.service
journalctl --user -u omarchy-ai.service -n 80 --no-pager
```

Follow the live log with `journalctl --user -u omarchy-ai.service -f`.

| What | Where |
| --- | --- |
| Service log | `journalctl --user -u omarchy-ai.service` |
| Your settings | `~/.config/omarchy-ai/config.yaml` |
| Gateway key | `~/.config/omarchy-ai/vercel-ai-gateway-key` |
| Wake word | `~/.config/omarchy-ai/wake_models/omachy.onnx` |
| Self-update log | `journalctl --user -u omarchy-ai-update.service` |
| Self-update progress | `~/.local/state/omarchy-ai/updates/install.json` |

`systemctl --user is-active omarchy-ai.service` prints `active` when the
daemon is up.

## 10. When the first run fails

**The service is inactive.** Start it:

```bash
systemctl --user enable --now omarchy-ai.service
```

On the Omarchy package, run `omarchy-ai` once before that so the wake
model is copied into `~/.config/omarchy-ai/wake_models/`. If the journal
says `wake word 'omachy' not found`, that copy has not happened yet.

**You say omachy and the conversation ends immediately.** The default
conversation model is still **OpenAI Live**, and
`~/.config/omarchy-ai/key` is missing. Set **Conversation model** to
**Gateway voice**, save the Gateway key, and click **Apply saved
changes**. The journal line is `live session crashed`.

**The journal says `Vercel AI Gateway key is not configured`.** The key
file is missing. Repeat [step 3](#3-enter-the-key-in-settings).

**The journal says `Gateway request failed`.** The key was rejected, or
the account cannot call that model. HTTP 401 means the key is wrong or
revoked. Create a new key, save it again, and restart. HTTP 402, or the
words "credits used up", means the credit balance is empty. The screen
shows red dollar signs and a notification. Add AI Gateway Credits. See
[pricing](https://vercel.com/docs/ai-gateway/pricing).

**Apply saved changes does nothing useful.** A conversation is still open.
The panel says a conversation appears to be in progress. Say **bye**, then
click **Apply saved changes** again.

**The bar icon is missing.** Archive and OmaStore installs put it on the
right. Move it back with
`omarchy bar move omarchy-ai.settings --section right`. The Omarchy
package needs the plugin command in
[step 3](#3-enter-the-key-in-settings). The terminal commands on that step
work either way.

**The wake word never fires, and Super + ` does.** The shortcut starts a
conversation without the microphone model. Check that PipeWire is running
(`systemctl --user status pipewire`) and that the journal contains
`loaded wake word model`. In the panel's **Voice** section, **Active
models** should include `omachy.onnx`.

**Pair a phone stays disabled.** Turn on **Enable phone access** and apply
the restart. The button also stays disabled while a QR code is being
generated. `qrencode` is installed by `install.sh` and by the Omarchy
package.

**The settings command says `no valid Vercel AI Gateway key provided`.**
The pipe was empty, the key is shorter than 12 characters, or a space was
pasted with it. Paste the key again with `printf`.

## 11. Uninstall

Your key and `~/.config/omarchy-ai/` stay until you delete that directory
yourself.

**Omarchy package.** Disable the service, then drop the package:

```bash
systemctl --user disable --now omarchy-ai.service
omarchy pkg drop omarchy-ai
```

That removes `/opt/omarchy-ai`, the `/usr/bin` commands, the desktop file,
and `/usr/lib/systemd/user/omarchy-ai.service`.

**Release archive, OmaStore tree, or a git checkout.** From that directory:

```bash
bash scripts/uninstall.sh
```

This disables the user service and deletes
`~/.config/systemd/user/omarchy-ai.service`. The Python environment stays
in that directory's `.venv/`. Delete the directory when you want the
program files gone.

Desktop plugins stay in `~/.config/omarchy/plugins/`. Remove the settings
icon with:

```bash
omarchy plugin remove omarchy-ai.settings
```

The other plugins installed beside it are `omarchy-ai.watchdog`,
`omarchy-ai.assistant-huds`, `omarchy-ai.chat-hud`,
`omarchy-ai.window-labels`, `omarchy-ai.myapi`,
`omarchy-ai.tv-discovery`, and `omarchy-ai.quota-alert`. Remove each the
same way if you want them gone.

The voice and typing shortcuts stay in `~/.config/hypr/bindings.lua`,
between the lines `-- >>> Omarchy AI assistant toggle >>>` and
`-- <<< Omarchy AI assistant toggle <<<`. Delete that block if you want
the keys back.

## Optional: OpenAI Live and Gemini Live

Leave **Conversation model** on **Gateway voice** if the Gateway key is
the only key you have.

- **OpenAI Live** talks to OpenAI directly. The field placeholder is
  **OpenAI key**. The key normally starts with `sk-`. It is stored at
  `~/.config/omarchy-ai/key`. From a terminal:
  `printf '%s' 'sk-...' | .venv/bin/omarchy-ai-settings set-api-key`
  (or `omarchy-ai-settings` on the package).
- **Gemini Live** talks to Google directly. The field placeholder is
  **Google AI Studio key**. It is stored at
  `~/.config/omarchy-ai/gemini-key`. From a terminal:
  `printf '%s' 'YOUR_KEY' | .venv/bin/omarchy-ai-settings set-gemini-api-key`.

Jev still uses the Vercel AI Gateway key when either of those models is
selected. Save that key too, then click **Apply saved changes**.
