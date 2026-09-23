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
  property int activationNonce: 0
  property string glitchLine: ""
  // Most recent tool outcome, held through the assistant's spoken reply.
  // It resets as soon as speech ends and the assistant returns to listening.
  property string outcomeTone: ""

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
  // Failure reads as breakage, not just a color swap: after an "err"
  // outcome the visualizer splices in far more artifacts and they are
  // mostly crossed glyphs. The mock's ✖ (U+2716 HEAVY MULTIPLICATION X)
  // is deliberately absent: it carries the Unicode Emoji property, so
  // fontconfig serves it from Noto Color Emoji, which renders a grey
  // bitmap X that ignores the Text color entirely (confirmed on screen —
  // one grey glyph in an otherwise red row). ✕ and the box-drawing
  // crosses come from the UI font and take the color.
  readonly property string errChars: "╳╳╳✕✕╳╱╲"
  // Ticks only while the assistant is thinking — that state carries no
  // audio levels, so the row has nothing to redraw on without it.
  property int thinkTick: 0

  function pushLevel(v) {
    var next = levels.slice()
    var n = Math.max(0, Math.min(1, Number(v)))
    if (isNaN(n)) n = 0
    next.push(n)
    if (next.length > root.maxLevels) next = next.slice(next.length - root.maxLevels)
    levels = next
  }

  function visualizerLine() {
    if (root.convState === "thinking") {
      // No audio to meter while she thinks, so the row becomes the word
      // itself with a pulse travelling through it — the letters nearest
      // the head dissolve into glitch/braille and settle again behind it.
      var word = "THINKING"
      var head = ((root.thinkTick * 42) / 110) % (word.length + 5)
      var out = ""
      for (var k = 0; k < word.length; k++) {
        var d = Math.abs(head - k)
        if (d < 0.9)
          out += root.glitchChars.charAt(Math.floor(Math.random() * root.glitchChars.length))
        else if (d < 2)
          out += root.brailleChars[Math.max(0, 8 - Math.round(d * 3))]
        else
          out += word.charAt(k)
      }
      return out
    }
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
      // Match the richer server/phone visualizer: mostly braille texture,
      // some solid meter blocks, and rare glitch artifacts.
      var r = Math.random()
      var broken = root.outcomeTone === "err"
      if (r < (broken ? 0.34 : 0.04) && idx > 0) {
        var set = broken ? root.errChars : root.glitchChars
        s += set.charAt(Math.floor(Math.random() * set.length))
      } else if (r < 0.62) {
        s += root.brailleChars[idx]
      } else {
        s += root.blockChars.charAt(idx)
      }
    }
    return s
  }

  // Palette shared with the phone bridge (src/omarchy_ai/phone/static/
  // index.html): one signal color per conversation state, read the same
  // way on a phone across the room and on the desktop overlay.
  //   red   = not listening   amber = connecting/thinking
  //   green = mic live        blue  = assistant speaking
  // Names are kept from the original "rain" set so every call site below
  // (and the settings panel preview) keeps working.
  readonly property color rainGreen: "#3fb950"   // live / ok
  readonly property color rainCyan: "#4493f8"    // speaking / accent
  readonly property color rainLime: "#d29922"    // thinking / connecting
  readonly property color rainErr: "#f85149"     // not listening / error
  readonly property color inkMuted: "#9198a1"
  readonly property color inkFaint: "#6e7681"
  readonly property color hairline: "#2a313c"
  readonly property color surface: "#05080d"
  readonly property color rainDim: Util.alpha(inkFaint, 0.8)

  // The wave carries the same signal color as the dot, so a glance at the
  // bottom of the screen answers "is it hearing me?" without the card.
  function visualizerColor() {
    if (root.outcomeTone === "err") return root.rainErr
    if (root.outcomeTone === "ok") return root.rainGreen
    return root.stateColor(root.convState)
  }

  function stateColor(s) {
    if (s === "listening") return rainGreen
    if (s === "thinking") return rainLime
    if (s === "speaking") return rainCyan
    if (s === "connecting") return rainLime
    // Anything else means the assistant is not hearing you — red, the
    // same "not listening" signal the phone bridge shows.
    return rainErr
  }

  // Busy/processing states get a pulsing dot (a cheap stand-in for a
  // "glitch" cue); settled states hold solid.
  function statePulses(s) {
    return s === "connecting" || s === "thinking" || s === "idle"
  }

  function toneColor(tone) {
    if (tone === "ok") return rainGreen
    if (tone === "err") return rainErr
    if (tone === "state") return inkFaint
    return inkMuted // "call" and anything unrecognized
  }

  function reset() {
    lines = []
    levels = []
    convState = "connecting"
    outcomeTone = ""
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
    activationNonce += 1
    opened = true
  }

  function close() {
    opened = false
  }

  Timer {
    id: glitchTimer
    interval: 1500
    repeat: true
    running: root.opened
    onTriggered: {
      // Idle means the assistant is not hearing anything — the HUD should
      // look switched off, not busy.
      if (root.convState === "idle") { root.glitchLine = ""; return }
      var chars = "01/#%<>[]{}+=_*:;!?"
      var line = ""
      for (var i = 0; i < 18 + Math.floor(Math.random() * 18); i++)
        line += chars.charAt(Math.floor(Math.random() * chars.length))
      root.glitchLine = line
    }
  }

  Timer {
    id: thinkTimer
    interval: 42
    repeat: true
    running: root.opened && root.convState === "thinking"
    onTriggered: root.thinkTick += 1
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
          var speechFinished = root.convState === "speaking" && s !== "speaking"
          root.convState = s
          if (speechFinished) root.outcomeTone = ""
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
        if (kind === "tool_result") root.outcomeTone = tone === "err" ? "err" : "ok"
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

  // Co-pilot mode: is the user touching keyboard/mouse? ext-idle-notify,
  // the same IdleMonitor Omarchy's own idle service uses. Idle after 30s
  // without input means the assistant is the only operator, so her work is
  // shown on the user's workspace; while the user is active she works in
  // the background (src/omarchy_ai/execution/operator.py). Inhibitors
  // (video playback) are ignored: they say nothing about who operates.
  IdleMonitor {
    id: operatorIdle
    enabled: true
    timeout: 30
    respectInhibitors: false
  }
  // "Is the user operating right now?" A 30s window was far too eager: the
  // user touches the keyboard/mouse within 30s of talking to her all the
  // time, so demo work ran hidden in the background (2026-09-23 17:45).
  IdleMonitor {
    id: operatorBusy
    enabled: true
    timeout: 5
    respectInhibitors: false
  }

  IpcHandler {
    target: "watchdog"
    function operator(): string {
      if (!operatorBusy.isIdle) return "active"   // input in the last 5s
      return operatorIdle.isIdle ? "idle" : "recent"
    }
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

      // The noise floor. Under the wake-up artwork, which is under the
      // card — the card must stay readable through all of it.
      AmbientRain {
        anchors.fill: parent
        active: root.opened
        broken: root.outcomeTone === "err"
        idle: root.convState === "idle"
        accent: root.rainCyan
        errColor: root.rainErr
      }

      ActivationEffects {
        anchors.fill: parent
        active: root.opened
        accent: root.rainCyan
        activationNonce: root.activationNonce
      }

      // The drifting glyph row along the very bottom edge. root.glitchLine
      // has been computed by glitchTimer since the first version of this
      // plugin and never rendered anywhere — the property was dead. This
      // is the element the mock draws it as.
      Text {
        id: glitchRow
        visible: root.glitchLine.length > 0
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Style.space(14)
        width: Math.min(parent.width - Style.space(48), Style.space(980))
        textFormat: Text.PlainText
        text: root.glitchLine
        horizontalAlignment: Text.AlignHCenter
        elide: Text.ElideRight
        maximumLineCount: 1
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        font.letterSpacing: Style.font.caption * 0.24
        color: root.outcomeTone === "err" ? root.rainErr : root.rainCyan
        opacity: root.opened ? 0.32 : 0
        Behavior on opacity { NumberAnimation { duration: 180 } }
      }

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
        width: Math.min(parent.width - Style.space(48), Style.space(720))
        height: Style.space(118)
        textFormat: Text.PlainText
        // Keep the dependency explicit: QML cannot reliably infer that a
        // property read inside visualizerLine() should invalidate this
        // binding. Without this, levels changed over IPC but the rendered
        // ASCII row stayed at its initial idle value.
        text: { var samples = root.levels; var t = root.thinkTick; return root.visualizerLine() }
        font.family: Style.font.family
        font.pixelSize: Style.font.displayLarge
        // The word needs air between its letters; the meter does not.
        font.letterSpacing: Style.font.displayLarge * (root.convState === "thinking" ? 0.12 : 0.04)
        color: root.visualizerColor()
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignBottom
        elide: Text.ElideNone
        // Full strength only while there is something to say — a faint
        // ghost of the row the rest of the time.
        opacity: !root.opened ? 0
          : (root.convState === "speaking" || root.convState === "thinking") ? 1 : 0.28

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
        // Hairline card, not a glowing frame — matches the bridge, where
        // color is spent on state and everything else stays quiet.
        color: Util.alpha(root.surface, 0.92)
        radius: Style.cornerRadius
        borderSpec: Border.flat(root.hairline, 1)
        opacity: root.opened ? 1 : 0

        // Scanline sweep — a single translucent bar drifting down the card
        // on a loop. Cheap (one animated Rectangle, no shader) and paused
        // whenever the overlay is closed.
        Rectangle {
          id: scanline
          x: 0
          width: card.width
          height: Math.max(1, Style.space(2))
          color: Util.alpha(root.rainCyan, 0.06)
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
              width: Style.space(7)
              height: Style.space(7)
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
              color: root.inkFaint
            }
          }

          Rectangle {
            id: divider
            width: parent.width
            height: 1
            color: root.hairline
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
