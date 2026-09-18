package ai.omarchy.receiver

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.webrtc.RendererCommon
import org.webrtc.SurfaceViewRenderer

/**
 * Full-screen remote video surface with a status/connect overlay that hides
 * itself once a stream is actually flowing (ICE CONNECTED/COMPLETED) --
 * this is what should be showing on the TV once
 * `scripts/spike_cast_sender.py` (or the real encoder) is running.
 */
@Composable
fun ReceiverScreen(viewModel: ReceiverViewModel) {
    val connectionState by viewModel.connectionState.collectAsStateWithLifecycle()
    val remoteVideoTrack by viewModel.remoteVideoTrack.collectAsStateWithLifecycle()
    val microphoneActive by viewModel.microphoneActive.collectAsStateWithLifecycle()
    var rendererRef by remember { mutableStateOf<SurfaceViewRenderer?>(null) }

    Box(modifier = Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { context ->
                SurfaceViewRenderer(context).apply {
                    init(viewModel.eglBase.eglBaseContext, null)
                    setScalingType(RendererCommon.ScalingType.SCALE_ASPECT_FIT)
                    setMirror(false)
                    setEnableHardwareScaler(true)
                    rendererRef = this
                }
            },
            onRelease = { renderer ->
                remoteVideoTrack?.removeSink(renderer)
                renderer.release()
                rendererRef = null
            },
        )

        DisposableEffect(remoteVideoTrack, rendererRef) {
            val renderer = rendererRef
            val track = remoteVideoTrack
            if (renderer != null && track != null) {
                track.addSink(renderer)
            }
            onDispose {
                if (renderer != null && track != null) {
                    track.removeSink(renderer)
                }
            }
        }

        if (!connectionState.isStreaming()) {
            StatusOverlay(connectionState = connectionState)
        }

        if (microphoneActive) {
            Text(
                text = "TV microphone active · Say Omachy",
                color = Color(0xFF39FF88),
                fontSize = 14.sp,
                modifier = Modifier.align(Alignment.TopStart).background(Color.Black.copy(alpha = 0.7f)).padding(12.dp),
            )
        }

        // Build version, always visible (even while actively streaming --
        // unlike the status text below, which hides once mirroring starts)
        // so it can be read off the screen at any time, e.g. via `adb
        // shell screencap`, without needing to interrupt a live cast.
        // Small/dim, top-right corner, deliberately unobtrusive -- see
        // STATUS.md's receiver-version-tracking entry for why this exists
        // (there was previously no on-screen way to tell an old install
        // apart from a freshly built one). BuildConfig.VERSION_NAME is the
        // git-derived value set in app/build.gradle.kts.
        Text(
            text = "v${BuildConfig.VERSION_NAME}",
            color = Color.White.copy(alpha = 0.35f),
            fontSize = 12.sp,
            modifier = Modifier.align(Alignment.TopEnd).padding(12.dp),
        )
    }
}

@Composable
private fun StatusOverlay(connectionState: CastConnectionState) {
    Box(modifier = Modifier.fillMaxSize()) {
        // Omarchy's own Catppuccin wallpaper as the waiting-screen
        // background (user request -- was plain black before). Sourced
        // from /usr/share/omarchy/themes/catppuccin/backgrounds/omarchy.png
        // on the desktop machine, copied in as a drawable resource. Crop
        // (not Fit) so it fills the TV's full-bleed waiting screen the same
        // way a desktop wallpaper fills a monitor, rather than letterboxing.
        Image(
            painter = painterResource(id = R.drawable.omarchy_wallpaper),
            contentDescription = null,
            modifier = Modifier.fillMaxSize(),
            contentScale = ContentScale.Crop,
        )
        // No IP field / Connect button -- the app always auto-connects on
        // launch (ReceiverViewModel.init), per the user's own request to
        // remove them; there's nothing for a person to do here. All
        // status/state text lives at the bottom of the screen instead of
        // center, also per the user's own request, so it reads like a
        // status bar/log rather than the focal point of the screen.
        Column(
            modifier = Modifier.fillMaxSize().padding(48.dp),
            verticalArrangement = Arrangement.Bottom,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Image(
                painter = painterResource(R.drawable.omarchy_wordmark),
                contentDescription = "Omarchy",
                modifier = Modifier.width(256.dp).height(60.dp),
                contentScale = ContentScale.Fit,
            )
            Spacer(modifier = Modifier.height(16.dp))
            Text(
                text = "Omarchy AI Receiver",
                style = MaterialTheme.typography.headlineMedium,
                color = Color.White,
            )
            Spacer(modifier = Modifier.height(16.dp))
            Text(text = statusText(connectionState), color = Color.White)
        }
    }
}

private fun statusText(state: CastConnectionState): String = when (state) {
    CastConnectionState.Idle -> "Not connected"
    CastConnectionState.SignalingConnected -> "Signaling connected, waiting for the desktop to start casting..."
    CastConnectionState.Negotiating -> "Negotiating WebRTC connection..."
    is CastConnectionState.IceState -> "ICE state: ${state.state}"
    is CastConnectionState.Failed -> "Failed: ${state.reason}"
    CastConnectionState.Closed -> "Disconnected"
}
