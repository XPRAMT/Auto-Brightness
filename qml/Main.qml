import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: root
    width: 980
    height: 700
    visible: true
    title: "Brightness (QML)"

    color: "#111318"

    property color accent: "#4CC2FF"
    property color panel: "#1A1D24"
    property color panel2: "#20242D"
    property color textPrimary: "#F4F7FF"
    property color textSecondary: "#AAB4C5"

    function winUiShadowColor(baseAlpha) {
        return Qt.rgba(0, 0, 0, baseAlpha)
    }

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#111318" }
            GradientStop { position: 1.0; color: "#0B0D12" }
        }
    }

    header: Rectangle {
        height: 74
        color: "transparent"

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 26
            anchors.rightMargin: 26
            spacing: 16

            ColumnLayout {
                spacing: 2
                Label {
                    text: "Brightness Control"
                    color: root.textPrimary
                    font.pixelSize: 27
                    font.bold: true
                    font.family: "Segoe UI"
                }
                Label {
                    text: "Python + QML (WinUI 3 風格)"
                    color: root.textSecondary
                    font.pixelSize: 13
                    font.family: "Segoe UI"
                }
            }

            Item { Layout.fillWidth: true }

            Button {
                text: "重新掃描"
                font.family: "Segoe UI"
                font.pixelSize: 13
                highlighted: true
                onClicked: backend.refreshMonitors()
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: 88
        anchors.bottomMargin: 16
        anchors.leftMargin: 20
        anchors.rightMargin: 20
        spacing: 14

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 132
            radius: 16
            color: root.panel
            border.width: 1
            border.color: "#2A2E38"

            layer.enabled: true

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 10

                Label {
                    text: "全域亮度"
                    color: root.textPrimary
                    font.pixelSize: 16
                    font.bold: true
                    font.family: "Segoe UI"
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10

                    Slider {
                        id: globalSlider
                        Layout.fillWidth: true
                        from: 0
                        to: 100
                        stepSize: 1
                        value: backend.globalBrightness
                        onMoved: backend.setAllBrightness(Math.round(value))
                    }

                    Label {
                        text: Math.round(globalSlider.value) + "%"
                        color: root.accent
                        font.pixelSize: 24
                        font.bold: true
                        font.family: "Segoe UI"
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 16
            color: root.panel2
            border.width: 1
            border.color: "#2A2E38"

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 12

                Label {
                    text: "顯示器"
                    color: root.textPrimary
                    font.pixelSize: 16
                    font.bold: true
                    font.family: "Segoe UI"
                }

                ListView {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 10
                    clip: true
                    model: backend.monitors

                    delegate: Rectangle {
                        required property var modelData
                        width: ListView.view.width
                        height: 96
                        radius: 12
                        color: "#171A21"
                        border.width: 1
                        border.color: "#2A2E38"

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 14
                            spacing: 12

                            ColumnLayout {
                                Layout.preferredWidth: 250
                                spacing: 2

                                Label {
                                    text: modelData.name
                                    color: root.textPrimary
                                    font.pixelSize: 15
                                    font.bold: true
                                    font.family: "Segoe UI"
                                    elide: Label.ElideRight
                                }

                                Label {
                                    text: "DDC/CI 亮度控制"
                                    color: root.textSecondary
                                    font.pixelSize: 12
                                    font.family: "Segoe UI"
                                }
                            }

                            Slider {
                                id: monitorSlider
                                Layout.fillWidth: true
                                from: 0
                                to: 100
                                stepSize: 1
                                value: modelData.brightness
                                onMoved: backend.setMonitorBrightness(modelData.index, Math.round(value))
                            }

                            Label {
                                text: Math.round(monitorSlider.value) + "%"
                                color: root.accent
                                font.pixelSize: 20
                                font.bold: true
                                font.family: "Segoe UI"
                                Layout.preferredWidth: 70
                                horizontalAlignment: Text.AlignRight
                            }
                        }
                    }

                    footer: Item {
                        width: ListView.view.width
                        height: backend.monitors.length === 0 ? 100 : 10

                        Label {
                            anchors.centerIn: parent
                            visible: backend.monitors.length === 0
                            text: "目前沒有可控制的螢幕"
                            color: root.textSecondary
                            font.pixelSize: 14
                            font.family: "Segoe UI"
                        }
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 40
            color: "transparent"

            Label {
                anchors.verticalCenter: parent.verticalCenter
                anchors.left: parent.left
                text: backend.statusMessage
                color: root.textSecondary
                font.pixelSize: 12
                font.family: "Segoe UI"
                elide: Label.ElideRight
                width: parent.width
            }
        }
    }
}
