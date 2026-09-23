import QtQuick

// Sparse bursts of characters flashing on in random places across the
// whole overlay — the ambient noise floor of the HUD, and the thing that
// makes the screen feel live between tool calls.
//
// Deliberately NOT a uniform Matrix grid of falling columns: short runs
// of one to six glyphs, at random sizes and positions, each scrambling
// before it settles and then fading out slowly. At most `maxBlips` are
// alive at once.
//
// The noise stays accent-blue whatever the conversation state is — only a
// failed tool call recolors it. That keeps "something broke" the single
// loudest thing on screen instead of one signal among several.
Canvas {
    id: rain
    required property bool active
    // Last tool result failed: denser bursts, crossed glyphs, red.
    property bool broken: false
    // Nothing is being heard — stop spawning, but let what is on screen
    // finish fading rather than blinking out.
    property bool idle: false
    property color accent: "#4493f8"
    property color errColor: "#f85149"

    readonly property string rainChars: "01/#%<>[]{}+=_*:;!?"
    // No ✖ (U+2716) here: it carries the Unicode Emoji property, so
    // fontconfig serves it from Noto Color Emoji as a bitmap, and a bitmap
    // glyph ignores fillStyle — it renders grey in an otherwise red row.
    readonly property string errChars: "╳╳╳✕✕╳╱╲"
    readonly property int maxBlips: 70

    property var blips: []

    onActiveChanged: {
        if (active) {
            ticks.start()
        } else {
            ticks.stop()
            blips = []
            requestPaint()
        }
    }
    Component.onCompleted: if (active) ticks.start()

    function spawn(now) {
        if (Math.random() > (broken ? 0.55 : 0.18)) return
        if (width < 200 || height < 120) return
        var set = broken ? errChars : rainChars
        var runLength = 1 + Math.floor(Math.random() * (broken ? 6 : 4))
        var text = ""
        for (var i = 0; i < runLength; i++)
            text += set.charAt(Math.floor(Math.random() * set.length))
        var next = blips.slice()
        next.push({
            x: 20 + Math.random() * (width - 140),
            y: 24 + Math.random() * (height - 60),
            text: text, set: set,
            size: 11 + Math.floor(Math.random() * 9),
            born: now, life: 700 + Math.random() * 1400
        })
        if (next.length > maxBlips) next = next.slice(next.length - maxBlips)
        blips = next
    }

    Timer {
        id: ticks
        interval: 42
        repeat: true
        onTriggered: {
            var now = Date.now()
            if (!rain.idle) rain.spawn(now)
            var alive = []
            for (var i = 0; i < rain.blips.length; i++)
                if (now - rain.blips[i].born < rain.blips[i].life) alive.push(rain.blips[i])
            rain.blips = alive
            rain.requestPaint()
        }
    }

    onPaint: {
        var ctx = getContext("2d")
        ctx.reset()
        if (!active) return
        var now = Date.now()
        ctx.fillStyle = broken ? errColor : accent
        ctx.textAlign = "left"
        for (var i = 0; i < blips.length; i++) {
            var b = blips[i]
            var p = (now - b.born) / b.life
            if (p < 0 || p > 1) continue
            ctx.font = b.size + "px monospace"
            // Quick flash in, slow fade out.
            ctx.globalAlpha = (p < 0.12 ? p / 0.12 : 1 - (p - 0.12) / 0.88) * 0.45
            // Each burst scrambles before it settles, like the wordmark does.
            var text = b.text
            if (p < 0.35) {
                var resolved = Math.floor((p / 0.35) * b.text.length)
                text = ""
                for (var c = 0; c < b.text.length; c++)
                    text += c < resolved ? b.text.charAt(c)
                                         : b.set.charAt(Math.floor(Math.random() * b.set.length))
            }
            ctx.fillText(text, b.x, b.y)
        }
    }
}
