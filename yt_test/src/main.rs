use rusty_ytdl::{Video, VideoOptions, VideoQuality, VideoSearchOptions};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let url = "https://www.youtube.com/watch?v=v2AC41dglnM&hl=en";
    let client = reqwest::Client::builder()
        .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        .build()?;

    let video_options = VideoOptions {
        quality: VideoQuality::HighestAudio,
        filter: VideoSearchOptions::Audio,
        request_options: rusty_ytdl::RequestOptions {
            client: Some(client),
            ..Default::default()
        },
        ..Default::default()
    };

    println!("Fetching video info...");
    let video = match Video::new_with_options(url, video_options) {
        Ok(v) => v,
        Err(e) => {
            println!("Error instantiating video: {:?}", e);
            return Err(e.into());
        }
    };

    println!("Fetching stream info...");
    match video.get_info().await {
        Ok(info) => {
            println!("Success! Title: {}", info.video_details.title);
        }
        Err(e) => {
            println!("Error getting info: {:?}", e);
        }
    }

    Ok(())
}
