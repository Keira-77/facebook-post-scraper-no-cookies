thonimport json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from utils.logger import get_logger
from utils.time_formatter import parse_datetime_to_unix_ms

@dataclass
class FacebookPost:
    facebookUrl: str
    pageId: Optional[str]
    postId: Optional[str]
    pageName: Optional[str]
    url: Optional[str]
    time: Optional[str]
    timestamp: Optional[int]
    likes: Optional[int]
    comments: Optional[int]
    shares: Optional[int]
    text: Optional[str]
    link: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "facebookUrl": self.facebookUrl,
            "pageId": self.pageId,
            "postId": self.postId,
            "pageName": self.pageName,
            "url": self.url,
            "time": self.time,
            "timestamp": self.timestamp,
            "likes": self.likes,
            "comments": self.comments,
            "shares": self.shares,
            "text": self.text,
            "link": self.link,
        }

class FacebookPostScraper:
    """
    Scrapes public Facebook page posts without cookies or login.

    This implementation works on the standard web HTML response and makes
    conservative assumptions about the structure. The selectors may need to
    be adjusted over time as Facebook updates its frontend.
    """

    def __init__(
        self,
        user_agent: Optional[str] = None,
        request_timeout: int = 15,
        max_retries: int = 3,
        sleep_between_requests: float = 1.0,
    ) -> None:
        self.logger = get_logger(self.__class__.__name__)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent
                or (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                )
            }
        )
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.sleep_between_requests = sleep_between_requests

    def scrape_page(self, page_url: str, max_posts: int = 50) -> List[Dict[str, Any]]:
        """
        Scrape posts from a public Facebook page URL.

        :param page_url: Public Facebook page URL (e.g. https://www.facebook.com/nytimes/)
        :param max_posts: Max number of posts to attempt to scrape.
        :return: List of post dictionaries.
        """
        html = self._fetch_page_html(page_url)
        if not html:
            self.logger.warning("No HTML content returned for %s", page_url)
            return []

        soup = BeautifulSoup(html, "html.parser")

        page_name = self._extract_page_name(soup)
        page_id = self._extract_page_id(soup)

        self.logger.debug("Resolved page name=%r page_id=%r", page_name, page_id)

        posts: List[FacebookPost] = []
        for post_el in self._iter_post_elements(soup):
            if len(posts) >= max_posts:
                break

            try:
                post = self._parse_single_post(
                    post_el, page_url, page_name, page_id
                )
                if post:
                    posts.append(post)
            except Exception as exc:
                self.logger.warning(
                    "Failed to parse post element: %s", exc, exc_info=True
                )

        return [p.to_dict() for p in posts]

    # =============================
    # HTTP and HTML helpers
    # =============================

    def _fetch_page_html(self, url: str) -> Optional[str]:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                self.logger.debug("Fetching %s (attempt %d)", url, attempt)
                resp = self.session.get(url, timeout=self.request_timeout)
                if resp.status_code != 200:
                    self.logger.warning(
                        "Non-200 response (%d) for %s", resp.status_code, url
                    )
                    last_exc = RuntimeError(
                        f"HTTP {resp.status_code} for {url}"
                    )
                else:
                    return resp.text
            except Exception as exc:
                last_exc = exc
                self.logger.warning(
                    "Request error for %s (attempt %d): %s",
                    url,
                    attempt,
                    exc,
                    exc_info=True,
                )

            time.sleep(self.sleep_between_requests)

        if last_exc:
            self.logger.error(
                "Exhausted retries fetching %s: %s", url, last_exc
            )
        return None

    def _extract_page_name(self, soup: BeautifulSoup) -> Optional[str]:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return og_title["content"].strip()

        if soup.title and soup.title.string:
            return soup.title.string.strip()

        return None

    def _extract_page_id(self, soup: BeautifulSoup) -> Optional[str]:
        """
        Attempts to extract a numeric page ID from known meta tags or embedded JSON.
        """

        # Approach 1: Android deep link meta tag
        android_meta = soup.find("meta", property="al:android:url")
        if android_meta and android_meta.get("content"):
            content = android_meta["content"]
            # Example: fb://page/5281959998
            parts = content.split("/")
            for part in reversed(parts):
                if part.isdigit():
                    return part

        # Approach 2: Look through script tags for a numeric page ID field
        for script in soup.find_all("script"):
            if not script.string:
                continue
            text = script.string
            # Heuristic: look for "pageID":"12345"
            marker = '"pageID":"'
            idx = text.find(marker)
            if idx != -1:
                start = idx + len(marker)
                end = text.find('"', start)
                if end != -1:
                    candidate = text[start:end]
                    if candidate.isdigit():
                        return candidate

        return None

    def _iter_post_elements(self, soup: BeautifulSoup) -> List[Tag]:
        """
        Return a list of container elements that look like posts.

        Facebook's DOM is complex and subject to change, so we use a couple
        of defensive heuristics instead of hard-coding a single selector.
        """
        candidates: List[Tag] = []

        # Heuristic 1: desktop feed units
        feed_units = soup.select('div[data-pagelet^="FeedUnit_"]')
        candidates.extend(feed_units)

        # Heuristic 2: role="article" blocks
        article_units = soup.find_all("div", attrs={"role": "article"})
        for el in article_units:
            if el not in candidates:
                candidates.append(el)

        # Fallback: if nothing matched, just try any big <div> with a data-ft attribute
        if not candidates:
            self.logger.debug(
                "No standard post containers found, using fallback data-ft selector."
            )
            fallback = soup.find_all("div", attrs={"data-ft": True})
            candidates.extend(fallback)

        self.logger.debug("Identified %d potential post containers.", len(candidates))
        return candidates

    # =============================
    # Single post parsing
    # =============================

    def _parse_single_post(
        self,
        el: Tag,
        page_url: str,
        page_name: Optional[str],
        page_id: Optional[str],
    ) -> Optional[FacebookPost]:
        post_url, post_id = self._extract_post_url_and_id(el, page_url)
        text = self._extract_post_text(el)
        link = self._extract_external_link(el)
        likes, comments, shares = self._extract_engagement_counts(el)
        time_str, timestamp_ms = self._extract_time_metadata(el)

        if not post_url and not text:
            # This isn't a real post we can make sense of
            return None

        post = FacebookPost(
            facebookUrl=page_url,
            pageId=page_id,
            postId=post_id,
            pageName=page_name,
            url=post_url,
            time=time_str,
            timestamp=timestamp_ms,
            likes=likes,
            comments=comments,
            shares=shares,
            text=text,
            link=link,
        )

        return post

    def _extract_post_url_and_id(
        self, el: Tag, page_url: str
    ) -> (Optional[str], Optional[str]):
        """
        Attempts to find a link to the post itself and extract a post ID from it.
        """
        candidates: List[str] = []

        # Links that typically represent posts
        for a in el.find_all("a", href=True):
            href = a["href"]
            if any(
                marker in href
                for marker in ("/posts/", "/photos/", "/videos/", "story_fbid=")
            ):
                candidates.append(href)

        if not candidates:
            return None, None

        href = candidates[0]
        full_url = urljoin("https://www.facebook.com", href)
        post_id = self._parse_post_id_from_url(full_url)

        # Normalize to remove tracking query params where possible
        parsed = urlparse(full_url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query and "story_fbid" in parsed.query:
            clean_url = full_url  # keep full query if needed to identify the post

        return clean_url, post_id

    def _parse_post_id_from_url(self, url: str) -> Optional[str]:
        """
        Extract a post ID from typical Facebook post URL structures.
        """
        # Example: https://www.facebook.com/nytimes/posts/10153102374144999
        path_parts = urlparse(url).path.strip("/").split("/")
        for part in reversed(path_parts):
            if part.isdigit():
                return part

        # Example: ?story_fbid=10153102374144999&id=5281959998
        parsed = urlparse(url)
        if parsed.query:
            from urllib.parse import parse_qs

            q = parse_qs(parsed.query)
            for key in ("story_fbid", "fbid"):
                if key in q and q[key]:
                    v = q[key][0]
                    if v.isdigit():
                        return v

        return None

    def _extract_post_text(self, el: Tag) -> Optional[str]:
        """
        Extract the main text of the post.

        We look for text containers that are typically used for post bodies,
        and then normalize whitespace.
        """
        pieces: List[str] = []

        # Approach 1: divs with dir="auto" often contain message text
        for div in el.find_all("div", attrs={"dir": "auto"}):
            text = div.get_text(" ", strip=True)
            if not text:
                continue
            pieces.append(text)

        # Approach 2: paragraph tags
        if not pieces:
            for p in el.find_all("p"):
                text = p.get_text(" ", strip=True)
                if not text:
                    continue
                pieces.append(text)

        if not pieces:
            return None

        # Remove duplicates while preserving order
        seen = set()
        unique_pieces = []
        for t in pieces:
            if t in seen:
                continue
            seen.add(t)
            unique_pieces.append(t)

        result = " ".join(unique_pieces).strip()
        return result or None

    def _extract_external_link(self, el: Tag) -> Optional[str]:
        """
        Look for external links shared in the post body.
        """
        for a in el.find_all("a", href=True):
            href = a["href"]
            if "facebook.com" not in href:
                if href.startswith("/"):
                    href = urljoin("https://www.facebook.com", href)
                return href
        return None

    def _extract_engagement_counts(
        self, el: Tag
    ) -> (Optional[int], Optional[int], Optional[int]):
        """
        Extract likes, comments, and shares counts.

        The exact markup is volatile; we use heuristics to parse numbers
        that are near words like "like", "comment", "share".
        """
        text = el.get_text(" ", strip=True)
        if not text:
            return None, None, None

        likes = self._find_number_near_keyword(text, ["like", "likes", "reaction"])
        comments = self._find_number_near_keyword(
            text, ["comment", "comments", "reply", "replies"]
        )
        shares = self._find_number_near_keyword(text, ["share", "shares"])

        return likes, comments, shares

    def _find_number_near_keyword(
        self, text: str, keywords: List[str]
    ) -> Optional[int]:
        """
        Search for patterns like '123 likes' or 'likes 123' in the text.
        """
        import re

        for kw in keywords:
            # 123 likes
            pattern1 = rf"(\d[\d,\.]*)\s+{kw}"
            # likes 123
            pattern2 = rf"{kw}\s+(\d[\d,\.]*)"

            for pattern in (pattern1, pattern2):
                m = re.search(pattern, text, flags=re.IGNORECASE)
                if m:
                    raw = m.group(1).replace(",", "")
                    try:
                        return int(float(raw))
                    except ValueError:
                        continue

        return None

    def _extract_time_metadata(
        self, el: Tag
    ) -> (Optional[str], Optional[int]):
        """
        Find the human-readable time string and associated Unix timestamp (ms).
        """
        # Many time elements appear as <abbr> or <a> with data-utime
        time_str: Optional[str] = None
        timestamp_ms: Optional[int] = None

        for candidate in el.find_all(["abbr", "a", "span"]):
            utime = candidate.get("data-utime")
            if utime:
                try:
                    ts_seconds = int(utime)
                    timestamp_ms = ts_seconds * 1000
                except ValueError:
                    pass

                title = candidate.get("title")
                if title:
                    time_str = title.strip()
                else:
                    text = candidate.get_text(" ", strip=True)
                    if text:
                        time_str = text
                break

        # Fallback: attempt to parse any text that looks like a date
        if not timestamp_ms and time_str:
            timestamp_ms = parse_datetime_to_unix_ms(time_str)

        return time_str, timestamp_ms