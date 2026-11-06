"""Collect sneaker news and prepare locally saved, human-reviewed social drafts."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.robotparser
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from tkinter import END, StringVar, Tk, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


DEFAULT_FEEDS = ("https://sneakernews.com/feed/",)
USER_AGENT = "KICKVAULT-SneakerUpdateBot/1.0"
DRAFTS_DIR = Path(__file__).resolve().parent / "KICKVAULT_Drafts"
IMAGES_DIR = DRAFTS_DIR / "images"
STATE_FILE = DRAFTS_DIR / "review_queue.json"
IMAGE_LIMIT = 10 * 1024 * 1024
PLATFORMS = ("Instagram", "Facebook", "TikTok", "X")
PLATFORM_LIMITS = {"Instagram": 2200, "Facebook": 5000, "TikTok": 2200, "X": 280}

_opener = urllib.request.build_opener(
    urllib.request.HTTPHandler(),
    urllib.request.HTTPSHandler(),
)
_robots_cache: dict[str, tuple[urllib.robotparser.RobotFileParser | None, str]] = {}


def is_public_http_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname in ("localhost",) or hostname.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return True
        return address.is_global
    except (TypeError, ValueError):
        return False


def open_url(url: str, timeout: int = 20):
    if not is_public_http_url(url):
        raise ValueError(f"Rejected non-public or invalid URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return _opener.open(request, timeout=timeout)


def clean_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


class ArticleHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, str] = {}
        self.images: list[str] = []
        self.article_text: list[str] = []
        self.article_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        if tag.lower() in ("article", "main"):
            self.article_depth += 1

        if tag.lower() == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if key and content:
                self.metadata[key] = content

        if tag.lower() == "img":
            for key in ("src", "data-src", "data-lazy-src", "data-original"):
                if values.get(key):
                    self.images.append(values[key].strip())
            if values.get("srcset"):
                self.images.append(values["srcset"].split(",")[0].strip().split()[0])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("article", "main") and self.article_depth:
            self.article_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.article_depth and data.strip():
            self.article_text.append(data.strip())


def robots_permission(url: str) -> tuple[bool, str]:
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _robots_cache:
        robots_url = urllib.parse.urljoin(origin, "/robots.txt")
        try:
            with open_url(robots_url, timeout=8) as response:
                text = response.read(512 * 1024).decode("utf-8", errors="replace")
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(text.splitlines())
            _robots_cache[origin] = (parser, "")
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                parser = urllib.robotparser.RobotFileParser()
                parser.parse([])
                _robots_cache[origin] = (parser, "")
            elif error.code in (401, 403):
                _robots_cache[origin] = (None, f"robots.txt returned HTTP {error.code}")
            else:
                _robots_cache[origin] = (None, f"Could not check robots.txt: {error}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            _robots_cache[origin] = (None, f"Could not check robots.txt: {error}")

    parser, error_text = _robots_cache[origin]
    if parser is None:
        return False, error_text
    if not parser.can_fetch(USER_AGENT, url):
        return False, "robots.txt disallows access"
    return True, ""


def canonical_link(link: str) -> str:
    parsed = urllib.parse.urlsplit(link.strip())
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [
        (key, value)
        for key, value in query
        if not key.lower().startswith("utm_")
        and key.lower() not in ("fbclid", "gclid", "mc_cid", "mc_eid")
    ]
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, urllib.parse.urlencode(query), "")
    )


def extract_feed_item(element: ET.Element, feed_url: str) -> dict[str, str | list[str]]:
    title = ""
    link = ""
    published = ""
    description = ""
    content = ""
    image_urls: list[str] = []

    for child in element:
        name = local_name(child.tag)
        if name == "title" and child.text:
            title = clean_text(child.text)
        elif name == "link":
            link = child.attrib.get("href", child.text or "").strip()
        elif name in ("pubdate", "published", "updated") and child.text and not published:
            published = clean_text(child.text)
        elif name == "description" and child.text:
            description = child.text
        elif name == "encoded" and child.text:
            content = child.text

    for node in element.iter():
        name = local_name(node.tag)
        if name in ("content", "thumbnail", "enclosure"):
            mime = node.attrib.get("type", "").lower()
            candidate = node.attrib.get("url", "").strip()
            if candidate and (name != "enclosure" or not mime or mime.startswith("image/")):
                image_urls.append(urllib.parse.urljoin(link or feed_url, candidate))

    for source in (description, content):
        parser = ArticleHTMLParser()
        parser.feed(source)
        image_urls.extend(urllib.parse.urljoin(link or feed_url, item) for item in parser.images)

    summary = clean_text(content) or clean_text(description)
    return {
        "title": title,
        "link": link,
        "published": published,
        "summary": summary[:600],
        "feed": feed_url,
        "image_urls": image_urls,
    }


def parse_feed(feed_url: str) -> list[dict[str, str | list[str]]]:
    with open_url(feed_url) as response:
        root = ET.fromstring(response.read(5 * 1024 * 1024))

    stories: list[dict[str, str | list[str]]] = []
    for element in root.iter():
        if local_name(element.tag) not in ("item", "entry"):
            continue
        story = extract_feed_item(element, feed_url)
        if story["title"] and is_public_http_url(str(story["link"])):
            stories.append(story)
    return stories


def unique_valid_image_urls(candidates: list[str], base_url: str) -> list[str]:
    results: list[str] = []
    for candidate in candidates:
        candidate = urllib.parse.urljoin(base_url, candidate.strip())
        if is_public_http_url(candidate) and candidate not in results:
            results.append(candidate)
    return results


def enrich_story(story: dict[str, str | list[str]]) -> dict[str, str | list[str]]:
    link = str(story["link"])
    image_urls = list(story.get("image_urls", []))
    summary = str(story.get("summary", ""))
    detail_text = summary
    source_name = urllib.parse.urlparse(link).hostname or "Original source"
    source_name = source_name.removeprefix("www.").split(".")[0].replace("-", " ").title()
    page_note = ""

    allowed, reason = robots_permission(link)
    if allowed:
        try:
            with open_url(link) as response:
                page = response.read(2 * 1024 * 1024).decode(
                    response.headers.get_content_charset() or "utf-8",
                    errors="replace",
                )
            parser = ArticleHTMLParser()
            parser.feed(page)
            metadata = parser.metadata
            for key in ("og:image", "twitter:image", "twitter:image:src"):
                if metadata.get(key):
                    image_urls.insert(0, urllib.parse.urljoin(link, metadata[key]))
            page_summary = metadata.get("og:description") or metadata.get("description") or ""
            if not summary and page_summary:
                summary = clean_text(page_summary)[:600]
            if parser.article_text:
                detail_text = f"{detail_text} {' '.join(parser.article_text)}"
            elif page_summary:
                detail_text = f"{detail_text} {page_summary}"
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, ValueError) as error:
            page_note = f"Article details could not be fetched: {error}"
    else:
        page_note = f"Article page not fetched: {reason}"

    image_urls = unique_valid_image_urls(image_urls, link)
    release_date = extract_release_date(detail_text)
    price = extract_price(detail_text)

    image_path = ""
    if not image_urls:
        image_status = "No images found."
    else:
        image_path, image_status = download_image(image_urls[0], link)

    return {
        **story,
        "summary": summary[:600],
        "release_date": release_date,
        "price": price,
        "source_name": source_name,
        "image_url": image_urls[0] if image_urls else "",
        "image_path": image_path,
        "image_status": image_status,
        "article_note": page_note,
    }


def extract_release_date(text: str) -> str:
    patterns = (
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s+\d{4})?\b",
        r"\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?"
        r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s+\d{4})?\b",
        r"\b(?:Spring|Summer|Fall|Autumn|Holiday)\s+\d{4}\b",
        r"\b\d{1,2}/\d{1,2}/\d{4}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return ""


def extract_price(text: str) -> str:
    pattern = (
        r"(?<![\w])(?:US\$|USD\s*|[$€£])\s?\d{2,4}(?:\.\d{2})?"
        r"(?!\d)"
    )
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return re.sub(r"\s+", "", match.group(0)) if match else ""


def image_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def download_image(image_url: str, article_url: str) -> tuple[str, str]:
    if not is_public_http_url(image_url):
        return "", "Image download failed: invalid or non-public image URL."

    allowed, reason = robots_permission(image_url)
    if not allowed:
        return "", f"Image found but not downloaded: {reason}."

    try:
        with open_url(image_url, timeout=20) as response:
            declared_type = response.headers.get_content_type().lower()
            data = response.read(IMAGE_LIMIT + 1)
        if len(data) > IMAGE_LIMIT:
            return "", "Image download failed: file is larger than 10 MB."

        detected = image_type(data)
        if detected is None:
            return "", "Image download failed: response is not a supported image file."
        actual_type, extension = detected
        if declared_type not in (actual_type, "application/octet-stream"):
            return "", f"Image download failed: file type mismatch ({declared_type})."

        file_name = hashlib.sha256(article_url.encode("utf-8")).hexdigest() + extension
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        destination = IMAGES_DIR / file_name
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(destination)
        return str(destination), f"Downloaded {actual_type} image."
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return "", f"Image download failed: {error}"


def build_caption(story: dict, platform: str) -> str:
    title = clean_text(str(story.get("title", "")))
    summary = clean_text(str(story.get("summary", "")))
    release_date = str(story.get("release_date", "")).strip()
    price = str(story.get("price", "")).strip()
    source_name = str(story.get("source_name", "Original source"))
    link = str(story.get("link", ""))
    limit = PLATFORM_LIMITS[platform]

    fixed_lines = []
    if release_date:
        fixed_lines.append(f"Release: {release_date}")
    if price:
        fixed_lines.append(f"Price: {price}")
    fixed_lines.append("#KICKVAULT #SneakerNews")
    fixed_lines.append(f"Source: {source_name} | {link}")

    def compose(headline: str, excerpt: str) -> str:
        lines = [headline, *fixed_lines]
        if excerpt:
            lines.insert(1, excerpt)
        return "\n".join(lines)

    caption = compose(title, summary)
    if len(caption) > limit and summary:
        summary = summary[: max(0, len(summary) - (len(caption) - limit) - 1)].rstrip()
        caption = compose(title, summary)
    if len(caption) > limit:
        title = title[: max(1, len(title) - (len(caption) - limit) - 1)].rstrip()
        caption = compose(title, summary)
    if len(caption) > limit:
        return caption[:limit]
    return caption


def build_drafts(story: dict) -> dict[str, str]:
    return {platform: build_caption(story, platform) for platform in PLATFORMS}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"items": {}}
    with STATE_FILE.open("r", encoding="utf-8") as state_file:
        data = json.load(state_file)
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        raise ValueError(f"{STATE_FILE} has an invalid format")
    return data


def save_state(state: dict) -> None:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2, ensure_ascii=False)
    temporary.replace(STATE_FILE)


def configured_feeds() -> list[str]:
    configured = os.getenv("SNEAKER_FEEDS", "").strip()
    feeds = configured.split(",") if configured else list(DEFAULT_FEEDS)
    return [feed.strip() for feed in feeds if feed.strip()]


def collect_updates(state: dict) -> tuple[dict, int, list[str]]:
    known_items = state["items"]
    messages: list[str] = []
    seen_this_run: set[str] = set()
    new_count = 0

    for feed_url in configured_feeds():
        try:
            stories = parse_feed(feed_url)
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ET.ParseError,
            ValueError,
        ) as error:
            messages.append(f"Feed error ({feed_url}): {error}")
            continue

        for story in stories:
            link = canonical_link(str(story["link"]))
            key = hashlib.sha256(link.encode("utf-8")).hexdigest()
            if key in known_items or key in seen_this_run:
                continue
            seen_this_run.add(key)

            item = enrich_story(story)
            item["status"] = "pending"
            item["found_at"] = datetime.now(timezone.utc).isoformat()
            item["drafts"] = build_drafts(item)
            known_items[key] = item
            new_count += 1

    state["items"] = known_items
    save_state(state)
    return state, new_count, messages


class ReviewApp:
    def __init__(self, root: Tk, state: dict) -> None:
        self.root = root
        self.state = state
        self.selected_key = ""
        self.refreshing = False

        root.title("KICKVAULT Sneaker News Review")
        root.geometry("1020x720")
        root.minsize(800, 560)

        toolbar = ttk.Frame(root, padding=10)
        toolbar.pack(fill="x")
        self.refresh_button = ttk.Button(
            toolbar, text="Collect sneaker news", command=self.refresh
        )
        self.refresh_button.pack(side="left")
        ttk.Button(toolbar, text="Open drafts folder", command=self.open_drafts).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            toolbar,
            text="Nothing is published automatically. Review and post approved drafts yourself.",
        ).pack(side="left", padx=12)

        body = ttk.Panedwindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(left, weight=1)
        body.add(right, weight=3)

        ttk.Label(left, text="Saved articles").pack(anchor="w", pady=(0, 6))
        self.filter_var = StringVar(value="All")
        filter_row = ttk.Frame(left)
        filter_row.pack(fill="x", pady=(0, 6))
        ttk.Label(filter_row, text="Show:").pack(side="left")
        filter_box = ttk.Combobox(
            filter_row,
            textvariable=self.filter_var,
            values=("All", "Pending", "Approved", "Rejected"),
            state="readonly",
            width=12,
        )
        filter_box.pack(side="left", padx=5)
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self.populate_list())
        self.article_list = ttk.Treeview(
            left, columns=("status",), show="tree headings", selectmode="browse"
        )
        self.article_list.heading("#0", text="Headline")
        self.article_list.heading("status", text="Status")
        self.article_list.column("#0", width=250, stretch=True)
        self.article_list.column("status", width=75, stretch=False)
        self.article_list.pack(fill="both", expand=True)
        self.article_list.bind("<<TreeviewSelect>>", self.select_article)

        self.title_var = StringVar(value="Select an article to review.")
        ttk.Label(
            right, textvariable=self.title_var, font=("Segoe UI", 12, "bold"),
            wraplength=680,
        ).pack(anchor="w", pady=(0, 8))
        self.details = ScrolledText(right, height=8, wrap="word", state="disabled")
        self.details.pack(fill="x", pady=(0, 8))

        caption_toolbar = ttk.Frame(right)
        caption_toolbar.pack(fill="x")
        ttk.Label(caption_toolbar, text="Caption for:").pack(side="left")
        self.platform_var = StringVar(value="Instagram")
        platform_box = ttk.Combobox(
            caption_toolbar,
            textvariable=self.platform_var,
            values=PLATFORMS,
            state="readonly",
            width=14,
        )
        platform_box.pack(side="left", padx=6)
        platform_box.bind("<<ComboboxSelected>>", lambda _event: self.load_caption())
        self.caption = ScrolledText(right, height=8, wrap="word")
        self.caption.pack(fill="both", expand=True, pady=(6, 8))

        actions = ttk.Frame(right)
        actions.pack(fill="x")
        ttk.Button(actions, text="Save caption edits", command=self.save_caption).pack(
            side="left"
        )
        ttk.Button(actions, text="Open source article", command=self.open_source).pack(
            side="left", padx=6
        )
        ttk.Button(actions, text="Open image", command=self.open_image).pack(side="left")
        ttk.Button(actions, text="Approve", command=lambda: self.set_status("approved")).pack(
            side="right"
        )
        ttk.Button(actions, text="Reject", command=lambda: self.set_status("rejected")).pack(
            side="right", padx=6
        )
        self.status_var = StringVar(value=f"Drafts are saved in: {DRAFTS_DIR}")
        ttk.Label(root, textvariable=self.status_var, anchor="w", padding=(10, 5)).pack(
            fill="x"
        )

        self.populate_list()

    def populate_list(self) -> None:
        current = self.selected_key
        for row in self.article_list.get_children():
            self.article_list.delete(row)
        wanted = self.filter_var.get().lower()
        items = self.state["items"]
        for key, item in sorted(
            items.items(),
            key=lambda pair: pair[1].get("found_at", ""),
            reverse=True,
        ):
            status = item.get("status", "pending")
            if wanted != "all" and status != wanted:
                continue
            self.article_list.insert(
                "", END, iid=key, text=str(item.get("title", "Untitled")),
                values=(status.title(),),
            )
        if current in self.article_list.get_children():
            self.article_list.selection_set(current)
            self.article_list.see(current)

    def selected_item(self) -> dict | None:
        return self.state["items"].get(self.selected_key)

    def select_article(self, _event=None) -> None:
        selection = self.article_list.selection()
        self.selected_key = selection[0] if selection else ""
        item = self.selected_item()
        if item is None:
            self.title_var.set("Select an article to review.")
            self.set_details("")
            self.caption.delete("1.0", END)
            return

        self.title_var.set(str(item.get("title", "Untitled")))
        details = [
            f"Status: {item.get('status', 'pending').title()}",
            f"Published: {item.get('published') or 'Not provided'}",
            f"Release date: {item.get('release_date') or 'Not provided'}",
            f"Price: {item.get('price') or 'Not provided'}",
            f"Summary: {item.get('summary') or 'No summary provided.'}",
            f"Source: {item.get('source_name', 'Original source')} — {item.get('link', '')}",
            f"Image: {item.get('image_status', 'No images found.')}",
        ]
        if item.get("image_path"):
            details.append(f"Saved image: {item['image_path']}")
        if item.get("article_note"):
            details.append(str(item["article_note"]))
        self.set_details("\n\n".join(details))
        self.load_caption()

    def set_details(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", END)
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def load_caption(self) -> None:
        item = self.selected_item()
        self.caption.delete("1.0", END)
        if item:
            self.caption.insert(
                "1.0",
                item.get("drafts", {}).get(self.platform_var.get(), ""),
            )

    def save_caption(self) -> None:
        item = self.selected_item()
        if not item:
            messagebox.showinfo("Select an article", "Choose an article first.")
            return
        item.setdefault("drafts", {})[self.platform_var.get()] = self.caption.get(
            "1.0", "end-1c"
        ).strip()
        save_state(self.state)
        self.status_var.set("Caption saved.")

    def set_status(self, status: str) -> None:
        item = self.selected_item()
        if not item:
            messagebox.showinfo("Select an article", "Choose an article first.")
            return
        item["status"] = status
        save_state(self.state)
        self.populate_list()
        self.select_article()
        self.status_var.set(f"Article marked {status}. Nothing was published.")

    def open_source(self) -> None:
        item = self.selected_item()
        if item:
            import webbrowser

            webbrowser.open(str(item["link"]))

    def open_image(self) -> None:
        item = self.selected_item()
        image_path = str(item.get("image_path", "")) if item else ""
        if image_path and Path(image_path).is_file():
            import webbrowser

            webbrowser.open(Path(image_path).resolve().as_uri())
        else:
            messagebox.showinfo("No saved image", "There is no downloaded image for this article.")

    def open_drafts(self) -> None:
        import webbrowser

        webbrowser.open(DRAFTS_DIR.resolve().as_uri())

    def refresh(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.refresh_button.configure(state="disabled")
        self.status_var.set("Collecting updates and preparing drafts...")
        threading.Thread(target=self.refresh_worker, daemon=True).start()

    def refresh_worker(self) -> None:
        try:
            state, count, messages = collect_updates(self.state)
            self.root.after(0, self.refresh_complete, state, count, messages, "")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self.root.after(0, self.refresh_complete, self.state, 0, [], str(error))

    def refresh_complete(
        self, state: dict, count: int, messages: list[str], fatal_error: str
    ) -> None:
        self.refreshing = False
        self.refresh_button.configure(state="normal")
        self.state = state
        self.populate_list()
        if fatal_error:
            self.status_var.set(f"Could not save or load the queue: {fatal_error}")
            messagebox.showerror("KICKVAULT update error", fatal_error)
        elif messages:
            self.status_var.set(
                f"Collected {count} new article(s); {len(messages)} feed error(s)."
            )
            messagebox.showwarning("Some feeds could not be checked", "\n".join(messages))
        else:
            self.status_var.set(f"Collected {count} new article(s). Review the queue.")


def main() -> int:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        state = load_state()
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Could not load the KICKVAULT review queue: {error}", file=sys.stderr)
        return 1

    root = Tk()
    ReviewApp(root, state)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
