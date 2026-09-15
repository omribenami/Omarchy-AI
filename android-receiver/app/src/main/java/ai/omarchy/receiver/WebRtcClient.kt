package ai.omarchy.receiver

import android.content.Context
import android.util.Log
import java.util.concurrent.ConcurrentLinkedQueue
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.DefaultVideoEncoderFactory
import org.webrtc.EglBase
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RtpTransceiver
import org.webrtc.SessionDescription
import org.webrtc.VideoTrack
import org.webrtc.audio.JavaAudioDeviceModule

private const val TAG = "WebRtcClient"

/** Mirrors the handful of states the UI actually needs to react to. */
sealed interface CastConnectionState {
    data object Idle : CastConnectionState
    data object SignalingConnected : CastConnectionState
    data object Negotiating : CastConnectionState
    data class IceState(val state: PeerConnection.IceConnectionState) : CastConnectionState
    data class Failed(val reason: String) : CastConnectionState
    data object Closed : CastConnectionState
}

internal fun CastConnectionState.isStreaming(): Boolean =
    this is CastConnectionState.IceState &&
        (
            state == PeerConnection.IceConnectionState.CONNECTED ||
                state == PeerConnection.IceConnectionState.COMPLETED
            )

/**
 * Drives one receive-only WebRTC session against a `webrtcbin` sender (see
 * `scripts/spike_cast_sender.py`, and eventually the real
 * `src/omarchy_ai/display/encoder.py`) via [SignalingClient]. This is a
 * one-for-one Kotlin/`org.webrtc` port of
 * `scripts/spike_cast_receiver.py`'s webrtcbin logic: connect signaling as
 * "viewer" -> receive an offer -> setRemoteDescription -> createAnswer ->
 * setLocalDescription -> send the answer -> exchange ICE candidates ->
 * render the remote video track. Unlike the Python spike (which routes
 * decoded audio to `fakesink` to dodge a mic feedback loop on the same
 * desktop), this receiver has no capture path to feed back into, so remote
 * audio is left to play normally -- `JavaAudioDeviceModule` handles output
 * automatically once a remote audio track exists, no extra wiring needed.
 *
 * Per the STUN-wait lesson already learned for the voice client (see
 * STATUS.md "Connection latency"), [PeerConnection.RTCConfiguration] is
 * built with an empty ICE server list: this is a LAN-only connection
 * (desktop and TV on the same subnet), so there is nothing for a STUN
 * server to usefully do here either, only a fixed timeout to pay for
 * nothing.
 */
class WebRtcClient(context: Context, private val eglBase: EglBase) {

    private val _connectionState = MutableStateFlow<CastConnectionState>(CastConnectionState.Idle)
    val connectionState: StateFlow<CastConnectionState> = _connectionState

    private val _remoteVideoTrack = MutableStateFlow<VideoTrack?>(null)
    val remoteVideoTrack: StateFlow<VideoTrack?> = _remoteVideoTrack

    private val factory: PeerConnectionFactory
    private var peerConnection: PeerConnection? = null
    private var signaling: SignalingClient? = null
    private var remoteDescriptionSet = false
    private val pendingRemoteCandidates = ConcurrentLinkedQueue<IceCandidate>()

