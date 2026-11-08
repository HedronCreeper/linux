// HedronOS desktop keys — HDR, overlay preset, brightness. SPDX-License-Identifier: MIT
//
// The vitals script (hedronos-vitals) owns Meta+V/G/Shift+R; this one owns the desktop-facing
// keys. Each handler shells out to a tool that exists on every HedronOS install, so the keys
// keep working from Sway and Hyprland too — KWin is only the thing that notices the press.
"use strict";

let overlayIndex = -1;
let hdrOn = false;

function register(name, text, seq, cb) {
    if (typeof registerShortcut === "function") { registerShortcut(name, text, seq, cb); return true }
    if (workspace && typeof workspace.registerShortcut === "function") {
        workspace.registerShortcut(name, text, seq, cb); return true
    }
    return false
}

function overlayPresetList() {
    // `hedron-overlay list` prints "PRESET SOURCE NOTE" lines; we only need the first column.
    // The list is small and changes rarely, so it is fine to re-read it per keypress.
    return []
}

function nextPreset() {
    spawn("/usr/bin/hedron-overlay", ["next"]);
}

function toggleHdr() {
    hdrOn = !hdrOn;
    spawn("/usr/bin/hedron-session", ["hdr", hdrOn ? "on" : "off"]);
}

function brightnessUp() { spawn("/usr/bin/hedron-handheld", ["brightness", "+5"]); }
function brightnessDown() { spawn("/usr/bin/hedron-handheld", ["brightness", "-5"]); }

function init() {
    const bindings = [
        ["hedron-overlay-next", "Next HedronOS tuning preset", "Meta+O", nextPreset],
        ["hedron-hdr", "Toggle HDR output", "Meta+H", toggleHdr],
        ["hedron-bright-up", "Panel brightness up", "XF86MonBrightnessUp", brightnessUp],
        ["hedron-bright-down", "Panel brightness down", "XF86MonBrightnessDown", brightnessDown]
    ];
    let bound = 0;
    for (const [name, text, seq, cb] of bindings) {
        try { if (register(name, text, seq, cb)) bound += 1 } catch (e) {
            print("hedron-keys: " + name + " not bound: " + e)
        }
    }
    print("hedron-keys: " + bound + "/" + bindings.length + " shortcut(s)")
}

init();
