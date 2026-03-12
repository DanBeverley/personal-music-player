import 'dart:ffi' as ffi;
import 'dart:io' show Platform;
import 'package:flutter/foundation.dart';
import 'package:ffi/ffi.dart';

typedef AuralisInitCType = ffi.Bool Function();
typedef AuralisInitDartType = bool Function();

typedef AuralisLoadFileCType = ffi.Bool Function(ffi.Pointer<Utf8> filepath);
typedef AuralisLoadFileDartType = bool Function(ffi.Pointer<Utf8> filepath);

typedef AuralisPlayCType = ffi.Void Function();
typedef AuralisPlayDartType = void Function();

typedef AuralisPauseCType = ffi.Void Function();
typedef AuralisPauseDartType = void Function();

typedef AuralisSeekCType = ffi.Void Function(ffi.Uint32 positionMs);
typedef AuralisSeekDartType = void Function(int positionMs);

typedef AuralisSetLoopCType = ffi.Void Function(
    ffi.Bool enable, ffi.Uint32 startMs, ffi.Uint32 endMs);
typedef AuralisSetLoopDartType = void Function(
    bool enable, int startMs, int endMs);

class AudioEngineFFI {
  late ffi.DynamicLibrary _lib;
  late AuralisInitDartType initEngine;
  late AuralisLoadFileDartType loadFile;
  late AuralisPlayDartType play;
  late AuralisPauseDartType pause;
  late AuralisSeekDartType seek;
  late AuralisSetLoopDartType setLoop;

  AudioEngineFFI() {
    try {
      _lib = Platform.isAndroid
          ? ffi.DynamicLibrary.open('libauralis.so')
          : ffi.DynamicLibrary.process();

      initEngine = _lib.lookupFunction<AuralisInitCType, AuralisInitDartType>(
          'auralis_init');
      loadFile =
          _lib.lookupFunction<AuralisLoadFileCType, AuralisLoadFileDartType>(
              'auralis_load_file');
      play = _lib.lookupFunction<AuralisPlayCType, AuralisPlayDartType>(
          'auralis_play');
      pause = _lib.lookupFunction<AuralisPauseCType, AuralisPauseDartType>(
          'auralis_pause');
      seek = _lib.lookupFunction<AuralisSeekCType, AuralisSeekDartType>(
          'auralis_seek');
      setLoop =
          _lib.lookupFunction<AuralisSetLoopCType, AuralisSetLoopDartType>(
              'auralis_set_loop');
    } catch (e) {
      debugPrint(
          'FFI LOAD ERROR: Native library not found. Falling back to empty mocks! Error: $e');
      initEngine = () => true;
      loadFile = (ptr) => true;
      play = () {};
      pause = () {};
      seek = (ms) {};
      setLoop = (e, s, end) {};
    }
  }
}
