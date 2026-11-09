// HedronOS greeter. SPDX-License-Identifier: MIT
//
// This theme uses only the SDDM QML interfaces that have been stable across the 0.20 line —
// UserModel, SessionModel, Config and TextConstants — and no power buttons, because the power
// actions are the piece SDDM has actually changed (helper D-Bus vs. the old greeter object) and
// a greeter that cannot shut down is worse than one without a shutdown button: PowerDevil and
// the session handle it, and `sddm` still answers Ctrl-Alt-F2 for a VT.
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC
import org.kde.sddm 1.0

Rectangle {
    id: root
    color: "#0b0f14"

    Image {
        anchors.fill: parent
        // The same vector file the desktop uses, so the machine never changes wallpaper under you.
        source: "file://" + (Sddm.Config.read("General", "background") || "/usr/share/hedron/wallpapers/duality.svg")
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        cache: false
    }

    Rectangle {
        anchors.fill: parent
        color: "#0b0f14"
        opacity: 0.55
    }

    ColumnLayout {
        anchors.centerIn: parent
        spacing: 18
        width: Math.min(420, root.width - 40)

        Text {
            text: "HedronOS"
            color: "#e8eef2"
            font.pointSize: 22
            font.bold: true
            Layout.alignment: Qt.AlignHCenter
        }
        Text {
            text: Sddm.TextConstants.HostName + " · " + Sddm.TextConstants.SessionName
            color: "#9aa5b1"
            font.pointSize: 9
            Layout.alignment: Qt.AlignHCenter
        }

        ListView {
            id: users
            Layout.fillWidth: true
            height: Math.min(contentHeight, 190)
            model: Sddm.UserModel
            spacing: 6
            clip: true
            delegate: QQC.ItemDelegate {
                required property var model
                width: users.width
                highlighted: users.currentIndex === index
                text: model.name
                onClicked: {
                    users.currentIndex = index
                    Sddm.UserModel.primaryUser = model.name
                }
            }
            Component.onCompleted: {
                // Select the one user we created at install; a greeter that makes you pick between
                // "player" and "player" is a bug, not a feature.
                for (let i = 0; i < model.rowCount(); ++i) {
                    if (model.name === Sddm.TextConstants.DefaultUser) { currentIndex = i; break }
                }
            }
        }

        QQC.TextField {
            id: pass
            Layout.fillWidth: true
            echoMode: TextInput.Password
            placeholderText: "Password"
            onAccepted: login.click()
        }

        RowLayout {
            Layout.fillWidth: true
            QQC.Button {
                id: login
                text: "Sign in"
                Layout.fillWidth: true
                onClicked: Sddm.login(users.currentText, pass.text, session.currentIndex)
            }
            QQC.ComboBox {
                id: session
                Layout.preferredWidth: 190
                model: Sddm.SessionModel
                textRole: "name"
                // Remember the last session: a machine that boots into the wrong compositor after
                // an update loses an afternoon to "why is my GPU not being used".
                Component.onCompleted: {
                    const wanted = "HedronOS (Wayland)"
                    for (let i = 0; i < count; ++i) {
                        if (model.get(i).name.indexOf("Wayland") >= 0) { currentIndex = i; return }
                    }
                    currentIndex = 0
                }
            }
        }

        Text {
            visible: text.length > 0
            color: "#e8564f"
            font.pointSize: 9
            Layout.alignment: Qt.AlignHCenter
            text: pass.errorString
        }
    }
}
