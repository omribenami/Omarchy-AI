import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Typed conversation with the assistant (src/omarchy_ai/voice/text_chat.py).
// A view only: lines go out through `omarchy-ai-settings chat-send`, and the
// daemon pushes the whole conversation back with setState after each change.
//
// text_chat_mode "keybinding": SUPER+CTRL+` shows and focuses it, Escape or
// the close mark hides it and ends the session. "always": it stays on screen,
// and Escape only gives the keyboard back.
Item {
  id: root
  readonly property string py: "@OMARCHY_AI_SETTINGS@"
  property bool pinned: false
  property bool open: false
  property bool active: false          // owns the keyboard right now
  property bool focusPrimed: false
  property var messages: []
  property string status: "idle"
  property string error: ""
  property bool sending: false
  readonly property bool shown: pinned || open
  signal focusRequested()   // the input lives in the per-screen window
  readonly property color accent: "#39ff88"
  readonly property color cyan: "#39e6ff"
  readonly property color red: "#ff5f5f"
  readonly property color surface: Color.background
  readonly property color ink: Color.foreground
  readonly property color faint: Util.alpha(Color.foreground, 0.58)

  function readState(payloadJson) {
    try {
      var p = JSON.parse(payloadJson || "{}")
      root.messages = Array.isArray(p.messages) ? p.messages : []
      root.status = p.status || "idle"
      root.error = p.error || ""
    } catch (e) {
      console.warn("assistantChat: invalid payload", e)
    }
  }

  function activate() {
    root.open = true
    root.focusPrimed = false
    root.active = true
    primeTimer.restart()
    Qt.callLater(root.focusRequested)
  }

  function release() {
    root.active = false
    if (!root.pinned) {
      root.open = false
      run(["chat-close"], null)
    }
  }

  function send(field) {
    var text = field.text.trim()
    if (!text || root.sending) return
    root.sending = true
    run(["chat-send"], function(result) {
      root.sending = false
      if (result && result.ok === false) { root.error = result.error || "Not sent"; root.status = "error" }
      else field.text = ""
    }, { OMARCHY_AI_CHAT_TEXT: text })
  }

  // One helper process at a time; a queue keeps order (same as the settings panel).
  property var _queue: []
  function run(args, cb, env) {
    root._queue.push({ argv: [root.py].concat(args), cb: cb, env: env || ({}) })
    root._next()
  }
  function _next() {
    if (helper.running || root._queue.length === 0) return
    var job = root._queue.shift()
    helper._cb = job.cb
    helper.environment = job.env
    helper.command = job.argv
    helper.running = true
  }
  Process {
    id: helper
    property var _cb: null
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var result = null
        try { result = text.trim() ? JSON.parse(text) : null } catch (e) { result = { ok: false, error: "bad helper output" } }
        var cb = helper._cb
        helper._cb = null
        if (cb) cb(result)
      }
    }
    onRunningChanged: if (!running) Qt.callLater(root._next)
  }

  Component.onCompleted: run(["get"], function(result) {
    if (result && result.fields) root.pinned = result.fields.text_chat_mode === "always"
  })

  // Exclusive for a moment on open (Hyprland focuses a mapped surface that
  // way), then OnDemand so a click elsewhere gives the keyboard back -- the
  // same prime Omarchy's own KeyboardPanel uses.
  Timer { id: primeTimer; interval: 150; onTriggered: root.focusPrimed = true }

  IpcHandler {
    target: "assistantChat"
    function toggle(): string {
      if (root.active && !root.pinned) root.release()
      else root.activate()
      return "ok"
    }
    function show(): string { root.activate(); return "ok" }
    function hide(): string { root.release(); return "ok" }
    function setPinned(value: string): string {
      root.pinned = value === "true"
      if (!root.pinned && !root.active) root.open = false
      return "ok"
    }
    function setState(payloadJson: string): string { root.readState(payloadJson); return "ok" }
    function ping(): string { return "ok" }
  }

  Variants {
    model: Quickshell.screens
    PanelWindow {
      id: panel
      required property var modelData
      screen: modelData
      // One HUD, on the focused monitor.
      visible: root.shown && (Hyprland.focusedMonitor ? Hyprland.focusedMonitor.name === modelData.name : true)
      anchors { bottom: true; right: true }
      implicitWidth: card.width + Style.space(40)
      implicitHeight: card.height + Style.space(40)
      color: "transparent"
      WlrLayershell.namespace: "omarchy-ai-chat-hud"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: root.active
        ? (root.focusPrimed ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.Exclusive)
        : WlrKeyboardFocus.None
      exclusionMode: ExclusionMode.Ignore

      Connections {
        target: root
        function onFocusRequested() { if (panel.visible) input.forceActiveFocus() }
      }

      BorderSurface {
        id: card
        anchors.centerIn: parent
        width: Style.space(460)
        height: header.height + (log.count > 0 ? Math.min(Style.space(360), log.contentHeight + Style.space(12)) : 0)
                + inputRow.height + Style.space(44)
        color: Util.alpha(root.surface, 0.94)
        radius: Style.cornerRadius
        borderSpec: Border.flat(Util.alpha(root.accent, root.active ? 0.8 : 0.5), 1)

        Rectangle {
          width: parent.width; height: 1; color: root.accent; opacity: 0.35
          SequentialAnimation on y {
            running: card.visible; loops: Animation.Infinite
            NumberAnimation { from: 0; to: card.height; duration: 3600; easing.type: Easing.Linear }
          }
        }

        Item {
          id: header
          anchors { left: parent.left; right: parent.right; top: parent.top; margins: Style.space(14) }
          height: Style.space(26)
          Rectangle {
            id: dot
            width: Style.space(7); height: width; radius: width / 2
            color: root.status === "error" ? root.red : (root.status === "idle" ? root.faint : root.accent)
            anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
            SequentialAnimation on opacity {
              running: card.visible && (root.status === "thinking" || root.status === "connecting"); loops: Animation.Infinite
              NumberAnimation { from: 1; to: 0.25; duration: 400 }
              NumberAnimation { from: 0.25; to: 1; duration: 400 }
            }
          }
          Text {
            anchors.left: parent.left; anchors.leftMargin: Style.space(16); anchors.verticalCenter: parent.verticalCenter
            text: "TEXT CHAT"; color: root.accent; font.family: Style.font.family
            font.pixelSize: Style.font.title; font.bold: true
          }
          Text {
            anchors.right: closeText.left; anchors.rightMargin: Style.space(12); anchors.verticalCenter: parent.verticalCenter
            text: "[" + ({ idle: "OFFLINE", connecting: "LINKING", thinking: "WORKING", ready: "LIVE", error: "ERROR" }[root.status] || root.status.toUpperCase()) + "]"
            color: root.status === "error" ? root.red : root.cyan; opacity: 0.8
            font.family: Style.font.family; font.pixelSize: Style.font.caption
          }
          Text {
            id: closeText
            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
            text: root.pinned ? "–" : "×"; color: closeMouse.containsMouse ? root.accent : root.faint
            font.family: Style.font.family; font.pixelSize: Style.font.title
            MouseArea {
              id: closeMouse; anchors.fill: parent; anchors.margins: -Style.space(8); hoverEnabled: true
              // Pinned: end the session and clear the log; otherwise hide.
              onClicked: root.pinned ? root.run(["chat-clear"], null) : root.release()
            }
          }
        }

        ListView {
          id: log
          anchors { left: parent.left; right: parent.right; top: header.bottom; margins: Style.space(14); topMargin: Style.space(10) }
          height: count > 0 ? Math.min(Style.space(360), contentHeight + Style.space(4)) : 0
          model: root.messages
          spacing: Style.space(8); clip: true; boundsBehavior: Flickable.StopAtBounds
          onCountChanged: Qt.callLater(positionViewAtEnd)
          onContentHeightChanged: Qt.callLater(positionViewAtEnd)
          delegate: Text {
            required property var modelData
            width: log.width
            text: (modelData.role === "user" ? "> " : (modelData.role === "error" ? "! " : "")) + String(modelData.text || "")
            color: modelData.role === "user" ? root.cyan : (modelData.role === "error" ? root.red : root.ink)
            font.family: Style.font.family; font.pixelSize: Style.font.body
            wrapMode: Text.Wrap; textFormat: Text.PlainText
          }
        }

        Row {
          id: inputRow
          anchors { left: parent.left; right: parent.right; bottom: parent.bottom; margins: Style.space(14) }
          spacing: Style.space(8)
          TextField {
            id: input
            width: parent.width
            foreground: root.ink; accent: root.accent
            placeholderText: root.sending ? "sending…" : "Type to Omarchy AI…  (Esc to " + (root.pinned ? "release" : "close") + ")"
            maximumLength: 8000
            onAccepted: root.send(input)
            // Clicked elsewhere: give the keyboard back, keep the HUD.
            onActiveFocusChanged: if (!activeFocus && root.active && root.focusPrimed) root.active = false
            Keys.onEscapePressed: root.release()
          }
        }
        MouseArea {
          // Inactive, the layer takes no keyboard at all; a click takes it back.
          anchors.fill: inputRow
          enabled: !root.active
          onClicked: root.activate()
        }
      }
    }
  }
}
