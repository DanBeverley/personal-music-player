use rusty_ytdl::{Video, VideoOptions, VideoQuality, VideoSearchOptions};
use std::ffi::{CStr, CString};
use std::os::raw::c_char;
use std::path::Path;

// Expose a C-compatible synchronous function for downloading an audio stream
// returns 0 on success, non-zero on error

#[no_mangle]
pub extern "C" fn auralis_download_youtube_audio(
    url_cstr: *const c_char,
    out_path_cstr: *const c_char,
) -> i32 {
    if url_cstr.is_null() || out_path_cstr.is_null() {
        return -1;
    }

    let url = unsafe { CStr::from_ptr(url_cstr).to_string_lossy().into_owned() };
    let out_path = unsafe { CStr::from_ptr(out_path_cstr).to_string_lossy().into_owned() };

    // Setup tokio runtime since FFI calls are synchronous from the C/Dart perspective
    let rt = match tokio::runtime::Runtime::new() {
        Ok(rt) => rt,
        Err(_) => return -2,
    };

    let result = rt.block_on(async {
        // Bypass YouTube bot-detection/mobile-restrictions by impersonating a Windows Desktop Chrome client
        let client = rusty_ytdl::reqwest::Client::builder()
            .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            .build()
            .unwrap_or_default();

        let video_options = VideoOptions {
            quality: VideoQuality::HighestAudio,
            filter: VideoSearchOptions::Audio,
            request_options: rusty_ytdl::RequestOptions {
                client: Some(client),
                ..Default::default()
            },
            ..Default::default()
        };

        let video = Video::new_with_options(&url, video_options)?;
        
        // Grab the stream and download it to the specified path
        let path = Path::new(&out_path);
        video.download(path).await?;
        
        Ok::<(), Box<dyn std::error::Error>>(())
    });

    match result {
        Ok(_) => 0,
        Err(e) => {
            let err_path = format!("{}.err.txt", out_path);
            let _ = std::fs::write(&err_path, format!("Rust Error: {:?}", e));
            -3
        }
    }
}

// Memory management function to free allocated strings if we ever return them
#[no_mangle]
pub extern "C" fn auralis_free_string(s: *mut c_char) {
    if s.is_null() {
        return;
    }
    unsafe {
        let _ = CString::from_raw(s);
    }
}
