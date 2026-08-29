import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Voxbox bar widget: a speaker glyph that owns the speech daemon and hosts the
// panel. Left click: panel. Right click: capture a region straight away.
// Middle click: play/pause.
BarWidget {
  id: root
  moduleName: "io.github.nousd.voxbox"

  // ---- daemon state, consumed by Panel.qml through hostWidget
  property bool daemonReady: false
  property bool daemonMissing: false
  property bool everReady: false
  property bool playing: false
  property int sentenceIndex: 0
  property int sentenceTotal: 0
  property var sentences: []
  property string sourceLine: ""
  property string statusLine: "idle"
  property var voices: []
  property string voice: ""
  property real speed: 1.0
  property real volume: 0.85
  property bool reopenAfterCapture: false
  property var pendingCmds: []
  property bool installing: false
  property string installLog: ""

  function installEngine() {
    if (installing) return
    installing = true
    installLog = "starting install…"
    installProc.running = true
  }

  // Anything derived from captured text or a subprocess is inert before it
  // reaches a component whose Text items we do not control (PanelHero and the
  // shared Ui controls default to AutoText): drop the three characters that
  // can make Qt treat a string as rich text.
  function plain(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/[<>&]/g, " ").replace(/\s+/g, " ").trim()
  }

  function ensureDaemon() {
    if (!daemon.running) {
      daemonMissing = false
      daemon.running = true
      statusLine = "starting engine…"
    }
  }

  function sendCmd(obj) {
    ensureDaemon()
    if (!daemonReady) {
      pendingCmds.push(obj)
      return
    }
    daemon.write(JSON.stringify(obj) + "\n")
  }

  function captureRegion() {
    // The panel would sit on top of the text being captured; drop it first.
    reopenAfterCapture = opened
    close()
    sendCmd({ cmd: "region" })
  }

  function readSelection() { sendCmd({ cmd: "selection" }) }

  function handleEvent(msg) {
    switch (msg.event) {
    case "ready":
      daemonReady = true
      everReady = true
      voices = msg.voices || []
      voice = msg.voice || ""
      speed = msg.speed || 1.0
      volume = msg.volume === undefined ? 0.85 : msg.volume
      statusLine = "idle"
      var queued = pendingCmds
      pendingCmds = []
      for (var i = 0; i < queued.length; i++) sendCmd(queued[i])
      break
    case "text":
      sentences = msg.sentences || []
      sentenceTotal = sentences.length
      sentenceIndex = 0
      sourceLine = sentenceTotal > 0 ? ((msg.words || 0) + " words from " + plain(msg.source || "screen") + (msg.truncated ? " · truncated" : "")) : ""
      statusLine = sentenceTotal > 0 ? "ready" : "idle"
      if (reopenAfterCapture) {
        reopenAfterCapture = false
        open()
      }
      break
    case "sentence":
      sentenceIndex = msg.index || 0
      break
    case "state":
      playing = msg.playing === true
      statusLine = playing ? "reading"
        : (sentenceTotal === 0 ? "idle" : (msg.finished ? "done" : "paused"))
      break
    case "config":
      if (msg.voice) voice = msg.voice
      if (msg.speed !== undefined) speed = msg.speed
      if (msg.volume !== undefined) volume = msg.volume
      break
    case "export":
      statusLine = msg.done ? ("saved " + plain(String(msg.path || "").split("/").pop())) : ("exporting " + plain(msg.progress || ""))
      break
    case "cancelled":
      statusLine = "cancelled"
      if (reopenAfterCapture) { reopenAfterCapture = false; open() }
      break
    case "error":
      statusLine = plain(msg.message || "error").slice(0, 60)
      if (reopenAfterCapture) { reopenAfterCapture = false; open() }
      break
    }
  }

  Process {
    id: installProc
    command: ["bash", Quickshell.env("HOME") + "/.config/omarchy/plugins/io.github.nousd.voxbox/install.sh"]
    stdout: SplitParser {
      onRead: function(l) {
        l = String(l).replace(/\x1b\[[0-9;]*m/g, "").trim()
        if (l) root.installLog = root.plain(l).slice(0, 110)
      }
    }
    stderr: SplitParser {
      onRead: function(l) {
        l = String(l).replace(/\x1b\[[0-9;]*m/g, "").trim()
        if (l) root.installLog = root.plain(l).slice(0, 110)
      }
    }
    onExited: function(code, status) {
      root.installing = false
      if (code === 0) {
        root.installLog = ""
        root.statusLine = "engine installed"
        root.daemonMissing = false
        root.everReady = false
        root.ensureDaemon()
        root.sendCmd({ cmd: "status" })
      } else {
        root.installLog = "install failed (exit " + code + ") — see the README"
      }
    }
  }

  Process {
    id: daemon
    command: ["bash", "-lc", "exec voxbox daemon"]
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) {
        line = String(line).trim()
        if (!line) return
        if (line.length > 2 * 1024 * 1024) return   // never parse an unbounded line
        try { root.handleEvent(JSON.parse(line)) } catch (e) { /* not an event line */ }
      }
    }
    onExited: function(code, status) {
      root.daemonReady = false
      root.playing = false
      if (!root.everReady) {
        root.daemonMissing = true
        root.statusLine = "backend not installed"
      }
    }
  }

  // ---- panel plumbing (same shape as the built-in clock widget)
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function togglePanel() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  IpcHandler {
    target: "voxbox"
    function open(): void { root.ensureDaemon(); root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.ensureDaemon(); root.togglePanel() }
    function region(): void { root.captureRegion() }
    function selection(): void { root.readSelection() }
    function playpause(): void { root.sendCmd({ cmd: "toggle" }) }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰔊"
    active: root.playing
    onPressed: function(btn) {
      if (btn === Qt.RightButton) root.captureRegion()
      else if (btn === Qt.MiddleButton) root.sendCmd({ cmd: "toggle" })
      else { root.ensureDaemon(); root.togglePanel() }
    }
  }
}
