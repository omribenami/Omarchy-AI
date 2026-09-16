import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Small centered "which TV?" overlay for the Omarchy AI voice assistant's
// casting flow — shown by execution/actions.py's start_casting the
// instant a cast is requested, kept live-updated by
// display/tv_overlay.py's background poll loop while it's open, and
// dismissed automatically once a target is resolved (by voice or by a
// manual click here).
//
// CRITICAL, per the task this was built from: this overlay and the voice
// agent share the exact same device state. Nothing here maintains its own
// discovery — devices arrive purely via IPC pushes (show/update) from
// display/registry.py's snapshot, and a manual click here
// (clickDevice below) calls the identical execution.actions.start_casting
// function the voice path calls (via cli/settings.py's
// select-cast-target), not a separate connect implementation.
//
// Visual language deliberately reused from omarchy-ai.watchdog's
// hacking-HUD look (same accent palette, glitch/braille character mixing,
// scanline sweep) rather than inventing a new one — this overlay should
// read as a sibling of that one, not a bolted-on settings dialog. Unlike
// watchdog/window-labels (fully click-through, `mask: Region {}`), this
// one needs real mouse interaction on its card — confirmed the idiom this
// shell already uses for that (plugins/notifications/Service.qml):
// `mask: Region { item: <interactive item> }` scopes the click region to
// just that item, leaving the rest of the transparent full-screen surface
// click-through.
Item {
  id: root

  property bool opened: false
  // [{name, address, status, last_seen}], as pushed by display/registry.py
  property var devices: []
  property string selectedAddress: ""
  property bool refreshing: false
  readonly property string py: "/home/ben-ami/Git/omarchy-ai/.venv/bin/omarchy-ai-settings"

  readonly property color rainGreen: "#39ff88"
  readonly property color rainCyan: "#39e6ff"
  readonly property color rainLime: "#a6ff4d"
  readonly property color rainErr: "#ff5f5f"

  function statusColor(s) {
    if (s === "connected") return rainGreen
    if (s === "connecting") return rainCyan
    if (s === "online") return rainLime
    if (s === "offline") return Util.alpha(rainErr, 0.75)
    return Util.alpha(rainGreen, 0.35) // "unknown"
  }
  function statusLabel(s) {
    if (s === "connected") return "CONNECTED"
    if (s === "connecting") return "CONNECTING…"
    if (s === "online") return "ONLINE"
    if (s === "offline") return "OFFLINE"
    return "UNKNOWN"
  }
  function statusPulses(s) {
    return s === "connecting"
  }

  // Same glitch/braille density-ramp vocabulary as Watchdog.qml's
  // visualizerLine() — reused here for a small decorative "scanning" row
  // rather than an amplitude readout, so the two overlays share one
  // visual language instead of two.
  readonly property var glitchChars: ["▓", "▒", "░", "╳", "┃", "╎", "⠿", "⣿"]
  function glitchNoise(len) {
    var s = ""
    for (var i = 0; i < len; i++)
      s += glitchChars[Math.floor(Math.random() * glitchChars.length)]
    return s
  }

  function parseDevices(payloadJson) {
    try {
      var p = JSON.parse(payloadJson || "{}")
      return Array.isArray(p.devices) ? p.devices : root.devices
    } catch (err) {
      console.warn("tvDiscovery: could not parse payload:", err)
      return root.devices
    }
  }

  function open(payloadJson) {
    root.devices = parseDevices(payloadJson)
    root.selectedAddress = ""
    dismissTimer.stop()
    selectionTimeout.restart()
    root.opened = true
  }

  function updateDevices(payloadJson) {
    root.devices = parseDevices(payloadJson)
  }

  function selectDevice(payloadJson) {
    try {
      var p = JSON.parse(payloadJson || "{}")
      root.selectedAddress = String(p.address || "")
    } catch (err) {
      console.warn("tvDiscovery: could not parse select payload:", err)
    }
    // Long enough to actually read "CONNECTED" before the fade-out — same
    // "let the user see the outcome" grace period execution/actions.py's
    // own start_casting sleeps for server-side.
    dismissTimer.restart()
  }

  function close() {
    dismissTimer.stop()
    selectionTimeout.stop()
    root.opened = false
    root.selectedAddress = ""
  }

  Timer {
    id: dismissTimer
    interval: 1200
    onTriggered: root.close()
  }

  // Never leave the display picker stranded if discovery or pairing fails.
  Timer {
    id: selectionTimeout
    interval: 30000
    onTriggered: root.close()
  }

  IpcHandler {
    target: "tvDiscovery"
    function show(payloadJson: string): string { root.open(payloadJson); return "ok" }
    function update(payloadJson: string): string { root.updateDevices(payloadJson); return "ok" }
    function select(payloadJson: string): string { root.selectDevice(payloadJson); return "ok" }
    function hide(): string { root.close(); return "ok" }
    function state(): string { return root.opened ? "open" : "closed" }
    function ping(): string { return "ok" }
  }

  // ---- Manual click / Refresh backend — same "shell out to
  // src/omarchy_ai/cli/settings.py via a serialized Process queue" pattern
  // every other interactive panel in this project uses. ----
  property var _queue: []

  function _enqueue(argv, cb) {
    root._queue.push({ argv: argv, cb: cb })
    root._processQueue()
  }

  function _processQueue() {
    if (backendProc.running) return
    if (root._queue.length === 0) return
    var next = root._queue.shift()
    backendProc._cb = next.cb
    backendProc.command = next.argv
    backendProc.running = true
  }

  Process {
    id: backendProc
    property var _cb: null
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var result = null
        try {
          result = JSON.parse(text || "{}")
        } catch (e) {
          result = { error: "backend returned invalid output" }
        }
        var cb = backendProc._cb
        backendProc._cb = null
        if (cb) cb(result)
        root._processQueue()
      }
    }
  }

  // Clicking a row calls execution.actions.start_casting directly (via
  // cli/settings.py's select-cast-target) — the exact same function a
  // voice-resolved target calls. The real outcome (connecting -> connected
  // or a failure) arrives back here the normal way: start_casting itself
  // drives registry.mark_*/tv_overlay.push_update()/select(), which are
  // just more IPC calls into this same handler.
  function clickDevice(address) {
    if (root.selectedAddress !== "") return // a resolution is already in flight
    root.selectedAddress = address // optimistic highlight while connecting
    root._enqueue([root.py, "select-cast-target", address], function(result) {
      if (result && result.error) {
        console.warn("tvDiscovery: select-cast-target failed:", result.error)
      }
    })
  }

  function refresh() {
    if (root.refreshing) return
    root.refreshing = true
    root._enqueue([root.py, "refresh-cast-targets"], function(result) {
      root.refreshing = false
      if (result && result.devices) root.devices = result.devices
    })
  }

  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: panel
      required property var modelData
      screen: modelData
      visible: root.opened
      anchors { top: true; bottom: true; left: true; right: true }
      color: "transparent"
      WlrLayershell.namespace: "omarchy-ai-tv-discovery"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      exclusionMode: ExclusionMode.Ignore
      // Click-through everywhere except the card itself — the rest of
      // this full-screen transparent surface must never eat input meant
      // for the desktop underneath it.
      mask: Region { item: card }

      readonly property int cardWidth: Style.space(300)

      BorderSurface {
        id: card
        width: panel.cardWidth
        anchors.centerIn: parent
        height: cardColumn.implicitHeight + card.borderTop + card.borderBottom + Style.space(28)
        color: Util.alpha(Color.background, 0.90)
        radius: Style.cornerRadius
        borderSpec: Border.flat(Util.alpha(root.rainGreen, 0.55), Math.max(1, Style.space(2)))
        opacity: root.opened ? 1 : 0
        scale: root.opened ? 1.0 : 0.96

        Behavior on opacity { NumberAnimation { duration: 180; easing.type: Easing.OutQuad } }
        Behavior on scale { NumberAnimation { duration: 220; easing.type: Easing.OutBack } }

        // Scanline sweep, same look as Watchdog.qml's card — the one
        // shared "glitch" cue every overlay in this project uses, so
        // opening/closing/updating all read as the same instrument.
        Rectangle {
          width: card.width
          height: Math.max(1, Style.space(2))
          color: Util.alpha(root.rainGreen, 0.12)
          y: 0
          SequentialAnimation on y {
            running: root.opened
            loops: Animation.Infinite
            NumberAnimation { from: 0; to: card.height; duration: 2600; easing.type: Easing.Linear }
            PauseAnimation { duration: 150 }
          }
        }

        Column {
          id: cardColumn
          anchors.horizontalCenter: parent.horizontalCenter
          anchors.top: parent.top
          anchors.topMargin: card.borderTop + Style.space(14)
          width: parent.width - Style.space(28)
          spacing: Style.space(10)

          Item {
            width: parent.width
            height: Math.max(titleText.implicitHeight, glitchText.implicitHeight)

            Text {
              id: titleText
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: "SELECT DISPLAY"
              font.family: Style.font.family
              font.bold: true
              font.pixelSize: Style.font.title
              color: root.rainGreen
            }

            // Small glitch-noise flourish, live during a refresh — the
            // "updating" glitch cue the task asked for, distinct from the
            // steady scanline above.
            Text {
              id: glitchText
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: root.refreshing ? root.glitchNoise(6) : ""
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              color: Util.alpha(root.rainCyan, 0.7)

              Timer {
                interval: 130
                running: root.refreshing
                repeat: true
                onTriggered: glitchText.text = root.glitchNoise(6)
              }
            }
          }

          Rectangle {
            width: parent.width
            height: 1
            color: Util.alpha(root.rainGreen, 0.25)
          }

          Text {
            width: parent.width
            visible: root.devices.length === 0
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            text: "No displays found yet…"
            font.family: Style.font.family
            font.italic: true
            font.pixelSize: Style.font.bodySmall
            color: Util.alpha(root.rainGreen, 0.5)
          }

          Repeater {
            model: root.devices

            delegate: Rectangle {
              id: row
              required property var modelData
              readonly property bool isSelected: root.selectedAddress === row.modelData.address

              width: cardColumn.width
              height: rowLabel.implicitHeight + Style.space(14)
              radius: Style.cornerRadius
              color: rowArea.containsMouse || row.isSelected
                ? Util.alpha(root.rainGreen, 0.10)
                : "transparent"

              Behavior on color { ColorAnimation { duration: 140 } }

              Rectangle {
                id: dot
                width: Style.space(8)
                height: Style.space(8)
                radius: width / 2
                anchors.left: parent.left
                anchors.leftMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                color: root.statusColor(row.modelData.status)

                Behavior on color { ColorAnimation { duration: 220 } }

                SequentialAnimation {
                  running: root.statusPulses(row.modelData.status)
                  loops: Animation.Infinite
                  NumberAnimation { target: dot; property: "opacity"; from: 1.0; to: 0.35; duration: 380; easing.type: Easing.InOutQuad }
                  NumberAnimation { target: dot; property: "opacity"; from: 0.35; to: 1.0; duration: 380; easing.type: Easing.InOutQuad }
                }
              }

              Text {
                id: rowLabel
                anchors.left: dot.right
                anchors.leftMargin: Style.space(8)
                anchors.right: rowStatus.left
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                text: row.modelData.name
                elide: Text.ElideRight
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                color: Color.foreground
              }

              Text {
                id: rowStatus
                anchors.right: parent.right
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                text: root.statusLabel(row.modelData.status)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                color: root.statusColor(row.modelData.status)
              }

              MouseArea {
                id: rowArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.clickDevice(row.modelData.address)
              }
            }
          }

          Rectangle {
            width: parent.width
            height: 1
            color: Util.alpha(root.rainGreen, 0.25)
          }

          Item {
            width: parent.width
            height: refreshButton.implicitHeight

            Button {
              id: refreshButton
              anchors.right: parent.right
              text: root.refreshing ? "Scanning…" : "Refresh"
              bordered: true
              foreground: root.rainGreen
              fontFamily: Style.font.family
              fontSize: Style.font.caption
              enabled: !root.refreshing
              onClicked: root.refresh()
            }
          }
        }
      }
    }
  }
}
