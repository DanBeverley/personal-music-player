import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:record/record.dart';

import 'auth_provider.dart';
import 'proxy_runtime.dart';
import 'track_metadata.dart';

enum SongMatchPhase {
  idle,
  recording,
  importing,
  uploading,
  recognizing,
  resolved,
  ambiguous,
  failed,
}

class PendingSongMatchMedia {
  final String path;
  final String displayName;
  final String mimeType;
  final String sourceType;
  final String mediaKind;

  const PendingSongMatchMedia({
    required this.path,
    required this.displayName,
    required this.mimeType,
    required this.sourceType,
    required this.mediaKind,
  });

  factory PendingSongMatchMedia.fromMap(Map<String, dynamic> json) {
    return PendingSongMatchMedia(
      path: (json['path'] ?? '').toString(),
      displayName: (json['displayName'] ?? json['filename'] ?? '').toString(),
      mimeType: (json['mimeType'] ?? '').toString(),
      sourceType: (json['sourceType'] ?? 'shared').toString(),
      mediaKind: (json['mediaKind'] ?? '').toString(),
    );
  }

  Map<String, dynamic> toMap() {
    return <String, dynamic>{
      'path': path,
      'displayName': displayName,
      'mimeType': mimeType,
      'sourceType': sourceType,
      'mediaKind': mediaKind,
    };
  }
}

class SongMatchState {
  final SongMatchPhase phase;
  final bool hasMicrophonePermission;
  final String statusMessage;
  final String errorMessage;
  final PendingSongMatchMedia? pendingSharedMedia;
  final Map<String, dynamic>? resolvedTrack;
  final Map<String, dynamic>? recognizedMetadata;
  final List<Map<String, dynamic>> alternatives;
  final Map<String, dynamic> diagnostics;
  final double confidence;
  final int presentationToken;
  final int shareIntentToken;

  const SongMatchState({
    this.phase = SongMatchPhase.idle,
    this.hasMicrophonePermission = true,
    this.statusMessage = '',
    this.errorMessage = '',
    this.pendingSharedMedia,
    this.resolvedTrack,
    this.recognizedMetadata,
    this.alternatives = const <Map<String, dynamic>>[],
    this.diagnostics = const <String, dynamic>{},
    this.confidence = 0.0,
    this.presentationToken = 0,
    this.shareIntentToken = 0,
  });

  bool get isBusy =>
      phase == SongMatchPhase.recording ||
      phase == SongMatchPhase.importing ||
      phase == SongMatchPhase.uploading ||
      phase == SongMatchPhase.recognizing;

  bool get hasPendingSharedMedia => pendingSharedMedia != null;

  SongMatchState copyWith({
    SongMatchPhase? phase,
    bool? hasMicrophonePermission,
    String? statusMessage,
    String? errorMessage,
    PendingSongMatchMedia? pendingSharedMedia,
    bool clearPendingSharedMedia = false,
    Map<String, dynamic>? resolvedTrack,
    bool clearResolvedTrack = false,
    Map<String, dynamic>? recognizedMetadata,
    bool clearRecognizedMetadata = false,
    List<Map<String, dynamic>>? alternatives,
    Map<String, dynamic>? diagnostics,
    double? confidence,
    int? presentationToken,
    int? shareIntentToken,
  }) {
    return SongMatchState(
      phase: phase ?? this.phase,
      hasMicrophonePermission:
          hasMicrophonePermission ?? this.hasMicrophonePermission,
      statusMessage: statusMessage ?? this.statusMessage,
      errorMessage: errorMessage ?? this.errorMessage,
      pendingSharedMedia: clearPendingSharedMedia
          ? null
          : pendingSharedMedia ?? this.pendingSharedMedia,
      resolvedTrack:
          clearResolvedTrack ? null : resolvedTrack ?? this.resolvedTrack,
      recognizedMetadata: clearRecognizedMetadata
          ? null
          : recognizedMetadata ?? this.recognizedMetadata,
      alternatives: alternatives ?? this.alternatives,
      diagnostics: diagnostics ?? this.diagnostics,
      confidence: confidence ?? this.confidence,
      presentationToken: presentationToken ?? this.presentationToken,
      shareIntentToken: shareIntentToken ?? this.shareIntentToken,
    );
  }
}

