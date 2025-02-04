#!/usr/bin/env python3
# vim: ai ts=4 sts=4 et sw=4 nu

from http import HTTPStatus

import requests
import multiprocessing
from dateutil import parser as dt_parser
from zimscraperlib.download import stream_file
from zimscraperlib.image.transformation import resize_image

from youtube2zim.constants import CHANNEL, PLAYLIST, USER, YOUTUBE, logger
from youtube2zim.utils import get_slug, load_json, save_json

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
PLAYLIST_API = f"{YOUTUBE_API}/playlists"
PLAYLIST_ITEMS_API = f"{YOUTUBE_API}/playlistItems"
CHANNEL_SECTIONS_API = f"{YOUTUBE_API}/channelSections"
CHANNELS_API = f"{YOUTUBE_API}/channels"
SEARCH_API = f"{YOUTUBE_API}/search"
VIDEOS_API = f"{YOUTUBE_API}/videos"
MAX_VIDEOS_PER_REQUEST = 50  # for VIDEOS_API
RESULTS_PER_PAGE = 50  # max: 50
REQUEST_TIMEOUT = 60


class Playlist:
    def __init__(self, playlist_id, title, description, creator_id, creator_name):
        self.playlist_id = playlist_id
        self.title = title
        self.description = description
        self.creator_id = creator_id
        self.creator_name = creator_name
        self.slug = get_slug(title, js_safe=True)

    @classmethod
    def from_id(cls, playlist_id):
        playlist_json = get_playlist_json(playlist_id)
        return Playlist(
            playlist_id=playlist_id,
            title=playlist_json["snippet"]["title"],
            description=playlist_json["snippet"]["description"],
            creator_id=playlist_json["snippet"]["channelId"],
            creator_name=playlist_json["snippet"]["channelTitle"],
        )

    def __dict__(self):
        return {
            "playlist_id": self.playlist_id,
            "title": self.title,
            "description": self.description,
            "creator_id": self.creator_id,
            "creator_name": self.creator_name,
            "slug": self.slug.replace("_", "-"),
        }


def credentials_ok():
    """Check that a YouTube search is successful, validating API_KEY."""
    req = requests.get(
        SEARCH_API,
        params={"part": "snippet", "maxResults": 1, "key": YOUTUBE.api_key},
        timeout=REQUEST_TIMEOUT,
    )
    if req.status_code >= HTTPStatus.BAD_REQUEST:
        logger.error(f"HTTP {req.status_code} Error response: {req.text}")
    try:
        req.raise_for_status()
        return bool(req.json()["items"])
    except Exception:
        return False


