package com.danbeverley.ebb

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import com.ryanheise.audioservice.AudioServiceActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File

class MainActivity : AudioServiceActivity() {
    companion object {
        private const val SONG_MATCH_CHANNEL = "ebb/song_match_intents"
    }

    private var songMatchChannel: MethodChannel? = null
    private var pendingSharedMedia: Map<String, String>? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handleIncomingIntent(intent, notifyFlutter = false)
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        songMatchChannel = MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            SONG_MATCH_CHANNEL
        )
        songMatchChannel?.setMethodCallHandler { call, result ->
            when (call.method) {
                "getPendingSharedMedia" -> result.success(pendingSharedMedia)
                "clearPendingSharedMedia" -> {
                    pendingSharedMedia = null
                    result.success(null)
                }
                else -> result.notImplemented()
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIncomingIntent(intent, notifyFlutter = true)
    }

    private fun handleIncomingIntent(intent: Intent?, notifyFlutter: Boolean) {
        val resolved = resolveSharedMedia(intent) ?: return
        pendingSharedMedia = resolved
        if (notifyFlutter) {
            songMatchChannel?.invokeMethod("sharedMediaUpdated", resolved)
        }
    }

    private fun resolveSharedMedia(intent: Intent?): Map<String, String>? {
        if (intent == null) return null
        val action = intent.action ?: return null
        val uri = when (action) {
            Intent.ACTION_VIEW -> intent.data
            Intent.ACTION_SEND -> intent.getParcelableExtra(Intent.EXTRA_STREAM)
            Intent.ACTION_SEND_MULTIPLE -> {
                val streams = intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)
                streams?.firstOrNull()
            }
            else -> null
        } ?: return null
        val mimeType = (intent.type ?: contentResolver.getType(uri) ?: "").trim()
        if (!mimeType.startsWith("audio/") && !mimeType.startsWith("video/")) {
            return null
        }
        val copiedFile = copyUriToCache(uri) ?: return null
        return mapOf(
            "path" to copiedFile.absolutePath,
            "displayName" to copiedFile.name,
            "mimeType" to mimeType,
            "sourceType" to "shared",
            "mediaKind" to if (mimeType.startsWith("video/")) "video" else "audio",
        )
    }

    private fun copyUriToCache(uri: Uri): File? {
        return try {
            val input = contentResolver.openInputStream(uri) ?: return null
            val directory = File(cacheDir, "song_match_shared")
            if (!directory.exists()) {
                directory.mkdirs()
            }
            val displayName = queryDisplayName(uri)
            val safeName = displayName
                .replace(Regex("[^A-Za-z0-9._-]"), "_")
                .ifBlank { "shared_media_${System.currentTimeMillis()}" }
            val destination = File(directory, "${System.currentTimeMillis()}_$safeName")
            input.use { source ->
                destination.outputStream().use { output ->
                    source.copyTo(output)
                }
            }
            destination
        } catch (_: Exception) {
            null
        }
    }

    private fun queryDisplayName(uri: Uri): String {
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
            ?.use { cursor ->
                val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (nameIndex >= 0 && cursor.moveToFirst()) {
                    return cursor.getString(nameIndex) ?: ""
                }
            }
        val fromPath = uri.lastPathSegment ?: ""
        return fromPath.substringAfterLast('/')
    }
}
