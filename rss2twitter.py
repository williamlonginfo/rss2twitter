import hashlib
import html
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import feedparser
import requests
import tweepy

STATE_FILE = Path(__file__).with_name("published_ids.json")
MAX_MEDIA = 4
TIMEOUT = 15
USER_AGENT = "rss2twitter-bot/1.0"


@dataclass(frozen=True)
class TwitterClients:
    """Twitter clients used by the publisher.

    Media upload uses Twitter/X API v1.1, while tweet creation uses API v2.
    Both clients share the same user-context OAuth 1.0a tokens.
    """

    tweets: tweepy.Client
    media: tweepy.API


def load_published_ids() -> set:
    """从磁盘读取已发布 RSS 条目的 ID。

    读取 JSON 状态文件并返回已发布 ID 的集合。若文件不存在或无效，返回空集合。
    """
    if not STATE_FILE.exists():
        return set()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return set(data.get("published", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_published_ids(published_ids: set) -> None:
    """将已发布 RSS 条目的 ID 保存到磁盘。

    将当前已发布 ID 的集合写入 JSON 状态文件，以便后续运行可以保持状态。
    """
    STATE_FILE.write_text(
        json.dumps({"published": sorted(published_ids)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_env(name: str, required: bool = True) -> str:
    """读取必需的环境变量。

    如果变量为必需但缺失，则抛出 RuntimeError。
    """
    value = os.getenv(name, "")
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def parse_feed_urls() -> list[str]:
    """从 RSS_FEED_URLS 环境变量解析 RSS 源地址。

    支持逗号、分号或换行分隔的多个 RSS URL。
    """
    raw = get_env("RSS_FEED_URLS")
    urls = []
    for line in re.split(r"[,;\n]+", raw):
        line = line.strip()
        if line:
            urls.append(line)
    return urls


def build_twitter_clients() -> TwitterClients:
    """使用环境变量凭据创建 Twitter/X 发布客户端。"""
    api_key = get_env("TWITTER_API_KEY")
    api_secret = get_env("TWITTER_API_SECRET_KEY")
    access_token = get_env("TWITTER_ACCESS_TOKEN")
    access_secret = get_env("TWITTER_ACCESS_TOKEN_SECRET")

    tweets = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
        wait_on_rate_limit=True,
    )
    media_auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
    media = tweepy.API(media_auth, wait_on_rate_limit=True)
    return TwitterClients(tweets=tweets, media=media)


def verify_twitter_credentials(clients: TwitterClients) -> bool:
    """在处理 RSS 前验证 Twitter/X 用户上下文凭据。

    免费 Project 或旧 App 配置可能暂时无法通过 API v2 校验。此时返回 False，
    让定时任务优雅跳过发布，避免 GitHub Actions 因外部服务配置问题失败。
    """
    try:
        clients.media.verify_credentials()
        clients.tweets.get_me(user_auth=True)
    except tweepy.Forbidden as exc:
        print(
            "跳过发布: Twitter/X 认证未通过。当前 API Key / Access Token 所属的 Developer App "
            "没有通过 API v2 Project 校验。请确认该 App 已附加到一个 Project，权限为 "
            "Read and write，然后重新生成 Access Token 和 Access Token Secret，并更新 "
            "GitHub Secrets。"
        )
        print(f"Twitter/X 返回: {exc}")
        return False
    return True


def entry_unique_id(entry: dict) -> str:
    """为 RSS 条目生成稳定的唯一标识符。"""
    raw_id = entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title")
    if raw_id:
        return str(raw_id)

    content = entry.get("summary", "") or entry.get("title", "")
    raw_hash = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
    return raw_hash


def extract_image_urls(entry: dict) -> list[str]:
    """从 RSS 条目中提取图片 URL。

    支持 enclosure 中的图片地址，以及条目内容或摘要中的 <img> 标签。
    """
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
    """从 URL 下载图片到临时文件。

    成功时返回本地路径，下载失败时返回 None。
    """
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
    """构建 RSS 条目的推文文本。

    使用条目标题和链接，并在超过 Twitter 280 字符限制时进行截断。
    """
    title = entry.get("title", "")
    link = entry.get("link", "")
    tweet = f"{title}\n{link}" if title and link else title or link
    tweet = html.unescape(tweet).strip()
    if len(tweet) > 280:
        tweet = tweet[:276].rstrip() + "..."
    return tweet


def publish_entry(clients: TwitterClients, entry: dict) -> bool:
    """将单个 RSS 条目发布到 Twitter。

    下载最多 MAX_MEDIA 张图片，上传到 Twitter，并发布推文文本。发布成功返回 True。
    """
    media_ids = []
    image_urls = extract_image_urls(entry)
    for image_url in image_urls[:MAX_MEDIA]:
        image_path = download_image(image_url)
        if not image_path:
            continue
        try:
            result = clients.media.media_upload(str(image_path))
            media_ids.append(result.id)
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

    if media_ids:
        clients.tweets.create_tweet(text=tweet_text, media_ids=media_ids, user_auth=True)
    else:
        clients.tweets.create_tweet(text=tweet_text, user_auth=True)
    return True


def format_publish_error(exc: Exception) -> str:
    """将 Twitter/X 发布错误转换为更容易处理的提示。"""
    message = str(exc)
    if isinstance(exc, tweepy.Forbidden):
        message += (
            "\n提示: 这是 Twitter/X API 返回的 403。请确认这四个 Secrets 来自同一个"
            "已绑定 Project 的 Developer App，并且 App 权限是 Read and write："
            "TWITTER_API_KEY、TWITTER_API_SECRET_KEY、TWITTER_ACCESS_TOKEN、"
            "TWITTER_ACCESS_TOKEN_SECRET。修改 App 权限后需要重新生成 Access Token 和"
            "Access Token Secret。发布推文必须使用用户上下文 Access Token。"
        )
    return message


def sort_entries(entries: list[dict]) -> list[dict]:
    """排序 RSS 条目，尽可能将较早的内容先发布。"""
    def key(entry: dict):
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published:
            return tuple(published)
        return entry_unique_id(entry)

    return sorted(entries, key=key)


def main() -> None:
    """主流程：抓取 RSS 源并发布新条目。"""
    feed_urls = parse_feed_urls()
    if not feed_urls:
        raise RuntimeError("No RSS feed URLs configured. Set RSS_FEED_URLS environment variable.")

    clients = build_twitter_clients()
    if not verify_twitter_credentials(clients):
        return

    published_ids = load_published_ids()
    found_new = False
    stop_publishing = False

    for feed_url in feed_urls:
        if stop_publishing:
            break

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
                if publish_entry(clients, entry):
                    published_ids.add(uid)
                    found_new = True
            except tweepy.Forbidden as exc:
                print(f"发布失败: {uid} -> {format_publish_error(exc)}")
                print("本轮发布已停止，未成功发布的条目不会写入 published_ids.json。")
                stop_publishing = True
                break
            except Exception as exc:
                print(f"发布失败: {uid} -> {format_publish_error(exc)}")

    if found_new:
        save_published_ids(published_ids)
        print(f"已保存 {len(published_ids)} 个已发布条目。")
    else:
        print("没有检测到新的 RSS 条目。")


if __name__ == "__main__":
    main()
