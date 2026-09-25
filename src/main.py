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

from crawler import parse_source, match_keywords
from notify import resolve_push_config, send_wxpusher, build_message

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LAST_CHECK_PATH = BASE_DIR / "last_check.json"
SEEN_LIMIT = 100  # 每个源最多保留最近 100 条关键字


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


def main() -> int:
    config = load_json(CONFIG_PATH, {"sources": [], "push": {}})
    old_state = load_json(STATE_PATH, {"sources": {}})
    state = json.loads(json.dumps(old_state))  # 深拷贝，避免误改

    # 全局关注关键词（config.json 顶层 keywords，适用于所有源；空 = 关注全部）
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
            items = parse_source(source)
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] 抓取/解析失败: {exc}")
            src_stats[sid] = {"name": name, "error": str(exc)}
            continue

        matched = [it for it in items if match_keywords(it["title"], global_keywords)]
        if global_keywords:
            print(f"  [filter] 抓到 {len(items)} 条，命中关键词 {len(matched)} 条")

        info = state["sources"].get(sid, {})
        seen = set(info.get("seen", []))
        is_init = info.get("initialized", False)

        new_items = [it for it in matched if it["key"] not in seen]
        if is_init:
            for it in new_items:
                print(f"  [new] {it['title']} {it['url']}")
            all_new.extend({"source": name, **it} for it in new_items)
        else:
            print(f"  [init] 首次运行，记录 {len(matched)} 条基线（本次不推送）")

        src_stats[sid] = {
            "name": name,
            "fetched": len(items),
            "matched": len(matched),
            "new": len(new_items),
        }

        # 合并关键字：新条目在前，历史在后，截断到 SEEN_LIMIT
        merged = list(dict.fromkeys(
            [it["key"] for it in new_items] + [it["key"] for it in matched]
        ))[:SEEN_LIMIT]
        state["sources"][sid] = {
            "seen": merged,
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
        summary = f"软件站更新：{len(all_new)} 条新内容"
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
        if pushed:
            msg = f"有更新！{len(all_new)} 条新内容，已推送微信"
        else:
            msg = f"有更新！{len(all_new)} 条新内容（未配置推送密钥，未推送）"
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