class SongMatchNotifier extends StateNotifier<SongMatchState> {
  SongMatchNotifier(this.ref) : super(const SongMatchState()) {
    unawaited(_initializeIntentBridge());
  }

  final Ref ref;
  final AudioRecorder _recorder = AudioRecorder();
  static const MethodChannel _channel = MethodChannel('ebb/song_match_intents');
  bool _intentBridgeInitialized = false;
  String _recordingPath = '';

  Future<void> _initializeIntentBridge() async {
    if (_intentBridgeInitialized) return;
    _intentBridgeInitialized = true;
    _channel.setMethodCallHandler((call) async {
      if (call.method == 'sharedMediaUpdated') {
        final payload = _mapPayload(call.arguments);
        _ingestPendingSharedPayload(payload, isNewIntent: true);
      }
    });
    try {
      final payload = await _channel.invokeMethod<dynamic>('getPendingSharedMedia');
      _ingestPendingSharedPayload(_mapPayload(payload), isNewIntent: false);
    } catch (_) {
      return;
    }
  }

  Map<String, dynamic>? _mapPayload(dynamic payload) {
    if (payload is Map) {
      return payload.map(
        (key, value) => MapEntry(key.toString(), value),
      );
    }
    return null;
  }

  void _ingestPendingSharedPayload(
    Map<String, dynamic>? payload, {
    required bool isNewIntent,
  }) {
    if (payload == null) return;
    final media = PendingSongMatchMedia.fromMap(payload);
    if (media.path.trim().isEmpty) return;
    state = state.copyWith(
      pendingSharedMedia: media,
      shareIntentToken: isNewIntent ? state.shareIntentToken + 1 : state.shareIntentToken,
    );
  }

  Future<void> clearPendingSharedMedia() async {
    state = state.copyWith(clearPendingSharedMedia: true);
    try {
      await _channel.invokeMethod<void>('clearPendingSharedMedia');
    } catch (_) {
      return;
    }
  }

  Future<void> pickFileAndMatch() async {
    final result = await FilePicker.platform.pickFiles(
      allowMultiple: false,
      type: FileType.custom,
      allowedExtensions: <String>[
        'mp3',
        'm4a',
        'aac',
        'wav',
        'ogg',
        'flac',
        'opus',
        'webm',
        'mp4',
        'mov',
        'mkv',
        'avi',
        'm4v',
      ],
    );
    if (result == null || result.files.isEmpty) {
      return;
    }
    final picked = result.files.first;
    final path = picked.path;
    if (path == null || path.isEmpty) {
      _emitFailure('That file could not be opened.');
      return;
    }
    final pending = PendingSongMatchMedia(
      path: path,
      displayName: picked.name,
      mimeType: '',
      sourceType: 'uploaded',
      mediaKind: _inferMediaKind(path, ''),
    );
    await _uploadAndRecognize(pending);
  }

  Future<void> matchPendingSharedFile() async {
    final pending = state.pendingSharedMedia;
    if (pending == null) {
      _emitFailure('There is no shared audio or video ready yet.');
      return;
    }
    await _uploadAndRecognize(pending);
  }

  Future<void> startRecordingSnippet() async {
    try {
      final permission = await Permission.microphone.request();
      if (!permission.isGranted) {
        state = state.copyWith(
          phase: SongMatchPhase.failed,
          hasMicrophonePermission: false,
          errorMessage: 'Microphone permission is required to record a snippet.',
          presentationToken: state.presentationToken + 1,
        );
        return;
      }
      final tempDir = await getTemporaryDirectory();
      final fileName = 'song_match_${DateTime.now().millisecondsSinceEpoch}.m4a';
      final path = '${tempDir.path}/$fileName';
      _recordingPath = path;
      state = state.copyWith(
        phase: SongMatchPhase.recording,
        hasMicrophonePermission: true,
        statusMessage: 'Listening for a short snippet...',
        errorMessage: '',
        clearResolvedTrack: true,
        clearRecognizedMetadata: true,
        alternatives: const <Map<String, dynamic>>[],
      );
      await _recorder.start(
        const RecordConfig(
          encoder: AudioEncoder.aacLc,
          bitRate: 128000,
          sampleRate: 44100,
        ),
        path: path,
      );
    } catch (error) {
      _emitFailure('Could not start recording: $error');
    }
  }

