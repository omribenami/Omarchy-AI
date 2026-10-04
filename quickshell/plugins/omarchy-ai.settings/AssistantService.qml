import QtQuick
import Quickshell
import Quickshell.Io

// Loaded when omarchy-ai.settings is enabled (keepLoaded service).
// Omarchy's installer only clones the plugin and flips the enabled bit.
// This process is what installs and starts the assistant.
Item {
  id: root

  function _localPath(name) {
    var path = Qt.resolvedUrl(name).toString()
    if (path.indexOf("file://") === 0)
      path = decodeURIComponent(path.substring(7))
    return path
  }

  Component.onCompleted: starter.running = true

  Process {
    id: starter
    command: ["/usr/bin/bash", root._localPath("start-assistant.sh")]
  }
}
