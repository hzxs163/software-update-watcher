# -*- coding: utf-8 -*-
"""通用软件站列表页爬虫（多通道）。

通过 CSS 选择器配置即可适配任意列表页（如 x6d、423Down 等），
每个源在 config.json 中声明 item/title/date 选择器，无需改代码。

多通道抓取（应对站点对数据中心 IP 的封锁，如 GitHub Actions 访问 x6d 被 502）：
  1. 直连：完整浏览器请求头 + 指数退避重试 + 反爬验证页识别
  2. 公共 CORS 代理：allorigins / codetabs / cors.eu.org / thingproxy
  3. Jina Reader（r.jina.ai）：返回 markdown，按链接条目解析
"""
import json
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse, quote

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
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_RETRY_STATUS = {429, 500, 502, 503, 504}

# 免 key 公共 CORS 代理（构造完整请求 URL）
_PROXY_BUILDERS = [
    lambda u: "https://api.allorigins.win/raw?url=" + quote(u, safe=""),
    lambda u: "https://api.codetabs.com/v1/proxy?quest=" + quote(u, safe=""),
    lambda u: "https://cors.eu.org/" + u,
    lambda u: "https://thingproxy.freeboard.io/fetch/" + u,
]


def _headers_for(url: str) -> dict:
    """为指定页面生成带同源 Referer 的完整浏览器请求头。"""
    parsed = urlparse(url)
    return {**_BASE_HEADERS, "Referer": f"{parsed.scheme}://{parsed.netloc}/"}


def _backoff_seconds(attempt: int) -> float:
    return attempt * 5.0


def _is_validator_page(url: str, text: str) -> bool:
    """反爬验证页识别（如跳转到 /GE/CC/VALIDATOR 的 JS 验证页）。"""
    return "VALIDATOR" in url or "VALIDATOR" in (text or "")[:2000]


