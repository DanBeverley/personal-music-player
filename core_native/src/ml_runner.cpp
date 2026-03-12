#include "auralis.h"
#include <iostream>

// --- ML Runner ---
// This will eventually #include <onnxruntime_c_api.h>

AURALIS_EXPORT int auralis_detect_chorus(const char* filepath, ChorusBoundary* out_boundaries, int max_boundaries) {
    std::cout << "[ML] Detecting chorus for: " << filepath << std::endl;
    
    // Fake data for prototyping the FFI boundaries
    if (max_boundaries >= 1) {
        out_boundaries[0].start_ms = 45000;
        out_boundaries[0].end_ms = 75000;
        return 1;
    }
    
    return 0;
}
