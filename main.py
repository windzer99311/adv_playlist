import requests
import re
import json
import random
from urllib.parse import urlparse, parse_qs
from fastapi import FastAPI, HTTPException

app = FastAPI()

# ── Constants ────────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

INNERTUBE_API_URL = "https://www.youtube.com/youtubei/v1/browse"
INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
INNERTUBE_CONTEXT = {
    "client": {
        "clientName": "WEB",
        "clientVersion": "2.20240101.00.00",
        "hl": "en",
        "gl": "US",
    }
}


# ── URL Parser ────────────────────────────────────────────────────────────────

def normalize_to_playlist_url(url: str) -> str:
    """
    Converts any YouTube URL into a clean playlist URL that Vercel can handle.
      - Has list=  → https://youtube.com/playlist?list={list_id}
      - Only v=    → https://youtube.com/playlist?list=RD{video_id}
      - /shorts/   → https://youtube.com/playlist?list=RD{video_id}
    """
    url = url.replace("https://m.youtube.com", "https://www.youtube.com")
    if not url.startswith("https://www."):
        url = url.replace("https://youtube.com", "https://www.youtube.com")

    # youtu.be/VIDEO_ID short links
    if "youtu.be/" in url:
        video_id = urlparse(url).path.lstrip("/")
        return f"https://youtube.com/playlist?list=RD{video_id}"

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    path = parsed.path.rstrip("/")

    list_id = qs.get("list", [None])[0]
    video_id = qs.get("v", [None])[0]

    # shorts
    if "/shorts/" in path:
        video_id = path.split("/shorts/")[1]

    if list_id:
        return f"https://youtube.com/playlist?list={list_id}"
    elif video_id:
        return f"https://youtube.com/playlist?list=RD{video_id}"
    else:
        raise ValueError(f"Could not extract list or video ID from URL: {url}")


def parse_youtube_url(url: str) -> dict:
    """
    Normalizes any YouTube URL then detects its type.

    Supported types:
      - radio   : list=RDxxx
      - playlist: list=PLxxx / list=anything else
    """
    norm = normalize_to_playlist_url(url)
    parsed = urlparse(norm)
    qs = parse_qs(parsed.query)
    list_id = qs.get("list", [None])[0]

    if not list_id:
        raise ValueError(f"Could not parse normalized URL: {norm}")

    if list_id.startswith("RD"):
        video_id = list_id[2:]
        return {"type": "radio", "video_id": video_id, "list_id": list_id, "normalized_url": norm}

    return {"type": "playlist", "list_id": list_id, "normalized_url": norm}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
    }


def fetch_page(url: str) -> str:
    resp = requests.get(url, headers=get_headers(), timeout=15)
    resp.raise_for_status()
    return resp.text


def extract_initial_data(html: str) -> dict:
    """Pull ytInitialData JSON from any YouTube HTML page."""
    # Try both common patterns
    for pattern in [
        r"var ytInitialData\s*=\s*(\{.*?\});\s*(?:var|</script>|window\[)",
        r"ytInitialData\s*=\s*(\{.*?\});\s*(?:var|</script>|window\[)",
    ]:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue

    raise ValueError("ytInitialData not found or not parseable")


def safe_get(d: dict, *keys):
    """Safely traverse nested dicts/lists."""
    for k in keys:
        if d is None:
            return None
        if isinstance(d, list):
            try:
                d = d[int(k)]
            except (IndexError, ValueError, TypeError):
                return None
        elif isinstance(d, dict):
            d = d.get(k)
        else:
            return None
    return d


def get_text(obj) -> str | None:
    """Extract text from YouTube's various text formats."""
    if not obj:
        return None
    if isinstance(obj, str):
        return obj
    if "simpleText" in obj:
        return obj["simpleText"]
    if "runs" in obj:
        return "".join(r.get("text", "") for r in obj["runs"])
    return None


def worst_thumbnail(thumbs: list) -> str | None:
    if not thumbs:
        return None
    return thumbs[0].get("url")


# ── Item Parsers ──────────────────────────────────────────────────────────────

