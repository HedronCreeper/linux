// HedronOS first-run app chooser (Kirigami) — the GUI half of `hedron-setup`.
// SPDX-License-Identifier: MIT
//
// Same contract as the TUI: it only ever calls `hedron-setup --select … --json` and
// `hedronos-install`, so both front ends cannot drift from each other.
import QtQuick
import QtQuick.Controls as QQC
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import local.hedron.feed 1.0

Kirigami.ApplicationWindow {
    id: win
    width: 1180
    height: 780
    title: "Welcome to HedronOS — pick your apps"

    property var apps: []
    property var cats: []
    property var selected: []
    property string note: ""
    property real sizeMib: 0

    HedronFeed { id: feed }

    property string pending: "print"

    function argvFor(kind, extra) {
        let argv
        if (kind === "print") {
            argv = ["hedron-setup", "--doc"]
        } else if (kind === "toggle") {
            argv = ["hedron-setup", "--out", SELECTION_PATH, "--select", extra, "--json"]
        } else if (kind === "bundle") {
            argv = ["hedron-setup", "--out", SELECTION_PATH, "--bundle", extra, "--json"]
        } else {
            argv = ["sh", "-c", "if command -v konsole >/dev/null; then konsole -e hedronos-install; "
                               "elif command -v xterm >/dev/null; then xterm -e hedronos-install; "
                               "else echo \'run hedronos-install in a terminal\'; sleep 30; fi"]
        }
        win.pending = kind
        feed.run(argv, 240000)
    }

    readonly property string SELECTION_PATH: "/var/lib/hedron/selected-apps.json"

    function reload() { argvFor("print", "") }
    function toggle(id) { argvFor("toggle", id) }
    function applyBundle(name) { argvFor("bundle", name) }
    function install() { argvFor("install", "") }

    Component.onCompleted: reload()

    Connections {
        target: feed
        function onResultReady(requestId, ok, reply) {
            if (!ok) {
                win.note = "hedron-setup said: " + reply
                return
            }
            if (win.pending !== "print") {          // bundle/toggle/install: re-read the model
                if (win.pending === "install") win.note = "installer launched in a terminal window"
                win.reload()
                return
            }
            let doc
            try { doc = JSON.parse(reply) } catch (e) { win.note = "could not read the catalogue"; return }
            win.apps = doc.apps || []
            win.cats = (doc.categories || []).filter(c => c.count > 0)
            win.selected = doc.selected || []
            win.sizeMib = doc.size_mib || 0
            win.note = doc.selected_count + " app(s) · ~" + doc.size_human + " — Steam is locked on"
        }
    }

    pageRow.header: RowLayout {
        Layout.margins: Kirigami.Units.largeSpacing
        QQC.Label { font.pointSize: 13; font.bold: true; text: "Steam is already ticked — it is the point of this distro."
                            Layout.fillWidth: true }
        QQC.Label { text: win.selected.length + " apps · ~" + Math.round(win.sizeMib / 1024) + " GiB" }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Kirigami.Units.largeSpacing
        spacing: Kirigami.Units.largeSpacing

        RowLayout {
            Layout.fillWidth: true
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                visible: win.note.length > 0
                text: win.note
                type: Kirigami.MessageType.Information
                showCloseButton: true
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            QQC.Label {
                Layout.preferredWidth: 220
                text: "Categories"
                opacity: 0.7
            }
            ListView {
                Layout.preferredWidth: 220
                Layout.fillHeight: true
                model: win.cats
                delegate: Kirigami.ItemDelegate {
                    required property var modelData
                    label: (modelData.name || modelData.id) + " (" + (modelData.count || 0) + ")"
                    onClicked: win.reload()
                }
            }
            GridView {
                id: grid
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                cellWidth: 360
                cellHeight: 132
                model: win.apps
                delegate: Kirigami.AbstractCard {
                    required property var modelData
                    width: grid.cellWidth - 8
                    height: grid.cellHeight - 8
                    contentItem: RowLayout {
                        spacing: Kirigami.Units.largeSpacing
                        QQC.CheckBox {
                            checked: !!(modelData.selected)
                            enabled: !(modelData.locked)
                            onToggled: win.toggle(modelData.app || modelData.id)
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            QQC.Label {
                                text: (modelData.name || modelData.app) + (modelData.locked ? "  🔒" : "")
                                font.bold: true
                            }
                            QQC.Label {
                                Layout.fillWidth: true
                                text: modelData.summary || ""
                                wrapMode: Text.WordWrap
                                opacity: 0.7
                                elide: Text.ElideRight
                                maximumLineCount: 3
                            }
                        }
                    }
                }
            }
        }
    }

    footer: QQC.ToolBar {
        RowLayout {
            anchors.fill: parent
            anchors.margins: Kirigami.Units.smallSpacing
            QQC.Button {
                text: "Gaming essentials"
                onClicked: win.argvFor("bundle", "gaming-essentials")
            }
            QQC.Button {
                text: "Reset to defaults"
                onClicked: { win.argvFor("print", ""); win.argvFor("toggle", "steam") }
            }
            Item { Layout.fillWidth: true }
            QQC.Button {
                text: "Install HedronOS"
                onClicked: win.install()
            }
        }
    }
}