  Future<void> stopRecordingAndMatch() async {
    if (state.phase != SongMatchPhase.recording) return;
    try {
      final path = await _recorder.stop();
      final resolvedPath = (path?.trim().isNotEmpty ?? false) ? path!.trim() : _recordingPath;
      if (resolvedPath.isEmpty) {
        _emitFailure('Recording did not produce any audio.');
        return;
      }
      final pending = PendingSongMatchMedia(
        path: resolvedPath,
        displayName: File(resolvedPath).uri.pathSegments.last,
        mimeType: 'audio/mp4',
        sourceType: 'recorded',
        mediaKind: 'audio',
      );
      await _uploadAndRecognize(pending);
    } catch (error) {
      _emitFailure('Could not finish recording: $error');
    }
  }

  Future<void> cancelRecording() async {
    if (state.phase != SongMatchPhase.recording) return;
    try {
      await _recorder.stop();
    } catch (_) {}
    _recordingPath = '';
    reset();
  }

  Future<void> _uploadAndRecognize(PendingSongMatchMedia media) async {
    final file = File(media.path);
    if (!file.existsSync()) {
      _emitFailure('That audio snippet is no longer available.');
      return;
    }
    final fileName = media.displayName.trim().isEmpty
        ? file.uri.pathSegments.last
        : media.displayName.trim();
    state = state.copyWith(
      phase: media.sourceType == 'shared'
          ? SongMatchPhase.importing
          : SongMatchPhase.uploading,
      statusMessage: media.sourceType == 'shared'
          ? 'Importing shared media...'
          : 'Uploading media...',
      errorMessage: '',
      clearResolvedTrack: true,
      clearRecognizedMetadata: true,
      alternatives: const <Map<String, dynamic>>[],
    );
    final request = http.MultipartRequest(
      'POST',
      buildProxyUri('/recognize_audio'),
    );
    request.fields['source_type'] = media.sourceType;
    request.fields['media_kind'] = media.mediaKind;
    request.fields['user_scope_id'] = currentAuthenticatedUserId ?? 'guest';
    request.fields['filename'] = fileName;
    if (media.mimeType.trim().isNotEmpty) {
      request.fields['mime_type'] = media.mimeType.trim();
    }
    request.files.add(await http.MultipartFile.fromPath('media', file.path, filename: fileName));
    try {
      debugProxyLog(
        'song_match',
        'upload start source=${media.sourceType} kind=${media.mediaKind} file="$fileName"',
      );
      state = state.copyWith(
        phase: SongMatchPhase.recognizing,
        statusMessage: 'Matching your song...',
      );
      final streamed = await appHttpClient.send(request);
      final body = await streamed.stream.bytesToString();
      final payload = body.trim().isNotEmpty
          ? jsonDecode(body) as Map<String, dynamic>
          : <String, dynamic>{};
      debugProxyLog(
        'song_match',
        'upload done status=${streamed.statusCode} recognition=${payload['recognition_status']} diagnostics=${compactDiagnosticValue(payload['diagnostics'])}',
      );
      if (streamed.statusCode < 200 || streamed.statusCode >= 300) {
        final detail = (payload['detail'] ?? payload['error'] ?? 'Song match failed.')
            .toString();
        _emitFailure(detail);
        return;
      }
      final recognitionStatus =
          (payload['recognition_status'] ?? '').toString().trim().toLowerCase();
      final resolvedTrackRaw = payload['resolved_track'];
      final recognizedMetadataRaw = payload['recognized_metadata'];
      final alternativesRaw = (payload['alternatives'] as List<dynamic>? ?? const <dynamic>[]);
      final diagnostics = payload['diagnostics'] is Map
          ? Map<String, dynamic>.from(payload['diagnostics'] as Map)
          : const <String, dynamic>{};
      final resolvedTrack = resolvedTrackRaw is Map
          ? normalizeTrack(Map<String, dynamic>.from(resolvedTrackRaw))
          : null;
      final alternatives = alternativesRaw
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .map((entry) {
            final resolved = entry['resolved_track'];
            return <String, dynamic>{
              ...entry,
              if (resolved is Map)
                'resolved_track': normalizeTrack(Map<String, dynamic>.from(resolved)),
            };
          })
          .toList(growable: false);
      if (recognitionStatus == 'resolved' && resolvedTrack != null) {
        if (media.sourceType == 'shared') {
          unawaited(clearPendingSharedMedia());
        }
        state = state.copyWith(
          phase: SongMatchPhase.resolved,
          statusMessage: 'Match found.',
          resolvedTrack: resolvedTrack,
          recognizedMetadata: recognizedMetadataRaw is Map
              ? Map<String, dynamic>.from(recognizedMetadataRaw)
              : null,
          diagnostics: diagnostics,
          confidence: (payload['confidence'] as num?)?.toDouble() ?? 0.0,
          presentationToken: state.presentationToken + 1,
        );
        return;
      }
      if (recognitionStatus == 'ambiguous' && alternatives.isNotEmpty) {
        state = state.copyWith(
          phase: SongMatchPhase.ambiguous,
          statusMessage: 'A few good candidates were found.',
          recognizedMetadata: recognizedMetadataRaw is Map
              ? Map<String, dynamic>.from(recognizedMetadataRaw)
              : null,
          alternatives: alternatives,
          diagnostics: diagnostics,
          confidence: (payload['confidence'] as num?)?.toDouble() ?? 0.0,
          presentationToken: state.presentationToken + 1,
        );
        return;
      }
      if (recognitionStatus == 'provider_unavailable') {
        _emitFailure(
          'Song matching is not configured on the proxy yet. Add ACRCloud credentials first.',
          diagnostics: diagnostics,
        );
        return;
      }
      if (recognitionStatus == 'no_match') {
        _emitFailure(
          'No confident match was found from that clip. Try a cleaner or longer snippet.',
          diagnostics: diagnostics,
        );
        return;
      }
      _emitFailure(
        'Song matching could not find a playable result from that media.',
        diagnostics: diagnostics,
      );
    } catch (error) {
      _emitFailure('Song matching failed: $error');
    }
  }

