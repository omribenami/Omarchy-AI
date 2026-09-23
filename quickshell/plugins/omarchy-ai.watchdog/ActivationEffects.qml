import QtQuick
import "Wordmark.js" as Wordmark

// The wake-up: random glyphs rain in from off-screen left and lock into
// the Omarchy wordmark, it holds for a beat, then blows outward in every
// direction, scrambling as it goes.
//
// This replaces the original hand-typed ASCII block logo, which only ever
// scrambled *out* — there was no arrival, and the letterforms were drawn
// by hand rather than taken from the real mark. The wordmark now comes
// from Wordmark.js (the actual logo raster, 252x53), so it is the real
// logo rather than an approximation of it.
//
// Additive, input-transparent artwork. The frame timer runs only for the
// ~3s the sequence lasts and then stops itself.
Canvas {
    id: art
    required property bool active
    property int activationNonce: 0
    property color accent: "#4493f8"

    // Same alphabet the ambient rain scrambles through, so the wake-up and
    // the idle noise read as the same system.
    readonly property string noise: "01/#%<>[]{}+=_*:;!?"

    // Beat boundaries, seconds from the start of the sequence:
    //   0  -> T1  glyphs fly in from the left and decrypt into place
    //   T1 -> T2  the solid mark holds, subtitle under it
    //   T2 -> T3  everything scatters outward and fades
    readonly property real t1: 0.9
    readonly property real t2: 1.75
    readonly property real t3: 2.9

    property var particles: []
    property real started: 0

    function begin() {
        var cells = Wordmark.cells
        var maxCol = 1
        for (var c = 0; c < cells.length; c++) maxCol = Math.max(maxCol, cells[c][0])
        var parts = []
        for (var i = 0; i < cells.length; i++) {
            parts.push({
                cx: cells[i][0], cy: cells[i][1],
                // Every glyph flies in from off-screen left; the left-most
                // columns land first, so the mark decrypts left to right.
                ox: -(0.55 + Math.random() * 0.8),
                oy: (Math.random() - 0.5) * 0.06,
                delay: (cells[i][0] / maxCol) * 0.45 + Math.random() * 0.12,
                ang: Math.random() * Math.PI * 2,
                spd: 0.35 + Math.random(),
                seed: (i * 7919) % 997
            })
        }
        particles = parts
        started = Date.now()
        frames.start()
    }

    onActivationNonceChanged: if (active) begin()
    onActiveChanged: {
        if (active) {
            begin()
        } else {
            frames.stop()
            particles = []
            requestPaint()
        }
    }
    Component.onCompleted: if (active) begin()

    Timer {
        id: frames
        interval: 33
        repeat: true
        onTriggered: {
            if ((Date.now() - art.started) / 1000 > art.t3) {
                art.particles = []
                stop()
            }
            art.requestPaint()
        }
    }

    onPaint: {
        var ctx = getContext("2d")
        ctx.reset()
        if (!active || !particles.length) return

        var age = (Date.now() - started) / 1000
        if (age < 0 || age > t3) return

        var mw = Wordmark.w, mh = Wordmark.h
        var unit = Math.min((width * 0.62) / mw, (height * 0.3) / mh)
        var logoW = mw * unit, logoH = mh * unit
        var x0 = width / 2 - logoW / 2
        var y0 = height * 0.42 - logoH / 2
        var cellPx = Wordmark.step * unit

        ctx.save()
        ctx.textAlign = "center"
        ctx.textBaseline = "middle"
        ctx.font = Math.max(8, cellPx * 1.1) + "px monospace"
        ctx.fillStyle = accent

        // The solid mark, crisp, while it is held.
        var markAlpha = 0
        if (age >= t1 && age < t1 + 0.14) markAlpha = (age - t1) / 0.14
        else if (age >= t1 + 0.14 && age < t2) markAlpha = 1
        else if (age >= t2 && age < t2 + 0.22) markAlpha = 1 - (age - t2) / 0.22

        if (markAlpha > 0) {
            var runs = Wordmark.runs
            // A cheap stand-in for the mock's shadowBlur: the same spans
            // drawn once oversized and faint. Qt's Canvas applies a shadow
            // per fill, and 663 shadowed fills a frame is not worth it.
            var bleed = unit * 1.6
            ctx.globalAlpha = markAlpha * 0.16
            for (var g = 0; g < runs.length; g++) {
                ctx.fillRect(x0 + runs[g][0] * unit - bleed, y0 + runs[g][1] * unit - bleed,
                             runs[g][2] * unit + bleed * 2, unit + bleed * 2)
            }
            ctx.globalAlpha = markAlpha
            for (var r = 0; r < runs.length; r++) {
                ctx.fillRect(x0 + runs[r][0] * unit, y0 + runs[r][1] * unit,
                             runs[r][2] * unit, unit)
            }
        }

        for (var p = 0; p < particles.length; p++) {
            var q = particles[p]
            var tx = x0 + q.cx * cellPx + cellPx / 2
            var ty = y0 + q.cy * cellPx + cellPx / 2
            var sx = tx, sy = ty, alpha = 0, iter = 0
            if (age < t1) {
                var t = Math.max(0, Math.min(1, (age - q.delay) / (t1 - q.delay)))
                var ease = t * t * (3 - 2 * t)
                sx = tx + q.ox * width * (1 - ease)
                sy = ty + q.oy * height * (1 - ease)
                alpha = 0.25 + 0.6 * t
                iter = Math.floor(age / 0.075)          // decrypt churn on the way in
            } else if (age < t2) {
                alpha = Math.max(0, 1 - (age - t1) / 0.18) * 0.7
                iter = Math.floor(age / 0.075)
            } else {
                // Reverse decrypt: exactly five glyph iterations while the
                // mark scatters outward and fades.
                var k = Math.min(1, (age - t2) / (t3 - t2))
                var dist = k * k * Math.max(width, height) * 0.6 * q.spd
                sx = tx + Math.cos(q.ang) * dist
                sy = ty + Math.sin(q.ang) * dist
                alpha = (1 - k) * 0.85
                iter = Math.floor(k * 5)
            }
            if (alpha <= 0.01) continue
            ctx.globalAlpha = alpha
            ctx.fillText(noise.charAt((q.seed + iter * 17) % noise.length), sx, sy)
        }

        if (markAlpha > 0) {
            ctx.globalAlpha = markAlpha * 0.75
            ctx.font = Math.max(10, cellPx * 0.8) + "px monospace"
            ctx.fillText("A I   V O I C E   A S S I S T A N T",
                         width / 2, y0 + logoH + cellPx * 1.4)
        }

        ctx.globalAlpha = (age < t2 ? 1 : Math.max(0, 1 - (age - t2))) * 0.14
        ctx.fillRect(0, ((age % 1.05) / 1.05) * height, width, 2)
        ctx.restore()
    }
}
