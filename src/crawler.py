# -*- coding: utf-8 -*-
"""通用软件站列表页爬虫（多通道 + 按域名隔离降级）。

通过 CSS 选择器配置即可适配任意列表页（如 x6d、423Down 等），
每个源在 config.json 中声明 item/title/date 选择器，无需改代码。

多通道抓取（应对站点对数据中心 IP 的封锁，如 GitHub Actions 访问 x6d 被 502）：
  1. 直连：完整浏览器请求头 + 指数退避重试 + 反爬验证页识别
  2. 公共 CORS 代理：allorigins / codetabs / cors.eu.org
  3. Jina Reader（r.jina.ai）：返回 markdown，按链接条目解析

性能与隔离要点：
  - 连接复用：每线程一个 requests.Session（keep-alive，省掉逐次 TLS 握手）
  - 降级隔离：直连连续失败计数**按域名**统计，A 站被封不会拖累 B 站走慢代理
  - 同轮去重：同一 URL 在一次运行内只抓一次（列表页与分页模板常重叠）
"""
import re
import threading
import time
from datetime import datetime
from urllib.parse import quote, urljoin, urlparse

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

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MIN_USEFUL_LEN = 500
_ASSET_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp",
               ".css", ".js", ".json", ".xml", ".mp4", ".m3u8")

# 免 key 公共 CORS 代理（构造完整请求 URL）
# 注：thingproxy.freeboard.io 域名已失效（DNS 解析不到），故移除。
_PROXY_BUILDERS = [
    lambda u: "https://api.allorigins.win/raw?url=" + quote(u, safe=""),
    lambda u: "https://api.codetabs.com/v1/proxy?quest=" + quote(u, safe=""),
    lambda u: "https://cors.eu.org/" + u,
]

_tls = threading.local()
_HOST_STATE: dict[str, dict] = {}
_HOST_LOCK = threading.Lock()
_URL_CACHE: dict[str, str] = {}
_URL_LOCK = threading.Lock()
_RUN_ERRORS: dict[str, str] = {}   # 本轮内各 URL 的失败原因（url -> message）


def _session() -> requests.Session:
    """每线程一个 Session，复用 TCP/TLS 连接。"""
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(_BASE_HEADERS)
        _tls.session = s
    return s


def _host_of(url: str) -> str:
    return urlparse(url).netloc or url


def clear_url_cache() -> None:
    """清空同轮抓取缓存与失败记录（main.py 每轮开始时调用）。"""
    with _URL_LOCK:
        _URL_CACHE.clear()
        _RUN_ERRORS.clear()


def run_errors() -> dict:
    """本轮已记录的抓取失败原因（url -> message），供日志与 last_check.json 使用。"""
    with _URL_LOCK:
        return dict(_RUN_ERRORS)


def _headers_for(url: str) -> dict:
    """为指定页面生成带同源 Referer 的完整浏览器请求头。"""
    parsed = urlparse(url)
    return {**_BASE_HEADERS, "Referer": f"{parsed.scheme}://{parsed.netloc}/"}


def _backoff_seconds(attempt: int) -> float:
    return attempt * 5.0


def _is_validator_page(url: str, text: str) -> bool:
    """反爬验证页识别（如跳转到 /GE/CC/VALIDATOR 的 JS 验证页）。"""
    return "VALIDATOR" in url or "VALIDATOR" in (text or "")[:2000]


def _host_state(host: str) -> dict:
    """按域名取降级状态：连续直连失败数 / 调用次数。

    连续失败达 3 次即降级跳过直连，之后每 25 次调用探测一次，
    WAF 冷却后自动切回快速直连。**关键是分域名统计**：
    某一站点封数据中心 IP，不应把其他站点的直连一起降级。
    """
    with _HOST_LOCK:
        return _HOST_STATE.setdefault(host, {"fails": 0, "calls": 0})


