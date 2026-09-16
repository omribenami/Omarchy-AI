import java.security.MessageDigest

plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.compose.compiler)
}

// Real version tracking (see STATUS.md's "receiver version tracking" entry
// -- before this, versionCode/versionName were hardcoded to 1/"1.0" since
// the very first build months ago, so there was no way, on-screen or via
// `dumpsys package`, to tell an old install apart from a newly built one).
// android-receiver/ isn't its own git repo, it's a subdirectory of the
// omarchy-ai repo -- `git rev-parse`/`rev-list` run fine from here and
// report the whole repo's HEAD, which is fine: this doesn't need to be
// scoped to just this subdirectory, it just needs to change whenever a new
// APK is built from different source than the last one.
//
// `providers.exec {}` (not a plain ProcessBuilder/`project.exec {}`): this
// Gradle version (9.1) runs with the configuration cache on by default,
// which refuses a raw external-process call made directly at
// configuration time ("Starting an external process ... during
// configuration time is unsupported" -- confirmed live, the first two
// approaches tried both hit it). Routing through ProviderFactory.exec
// registers the process as a tracked configuration-cache input instead of
// an untracked side effect, which is what actually satisfies it.
// `rootDir`/`providers` are only resolvable in the script's own
// implicit-Project scope, so both are captured into plain vals here and
// passed in explicitly rather than referenced from inside gitOutput,
// which (as a free top-level fun) has no implicit receiver of its own.
val repoRoot: java.io.File = rootDir
val execProviders: ProviderFactory = providers

fun gitOutput(execProviders: ProviderFactory, repoRoot: java.io.File, vararg args: String): String {
    return try {
        execProviders.exec {
            commandLine(listOf("git") + args.toList())
            workingDir = repoRoot
            isIgnoreExitValue = true
        }.standardOutput.asText.get().trim()
    } catch (e: Exception) {
        ""
    }
}

// versionName: short commit hash, so a human (or `dumpsys package`) can
// see at a glance which build is installed. A "-dirty" suffix marks an
// APK built from uncommitted changes (this project builds locally, not
// from CI, so that's a real case) -- still human-readable, not used for
// Android's own version comparison.
val gitHash = gitOutput(execProviders, repoRoot, "rev-parse", "--short", "HEAD").ifBlank { "unknown" }
val gitDirty = gitOutput(execProviders, repoRoot, "status", "--porcelain").isNotBlank()
// Distinguish successive local builds even while HEAD stays unchanged.
val sourceDigest = MessageDigest.getInstance("SHA-256")
fileTree("src").files.sortedBy { it.relativeTo(projectDir).path }.forEach {
    sourceDigest.update(it.relativeTo(projectDir).path.toByteArray())
    sourceDigest.update(it.readBytes())
}
sourceDigest.update(file("build.gradle.kts").readBytes())
val sourceHash = sourceDigest.digest().joinToString("") { "%02x".format(it) }.take(12)
val receiverVersionName = "$gitHash-$sourceHash"

// versionCode: Android requires a monotonically increasing integer, and a
// hash isn't one. Commit count (`git rev-list --count HEAD`) is simplest
// and robust here -- it only ever goes up, needs no manual bookkeeping,
// and (unlike a build timestamp) is stable/reproducible for the exact
// same commit. Dirty/uncommitted builds still increment nothing new, but
// that's fine: versionCode's only real job is ordering installs, and
// versionName (above) is what actually gets read to tell builds apart.
val receiverVersionCode = gitOutput(execProviders, repoRoot, "rev-list", "--count", "HEAD").toIntOrNull() ?: 1

android {
    namespace = "ai.omarchy.receiver"
    compileSdk = 36
    defaultConfig {
        applicationId = "ai.omarchy.receiver"
        minSdk = 26
        targetSdk = 36
        versionCode = receiverVersionCode
        versionName = receiverVersionName
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
      compose = true
      aidl = false
      buildConfig = true
      shaders = false
    }

    packaging {
      resources {
        excludes += "/META-INF/{AL2.0,LGPL2.1}"
      }
    }
}

kotlin {
    jvmToolchain(17)
}

dependencies {
  val composeBom = platform(libs.androidx.compose.bom)
  implementation(composeBom)
  androidTestImplementation(composeBom)

  // Core Android dependencies
  implementation(libs.androidx.core.ktx)
  implementation(libs.androidx.lifecycle.runtime.ktx)
  implementation(libs.androidx.activity.compose)

  // Arch Components
  implementation(libs.androidx.lifecycle.runtime.compose)
  implementation(libs.androidx.lifecycle.viewmodel.compose)

  // Compose
  implementation(libs.androidx.compose.ui)
  implementation(libs.androidx.compose.ui.tooling.preview)
  implementation(libs.androidx.compose.material3)
  // Tooling
  debugImplementation(libs.androidx.compose.ui.tooling)
  // Instrumented tests
  androidTestImplementation(libs.androidx.compose.ui.test.junit4)
  debugImplementation(libs.androidx.compose.ui.test.manifest)

  // Local tests: jUnit, coroutines, Android runner
  testImplementation(libs.junit)
  testImplementation(libs.kotlinx.coroutines.test)

  // Instrumented tests: jUnit rules and runners
  androidTestImplementation(libs.androidx.test.core)
  androidTestImplementation(libs.androidx.test.ext.junit)
  androidTestImplementation(libs.androidx.test.runner)
  androidTestImplementation(libs.androidx.test.espresso.core)

  // Casting: WebRTC receive-only client + its signaling transport (see
  // ADR-0001 D7 and src/omarchy_ai/display/signaling.py).
  implementation(libs.stream.webrtc.android)
  implementation(libs.okhttp)
}