    init {
        val appContext = context.applicationContext
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(appContext).createInitializationOptions(),
        )
        val encoderFactory = DefaultVideoEncoderFactory(eglBase.eglBaseContext, /* enableIntelVp8Encoder = */ true, /* enableH264HighProfile = */ true)
        val decoderFactory = DefaultVideoDecoderFactory(eglBase.eglBaseContext)
        val audioDeviceModule = JavaAudioDeviceModule.builder(appContext).createAudioDeviceModule()
        factory = PeerConnectionFactory.builder()
            .setVideoEncoderFactory(encoderFactory)
            .setVideoDecoderFactory(decoderFactory)
            .setAudioDeviceModule(audioDeviceModule)
            .createPeerConnectionFactory()
    }

    /** `signalingUrl` is a full `ws://host:port` URL. */
    fun connect(signalingUrl: String) {
        disconnect()

        val rtcConfig = PeerConnection.RTCConfiguration(emptyList()).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE
        }
        peerConnection = factory.createPeerConnection(
            rtcConfig,
            object : PeerConnectionObserverAdapter() {
                override fun onIceCandidate(candidate: IceCandidate?) {
                    candidate?.let { signaling?.sendIceCandidate(it) }
                }

                override fun onIceConnectionChange(newState: PeerConnection.IceConnectionState?) {
                    newState ?: return
                    Log.i(TAG, "ICE connection state -> $newState")
                    _connectionState.value = when (newState) {
                        PeerConnection.IceConnectionState.FAILED -> CastConnectionState.Failed("ICE connection failed")
                        else -> CastConnectionState.IceState(newState)
                    }
                }

                override fun onTrack(transceiver: RtpTransceiver?) {
                    val track = transceiver?.receiver?.track()
                    if (track is VideoTrack) {
                        Log.i(TAG, "remote video track received: ${track.id()}")
                        _remoteVideoTrack.value = track
                    }
                    // Remote audio needs no extra wiring: JavaAudioDeviceModule
                    // plays a remote audio track automatically once it exists
                    // and isn't explicitly disabled.
                }
            },
        )

        if (peerConnection == null) {
            _connectionState.value = CastConnectionState.Failed("createPeerConnection returned null")
            return
        }

        signaling = SignalingClient(
            url = signalingUrl,
            listener = object : SignalingClient.Listener {
                override fun onConnected() {
                    _connectionState.value = CastConnectionState.SignalingConnected
                }

                override fun onOffer(sdp: SessionDescription) {
                    handleOffer(sdp)
                }

                override fun onIceCandidate(candidate: IceCandidate) {
                    val pc = peerConnection
                    if (pc == null || !remoteDescriptionSet) {
                        pendingRemoteCandidates.add(candidate)
                    } else {
                        pc.addIceCandidate(candidate)
                    }
                }

                override fun onDisconnected(reason: String) {
                    Log.i(TAG, "signaling disconnected: $reason")
                    _connectionState.value = CastConnectionState.Closed
                }

                override fun onError(message: String) {
                    _connectionState.value = CastConnectionState.Failed(message)
                }
            },
        ).also { it.connect() }
    }

    private fun handleOffer(offer: SessionDescription) {
        val pc = peerConnection ?: return
        _connectionState.value = CastConnectionState.Negotiating
        pc.setRemoteDescription(
            object : SdpObserverAdapter() {
                override fun onSetSuccess() {
                    remoteDescriptionSet = true
                    drainPendingCandidates(pc)
                    pc.createAnswer(
                        object : SdpObserverAdapter() {
                            override fun onCreateSuccess(description: SessionDescription?) {
                                description ?: return
                                pc.setLocalDescription(
                                    object : SdpObserverAdapter() {
                                        override fun onSetSuccess() {
                                            signaling?.sendAnswer(description)
                                        }

                                        override fun onSetFailure(error: String?) {
                                            _connectionState.value =
                                                CastConnectionState.Failed("setLocalDescription: $error")
                                        }
                                    },
                                    description,
                                )
                            }

                            override fun onCreateFailure(error: String?) {
                                _connectionState.value = CastConnectionState.Failed("createAnswer: $error")
                            }
                        },
                        MediaConstraints(),
                    )
                }

                override fun onSetFailure(error: String?) {
                    _connectionState.value = CastConnectionState.Failed("setRemoteDescription: $error")
                }
            },
            offer,
        )
    }

    private fun drainPendingCandidates(pc: PeerConnection) {
        while (true) {
            val candidate = pendingRemoteCandidates.poll() ?: break
            pc.addIceCandidate(candidate)
        }
    }

    fun disconnect() {
        signaling?.close()
        signaling = null
        peerConnection?.close()
        peerConnection?.dispose()
        peerConnection = null
        remoteDescriptionSet = false
        pendingRemoteCandidates.clear()
        _remoteVideoTrack.value = null
        _connectionState.value = CastConnectionState.Idle
    }

    fun release() {
        disconnect()
        factory.dispose()
    }
}
