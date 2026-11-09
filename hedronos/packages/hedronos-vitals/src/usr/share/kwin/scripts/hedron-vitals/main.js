// HedronOS vitals KWin script: the shortcut, nothing else. SPDX-License-Identifier: MIT
//
// Deliberately tiny and deliberately not a renderer. Drawing a HUD inside the compositor means
// an effect + a shader + a lifetime problem when the GPU driver misbehaves; instead this script
// flips a flag file and the Plasma applet (and `hedron-vitals watch` in a terminal) read it.
// KWin scripting's job here is exactly one thing: a global key that works even when no panel is
// running and the game owns the screen.
"use strict";

function register(name, text, seq, cb) {
    // KWin 6 exposes registerShortcut() at script scope; older builds only had
    // workspace.registerShortcut. Bind whichever exists instead of writing a session-breaking
    // assumption into a boot-time script.
    if (typeof registerShortcut === "function") {
        registerShortcut(name, text, seq, cb);
        return true;
    }
    if (workspace && typeof workspace.registerShortcut === "function") {
        workspace.registerShortcut(name, text, seq, cb);
        return true;
    }
    return false;
}

function toggleHud() {
    spawn("/usr/bin/hedron-vitals", ["toggle"]);
}

function toggleGameLock() {
    // game-on/game-off are hedron-tune verbs; the script only remembers which way it last went,
    // so a second press always returns the machine to normal.
    if (typeof gameLockOn === "undefined") {
        gameLockOn = false;
    }
    gameLockOn = !gameLockOn;
    spawn("/usr/bin/hedron-tune", [gameLockOn ? "game-on" : "game-off"]);
}

function saveClip() {
    spawn("/usr/bin/hedron-replay", ["save"]);
}

let gameLockOn = false;

function init() {
    const bindings = [
        ["hedron-vitals-toggle", "Toggle the HedronOS vitals HUD", "Meta+V", toggleHud],
        ["hedron-game-lock", "Toggle HedronOS gaming lock (scheduler + clocks)", "Meta+G", toggleGameLock],
        ["hedron-replay-save", "Save the last 30 seconds of gameplay", "Meta+Shift+R", saveClip]
    ];
    let bound = 0;
    for (const [name, text, seq, cb] of bindings) {
        try {
            if (register(name, text, seq, cb)) {
                bound += 1;
            }
        } catch (e) {
            // A conflicting shortcut (someone else grabbed Meta+V) must not break the session.
            print("hedron-vitals: could not bind " + name + ": " + e);
        }
    }
    print("hedron-vitals: " + bound + "/" + bindings.length + " global shortcut(s) active");
}

init();