def parse_playlist_video_renderer(item: dict) -> dict | None:
    """Standard playlist page item."""
    r = item.get("playlistVideoRenderer", {})
    if not r:
        return None
    vid = safe_get(r, "navigationEndpoint", "watchEndpoint", "videoId")
    if not vid:
        return None
    return {
        "title": get_text(r.get("title")),
        "thumbnail": worst_thumbnail(safe_get(r, "thumbnail", "thumbnails") or []),
        "videoId": vid,
    }


def parse_panel_video_renderer(item: dict) -> dict | None:
    """Watch-next panel item (radio/mix/watch+list)."""
    r = item.get("playlistPanelVideoRenderer", {})
    if not r:
        return None
    vid = safe_get(r, "navigationEndpoint", "watchEndpoint", "videoId")
    if not vid:
        return None
    return {
        "title": get_text(r.get("title")),
        "thumbnail": worst_thumbnail(safe_get(r, "thumbnail", "thumbnails") or []),
        "videoId": vid,
    }


def parse_lockup_view_model(item: dict) -> dict | None:
    """Newer YouTube lockupViewModel format (secondary results)."""
    lvm = item.get("lockupViewModel", {})
    if not lvm:
        return None
    try:
        title = safe_get(lvm, "metadata", "lockupMetadataViewModel", "title", "content")
        thumb = safe_get(lvm, "contentImage", "thumbnailViewModel", "image", "sources", 0, "url")
        # video ID is buried deep inside the action chain
        vid = safe_get(
            lvm,
            "metadata", "lockupMetadataViewModel", "menuButton",
            "buttonViewModel", "onTap", "innertubeCommand",
            "showSheetCommand", "panelLoadingStrategy", "inlineContent",
            "sheetViewModel", "content", "listViewModel", "listItems",
            0, "listItemViewModel", "rendererContext", "commandContext",
            "onTap", "innertubeCommand", "signalServiceEndpoint", "actions",
            0, "addToPlaylistCommand", "videoId"
        )
        if not vid:
            return None
        return {"title": title, "thumbnail": thumb, "videoId": vid}
    except Exception:
        return None


# ── Continuation (pagination) ─────────────────────────────────────────────────

def get_continuation_token(data: dict) -> str | None:
    """Recursively search for a continuationCommand token."""
    if isinstance(data, dict):
        if "continuationCommand" in data:
            return data["continuationCommand"].get("token")
        for v in data.values():
            result = get_continuation_token(v)
            if result:
                return result
    elif isinstance(data, list):
        for item in data:
            result = get_continuation_token(item)
            if result:
                return result
    return None


