import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

// Red dollar signs popping over every screen, with the provider's name in
// the middle, when the Omarchy AI assistant runs out of credits or quota
// (src/omarchy_ai/core/quota.py). The spoken warning and a desktop
// notification go out at the same time; this is the part you can't miss.
//
// Same IPC pattern as omarchy-ai.window-labels: the payload is a JSON
// object ({"provider": "OpenAI", "message": "credits used up"}), never a
// bare array (`qs ipc call` splats arrays into several arguments).
// Visual-only and click-through; it closes itself after ~6.5s.
Item {
  id: root

  property bool opened: false
  property string provider: ""
  property string message: ""
  // One entry per dollar sign: fractions of the screen, delay, size, drift.
  property var bursts: []

  readonly property color red: "#ff2a3d"
  readonly property color deep: "#5a0008"

  function open(payloadJson) {
    var parsed = {}
    try {
      parsed = JSON.parse(payloadJson || "{}") || {}
    } catch (err) {
      console.warn("quotaAlert: could not parse payload:", err)
    }
    provider = String(parsed.provider || "AI provider")
    message = String(parsed.message || "credits used up")
    var next = []
    for (var i = 0; i < 36; i++) {
      next.push({
        fx: 0.04 + Math.random() * 0.92,
        fy: 0.12 + Math.random() * 0.8,
        delay: Math.floor(Math.random() * 1400),
        size: 44 + Math.floor(Math.random() * 90),
        drift: 70 + Math.random() * 120,
        tilt: -18 + Math.random() * 36
      })
    }
    // A new array recreates every delegate, so a repeated alert replays.
    bursts = next
    opened = true
    closer.restart()
  }

  function close() {
    opened = false
    bursts = []
  }

  Timer {
    id: closer
    interval: 6500
    onTriggered: root.close()
  }

  IpcHandler {
    target: "quotaAlert"
    function show(payloadJson: string): string { root.open(payloadJson); return "ok" }
    function hide(): string { root.close(); return "ok" }
    function state(): string { return root.opened ? "open" : "closed" }
    function ping(): string { return "ok" }
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
      WlrLayershell.namespace: "omarchy-ai-quota-alert"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      exclusionMode: ExclusionMode.Ignore
      mask: Region {}

      Repeater {
        model: root.bursts

        delegate: Text {
          id: sign
          required property var modelData
          text: "$"
          color: root.red
          style: Text.Outline
          styleColor: root.deep
          font.bold: true
          font.pixelSize: sign.modelData.size
          rotation: sign.modelData.tilt
          x: sign.modelData.fx * panel.width - width / 2
          y: sign.modelData.fy * panel.height - height / 2
          opacity: 0
          scale: 0.2
          transformOrigin: Item.Center

          SequentialAnimation {
            running: true
            PauseAnimation { duration: sign.modelData.delay }
            ParallelAnimation {
              NumberAnimation { target: sign; property: "scale"; to: 1.35; duration: 180; easing.type: Easing.OutBack }
              NumberAnimation { target: sign; property: "opacity"; to: 1; duration: 120 }
            }
            ParallelAnimation {
              NumberAnimation { target: sign; property: "scale"; to: 1.0; duration: 260 }
              NumberAnimation {
                target: sign; property: "y"; duration: 1700; easing.type: Easing.OutQuad
                to: sign.modelData.fy * panel.height - sign.height / 2 - sign.modelData.drift
              }
              SequentialAnimation {
                PauseAnimation { duration: 700 }
                NumberAnimation { target: sign; property: "opacity"; to: 0; duration: 1000; easing.type: Easing.InQuad }
              }
            }
          }
        }
      }

      Rectangle {
        id: caption
        anchors.centerIn: parent
        width: label.implicitWidth + 64
        height: label.implicitHeight + 36
        radius: 10
        color: Qt.rgba(0.08, 0, 0.01, 0.88)
        border.color: root.red
        border.width: 3
        opacity: 0
        scale: 0.6
        visible: root.opened

        Text {
          id: label
          anchors.centerIn: parent
          textFormat: Text.PlainText
          text: "$  " + root.provider + " " + root.message + "  $"
          color: root.red
          font.bold: true
          font.pixelSize: 38
        }

        SequentialAnimation {
          running: root.opened
          ParallelAnimation {
            NumberAnimation { target: caption; property: "opacity"; to: 1; duration: 160 }
            NumberAnimation { target: caption; property: "scale"; to: 1; duration: 260; easing.type: Easing.OutBack }
          }
          PauseAnimation { duration: 5000 }
          NumberAnimation { target: caption; property: "opacity"; to: 0; duration: 900 }
        }
      }
    }
  }
}
