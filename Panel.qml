import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// Voxbox panel: transport, live sentence list, voice picker, speed/volume.
// All state lives in BarWidget.qml (hostWidget), which owns the daemon.
Panel {
  id: root
  moduleName: "io.github.nousd.voxbox"
  ipcTarget: ""
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root
  readonly property var host: hostWidget

  readonly property color contentForeground: bar ? bar.foreground : Color.foreground
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color dim: Qt.darker(contentForeground, 1.4)

  readonly property int curIndex: host ? host.sentenceIndex : 0
  onCurIndexChanged: sentenceList.positionViewAtIndex(curIndex, ListView.Contain)

  function open() {
    if (host) { host.ensureDaemon(); host.sendCmd({ cmd: "status" }) }
    root.controller.show()
  }
  function close() { root.controller.hide() }
  function toggle() { root.opened ? root.close() : root.open() }
  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function send(obj) { if (host) host.sendCmd(obj) }
  function exportPath() {
    return Quickshell.env("HOME") + "/Music/voxbox-"
      + Qt.formatDateTime(new Date(), "yyyyMMdd-HHmmss") + ".mp3"
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(430))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onMoveRequested: function(dx, dy) { if (dx !== 0) root.send({ cmd: "jump", delta: dx }) }
      onActivateRequested: root.send({ cmd: "toggle" })
      onTextKey: function(t) {
        if (t === " ") root.send({ cmd: "toggle" })
        else if (t === "r" || t === "R") { if (root.host) root.host.captureRegion() }
        else if (t === "s" || t === "S") { if (root.host) root.host.readSelection() }
      }

      Column {
        id: contentColumn
        width: parent.width
        spacing: Style.space(10)

        PanelHero {
          title: "Voxbox"
          meta: ((root.host ? root.host.statusLine : "idle") + (root.host && root.host.sourceLine ? " · " + root.host.sourceLine : "")).toUpperCase()
          foreground: root.contentForeground
          fontFamily: root.contentFontFamily
          iconComponent: Component {
            Text {
              text: "󰔊"
              color: root.contentForeground
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.display
            }
          }
        }

        PanelSeparator { foreground: root.contentForeground }

        Text {
          visible: root.host ? root.host.daemonMissing : false
          width: parent.width
          wrapMode: Text.Wrap
          text: "The speech engine is not installed. Run install.sh from the Voxbox repository, then reopen this panel."
          color: root.dim
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.bodySmall
        }

        // ---- the text being read: click a sentence to start from it
        Rectangle {
          width: parent.width
          height: Style.space(170)
          radius: Style.cornerRadius
          color: Style.normalFillFor(root.contentForeground, Color.accent)
          border.color: Style.normalBorderFor(root.contentForeground, Color.accent)
          border.width: Style.normalBorderWidth

          ListView {
            id: sentenceList
            anchors.fill: parent
            anchors.margins: Style.space(6)
            clip: true
            spacing: Style.space(2)
            boundsBehavior: Flickable.StopAtBounds
            model: root.host ? root.host.sentences : []
            delegate: Rectangle {
              required property int index
              required property var modelData
              width: sentenceList.width
              height: rowText.implicitHeight + Style.space(6)
              radius: Style.cornerRadius
              color: index === root.curIndex
                ? Qt.alpha(Color.accent, 0.35)
                : (rowHover.hovered ? Style.hoverFillFor(root.contentForeground, Color.accent) : "transparent")
              Text {
                id: rowText
                anchors.verticalCenter: parent.verticalCenter
                x: Style.space(6)
                width: parent.width - Style.space(12)
                text: modelData
                wrapMode: Text.Wrap
                color: root.contentForeground
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.body
              }
              HoverHandler { id: rowHover }
              TapHandler {
                onTapped: {
                  root.send({ cmd: "goto", index: index })
                  root.send({ cmd: "play" })
                }
              }
            }
            Text {
              visible: sentenceList.count === 0
              anchors.centerIn: parent
              text: "Capture a region or read your selection to begin"
              color: root.dim
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }
        }

        Row {
          spacing: Style.spacing.controlGap
          Button {
            text: "Region"
            iconText: "󰴑"
            bordered: true
            foreground: root.contentForeground
            tooltipText: "Drag a box, read what is inside it  (r)"
            onClicked: if (root.host) root.host.captureRegion()
          }
          Button {
            text: "Selection"
            iconText: "󰽏"
            bordered: true
            foreground: root.contentForeground
            tooltipText: "Read the highlighted text  (s)"
            onClicked: if (root.host) root.host.readSelection()
          }
          Button {
            text: "Export"
            iconText: "󰈣"
            bordered: true
            foreground: root.contentForeground
            tooltipText: "Save as MP3 into ~/Music"
            onClicked: root.send({ cmd: "export", path: root.exportPath() })
          }
        }

        PanelSeparator { foreground: root.contentForeground }

        // ---- transport
        Row {
          anchors.horizontalCenter: parent.horizontalCenter
          spacing: Style.spacing.controlGap
          PanelActionButton {
            iconText: "󰒮"; bordered: true; foreground: root.contentForeground
            tooltipText: "Previous sentence"
            onClicked: root.send({ cmd: "jump", delta: -1 })
          }
          PanelActionButton {
            iconText: root.host && root.host.playing ? "󰏤" : "󰐊"
            bordered: true; foreground: root.contentForeground
            tooltipText: "Play / pause  (space)"
            onClicked: root.send({ cmd: "toggle" })
          }
          PanelActionButton {
            iconText: "󰒭"; bordered: true; foreground: root.contentForeground
            tooltipText: "Next sentence"
            onClicked: root.send({ cmd: "jump", delta: 1 })
          }
          PanelActionButton {
            iconText: "󰓛"; bordered: true; foreground: root.contentForeground
            tooltipText: "Stop and rewind"
            onClicked: root.send({ cmd: "stop" })
          }
        }

        // ---- voice
        Row {
          width: parent.width
          spacing: Style.spacing.controlGap
          SearchableDropdown {
            id: voiceDropdown
            width: parent.width - previewButton.width - Style.spacing.controlGap
            label: "VOICE"
            foreground: root.contentForeground
            fontFamily: root.contentFontFamily
            options: root.host ? root.host.voices.map(function(v) { return { value: v.id, label: v.label } }) : []
            value: root.host ? root.host.voice : ""
            placeholderText: "Search 85 voices…"
            onChanged: function(value) { root.send({ cmd: "set", voice: value }) }
          }
          PanelActionButton {
            id: previewButton
            anchors.bottom: parent.bottom
            anchors.bottomMargin: Math.max(0, (Style.spacing.controlHeight - height) / 2)
            iconText: "󰐊"; bordered: true; foreground: root.contentForeground
            tooltipText: "Hear this voice"
            onClicked: root.send({ cmd: "preview", voice: root.host ? root.host.voice : "" })
          }
        }

        // ---- speed
        Item {
          width: parent.width
          height: speedHeader.implicitHeight
          PanelSectionHeader { id: speedHeader; text: "SPEED"; foreground: root.contentForeground; fontFamily: root.contentFontFamily }
          PanelSectionHeader {
            anchors.right: parent.right
            text: (root.host ? root.host.speed : 1.0).toFixed(2) + "×"
            foreground: root.contentForeground; fontFamily: root.contentFontFamily
          }
        }
        PanelSlider {
          width: parent.width
          bar: root.bar
          minimum: 0.5; maximum: 2.0; step: 0.05
          value: root.host ? root.host.speed : 1.0
          onReleased: function(value) { root.send({ cmd: "set", speed: value }) }
        }

        // ---- volume
        Item {
          width: parent.width
          height: volumeHeader.implicitHeight
          PanelSectionHeader { id: volumeHeader; text: "VOLUME"; foreground: root.contentForeground; fontFamily: root.contentFontFamily }
          PanelSectionHeader {
            anchors.right: parent.right
            text: Math.round((root.host ? root.host.volume : 0.85) * 100) + "%"
            foreground: root.contentForeground; fontFamily: root.contentFontFamily
          }
        }
        PanelSlider {
          width: parent.width
          bar: root.bar
          minimum: 0.0; maximum: 1.0; step: 0.02
          value: root.host ? root.host.volume : 0.85
          onReleased: function(value) { root.send({ cmd: "set", volume: value }) }
        }
      }
    }
  }
}