def fetch_continuation(token: str) -> dict:
    """Call YouTube's internal browse API for the next page."""
    resp = requests.post(
        INNERTUBE_API_URL,
        params={"key": INNERTUBE_KEY},
        json={"continuation": token, "context": INNERTUBE_CONTEXT},
        headers={**get_headers(), "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def extract_items_from_continuation(data: dict) -> tuple[list, str | None]:
    """Pull video items and next token from a continuation response."""
    items = []
    token = None

    # Items live inside onResponseReceivedActions
    for action in data.get("onResponseReceivedActions", []):
        contents = (
                safe_get(action, "appendContinuationItemsAction", "continuationItems") or
                safe_get(action, "reloadContinuationItemsCommand", "continuationItems") or
                []
        )
        for c in contents:
            parsed = (
                    parse_playlist_video_renderer(c) or
                    parse_panel_video_renderer(c)
            )
            if parsed:
                items.append(parsed)
            # continuation button
            if "continuationItemRenderer" in c:
                token = get_continuation_token(c)

    return items, token


# ── Core Extractors ───────────────────────────────────────────────────────────

def find_playlist_contents(data) -> list | None:
    """Recursively search for playlistVideoListRenderer contents anywhere in ytInitialData."""
    if isinstance(data, dict):
        if "playlistVideoListRenderer" in data:
            return data["playlistVideoListRenderer"].get("contents")
        for v in data.values():
            result = find_playlist_contents(v)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = find_playlist_contents(item)
            if result is not None:
                return result
    return None


def extract_playlist_page(list_id: str, max_pages: int = 10) -> list:
    """Extract full playlist from youtube.com/playlist?list=xxx"""
    url = f"https://www.youtube.com/playlist?list={list_id}"
    html = fetch_page(url)
    data = extract_initial_data(html)

    # Try known path first, fall back to recursive search
    contents = None
    try:
        contents = (
            data["contents"]
            ["twoColumnBrowseResultsRenderer"]["tabs"][0]
            ["tabRenderer"]["content"]
            ["sectionListRenderer"]["contents"][0]
            ["itemSectionRenderer"]["contents"][0]
            ["playlistVideoListRenderer"]["contents"]
        )
    except (KeyError, IndexError, TypeError):
        contents = find_playlist_contents(data)

    if not contents:
        raise ValueError("Could not find playlist contents in ytInitialData")

    videos = []
    token = None
    for c in contents:
        parsed = parse_playlist_video_renderer(c)
        if parsed:
            videos.append(parsed)
        if "continuationItemRenderer" in c:
            token = get_continuation_token(c)

    # Paginate
    page = 1
    while token and page < max_pages:
        cont_data = fetch_continuation(token)
        more, token = extract_items_from_continuation(cont_data)
        videos += more
        page += 1

    return videos


def extract_watch_next_playlist(video_id: str, list_id: str) -> list:
    """Extract playlist shown in the watch-next panel."""
    url = f"https://www.youtube.com/watch?v={video_id}&list={list_id}"
    html = fetch_page(url)
    data = extract_initial_data(html)

    try:
        contents = (
            data["contents"]
            ["twoColumnWatchNextResults"]["playlist"]
            ["playlist"]["contents"]
        )
    except (KeyError, TypeError):
        raise ValueError("Could not find watch-next playlist in ytInitialData")

    videos = []
    for c in contents:
        parsed = parse_panel_video_renderer(c)
        if parsed:
            videos.append(parsed)
    return videos


def extract_radio(video_id: str, list_id: str) -> list:
    """Extract YouTube Mix / Radio playlist."""
    url = f"https://www.youtube.com/watch?v={video_id}&list={list_id}"
    html = fetch_page(url)
    data = extract_initial_data(html)

    try:
        contents = (
            data["contents"]
            ["twoColumnWatchNextResults"]["playlist"]
            ["playlist"]["contents"]
        )
        videos = []
        for c in contents:
            parsed = parse_panel_video_renderer(c)
            if parsed and parsed["videoId"] != video_id:
                videos.append(parsed)
        return videos
    except (KeyError, TypeError):
        pass

    # Fallback: secondary results
    try:
        results = (
            data["contents"]
            ["twoColumnWatchNextResults"]["secondaryResults"]
            ["secondaryResults"]["results"]
        )
        videos = []
        for item in results:
            parsed = parse_lockup_view_model(item)
            if parsed:
                videos.append(parsed)
        return videos
    except (KeyError, TypeError):
        raise ValueError("Could not extract radio playlist")


def extract_single_video(video_id: str) -> list:
    """For a plain video URL, return just its metadata as a single-item list."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    html = fetch_page(url)
    data = extract_initial_data(html)

    try:
        vd = data["videoDetails"]
        return [{
            "title": vd.get("title"),
            "thumbnail": worst_thumbnail(safe_get(vd, "thumbnail", "thumbnails") or []),
            "videoId": vd.get("videoId"),
        }]
    except (KeyError, TypeError):
        raise ValueError("Could not extract video details")


# ── FastAPI Endpoint ──────────────────────────────────────────────────────────

@app.get("/playlist")
def get_playlist(url: str, max_pages: int = 10):
    """
    Universal YouTube extractor.

    Supports:
      - youtube.com/playlist?list=PLxxx          → full playlist
      - youtube.com/watch?v=xxx&list=PLxxx       → watch-next playlist
      - youtube.com/watch?v=xxx&list=RDxxx       → radio/mix
      - youtube.com/watch?v=xxx                  → single video info
      - youtube.com/shorts/xxx                   → single short info
    """
    try:
        info = parse_youtube_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        match info["type"]:
            case "playlist":
                videos = extract_watch_next_playlist("zaFGQEIcetM", info["list_id"])

            case "radio":
                videos = extract_radio(info["video_id"], info["list_id"])

            case _:
                raise HTTPException(status_code=400, detail=f"Unsupported URL type: {info['type']}")

    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except requests.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"YouTube request failed: {e}")

    return {"data": videos}
