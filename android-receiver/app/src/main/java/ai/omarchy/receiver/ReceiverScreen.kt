package ai.omarchy.receiver

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
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
import androidx.compose.ui.unit.dp
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
    val hostInput by viewModel.hostInput.collectAsStateWithLifecycle()
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
            StatusOverlay(
                connectionState = connectionState,
                hostInput = hostInput,
                onHostChange = viewModel::updateHost,
                onConnect = { viewModel.connect(hostInput) },
            )
        }
    }
}

@Composable
private fun StatusOverlay(
    connectionState: CastConnectionState,
    hostInput: String,
    onHostChange: (String) -> Unit,
    onConnect: () -> Unit,
) {
    Column(
        modifier = Modifier.fillMaxSize().padding(48.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            text = "Omarchy AI Receiver",
            style = MaterialTheme.typography.headlineMedium,
            color = Color.White,
        )
        Spacer(modifier = Modifier.height(16.dp))
        Text(text = statusText(connectionState), color = Color.White)
        Spacer(modifier = Modifier.height(24.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                value = hostInput,
                onValueChange = onHostChange,
                label = { Text("Sender IP") },
                singleLine = true,
                modifier = Modifier.widthIn(min = 220.dp),
            )
            Spacer(modifier = Modifier.width(16.dp))
            Button(onClick = onConnect) { Text("Connect") }
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
