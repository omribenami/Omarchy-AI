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
  readonly property color green: "#39ff88"
  readonly property color cyan: "#39e6ff"
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
    function hideAll(): string { root.tasksOpen = false; root.routinesOpen = false; return "ok" }
    function ping(): string { return "ok" }
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
      visible: root.tasksOpen || root.routinesOpen
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
        anchors.rightMargin: Style.space(20); anchors.topMargin: Style.space(80)
        spacing: Style.space(14)
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
}
