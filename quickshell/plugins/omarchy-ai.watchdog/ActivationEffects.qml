import QtQuick

// Additive, input-transparent artwork. Timers sleep between glitch bursts.
Canvas {
    id: art
    required property bool active
    property int activationNonce: 0
    property color accent: "#39e6ff"
    property var particles: []
    property var glitches: []
    property real started: 0
    property real burstStarted: 0
    // Same block/braille vocabulary as the live voice visualizer.
    // Broad but coherent terminal alphabet: the same meter blocks/braille,
    // plus shaded cells and a few small console separators for scrambling.
    readonly property string noise: " ▁▂▃▄▅▆▇█⠂⠁⠃⠇⡇⡏⡟⡿⣿▓▒░╳┃╎·•:;+=<>[]{}01"
    readonly property var logo: [
        "                 ▄▄▄",
        " ▄█████▄    ▄███████████▄    ▄███████   ▄███████   ▄███████   ▄█   █▄    ▄█   █▄",
        "███   ███  ███   ███   ███  ███   ███  ███   ███  ███   ███  ███   ███  ███   ███",
        "███   ███  ███   ███   ███  ███   ███  ███   ███  ███   █▀   ███   ███  ███   ███",
        "███   ███  ███   ███   ███ ▄███▄▄▄███ ▄███▄▄▄██▀  ███       ▄███▄▄▄███▄ ███▄▄▄███",
        "███   ███  ███   ███   ███ ▀███▀▀▀███ ▀███▀▀▀▀    ███      ▀▀███▀▀▀███  ▀▀▀▀▀▀███",
        "███   ███  ███   ███   ███  ███   ███ ██████████  ███   █▄   ███   ███  ▄██   ███",
        "███   ███  ███   ███   ███  ███   ███  ███   ███  ███   ███  ███   ███  ███   ███",
        " ▀█████▀    ▀█   ███   █▀   ███   █▀   ███   ███  ███████▀   ███   █▀    ▀█████▀",
        "                                       ███   █▀",
        "",
        "                         [o_o]",
        "                       <| AI |>"
    ]

    function begin() {
        var points = []
        for (var row = 0; row < logo.length; row++) {
            for (var col = 0; col < logo[row].length; col++) {
                if (logo[row][col] === " ") continue
                var angle = Math.atan2(row - 6, (col - 23) * 0.5) + (Math.random() - 0.5) * 1.2
                points.push({ch: logo[row][col], col: col, row: row,
                    dx: Math.cos(angle) * (250 + Math.random() * 650),
                    dy: Math.sin(angle) * (250 + Math.random() * 650),
                    // Each character exits at a different time, like a
                    // terminal text-scramble rather than a fade animation.
                    delay: Math.random() * 4200})
            }
        }
        particles = points
        glitches = []
        started = Date.now()
        frames.start()
        scheduleGlitch()
    }
    function scheduleGlitch() {
        // Several short bursts per turn: the HUD feels active without
        // becoming a permanent wall of noise.
        // Once the identity has faded, only rare, tiny meter-like glitches
        // remain: two or three characters every five to eight seconds.
        nextGlitch.interval = 5000 + Math.random() * 3000
        nextGlitch.restart()
    }
    onActivationNonceChanged: if (active) begin()
    onActiveChanged: {
        if (active) begin()
        else {
            frames.stop()
            nextGlitch.stop()
            particles = []
            glitches = []
            requestPaint()
        }
    }
    Component.onCompleted: if (active) begin()

    Timer {
        id: nextGlitch
        onTriggered: {
            if (!art.active) return
            var fragments = []
            // One group per burst, always two or three adjacent characters.
            fragments.push({
                length: 2 + Math.floor(Math.random() * 2),
                x: 0.08 + Math.random() * 0.84,
                y: 0.12 + Math.random() * 0.72,
                phase: Math.random() * Math.PI * 2,
                size: 0.7 + Math.random() * 0.7
            })
            art.glitches = fragments
            art.burstStarted = Date.now()
            frames.start()
            art.scheduleGlitch()
        }
    }
    Timer {
        id: frames
        interval: 33
        repeat: true
        onTriggered: {
            // All logo characters have completed their two scramble frames.
            if (Date.now() - art.started > 6200) art.particles = []
            if (Date.now() - art.burstStarted > 520) art.glitches = []
            art.requestPaint()
            if (!art.particles.length && !art.glitches.length) stop()
        }
    }
    onPaint: {
        var ctx = getContext("2d")
        ctx.reset()
        if (!active) return
        var size = Math.max(7, Math.min(15, width / 78, height / 28))
        var cell = size * 0.61
        var columns = 0
        for (var row = 0; row < logo.length; row++) columns = Math.max(columns, logo[row].length)
        var left = (width - columns * cell) / 2
        var top = (height - logo.length * size * 1.2) / 2
        ctx.font = "bold " + size + "px monospace"
        ctx.textBaseline = "top"
        ctx.fillStyle = accent
        var age = Date.now() - started
        for (var i = 0; i < particles.length; i++) {
            var p = particles[i]
            // Fixed-position text scramble: original -> two changing ASCII
            // frames -> gone. No opacity fade and no spatial movement.
            var exitStart = 1200 + p.delay
            if (age < exitStart) {
                ctx.globalAlpha = 1.0
                ctx.fillText(p.ch, left + p.col * cell, top + p.row * size * 1.2)
                continue
            }
            var scrambleStep = Math.floor((age - exitStart) / 160)
            if (scrambleStep >= 2) continue
            var scrambleGlyph = noise[Math.floor(Math.abs(Math.sin((scrambleStep + 1) * 17.17 + p.col * 3.1 + p.row * 7.7)) * noise.length) % noise.length]
            ctx.globalAlpha = 1.0
            ctx.fillText(scrambleGlyph, left + p.col * cell, top + p.row * size * 1.2)
        }
        ctx.font = "bold " + Math.max(11, size) + "px monospace"
        for (var k = 0; k < glitches.length; k++) {
            var g = glitches[k]
            var burstAge = Date.now() - burstStarted
            var burstStep = Math.floor(burstAge / 170)
            if (burstStep >= 3) continue
            ctx.globalAlpha = 1.0
            ctx.fillStyle = accent
            ctx.font = "bold " + Math.max(10, size * g.size) + "px monospace"
            // Adjacent characters form one tiny scramble cluster.
            for (var c = 0; c < 3; c++) {
                var text = noise[Math.floor(Math.abs(Math.sin((burstStep + 1) * 13.7 + g.phase + c * 4.2)) * noise.length) % noise.length]
                ctx.fillText(text, g.x * width + c * size * 0.62, g.y * height)
            }
        }
    }
}
