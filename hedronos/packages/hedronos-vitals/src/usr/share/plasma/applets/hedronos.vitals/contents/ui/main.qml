// HedronOS vitals panel applet. SPDX-License-Identifier: MIT
//
// One QTimer, one file read, no C++, no extra daemon: `hedron-vitalsd` already samples
// /sys once for everybody and publishes /run/hedron/vitals.json, so the applet's whole job is
// to render that file without becoming a second sampler that fights the first one for I/O.
import QtQuick
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PC
import org.kde.ksvg as KSvg
import QtQuick.Layouts

PC.Label {
    id: root

    property var data: ({})
    property bool hud: true

    function hudEnabled() {
        // The Meta+V shortcut writes this file (see `hedron-vitals toggle`); the applet obeys it
        // rather than keeping its own idea of whether it is on.
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file:///run/user/" + Qt.platform + "/hedron/hud.enabled", false)
        try { xhr.send() } catch (e) { return true }
        return xhr.status === 0 || xhr.status === 200
    }

    function readVitals() {
        if (!hudEnabled()) {
            root.text = ""
            return
        }
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file:///run/hedron/vitals.json", false)
        try {
            xhr.send()
        } catch (e) {
            root.text = "vitals: n/a"
            return
        }
        if (xhr.status !== 0 && xhr.status !== 200) {
            root.text = "vitals: n/a"
            return
        }
        try {
            root.data = JSON.parse(xhr.responseText)
        } catch (e) {
            root.text = "vitals: ?"     // the daemon was mid-write; the next tick will be fine
            return
        }
        const d = root.data
        const fps = d.game && d.game.fps ? Math.round(d.game.fps) : "—"
        const ft = d.game && d.game.p99_ms ? d.game.p99_ms.toFixed(1) : "—"
        const gpu = d.gpu && d.gpu.temp_c !== undefined ? Math.round(d.gpu.temp_c) : "—"
        const cpu = d.cpu && d.cpu.temp_c !== undefined ? Math.round(d.cpu.temp_c) : "—"
        const pw = d.power && d.power.package_w !== undefined ? Math.round(d.power.package_w) : "—"
        root.text = (d.stale ? "⚠ " : "") + fps + " fps · p99 " + ft + " ms · " + gpu + "°/" + cpu + "° · " + pw + " W"
    }

    Timer {
        interval: 500
        running: root.hud
        repeat: true
        onTriggered: root.readVitals()
    }

    Component.onCompleted: {
        plasmoid.title = "HedronOS Vitals"
        plasmoid.icon = "hardware-monitor"
        readVitals()
    }
}
