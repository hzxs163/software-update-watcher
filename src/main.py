#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""软件站更新监控主入口。

流程：
  1. 读取 config.json（关注的源）与 state.json（上次看到的关键字）
  2. 依次抓取各源列表页，解析条目
  3. 与上次状态对比，找出新增条目
  4. 有新增则通过 WxPusher 推送
  5. state.json 仅在关键字集合变化时落盘（避免每次运行产生无效提交）

用法：
  python src/main.py
环境变量（可选，对应 config.push 中的 ${VAR} 模板）：
  WXPUSHER_APP_TOKEN   WxPusher 应用令牌
  WXPUSHER_UID         微信用户 UID，或 topic:主题ID
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from crawler import parse_source, parse_source_pages, match_keywords
from notify import resolve_push_config, send_wxpusher, build_message
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LAST_CHECK_PATH = BASE_DIR / "last_check.json"
SEEN_LIMIT = 100  # 每个源最多保留最近 100 条关键字
INDEX_DIR = BASE_DIR / "index"   # 各源索引快照（前端搜索加速用）
MAX_INDEX = 50000                 # 索引上限（全量索引，前端搜索加速用）


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[warn] 无法解析 {path.name}，将使用默认值: {exc}")
    return default


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_local() -> str:
    """运行环境的本地时间（Actions 中已设 TZ=Asia/Shanghai）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def update_index(source: dict, items: list) -> bool:
    """更新 {sid}.json 索引快照：按 URL 去重合并，日期倒序，截断保留最新。

    返回是否写入（内容无变化时不写，避免无效提交）。
    """
    sid = source.get("id") or source.get("name", "source")
    idx_path = INDEX_DIR / f"{sid}.json"
    old = load_json(idx_path, {"updated_at": "", "items": []})
    merged = {}
    for it in old.get("items", []):
        if it.get("url"):
            merged[it["url"]] = it
    for it in items:
        if it.get("url"):
            merged[it["url"]] = {
                "title": it["title"], "url": it["url"], "date": it.get("date", "")
            }
    new_items = sorted(merged.values(), key=lambda x: x.get("date", ""),
                       reverse=True)[:MAX_INDEX]
    if old.get("items") != new_items:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        idx_path.write_text(
            json.dumps({"updated_at": now_local(), "items": new_items},
                       ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        return True
    return False


def _fetch_source_items(source: dict, keywords: list) -> list:
    """抓取单个源。

    若源配置了 search_url 模板（如 https://www.423down.com/search/{keyword}）：
      对每个关注关键词逐个站内搜索，覆盖该站全站历史记录；
    否则抓取 list_url 列表页，由调用方按关键词过滤。
    """
    tpl = (source.get("search_url") or "").strip()
    if tpl and keywords:
        all_items = []
        for kw in keywords:
            url = tpl.replace("{keyword}", quote(str(kw)))
            src = dict(source, list_url=url)
            # 搜索页结构可能不同于列表页，支持 search_* 选择器覆盖
            for k in ("item_selector", "title_selector", "date_selector"):
                sk = "search_" + k
                if source.get(sk):
                    src[k] = source[sk]
            try:
                all_items.extend(parse_source(src))
            except Exception as exc:  # noqa: BLE001
                print(f"  [error] 搜索「{kw}」失败: {exc}")
        return all_items
    return parse_source(source)


def seen_changed(old_state: dict, new_state: dict) -> bool:
    """对比新旧 state 的 seen 关键字集合是否变化。"""
    old_srcs = old_state.get("sources", {})
    new_srcs = new_state.get("sources", {})
    if set(old_srcs) != set(new_srcs):
        return True
    for sid, new_info in new_srcs.items():
        if old_srcs.get(sid, {}).get("seen") != new_info.get("seen"):
            return True
    return False


def build_full_index(sources: list[dict]) -> None:
    """全量重建各源索引：抓取分页模板 2..full_pages（或动态到空页）。"""
    from crawler import parse_source
    for source in sources:
        sid = source.get("id") or source.get("name", "source")
        name = source.get("name", sid)
        tpl = (source.get("pagination_tpl") or "").strip()
        full_pages = int(source.get("full_pages", 0) or 0)
        if not tpl or full_pages <= 0:
            print(f"  [full] {name}: 无 pagination_tpl/full_pages 配置，跳过")
            continue
        print(f"  [full] {name}: 全量抓取 {full_pages} 页")
        merged = {}
        for n in list(range(1, full_pages + 1)):
            url = source["list_url"] if n == 1 else tpl.replace("{n}", str(n))
            try:
                src = dict(source, list_url=url)
                for it in parse_source(src):
                    if it.get("url"):
                        merged[it["url"]] = {
                            "title": it["title"], "url": it["url"], "date": it.get("date", "")
                        }
            except Exception as exc:  # noqa: BLE001
                print(f"  [full] 第 {n} 页失败: {exc}")
        items = sorted(merged.values(), key=lambda x: x.get("date", ""), reverse=True)[:MAX_INDEX]
        update_index(source, items)
        print(f"  [full] {name} 完成：{len(items)} 条")


def main() -> int:
    config = load_json(CONFIG_PATH, {"sources": [], "push": {}})
    old_state = load_json(STATE_PATH, {"sources": {}})
    state = json.loads(json.dumps(old_state))  # 深拷贝，避免误改

    # 全局关注关键词（config.json 顶层 keywords，适用于所有源；空 = 关注全部）
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-index", action="store_true",
                        help="全量重建索引（抓取全站所有分页）")
    args, _ = parser.parse_known_args()

    if args.full_index:
        build_full_index([s for s in config.get("sources", []) if s.get("enabled", True)])
        print("[full-index] 全量索引重建完成")

    global_keywords = config.get("keywords") or []
    kw_txt = f"，全局关键词: {', '.join(str(k) for k in global_keywords)}" \
        if global_keywords else "，关注全部"
    print(f"[config] 关注关键词: {kw_txt.strip('，')}")

    sources = [s for s in config.get("sources", []) if s.get("enabled", True)]
    if not sources:
        print("[warn] config.json 中没有启用的源，请先在网页端/仓库中配置")

    all_new: list[dict] = []
    now = now_iso()
    src_stats = {}  # 各源统计，用于 last_check.json

    for source in sources:
        sid = source.get("id") or source.get("name", "source")
        name = source.get("name", sid)
        print(f"[check] {name} -> {source.get('list_url')}")

        try:
            items = _fetch_source_items(source, global_keywords)
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] 抓取/解析失败: {exc}")
            src_stats[sid] = {"name": name, "error": str(exc)}
            continue

        matched = [it for it in items if match_keywords(it["title"], global_keywords)]
        if global_keywords:
            print(f"  [filter] 抓到 {len(items)} 条，命中关键词 {len(matched)} 条")

        info = state["sources"].get(sid, {})
        seen_raw = info.get("seen", {})
        # 兼容旧格式（key 数组）→ {key: None}；None 表示无标题记录，仅做新条目检测
        if isinstance(seen_raw, list):
            seen = {k: None for k in seen_raw}
        else:
            seen = seen_raw if isinstance(seen_raw, dict) else {}
        is_init = info.get("initialized", False)

        # 双维度检测：新 URL = 新发布；URL 相同但标题变了 = 内容/版本更新
        new_items = []
        for it in matched:
            if it["key"] not in seen:
                new_items.append({"item": it, "kind": "new"})
            else:
                old_title = seen[it["key"]]
                if old_title is not None and old_title != it["title"]:
                    new_items.append({"item": it, "kind": "updated"})
        if is_init:
            for entry in new_items:
                it = entry["item"]
                tag = "[new]" if entry["kind"] == "new" else "[updated]"
                print(f"  {tag} {it['title']} {it['url']}")
                if entry["kind"] == "updated":
                    print(f"        标题变化（{seen[it['key']]} -> {it['title']}），视为更新")
            all_new.extend({"source": name, "kind": e["kind"], **e["item"]} for e in new_items)
        else:
            print(f"  [init] 首次运行，记录 {len(matched)} 条基线（本次不推送）")

        # 索引快照：抓列表页前 3 页增量合并（--full-index 时全量重建）
        try:
            index_items = parse_source_pages(source, 3)
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] 索引快照抓取失败({name}): {exc}")
            index_items = items
        if update_index(source, index_items):
            print(f"  [index] {sid}.json 索引已更新（{len(index_items)} 条本轮）")

        src_stats[sid] = {
            "name": name,
            "fetched": len(items),
            "matched": len(matched),
            "new": len(new_items),
        }

        # 记录最新标题作为基线（key -> title）
        state["sources"][sid] = {
            "seen": {it["key"]: it["title"] for it in matched},
            "last_check": now,
            "initialized": True,
        }

    if seen_changed(old_state, state):
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("[state] state.json 已更新")
    else:
        print("[state] 无变化，state.json 保持不变")

    pushed = False
    if all_new:
        push_cfg = resolve_push_config(config)
        n_new = sum(1 for e in all_new if e.get("kind") == "new")
        n_upd = sum(1 for e in all_new if e.get("kind") == "updated")
        summary = f"软件站更新：{n_new} 条新发布" + (f"，{n_upd} 条内容更新" if n_upd else "")
        html_content = build_message(all_new)
        if push_cfg["token"] and push_cfg["target_value"]:
            ok = send_wxpusher(push_cfg, summary, html_content)
            pushed = ok
            print(f"[push] WxPusher 推送{'成功' if ok else '失败'}"
                  f"（{len(all_new)} 条新增）")
        else:
            print("[push] 未配置 WXPUSHER_APP_TOKEN / target，"
                  "跳过推送（新增条目见上方日志）")

    # 写 last_check.json（每次运行都写，供网页端展示最近检查结果）
    if all_new:
        n_new = sum(1 for e in all_new if e.get("kind") == "new")
        n_upd = sum(1 for e in all_new if e.get("kind") == "updated")
        detail = f"{n_new} 条新发布" + (f"，{n_upd} 条内容更新" if n_upd else "")
        if pushed:
            msg = f"有更新！{detail}，已推送微信"
        else:
            msg = f"有更新！{detail}（未配置推送密钥，未推送）"
    else:
        msg = "无更新"
    last_check = {
        "checked_at": now_local(),
        "keywords": global_keywords,
        "new_total": len(all_new),
        "pushed": pushed,
        "message": msg,
        "sources": src_stats,
    }
    LAST_CHECK_PATH.write_text(
        json.dumps(last_check, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"[done] 检查完成，新增 {len(all_new)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
