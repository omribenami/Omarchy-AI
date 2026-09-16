package ai.omarchy.receiver

import android.content.Context
import android.content.BroadcastReceiver
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbManager
import android.hardware.usb.UsbConstants
import androidx.core.content.ContextCompat
import android.Manifest
import android.content.pm.PackageManager
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import org.webrtc.AudioSource
import org.webrtc.AudioTrack
import org.webrtc.MediaStreamTrack
import android.util.Log
import android.os.Handler
import android.os.Looper
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
import org.webrtc.VideoSink
import java.util.concurrent.atomic.AtomicBoolean
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

    private val appContext = context.applicationContext
    private val audioManager = appContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val usbManager = appContext.getSystemService(Context.USB_SERVICE) as UsbManager
    private val usbReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            handler.postDelayed({ refreshMicrophone() }, 300)
        }
    }
    private val audioDeviceModule: JavaAudioDeviceModule
    private var firstFrameSink: VideoSink? = null
    private var micSource: AudioSource? = null
    private var micTrack: AudioTrack? = null
    private val _microphoneActive = MutableStateFlow(false)
    val microphoneActive: StateFlow<Boolean> = _microphoneActive
    private val deviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(devices: Array<out AudioDeviceInfo>) { refreshMicrophone() }
        override fun onAudioDevicesRemoved(devices: Array<out AudioDeviceInfo>) { refreshMicrophone() }
    }
    private val factory: PeerConnectionFactory
    private var peerConnection: PeerConnection? = null
    private var signaling: SignalingClient? = null
    private val handler = Handler(Looper.getMainLooper())
    private var generation = 0
    private var peerGeneration = 0
    private var reconnectAttempts = 0
    private var reconnectTask: Runnable? = null
    private var remoteDescriptionSet = false
    private val pendingRemoteCandidates = ConcurrentLinkedQueue<IceCandidate>()

    init {
        val appContext = context.applicationContext
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(appContext).createInitializationOptions(),
        )
        val encoderFactory = DefaultVideoEncoderFactory(eglBase.eglBaseContext, /* enableIntelVp8Encoder = */ true, /* enableH264HighProfile = */ true)
        // HY300's texture decoder reproducibly crashes during reconnect:
        // "Rendered texture metadata was null". Keep MediaCodec decoding,
        // but request byte-buffer output instead of its SurfaceTexture path.
        val decoderFactory = DefaultVideoDecoderFactory(null as EglBase.Context?)
        audioDeviceModule = JavaAudioDeviceModule.builder(appContext).createAudioDeviceModule()
        factory = PeerConnectionFactory.builder()
            .setVideoEncoderFactory(encoderFactory)
            .setVideoDecoderFactory(decoderFactory)
            .setAudioDeviceModule(audioDeviceModule)
            .createPeerConnectionFactory()
        audioManager.registerAudioDeviceCallback(deviceCallback, handler)
        ContextCompat.registerReceiver(appContext, usbReceiver, IntentFilter().apply {
            addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED)
            addAction(UsbManager.ACTION_USB_DEVICE_DETACHED)
        }, ContextCompat.RECEIVER_NOT_EXPORTED)
    }

    /** `signalingUrl` is a full `ws://host:port` URL. */
    fun connect(signalingUrl: String) {
        disconnect()

        val current = generation
        fun dispatch(block: () -> Unit) {
            handler.post { if (generation == current) block() }
        }
        fun retry(reason: String) {
            if (reconnectTask != null) return
            _connectionState.value = CastConnectionState.Failed(reason)
            closePeer()
            if (reconnectAttempts >= 5) return
            val delay = minOf(10000L, 1000L shl reconnectAttempts++)
            reconnectTask = Runnable {
                reconnectTask = null
                if (generation == current) connect(signalingUrl)
            }.also { handler.postDelayed(it, delay) }
        }

        signaling = SignalingClient(
            url = signalingUrl,
            listener = object : SignalingClient.Listener {
                override fun onConnected() {
                    dispatch {
                        _connectionState.value = CastConnectionState.SignalingConnected
                        refreshMicrophone()
                    }
                }

                override fun onOffer(sdp: SessionDescription) {
                    dispatch {
                        createPeer()
                        handleOffer(sdp)
                    }
                }

                override fun onIceCandidate(candidate: IceCandidate) {
                    dispatch {
                    val pc = peerConnection
                    if (pc == null || !remoteDescriptionSet) {
                        pendingRemoteCandidates.add(candidate)
                    } else {
                        pc.addIceCandidate(candidate)
                    }
                    }
                }

                override fun onSenderStopped() {
                    dispatch {
                        closePeer()
                        _connectionState.value = CastConnectionState.SignalingConnected
                    }
                }

                override fun onDisconnected(reason: String) {
                    Log.i(TAG, "signaling disconnected: $reason")
                    dispatch { retry(reason) }
                }

                override fun onError(message: String) {
                    dispatch { retry(message) }
                }
            },
        ).also { it.connect() }
    }

    private fun createPeer() {
        closePeer()
        val current = peerGeneration
        val rtcConfig = PeerConnection.RTCConfiguration(emptyList()).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE
        }
        peerConnection = factory.createPeerConnection(
            rtcConfig,
            object : PeerConnectionObserverAdapter() {
                override fun onIceCandidate(candidate: IceCandidate?) {
                    handler.post { if (peerGeneration == current) candidate?.let { signaling?.sendIceCandidate(it) } }
                }

                override fun onIceConnectionChange(newState: PeerConnection.IceConnectionState?) {
                    newState ?: return
                    handler.post {
                    if (peerGeneration != current) return@post
                    if (newState == PeerConnection.IceConnectionState.CONNECTED || newState == PeerConnection.IceConnectionState.COMPLETED) reconnectAttempts = 0
                    Log.i(TAG, "ICE connection state -> $newState")
                    _connectionState.value = when (newState) {
                        PeerConnection.IceConnectionState.FAILED -> CastConnectionState.Failed("ICE connection failed")
                        else -> CastConnectionState.IceState(newState)
                    }
                    }
                }

                override fun onTrack(transceiver: RtpTransceiver?) {
                    handler.post {
                    if (peerGeneration != current) return@post
                    val track = transceiver?.receiver?.track()
                    if (track is VideoTrack) {
                        Log.i(TAG, "remote video track received: ${track.id()}")
                        _remoteVideoTrack.value = track
                        val delivered = AtomicBoolean(false)
                        firstFrameSink = VideoSink {
                            if (delivered.compareAndSet(false, true)) handler.post {
                                if (peerGeneration == current) {
                                    Log.i(TAG, "first decoded video frame received")
                                    signaling?.sendVideoReady()
                                }
                            }
                        }.also { track.addSink(it) }
                    }
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

    }

    private fun handleOffer(offer: SessionDescription) {
        val pc = peerConnection ?: return
        _connectionState.value = CastConnectionState.Negotiating
        pc.setRemoteDescription(
            object : SdpObserverAdapter() {
                override fun onSetSuccess() {
                    handler.post {
                    if (peerConnection !== pc) return@post
                    remoteDescriptionSet = true
                    val audio = pc.transceivers.firstOrNull { it.mediaType == MediaStreamTrack.MediaType.MEDIA_TYPE_AUDIO }
                    if (audio != null) {
                        micSource = factory.createAudioSource(MediaConstraints().apply {
                            mandatory.add(MediaConstraints.KeyValuePair("googEchoCancellation", "true"))
                            mandatory.add(MediaConstraints.KeyValuePair("googNoiseSuppression", "true"))
                        })
                        micTrack = factory.createAudioTrack("tv-microphone", micSource)
                        micTrack?.setEnabled(false)
                        audio.sender.setTrack(micTrack, false)
                        audio.direction = RtpTransceiver.RtpTransceiverDirection.SEND_RECV
                        refreshMicrophone()
                    }
                    drainPendingCandidates(pc)
                    pc.createAnswer(
                        object : SdpObserverAdapter() {
                            override fun onCreateSuccess(description: SessionDescription?) {
                                description ?: return
                                handler.post {
                                if (peerConnection !== pc) return@post
                                pc.setLocalDescription(
                                    object : SdpObserverAdapter() {
                                        override fun onSetSuccess() {
                                            handler.post { if (peerConnection === pc) signaling?.sendAnswer(description) }
                                        }

                                        override fun onSetFailure(error: String?) {
                                            _connectionState.value =
                                                CastConnectionState.Failed("setLocalDescription: $error")
                                        }
                                    },
                                    description,
                                )
                                }
                            }

                            override fun onCreateFailure(error: String?) {
                                _connectionState.value = CastConnectionState.Failed("createAnswer: $error")
                            }
                        },
                        MediaConstraints(),
                    )
                    }
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
        generation++
        reconnectTask?.let { handler.removeCallbacks(it) }
        reconnectTask = null
        signaling?.close()
        signaling = null
        closePeer()
        _connectionState.value = CastConnectionState.Idle
    }

    private fun closePeer() {
        peerGeneration++
        firstFrameSink?.let { _remoteVideoTrack.value?.removeSink(it) }
        firstFrameSink = null
        _remoteVideoTrack.value = null
        peerConnection?.close()
        peerConnection?.dispose()
        peerConnection = null
        micTrack?.dispose()
        micTrack = null
        micSource?.dispose()
        micSource = null
        _microphoneActive.value = false
        remoteDescriptionSet = false
        pendingRemoteCandidates.clear()
        _remoteVideoTrack.value = null
        _connectionState.value = CastConnectionState.Idle
    }

    fun refreshMicrophone() {
        val device = audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS).firstOrNull {
            it.type == AudioDeviceInfo.TYPE_USB_DEVICE || it.type == AudioDeviceInfo.TYPE_USB_HEADSET ||
                it.type == AudioDeviceInfo.TYPE_WIRED_HEADSET
        }
        // Some Allwinner TV firmware routes USB capture through its default
        // input while omitting USB from AudioManager.getDevices(). Confirm
        // a real USB audio-streaming IN endpoint, never a generic USB device.
        val usbInput = usbManager.deviceList.values.any { usb ->
            (0 until usb.interfaceCount).any { index ->
                val intf = usb.getInterface(index)
                intf.interfaceClass == UsbConstants.USB_CLASS_AUDIO && intf.interfaceSubclass == 2 &&
                    (0 until intf.endpointCount).any { intf.getEndpoint(it).direction == UsbConstants.USB_DIR_IN }
            }
        }
        val enabled = (device != null || usbInput) && micTrack != null &&
            appContext.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        audioDeviceModule.setPreferredInputDevice(device)
        Log.i(TAG, "external microphone: audioDevice=${device?.productName}, usbInput=$usbInput, enabled=$enabled")
        audioDeviceModule.setMicrophoneMute(!enabled)
        micTrack?.setEnabled(enabled)
        _microphoneActive.value = enabled
        signaling?.sendMicrophoneState(enabled)
    }

    fun release() {
        appContext.unregisterReceiver(usbReceiver)
        audioManager.unregisterAudioDeviceCallback(deviceCallback)
        disconnect()
        factory.dispose()
        audioDeviceModule.release()
    }
}
