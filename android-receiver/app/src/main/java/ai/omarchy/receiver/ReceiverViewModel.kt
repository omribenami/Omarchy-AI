package ai.omarchy.receiver

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.webrtc.EglBase

/**
 * Orchestrates [WebRtcClient] for the Compose UI ([ReceiverScreen]) and
 * remembers the last sender IP in [android.content.SharedPreferences] so a
 * relaunch (e.g. `adb shell am start`, the same mechanism Phase 0 already
 * confirmed works, see STATUS.md) reconnects without anyone touching the TV
 * -- the whole point of this app per ADR-0001.
 */
class ReceiverViewModel(application: Application) : AndroidViewModel(application) {

    val eglBase: EglBase = EglBase.create()
    private val client = WebRtcClient(application, eglBase)
    private val prefs = application.getSharedPreferences(PREFS_NAME, Application.MODE_PRIVATE)

    val connectionState: StateFlow<CastConnectionState> = client.connectionState
    val remoteVideoTrack = client.remoteVideoTrack
    val microphoneActive = client.microphoneActive
    fun refreshMicrophone() = client.refreshMicrophone()

    private val _hostInput = MutableStateFlow(prefs.getString(PREF_HOST, DEFAULT_HOST) ?: DEFAULT_HOST)
    val hostInput: StateFlow<String> = _hostInput

    init {
        // Auto-connect on launch using whatever host was last used (or the
        // built-in default) -- no manual TV navigation, matching the Phase 0
        // vertical slice's own bar.
        if (_hostInput.value.isNotBlank()) {
            connect(_hostInput.value)
        }
    }

    fun updateHost(value: String) {
        _hostInput.value = value
    }

    fun connect(host: String = _hostInput.value, port: Int = DEFAULT_SIGNALING_PORT) {
        val trimmed = host.trim()
        if (trimmed.isEmpty()) return
        _hostInput.value = trimmed
        prefs.edit().putString(PREF_HOST, trimmed).apply()
        client.connect("ws://$trimmed:$port")
    }

    fun disconnect() {
        client.disconnect()
    }

    override fun onCleared() {
        client.release()
        eglBase.release()
        super.onCleared()
    }

    companion object {
        private const val PREFS_NAME = "receiver"
        private const val PREF_HOST = "signaling_host"

        // This desktop's own LAN IP at the time this was written (confirmed
        // via `ip route get 192.168.1.86`, the paired TV) -- a reasonable
        // first-run default; editable in the UI, and whatever is actually
        // used gets remembered above.
        private const val DEFAULT_HOST = "192.168.1.65"
        const val DEFAULT_SIGNALING_PORT = 8765
    }
}
