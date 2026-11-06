import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlparse, urlsplit, urlunsplit

import feedparser
import requests
import streamlit as st
from bs4 import BeautifulSoup

PROJECT_DIR = Path(__file__).resolve().parent
DRAFTS_DIR = PROJECT_DIR / "KICKVAULT_Drafts"
IMAGES_DIR = DRAFTS_DIR / "images"
STATE_FILE = DRAFTS_DIR / "review_queue.json"
DEFAULT_FEEDS = [
    "https://sneakernews.com/feed/",
    "https://www.solecollector.com/feed/",
    "https://www.nicekicks.com/feed/",
]
PLATFORMS = ["Instagram", "Instagram Reels", "TikTok", "Facebook", "X"]
PLATFORM_LIMITS = {"Instagram": 2200, "Instagram Reels": 2200, "TikTok": 2200, "Facebook": 5000, "X": 280}
USER_AGENT = "KICKVAULT/1.0 (+https://example.com; sneaker bot)"
IMAGE_LIMIT = 10 * 1024 * 1024
SOURCE_GUIDE = """
Preferred sources: Sneaker News, Sole Collector, and Nice Kicks. If a feed is unavailable or malformed,
we keep the app running and show the feed error instead of fabricating data.
"""

MUSIC_LIBRARY_LINKS = [
    {
        "title": "TikTok Trending Sounds",
        "platform": "TikTok",
        "mood": "Energy / hype",
        "content_type": "Sneaker hype",
        "check_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "commercial_use": "Verify inside TikTok's Commercial Music Library",
        "link": "https://www.tiktok.com/music",
        "notes": "Live trend data changes fast; select the final sound inside TikTok for exact licensing and account availability.",
    },
    {
        "title": "Instagram Reels Music",
        "platform": "Instagram",
        "mood": "Chill / luxury",
        "content_type": "Unboxing",
        "check_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "commercial_use": "Check Instagram's available music options",
        "link": "https://music.instagram.com/",
        "notes": "Availability varies by country and account type; use official music options before publishing.",
    },
    {
        "title": "TikTok Commercial Music Library",
        "platform": "TikTok",
        "mood": "Clean / upbeat",
        "content_type": "Product showcase",
        "check_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "commercial_use": "Verified commercial-use library",
        "link": "https://www.tiktok.com/music-library",
        "notes": "This is the safest place to confirm a commercial-use sound before publishing a branded sneaker post.",
    },
]

st.set_page_config(page_title="KICKVAULT", page_icon="👟", layout="wide")


def canonical_link(link: str) -> str:
    candidate = (link or "").strip()
    if not candidate:
        return ""
    parsed = urlsplit(candidate)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    filtered = []
    for key, value in query:
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in {"fbclid", "gclid", "mc_cid", "mc_eid"}:
            continue
        filtered.append((key, value))
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "&".join(f"{k}={v}" for k, v in filtered), ""))


def item_key_from_link(link: str) -> str:
    normalized = canonical_link(link)
    if not normalized:
        return hashlib.sha256(b"unknown").hexdigest()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def clean_text(value: str) -> str:
    if not value:
        return ""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def host_name(url: str) -> str:
    try:
        hostname = urlparse(url).hostname or "Original source"
    except Exception:
        return "Original source"
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname.split(".")[0].replace("-", " ").title()


