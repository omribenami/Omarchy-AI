package ai.omarchy.receiver

import android.Manifest
import android.content.Intent
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import ai.omarchy.receiver.theme.OmarchyReceiverTheme

class MainActivity : ComponentActivity() {
    private val viewModel: ReceiverViewModel by viewModels()

    // See the RECORD_AUDIO comment in AndroidManifest.xml / WebRtcClient.kt
    // -- requested up front so WebRTC's audio device module never has to
    // find out the hard way that it's missing.
    private val requestRecordAudio =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { viewModel.refreshMicrophone() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestRecordAudio.launch(Manifest.permission.RECORD_AUDIO)

        // A cast target should never let the system blank the screen
        // mid-stream.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        connectFromIntent(intent)
        enableEdgeToEdge()
        setContent {
            OmarchyReceiverTheme {
                Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    ReceiverScreen(viewModel)
                }
            }
        }
    }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        connectFromIntent(intent)
    }

    private fun connectFromIntent(intent: Intent) {
        val host = intent.getStringExtra("signaling_host")
        if (!host.isNullOrBlank()) viewModel.connect(host)
    }

}
