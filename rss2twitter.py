import hashlib
import html
import json
import os
import re
import tempfile
from pathlib import Path

import feedparser
import requests
import tweepy

STATE_FILE = Path(__file__).with_name("published_ids.json")
MAX_MEDIA = 4
TIMEOUT = 15
USER_AGENT = "rss2twitter-bot/1.0"


def load_published_ids() -> set:
    if not STATE_FILE.exists():
        return set()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return set(data.get("published", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_published_ids(published_ids: set) -> None:
    STATE_FILE.write_text(
        json.dumps({"published": sorted(published_ids)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_env(name: str, required: bool = True) -> str:
    value = os.getenv(name, "")
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def parse_feed_urls() -> list[str]:
    raw = get_env("RSS_FEED_URLS")
    urls = []
    for line in re.split(r"[,;\n]+", raw):
        line = line.strip()
        if line:
            urls.append(line)
    return urls


def build_twitter_api() -> tweepy.API:
    api_key = get_env("TWITTER_API_KEY")
    api_secret = get_env("TWITTER_API_SECRET_KEY")
    access_token = get_env("TWITTER_ACCESS_TOKEN")
    access_secret = get_env("TWITTER_ACCESS_TOKEN_SECRET")

    auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
    return tweepy.API(auth, wait_on_rate_limit=True, wait_on_rate_limit_notify=False)


def entry_unique_id(entry: dict) -> str:
    raw_id = entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title")
    if raw_id:
        return str(raw_id)

    content = entry.get("summary", "") or entry.get("title", "")
    raw_hash = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
    return raw_hash


def extract_image_urls(entry: dict) -> list[str]:
    urls = []

    for enclosure in entry.get("enclosures", []):
        href = enclosure.get("href") or enclosure.get("url")
        media_type = enclosure.get("type", "")
        if href and media_type.startswith("image"):
            urls.append(href)

    for content in entry.get("content", []):
        html_text = content.get("value") if isinstance(content, dict) else str(content)
        urls.extend(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_text))

    summary = entry.get("summary", "")
    urls.extend(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', summary))

    return [url for url in urls if url]


def download_image(url: str) -> Path | None:
    try:
        response = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, stream=True)
        response.raise_for_status()
    except requests.RequestException:
        return None

    suffix = Path(url).suffix
    if not suffix or len(suffix) > 8:
        suffix = ".jpg"

    file_path = Path(tempfile.mkdtemp()) / f"image{suffix}"
    try:
        with file_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    handle.write(chunk)
        return file_path
    except OSError:
        return None


def build_tweet_text(entry: dict) -> str:
    title = entry.get("title", "")
    link = entry.get("link", "")
    tweet = f"{title}\n{link}" if title and link else title or link
    tweet = html.unescape(tweet).strip()
    if len(tweet) > 280:
        tweet = tweet[:276].rstrip() + "..."
    return tweet


def publish_entry(api: tweepy.API, entry: dict) -> bool:
    media_ids = []
    image_urls = extract_image_urls(entry)
    for image_url in image_urls[:MAX_MEDIA]:
        image_path = download_image(image_url)
        if not image_path:
            continue
        try:
            result = api.media_upload(str(image_path))
            media_ids.append(result.media_id_string)
        except Exception:
            continue
        finally:
            try:
                image_path.unlink()
            except OSError:
                pass

    tweet_text = build_tweet_text(entry)
    if not tweet_text:
        return False

    api.update_status(status=tweet_text, media_ids=media_ids or None)
    return True


def sort_entries(entries: list[dict]) -> list[dict]:
    def key(entry: dict):
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published:
            return tuple(published)
        return entry_unique_id(entry)

    return sorted(entries, key=key)


def main() -> None:
    feed_urls = parse_feed_urls()
    if not feed_urls:
        raise RuntimeError("No RSS feed URLs configured. Set RSS_FEED_URLS environment variable.")

    api = build_twitter_api()
    published_ids = load_published_ids()
    found_new = False

    for feed_url in feed_urls:
        feed = feedparser.parse(feed_url)
        if feed.bozo:
            print(f"警告: RSS 源解析失败: {feed_url}")
            continue

        for entry in sort_entries(feed.entries):
            uid = entry_unique_id(entry)
            if uid in published_ids:
                continue

            try:
                print(f"发布: {entry.get('title', entry.get('link', uid))}")
                if publish_entry(api, entry):
                    published_ids.add(uid)
                    found_new = True
            except Exception as exc:
                print(f"发布失败: {uid} -> {exc}")

    if found_new:
        save_published_ids(published_ids)
        print(f"已保存 {len(published_ids)} 个已发布条目。")
    else:
        print("没有检测到新的 RSS 条目。")


if __name__ == "__main__":
    main()
