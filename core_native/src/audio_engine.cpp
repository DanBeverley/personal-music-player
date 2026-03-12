// Define MINIAUDIO_IMPLEMENTATION in exactly one C/C++ file
#define MINIAUDIO_IMPLEMENTATION
#include "miniaudio.h"
#include "auralis.h"

#include <iostream>
#ifdef __ANDROID__
#include <android/log.h>
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "AuralisEngine", __VA_ARGS__)
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, "AuralisEngine", __VA_ARGS__)
#else
#define LOGE(...) std::cerr << __VA_ARGS__ << std::endl
#define LOGI(...) std::cout << __VA_ARGS__ << std::endl
#endif
#include <mutex>
#include <atomic>

// Global state for simplicity in prototyping
static ma_engine engine;
static ma_sound current_sound;
static bool is_initialized = false;
static bool sound_loaded = false;

// Loop state
static std::atomic<bool> loop_enabled{false};
static std::atomic<uint32_t> loop_start_ms{0};
static std::atomic<uint32_t> loop_end_ms{0};

// Custom data source callback could be implemented here for gapless looping,
// but for MVP we rely on miniaudio's internal loop points if possible,
// or we will manually seek during the audio callback.
// Actually, ma_sound_set_looping & ma_data_source_set_loop_point_in_pcm_frames
// provides gapless looping natively!

AURALIS_EXPORT bool auralis_init() {
    if (is_initialized) return true;
    
    ma_engine_config engineConfig = ma_engine_config_init();
    // Reverting strict periodSize constraint to fix Android silence bug:
    // engineConfig.periodSizeInMilliseconds = 10;
    
    ma_result result = ma_engine_init(&engineConfig, &engine);
    if (result != MA_SUCCESS) {
        LOGE("Failed to initialize audio engine. Error code: %d", result);
        return false;
    }
    
    is_initialized = true;
    return true;
}

AURALIS_EXPORT void auralis_shutdown() {
    if (!is_initialized) return;
    
    if (sound_loaded) {
        ma_sound_uninit(&current_sound);
        sound_loaded = false;
    }
    
    ma_engine_uninit(&engine);
    is_initialized = false;
}

AURALIS_EXPORT bool auralis_load_file(const char* filepath) {
    if (!is_initialized) return false;
    
    if (sound_loaded) {
        ma_sound_uninit(&current_sound);
        sound_loaded = false;
    }
    
    ma_result result = ma_sound_init_from_file(
        &engine, filepath, MA_SOUND_FLAG_STREAM, NULL, NULL, &current_sound);
        
    if (result != MA_SUCCESS) {
        LOGE("Failed to load sound file: %s. Error code: %d", filepath, result);
        return false;
    }
    
    LOGI("Successfully loaded sound stream: %s", filepath);
    
    sound_loaded = true;
    return true;
}

AURALIS_EXPORT void auralis_play() {
    if (sound_loaded) {
        ma_sound_start(&current_sound);
    }
}

AURALIS_EXPORT void auralis_pause() {
    if (sound_loaded) {
        ma_sound_stop(&current_sound);
    }
}

AURALIS_EXPORT void auralis_stop() {
    if (sound_loaded) {
        ma_sound_stop(&current_sound);
        ma_sound_seek_to_pcm_frame(&current_sound, 0);
    }
}

AURALIS_EXPORT void auralis_seek(uint32_t position_ms) {
    if (!sound_loaded) return;
    
    uint32_t sample_rate;
    ma_sound_get_data_format(&current_sound, NULL, NULL, &sample_rate, NULL, 0);
    uint64_t target_frame = (uint64_t)position_ms * sample_rate / 1000;
    
    ma_sound_seek_to_pcm_frame(&current_sound, target_frame);
}

AURALIS_EXPORT void auralis_set_loop(bool enable, uint32_t start_ms, uint32_t end_ms) {
    if (!sound_loaded) return;
    
    if (enable) {
        // Convert ms to PCM frames
        // In miniaudio, you can fetch format info:
        uint32_t sample_rate;
        ma_sound_get_data_format(&current_sound, NULL, NULL, &sample_rate, NULL, 0);
        
        uint64_t start_frame = (uint64_t)start_ms * sample_rate / 1000;
        uint64_t end_frame = (uint64_t)end_ms * sample_rate / 1000;
        
        ma_data_source_set_loop_point_in_pcm_frames(current_sound.pDataSource, start_frame, end_frame);
        ma_sound_set_looping(&current_sound, true);
    } else {
        ma_sound_set_looping(&current_sound, false);
    }
}
