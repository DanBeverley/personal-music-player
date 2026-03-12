#include "auralis.h"
#include <iostream>

// --- DSP Effects ---
// These will be wired to miniaudio's internal effects node graph or a custom data source callback.

AURALIS_EXPORT void auralis_set_pitch(float pitch) {
    // Placeholder: Need to connect modifying pitch (Phase Vocoder for independent pitch vs speed)
    std::cout << "[DSP] Setting pitch to " << pitch << std::endl;
}

AURALIS_EXPORT void auralis_set_rate(float rate) {
    // Placeholder for playback speed control
    std::cout << "[DSP] Setting rate to " << rate << std::endl;
}
