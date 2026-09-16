package ai.omarchy.receiver

import android.util.Log
import java.util.concurrent.TimeUnit
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONException
import org.json.JSONObject
import org.webrtc.IceCandidate
import org.webrtc.SessionDescription

private const val TAG = "SignalingClient"

/**
 * Talks to `src/omarchy_ai/display/signaling.py`'s tiny JSON relay over a
 * plain WebSocket -- this class mirrors that module's protocol exactly (see
 * its docstring, the source of truth):
 *
 *   -> first message: {"role": "viewer"}
 *   <- {"type": "offer", "sdp": "..."}
 *   -> {"type": "answer", "sdp": "..."}
 *   <-/-> {"type": "ice", "candidate": "...", "sdpMLineIndex": N, "sdpMid": "..."}
 *   -> {"type": "bye"}
 *
 * The sender side (`scripts/spike_cast_sender.py`, GStreamer's webrtcbin)
 * only ever emits `sdpMLineIndex` + `candidate` for ICE, no `sdpMid` --
 * `onIceCandidate` below tolerates that (defaults to an empty string, same
 * as `scripts/spike_cast_receiver.py`'s Python-side webrtcbin counterpart
 * does).
 */
class SignalingClient(private val url: String, private val listener: Listener) {

    interface Listener {
        fun onConnected()
        fun onOffer(sdp: SessionDescription)
        fun onIceCandidate(candidate: IceCandidate)
        fun onSenderStopped()
        fun onDisconnected(reason: String)
        fun onError(message: String)
    }

    private val client = OkHttpClient.Builder().pingInterval(15, TimeUnit.SECONDS).build()
    private var socket: WebSocket? = null
    @Volatile private var closedByUs = false

    fun connect() {
        closedByUs = false
        val request = Request.Builder().url(url).build()
        socket = client.newWebSocket(
            request,
            object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    if (closedByUs) { webSocket.close(1000, null); return }
                    Log.i(TAG, "connected to $url")
                    webSocket.send(JSONObject().put("role", "viewer").toString())
                    listener.onConnected()
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    if (!closedByUs) handleMessage(text)
                }

                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                    webSocket.close(1000, null)
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    if (!closedByUs) {
                        listener.onDisconnected(reason.ifBlank { "closed ($code)" })
                    }
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    if (closedByUs) return
                    Log.e(TAG, "signaling failure", t)
                    listener.onError(t.message ?: t.javaClass.simpleName)
                }
            },
        )
    }

    private fun handleMessage(text: String) {
        val msg = try {
            JSONObject(text)
        } catch (e: JSONException) {
            Log.w(TAG, "bad signaling message: $text", e)
            return
        }
        when (val type = msg.optString("type")) {
            "offer" -> listener.onOffer(
                SessionDescription(SessionDescription.Type.OFFER, msg.optString("sdp")),
            )
            "ice" -> listener.onIceCandidate(
                IceCandidate(
                    msg.optString("sdpMid", ""),
                    msg.optInt("sdpMLineIndex", 0),
                    msg.optString("candidate"),
                ),
            )
            "bye" -> listener.onSenderStopped()
            else -> Log.w(TAG, "unhandled signaling message type: $type")
        }
    }

    fun sendVideoReady() {
        send(JSONObject().put("type", "video-ready"))
    }

    fun sendMicrophoneState(enabled: Boolean) {
        send(JSONObject().put("type", "microphone").put("enabled", enabled))
    }

    fun sendAnswer(sdp: SessionDescription) {
        send(JSONObject().put("type", "answer").put("sdp", sdp.description))
    }

    fun sendIceCandidate(candidate: IceCandidate) {
        send(
            JSONObject()
                .put("type", "ice")
                .put("candidate", candidate.sdp)
                .put("sdpMLineIndex", candidate.sdpMLineIndex)
                .put("sdpMid", candidate.sdpMid ?: ""),
        )
    }

    fun close() {
        closedByUs = true
        val ws = socket ?: return
        send(JSONObject().put("type", "bye"))
        ws.close(1000, "viewer closing")
        socket = null
    }

    private fun send(json: JSONObject) {
        val sent = socket?.send(json.toString()) ?: false
        if (!sent) Log.w(TAG, "failed to send (socket not open): ${json.optString("type")}")
    }
}
