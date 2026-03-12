from ytmusicapi import YTMusic
import traceback

ytmusic = YTMusic()

def test_suggest():
    try:
        suggestions = ytmusic.get_search_suggestions("killer queen")
        results = [s.get("text", s) if isinstance(s, dict) else s for s in suggestions]
        print("Suggest OK:", results)
    except Exception as e:
        print("Suggest FAIL:")
        traceback.print_exc()

def test_search():
    try:
        raw_results = ytmusic.search("killer queen", filter="songs", limit=10)
        results = []
        for entry in raw_results:
            results.append({
                "id": entry.get("videoId"),
                "title": entry.get("title"),
                "duration": entry.get("duration_seconds") or 0,
                "thumbnail": entry.get("thumbnails", [{}])[-1].get("url") if entry.get("thumbnails") else None,
                "channel": ", ".join([a.get("name", "") for a in entry.get("artists", [])])
            })
        print("Search OK. Found:", len(results))
    except Exception as e:
        print("Search FAIL:")
        traceback.print_exc()

def test_recommend():
    req_limit = 10
    try:
        home_feed = ytmusic.get_home(limit=req_limit)
        results = []
        for carousel in home_feed:
            for item in carousel.get("contents", []):
                if "videoId" in item and item["videoId"]:
                    results.append({
                        "id": item["videoId"],
                        "title": item.get("title"),
                        "duration": 0,
                        "thumbnail": item.get("thumbnails", [{}])[-1].get("url") if item.get("thumbnails") else None,
                        "channel": ", ".join([a.get("name", "") for a in item.get("artists", [])]) if item.get("artists") else "YT Music Mix"
                    })
                if len(results) >= req_limit + 10:
                    break
            if len(results) >= req_limit + 10:
                break
        print("Recommend (home) OK. Found:", len(results))
    except Exception as e:
        print("Recommend FAIL:")
        traceback.print_exc()

test_suggest()
test_search()
test_recommend()
