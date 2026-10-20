import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Wayland

ShellRoot {
    id: root

    // ---------- config (tweak me) ----------
    property string edge: "right"          // right | top | bottom
    property int orbSize: 64
    property int panelW: 360
    property int panelH: 480
    property real idleOpacity: 0.35
    property string accent: "#7aa2f7"
    property color bgCol: Qt.rgba(0x16/255, 0x16/255, 0x1e/255, 0.85)
    property string apiUrl: "http://127.0.0.1:8000/chat"

    // ---------- state ----------
    property string mode: "idle"           // idle | peek | open
    property int userPos: 120              // offset along the edge
    property bool thinking: false

    readonly property bool rightE: edge === "right"
    readonly property bool topE: edge === "top"
    readonly property bool bottomE: edge === "bottom"
    readonly property int idleW: Math.round(orbSize * 0.6)

    // how many px of the window sit on-screen (perpendicular to the edge)
    property int visLen: mode === "open" ? (rightE ? panelW : panelH)
                        : mode === "peek" ? orbSize + 4
                        : idleW
    Behavior on visLen { NumberAnimation { duration: 300; easing.type: Easing.OutBack; easing.overshoot: 1.1 } }

    // window is FIXED size — never resizes, so no per-frame buffer realloc
    readonly property int offAmt: (rightE ? panelW : panelH) - visLen

    function closePanel() { mode = "idle"; }

    function post(url, body, cb) {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", url, true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4) cb(xhr.status, xhr.responseText);
        };
        xhr.send(JSON.stringify(body));
    }

    function send() {
        const t = input.text.trim();
        if (!t) return;
        chatModel.append({ role: "user", text: t });
        input.text = "";
        thinking = true;
        idleTimer.restart();
        post(apiUrl, { message: t }, (status, body) => {
            thinking = false;
            let reply;
            if (status === 200) {
                reply = body;
                try {
                    const j = JSON.parse(body);
                    reply = j.reply ?? j.response ?? j.message ?? j.content ?? body;
                } catch (e) {}
            } else {
                reply = "⚠️ " + status + " — " + String(body).slice(0, 160);
            }
            chatModel.append({ role: "assistant", text: String(reply) });
        });
    }

    Timer {
        id: idleTimer
        interval: 15000
        onTriggered: if (root.mode === "open") root.closePanel()
    }

    PanelWindow {
        id: win

        WlrLayershell.layer: WlrLayer.Top
        WlrLayershell.namespace: "sable-overlay"
        exclusiveZone: -1
        color: "transparent"

        anchors {
            top: root.rightE || root.topE
            bottom: root.bottomE
            right: root.rightE
            left: root.topE || root.bottomE
        }
        margins {
            top: root.topE ? -root.offAmt : (root.rightE ? root.userPos : 0)
            bottom: root.bottomE ? -root.offAmt : 0
            right: root.rightE ? -root.offAmt : 0
            left: root.rightE ? 0 : root.userPos
        }

        implicitWidth: root.panelW
        implicitHeight: root.panelH

        Rectangle {
            id: surface
            anchors.fill: parent
            opacity: root.mode === "idle" ? root.idleOpacity : 1.0
            Behavior on opacity { NumberAnimation { duration: 300 } }
            radius: 18
            color: root.bgCol
            border.color: Qt.rgba(1, 1, 1, 0.08)
            border.width: 1

            // ---------- orb ----------
            Item {
                id: orb
                width: root.orbSize
                height: root.orbSize
                // hug the side that's actually on-screen
                x: root.rightE ? 0
                               : Math.min(Math.max(root.userPos, 0), root.panelW - root.orbSize)
                y: root.rightE ? 0
                              : root.topE ? root.panelH - root.orbSize : 0

                MouseArea {
                    id: orbMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.OpenHandCursor

                    property int startPos
                    property bool dragged: false

                    onContainsMouseChanged: {
                        if (containsMouse && root.mode === "idle") root.mode = "peek";
                        else if (!containsMouse && root.mode === "peek") root.mode = "idle";
                    }

                    onPressed: (mouse) => {
                        startPos = root.rightE ? mouse.y : mouse.x;
                        dragged = false;
                        cursorShape = Qt.ClosedHandCursor;
                    }

                    onPositionChanged: (mouse) => {
                        if (!pressed) return;
                        const delta = (root.rightE ? mouse.y : mouse.x) - startPos;
                        if (Math.abs(delta) > 6) dragged = true;
                        if (!dragged) return;
                        const lim = root.rightE ? (win.screen ? win.screen.height : 1080)
                                                : (win.screen ? win.screen.width : 1920);
                        root.userPos = Math.min(Math.max(root.userPos + delta, 30), lim - root.orbSize - 30);
                        startPos = root.rightE ? mouse.y : mouse.x;
                    }

                    onReleased: {
                        cursorShape = Qt.OpenHandCursor;
                        if (!dragged) root.mode = "open";
                    }
                }

                // glyph hugs the visible side of the orb
                Text {
                    id: orbGlyph
                    x: root.rightE ? 8 : (orb.width - width) / 2
                    y: root.rightE ? (orb.height - height) / 2
                     : root.topE ? orb.height - height - 8 : 8
                    text: root.thinking ? "◌" : "✦"
                    font.pixelSize: root.orbSize * 0.45
                    color: root.accent

                    SequentialAnimation on scale {
                        running: root.thinking
                        loops: Animation.Infinite
                        NumberAnimation { to: 1.25; duration: 500 }
                        NumberAnimation { to: 1.0; duration: 500 }
                    }

                    SequentialAnimation on opacity {
                        running: root.mode === "idle"
                        loops: Animation.Infinite
                        NumberAnimation { to: 0.45; duration: 1200; easing.type: Easing.InOutSine }
                        NumberAnimation { to: 1.0; duration: 1200; easing.type: Easing.InOutSine }
                        onRunningChanged: if (!running) orbGlyph.opacity = 1.0
                    }
                }
            }

            // ---------- panel ----------
            Column {
                id: panelLayer
                property real slide: root.mode === "open" ? 0 : 10
                Behavior on slide { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }
                transform: Translate { y: panelLayer.slide }
                anchors.fill: parent
                anchors.margins: 12
                spacing: 8
                opacity: root.mode === "open" ? 1 : 0
                enabled: root.mode === "open"
                visible: opacity > 0.02
                Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }

                // header — doubles as drag handle
                Item {
                    width: parent.width
                    height: 30

                    MouseArea {
                        id: dragHandle
                        anchors.fill: parent
                        cursorShape: Qt.SizeAllCursor

                        property int startPos

                        onPressed: (mouse) => startPos = root.rightE ? mouse.y : mouse.x
                        onPositionChanged: (mouse) => {
                            if (!pressed) return;
                            const delta = (root.rightE ? mouse.y : mouse.x) - startPos;
                            const lim = root.rightE ? (win.screen ? win.screen.height : 1080)
                                                    : (win.screen ? win.screen.width : 1920);
                            root.userPos = Math.min(Math.max(root.userPos + delta, 30), lim - root.panelH - 30);
                            startPos = root.rightE ? mouse.y : mouse.x;
                        }
                    }

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.left: parent.left
                        text: "✦ sable"
                        color: root.accent
                        font.pixelSize: 13
                        font.bold: true
                    }

                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.right: parent.right
                        width: 22; height: 22; radius: 11
                        color: closeMa.containsMouse ? Qt.rgba(1, 0.3, 0.35, 0.25) : "transparent"

                        Text {
                            anchors.centerIn: parent
                            text: "✕"
                            color: "#8899aa"
                            font.pixelSize: 11
                        }
                        MouseArea { id: closeMa; anchors.fill: parent; hoverEnabled: true; onClicked: root.closePanel() }
                    }
                }

                // messages
                ListView {
                    id: chatList
                    width: parent.width
                    height: parent.height - 30 - inputRow.height - 16
                    model: ListModel { id: chatModel }
                    clip: true
                    spacing: 6

                    delegate: Item {
                        width: chatList.width
                        height: bubble.height + 4

                        Rectangle {
                            id: bubble
                            width: Math.min(bubbleText.implicitWidth + 20, chatList.width * 0.85)
                            height: bubbleText.implicitHeight + 14
                            radius: 10
                            anchors.right: model.role === "user" ? parent.right : undefined
                            anchors.left: model.role === "user" ? undefined : parent.left
                            color: model.role === "user" ? Qt.rgba(0.48, 0.63, 0.97, 0.22) : Qt.rgba(1, 1, 1, 0.06)

                            Text {
                                id: bubbleText
                                anchors.centerIn: parent
                                width: Math.min(implicitWidth, chatList.width * 0.85 - 20)
                                text: model.text
                                wrapMode: Text.Wrap
                                color: model.role === "user" ? "#cdd9ff" : "#dfe6f0"
                                font.pixelSize: 12
                            }
                        }
                    }

                    onCountChanged: positionViewAtEnd()
                }

                Row {
                    id: inputRow
                    width: parent.width
                    spacing: 6

                    TextField {
                        id: input
                        width: parent.width - 84
                        placeholderText: "talk to him…"
                        font.pixelSize: 12
                        color: "#e8eef7"
                        background: Rectangle {
                            radius: 10
                            color: Qt.rgba(1, 1, 1, 0.05)
                            border.color: input.activeFocus ? root.accent : Qt.rgba(1, 1, 1, 0.1)
                        }
                        Keys.onReturnPressed: root.send()
                        Keys.onEnterPressed: root.send()
                        Keys.onEscapePressed: root.closePanel()
                    }

                    Button {
                        id: micBtn
                        width: 34; height: 34
                        flat: true
                        contentItem: Text { text: "🎤"; anchors.centerIn: parent; font.pixelSize: 14 }
                        background: Rectangle {
                            radius: 10
                            color: micBtn.pressed ? Qt.rgba(1, 1, 1, 0.12) : Qt.rgba(1, 1, 1, 0.06)
                        }
                        onClicked: chatModel.append({ role: "assistant", text: "🎤 voice not wired yet — tomorrow 😏" })
                    }

                    Button {
                        id: sendBtn
                        width: 34; height: 34
                        flat: true
                        contentItem: Text { text: "➤"; anchors.centerIn: parent; color: root.accent; font.pixelSize: 13 }
                        background: Rectangle {
                            radius: 10
                            color: Qt.rgba(0.48, 0.63, 0.97, 0.2)
                        }
                        onClicked: root.send()
                    }
                }
            }
        }
    }

    Shortcut {
        sequence: "Esc"
        enabled: root.mode === "open"
        onActivated: root.closePanel()
    }
}
