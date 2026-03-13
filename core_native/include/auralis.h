#ifndef AURALIS_H
#define AURALIS_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// API definitions for Dart FFI
#if defined(_WIN32)
#define AURALIS_EXPORT __declspec(dllexport)
#else
#define AURALIS_EXPORT __attribute__((visibility("default")))
#endif

// --- Audio Engine ---
AURALIS_EXPORT bool auralis_init();
AURALIS_EXPORT void auralis_shutdown();
AURALIS_EXPORT bool auralis_load_file(const char* filepath);
AURALIS_EXPORT void auralis_play();
AURALIS_EXPORT void auralis_pause();
AURALIS_EXPORT void auralis_stop();
AURALIS_EXPORT void auralis_seek(uint32_t position_ms);
AURALIS_EXPORT uint32_t auralis_get_position();
AURALIS_EXPORT void auralis_set_loop(bool enable, uint32_t start_ms, uint32_t end_ms);

// --- DSP Effects ---
AURALIS_EXPORT void auralis_set_pitch(float pitch);
AURALIS_EXPORT void auralis_set_rate(float rate);

// --- ML Runner ---
typedef struct {
    uint32_t start_ms;
    uint32_t end_ms;
} ChorusBoundary;

AURALIS_EXPORT int auralis_detect_chorus(const char* filepath, ChorusBoundary* out_boundaries, int max_boundaries);

#ifdef __cplusplus
}
#endif

#endif // AURALIS_H