def extract_release_date(text: str) -> str:
    patterns = [
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?\b",
        r"\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?\b",
        r"\b(?:Spring|Summer|Fall|Autumn|Holiday)\s+\d{4}\b",
        r"\b\d{1,2}/\d{1,2}/\d{4}\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return ""


def extract_price(text: str) -> str:
    match = re.search(r"(?i)(?:US\$|USD\s*|[$€£])\s?\d{2,4}(?:\.\d{2})?(?:\s*(?:USD|AUD|CAD|EUR|GBP|JPY|KRW|MXN|CNY|SGD))?", text)
    if not match:
        return ""
    return match.group(0).replace(" ", "")


def load_state() -> dict[str, Any]:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        default_state = {"items": {}}
        save_state(default_state)
        return default_state
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        default_state = {"items": {}}
        save_state(default_state)
        return default_state
    if not isinstance(data, dict) or not isinstance(data.get("items", {}), dict):
        raise ValueError(f"{STATE_FILE} is not a valid KICKVAULT state file.")
    return data


def save_state(state: dict[str, Any]) -> None:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    temp.replace(STATE_FILE)


def configured_feeds() -> list[str]:
    configured = os.getenv("SNEAKER_FEEDS", "").strip()
    if configured:
        return [item.strip() for item in configured.split(",") if item.strip()]
    return list(DEFAULT_FEEDS)


def is_public_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname in {"localhost"} or hostname.endswith(".local"):
            return False
        return True
    except Exception:
        return False


def safe_get(url: str, timeout: int = 20) -> requests.Response:
    if not is_public_http_url(url):
        raise ValueError(f"Rejected non-public or invalid URL: {url}")
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response


def is_valid_image(url: str) -> bool:
    return bool(url) and is_public_http_url(url) and re.search(r"\.(?:jpg|jpeg|png|gif|webp)(?:\?|$)", url, flags=re.I) is not None


def download_image(image_url: str, article_url: str) -> tuple[str, str]:
    if not is_valid_image(image_url):
        return "", "No image found."
    try:
        response = safe_get(image_url, timeout=20)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/"):
            return "", "Image source was not a valid image file."
        data = response.content[: IMAGE_LIMIT + 1]
        if len(data) > IMAGE_LIMIT:
            return "", "Image download failed: file is larger than 10 MB."
        ext = ".jpg"
        if content_type == "image/png":
            ext = ".png"
        elif content_type == "image/gif":
            ext = ".gif"
        elif content_type == "image/webp":
            ext = ".webp"
        file_name = item_key_from_link(article_url) + ext
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        destination = IMAGES_DIR / file_name
        destination.write_bytes(data)
        return str(destination), f"Downloaded {content_type} image."
    except Exception as exc:  # noqa: BLE001
        return "", f"Image download failed: {exc}"


def build_caption(story: dict[str, Any], platform: str) -> str:
    title = clean_text(str(story.get("title", "")))
    summary = clean_text(str(story.get("summary", "")))
    release_date = str(story.get("release_date", "")).strip()
    price = str(story.get("price", "")).strip()
    source_name = str(story.get("source_name", "Original source"))
    article_link = str(story.get("link", ""))
    limit = PLATFORM_LIMITS.get(platform, 2200)

    if platform == "Instagram":
        prefix = "Fresh sneaker drop alert 👟\n"
        cta = "Comment your favorite pair below."
    elif platform == "Instagram Reels":
        prefix = "Sneaker watch: quick take 🎥\n"
        cta = "Save this for your next sneaker day."
    elif platform == "TikTok":
        prefix = "Sneaker drop check 👟\n"
        cta = "Follow for more sneaker drops."
    elif platform == "Facebook":
        prefix = "Sneaker news update 👟\n"
        cta = "Drop a comment with your pick."
    else:
        prefix = "Sneaker watch 👟\n"
        cta = "What pair are you buying first?"

    lines = [prefix, title]
    if summary:
        lines.append(summary[:220])
    if release_date:
        lines.append(f"Release: {release_date}")
    if price:
        lines.append(f"Price: {price}")
    lines.append(cta)
    lines.append("#KICKVAULT #SneakerNews #SneakerDrop #Streetwear")
    if article_link:
        lines.append(f"Source: {source_name} | {article_link}")

    caption = "\n".join(lines)
    if len(caption) > limit:
        caption = caption[: max(1, limit - 20)].rstrip() + "…"
    return caption


def build_drafts(story: dict[str, Any]) -> dict[str, str]:
    return {platform: build_caption(story, platform) for platform in PLATFORMS}


def parse_feed(feed_url: str) -> list[dict[str, Any]]:
    feed = feedparser.parse(feed_url)
    entries: list[dict[str, Any]] = []
    for item in feed.entries:
        title = clean_text(item.get("title", "") or "")
        link = item.get("link") or item.get("id") or ""
        if not title or not link:
            continue
        summary = clean_text(item.get("summary", "") or item.get("description", "") or "")
        published = item.get("published") or item.get("updated") or ""
        entries.append(
            {
                "title": title,
                "link": link,
                "summary": summary[:700],
                "published": published,
                "source_name": host_name(link),
            }
        )
    return entries


def enrich_story(story: dict[str, Any]) -> dict[str, Any]:
    link = story.get("link", "")
    summary = clean_text(story.get("summary", ""))
    source_name = story.get("source_name") or host_name(link)
    detail_text = summary
    image_url = ""
    image_status = "No image found."
    article_note = ""
    try:
        response = safe_get(link, timeout=15)
        soup = BeautifulSoup(response.text, "html.parser")
        metadata = {}
        for tag in soup.find_all("meta"):
            key = tag.get("property") or tag.get("name")
            content = tag.get("content")
            if key and content:
                metadata[str(key).lower()] = content.strip()

        og_summary = metadata.get("og:description") or metadata.get("description") or ""
        if og_summary and not summary:
            summary = clean_text(og_summary)

        candidates: list[str] = []
        for key in ("og:image", "twitter:image", "twitter:image:src"):
            if metadata.get(key):
                candidates.append(str(metadata[key]))
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src:
                candidates.append(str(src))
        if candidates:
            image_url = candidates[0]

        article_text = " ".join(clean_text(node.get_text(" ", strip=True)) for node in soup.select("article, main, .entry-content, .post-content"))
        if article_text:
            detail_text = " ".join([summary, article_text])
        else:
            detail_text = summary or ""

    except Exception as exc:  # noqa: BLE001
        article_note = f"Article details could not be fetched: {exc}"

    if image_url:
        image_path, image_status = download_image(image_url, link)
        if not image_path:
            image_status = image_status
    else:
        image_path = ""

    release_date = extract_release_date(detail_text)
    price = extract_price(detail_text)

    return {
        "title": clean_text(story.get("title", "")) or "Untitled sneaker article",
        "link": link,
        "summary": summary[:700],
        "release_date": release_date,
        "price": price,
        "source_name": source_name,
        "published": story.get("published", ""),
        "image_url": image_url,
        "image_path": image_path,
        "image_status": image_status,
        "article_note": article_note,
        "status": "pending",
        "found_at": datetime.now(timezone.utc).isoformat(),
    }


def collect_updates(state: dict[str, Any]) -> tuple[dict[str, Any], int, list[str]]:
    known_items = state.get("items", {})
    new_count = 0
    messages: list[str] = []
    for feed_url in configured_feeds():
        try:
            entries = parse_feed(feed_url)
        except Exception as exc:  # noqa: BLE001
            messages.append(f"Feed error ({feed_url}): {exc}")
            continue
        for entry in entries:
            link = canonical_link(entry.get("link", ""))
            if not link:
                continue
            item_id = item_key_from_link(link)
            if item_id in known_items:
                continue
            story = enrich_story({**entry, "link": link})
            story["drafts"] = build_drafts(story)
            known_items[item_id] = story
            new_count += 1
    state["items"] = known_items
    save_state(state)
    return state, new_count, messages


def get_stats(state: dict[str, Any]) -> dict[str, int]:
    items = state.get("items", {})
    counts = {"new": 0, "pending": 0, "approved": 0, "published": 0, "rejected": 0}
    for item in items.values():
        status = str(item.get("status", "pending")).lower()
        counts["new"] += 1
        if status == "pending":
            counts["pending"] += 1
        elif status == "approved":
            counts["approved"] += 1
        elif status == "rejected":
            counts["rejected"] += 1
        elif status == "published":
            counts["published"] += 1
    return counts


def update_status(state: dict[str, Any], item_id: str, new_status: str) -> None:
    items = state.setdefault("items", {})
    if item_id in items:
        items[item_id]["status"] = new_status
        save_state(state)


def render_home(state: dict[str, Any]) -> None:
    stats = get_stats(state)
    st.title("KICKVAULT Dashboard")
    st.caption("Mobile-ready sneaker news, content planning, and review workspace.")
    kpis = st.columns(4)
    with kpis[0]:
        st.metric("New articles", stats["new"])
    with kpis[1]:
        st.metric("Awaiting review", stats["pending"])
    with kpis[2]:
        st.metric("Approved drafts", stats["approved"])
    with kpis[3]:
        st.metric("Published posts", stats["published"])
    items = list(state.get("items", {}).values())
    if not items:
        st.info("No sneaker articles have been collected yet. Use the refresh button in the sidebar to fetch the current feed.")
        return
    latest = sorted(items, key=lambda x: x.get("found_at", ""), reverse=True)[:5]
    st.subheader("Latest articles")
    for article in latest:
        with st.container(border=True):
            st.markdown(f"### {article.get('title', 'Untitled')}")
            st.write(f"{article.get('source_name', 'Original source')} • {article.get('status', 'pending').title()}")
            if article.get("summary"):
                st.write(article["summary"])
            if article.get("release_date") or article.get("price"):
                st.write(f"Release date: {article.get('release_date') or 'Unknown'} • Price: {article.get('price') or 'Not listed'}")


def render_sneaker_news(state: dict[str, Any]) -> None:
    st.title("Sneaker News")
    items = sorted(state.get("items", {}).values(), key=lambda x: x.get("found_at", ""), reverse=True)
    if not items:
        st.info("No articles available yet.")
        return
    for article in items:
        with st.container(border=True):
            st.markdown(f"#### {article.get('title', 'Untitled')}")
            st.write(f"Source: {article.get('source_name', 'Original source')} | {article.get('published') or 'Publication date unavailable'}")
            if article.get("release_date"):
                st.write(f"Release date: {article.get('release_date')}")
            if article.get("price"):
                st.write(f"Price: {article.get('price')}")
            st.write(article.get("summary") or "No summary available.")
            if article.get("image_path"):
                st.image(article["image_path"], use_container_width=True)
            else:
                st.caption("No image found")
            st.link_button("Open source article", article.get("link", "#"))


def render_drops(state: dict[str, Any]) -> None:
    st.title("New Sneaker Drops")
    items = [item for item in state.get("items", {}).values() if item.get("release_date") or item.get("price")]
    if not items:
        st.info("No release or pricing data is available yet. The app only shows verified details from the feed or article copy.")
        return
    for item in sorted(items, key=lambda x: x.get("release_date") or "", reverse=True):
        with st.container(border=True):
            st.markdown(f"### {item.get('title', 'Untitled')}")
            st.write(f"Release: {item.get('release_date') or 'Unknown'}")
            st.write(f"Price: {item.get('price') or 'Not listed'}")
            st.write(item.get("summary") or "No summary available.")


def render_images(state: dict[str, Any]) -> None:
    st.title("Sneaker Pictures")
    items = [item for item in state.get("items", {}).values() if item.get("image_path")]
    if not items:
        st.info("No usable images are available yet. Some articles may have no image or the source may block downloads.")
        return
    columns = st.columns(2)
    for index, item in enumerate(items):
        with columns[index % 2]:
            st.image(item["image_path"], use_container_width=True)
            st.caption(item.get("title", "Untitled sneaker article"))
            if item.get("image_status"):
                st.write(item["image_status"])
            st.link_button("View source", item.get("link", "#"))


def render_drafts(state: dict[str, Any]) -> None:
    st.title("Content Drafts")
    items = list(state.get("items", {}).values())
    if not items:
        st.info("No draft content yet.")
        return
    selected = st.selectbox("Review draft", options=[item.get("title", "Untitled") for item in items], index=0)
    item = next((candidate for candidate in items if candidate.get("title") == selected), items[0])
    item_id = next(key for key, value in state["items"].items() if value is item)
    if item.get("image_path"):
        st.image(item["image_path"], width=400)
    else:
        st.info("No image found")
    st.write(f"Source: {item.get('source_name', 'Original source')} — {item.get('link', '')}")
    platform = st.selectbox("Platform", PLATFORMS, key=f"platform-{item_id}")
    draft = item.get("drafts", {}).get(platform, build_caption(item, platform))
    edited_text = st.text_area("Editable caption", value=draft, height=220, key=f"caption-{item_id}-{platform}")
    if st.button("Save draft", key=f"save-{item_id}-{platform}"):
        item.setdefault("drafts", {})[platform] = edited_text
        save_state(state)
        st.success("Draft saved.")
    cols = st.columns(4)
    with cols[0]:
        if st.button("Approve", key=f"approve-{item_id}"):
            update_status(state, item_id, "approved")
            st.rerun()
    with cols[1]:
        if st.button("Reject", key=f"reject-{item_id}"):
            update_status(state, item_id, "rejected")
            st.rerun()
    with cols[2]:
        if st.button("Reopen", key=f"reopen-{item_id}"):
            update_status(state, item_id, "pending")
            st.rerun()
    with cols[3]:
        if st.button("Mark published", key=f"publish-{item_id}"):
            update_status(state, item_id, "published")
            st.rerun()
    st.write(f"Status: {item.get('status', 'pending').title()}")


def render_music(state: dict[str, Any]) -> None:
    st.title("Trending Music")
    st.info("Live trend data changes frequently and is not always available via a public API. KICKVAULT uses official platform links and explains licensing limits instead of inventing rankings or popularity.")
    tracks = MUSIC_LIBRARY_LINKS
    filter_platform = st.selectbox("Platform", ["All", "TikTok", "Instagram"])
    filter_mood = st.selectbox("Mood", ["All", "Energy / hype", "Chill / luxury", "Clean / upbeat"])
    filtered = tracks
    if filter_platform != "All":
        filtered = [track for track in filtered if track["platform"] == filter_platform]
    if filter_mood != "All":
        filtered = [track for track in filtered if track["mood"] == filter_mood]
    for track in filtered:
        with st.container(border=True):
            st.markdown(f"### {track['title']}")
            st.write(f"Platform: {track['platform']} | Mood: {track['mood']} | Last checked: {track['check_date']}")
            st.write(f"Commercial use: {track['commercial_use']}")
            st.write(f"Why it fits: {track['notes']}")
            st.link_button("Open official sound or library", track["link"])


def render_approved(state: dict[str, Any]) -> None:
    st.title("Approved Posts")
    approved = [item for item in state.get("items", {}).values() if str(item.get("status", "")).lower() == "approved"]
    if not approved:
        st.info("No approved posts yet.")
        return
    for item in approved:
        st.markdown(f"### {item.get('title', 'Untitled')}")
        st.write(item.get("summary") or "No summary available.")


def render_published(state: dict[str, Any]) -> None:
    st.title("Published Posts")
    published = [item for item in state.get("items", {}).values() if str(item.get("status", "")).lower() == "published"]
    if not published:
        st.info("Nothing has been marked as published yet. Approval does not equal publication.")
        return
    for item in published:
        st.markdown(f"### {item.get('title', 'Untitled')}")
        st.write(item.get("summary") or "No summary available.")


def render_settings(state: dict[str, Any]) -> None:
    st.title("Settings")
    st.write(SOURCE_GUIDE)
    st.subheader("News feeds")
    st.code("\n".join(configured_feeds()))
    st.subheader("Scheduling note")
    st.write("Automatic background updates depend on the hosting platform. On local or streamlit-based hosting, this app uses a manual refresh action and explains the limitation instead of pretending a cron job exists.")
    st.subheader("Optional AI enhancement")
    st.write("An external LLM can be connected later for longer caption variations, but the app works without a paid AI service and never invents product data.")
    if st.button("Refresh news now"):
        state, count, warnings = collect_updates(state)
        st.session_state["state"] = state
        if warnings:
            st.warning("\n".join(warnings))
        else:
            st.success(f"Collected {count} new article(s).")


def main() -> None:
    if "state" not in st.session_state:
        st.session_state["state"] = load_state()
    state = st.session_state["state"]
    st.sidebar.title("KICKVAULT")
    st.sidebar.caption("Sneaker News, Drops, and Content Review")
    if st.sidebar.button("Refresh feed"):
        state, count, warnings = collect_updates(state)
        st.session_state["state"] = state
        if warnings:
            st.sidebar.warning("\n".join(warnings))
        st.sidebar.success(f"Collected {count} new article(s).")
    st.sidebar.markdown("### Source list")
    for source in DEFAULT_FEEDS:
        st.sidebar.caption(source)

    tabs = st.tabs([
        "Home Dashboard",
        "Sneaker News",
        "New Sneaker Drops",
        "Sneaker Pictures",
        "Content Drafts",
        "Trending Music",
        "Approved Posts",
        "Published Posts",
        "Settings",
    ])

    with tabs[0]:
        render_home(state)
    with tabs[1]:
        render_sneaker_news(state)
    with tabs[2]:
        render_drops(state)
    with tabs[3]:
        render_images(state)
    with tabs[4]:
        render_drafts(state)
    with tabs[5]:
        render_music(state)
    with tabs[6]:
        render_approved(state)
    with tabs[7]:
        render_published(state)
    with tabs[8]:
        render_settings(state)


if __name__ == "__main__":
    main()
