import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "omarchy-ai.myapi"
  ipcTarget: "omarchy-ai.myapi"

  readonly property string py: "@OMARCHY_AI_SETTINGS@"

  property var snapshot: ({})
  property var dashboard: ({})
  property string period: "7d"
  property bool loading: false
  property string dashboardError: ""
  property string selectedDevice: ""
  property string breakdown: "services"
  readonly property var devices: dashboard.devices || []
  readonly property var selected: {
    for (var i = 0; i < devices.length; i++) if (devices[i].id === selectedDevice) return devices[i]
    return null
  }
  readonly property var services: selected ? selected.services : (dashboard.serviceTotals || [])
  readonly property var daily: selected ? selected.daily : (dashboard.allDaily || [])
  readonly property var labels: dashboard.bucketLabels || []
  readonly property real maxDaily: Math.max.apply(null, [1].concat(daily))
  function count(value) { return value === undefined || value === null ? "—" : Number(value).toLocaleString(Qt.locale(), 'f', 0) }
  function choosePeriod(value) {
    root.period = value; root.selectedDevice = ""; dashboardScroll.contentY = 0; root.dashboard = ({}); root.fetchUsage()
  }
  property bool connecting: false
  property string statusMessage: ""
  property string statusTone: "info" // "info" | "ok" | "error"

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color accent: Color.accent
  readonly property color dim: Qt.rgba(fg.r, fg.g, fg.b, 0.72)
  readonly property color track: Qt.darker(fg, 2.2)
  readonly property color statusColor: statusTone === "error" ? "#ff6b6b" : (statusTone === "ok" ? accent : dim)

  readonly property bool myapiEnabled: !!(snapshot.fields && snapshot.fields.myapi_enabled)
  readonly property var myapiState: snapshot.myapi || ({})
  readonly property bool connected: !!myapiState.connected

  visible: myapiEnabled
  implicitWidth: myapiEnabled ? button.implicitWidth : 0
  implicitHeight: myapiEnabled ? button.implicitHeight : 0

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }

  property var _queue: []

  function _enqueue(argv, cb, env) {
    root._queue.push({ argv: argv, cb: cb, env: env || ({}) })
    root._processQueue()
  }

  function _processQueue() {
    if (proc.running || proc._cb !== null) return
    if (root._queue.length === 0) return
    var next = root._queue.shift()
    proc._cb = next.cb
    proc.command = next.argv
    proc.environment = next.env || ({})
    proc.running = true
  }

  Process {
    id: proc
    onRunningChanged: if (!running) Qt.callLater(root._processQueue)
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
        var wasConnected = root.connected
        root.snapshot = result
        if (root.opened && root.connected && !wasConnected) root.fetchUsage()
      }
    })
  }

  function fetchUsage() {
    if (!root.connected) return
    var requested = root.period
    root.loading = true
    root._enqueue([root.py, "myapi-dashboard", requested], function(result) {
      if (requested !== root.period) return
      root.loading = false
      root.dashboardError = result.error || ""
      if (!result.error) root.dashboard = result
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
        root.dashboard = ({})
        root.selectedDevice = ""
        root.statusTone = "ok"
        root.statusMessage = "Disconnected from MyApi"
      }
    })
  }

  onOpenedChanged: if (opened) { dashboardScroll.contentY = 0; root.statusMessage = ""; root.statusTone = "info"; root.fetchSnapshot(); root.fetchUsage() }

  Component.onCompleted: root.fetchSnapshot()

  Timer {
    interval: 5000
    running: true
    repeat: true
    onTriggered: {
      if (!proc.running && root._queue.length === 0) root.fetchSnapshot()

    }
  }

  Timer {
    interval: 30000; running: root.opened && root.connected; repeat: true
    onTriggered: if (!root.loading) root.fetchUsage()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
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
    contentWidth: panel.fittedContentWidth(Style.space(490))
    contentHeight: panel.fittedContentHeight(Math.min(column.implicitHeight, Style.space(660)))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      Flickable {
        id: dashboardScroll
        anchors.fill: parent
        contentWidth: width; contentHeight: column.implicitHeight
        clip: true; boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.rightMargin: Style.space(10)
        anchors.top: parent.top
        spacing: Style.space(14)

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
            text: root.connected ? "Account activity · all devices and agents" : "Connect your MyApi account"
            color: Qt.darker(root.fg, 1.5)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        PanelSeparator { foreground: root.fg }

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

        Column {
          width: parent.width; spacing: Style.space(12)
          visible: root.connected
          Row {
            width: parent.width; spacing: Style.space(8)
            ButtonGroup {
              width: parent.width - refresh.width - parent.spacing
              foreground: root.fg; fontFamily: root.bar.fontFamily; fontSize: Style.font.bodySmall
              value: root.period
              options: [{value:"24h",label:"Day"},{value:"7d",label:"Week"},{value:"30d",label:"Month"}]
              onChanged: function(v) { root.choosePeriod(v) }
            }
            Button {
              id: refresh; text: root.loading ? "Loading…" : "Refresh"
              foreground: root.fg; fontFamily: root.bar.fontFamily; fontSize: Style.font.caption
              focusable: true; enabled: !root.loading; onClicked: root.fetchUsage()
            }
          }
          Text {
            width: parent.width; wrapMode: Text.WordWrap; textFormat: Text.PlainText
            visible: root.dashboardError.length > 0
            text: "Dashboard unavailable: " + root.dashboardError
            color: Color.urgent; font.family: root.bar.fontFamily; font.pixelSize: Style.font.caption
          }
          Column {
            width: parent.width; spacing: Style.space(12)
            visible: root.dashboard.grand !== undefined
            Row {
              width: parent.width; spacing: Style.space(8)
              Repeater {
                model: [{label:"API CALLS",value:root.count(root.dashboard.grand)},
                  {label:"ACTIVE AGENTS",value:root.count(root.dashboard.activeCount) + "/" + root.count(root.dashboard.deviceCount)},
                  {label:"ERROR RATE",value:root.dashboard.errorRate + "%"}]
                delegate: Rectangle {
                  required property var modelData
                  width: (parent.width - Style.space(16)) / 3; height: Style.space(65)
                  radius: Style.cornerRadius
                  color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.05)
                  Column {
                    anchors.centerIn: parent; spacing: Style.space(5)
                    Text { text: modelData.value; color: root.fg; font.family: root.bar.fontFamily; font.pixelSize: Style.font.title; font.bold: true }
                    Text { text: modelData.label; color: root.dim; font.family: root.bar.fontFamily; font.pixelSize: Style.font.caption }
                  }
                }
              }
            }
            Row {
              width: parent.width
              Text {
                width: parent.width - clearFilter.width
                text: root.selected ? root.selected.label : "All agents · last " + ({"24h":"24 hours","7d":"7 days","30d":"30 days"}[root.period])
                elide: Text.ElideRight; color: root.fg; font.family: root.bar.fontFamily; font.pixelSize: Style.font.bodySmall
              }
              Button { id: clearFilter; visible: !!root.selected; text:"Clear"; foreground:root.fg; fontFamily:root.bar.fontFamily; fontSize:Style.font.caption; onClicked: root.selectedDevice = "" }
            }
            Row {
              width: parent.width; height: Style.space(84); spacing: Style.space(3)
              Repeater {
                model: root.daily
                delegate: Item {
                  required property var modelData
                  required property int index
                  width: (parent.width - parent.spacing * Math.max(0, root.daily.length - 1)) / Math.max(1,root.daily.length)
                  height: parent.height
                  Rectangle {
                    anchors.bottom: parent.bottom; width: parent.width
                    height: Math.max(modelData > 0 ? 2 : 0, parent.height * modelData / root.maxDaily)
                    color: root.accent; opacity: hover.containsMouse ? 1 : 0.65
                  }
                  MouseArea { id:hover; anchors.fill:parent; hoverEnabled:true }
                  ToolTip.visible: hover.containsMouse
                  ToolTip.text: (root.labels[index] || "") + " · " + root.count(modelData) + " calls"
                }
              }
            }
            Row {
              width: parent.width
              Text { width: parent.width/2; text:root.labels[0] || ""; color:root.dim; font.family:root.bar.fontFamily; font.pixelSize:Style.font.caption }
              Text { width: parent.width/2; horizontalAlignment:Text.AlignRight; text:root.labels[root.labels.length-1] || ""; color:root.dim; font.family:root.bar.fontFamily; font.pixelSize:Style.font.caption }
            }
            PanelSeparator { foreground: root.fg }
            ButtonGroup {
              width:parent.width; foreground:root.fg; fontFamily:root.bar.fontFamily; fontSize:Style.font.bodySmall
              value:root.breakdown; options:[{value:"services",label:"Services called"},{value:"agents",label:"Devices & agents"}]
              onChanged:function(v) { root.breakdown = v }
            }
            Text { visible:root.breakdown === "services" && root.services.length === 0; text:"No service calls in this period."; color:root.dim; font.family:root.bar.fontFamily; font.pixelSize:Style.font.caption }
            Repeater {
              model: root.services
              delegate: Item {
                required property var modelData
                visible: root.breakdown === "services"
                width: parent.width; height: Style.space(29)
                Rectangle {
                  height:parent.height; width:parent.width * Math.min(1, modelData.value / Math.max(1, root.selected ? root.selected.value : root.dashboard.grand))
                  color:root.accent; opacity:0.12; radius:Style.cornerRadius
                }
                Text { anchors.left:parent.left; anchors.leftMargin:Style.space(8); anchors.verticalCenter:parent.verticalCenter; width:parent.width*0.7; elide:Text.ElideRight; textFormat:Text.PlainText; text:modelData.key; color:root.fg; font.family:root.bar.fontFamily; font.pixelSize:Style.font.bodySmall }
                Text { anchors.right:parent.right; anchors.rightMargin:Style.space(8); anchors.verticalCenter:parent.verticalCenter; text:root.count(modelData.value); color:root.dim; font.family:root.bar.fontFamily; font.pixelSize:Style.font.bodySmall }
              }
            }
            Text { visible:root.breakdown === "agents"; text:"Select an agent to filter the chart and services."; color:root.dim; font.family:root.bar.fontFamily; font.pixelSize:Style.font.caption }
            Repeater {
              model: root.devices
              delegate: Button {
                required property var modelData
                visible:root.breakdown === "agents"
                width:parent.width; leftAlign:true; selected:root.selectedDevice === modelData.id
                text:(modelData.label.length > 30 ? modelData.label.slice(0,29) + "…" : modelData.label) + " · " + root.count(modelData.value) + " calls"
                tooltipText:modelData.label + " · " + modelData.status + " · last seen " + modelData.lastSeen + " · errors " + modelData.errPct + "%"
                fontFamily:root.bar.fontFamily; fontSize:Style.font.bodySmall; foreground:root.fg; focusable:true
                onClicked:root.selectedDevice = root.selectedDevice === modelData.id ? "" : modelData.id
              }
            }
            PanelSeparator { foreground:root.fg }
            PanelSectionHeader { text:"INSIGHTS"; foreground:root.fg; fontFamily:root.bar.fontFamily }
            Text {
              width:parent.width; wrapMode:Text.WordWrap; textFormat:Text.PlainText
              text: {
                var lines = []
                var agents = root.devices.slice().sort(function(a,b){return b.value-a.value})
                var services = (root.dashboard.serviceTotals || []).slice().sort(function(a,b){return b.value-a.value})
                if (agents.length && root.dashboard.grand > 0) lines.push(agents[0].label + " accounts for " + Math.round(100*agents[0].value/root.dashboard.grand) + "% of calls.")
                if (services.length) lines.push("Most-used service: " + services[0].key + " (" + root.count(services[0].value) + " calls).")
                lines.push(root.count(root.dashboard.rateLimited) + " rate-limited calls in this period.")
                return lines.join("\n")
              }
              color:root.dim; font.family:root.bar.fontFamily; font.pixelSize:Style.font.bodySmall
            }
            Text {
              width:parent.width; wrapMode:Text.WordWrap
              text:"Updated " + (root.dashboard.timestamp ? new Date(root.dashboard.timestamp).toLocaleTimeString(Qt.locale(), "HH:mm") : "—") + (root.dashboardError ? " · showing previous data" : " · account-wide")
              color:root.dim; font.family:root.bar.fontFamily; font.pixelSize:Style.font.caption
            }
          }
          PanelSeparator { foreground:root.fg }
          Row {
            spacing:Style.space(8)
            Button { text:"Open full dashboard"; bordered:true; foreground:root.fg; fontFamily:root.bar.fontFamily; focusable:true; onClicked:Qt.openUrlExternally("https://www.myapiai.com/dashboard/") }
            Button { text:"Disconnect"; foreground:root.dim; fontFamily:root.bar.fontFamily; focusable:true; onClicked:root.disconnectMyapi() }
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
}
