#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""软件站更新监控主入口。

流程：
  1. 读取 config.json（关注的源）与 state.json（历史基线）
  2. 依次抓取各源（有 search_url 则按关键词站内搜索），解析条目
  3. 与历史基线**合并**对比，找出新增条目 / 标题变化的内容更新
  4. 有新增则通过 WxPusher 推送；源连续多轮抓取失败也会推送告警
  5. 抓取失败的源**保留旧基线**，绝不写空，避免下轮恢复时误报一堆「新发布」

用法：
  python src/main.py                      # 常规检查
  python src/main.py --full-index         # 全量重建索引（可反复运行，每轮增量合并）
  python src/main.py --source gndown --pages 60   # 分批跑全量，避免撞 Actions 超时
环境变量（可选，对应 config.push 中的 ${VAR} 模板）：
  WXPUSHER_APP_TOKEN   WxPusher 应用令牌
  WXPUSHER_UID         微信用户 UID，或 topic:主题ID
退出码：
  0  正常（含部分源失败——已记录并告警）
  1  所有启用的源本轮全部抓取失败（Actions 会显示为失败，便于发现问题）
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crawler import (
    clear_url_cache,
    match_keywords,
    parse_keyword_search,
    parse_source,
    parse_source_pages,
)
from notify import build_message, resolve_push_config, send_wxpusher

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LAST_CHECK_PATH = BASE_DIR / "last_check.json"
SEEN_LIMIT = 100        # 每个源保留最近多少条基线关键字（超出的按日期淘汰最旧）
INDEX_DIR = BASE_DIR / "index"   # 各源索引快照（前端搜索加速用）
MAX_INDEX = 3000        # 索引条数上限（全量索引，前端搜索加速用）
FAIL_ALERT_STREAK = 4   # 连续这么多轮抓不到 → 推一条「源已失效」告警（每次故障只推一次）
DEFAULT_WORKERS = 4     # 同站内分页/关键词的并发抓取数


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[warn] 无法解析 {path.name}，将使用默认值: {exc}")
    return default


def _local_tz():
    """北京时间时区；zoneinfo 不可用（缺 tzdata）时退回固定 +08:00。"""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_local() -> str:
    """北京时间（代码内显式换算，不依赖运行环境的 TZ）。"""
    return datetime.now(_local_tz()).strftime("%Y-%m-%d %H:%M:%S")


def sort_items(items: list) -> list:
    """按日期倒序；无日期的排最后（用 0000-00-00 兜底，避免混排时抢到前面）。"""
    return sorted(items, key=lambda x: (x.get("date") or "0000-00-00",
                                        x.get("title") or ""), reverse=True)


def sanitize_future_date(item: dict, today: str) -> dict:
    """清掉「未来日期」——这类值只可能来自旧版本的年份推断 bug（曾出现 2027-10-09）。

    新代码不会再把日期推外推到未来，但索引是跨运行合并的，旧数据得在这里洗掉；
    宁可留空，也不留一个看起来像「预告」的错误日期。
    """
    d = item.get("date") or ""
    if d and d > today:
        item = dict(item)
        item["date"] = ""
    return item


