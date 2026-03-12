from ytmusicapi import YTMusic
import json

ytmusic = YTMusic()
song = ytmusic.get_song("killer queen") # Wait, video id for Killer Queen is e.g. "2ZBtPf7FOoM"
results = ytmusic.search("killer queen", filter="songs", limit=1)
v_id = results[0]['videoId']
print("Video:", v_id)

song = ytmusic.get_song(v_id)
watch = ytmusic.get_watch_playlist(videoId=v_id)
print("Keys in song:", song.keys())
print("Video details:", song.get('videoDetails'))
if 'microformat' in song:
    mf = song['microformat'].get('microformatDataRenderer', {})
    print("Publish date:", mf.get('publishDate', 'N/A'))
