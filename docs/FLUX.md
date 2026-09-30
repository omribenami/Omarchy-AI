# Omarchy AI with Flux

[Flux](https://github.com/bjarneo/flux) by [@bjarneo](https://github.com/bjarneo)
connects an Omarchy computer and an Android phone: notifications, clipboard,
files, SMS, a remote touchpad and keyboard, the desktop on the phone, herdr
terminals, and fingerprint approval for `sudo`. All of that is Flux's work.
Omarchy AI builds on it and does not replace any of it.

This page covers two parts:

1. **What works today with stock Flux.** It is in Omarchy AI 0.12.0 and needs
   no change to the Flux app.
2. **Omarchy AI inside the Flux Android app.** This is a prototype of changes to
   the Flux app. Flux has not chosen a license yet, so we do not publish a
   modified Flux APK or its source without the author's permission. We have
   [asked for it](#status-of-the-flux-app-changes).

## 1. With stock Flux

Install Flux on the computer and the phone, and pair them, as the
[Flux README](https://github.com/bjarneo/flux) describes. Check it with
`flux-cli status`: the phone should show as `connected` and `paired`.

### Omarchy AI notifications on the phone

Everything Omarchy AI tells you on the desktop also reaches the paired phone
through Flux: a task that needs approval or has a question, a finished or
failed task, a routine, a provider out of credit or quota, and a tool that
asks to be installed. Titles start with `Omarchy AI ·`, and approvals start
with `Omarchy AI · Approval:`.

- On by default. It needs no setup beyond a paired, connected phone.
- To turn it off, set `flux_notifications: false` in
  `~/.config/omarchy-ai/config.yaml`.
- Without Flux, or with no phone connected, nothing is sent and nothing fails.

### Approve with your fingerprint

Flux can sign an approval with a key in the phone's secure hardware that only
works after a strong biometric check ([Flux's approval
design](https://github.com/bjarneo/flux/blob/master/docs/approve.md)). Omarchy
AI uses the same key:

- A task that waits for your approval asks the phone once: the fingerprint
  prompt, with no separate notification. A fingerprint there approves it.
  Without fingerprint approval, the phone gets one notification that says to
  approve on the desktop. Omarchy AI rebuilds the signed message itself and verifies it
  with the root-owned key, as Flux's PAM helper does.
- With Flux approval on for `sudo`, tasks that need root send no saved
  password. `sudo` asks the phone, and you approve with your fingerprint.
- No answer on the phone falls back to the usual desktop notification, the
  approval PIN, or the password.

Turn it on:

```bash
sudo flux-cli approve setup                # enroll the phone, turn it on for sudo
sudo flux-cli approve enable polkit-1      # optional: graphical admin prompts too
flux-cli approve status                    # check
```

`flux-cli approve status` should say that your phone can approve, and that
`sudo` asks the phone first. Omarchy AI checks this on every request, so it
needs no restart.

Approve a request while the phone is unlocked in your hand. The phone has
about 20 seconds to answer, and after that the password prompt takes over.

#### Optional: unlock the Omarchy lock screen from the phone

`flux-cli approve enable hyprlock` only handles hyprlock. The Omarchy lock
screen (Quickshell) uses the PAM service `omarchy-lock-password` instead, so
this step is manual. Only do it if you are comfortable with PAM:

```bash
sudo cp -p /etc/pam.d/omarchy-lock-password /etc/flux/approve/pam-backup/omarchy-lock-password
sudoedit /etc/pam.d/omarchy-lock-password
```

Add these two lines directly **after** the first
`auth required pam_faillock.so preauth ...` line:

```
# Flux: approve with the phone fingerprint. `sudo flux-cli approve disable` removes it.
auth sufficient pam_exec.so quiet stdout /usr/lib/flux/flux-approve
```

To unlock, type any character, press Enter, and approve on the phone.

Things to know:

- If you type your real password, it is checked after the phone request is
  declined or times out (about 20 seconds). Declining on the phone skips the
  wait.
- Placing the line after the `faillock` pre-check means that a locked-out
  account (10 failed tries) cannot be unlocked from the phone either.
- An Omarchy update that re-runs `omarchy-apply-lock` rewrites this file and
  removes the line. `flux-cli approve disable` does not remove it, because
  Flux does not manage this file. To undo it, copy the backup back.

### Phone bridge endpoints the Flux app uses

These are part of Omarchy AI's phone bridge (`phone_bridge_enabled: true`,
HTTPS on port 8766). They matter only to the modified Flux app below.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/hello` | Public, no pairing data: tells the app that Omarchy AI answers on this computer |
| `GET /api/flux/challenge`, `POST /api/flux/pair` | Automatic pairing: the phone signs a single-use nonce with its Flux identity key, checked against desktop Flux's `devices.json` |
| `GET /api/wake`, `/api/wake/<model>.onnx` | The computer's wake-word models, for "Wake Omarchy" on the phone |
| `GET /api/tvs`, `POST /api/cast`, `POST /api/cast/stop` | Mirror to TV from the phone |
| `POST /api/live/offer`, `POST /api/tool` | Voice and text calls: the app's native WebRTC client sends its offer (with an `oai-events` data channel) and, for OpenAI, runs the model's tool calls on the computer |
| `GET /api/approvals`, `POST /api/approvals/fingerprint`, `POST /api/approvals/respond` | The pending-approvals list: Approve sends the fingerprint prompt (only the signed fingerprint approves), Deny needs nothing more |
| `POST /api/ask` | Send a request to the Task Runtime |
| `POST /api/flux/setting` | Turn on an allowlisted Flux feature (`remote_input`, `remote_desktop`, `herdr`, `herdr_control`, `herdr_terminals`) through fluxd's own `settings.set` |
| `POST /mirror/start`, `GET /mirror/stream`, `POST /mirror/stop` | The screen stream that the app shows in its remote desktop |

Every endpoint except `/api/hello` requires a paired session. The app uses
only these endpoints and never opens Omarchy AI's web page.

## 2. Omarchy AI inside the Flux app (prototype)

Built and tested on a Samsung S23 Ultra (One UI, Android 16) against Flux
0.5.0. Every Omarchy AI surface in the app appears only for a computer where
Omarchy AI answered `/api/hello`, so Flux users without Omarchy AI see stock
Flux.

- **Omarchy AI section** on the device page, with Text with Omarchy, Mirror to
  TV, Wake Omarchy, and a switch that makes Omarchy AI the phone's digital
  assistant (a bottom sheet with Omarchy's ASCII voice meter).
- **Wake word on the phone.** openWakeWord runs on the phone with the
  computer's own models. The audio never leaves the phone until the wake word
  is heard.
- **Remote desktop from Omarchy AI's screen stream.** It uses a pinned
  certificate and automatic pairing, so there is no certificate warning. Flux's
  touch, keys and panels are unchanged.
- **Talk to Omarchy in the remote-control keyboard.** The Omarchy key runs the
  call in place. The key shows the connection state (pulsing while connecting,
  a green frame when connected, red on an error) and Omarchy's ASCII visualizer
  while she talks, so the screen stays visible.
- **Pending approvals** on the computer's page in Flux, each with Approve
  (your fingerprint) and Deny. An approval nobody answers expires after
  `task_approval_hours` (4 by default), and one the current rules no longer
  ask about goes ahead on its own. Saying "approve" to Omarchy for a HIGH-risk
  step sends the same fingerprint prompt.
- **Notification taps.** Tapping an approval opens Flux's fingerprint prompt.
  Tapping any other Omarchy AI notification opens the computer's page in Flux.
  No tap opens Omarchy AI's web page.
- **Audio routing.** The app keeps a connected car (Android Auto), Bluetooth,
  USB or TV output. With no external output it uses the loudspeaker, and it
  releases the route when the call ends.
- **One-tap Flux settings.** Flux features that are off get an Omarchy button
  that turns them on through fluxd.

### Status of the Flux app changes

Flux is a public repository without a license yet, so its code is all rights
reserved by default. We asked the author whether they want these changes, as
a pull request or an optional build, and whether a modified APK may be
distributed: [bjarneo/flux#77](https://github.com/bjarneo/flux/issues/77). Until they answer, the Android changes and the
APK are not published. Everything in part 1 works now with stock Flux.

## Credits

Flux, its Android app, its KDE Connect-compatible protocol, the approval
design and its PAM helper are the work of
[@bjarneo](https://github.com/bjarneo) and the Flux
contributors: <https://github.com/bjarneo/flux>. Omarchy AI's parts use Flux's
public interfaces (`flux-cli`, the fluxd socket, the enrolled approval key)
and do not copy Flux code.
