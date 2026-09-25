# -*- coding: utf-8 -*-
"""通用软件站列表页爬虫。

通过 CSS 选择器配置即可适配任意列表页（如 x6d、423Down 等），
每个源在 config.json 中声明 item/title/date 选择器，无需改代码。

内置反爬应对：
  - 完整浏览器请求头（Referer 按站点自动生成）
  - 对 429/502/503/504 及反爬验证页做指数退避重试（最多 3 次）
"""
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _headers_for(url: str) -> dict:
    """为指定页面生成带同源 Referer 的完整浏览器请求头。"""
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    return {**_BASE_HEADERS, "Referer": origin + "/"}


def _backoff_seconds(attempt: int) -> float:
    return attempt * 5.0


def fetch_html(url: str, timeout: int = 30, max_retries: int = 3) -> str:
    """拉取页面 HTML，自动处理编码；对限流/网关错误做指数退避重试。

    抛出的异常会说明是被反爬拦截还是网络错误。
    """
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=_headers_for(url), timeout=timeout)
            if resp.status_code in _RETRY_STATUS:
                last_exc = RuntimeError(f"HTTP {resp.status_code}")
                print(f"  [retry] {url} -> {resp.status_code}，"
                      f"{_backoff_seconds(attempt):.0f}s 后重试 ({attempt}/{max_retries})")
                time.sleep(_backoff_seconds(attempt))
                continue
            resp.raise_for_status()

            # 反爬验证页识别（如跳转到 /GE/CC/VALIDATOR 的 JS 验证页）
            if "VALIDATOR" in resp.url or "VALIDATOR" in (resp.text or "")[:2000]:
                last_exc = RuntimeError("被站点反爬验证页拦截（VALIDATOR）")
                print(f"  [retry] {url} 触发反爬验证，"
                      f"{_backoff_seconds(attempt):.0f}s 后重试 ({attempt}/{max_retries})")
                time.sleep(_backoff_seconds(attempt))
                continue

            encoding = resp.apparent_encoding or resp.encoding
            resp.encoding = encoding
            return resp.text
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            print(f"  [retry] {url} 请求异常: {exc}，"
                  f"{_backoff_seconds(attempt):.0f}s 后重试 ({attempt}/{max_retries})"
                  if attempt < max_retries else f"  [error] {url} 请求异常: {exc}")
            if attempt < max_retries:
                time.sleep(_backoff_seconds(attempt))
    raise last_exc


def parse_source(source: dict) -> list[dict]:
    """抓取并解析单个源，返回条目列表。

    每个条目: {"key": 去重键, "title": 标题, "url": 详情页地址, "date": 发布日期}
    """
    list_url = (source.get("list_url") or "").strip()
    if not list_url:
        raise ValueError(f"源 {source.get('name', '?')} 缺少 list_url")

    html = fetch_html(list_url)
    soup = BeautifulSoup(html, "html.parser")

    item_selector = source.get("item_selector", "li").strip()
    title_selector = source.get("title_selector", "a").strip()
    date_selector = (source.get("date_selector") or "").strip()
    date_prefix = source.get("date_prefix", "")
    max_items = int(source.get("max_items", 30))

    items = []
    for el in soup.select(item_selector)[:max_items]:
        title_a = el.select_one(title_selector)
        if title_a is None:
            continue
        title = title_a.get_text(strip=True)
        href = (title_a.get("href") or "").strip()
        if not title:
            continue

        link = urljoin(list_url, href)
        date = _extract_date(el, date_selector, date_prefix)

        # 去重键：优先详情页地址，无地址时用 标题|日期
        key = link if href else f"{title}|{date}"
        items.append({"key": key, "title": title, "url": link, "date": date})

    if not items:
        print(f"  [warn] 源 {source.get('name', list_url)} 未解析到任何条目，"
              f"请检查 item_selector={item_selector!r} / title_selector={title_selector!r}")
    return items


def _extract_date(item_el, date_selector: str, date_prefix: str) -> str:
    if not date_selector:
        return ""
    el = item_el.select_one(date_selector)
    if el is None:
        return ""
    text = el.get_text(strip=True)
    text = text.replace(date_prefix, "").strip()
    # 只保留类似 2026-09-25 / 2026/09/25 的日期部分
    m = re.search(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}", text)
    return m.group(0) if m else text


def clean_text(raw: str) -> str:
    """去掉 HTML 标签与多余空白，用于生成纯文本摘要。"""
    return _HTML_TAG_RE.sub("", raw or "").strip()


def match_keywords(title: str, keywords: list) -> bool:
    """判断标题是否命中任一关注关键词（大小写不敏感）。

    keywords 为空时视为关注全部，返回 True。
    """
    keys = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not keys:
        return True
    t = title.lower()
    return any(k.lower() in t for k in keys)
