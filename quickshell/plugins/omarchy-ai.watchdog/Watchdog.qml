import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// "Watch Dogs" hacking-HUD overlay for the Omarchy AI voice assistant.
//
// Companion to omarchy.osd and omarchy-ai.window-labels (same IPC pattern —
// see $OMARCHY_PATH/shell/plugins/osd/Osd.qml and this project's own
// ~/.config/omarchy/plugins/omarchy-ai.window-labels/WindowLabels.qml).
// Unlike either of those (one-shot show/auto-hide, or static show/hide),
// this plugin stays open for an entire conversation and receives a stream
// of `event` calls while it's up — a live conversation-state indicator plus
// a scrolling feed of the assistant's *real* tool calls, styled as
// cascading terminal text. It is driven entirely by
// src/omarchy_ai/voice/live.py's own lifecycle (session connect, tool
// calls/results, state transitions, hangup) via src/omarchy_ai/voice/
// watchdog.py — never by the model directly.
Item {
  id: root

  property bool opened: false
  // Named convState, not "state" — Item already reserves a "state"
  // property for QtQuick's States/Transitions machinery, and redeclaring
  // it here would collide with that (confirmed: shadowing a base Item
  // property by name in QML errors, it doesn't override cleanly the way
  // it would in C++).
  property string convState: "connecting"
  // Feed entries: [{text, tone}], tone one of "call" | "ok" | "err" |
  // "state". Newest last — the ListView renders bottom-anchored so new
  // lines cascade in at the bottom and old ones scroll up, like a terminal.
  property var lines: []
  readonly property int maxLines: 60

  // "feed" (original tool-call/state text), "visualizer" (ASCII/unicode
  // amplitude bars driven by level() below), or "both" — selected via the
  // omarchy-ai.settings panel, sent as part of the start() payload each
  // session (src/omarchy_ai/voice/watchdog.py's start(display_mode)).
  property string displayMode: "feed"

  // Rolling amplitude buffer for the ASCII visualizer, 0..1 per sample —
  // a scrolling-waveform look with a short window rather than a single
  // number. Only accumulated while convState === "speaking" and cleared
  // whenever it isn't, so a conversation that goes quiet doesn't leave a
  // stale frozen frame on screen (per the original ask).
  property var levels: []
  readonly property int maxLevels: 10
  // Two density ramps, both indexed 0-8 by amplitude. Blocks give the
  // clean equalizer-bar read; braille gives the "dotted texture" the
  // glitch look is built from (dot counts 0..8, so density tracks
  // amplitude the same way bar height does). Mixing the two per column,
  // reshuffled on every level update (~10Hz), is what reads as glitchy
  // rather than as a tidy audio meter.
  readonly property string blockChars: " ▁▂▃▄▅▆▇█"
  readonly property var brailleChars: ["⠀", "⠁", "⠃", "⠇", "⡇", "⡏", "⡟", "⡿", "⣿"]
  // Rare full-column artifacts spliced in at low probability — the
  // "dropped frame" flicker of a glitching readout.
  readonly property string glitchChars: "▓▒░╳┃╎"

  function pushLevel(v) {
    var next = levels.slice()
    var n = Math.max(0, Math.min(1, Number(v)))
    if (isNaN(n)) n = 0
    next.push(n)
    if (next.length > root.maxLevels) next = next.slice(next.length - root.maxLevels)
    levels = next
  }

  function visualizerLine() {
    if (root.levels.length === 0) {
      // Faint idle baseline rather than a blank row, so the visualizer
      // reads as "waiting" instead of looking broken/empty. Sparse and
      // irregular, not an even dotted rule.
      var idle = ""
      for (var j = 0; j < root.maxLevels; j++)
        idle += (Math.random() < 0.22 ? "⠂" : (Math.random() < 0.12 ? "⠁" : " "))
      return idle
    }
    var s = ""
    for (var i = 0; i < root.levels.length; i++) {
      var idx = Math.max(0, Math.min(8, Math.round(root.levels[i] * 8)))
      var r = Math.random()
      if (r < 0.04 && idx > 0) {
        // Artifact column: ignore the real amplitude entirely for one
        // frame at one position.
        s += root.glitchChars.charAt(Math.floor(Math.random() * root.glitchChars.length))
      } else if (r < 0.62) {
        s += root.brailleChars[idx]
      } else {
        s += root.blockChars.charAt(idx)
      }
    }
    return s
  }

  readonly property color rainGreen: "#39ff88"
  readonly property color rainCyan: "#39e6ff"
  readonly property color rainLime: "#a6ff4d"
  readonly property color rainErr: "#ff5f5f"
  readonly property color rainDim: Util.alpha(rainGreen, 0.55)

  function stateColor(s) {
    if (s === "listening") return rainCyan
    if (s === "thinking") return rainLime
    if (s === "speaking") return rainGreen
    if (s === "connecting") return rainDim
    return rainDim
  }

  // Busy/processing states get a pulsing dot (a cheap stand-in for a
  // "glitch" cue); settled states hold solid.
  function statePulses(s) {
    return s === "connecting" || s === "thinking"
  }

  function toneColor(tone) {
    if (tone === "ok") return rainCyan
    if (tone === "err") return rainErr
    if (tone === "state") return Util.alpha(rainCyan, 0.6)
    return rainGreen // "call" and anything unrecognized
  }

  function reset() {
    lines = []
    levels = []
    convState = "connecting"
  }

  function open(payloadJson) {
    // The start payload now optionally carries displayMode (see
    // watchdog.py's start(display_mode)) — default to "feed" for any
    // caller that omits it (keeps this backward compatible with the CLI
    // shape osd/windowLabels use, and with a bare `start '{}'` call).
    reset()
    var mode = "feed"
    try {
      var p = JSON.parse(payloadJson || "{}")
      if (p.displayMode === "feed" || p.displayMode === "visualizer" || p.displayMode === "both")
        mode = p.displayMode
    } catch (err) {
      // Ignore — fall back to "feed".
    }
    displayMode = mode
    opened = true
  }

  function close() {
    opened = false
  }

  function pushLine(text, tone) {
    var next = lines.slice()
    next.push({ text: String(text || ""), tone: String(tone || "call") })
    if (next.length > root.maxLines) next = next.slice(next.length - root.maxLines)
    lines = next
    // No explicit feedList.positionViewAtEnd() here — feedList lives
    // inside the per-screen Variants delegate below, a different id scope
    // than this function's (root's); calling it from here threw a real,
    // pre-existing ReferenceError every time (harmless in practice, since
    // `lines` is already reassigned above and feedList's own
    // `onCountChanged: positionViewAtEnd()` handles the scroll — but it
    // was spamming the journal on every single feed line). Confirmed via
    // journalctl this fired even in plain "feed" mode with a freshly
    // restarted shell, i.e. it was never actually scoped correctly, not
    // a regression from the visualizer work.
  }

  function handleEvent(payloadJson) {
    try {
      var p = JSON.parse(payloadJson || "{}")
      var kind = p.kind || ""
      if (kind === "state") {
        var s = String(p.state || "").trim()
        if (s.length > 0) {
          root.convState = s
          // Decay/clear the visualizer the instant the conversation isn't
          // "speaking" anymore, so switching to listening/thinking (or a
          // fresh turn) never shows a stale frozen waveform.
          if (s !== "speaking") root.levels = []
        }
        if (p.text) pushLine(p.text, "state")
      } else if (kind === "level") {
        // Real-time output amplitude from live.py's _play_remote_audio —
        // only meaningful while the assistant is actually speaking; drop
        // stray/late samples that arrive just after a state flip instead
        // of letting them repopulate a buffer we just cleared above.
        if (root.convState === "speaking") pushLevel(p.level)
      } else if (kind === "tool_call" || kind === "tool_result") {
        var tone = kind === "tool_result" ? (p.tone || "ok") : "call"
        if (p.text) pushLine(p.text, tone)
      } else if (p.text) {
        // Unknown kind — still show it rather than silently drop real
        // activity the caller wanted visible.
        pushLine(p.text, p.tone || "call")
      }
    } catch (err) {
      console.warn("watchdog: could not parse event payload:", err)
    }
  }

  IpcHandler {
    target: "watchdog"
    function start(payloadJson: string): string {
      root.open(payloadJson)
      return "ok"
    }
    function event(payloadJson: string): string {
      root.handleEvent(payloadJson)
      return "ok"
    }
    function stop(): string { root.close(); return "ok" }
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
      WlrLayershell.namespace: "omarchy-ai-watchdog"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      exclusionMode: ExclusionMode.Ignore
      // Visual-only, like the OSD and window labels: never steal input.
      mask: Region {}

      readonly property int cardWidth: Style.space(440)
      readonly property int cardHeight: Style.space(268)
      readonly property int cardMargin: Style.space(20)

      // The ASCII/unicode amplitude visualizer lives OUT here, a sibling of
      // the card rather than a row inside it: frameless (no surface, border
      // or background of its own) and centered along the bottom edge, per
      // the user's own description of what they wanted — "outside of the
      // watchdog box, frameless in the center bottom area of the screen".
      // One Text element, one string rebuild per level() event (~10Hz), no
      // per-column child items.
      Text {
        id: visualizerRow
        visible: root.displayMode === "visualizer" || root.displayMode === "both"
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Style.space(48)
        textFormat: Text.PlainText
        text: root.visualizerLine()
        font.family: Style.font.family
        font.pixelSize: Style.font.displayLarge
        color: root.convState === "speaking" ? root.rainCyan : Util.alpha(root.rainCyan, 0.30)
        elide: Text.ElideNone
        opacity: root.opened ? 1 : 0

        Behavior on color { ColorAnimation { duration: 200 } }
        Behavior on opacity { NumberAnimation { duration: 180 } }
      }

      BorderSurface {
        id: card
        // In visualizer-only mode the card is the thing the user opted out
        // of — hide it entirely rather than leaving an empty header/divider
        // shell floating in the corner.
        visible: root.displayMode !== "visualizer"
        width: panel.cardWidth
        height: panel.cardHeight
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.rightMargin: panel.cardMargin
        anchors.bottomMargin: panel.cardMargin
        color: Util.alpha(Color.background, 0.88)
        radius: Style.cornerRadius
        borderSpec: Border.flat(Util.alpha(root.rainGreen, 0.55), Math.max(1, Style.space(2)))
        opacity: root.opened ? 1 : 0

        // Scanline sweep — a single translucent bar drifting down the card
        // on a loop. Cheap (one animated Rectangle, no shader) and paused
        // whenever the overlay is closed.
        Rectangle {
          id: scanline
          x: 0
          width: card.width
          height: Math.max(1, Style.space(2))
          color: Util.alpha(root.rainGreen, 0.10)
          y: 0
          SequentialAnimation on y {
            running: root.opened
            loops: Animation.Infinite
            NumberAnimation { from: 0; to: card.height; duration: 3200; easing.type: Easing.Linear }
            PauseAnimation { duration: 200 }
          }
        }

        Column {
          anchors.fill: parent
          anchors.margins: card.borderTop + Style.space(14)
          spacing: Style.space(8)

          // Header: pulsing state dot + state label + a small fixed title
          // so the overlay reads as an instrument, not a stray tooltip.
          // Plain anchors rather than Row/Layout — Row has no fillWidth
          // concept and this avoids pulling in QtQuick.Layouts for one
          // right-anchored label.
          Item {
            id: headerRow
            width: parent.width
            height: Math.max(stateDot.height, stateLabel.implicitHeight, watchdogLabel.implicitHeight)

            Rectangle {
              id: stateDot
              width: Style.space(9)
              height: Style.space(9)
              radius: width / 2
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              color: root.stateColor(root.convState)
              opacity: 1.0

              SequentialAnimation {
                running: root.opened && root.statePulses(root.convState)
                loops: Animation.Infinite
                onRunningChanged: if (!running) stateDot.opacity = 1.0
                NumberAnimation { target: stateDot; property: "opacity"; from: 1.0; to: 0.35; duration: 420; easing.type: Easing.InOutQuad }
                NumberAnimation { target: stateDot; property: "opacity"; from: 0.35; to: 1.0; duration: 420; easing.type: Easing.InOutQuad }
              }
            }

            Text {
              id: stateLabel
              anchors.left: stateDot.right
              anchors.leftMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: root.convState.toUpperCase()
              font.family: Style.font.family
              font.bold: true
              font.pixelSize: Style.font.title
              color: root.stateColor(root.convState)
            }

            Text {
              id: watchdogLabel
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: "WATCHDOG"
              font.family: Style.font.family
              font.bold: true
              font.pixelSize: Style.font.caption
              color: Util.alpha(root.rainGreen, 0.4)
            }
          }

          Rectangle {
            id: divider
            width: parent.width
            height: 1
            color: Util.alpha(root.rainGreen, 0.25)
          }

          ListView {
            id: feedList
            visible: root.displayMode !== "visualizer"
            width: parent.width
            height: visible
              ? parent.height - headerRow.height - divider.height - parent.spacing * 2
              : 0
            clip: true
            model: root.lines
            spacing: Style.space(2)
            boundsBehavior: Flickable.StopAtBounds
            interactive: false

            add: Transition {
              NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 180 }
            }

            delegate: Text {
              required property var modelData
              width: feedList.width
              textFormat: Text.PlainText
              text: modelData.text
              elide: Text.ElideRight
              maximumLineCount: 1
              font.family: Style.font.family
              font.italic: modelData.tone === "state"
              font.pixelSize: Style.font.bodySmall
              color: root.toneColor(modelData.tone)
            }

            onCountChanged: positionViewAtEnd()
          }
        }
      }
    }
  }
}