def get_channel_json(channel_id, *, for_username=False):
    """Fetch or retrieve-save and return the YouTube ChannelResult JSON."""
    fname = f"channel_{channel_id}"
    channel_json = load_json(YOUTUBE.cache_dir, fname)
    if channel_json is None:
        logger.debug(f"Query YouTube API for Channel #{channel_id}")
        req = requests.get(
            CHANNELS_API,
            params={
                "forUsername" if for_username else "id": channel_id,
                "part": "brandingSettings,snippet,contentDetails",
                "key": YOUTUBE.api_key,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if req.status_code >= HTTPStatus.BAD_REQUEST:
            logger.error(f"HTTP {req.status_code} Error response: {req.text}")
        req.raise_for_status()
        try:
            channel_json = req.json()["items"][0]
        except (KeyError, IndexError):
            if for_username:
                logger.error(f"Invalid username `{channel_id}`: Not Found")
            else:
                logger.error(f"Invalid channelId `{channel_id}`: Not Found")
            raise
        save_json(YOUTUBE.cache_dir, fname, channel_json)
    return channel_json


def get_channel_playlists_json(channel_id):
    """Fetch or retrieve-save and return the YouTube Playlists JSON for a channel."""
    fname = f"channel_{channel_id}_playlists"
    channel_playlists_json = load_json(YOUTUBE.cache_dir, fname)

    if channel_playlists_json is not None:
        return channel_playlists_json

    logger.debug(f"Query YouTube API for Playlists of channel #{channel_id}")

    items = []
    page_token = None
    while True:
        req = requests.get(
            PLAYLIST_API,
            params={
                "channelId": channel_id,
                "part": "id",
                "key": YOUTUBE.api_key,
                "maxResults": RESULTS_PER_PAGE,
                "pageToken": page_token,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if req.status_code >= HTTPStatus.BAD_REQUEST:
            logger.error(f"HTTP {req.status_code} Error response: {req.text}")
        req.raise_for_status()
        channel_playlists_json = req.json()
        items += channel_playlists_json["items"]
        save_json(YOUTUBE.cache_dir, fname, items)
        page_token = channel_playlists_json.get("nextPageToken")
        if not page_token:
            break
    return items


def get_playlist_json(playlist_id):
    """Fetch or retrieve-save and return the YouTube PlaylistResult JSON."""
    fname = f"playlist_{playlist_id}"
    playlist_json = load_json(YOUTUBE.cache_dir, fname)
    if playlist_json is None:
        logger.debug(f"Query YouTube API for Playlist #{playlist_id}")
        req = requests.get(
            PLAYLIST_API,
            params={"id": playlist_id, "part": "snippet", "key": YOUTUBE.api_key},
            timeout=REQUEST_TIMEOUT,
        )
        if req.status_code >= HTTPStatus.BAD_REQUEST:
            logger.error(f"HTTP {req.status_code} Error response: {req.text}")
        req.raise_for_status()
        try:
            playlist_json = req.json()["items"][0]
        except IndexError:
            logger.error(f"Invalid playlistId `{playlist_id}`: Not Found")
            raise
        save_json(YOUTUBE.cache_dir, fname, playlist_json)
    return playlist_json


def get_videos_json(playlist_id):
    """Retrieve a list of YouTube PlaylistItem dicts with necessary details."""

    fname = f"playlist_{playlist_id}_videos"
    items = load_json(YOUTUBE.cache_dir, fname)
    if items is not None:
        return items

    logger.debug(f"Querying YouTube API for PlaylistItems of playlist #{playlist_id}")

    items = []
    page_token = None
    while True:
        req = requests.get(
            PLAYLIST_ITEMS_API,
            params={
                "playlistId": playlist_id,
                "part": "snippet,contentDetails",
                "key": YOUTUBE.api_key,
                "maxResults": RESULTS_PER_PAGE,
                "pageToken": page_token,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if req.status_code >= HTTPStatus.BAD_REQUEST:
            logger.error(f"HTTP {req.status_code} Error response: {req.text}")
        req.raise_for_status()
        videos_json = req.json()
        items += videos_json["items"]
        page_token = videos_json.get("nextPageToken")
        if not page_token:
            break

    save_json(YOUTUBE.cache_dir, fname, items)
    return items


def get_videos_authors_info(videos_ids):
    """Query authors' info for each video from their respective channel."""

    items = load_json(YOUTUBE.cache_dir, "videos_channels")

    if items is not None:
        return items

    logger.debug(f"Querying YouTube API for Video details of {len(videos_ids)} videos")

    items = {}

    def retrieve_videos_for(videos_ids):
        """{videoId: {channelId: channelTitle}} for all videos_ids."""
        req_items = {}
        page_token = None
        while True:
            req = requests.get(
                VIDEOS_API,
                params={
                    "id": ",".join(videos_ids),
                    "part": "snippet",
                    "key": YOUTUBE.api_key,
                    "maxResults": RESULTS_PER_PAGE,
                    "pageToken": page_token,
                },
                timeout=REQUEST_TIMEOUT,
            )
            if req.status_code >= HTTPStatus.BAD_REQUEST:
                logger.error(f"HTTP {req.status_code} Error response: {req.text}")
            req.raise_for_status()
            videos_json = req.json()
            for item in videos_json["items"]:
                req_items.update(
                    {
                        item["id"]: {
                            "channelId": item["snippet"]["channelId"],
                            "channelTitle": item["snippet"]["channelTitle"],
                        }
                    }
                )
            page_token = videos_json.get("nextPageToken")
            if not page_token:
                break
        return req_items

    # Split it over n requests so that each request includes
    # at most MAX_VIDEOS_PER_REQUEST videoId to avoid URI size issues
    for interv in range(0, len(videos_ids), MAX_VIDEOS_PER_REQUEST):
        items.update(
            retrieve_videos_for(videos_ids[interv : interv + MAX_VIDEOS_PER_REQUEST])
        )

    save_json(YOUTUBE.cache_dir, "videos_channels", items)

    return items


def save_channel_branding(channels_dir, channel_id, *, save_banner=False):
    """Download, save, and resize profile [and banner] of a channel."""
    channel_json = get_channel_json(channel_id)

    thumbnails = channel_json["snippet"]["thumbnails"]
    thumbnail = None
    for quality in ("medium", "default"):  # high:800px, medium:240px, default:88px
        if quality in thumbnails.keys():
            thumbnail = thumbnails[quality]["url"]
            break

    channel_dir = channels_dir.joinpath(channel_id)
    channel_dir.mkdir(exist_ok=True)

    profile_path = channel_dir.joinpath("profile.jpg")
    if not profile_path.exists():
        if not thumbnail:
            raise Exception("Thumbnail not found")
        stream_file(thumbnail, profile_path)
        # Resize profile as we only use up 100px/80 sq
        resize_image(profile_path, width=100, height=100)

    # Currently disabled as per deprecation of the following property
    # without an alternative way to retrieve it (using the API)
    # See: https://developers.google.com/youtube/v3/revision_history#september-9,-2020
    if save_banner and False:
        banner = channel_json["brandingSettings"]["image"]["bannerImageUrl"]
        banner_path = channel_dir.joinpath("banner.jpg")
        if not banner_path.exists():
            stream_file(banner, banner_path)


def skip_deleted_videos(item):
    """Filter func to filter-out deleted videos from list."""
    return (
        item["snippet"]["title"] != "Deleted video"
        and item["snippet"]["description"] != "This video is unavailable."
    )


def skip_outofrange_videos(date_range, item):
    """Filter func to filter-out videos that are not within specified date range."""
    return dt_parser.parse(item["snippet"]["publishedAt"]).date() in date_range


def extract_playlists_details_from(collection_type, youtube_id):
    

    uploads_playlist_id = None
    main_channel_id = None
    if collection_type in (USER, CHANNEL):
        if collection_type == USER:
            # youtube_id is a just a name to fetch actual channelId through channel
            channel_json = get_channel_json(youtube_id, for_username=True)
        else:
            # youtube_id is a channelId
            channel_json = get_channel_json(youtube_id)

        main_channel_id = channel_json["id"]

        
        playlist_ids = [p["id"] for p in get_channel_playlists_json(main_channel_id)]
        
        playlist_ids += [channel_json["contentDetails"]["relatedPlaylists"]["uploads"]]
        uploads_playlist_id = playlist_ids[-1]
    elif collection_type == PLAYLIST:
        playlist_ids = youtube_id.split(",")
        main_channel_id = Playlist.from_id(playlist_ids[0]).creator_id
    else:
        raise NotImplementedError("Unsupported collection_type")

    return (
        [Playlist.from_id(playlist_id) for playlist_id in list(set(playlist_ids))],
        main_channel_id,
        uploads_playlist_id,
    )


def generate_subs(video):
    # code here to be added for the subtitles 
    pass 

def generate_all_subs(videos):
    with multiprocessing.Pool() as pool: 
        pool.map(generate_subs, videos)

def main():
    
    pass


if __name__ == "__main__":
    main()