def fetch_html(url: str, timeout: int = 30, use_cache: bool = True) -> str:
    """多通道拉取页面文本（HTML 或 markdown），任一通道成功即返回。

    同轮内同一 URL 只真正抓取一次（use_cache=True）。
    """
    if use_cache:
        with _URL_LOCK:
            if url in _URL_CACHE:
                return _URL_CACHE[url]

    text = _fetch_html_uncached(url, timeout)

    if use_cache:
        with _URL_LOCK:
            _URL_CACHE[url] = text
    return text


def _decode_text(resp) -> str:
    """按响应头 charset 解码；未声明或为 HTTP 默认 latin-1 时才跑自动探测。

    中文软件站大量是 GBK 页面，直接取 resp.text 会变乱码；
    而 apparent_encoding 对整页做字符统计，只在必要时才付出这个开销。
    """
    declared = (resp.headers.get("Content-Type") or "").lower()
    if "charset=" not in declared or "iso-8859-1" in declared:
        resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def _fetch_html_uncached(url: str, timeout: int) -> str:
    errors = []
    st = _host_state(_host_of(url))
    st["calls"] += 1

    # 通道1：直连（带浏览器头 + 重试）；本域名连续失败 3 次后降级，每 25 次探测恢复
    probe = st["fails"] < 3 or st["calls"] % 25 == 0
    if probe:
        try:
            text = _fetch_direct(url, timeout)
            st["fails"] = 0
            print("  [fetch] 通道: 直连成功")
            return text
        except Exception as exc:  # noqa: BLE001
            st["fails"] += 1
            errors.append(f"直连: {exc}")
    else:
        errors.append(f"直连已降级({_host_of(url)}，跳过，每25次自动探测恢复)")

    # 通道2：公共 CORS 代理
    for builder in _PROXY_BUILDERS:
        proxy_url = builder(url)
        host = proxy_url.split("/")[2]
        try:
            resp = _session().get(proxy_url, timeout=timeout)
            if resp.ok and len(resp.content) > _MIN_USEFUL_LEN:
                text = _decode_text(resp)
                print(f"  [fetch] 通道: 代理 {host} 成功")
                return text
            reason = (f"HTTP {resp.status_code}" if not resp.ok
                      else "响应过短(<500字节)")
            errors.append(f"代理 {host}: {reason}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"代理 {host}: {exc}")

    # 通道3：Jina Reader（markdown）。r.jina.ai 不通时连接会挂很久，超时单独封顶
    try:
        resp = _session().get(
            "https://r.jina.ai/" + url,
            headers={"User-Agent": _BASE_HEADERS["User-Agent"]},
            timeout=min(timeout * 2, 25),
        )
        if resp.ok and len(resp.content) > _MIN_USEFUL_LEN:
            text = _decode_text(resp)
            print("  [fetch] 通道: jina-reader 成功")
            return text
        errors.append(f"jina: {'HTTP ' + str(resp.status_code) if not resp.ok else '返回200但正文过短(<500字节)'}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"jina: {exc}")

    raise RuntimeError("所有抓取通道均失败: " + "；".join(errors))


def _fetch_direct(url: str, timeout: int, max_retries: int = 3) -> str:
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = _session().get(url, headers=_headers_for(url), timeout=timeout)
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

            return _decode_text(resp)
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

    每个条目: {"key": 去重键, "title": 标题, "url": 详情页地址, "date": ISO 日期}
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


def pagination_urls(source: dict, max_pages: int = 3) -> list[str]:
    """列出该源需要抓取的列表页 URL（已去重，含 list_url 自身）。"""
    urls = []
    list_url = (source.get("list_url") or "").strip()
    if list_url:
        urls.append(list_url)
    tpl = (source.get("pagination_tpl") or "").strip()
    if tpl:
        for n in range(2, 2 + max(0, max_pages - 1)):
            u = tpl.replace("{n}", str(n))
            if u not in urls:      # 423down 的 list_url 本身就是 /page/2，避免重复抓
                urls.append(u)
    return urls


def _run_jobs(jobs, workers: int = 4) -> dict:
    """并发抓取一组 (source, url) 任务，返回 {url: items 或 None}。

    None 表示该 URL 抓取失败（失败原因已打印/记录到 _LAST_ERRORS）。
    并发度按任务数收敛；任务数为 1 或 workers<=1 时退化为顺序执行，便于本地调试与测试。
    """
    from concurrent.futures import ThreadPoolExecutor

    results: dict = {}
    if not jobs:
        return results

    def one(src, url):
        try:
            return parse_source(src)
        except Exception as exc:  # noqa: BLE001
            _record_error(url, exc)
            return None

    if len(jobs) == 1 or workers <= 1:
        for src, url in jobs:
            results[url] = one(src, url)
        return results

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as ex:
        futs = {ex.submit(one, src, url): url for src, url in jobs}
        for fut, url in futs.items():
            results[url] = fut.result()
    return results


def _record_error(url: str, exc: Exception) -> None:
    """记录本轮各 URL 的失败原因（main.py 读取后写入 last_check.json）。"""
    with _URL_LOCK:
        _RUN_ERRORS[url] = f"{type(exc).__name__}: {exc}" if not isinstance(
            exc, RuntimeError) else str(exc)
    print(f"  [error] 抓取失败 {url}: {exc}")


def parse_source_pages(source: dict, max_pages: int = 3,
                       workers: int = 4) -> list[dict]:
    """抓取列表页前 max_pages 页并合并去重（用于索引快照的增量更新）。

    分页之间并发抓取；同轮内已抓过的 URL 直接命中缓存。
    """
    urls = pagination_urls(source, max_pages)
    results = _run_jobs([(dict(source, list_url=u), u) for u in urls], workers)
    merged = {}
    for its in results.values():
        for it in its or []:
            if it.get("url"):
                merged[it["url"]] = {
                    "title": it["title"], "url": it["url"], "date": it.get("date", "")
                }
    return list(merged.values())


def _search_source(source: dict) -> dict:
    """搜索页结构可能不同于列表页，允许用 search_* 选择器覆盖。"""
    src = dict(source)
    for k in ("item_selector", "title_selector", "date_selector", "date_prefix"):
        sk = "search_" + k
        if source.get(sk) is not None:
            src[k] = source[sk]
    return src


def parse_keyword_search(source: dict, keywords: list, workers: int = 4) -> list[dict]:
    """用源的 search_url 模板逐关键词站内搜索（并发），合并返回条目。

    - 部分关键词失败：打印警告并返回已成功抓到的条目
    - 全部关键词失败：抛 RuntimeError，调用方据此保留旧基线、不做任何覆盖
    """
    tpl = (source.get("search_url") or "").strip()
    if not tpl:
        raise ValueError(f"源 {source.get('name', '?')} 未配置 search_url")
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    base = _search_source(source)
    jobs = []
    for kw in (kws or [None]):
        url = tpl.replace("{keyword}", quote(kw)) if kw else tpl
        jobs.append((dict(base, list_url=url), url))

    results = _run_jobs(jobs, workers)
    failed = [u for u, its in results.items() if its is None]
    items = [it for its in results.values() if its for it in its]

    if failed:
        print(f"  [warn] {source.get('name', tpl)} 有 {len(failed)}/{len(jobs)} 个"
              f"关键词搜索失败: {', '.join(failed)}")
    if not items and failed:
        raise RuntimeError("站内搜索全部失败: " +
                           "；".join(_RUN_ERRORS.get(u, "") for u in failed))
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
    """解析 markdown 链接条目：[标题](https://...)。

    Jina Reader 这类 markdown 通道会把配图链接、带 title 的链接一并给出，
    直接收进条目会污染基线（实测 423down 混进 *.png 与 `URL "标题"` 形态的脏 key），
    所以这里过滤掉非网页资源并要求链接不含空白/引号。
    """
    items = []
    for m in list(_MD_LINK_RE.finditer(text))[:max_items]:
        title = m.group(1).strip()
        href = _clean_md_href(m.group(2))
        if not title or not href or href.startswith(("data:", "mailto:", "#")):
            continue
        if _is_asset_href(href):
            continue
        link = urljoin(base_url, href)
        if not link.lower().startswith(("http://", "https://")):
            continue
        items.append({"key": link, "title": title, "url": link, "date": ""})
    return items


def _clean_md_href(raw: str) -> str:
    """markdown 允许 [文本](URL "标题")；把尾部 title 与尖括号剥掉。"""
    h = str(raw or "").strip().strip("<>")
    if not h:
        return ""
    parts = h.split()
    h = parts[0] if parts else ""
    return h.strip('"\'')


def _is_asset_href(href: str) -> bool:
    """图片/样式/脚本等资源链接不是软件条目。"""
    path = urlparse(href).path.lower()
    return path.endswith(_ASSET_EXTS)


def _looks_like_markdown(text: str) -> bool:
    """粗略判断是否为 markdown（含较多 [..](..) 链接且几乎无 HTML 标签）。"""
    if "<html" in text.lower() or "<body" in text.lower():
        return False
    links = _MD_LINK_RE.findall(text)
    return len(links) >= 3


def normalize_date(raw: str, fallback_year: int | None = None) -> str:
    """把各种写法的日期归一化为 YYYY-MM-DD；无法识别返回空串。

    支持 2026-09-25 / 2026/09/25 / 2026年09月25日 / 09-20（缺年用 fallback）。
    统一格式后索引排序与前端排序才可靠。
    """
    t = str(raw or "").strip()
    if not t:
        return ""
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", t)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(?<!\d)(\d{1,2})[-/月.](\d{1,2})(?!\d)", t)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        # 站点只显示「月-日」，年份只能推断。先试给定年份（列表页常取当前年，
        # 搜索页可能是标题里的版本年份），推出来跑到未来就说明跨了年，退回上一年。
        now = datetime.now()
        for y in (fallback_year or now.year, (fallback_year or now.year) - 1):
            try:
                d = datetime(y, mm, dd)
            except ValueError:
                continue
            if d <= now:
                return f"{y:04d}-{mm:02d}-{dd:02d}"
        return ""
    return ""


def _extract_date(item_el, date_selector: str, date_prefix: str,
                  fallback_year: int | None = None) -> str:
    if not date_selector:
        return ""
    el = item_el.select_one(date_selector)
    if el is None:
        return ""
    # <time datetime="2026-09-25T10:00:00+08:00"> 这类机器可读属性优先
    attr = (el.get("datetime") or "").strip()
    if attr:
        norm = normalize_date(attr, fallback_year)
        if norm:
            return norm
    text = el.get_text(strip=True).replace(date_prefix, "").strip()
    return normalize_date(text, fallback_year)


def match_keywords(title: str, keywords: list, exclude: list | None = None) -> bool:
    """判断标题是否命中任一关注关键词（大小写不敏感），且不含排除词。

    keywords 为空时视为关注全部，返回 True。
    """
    keys = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]
    ex = [str(k).strip().lower() for k in (exclude or []) if str(k).strip()]
    t = str(title or "").lower()
    if any(k in t for k in ex):
        return False
    if not keys:
        return True
    return any(k in t for k in keys)


if __name__ == "__main__":
    # 命令行自测：python src/crawler.py [源id]  （读取 src/config.json 的真实规则）
    import json
    import sys
    from pathlib import Path

    sid = sys.argv[1] if len(sys.argv) > 1 else "x6d-green"
    cfg = json.loads((Path(__file__).resolve().parent / "config.json")
                     .read_text(encoding="utf-8"))
    src = next((s for s in cfg["sources"] if s.get("id") == sid), None)
    if src is None:
        print(f"config.json 中找不到源 {sid!r}，可选：" +
              ", ".join(s["id"] for s in cfg["sources"]))
        sys.exit(2)
    print(f"抓取源 {sid}: {src['list_url']}")
    for it in parse_source(src)[:10]:
        print(" -", it["date"] or "—", "|", it["title"], "|", it["url"])
