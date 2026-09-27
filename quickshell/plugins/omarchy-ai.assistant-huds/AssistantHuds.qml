import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
  id: root
  property bool tasksOpen: false
  property bool routinesOpen: false
  property var tasks: []
  property var routines: []
  // Tasks paused for the user's approval (assistant_huds.approval_items):
  // the floating envelope shows while any is waiting.
  property var approvals: []
  property bool approvalsOpen: false
  property string approvalError: ""
  property var answering: ({})
  readonly property string py: "@OMARCHY_AI_SETTINGS@"
  readonly property color green: "#39ff88"
  readonly property color cyan: "#39e6ff"
  // The approval envelope and card use the watchdog's palette (Watchdog.qml,
  // shared with the phone bridge): a quiet hairline card, the ghostly blue
  // of its rain, and color spent only on state.
  readonly property color wdBlue: "#4493f8"
  readonly property color wdGreen: "#3fb950"
  readonly property color wdAmber: "#d29922"
  readonly property color wdRed: "#f85149"
  readonly property color wdInkMuted: "#9198a1"
  readonly property color wdInkFaint: "#6e7681"
  readonly property color wdHairline: "#2a313c"
  readonly property color wdSurface: "#05080d"
  readonly property color surface: Color.background
  readonly property color ink: Color.foreground
  readonly property color faint: Util.alpha(Color.foreground, 0.58)

  function readItems(payloadJson) {
    try {
      var p = JSON.parse(payloadJson || "{}")
      return p && Array.isArray(p.items) ? p.items : []
    } catch (e) {
      console.warn("assistantHuds: invalid payload", e)
      return []
    }
  }

  IpcHandler {
    target: "assistantHuds"
    function showTasks(payloadJson: string): string {
      root.tasks = root.readItems(payloadJson); root.tasksOpen = true; return "ok"
    }
    function updateTasks(payloadJson: string): string {
      root.tasks = root.readItems(payloadJson); return "ok"
    }
    function hideTasks(): string { root.tasksOpen = false; return "ok" }
    function showRoutines(payloadJson: string): string {
      root.routines = root.readItems(payloadJson); root.routinesOpen = true; return "ok"
    }
    function updateRoutines(payloadJson: string): string {
      root.routines = root.readItems(payloadJson); return "ok"
    }
    function hideRoutines(): string { root.routinesOpen = false; return "ok" }
    function updateApprovals(payloadJson: string): string {
      root.setApprovals(root.readItems(payloadJson)); return "ok"
    }
    function showApprovals(): string { root.approvalsOpen = root.approvals.length > 0; return "ok" }
    function hideAll(): string { root.tasksOpen = false; root.routinesOpen = false; root.approvalsOpen = false; return "ok" }
    function ping(): string { return "ok" }
  }

  function setApprovals(items) {
    root.approvals = items
    if (items.length === 0) root.approvalsOpen = false
    root.approvalError = ""
  }

  // Approve/Deny go to the daemon (settings CLI -> control socket), which
  // resumes the task in its own process. A click here is a real human
  // approval, like the desktop notification's buttons.
  function respond(taskId, decision) {
    var busy = Object.assign({}, root.answering); busy[taskId] = true; root.answering = busy
    root.run(["approval-respond", decision, taskId], function(result) {
      var done = Object.assign({}, root.answering); delete done[taskId]; root.answering = done
      if (!result || result.ok === false) root.approvalError = (result && (result.error || result.message)) || "No answer from the assistant"
    })
  }

  property var _queue: []
  function run(args, cb) {
    root._queue.push({ argv: [root.py].concat(args), cb: cb })
    root._next()
  }
  function _next() {
    if (helper.running || root._queue.length === 0) return
    var job = root._queue.shift()
    helper._cb = job.cb
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
  // Approvals already waiting when the shell (re)loads this plugin.
  Component.onCompleted: run(["approvals"], function(result) {
    if (result && Array.isArray(result.items)) root.setApprovals(result.items)
  })

  // A neon envelope drawn on a canvas: faint "ghost" fill, glowing strokes.
  component Envelope: Item {
    id: env
    property color accent: root.wdBlue
    property bool hot: false
    implicitWidth: Style.space(58); implicitHeight: Style.space(42)
    onHotChanged: art.requestPaint()
    Canvas {
      id: art
      anchors.fill: parent
      onPaint: {
        var ctx = getContext("2d")
        var w = width, h = height, m = 7
        ctx.reset()
        ctx.lineJoin = "round"; ctx.lineCap = "round"
        ctx.shadowColor = env.accent; ctx.shadowBlur = env.hot ? 12 : 7
        ctx.fillStyle = Qt.rgba(root.wdSurface.r, root.wdSurface.g, root.wdSurface.b, 0.55)
        ctx.strokeStyle = Qt.rgba(env.accent.r, env.accent.g, env.accent.b, env.hot ? 1 : 0.85)
        ctx.lineWidth = 1.4
        ctx.beginPath(); ctx.rect(m, m, w - 2 * m, h - 2 * m); ctx.fill(); ctx.stroke()
        ctx.beginPath(); ctx.moveTo(m, m); ctx.lineTo(w / 2, h * 0.58); ctx.lineTo(w - m, m); ctx.stroke()
        ctx.globalAlpha = 0.45
        ctx.beginPath(); ctx.moveTo(m, h - m); ctx.lineTo(w * 0.4, h * 0.5)
        ctx.moveTo(w - m, h - m); ctx.lineTo(w * 0.6, h * 0.5); ctx.stroke()
      }
    }
  }

  component ApprovalCard: BorderSurface {
    id: acard
    readonly property color accent: root.wdBlue
    width: Style.space(430)
    // header + divider gap + rows + bottom margin (+ the error line when shown)
    height: Math.min(Style.space(520), aheader.height + alist.contentHeight + Style.space(64) + errorText.height)
    // Hairline card like the watchdog's: color goes on state, not the frame.
    color: Util.alpha(root.wdSurface, 0.92)
    radius: Style.cornerRadius
    borderSpec: Border.flat(root.wdHairline, 1)

    Rectangle {
      width: parent.width; height: Math.max(1, Style.space(2)); color: Util.alpha(root.wdBlue, 0.06)
      SequentialAnimation on y {
        running: acard.visible; loops: Animation.Infinite
        NumberAnimation { from: 0; to: acard.height; duration: 3200; easing.type: Easing.Linear }
        PauseAnimation { duration: 200 }
      }
    }
    Item {
      id: aheader
      anchors { left: parent.left; right: parent.right; top: parent.top; margins: Style.space(14) }
      height: Style.space(30)
      Rectangle {
        id: adot
        width: Style.space(7); height: width; radius: width / 2; color: root.wdAmber
        anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
        SequentialAnimation on opacity {
          running: acard.visible; loops: Animation.Infinite
          NumberAnimation { from: 1; to: 0.35; duration: 420; easing.type: Easing.InOutQuad }
          NumberAnimation { from: 0.35; to: 1; duration: 420; easing.type: Easing.InOutQuad }
        }
      }
      Text {
        anchors.left: adot.right; anchors.leftMargin: Style.space(8); anchors.verticalCenter: parent.verticalCenter
        text: "AWAITING APPROVAL"; color: root.wdAmber; font.family: Style.font.family
        font.pixelSize: Style.font.title; font.bold: true
      }
      Text {
        id: aclose
        anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
        text: "×"; color: acloseMouse.containsMouse ? root.wdInkMuted : root.wdInkFaint
        font.family: Style.font.family; font.pixelSize: Style.font.title
        MouseArea { id: acloseMouse; anchors.fill: parent; anchors.margins: -Style.space(8); hoverEnabled: true; onClicked: root.approvalsOpen = false }
      }
      Text {
        anchors.right: aclose.left; anchors.rightMargin: Style.space(12); anchors.verticalCenter: parent.verticalCenter
        text: "APPROVALS"; color: root.wdInkFaint; font.family: Style.font.family
        font.pixelSize: Style.font.caption; font.bold: true
      }
    }
    Rectangle {
      anchors { left: parent.left; right: parent.right; top: aheader.bottom; leftMargin: Style.space(14); rightMargin: Style.space(14) }
      height: 1; color: root.wdHairline
    }
    ListView {
      id: alist
      anchors { left: parent.left; right: parent.right; top: aheader.bottom; bottom: errorText.top; margins: Style.space(14); topMargin: Style.space(18) }
      model: root.approvals
      spacing: Style.space(14); clip: true; boundsBehavior: Flickable.StopAtBounds
      delegate: Column {
        id: row
        required property var modelData
        readonly property bool busy: !!root.answering[modelData.id]
        width: alist.width; spacing: Style.space(4)
        Text {
          width: parent.width; text: String(modelData.title || modelData.id)
          color: root.wdInkMuted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall
          elide: Text.ElideRight; maximumLineCount: 1
        }
        Text {
          width: parent.width; text: "[" + String(modelData.risk || "APPROVAL").toUpperCase() + "] " + String(modelData.subject || "")
          color: root.wdBlue; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WrapAnywhere; maximumLineCount: 3; elide: Text.ElideRight
        }
        Text {
          width: parent.width; visible: !!modelData.reasons; text: String(modelData.reasons || "")
          color: root.wdInkFaint; font.family: Style.font.family; font.pixelSize: Style.font.caption
          font.italic: true; elide: Text.ElideRight; maximumLineCount: 1
        }
        Row {
          spacing: Style.space(10); topPadding: Style.space(4)
          Repeater {
            model: [{ label: "APPROVE", decision: "approve", color: root.wdGreen }, { label: "DENY", decision: "deny", color: root.wdRed }]
            delegate: Rectangle {
              required property var modelData
              width: Style.space(96); height: Style.space(28); radius: Style.cornerRadius
              color: Util.alpha(modelData.color, bmouse.containsMouse ? 0.14 : 0.04)
              border.color: bmouse.containsMouse ? Util.alpha(modelData.color, 0.7) : root.wdHairline; border.width: 1
              opacity: row.busy ? 0.4 : 1
              Text {
                anchors.centerIn: parent; text: row.busy ? "…" : modelData.label; color: modelData.color
                font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true
              }
              MouseArea {
                id: bmouse; anchors.fill: parent; hoverEnabled: true; enabled: !row.busy
                onClicked: root.respond(row.modelData.id, parent.modelData.decision)
              }
            }
          }
        }
      }
    }
    Text {
      id: errorText
      anchors { left: parent.left; right: parent.right; bottom: parent.bottom; margins: Style.space(14) }
      visible: root.approvalError !== ""; height: visible ? implicitHeight : 0
      text: root.approvalError; color: root.wdRed
      font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.Wrap
    }
  }

  component HudCard: BorderSurface {
    id: card
    required property string heading
    required property string emptyText
    required property color accent
    required property var entries
    signal closeRequested()
    width: Style.space(430)
    height: Math.min(Style.space(430), Math.max(Style.space(150), header.height + list.contentHeight + Style.space(46)))
    color: Util.alpha(root.surface, 0.94)
    radius: Style.cornerRadius
    borderSpec: Border.flat(Util.alpha(card.accent, 0.5), 1)

    Rectangle {
      width: parent.width; height: 1; color: card.accent; opacity: 0.35
      SequentialAnimation on y {
        running: card.visible; loops: Animation.Infinite
        NumberAnimation { from: 0; to: card.height; duration: 3600; easing.type: Easing.Linear }
      }
    }

    Item {
      id: header
      anchors { left: parent.left; right: parent.right; top: parent.top; margins: Style.space(14) }
      height: Style.space(30)
      Rectangle {
        width: Style.space(7); height: width; radius: width / 2; color: card.accent
        anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
        SequentialAnimation on opacity {
          running: card.visible; loops: Animation.Infinite
          NumberAnimation { from: 1; to: 0.35; duration: 500 }
          NumberAnimation { from: 0.35; to: 1; duration: 500 }
        }
      }
      Text {
        anchors.left: parent.left; anchors.leftMargin: Style.space(16); anchors.verticalCenter: parent.verticalCenter
        text: card.heading; color: card.accent; font.family: Style.font.family
        font.pixelSize: Style.font.title; font.bold: true
      }
      Text {
        id: closeText
        anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
        text: "×"; color: closeMouse.containsMouse ? card.accent : root.faint
        font.family: Style.font.family; font.pixelSize: Style.font.title
        MouseArea { id: closeMouse; anchors.fill: parent; anchors.margins: -Style.space(8); hoverEnabled: true; onClicked: card.closeRequested() }
      }
    }

    Rectangle {
      anchors { left: parent.left; right: parent.right; top: header.bottom; leftMargin: Style.space(14); rightMargin: Style.space(14) }
      height: 1; color: Util.alpha(card.accent, 0.3)
    }

    ListView {
      id: list
      anchors { left: parent.left; right: parent.right; top: header.bottom; bottom: parent.bottom; margins: Style.space(14); topMargin: Style.space(18) }
      model: card.entries
      spacing: Style.space(9); clip: true; boundsBehavior: Flickable.StopAtBounds
      delegate: Column {
        required property var modelData
        width: list.width; spacing: Style.space(2)
        Text {
          width: parent.width; text: "> " + String(modelData.title || modelData.id || "Untitled")
          color: root.ink; font.family: Style.font.family; font.pixelSize: Style.font.body
          elide: Text.ElideRight; maximumLineCount: 1
        }
        Text {
          width: parent.width
          text: "[" + String(modelData.status || "active").toUpperCase() + "] " + String(modelData.detail || modelData.next || "")
          color: card.accent; opacity: 0.72; font.family: Style.font.family; font.pixelSize: Style.font.caption
          elide: Text.ElideRight; maximumLineCount: 1
        }
      }
      Text {
        anchors.centerIn: parent; visible: card.entries.length === 0
        text: card.emptyText; color: root.faint; font.family: Style.font.family; font.pixelSize: Style.font.body
      }
    }
  }

  Variants {
    model: Quickshell.screens
    PanelWindow {
      id: panel
      required property var modelData
      screen: modelData
      visible: root.tasksOpen || root.routinesOpen || root.approvalsOpen
      anchors { top: true; bottom: true; left: true; right: true }
      color: "transparent"
      WlrLayershell.namespace: "omarchy-ai-assistant-huds"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      exclusionMode: ExclusionMode.Ignore
      mask: Region { item: cards }

      Column {
        id: cards
        anchors.right: parent.right; anchors.top: parent.top
        anchors.rightMargin: Style.space(20)
        // Below the envelope while one is floating there.
        anchors.topMargin: Style.space(80) + (root.approvals.length > 0 ? Style.space(58) : 0)
        spacing: Style.space(14)
        ApprovalCard { visible: root.approvalsOpen }
        HudCard {
          visible: root.tasksOpen; heading: "CURRENT TASKS"; accent: root.green
          emptyText: "NO CURRENT TASKS"; entries: root.tasks
          onCloseRequested: root.tasksOpen = false
        }
        HudCard {
          visible: root.routinesOpen; heading: "ROUTINES / CRON"; accent: root.cyan
          emptyText: "NO ACTIVE ROUTINES"; entries: root.routines
          onCloseRequested: root.routinesOpen = false
        }
      }
    }
  }

  Variants {
    model: Quickshell.screens
    PanelWindow {
      required property var modelData
      screen: modelData
      visible: root.approvals.length > 0
      anchors { top: true; right: true }
      margins { top: Style.space(72); right: Style.space(20) }
      implicitWidth: Style.space(84); implicitHeight: Style.space(64)
      color: "transparent"
      WlrLayershell.namespace: "omarchy-ai-approval-envelope"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      exclusionMode: ExclusionMode.Ignore

      Item {
        id: floater
        width: envelope.width; height: envelope.height
        anchors.horizontalCenter: parent.horizontalCenter
        // Drifts up and down, and breathes, like the rest of the HUD.
        SequentialAnimation on y {
          running: parent.visible; loops: Animation.Infinite
          NumberAnimation { from: Style.space(10); to: Style.space(4); duration: 1400; easing.type: Easing.InOutSine }
          NumberAnimation { from: Style.space(4); to: Style.space(10); duration: 1400; easing.type: Easing.InOutSine }
        }
        SequentialAnimation on opacity {
          running: parent.visible; loops: Animation.Infinite
          NumberAnimation { from: 0.9; to: 0.45; duration: 1400; easing.type: Easing.InOutSine }
          NumberAnimation { from: 0.45; to: 0.9; duration: 1400; easing.type: Easing.InOutSine }
        }
        Envelope { id: envelope; hot: envMouse.containsMouse || root.approvalsOpen }
        Rectangle {
          visible: root.approvals.length > 1
          width: Style.space(18); height: width; radius: width / 2
          anchors { right: envelope.right; top: envelope.top; rightMargin: -Style.space(4); topMargin: -Style.space(4) }
          color: Util.alpha(root.wdSurface, 0.92); border.color: root.wdAmber; border.width: 1
          Text {
            anchors.centerIn: parent; text: String(root.approvals.length); color: root.wdAmber
            font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true
          }
        }
        MouseArea {
          id: envMouse; anchors.fill: envelope; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
          onClicked: root.approvalsOpen = !root.approvalsOpen
        }
      }
    }
  }
}
