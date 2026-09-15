import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Settings panel for the Omarchy AI voice assistant — wake word model
// selection, the Watch Dogs overlay's on/off + display mode, wake
// sensitivity, and voice. Follows the same "Panel base + BarIconButton +
// KeyboardPanel" shape every other first-party bar-widget panel in this
// shell uses (see $OMARCHY_PATH/shell/plugins/panels/power/Panel.qml,
// copied as the closest template — battery-status-poll style Process
// calls, a KeyboardPanel popup, PanelSectionHeader-divided sections).
//
// Unlike a first-party panel, there's no Python service already running
// inside Quickshell to ask — reads/writes of
// ~/.config/omarchy-ai/config.yaml and the "is it safe to restart the
// daemon" check both need real Python (YAML merge-over-defaults logic
// already lives in src/omarchy_ai/config.py; duplicating it in JS would
// drift). So every control here calls out to
// src/omarchy_ai/cli/settings.py (installed as the `omarchy-ai-settings`
// console script in this project's uv-managed venv) via a Process,
// exactly the way $OMARCHY_PATH/shell/plugins/panels/dropbox's Panel.qml
// shells out to its own status.py. The venv path is hardcoded absolute —
// this is a single-machine, single-checkout project (systemd's own unit
// template does the same with its @VENV@ substitution).
//
// The daemon (omarchy-ai.service) only reads config.yaml at process
// startup (core/daemon.py's OmaDaemon.__init__), so every write here is
// inert until a restart — the Restart button calls `... restart`, which
// itself checks (via journalctl, replaying daemon.py's own "wake word
// detected"/"session ended" log lines) whether a conversation is
// currently in progress and refuses if so, mirroring the exact restart
// discipline this whole project has followed by hand all day (see
// CLAUDE.md / STATUS.md). Chose "UI triggers restart, but only when
// provably safe" over "always just tell the user to restart manually" —
// safer default given how easy a careless restart already proved to be
// to get wrong today, while still not silently interrupting anyone.
Panel {
  id: root
  moduleName: "omarchy-ai.settings"
  ipcTarget: "omarchy-ai.settings"

  readonly property string py: "/home/ben-ami/Git/omarchy-ai/.venv/bin/omarchy-ai-settings"

  property var snapshot: ({})
  property bool loaded: false
  property bool dirty: false
  property bool restarting: false
  property string statusMessage: ""
  property string statusTone: "info" // "info" | "ok" | "error"

  readonly property var fields: snapshot.fields || ({})
  readonly property var wakeModels: snapshot.wake_models || []
  readonly property var displayModes: snapshot.watchdog_display_modes || ["feed", "visualizer", "both"]
  readonly property var voiceOptions: snapshot.voice_options || ["marin"]

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color statusColor: statusTone === "error" ? "#ff6b6b" : (statusTone === "ok" ? "#39ff88" : Qt.darker(fg, 1.4))

  // Whether a live gpt-live-1 conversation is currently connected — set
  // via IPC from src/omarchy_ai/voice/live.py at session connect/hangup
  // (voice/status_icon.py's set_live(), called unconditionally, not
  // gated by watchdog_enabled: this bar-icon indicator is a separate,
  // always-on thing from the optional Watch Dogs overlay). User's own
  // request: red when idle, green while actually talking.
  property bool live: false
  readonly property color liveColor: live ? "#39ff88" : "#ff5f5f"

  // ---- Process queue: one Process instance, serialized calls ------------
  property var _queue: []

  function _enqueue(argv, cb, env) {
    root._queue.push({ argv: argv, cb: cb, env: env || ({}) })
    root._processQueue()
  }

  function _processQueue() {
    if (settingsProc.running) return
    if (root._queue.length === 0) return
    var next = root._queue.shift()
    settingsProc._cb = next.cb
    settingsProc.command = next.argv
    // Secrets ride in the environment, never in `command` — argv shows up
    // in /proc/<pid>/cmdline, which is world-readable, so an API key
    // passed as an argument would be visible in `ps` to anyone else with
    // a shell here. /proc/<pid>/environ is 0400 owner-only.
    settingsProc.environment = next.env || ({})
    settingsProc.running = true
  }

  Process {
    id: settingsProc
    property var _cb: null
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var result = null
        try {
          result = JSON.parse(text || "{}")
        } catch (e) {
          result = { error: "settings helper returned invalid output" }
        }
        var cb = settingsProc._cb
        settingsProc._cb = null
        if (cb) cb(result)
        root._processQueue()
      }
    }
  }

  function fetchSnapshot() {
    root._enqueue([root.py, "get"], function(result) {
      if (result && !result.error) {
        root.snapshot = result
        root.loaded = true
      } else if (result) {
        root.statusTone = "error"
        root.statusMessage = result.error
      }
    })
  }

  // Generic setter for every field except wake models (which needs its
  // own resync step below — see the comment there).
  function setField(key, jsonValue, successMessage) {
    root._enqueue([root.py, "set", key, jsonValue], function(result) {
      if (result && result.error) {
        root.statusTone = "error"
        root.statusMessage = result.error
      } else if (result) {
        root.snapshot = result
        root.dirty = true
        root.statusTone = "ok"
        root.statusMessage = successMessage || "Saved — restart to apply"
      } else {
        root.statusTone = "error"
        root.statusMessage = "No response from settings helper"
      }
    })
  }

  // Write-only from this layer's point of view: the key goes out via the
  // environment and the field is wiped immediately, and the helper's reply
  // only ever carries "is one set / where", never the value back. Nothing
  // here can re-display a stored key, by design.
  function saveApiKey(key) {
    var trimmed = (key || "").trim()
    if (trimmed.length === 0) {
      root.statusTone = "error"
      root.statusMessage = "Paste a key first"
      return
    }
    root._enqueue([root.py, "set-api-key"], function(result) {
      if (result && result.error) {
        root.statusTone = "error"
        root.statusMessage = result.error
      } else if (result) {
        root.snapshot = result
        root.dirty = true
        root.statusTone = "ok"
        root.statusMessage = "API key saved — restart to apply"
      } else {
        root.statusTone = "error"
        root.statusMessage = "No response from settings helper"
      }
    }, { "OMARCHY_AI_API_KEY": trimmed })
  }

  // ---- Phone bridge pairing (QR code) --------------------------------
  // Inline in the Phone Bridge section below, not a separate PopupCard —
  // tried that first and reverted, confirmed live: PopupCard's
  // onOpenChanged calls bar.requestPopout()/releasePopout(), the same
  // "only one popup open at a time" coordination every bar popout uses
  // (including this settings panel's own main KeyboardPanel) — opening a
  // second popout while this one was already open forced the bar to
  // close *this* one instead of showing both, so pressing the QR button
  // made the whole settings menu disappear with nothing to replace it.
  // Inline growth inside the already-scrollable Phone Bridge section
  // (below the wake_threshold Slider, which is what the earlier
  // Flickable-scoping fix was for) doesn't have this problem at all.
  property bool pairing: false
  property string qrImageBase64: ""
  property string qrUrl: ""
  property real qrExpiresAt: 0
  readonly property int pairedCount: snapshot.phone_bridge_paired_count || 0

  function pairPhone() {
    root.pairing = true
    root.qrImageBase64 = ""
    root._enqueue([root.py, "pair-phone"], function(result) {
      root.pairing = false
      if (result && result.error) {
        root.statusTone = "error"
        root.statusMessage = result.error
      } else if (result) {
        root.qrImageBase64 = result.qr_png_base64
        root.qrUrl = result.url
        root.qrExpiresAt = result.expires_at
        root.statusTone = "info"
        root.statusMessage = "Scan within 5 minutes"
      }
    })
  }

  function revokePhones() {
    root._enqueue([root.py, "revoke-phones"], function(result) {
      if (result && result.error) {
        root.statusTone = "error"
        root.statusMessage = result.error
      } else if (result) {
        root.snapshot = result
        root.statusTone = "ok"
        root.statusMessage = "All paired phones revoked"
      }
    })
  }

  function setWakeModels(vals) {
    root._enqueue([root.py, "set", "custom_wake_model_paths", JSON.stringify(vals)], function(result) {
      if (result && result.error) {
        root.statusTone = "error"
        root.statusMessage = result.error
      } else if (result) {
        root.snapshot = result
        root.dirty = true
        root.statusTone = "ok"
        root.statusMessage = "Wake models updated — restart to apply"
      }
      // MultiSelect mutates its own `values` property locally the instant
      // a row is clicked (before this callback runs), which permanently
      // breaks the `values: root.fields.custom_wake_model_paths` binding
      // below. Resync explicitly to the server-confirmed list every time,
      // success or reject — this is what makes a rejected "deselect the
      // last active model" attempt visibly snap back instead of leaving
      // the checkbox unchecked while the backend silently ignored it.
      wakeModelsSelect.values = (root.snapshot.fields && root.snapshot.fields.custom_wake_model_paths) || []
    })
  }

  function doRestart() {
    root.restarting = true
    root.statusTone = "info"
    root.statusMessage = "Restarting…"
    root._enqueue([root.py, "restart"], function(result) {
      root.restarting = false
      if (result && result.restarted) {
        root.dirty = false
        root.statusTone = "ok"
        root.statusMessage = "Daemon restarted"
      } else {
        root.statusTone = "error"
        root.statusMessage = (result && result.reason) || "Restart failed"
      }
    })
  }

  // Own IpcHandler alongside the base Panel component's (same pattern
  // $OMARCHY_PATH/shell/plugins/agents/Panel.qml uses for its extra
  // refresh/next methods) — re-forwards open/close/show/hide/toggle and
  // adds setLive for the bar-icon status dot.
  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function setLive(payloadJson: string): string {
      try {
        var p = JSON.parse(payloadJson || "{}")
        root.live = !!p.live
      } catch (e) {
        console.warn("omarchy-ai.settings: could not parse setLive payload:", e)
      }
      return "ok"
    }
  }

  onOpenedChanged: if (opened) { root.statusMessage = ""; root.statusTone = "info"; fetchSnapshot() }

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // The real Omarchy mark, not a generic gear — user's own request,
    // preferring the actual logo over any AI-generic glyph.
    // /usr/share/omarchy/logo.svg is the full wordmark (1215x285,
    // spelling "Omarchy" in blocky letterforms) — confirmed live by
    // screenshot that shrinking it to bar-icon size turns it into
    // illegible noise, wordmarks don't scale down like that.
    // /usr/share/pixmaps/omarchy.png is the actual square app-icon mark
    // (300x300, confirmed via `magick ... -format %[pixel:...]` that the
    // green maze pattern is opaque and everything else is alpha 0) —
    // built to read at icon size already, so used as-is here rather than
    // reinventing a monochrome glyph version of it.
    text: ""
    iconComponent: Component {
      Item {
        anchors.fill: parent

        Image {
          anchors.fill: parent
          source: "file:///usr/share/pixmaps/omarchy.png"
          sourceSize.width: Style.bar.iconCanvas * 2
          sourceSize.height: Style.bar.iconCanvas * 2
          fillMode: Image.PreserveAspectFit
        }

        // Live-status dot — red (idle) / green (an actual gpt-live-1
        // conversation is connected right now), bottom-right corner.
        // Small enough not to obscure the logo mark itself; a thin dark
        // ring around it keeps it legible against either dot color on a
        // busy wallpaper/bar background.
        Rectangle {
          width: Math.max(5, Math.round(parent.width * 0.32))
          height: width
          radius: width / 2
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          color: root.liveColor
          border.color: Qt.darker(Color.background, 1.6)
          border.width: 1

          Behavior on color { ColorAnimation { duration: 200 } }
        }
      }
    }
    slotSize: Style.bar.statusSlot
    fontSize: Style.font.caption
    tooltipText: "Omarchy AI Settings"
    onPressed: root.toggle()
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    // No fixed pixel cap here on purpose: the OpenAI API key section grew
    // the panel past the original Style.space(560) cap, and its content
    // rendered outside the drawn box instead of the box growing to fit —
    // confirmed live by screenshot. fittedContentHeight's own
    // availableCardHeight (screen-relative) is already the real safety
    // bound, so let content size the panel and rely on that instead of a
    // second, easy-to-outgrow fixed number.
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      // A Flickable wrapper was tried here first (to fit the Phone Bridge
      // section, which had pushed real content past availableCardHeight)
      // and reverted — confirmed live, it made the wake_threshold Slider
      // (a horizontal-drag control) fight the Flickable's own vertical
      // drag-to-scroll for the same pointer gesture, moving the slider on
      // an attempted scroll rather than scrolling. Fixed at the root
      // instead: the Phone Bridge section (below) now stays compact by
      // showing its QR in a separate PopupCard rather than growing this
      // column inline, so the panel fits without scrolling again.
      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Header ----------
        Column {
          width: parent.width
          spacing: Style.spacing.xxs
          Text {
            textFormat: Text.PlainText
            text: "Omarchy AI"
            color: root.fg
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }
          Text {
            textFormat: Text.PlainText
            text: "Voice assistant settings"
            color: Qt.darker(root.fg, 1.5)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---------- Wake word ----------
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader { text: "WAKE WORD"; foreground: root.fg; fontFamily: root.bar.fontFamily }

          MultiSelect {
            id: wakeModelsSelect
            width: parent.width
            label: "Active models"
            placeholderText: "Search models..."
            noSelectionText: "None active"
            foreground: root.fg
            background: Color.popups.background
            fontFamily: root.bar.fontFamily
            values: root.fields.custom_wake_model_paths || []
            options: {
              var opts = []
              for (var i = 0; i < root.wakeModels.length; i++)
                opts.push({ value: root.wakeModels[i].path, label: root.wakeModels[i].name })
              return opts
            }
            onChanged: function(vals) { root.setWakeModels(vals) }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.wakeModels.length === 0
            text: "No trained *.onnx models found under ~/.config/omarchy-ai/wake_models/"
            color: Qt.darker(root.fg, 1.5)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: "Wake sensitivity  " + Number(root.fields.wake_threshold !== undefined ? root.fields.wake_threshold : 0.5).toFixed(2)
            color: root.fg
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          PanelSlider {
            id: thresholdSlider
            width: parent.width
            bar: root.bar
            minimum: 0.1
            maximum: 0.9
            step: 0.05
            value: root.fields.wake_threshold !== undefined ? root.fields.wake_threshold : 0.5
            onReleased: function(v) { root.setField("wake_threshold", String(Math.round(v * 100) / 100)) }
          }
        }

        PanelSeparator { foreground: root.fg }

        // Everything below scrolls; the header/wake-word section above
        // (including thresholdSlider) deliberately does not, and never
        // will — confirmed live, twice: putting a Slider inside a
        // Flickable makes the two fight over the same drag gesture (an
        // attempted scroll drags the slider instead). Keeping the Slider
        // permanently outside any scrollable region is the actual fix,
        // not just tuning gesture priorities. height is capped to a
        // fixed budget rather than availableCardHeight directly (which
        // would need this binding and the panel's own contentHeight
        // binding below to reference each other) — generous enough for
        // every section here to fit unscrolled on most screens, so
        // scrolling only kicks in once content genuinely exceeds it.
        Flickable {
          id: scrollArea
          width: parent.width
          height: Math.min(scrollColumn.implicitHeight, Style.space(360))
          contentWidth: width
          contentHeight: scrollColumn.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          interactive: contentHeight > height

          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: scrollColumn
          width: parent.width
          spacing: Style.space(14)

        // ---------- Watch Dogs overlay ----------
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader { text: "WATCH DOGS OVERLAY"; foreground: root.fg; fontFamily: root.bar.fontFamily }

          Toggle {
            id: watchdogToggle
            width: parent.width
            label: "Show overlay"
            description: "Hacking-HUD panel with live conversation state, bottom-right, while a session is open."
            foreground: root.fg
            checked: root.fields.watchdog_enabled !== undefined ? !!root.fields.watchdog_enabled : true
            onClicked: root.setField("watchdog_enabled", watchdogToggle.checked ? "false" : "true")
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: "Display mode"
            color: Qt.darker(root.fg, 1.4)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
          }

          ButtonGroup {
            width: parent.width
            foreground: root.fg
            background: Color.background
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            value: root.fields.watchdog_display_mode || "feed"
            options: [
              { value: "feed", label: "Text feed" },
              { value: "visualizer", label: "ASCII visualizer" },
              { value: "both", label: "Both" }
            ]
            onChanged: function(v) { root.setField("watchdog_display_mode", JSON.stringify(v), "Display mode updated — restart to apply") }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: "Visualizer: a small ASCII/unicode amplitude bar reacting to the assistant's own speech, in place of (or alongside) the tool-call feed."
            color: Qt.darker(root.fg, 1.5)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---------- Voice ----------
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader { text: "VOICE"; foreground: root.fg; fontFamily: root.bar.fontFamily }

          Dropdown {
            id: voiceDropdown
            width: parent.width
            showLabel: false
            foreground: root.fg
            background: Color.popups.background
            fontFamily: root.bar.fontFamily
            value: root.fields.voice || "marin"
            options: root.voiceOptions
            onChanged: function(v) { root.setField("voice", JSON.stringify(v), "Voice updated — restart to apply") }
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---------- OpenAI API key ----------
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader { text: "OPENAI API KEY"; foreground: root.fg; fontFamily: root.bar.fontFamily }

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: {
              var st = root.snapshot.api_key || ({})
              if (!st.set) return "No key set — the assistant can't start a conversation without one."
              if (st.source === "omavoice") return "Using the key from omavoice. Paste one here to give Omarchy AI its own."
              return "A key is set. Paste a new one to replace it."
            }
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            color: (root.snapshot.api_key && root.snapshot.api_key.set)
              ? Qt.darker(root.fg, 1.4) : "#ff6b6b"
          }

          Row {
            width: parent.width
            spacing: Style.space(8)

            TextField {
              id: apiKeyField
              width: parent.width - saveKeyButton.width - Style.space(8)
              password: true
              placeholderText: "sk-..."
              foreground: root.fg
              font.family: root.bar.fontFamily
              onAccepted: {
                root.saveApiKey(text)
                text = ""
              }
            }

            Button {
              id: saveKeyButton
              text: "Save"
              bordered: true
              foreground: root.fg
              fontFamily: root.bar.fontFamily
              onClicked: {
                root.saveApiKey(apiKeyField.text)
                apiKeyField.text = ""
              }
            }
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---------- Phone bridge (talk to Omarchy from a phone) ----------
        // The QR shows inline below the buttons when pressed — see the
        // qrImageBase64 property comment above for why this isn't a
        // separate popup.
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader { text: "CONNECT YOUR PHONE TO OMARCHY AI"; foreground: root.fg; fontFamily: root.bar.fontFamily }

          Toggle {
            id: phoneBridgeToggle
            width: parent.width
            label: "Enable"
            description: "A local page a phone on this network can open to talk to Omarchy. Pairing is required — an unpaired phone can't use it."
            foreground: root.fg
            checked: root.fields.phone_bridge_enabled !== undefined ? !!root.fields.phone_bridge_enabled : false
            onClicked: root.setField("phone_bridge_enabled", phoneBridgeToggle.checked ? "false" : "true", "Phone bridge updated — restart to apply")
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.pairedCount > 0
            text: root.pairedCount + (root.pairedCount === 1 ? " phone paired" : " phones paired")
            color: Qt.darker(root.fg, 1.4)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Row {
            width: parent.width
            spacing: Style.space(8)

            Button {
              id: qrButton
              text: root.pairing ? "Generating…" : "QR"
              bordered: true
              foreground: root.fg
              fontFamily: root.bar.fontFamily
              enabled: !root.pairing && !!root.fields.phone_bridge_enabled
              onClicked: root.pairPhone()
            }

            Button {
              text: "Revoke All"
              bordered: true
              foreground: "#ff6b6b"
              fontFamily: root.bar.fontFamily
              enabled: root.pairedCount > 0
              onClicked: root.revokePhones()
            }
          }

          // Inline, not a popup — see the qrImageBase64 property comment
          // above for why. A one-time pairing link, valid 5 minutes;
          // scanning it is the only way a phone can reach the real page
          // at all (see phone/server.py's _is_paired gate). The QR image
          // never leaves this machine's screen, so the exposure window is
          // just "however long this panel is open and visible."
          Column {
            width: parent.width
            spacing: Style.space(6)
            visible: root.qrImageBase64.length > 0

            Image {
              width: 180
              height: 180
              anchors.horizontalCenter: parent.horizontalCenter
              source: root.qrImageBase64.length > 0 ? ("data:image/png;base64," + root.qrImageBase64) : ""
              fillMode: Image.PreserveAspectFit
              smooth: false
            }

            Text {
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              textFormat: Text.PlainText
              wrapMode: Text.WordWrap
              text: "Scan with the phone's camera — expires in 5 minutes"
              color: Qt.darker(root.fg, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }

            Button {
              text: "Hide"
              bordered: true
              foreground: root.fg
              fontFamily: root.bar.fontFamily
              anchors.horizontalCenter: parent.horizontalCenter
              onClicked: root.qrImageBase64 = ""
            }
          }
        }
        PanelSeparator { foreground: root.fg }

        // ---------- Footer: status + restart ----------
        Column {
          width: parent.width
          spacing: Style.space(8)

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.statusMessage !== ""
            text: root.statusMessage
            color: root.statusColor
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Row {
            width: parent.width
            spacing: Style.space(10)

            Button {
              text: root.restarting ? "Restarting…" : "Restart daemon to apply"
              bordered: true
              foreground: root.fg
              fontFamily: root.bar.fontFamily
              fontSize: Style.font.bodySmall
              enabled: !root.restarting
              onClicked: root.doRestart()
            }

            Text {
              textFormat: Text.PlainText
              anchors.verticalCenter: parent.verticalCenter
              visible: root.dirty && !root.restarting
              text: "changes pending"
              color: Qt.darker(root.fg, 1.3)
              font.family: root.bar.fontFamily
              font.italic: true
              font.pixelSize: Style.font.caption
            }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: (root.snapshot.config_path || "~/.config/omarchy-ai/config.yaml")
            color: Qt.darker(root.fg, 1.7)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideMiddle
          }
        }
      }
      }
      }
    }
  }
}
