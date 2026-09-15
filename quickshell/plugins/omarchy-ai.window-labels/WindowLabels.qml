import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Floating name-label badges pinned over candidate windows.
//
// Companion to omarchy.osd (same IPC pattern, same visual language — see
// $OMARCHY_PATH/shell/plugins/osd/Osd.qml), built for the Omarchy AI voice
// assistant's "which window do you mean?" moment: several windows are
// plausible matches, so instead of describing them in speech it drops a
// small chip on each one's actual on-screen rectangle so the user can just
// look and answer. Unlike the OSD there is no auto-hide timer — the caller
// (the assistant) owns the lifecycle explicitly: show while asking, hide
// once it has an answer.
Item {
  id: root

  property bool opened: false
  // Sanitized entries: [{wx, wy, ww, wh, label}, ...] in global (virtual
  // desktop) coordinates — the same space hyprctl clients -j reports "at"/
  // "size" in.
  property var labels: []

  function open(payloadJson) {
    var next = []
    try {
      var parsed = JSON.parse(payloadJson || "{}")
      // The payload is an object carrying a `windows` array, not a bare
      // JSON array — confirmed live that `qs ipc call` splats a top-level
      // JSON array argument into multiple positional CLI arguments instead
      // of passing it through as one string, which broke `show` for any
      // call with more than one window. A bare array is still accepted
      // here too, in case a future caller reaches the IPC handler some
      // other way that doesn't go through that CLI.
      var list = Array.isArray(parsed) ? parsed
        : (parsed && Array.isArray(parsed.windows) ? parsed.windows : [])
      if (Array.isArray(list)) {
        for (var i = 0; i < list.length; i++) {
          var e = list[i] || {}
          var x = Number(e.x)
          var y = Number(e.y)
          var w = Number(e.width)
          var h = Number(e.height)
          if (!isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h)) continue
          next.push({
            wx: x, wy: y, ww: Math.max(0, w), wh: Math.max(0, h),
            label: String(e.label || "")
          })
        }
      }
    } catch (err) {
      console.warn("windowLabels: could not parse payload:", err)
    }
    labels = next
    // Nothing parsed is the same as being asked to hide — don't leave a
    // blank layer-shell surface armed for no reason.
    opened = next.length > 0
  }

  function close() {
    opened = false
    labels = []
  }

  IpcHandler {
    target: "windowLabels"
    function show(payloadJson: string): string {
      root.open(payloadJson)
      return "ok"
    }
    function hide(): string { root.close(); return "ok" }
    function state(): string { return root.opened ? "open" : "closed" }
    function ping(): string { return "ok" }
  }

  // One PanelWindow per output (Variants on Quickshell.screens, same
  // pattern omarchy.notifications/omarchy.background use) — a window's
  // global x/y can land on any monitor, and each layer-shell surface only
  // covers its own screen's local coordinate space.
  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: panel
      required property var modelData
      screen: modelData
      visible: root.opened
      anchors { top: true; bottom: true; left: true; right: true }
      color: "transparent"
      WlrLayershell.namespace: "omarchy-window-labels"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      exclusionMode: ExclusionMode.Ignore
      // Visual-only, like the OSD: never steal clicks from the desktop
      // underneath the badges.
      mask: Region {}

      Repeater {
        model: root.labels

        delegate: Item {
          id: badge
          required property var modelData

          // Translate this window's global rect into this screen's local
          // (surface) coordinate space.
          readonly property real localX: badge.modelData.wx - panel.modelData.x
          readonly property real localY: badge.modelData.wy - panel.modelData.y
          readonly property real winW: badge.modelData.ww
          readonly property real winH: badge.modelData.wh
          // A window whose rect doesn't overlap this particular screen at
          // all shouldn't get a ghost badge drawn from leftover negative/
          // overflowing coordinates.
          readonly property bool onThisScreen:
            localX < panel.modelData.width && localX + winW > 0 &&
            localY < panel.modelData.height && localY + winH > 0

          visible: badge.onThisScreen
          readonly property int inset: Style.space(10)
          x: Math.max(0, Math.min(badge.localX + badge.inset, panel.modelData.width - chip.width))
          y: Math.max(0, Math.min(badge.localY + badge.inset, panel.modelData.height - chip.height))
          width: chip.width
          height: chip.height

          BorderSurface {
            id: chip
            width: chip.borderLeft + Style.space(10) + labelText.implicitWidth + Style.space(10) + chip.borderRight
            height: chip.borderTop + Style.space(6) + labelText.implicitHeight + Style.space(6) + chip.borderBottom
            color: Util.alpha(Color.background, 0.95)
            radius: Style.cornerRadius
            borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))

            Text {
              id: labelText
              anchors.centerIn: parent
              textFormat: Text.PlainText
              text: badge.modelData.label
              font.family: Style.font.family
              font.bold: true
              font.pixelSize: Style.font.title
              color: Color.popups.text
              elide: Text.ElideRight
            }
          }
        }
      }
    }
  }
}
