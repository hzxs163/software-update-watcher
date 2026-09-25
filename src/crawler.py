# -*- coding: utf-8 -*-
"""通用软件站列表页爬虫。

通过 CSS 选择器配置即可适配任意列表页（如 x6d、423Down 等），
每个源在 config.json 中声明 item/title/date 选择器，无需改代码。
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def fetch_html(url: str, timeout: int = 30) -> str:
    """拉取页面 HTML，自动处理编码。"""
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    # 优先按响应头编码，乱码时回退到从内容嗅探
    encoding = resp.apparent_encoding or resp.encoding
    resp.encoding = encoding
    return resp.text


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