  void reset() {
    state = state.copyWith(
      phase: SongMatchPhase.idle,
      statusMessage: '',
      errorMessage: '',
      clearResolvedTrack: true,
      clearRecognizedMetadata: true,
      alternatives: const <Map<String, dynamic>>[],
      diagnostics: const <String, dynamic>{},
      confidence: 0.0,
    );
  }

  void _emitFailure(
    String message, {
    Map<String, dynamic> diagnostics = const <String, dynamic>{},
  }) {
    state = state.copyWith(
      phase: SongMatchPhase.failed,
      statusMessage: '',
      errorMessage: message,
      diagnostics: diagnostics,
      presentationToken: state.presentationToken + 1,
    );
  }

  String _inferMediaKind(String path, String mimeType) {
    final inferred = guessMediaKindFromPath(path, mimeType);
    return inferred;
  }

  @override
  void dispose() {
    unawaited(_recorder.dispose());
    super.dispose();
  }
}

String guessMediaKindFromPath(String path, String mimeType) {
  final lowerMime = mimeType.trim().toLowerCase();
  if (lowerMime.startsWith('video/')) return 'video';
  if (lowerMime.startsWith('audio/')) return 'audio';
  final lowerPath = path.trim().toLowerCase();
  for (final suffix in <String>['.mp4', '.mov', '.mkv', '.avi', '.m4v']) {
    if (lowerPath.endsWith(suffix)) {
      return 'video';
    }
  }
  return 'audio';
}

final songMatchProvider =
    StateNotifierProvider<SongMatchNotifier, SongMatchState>((ref) {
  return SongMatchNotifier(ref);
});
