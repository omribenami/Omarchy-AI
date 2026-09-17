# Gemini desktop conversations

In Assistant Settings, select Gemini, save a Google AI Studio key, then
choose **Apply saved changes**. The panel reports the running provider;
changing the selection saves configuration but does not replace an active
conversation. Keys stay hidden after saving; use **Edit key** to replace one.

Gemini microphone capture, playback, message reception and desktop actions
run independently. You can continue speaking while an action executes.
Desktop actions run in order, and non-blocking tool results are delivered
when the model is idle. Interrupting speech discards buffered playback.
Cancelling a pending tool skips it; an OS action already running cannot be
undone by interrupting speech. Session teardown waits for that action.

Saved preferences and archived-context safeguards are shared with the
desktop OpenAI integration. Errors return to wake-word listening instead
of silently switching providers. The configured session time limit applies.

The phone-browser bridge follows the selected provider. Gemini uses a
server-side WebRTC-to-Gemini bridge; the key remains on the computer.
Refresh the phone page after upgrading, then reconnect. Browser microphone
capture requests echo cancellation and noise suppression in both input modes.
Some shared tools, including screen analysis, retain their existing service
dependencies.

Desktop Gemini sessions load a private PipeWire WebRTC echo-cancellation
module with noise suppression and a high-pass filter. Both microphone and
assistant playback explicitly use its paired virtual devices. System default
devices are unchanged; the module is unloaded when the session ends. If the
processor cannot start, the session fails visibly instead of silently using
a raw microphone. Install `pipewire-audio`, `pipewire-pulse`, and `libpulse`.

Check failures with `journalctl --user -u omarchy-ai.service -n 100`.
No integration can guarantee uninterrupted availability of the external
API, network, microphone or audio server. To verify your local audio setup,
ask for a desktop action and speak another question while it is running.

Protocol reference: https://ai.google.dev/gemini-api/docs/live-api/tools
