import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// A separate bar icon/panel for the MyApi (myapiai.com) integration —
// deliberately not folded into omarchy-ai.settings/Panel.qml: that panel
// only carries the on/off switch for this one (see its "CONNECT SERVICES
// TO OMARCHY AI" section), everything else — connecting, disconnecting,
// live usage — lives here, its own icon, the same way the phone bridge and
// the daemon's live-status dot already sit on the main icon rather than in
// a menu. Hidden entirely unless myapi_enabled is on (see visible below).
//
// Same "shell out to src/omarchy_ai/cli/settings.py via a serialized
// Process queue" backend as the main settings panel — no separate service,
// same JSON-line-per-call contract. The usage table's visual language
// (a row whose own background fills proportionally to its share of the
// total, label left, count right) is deliberately modeled on
// $OMARCHY_PATH/shell/plugins/agents/Panel.qml's ModelRow — this bar
// already has one "per-provider usage" panel, this one should read like a
// sibling of it, not an unrelated one-off look.
Panel {
  id: root
  moduleName: "omarchy-ai.myapi"
  ipcTarget: "omarchy-ai.myapi"

  readonly property string py: "/home/ben-ami/Git/omarchy-ai/.venv/bin/omarchy-ai-settings"

  property var snapshot: ({})
  property var usageServices: ({})
  property bool connecting: false
  property string statusMessage: ""
  property string statusTone: "info" // "info" | "ok" | "error"

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color accent: "#39ff88"
  readonly property color dim: Qt.darker(fg, 1.45)
  readonly property color track: Qt.darker(fg, 2.2)
  readonly property color statusColor: statusTone === "error" ? "#ff6b6b" : (statusTone === "ok" ? accent : dim)

  readonly property bool myapiEnabled: !!(snapshot.fields && snapshot.fields.myapi_enabled)
  readonly property var myapiState: snapshot.myapi || ({})
  readonly property bool connected: !!myapiState.connected

  // Hidden entirely until enabled from the main settings panel — polled
  // rather than pushed over IPC: config changes are infrequent, and this
  // keeps both panels independent processes/components with no new
  // cross-plugin wiring, at the cost of up to ~5s to appear/disappear
  // after the toggle. Fine for a settings change, not a live stat.
  visible: myapiEnabled
  implicitWidth: myapiEnabled ? button.implicitWidth : 0
  implicitHeight: myapiEnabled ? button.implicitHeight : 0

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }

  // ---- Process queue: one Process instance, serialized calls (same
  // pattern as omarchy-ai.settings/Panel.qml — see its own comment for
  // why: secrets via environment, never argv). ----
  property var _queue: []

  function _enqueue(argv, cb, env) {
    root._queue.push({ argv: argv, cb: cb, env: env || ({}) })
    root._processQueue()
  }

  function _processQueue() {
    if (proc.running) return
    if (root._queue.length === 0) return
    var next = root._queue.shift()
    proc._cb = next.cb
    proc.command = next.argv
    proc.environment = next.env || ({})
    proc.running = true
  }

  Process {
    id: proc
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
        var cb = proc._cb
        proc._cb = null
        if (cb) cb(result)
        root._processQueue()
      }
    }
  }

  function fetchSnapshot() {
    root._enqueue([root.py, "get"], function(result) {
      if (result && !result.error) {
        root.snapshot = result
      }
    })
  }

  function fetchUsage() {
    root._enqueue([root.py, "myapi-usage"], function(result) {
      if (result && !result.error) {
        root.usageServices = result.services || ({})
      }
    })
  }

  function connectMyapi(code) {
    var trimmed = (code || "").trim()
    if (trimmed.length === 0) {
      root.statusTone = "error"
      root.statusMessage = "Paste the code from your MyApi dashboard first"
      return
    }
    root.connecting = true
    root._enqueue([root.py, "connect-myapi"], function(result) {
      root.connecting = false
      if (result && result.error) {
        root.statusTone = "error"
        root.statusMessage = result.error
      } else if (result) {
        root.snapshot = result
        root.statusTone = "ok"
        root.statusMessage = "Connected to MyApi"
        root.fetchUsage()
      }
    }, { "OMARCHY_AI_MYAPI_CODE": trimmed })
  }

  function disconnectMyapi() {
    root._enqueue([root.py, "disconnect-myapi"], function(result) {
      if (result && result.error) {
        root.statusTone = "error"
        root.statusMessage = result.error
      } else if (result) {
        root.snapshot = result
        root.statusTone = "ok"
        root.statusMessage = "Disconnected from MyApi"
      }
    })
  }

  // Refetch whenever opened, plus a standing timer so the icon's own
  // visible/hidden state (and the connected badge) stay current even while
  // closed — the main panel's toggle writes myapi_enabled independently of
  // this one being open.
  onOpenedChanged: if (opened) { root.statusMessage = ""; root.statusTone = "info"; root.fetchSnapshot(); root.fetchUsage() }

  Component.onCompleted: root.fetchSnapshot()

  Timer {
    interval: 5000
    running: true
    repeat: true
    onTriggered: {
      root.fetchSnapshot()
      if (root.opened && root.connected) root.fetchUsage()
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // No MyApi logo asset ships with this project, so a literal glyph
    // rather than a guessed-at brand mark — same "single icon-font
    // character, no custom image" shape every other non-branded
    // first-party bar icon in this shell uses (e.g. the Agents panel's
    // own `text: "󱚣"`). Material Design Icons' lightning-bolt glyph
    // (nf-md-lightning_bolt, U+F140B) specifically, not a plain Unicode
    // "⚡" — confirmed via the installed JetBrainsMono Nerd Font's own
    // cmap that the Agents panel's icon is drawn from the same nf-md-*
    // set, so this matches that convention rather than mixing glyph
    // families across panels.
    text: "󱐋"
    iconComponent: Component {
      Item {
        anchors.fill: parent

        OpticalGlyph {
          anchors.fill: parent
          text: button.text
          fontFamily: button.fontFamily
          fontSize: button.fontSize
          color: root.connected ? root.accent : button.foreground
        }

        // Small live-status dot, same visual weight/position as the main
        // Omarchy AI icon's own live-conversation dot — connected (green)
        // vs. not (no dot at all, rather than a second color, since
        // "not connected" is the icon's own resting state already).
        Rectangle {
          width: Math.max(5, Math.round(parent.width * 0.3))
          height: width
          radius: width / 2
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          visible: root.connected
          color: root.accent
          border.color: Qt.darker(Color.background, 1.6)
          border.width: 1
        }
      }
    }
    slotSize: Style.bar.statusSlot
    fontSize: Style.bar.iconFont
    tooltipText: "MyApi"
    onPressed: root.toggle()
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // Same title+subtitle shape as omarchy-ai.settings/Panel.qml's own
        // header, not a one-off — this panel should read as that one's
        // sibling.
        Column {
          width: parent.width
          spacing: Style.spacing.xxs
          Text {
            textFormat: Text.PlainText
            text: "MyApi"
            color: root.fg
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }
          Text {
            textFormat: Text.PlainText
            text: "Connect services to Omarchy AI"
            color: Qt.darker(root.fg, 1.5)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---- Not connected: credit + code entry ----
        Column {
          width: parent.width
          spacing: Style.space(10)
          visible: !root.connected

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: "Connect Gmail, Calendar, Drive, Notion, Slack, and 200+ other services from myapiai.com so Omarchy AI can use them directly."
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            color: root.dim
          }

          Button {
            text: "Open myapiai.com to get a code"
            bordered: true
            foreground: root.fg
            fontFamily: root.bar.fontFamily
            onClicked: Qt.openUrlExternally("https://www.myapiai.com")
          }

          Row {
            width: parent.width
            spacing: Style.space(8)

            TextField {
              id: codeField
              width: parent.width - connectButton.width - Style.space(8)
              placeholderText: "MYAPI-XXXXXXXX-XXXXXXXX"
              foreground: root.fg
              font.family: root.bar.fontFamily
              onAccepted: { root.connectMyapi(text); text = "" }
            }

            Button {
              id: connectButton
              text: root.connecting ? "Connecting…" : "Connect"
              bordered: true
              foreground: root.fg
              fontFamily: root.bar.fontFamily
              enabled: !root.connecting
              onClicked: { root.connectMyapi(codeField.text); codeField.text = "" }
            }
          }
        }

        // ---- Connected: status, usage, disconnect ----
        Column {
          width: parent.width
          spacing: Style.space(12)
          visible: root.connected

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: "Connected" + (root.myapiState.account ? (" as " + root.myapiState.account) : "")
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            color: root.accent
          }

          PanelSectionHeader { text: "USAGE"; foreground: root.fg; fontFamily: root.bar.fontFamily }

          // Same shape as the Agents panel's ModelRow: a row whose own
          // background fills to its share of the total call volume across
          // every connected service, label left, count right — reads as a
          // proportional breakdown at a glance rather than a bare table.
          Column {
                id: usageColumn
                width: parent.width
                spacing: Style.space(6)

                readonly property var serviceNames: Object.keys(root.usageServices).sort()
                readonly property real totalCalls: {
                  var t = 0
                  for (var i = 0; i < serviceNames.length; i++) t += (root.usageServices[serviceNames[i]].calls_total || 0)
                  return t
                }

                Text {
                  width: parent.width
                  visible: usageColumn.serviceNames.length === 0
                  textFormat: Text.PlainText
                  text: "No MyApi calls yet this session."
                  color: root.dim
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }

                Repeater {
                  model: usageColumn.serviceNames
                  delegate: Item {
                    required property var modelData
                    readonly property var stats: root.usageServices[modelData] || ({})
                    readonly property real share: usageColumn.totalCalls > 0 ? (stats.calls_total || 0) / usageColumn.totalCalls : 0

                    width: usageColumn.width
                    implicitHeight: rowLabel.implicitHeight + Style.spacing.lg

                    Rectangle {
                      anchors.fill: parent
                      radius: Style.cornerRadius
                      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.05)
                    }

                    Rectangle {
                      anchors.left: parent.left
                      anchors.top: parent.top
                      anchors.bottom: parent.bottom
                      width: parent.width * root.clamp(share, 0, 1)
                      radius: Style.cornerRadius
                      color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.22)
                    }

                    Text {
                      id: rowLabel
                      textFormat: Text.PlainText
                      text: modelData
                      color: root.fg
                      font.family: root.bar.fontFamily
                      font.pixelSize: Style.font.bodySmall
                      elide: Text.ElideRight
                      anchors.left: parent.left
                      anchors.leftMargin: Style.space(8)
                      anchors.right: rowCount.left
                      anchors.rightMargin: Style.space(8)
                      anchors.verticalCenter: parent.verticalCenter
                    }

                    Text {
                      id: rowCount
                      textFormat: Text.PlainText
                      text: (stats.calls_total || 0) + " (" + (stats.calls_today || 0) + " today)"
                      color: root.dim
                      font.family: root.bar.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: true
                      anchors.right: parent.right
                      anchors.rightMargin: Style.space(8)
                      anchors.verticalCenter: parent.verticalCenter
                    }
                  }
                }
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: "Run omarchy-ai-dashboard in a terminal for a live view."
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            color: root.dim
          }

          Button {
            text: "Disconnect"
            bordered: true
            foreground: "#ff6b6b"
            fontFamily: root.bar.fontFamily
            onClicked: root.disconnectMyapi()
          }

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: "Disconnect only stops Omarchy AI from using it locally — to fully revoke access, also remove this device from your MyApi dashboard's Devices/Agents list."
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            color: root.dim
          }
        }

        PanelSeparator { foreground: root.fg; visible: root.statusMessage.length > 0 }

        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          textFormat: Text.PlainText
          visible: root.statusMessage.length > 0
          text: root.statusMessage
          color: root.statusColor
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
        }
      }
    }
  }
}
