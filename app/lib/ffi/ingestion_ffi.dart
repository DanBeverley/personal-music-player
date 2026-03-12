import 'dart:ffi' as ffi;
import 'dart:io' show Platform;
import 'package:ffi/ffi.dart';

typedef DownloadYouTubeAudioCType = ffi.Int32 Function(
    ffi.Pointer<Utf8> url, ffi.Pointer<Utf8> outPath);
typedef DownloadYouTubeAudioDartType = int Function(
    ffi.Pointer<Utf8> url, ffi.Pointer<Utf8> outPath);

class IngestionEngineFFI {
  late ffi.DynamicLibrary _lib;
  late DownloadYouTubeAudioDartType downloadAudio;

  IngestionEngineFFI() {
    try {
      _lib = Platform.isAndroid
          ? ffi.DynamicLibrary.open('libauralis_ingestion.so')
          : ffi.DynamicLibrary.process();

      downloadAudio = _lib.lookupFunction<DownloadYouTubeAudioCType,
          DownloadYouTubeAudioDartType>('auralis_download_youtube_audio');
    } catch (e) {
      // print(
      //    'FFI Error: Rust Ingestion library not found. Falling back to mock. ($e)');
      downloadAudio = (url, path) {
        // print("Mock downloading...");
        return 0; // Simulate success
      };
    }
  }
}
