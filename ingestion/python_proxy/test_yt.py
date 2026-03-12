from ytmusicapi import YTMusic
import sys
import traceback

print("Testing ytmusicapi...")
ytmusic = YTMusic()
try:
    print("Suggest:")
    print(ytmusic.get_search_suggestions("killer queen"))
except Exception as e:
    traceback.print_exc()

try:
    print("Recommend (home):")
    home = ytmusic.get_home(limit=1)
    print("Home feeds:", len(home))
    if len(home) > 0:
        print(home[0])
except Exception as e:
    traceback.print_exc()
