cmake_minimum_required(VERSION 3.14)
project(AuralisEngine VERSION 1.0 LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED True)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

add_library(auralis SHARED
    src/audio_engine.cpp
    src/dsp_effects.cpp
    src/ml_runner.cpp
)

target_include_directories(auralis PUBLIC include)

if(WIN32)
    # Additional links for Windows
elseif(APPLE)
    # Provide framework settings or .dylib
elseif(ANDROID)
    find_library(log-lib log)
    target_link_libraries(auralis ${log-lib})
endif()
