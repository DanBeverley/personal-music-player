from ytmusicapi import YTMusic
import json
import traceback

ytmusic = YTMusic()
results = ytmusic.search("Bohemian Rhapsody", filter="songs", limit=1)
v_id = results[0]['videoId']
print("Video:", v_id)

try:
    song = ytmusic.get_song(v_id)
    watch = ytmusic.get_watch_playlist(videoId=v_id)
    print("Keys in song:", song.keys())
    
    release_date = ""
    artist = ""
    album = ""
    
    if "microformat" in song and "microformatDataRenderer" in song["microformat"]:
        release_date = song["microformat"]["microformatDataRenderer"].get("publishDate", "")
        
    vd = song.get("videoDetails", {})
    artist = vd.get("author", "")
    print("Artist:", artist)
    print("Release date:", release_date)
except Exception as e:
    print("ERROR!!!!")
    traceback.print_exc()
