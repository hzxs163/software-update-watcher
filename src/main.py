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

from crawler import parse_source
from notify import resolve_push_config, send_wxpusher, build_message

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
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

    sources = [s for s in config.get("sources", []) if s.get("enabled", True)]
    if not sources:
        print("[warn] config.json 中没有启用的源，请先在网页端/仓库中配置")

    all_new: list[dict] = []
    now = now_iso()

    for source in sources:
        sid = source.get("id") or source.get("name", "source")
        name = source.get("name", sid)
        print(f"[check] {name} -> {source.get('list_url')}")

        try:
            items = parse_source(source)
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] 抓取/解析失败: {exc}")
            continue

        info = state["sources"].get(sid, {})
        seen = set(info.get("seen", []))
        is_init = info.get("initialized", False)

        new_items = [it for it in items if it["key"] not in seen]
        if is_init:
            for it in new_items:
                print(f"  [new] {it['title']} {it['url']}")
            all_new.extend({"source": name, **it} for it in new_items)
        else:
            print(f"  [init] 首次运行，记录 {len(items)} 条基线（本次不推送）")

        # 合并关键字：新条目在前，历史在后，截断到 SEEN_LIMIT
        merged = list(dict.fromkeys(
            [it["key"] for it in new_items] + [it["key"] for it in items]
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

    if all_new:
        push_cfg = resolve_push_config(config)
        summary = f"软件站更新：{len(all_new)} 条新内容"
        html_content = build_message(all_new)
        if push_cfg["token"] and push_cfg["target_value"]:
            ok = send_wxpusher(push_cfg, summary, html_content)
            print(f"[push] WxPusher 推送{'成功' if ok else '失败'}"
                  f"（{len(all_new)} 条新增）")
        else:
            print("[push] 未配置 WXPUSHER_APP_TOKEN / target，"
                  "跳过推送（新增条目见上方日志）")

    print(f"[done] 检查完成，新增 {len(all_new)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