def fetch_html(url: str, timeout: int = 30) -> str:
    """多通道拉取页面文本（HTML 或 markdown），任一通道成功即返回。"""
    errors = []

    # 通道1：直连（带浏览器头 + 重试）
    try:
        text = _fetch_direct(url, timeout)
        print("  [fetch] 通道: 直连成功")
        return text
    except Exception as exc:  # noqa: BLE001
        errors.append(f"直连: {exc}")

    # 通道2：公共 CORS 代理
    for builder in _PROXY_BUILDERS:
        proxy_url = builder(url)
        try:
            resp = requests.get(proxy_url, headers=_BASE_HEADERS, timeout=timeout)
            if resp.ok and len(resp.text) > 500:
                print(f"  [fetch] 通道: 代理 {proxy_url.split('/')[2]} 成功")
                return resp.text
            errors.append(f"代理 {proxy_url.split('/')[2]}: HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"代理 {proxy_url.split('/')[2]}: {exc}")

    # 通道3：Jina Reader（markdown）
    try:
        resp = requests.get(
            "https://r.jina.ai/" + url,
            headers={"User-Agent": _BASE_HEADERS["User-Agent"]},
            timeout=timeout * 2,
        )
        if resp.ok and len(resp.text) > 500:
            print("  [fetch] 通道: jina-reader 成功")
            return resp.text
        errors.append(f"jina: HTTP {resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"jina: {exc}")

    raise RuntimeError("所有抓取通道均失败: " + "；".join(errors))


def _fetch_direct(url: str, timeout: int, max_retries: int = 3) -> str:
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

            if _is_validator_page(resp.url, resp.text):
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
            if attempt < max_retries:
                print(f"  [retry] {url} 请求异常: {exc}，"
                      f"{_backoff_seconds(attempt):.0f}s 后重试 ({attempt}/{max_retries})")
                time.sleep(_backoff_seconds(attempt))
            else:
                print(f"  [error] {url} 直连失败: {exc}")
    raise last_exc


def parse_source(source: dict) -> list[dict]:
    """抓取并解析单个源，返回条目列表。

    每个条目: {"key": 去重键, "title": 标题, "url": 详情页地址, "date": 发布日期}
    兼容 HTML（选择器解析）与 markdown（Jina Reader 的 [标题](链接) 形式）。
    """
    list_url = (source.get("list_url") or "").strip()
    if not list_url:
        raise ValueError(f"源 {source.get('name', '?')} 缺少 list_url")

    text = fetch_html(list_url)
    soup = BeautifulSoup(text, "html.parser")

    item_selector = source.get("item_selector", "li").strip()
    title_selector = source.get("title_selector", "a").strip()
    date_selector = (source.get("date_selector") or "").strip()
    date_prefix = source.get("date_prefix", "")
    max_items = int(source.get("max_items", 30))

    items = _parse_html_items(soup, source, item_selector, title_selector,
                              date_selector, date_prefix, max_items)
    if not items and _looks_like_markdown(text):
        print(f"  [info] 源 {source.get('name', list_url)} 返回 markdown，按链接条目解析")
        items = _parse_markdown_items(text, list_url, max_items)

    if not items:
        print(f"  [warn] 源 {source.get('name', list_url)} 未解析到任何条目，"
              f"请检查 item_selector={item_selector!r} / title_selector={title_selector!r}")
    return items


def _parse_html_items(soup, source: dict, item_selector: str, title_selector: str,
                      date_selector: str, date_prefix: str, max_items: int) -> list[dict]:
    items = []
    for el in soup.select(item_selector)[:max_items]:
        title_a = el.select_one(title_selector)
        if title_a is None:
            continue
        title = title_a.get_text(strip=True)
        href = (title_a.get("href") or "").strip()
        if not title:
            continue

        link = urljoin(source["list_url"], href)
        # 标题中的版本年份（如 CorelDRAW 2019）用于补全只有月-日的日期
        title_year = None
        _ym = re.search(r"(19|20)\d{2}", title)
        if _ym:
            title_year = int(_ym.group(0))
        date = _extract_date(el, date_selector, date_prefix, title_year)

        key = link if href else f"{title}|{date}"
        items.append({"key": key, "title": title, "url": link, "date": date})
    return items


def _parse_markdown_items(text: str, base_url: str, max_items: int) -> list[dict]:
    """解析 markdown 链接条目：[标题](https://...)。"""
    items = []
    for m in list(_MD_LINK_RE.finditer(text))[:max_items]:
        title = m.group(1).strip()
        href = m.group(2).strip()
        if not title or not href or href.startswith(("data:", "mailto:", "#")):
            continue
        link = urljoin(base_url, href)
        items.append({"key": link, "title": title, "url": link, "date": ""})
    return items


def _looks_like_markdown(text: str) -> bool:
    """粗略判断是否为 markdown（含较多 [..](..) 链接且几乎无 HTML 标签）。"""
    if "<html" in text.lower() or "<body" in text.lower():
        return False
    links = _MD_LINK_RE.findall(text)
    return len(links) >= 3


def _extract_date(item_el, date_selector: str, date_prefix: str,
                  fallback_year: int | None = None) -> str:
    if not date_selector:
        return ""
    el = item_el.select_one(date_selector)
    if el is None:
        return ""
    text = el.get_text(strip=True)
    text = text.replace(date_prefix, "").strip()
    # 完整日期：2026-09-25 / 2026/09/25 / 2026年09月25日
    m = re.search(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}", text)
    if m:
        return m.group(0)
    # 只有月-日（如 09-20）：优先用标题版本年份，否则用当前年（423down 等站点）
    m2 = re.search(r"(\d{1,2})[-/月.](\d{1,2})", text)
    if m2:
        y = fallback_year or datetime.now().year
        return f"{y}-{int(m2.group(1)):02d}-{int(m2.group(2)):02d}"
    return text


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


def _debug_dump(text: str, path: str = "/tmp/fetched.txt"):
    """调试辅助：把抓取内容落盘（仅排查时使用）。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


if __name__ == "__main__":
    # 命令行自测：python crawler.py <url>
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.x6d.com/html/23.html"
    print(f"抓取: {url}")
    text = fetch_html(url)
    print(f"通道返回 {len(text)} 字符, 前 120 字符:")
    print(text[:120].replace("\n", " "))
    source = {
        "name": "test", "list_url": url,
        "item_selector": "ul.list-soft li.layui-clear",
        "title_selector": "a.soft-title",
        "date_selector": "div.list-ca", "date_prefix": "时间：",
        "max_items": 5,
    }
    items = parse_source(source)
    for it in items:
        print(" -", it["title"], "|", it["url"])
