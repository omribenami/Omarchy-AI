package ai.omarchy.receiver

import org.webrtc.DataChannel
import org.webrtc.IceCandidate
import org.webrtc.MediaStream
import org.webrtc.PeerConnection
import org.webrtc.RtpReceiver
import org.webrtc.RtpTransceiver
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription

/**
 * `PeerConnection.Observer` and `SdpObserver` are plain Java interfaces with
 * a lot of methods this receiver doesn't care about (we never send tracks
 * or open data channels). These no-op adapters let call sites override only
 * the handful of callbacks that matter -- the same pattern as Android's own
 * `*Adapter` classes for multi-method listener interfaces.
 */
open class PeerConnectionObserverAdapter : PeerConnection.Observer {
  override fun onSignalingChange(newState: PeerConnection.SignalingState?) {}

  override fun onIceConnectionChange(newState: PeerConnection.IceConnectionState?) {}

  override fun onIceConnectionReceivingChange(receiving: Boolean) {}

  override fun onIceGatheringChange(newState: PeerConnection.IceGatheringState?) {}

  override fun onIceCandidate(candidate: IceCandidate?) {}

  override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>?) {}

  override fun onAddStream(stream: MediaStream?) {}

  override fun onRemoveStream(stream: MediaStream?) {}

  override fun onDataChannel(dataChannel: DataChannel?) {}

  override fun onRenegotiationNeeded() {}

  override fun onAddTrack(receiver: RtpReceiver?, mediaStreams: Array<out MediaStream>?) {}

  override fun onTrack(transceiver: RtpTransceiver?) {}
}

open class SdpObserverAdapter : SdpObserver {
  override fun onCreateSuccess(description: SessionDescription?) {}

  override fun onSetSuccess() {}

  override fun onCreateFailure(error: String?) {}

  override fun onSetFailure(error: String?) {}
}