def update_index(source: dict, items: list) -> bool:
    """更新 {sid}.json 索引快照：按 URL 去重合并，日期倒序，截断保留最新。

    返回是否写入（内容无变化时不写，避免无效提交）。
    """
    sid = source.get("id") or source.get("name", "source")
    idx_path = INDEX_DIR / f"{sid}.json"
    old = load_json(idx_path, {"updated_at": "", "items": []})
    today = datetime.now(_local_tz()).strftime("%Y-%m-%d")
    merged = {}
    for it in old.get("items", []):
        if it.get("url"):
            merged[it["url"]] = sanitize_future_date(it, today)
    for it in items:
        if it.get("url"):
            merged[it["url"]] = sanitize_future_date({
                "title": it["title"], "url": it["url"], "date": it.get("date", "")
            }, today)
    new_items = sort_items(list(merged.values()))[:MAX_INDEX]
    if old.get("items") != new_items:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        idx_path.write_text(
            json.dumps({"updated_at": now_local(), "items": new_items},
                       ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        return True
    return False


def normalize_seen(old) -> dict:
    """兼容旧格式（key 数组）→ {key: None}；None 表示无标题记录，仅做新条目检测。"""
    if isinstance(old, list):
        return {k: None for k in old}
    if isinstance(old, dict):
        return dict(old)
    return {}


def merge_seen(old_seen: dict, matched: list, limit: int = SEEN_LIMIT) -> dict:
    """把本轮命中的条目并入历史基线（只增不删，超出 limit 时淘汰最旧的）。

    合并而非覆盖是修正误报的关键：某轮抓取窗口变小 / 部分关键词失败时，
    历史条目不会因此丢失，下一轮也就不会被当成「新发布」重新推送。
    """
    merged = dict(old_seen)
    dates = {}
    for it in matched:
        merged[it["key"]] = it["title"]
        dates[it["key"]] = it.get("date") or ""
    if len(merged) <= limit:
        return merged
    order = list(merged)
    keep = sorted(order,
                  key=lambda k: (dates.get(k) or "0000-00-00", order.index(k)),
                  reverse=True)[:limit]
    return {k: merged[k] for k in sorted(keep, key=order.index)}


def fetch_items(source: dict, keywords: list, workers: int) -> list:
    """抓取单个源。全部通道/关键词失败时抛异常，调用方据此保留旧基线。

    若源配置了 search_url 模板（如 https://www.423down.com/search/{keyword}）：
      对每个关注关键词站内搜索，覆盖该站全站历史记录；
    否则抓取 list_url 列表页，由调用方按关键词过滤。
    """
    tpl = (source.get("search_url") or "").strip()
    if tpl and keywords:
        return parse_keyword_search(source, keywords, workers)
    return parse_source(source)


def seen_changed(old_state: dict, new_state: dict) -> bool:
    """对比新旧 state 的实质内容（seen / 失败计数），忽略每轮都变的时间戳。"""
    def meaningful(state):
        out = {}
        for sid, info in (state.get("sources") or {}).items():
            out[sid] = {"seen": info.get("seen"),
                        "fail_streak": info.get("fail_streak", 0)}
        return out

    return meaningful(old_state) != meaningful(new_state)


def build_full_index(sources: list, pages_cap: int | None, workers: int) -> None:
    """全量重建各源索引：抓取分页模板 1..full_pages（并发），增量合并进索引文件。

    可反复运行：每轮都与已有索引合并，撞超时/中断后接着跑即可，不会丢已有成果。
    """
    from crawler import pagination_urls

    for source in sources:
        name = source.get("name", source.get("id", "source"))
        tpl = (source.get("pagination_tpl") or "").strip()
        declared = int(source.get("full_pages", 0) or 0)
        if not tpl or declared <= 0:
            print(f"  [full] {name}: 无 pagination_tpl/full_pages 配置，跳过")
            continue
        limit = declared if not pages_cap else min(declared, pages_cap)
        urls = pagination_urls(source, limit)
        print(f"  [full] {name}: 本轮抓取 {len(urls)}/{declared} 页（并发 {workers}）")
        items = parse_source_pages(source, limit, workers)
        if update_index(source, items):
            print(f"  [full] {name} 完成：索引现有 {len(items)} 条")
        if pages_cap and declared > pages_cap:
            print(f"  [full] {name}: 尚有分页未跑，可再次运行（或调大 --pages）接着合并")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="软件站更新监控")
    p.add_argument("--full-index", action="store_true",
                   help="全量重建索引（抓取全站所有分页，耗时较长）")
    p.add_argument("--source", help="只处理指定源 id（默认处理全部启用的源）")
    p.add_argument("--pages", type=int, help="索引本轮最多抓取的页数（配合 --full-index 分批跑）")
    p.add_argument("--index-pages", type=int, default=3,
                   help="常规运行时列表页抓取的页数上限（默认 %(default)s）")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help="同站内分页/关键词并发数（默认 %(default)s）")
    p.add_argument("--no-index", action="store_true", help="跳过索引快照（只做更新检测）")
    return p.parse_known_args(argv)[0]


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_json(CONFIG_PATH, {"sources": [], "push": {}})
    old_state = load_json(STATE_PATH, {"sources": {}})
    state = json.loads(json.dumps(old_state))  # 深拷贝，避免误改
    if not isinstance(state.get("sources"), dict):
        state["sources"] = {}

    clear_url_cache()
    global_keywords = config.get("keywords") or []
    global_exclude = config.get("exclude_keywords") or []
    kw_txt = "、".join(str(k) for k in global_keywords) or "关注全部"
    print(f"[config] 关注关键词: {kw_txt}"
          + (f"；排除词: {'、'.join(global_exclude)}" if global_exclude else ""))

    sources = [s for s in config.get("sources", []) if s.get("enabled", True)]
    if args.source:
        sources = [s for s in sources if s.get("id") == args.source]
    if not sources:
        print("[warn] config.json 中没有匹配的启用源，请检查配置或 --source 参数")
        return 0

    if args.full_index:
        build_full_index(sources, args.pages, args.workers)

    all_new: list[dict] = []
    now = now_iso()
    src_stats = {}          # 各源统计，写入 last_check.json
    failed_names = []       # 本轮抓取失败的源
    alerts: list[str] = []  # 待推送的源健康告警

    for source in sources:
        sid = source.get("id") or source.get("name", "source")
        name = source.get("name", sid)
        print(f"[check] {name} -> {source.get('list_url')}")

        info = state["sources"].get(sid, {})
        old_seen = normalize_seen(info.get("seen", {}))
        prev_streak = int(info.get("fail_streak", 0) or 0)

        try:
            items = fetch_items(source, global_keywords, args.workers)
        except Exception as exc:  # noqa: BLE001
            streak = prev_streak + 1
            # 关键：失败时保留旧基线，只更新健康字段，下一轮恢复后不会误报
            new_info = dict(info)
            new_info.update({"seen": old_seen, "last_check": now,
                             "initialized": info.get("initialized", False),
                             "fail_streak": streak, "last_error": str(exc)})
            state["sources"][sid] = new_info
            src_stats[sid] = {"name": name, "error": str(exc), "fail_streak": streak,
                              "kept_seen": len(old_seen)}
            failed_names.append(name)
            print(f"  [error] 抓取/解析失败（第 {streak} 轮连续），"
                  f"保留旧基线 {len(old_seen)} 条，本轮不推送新增")
            if streak == FAIL_ALERT_STREAK:
                alerts.append(
                    f"<b>⚠ 源已连续 {streak} 轮抓取失败：</b>{name}"
                    f"<br/><span style='color:#888;font-size:12px'>{str(exc)[:300]}</span>")
            continue

        if prev_streak >= FAIL_ALERT_STREAK:
            alerts.append(f"<b>✅ 源已恢复正常：</b>{name}（此前连续失败 {prev_streak} 轮）")

        matched = [it for it in items
                   if match_keywords(it["title"], global_keywords, global_exclude)]
        if global_keywords:
            print(f"  [filter] 抓到 {len(items)} 条，命中关键词 {len(matched)} 条")

        is_init = bool(info.get("initialized", False)) and bool(old_seen)

        # 双维度检测：新 URL = 新发布；URL 相同但标题变了 = 内容/版本更新
        new_items = []
        for it in matched:
            if it["key"] not in old_seen:
                new_items.append({"item": it, "kind": "new"})
            else:
                old_title = old_seen[it["key"]]
                if old_title is not None and old_title != it["title"]:
                    new_items.append({"item": it, "kind": "updated"})
        if is_init:
            for entry in new_items:
                it = entry["item"]
                tag = "[new]" if entry["kind"] == "new" else "[updated]"
                print(f"  {tag} {it['title']} {it['url']}")
                if entry["kind"] == "updated":
                    print(f"        标题变化（{old_seen[it['key']]} -> {it['title']}），视为更新")
            all_new.extend({"source": name, "kind": e["kind"], **e["item"]}
                           for e in new_items)
        else:
            print(f"  [init] 首次运行，记录 {len(matched)} 条基线（本次不推送）")

        # 索引快照 = 列表页前 N 页 ∪ 本轮关键词搜索结果。
        # 后者是关键：深层列表页会被站点掐 IP（实测 x6d 约 195 页、423down 约 157 页起
        # 返回连接重置/302 死循环），而站内搜索一次就给出该关键词的全站历史。
        # 前端搜索因此不再依赖「实时公共代理是否争气」——冷门关键词也能从索引直接出全量。
        if not args.no_index:
            try:
                index_items = parse_source_pages(source, args.index_pages, args.workers)
            except Exception as exc:  # noqa: BLE001
                print(f"  [warn] 索引快照抓取失败({name}): {exc}")
                index_items = []
            index_items = index_items + items
            if update_index(source, index_items):
                print(f"  [index] {sid}.json 索引已更新（本轮并入 {len(index_items)} 条）")

        new_seen = merge_seen(old_seen, matched)
        state["sources"][sid] = {"seen": new_seen, "last_check": now,
                                 "initialized": True, "fail_streak": 0,
                                 "last_error": None}
        src_stats[sid] = {"name": name, "fetched": len(items), "matched": len(matched),
                          "new": len(new_items), "seen_total": len(new_seen)}

    if seen_changed(old_state, state):
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("[state] state.json 已更新")
    else:
        print("[state] 无变化，state.json 保持不变")

    push_cfg = resolve_push_config(config)
    pushed = push_update_alert(push_cfg, all_new)
    if alerts:
        push_health_alert(push_cfg, alerts)

    last_check = build_last_check(now_local(), global_keywords, all_new, src_stats,
                                  failed_names, len(sources), pushed)
    LAST_CHECK_PATH.write_text(
        json.dumps(last_check, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"[done] 检查完成，新增 {len(all_new)} 条；"
          f"抓取失败 {len(failed_names)}/{len(sources)} 个源")
    if failed_names and len(failed_names) == len(sources):
        print("::error:: 所有启用的源本轮全部抓取失败（详见 last_check.json 的 failed_sources）")
        return 1
    for n in failed_names:
        print(f"::warning:: 源抓取失败: {n}")
    return 0


def push_update_alert(push_cfg: dict, all_new: list[dict]) -> bool:
    """推送本次检测到的新增/更新条目。"""
    if not all_new:
        return False
    n_new = sum(1 for e in all_new if e.get("kind") == "new")
    n_upd = sum(1 for e in all_new if e.get("kind") == "updated")
    detail = f"{n_new} 条新发布" + (f"，{n_upd} 条内容更新" if n_upd else "")
    if push_cfg["token"] and push_cfg["target_value"]:
        ok = send_wxpusher(push_cfg, f"软件站更新：{detail}", build_message(all_new))
        print(f"[push] WxPusher 推送{'成功' if ok else '失败'}（{len(all_new)} 条新增）")
        return ok
    print("[push] 未配置 WXPUSHER_APP_TOKEN / target，跳过推送（新增条目见上方日志）")
    return False


def push_health_alert(push_cfg: dict, alerts: list[str]) -> bool:
    """源健康告警：连续多轮抓不到 / 恢复正常时推送，避免挂了两周没人知道。"""
    if not alerts:
        return False
    content = "<h4>软件更新监控 · 源健康告警</h4>" + "".join(
        f"<p>{a}</p>" for a in alerts)
    if not (push_cfg["token"] and push_cfg["target_value"]):
        for a in alerts:
            print(f"[alert] {a}")
        print("[alert] 未配置推送密钥，仅记录日志")
        return False
    ok = send_wxpusher(push_cfg, f"监控源健康告警（{len(alerts)} 条）", content)
    print(f"[alert] WxPusher 源健康告警{'成功' if ok else '失败'}")
    return ok


def build_last_check(checked_at: str, keywords: list, all_new: list,
                     src_stats: dict, failed_names: list, total_sources: int,
                     pushed: bool) -> dict:
    """组装供网页端展示的 last_check.json。"""
    if all_new:
        n_new = sum(1 for e in all_new if e.get("kind") == "new")
        n_upd = sum(1 for e in all_new if e.get("kind") == "updated")
        detail = f"{n_new} 条新发布" + (f"，{n_upd} 条内容更新" if n_upd else "")
        msg = f"有更新！{detail}" + ("，已推送微信" if pushed
                                     else "（未配置推送密钥，未推送）")
    elif failed_names:
        msg = f"无更新（{len(failed_names)}/{total_sources} 个源抓取失败：" \
              f"{'、'.join(failed_names)}）"
    else:
        msg = "无更新"
    return {
        "checked_at": checked_at,
        "checked_at_tz": "Asia/Shanghai",
        "keywords": keywords,
        "new_total": len(all_new),
        "pushed": pushed,
        "message": msg,
        "all_sources_failed": bool(failed_names) and len(failed_names) == total_sources,
        "failed_sources": failed_names,
        "sources": src_stats,
    }


if __name__ == "__main__":
    sys.exit(main())
