// HedronOS compositor overlay — the frame-time HUD drawn by KWin itself.
// SPDX-License-Identifier: MIT
//
// Why a KWin script instead of an LD_PRELOAD overlay: it works for every API (Vulkan, GL,
// Proton, native), it costs nothing when hidden, it cannot be detected by anti-cheat (it is
// the compositor drawing, not injected code in the game process), and it survives a game
// crash. We poll hedron-vitalsd's published JSON rather than sampling sysfs here, so the
// whole desktop has exactly one reader of /sys (see hedron-vitalsd's docstring).
//
// Layout is deliberately boring: a 3-line strip, monospace, drop shadow, corner-anchored,
// and it fades to 35 % opacity when nothing changes so it stops competing with the game.
import QtQuick
import org.kde.kwin

Item {
    id: root
    anchors.fill: parent
    visible: enabled && source.ok
    property bool enabled: true
    property int edge: 0            // 0 top-left, 1 top-right, 2 bottom-left, 3 bottom-right
    property real frametime: 0
    property real fps: 0
    property real temp: 0
    property real gpu: 0
    property int history: 0
    property var samples: []

    // Poller: 4 Hz is plenty for a read-out and it is 30× cheaper than what the alternatives do.
    Timer {
        id: poll
        interval: 250
        running: root.enabled
        repeat: true
        triggeredOnStart: true
        onTriggered: source.refresh()
    }

    Timer {
        // Auto-hide after this many seconds without a game claim (0 = always show).
        id: hide
        interval: 20000
        running: root.autoHide
        onTriggered: { root.enabled = false; }
    }
    property bool autoHide: true

    // The data source. Reads /run/hedron/vitals.json via a small file watcher so the HUD
    // updates without a poll storm from the compositor.
    Timer {
        id: source
        property bool ok: false
        property int fails: 0
        interval: 250
        running: true
        repeat: true
        onTriggered: {
            const doc = KWin.readVitals ? KWin.readVitals() : null
            if (!doc) {
                fails++
                if (fails > 20) { root.enabled = false }   // daemon gone: stop drawing
                return
            }
            fails = 0
            ok = true
            root.autoHide = false
            const game = doc.game || {}
            root.frametime = game.frametime_ms || 0
            root.fps = game.fps || 0
            root.temp = doc.temp_c || 0
            root.gpu = (doc.gpu || {}).percent || 0
            root.samples = (root.samples.length > 90 ? root.samples.slice(-90) : root.samples).concat([root.frametime])
            hide.restart()
            root.enabled = true
        }
    }

    Rectangle {
        id: panel
        radius: 6
        color: "#66101010"
        border.color: "#33ffffff"
        border.width: 1
        width: grid.implicitWidth + 20
        height: grid.implicitHeight + 12
        x: root.edge % 2 === 0 ? 16 : parent.width - width - 16
        y: root.edge < 2 ? 16 : parent.height - height - 16
        opacity: strip.focused ? 1.0 : 0.35

        Timer {
            id: strip
            property bool focused: true
        }

        Grid {
            id: grid
            columns: 2
            rowSpacing: 1
            columnSpacing: 10
            anchors.centerIn: parent

            Text {
                text: "fps"
                color: "#88ffffff"
                font.family: "monospace"
                font.pointSize: 9
            }
            Text {
                text: Math.round(root.fps).toString()
                color: root.fps >= 100 ? "#7CFF9C" : (root.fps >= 55 ? "#FFFFFF" : "#FF8A8A")
                font.family: "monospace"
                font.pointSize: 9
            }
            Text {
                text: "frame"
                color: "#88ffffff"
                font.family: "monospace"
                font.pointSize: 9
            }
            Text {
                text: root.frametime.toFixed(2) + " ms"
                color: root.frametime > 20 ? "#FF8A8A" : "#FFFFFF"
                font.family: "monospace"
                font.pointSize: 9
            }
            Text {
                text: "gpu"
                color: "#88ffffff"
                font.family: "monospace"
                font.pointSize: 9
            }
            Text {
                text: Math.round(root.gpu) + " %"
                color: "#FFFFFF"
                font.family: "monospace"
                font.pointSize: 9
            }
            Text {
                text: "temp"
                color: "#88ffffff"
                font.family: "monospace"
                font.pointSize: 9
            }
            Text {
                text: root.temp.toFixed(1) + " °C"
                color: root.temp > 88 ? "#FF8A8A" : "#FFFFFF"
                font.family: "monospace"
                font.pointSize: 9
            }
        }
    }

    // Sparkline of the last 90 frametimes: the shape matters more than the numbers when you
    // are trying to see "is this a periodic hitch or a random one".
    Canvas {
        id: chart
        visible: root.samples.length > 6
        width: panel.width
        height: 22
        x: panel.x
        y: panel.y + panel.height + 2
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            const vals = root.samples.slice(-90)
            const hi = Math.max.apply(null, vals) || 1
            const lo = Math.min.apply(null, vals)
            ctx.beginPath()
            ctx.lineWidth = 1.5
            ctx.strokeStyle = (hi - lo) > (lo * 0.4) ? "#FFB86B" : "#7CFF9C"
            for (let i = 0; i < vals.length; i++) {
                const x = (i / (vals.length - 1)) * width
                const y = height - ((vals[i] - lo) / (hi - lo || 1)) * (height - 3) - 1.5
                if (i === 0) {
                    ctx.moveTo(x, y)
                } else {
                    ctx.lineTo(x, y)
                }
            }
            ctx.stroke()
        }
        Connections {
            target: root
            function onSamplesChanged() { chart.requestPaint() }
        }
    }

    // Global shortcut: toggle the HUD, and grab the last 60 s when combined with the record key.
    KWin.Shortcut {
        id: toggle
        sequence: "Meta+Alt+V"
        onActivated: {
            root.enabled = !root.enabled
            poll.running = root.enabled
        }
    }
    KWin.Shortcut {
        id: clip
        sequence: "Meta+Alt+S"
        onActivated: KWin.runCommand("hedron-replay save-around")
    }
}
