// HedronOS Store — the GUI window. SPDX-License-Identifier: MIT
//
// Deliberately thin: every decision (what is installed, what a transaction will do, whether the
// index is trusted) comes from `hstore --json`. That is what lets the terminal and the GUI show
// the same plan and the same undo, and it means this file can be thrown away without touching
// store semantics. The only privileged type it uses is HedronFeed (see hedronos/qt).
import QtQuick
import QtQuick.Controls as QQC
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import local.hedron.feed 1.0

Kirigami.ApplicationWindow {
    id: win
    width: 1100
    height: 720
    title: "HedronOS Store"

    property var model: []
    property var cats: []
    property string query: ""
    property string category: ""
    property bool busy: false
    property string status: ""
    property string pendingOp: ""     // "search" | "install" | "status"

    function argvFor(kind, id) {
        let argv = ["hstore", "--json"]
        if (kind === "search") {
            argv = ["hstore", "search", "--json", "--limit", "400"]
            if (win.query.length) argv.push(win.query)
            if (win.category.length) argv.push("--category", win.category)
        } else if (kind === "install") {
            argv = ["hstore", "install", "--json", "--yes", id]
        } else if (kind === "categories") {
            argv = ["hstore", "categories", "--json"]
        } else {
            argv = ["hstore", "status", "--json"]
        }
        win.pendingOp = kind
        win.busy = true
        feed.run(argv, 180000)
    }

    Kirigami.Page {
        title: "Apps"

        ColumnLayout {
            anchors.fill: parent
            spacing: Kirigami.Units.smallSpacing

            RowLayout {
                Layout.fillWidth: true
                QQC.Label { text: "Install games and apps from one signed index — pacman, Flatpak, AUR, AppImage"
                            opacity: 0.75; Layout.fillWidth: true }
                QQC.Label { text: win.status }
                QQC.BusyIndicator { visible: win.busy; running: win.busy }
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: Kirigami.Units.largeSpacing

                ListView {
                    id: cats
                    Layout.preferredWidth: 190
                    Layout.fillHeight: true
                    clip: true
                    model: win.cats
                    delegate: Kirigami.ItemDelegate {
                        required property var modelData
                        width: cats.width
                        label: (modelData.name || modelData.category) + "  (" + (modelData.count || 0) + ")"
                        highlighted: win.category === (modelData.id || modelData.category)
                        onClicked: {
                            win.category = (modelData.id || modelData.category) === win.category ? "" : (modelData.id || modelData.category)
                            win.argvFor("search", "")
                        }
                    }
                }

                ListView {
                    id: list
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: Kirigami.Units.smallSpacing
                    model: win.model

                    delegate: Kirigami.AbstractCard {
                        required property var modelData
                        width: list.width
                        contentItem: RowLayout {
                            spacing: Kirigami.Units.largeSpacing

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                QQC.Label {
                                    text: (modelData.name || modelData.id) +
                                          ((modelData.flags || []).includes("locked") ? "  🔒" : "")
                                    font.pointSize: 13
                                    font.bold: true
                                }
                                QQC.Label {
                                    Layout.fillWidth: true
                                    text: modelData.summary || ""
                                    wrapMode: Text.WordWrap
                                    opacity: 0.75
                                }
                                QQC.Label {
                                    text: (modelData.source || "?") + ": " + (modelData.provider || "") +
                                          " · " + (modelData.tier || "community") +
                                          " · ~" + Math.round(modelData.size_mib || 0) + " MiB"
                                    font.pointSize: 8
                                    opacity: 0.6
                                }
                            }

                            QQC.Button {
                                enabled: !win.busy && !((modelData.flags || []).includes("locked"))
                                text: ((modelData.flags || []).includes("installed") ? "Install anyway" : "Install")
                                onClicked: win.argvFor("install", modelData.id)
                            }
                        }
                    }
                }
            }
        }
    }

    pageRow.header: RowLayout {
        Layout.margins: Kirigami.Units.smallSpacing
        Kirigami.SearchField {
            id: search
            Layout.fillWidth: true
            placeholderText: "Search games, tools, emulators, launchers…"
            onTextChanged: { win.query = text; win.argvFor("search", "") }
        }
        QQC.Button {
            flat: true
            text: "All"
            onClicked: { win.category = ""; win.argvFor("search", "") }
        }
    }

    footer: QQC.ToolBar {
        RowLayout {
            anchors.fill: parent
            anchors.margins: Kirigami.Units.smallSpacing
            QQC.Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                opacity: 0.6
                text: "Same signed catalogue the installer used. Every transaction is journaled — "
                      + "`hstore undo` in a terminal reverts it."
            }
            QQC.Button { text: "Refresh index"; onClicked: feed.run(["hstore", "update", "--json"], 600000) }
        }
    }

    Component.onCompleted: { argvFor("categories", ""); argvFor("search", "") }

    // HedronFeed is a single-flight bridge: one request at a time, routed by what we asked for.
    Connections {
        target: feed
        function onResultReady(requestId, ok, reply) {
            win.busy = false
            if (!ok && win.pendingOp !== "search") {
                win.status = "hstore said: " + reply
                return
            }
            let doc
            try {
                doc = JSON.parse(reply)
            } catch (e) {
                return          // pacman progress text and the like: shown, never fatal
            }
            if (win.pendingOp === "search") {
                win.model = doc.results || []
                win.status = (doc.total !== undefined ? doc.total : win.model.length) + " app(s)"
            } else if (win.pendingOp === "categories") {
                win.cats = doc.categories || []
            } else if (win.pendingOp === "install") {
                win.status = doc.ok ? ("installed " + (doc.applied || []).length + " step(s)")
                                     : ("not applied: " + (doc.reason || "unknown"))
                win.argvFor("search", "")
            } else {
                win.status = doc.ok ? "store is healthy" : "store problems: " + (doc.problems || []).join(", ")
            }
        }
    }
}
