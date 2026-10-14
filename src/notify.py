# -*- coding: utf-8 -*-
"""推送通知模块：WxPusher（微信推送）。

接口: POST https://wxpusher.zjiecode.com/api/send/message
推送配置支持 ${ENV_VAR} 模板，从环境变量读取，避免把密钥提交进仓库。

注意：标题与链接均来自被抓取站点属外部不可信内容，拼 HTML 前必须转义，
否则标题里一个单引号就能改写消息结构（甚至注入脚本链接）。
"""
import os
from html import escape

import requests

WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"
_SAFE_SCHEMES = ("http://", "https://")


def resolve_push_config(config: dict) -> dict:
    """从 config.push 解析出 (appToken, target, target_type)。

    - appToken: WxPusher 应用令牌（AT_ 开头），支持 ${ENV} 模板
    - target: 收件人。直接填 UID 字符串，或用 "topic:数字" 指定主题 ID
    """
    push = config.get("push", {}) or {}
    token = _resolve_env(str(push.get("appToken", "")).strip())
    target = _resolve_env(str(push.get("target", "")).strip())
    if target.startswith("topic:"):
        target_type = "topicIds"
        try:
            target_value = int(target.split(":", 1)[1])
        except ValueError:
            print(f"  [push] topic 主题 ID 不是数字: {target!r}，本次不推送")
            target_type, target_value = "uids", []
    else:
        target_type = "uids"
        target_value = [target] if target else []
    return {"token": token, "target_type": target_type, "target_value": target_value}


def safe_url(url) -> str:
    """只放行 http/https 链接，其余（javascript:、data: 等）一律丢弃。"""
    u = str(url or "").strip()
    return u if u.lower().startswith(_SAFE_SCHEMES) else ""


def send_wxpusher(cfg: dict, summary: str, html_content: str) -> bool:
    """发送 HTML 卡片消息，返回是否成功。"""
    token = cfg.get("token", "")
    if not token or not cfg.get("target_value"):
        return False
    payload = {
        "appToken": token,
        "summary": summary[:500],
        "content": html_content,
        "contentType": 2,  # 2 = HTML
        cfg["target_type"]: cfg["target_value"],
    }
    try:
        resp = requests.post(WXPUSHER_API, json=payload, timeout=15)
        try:
            data = resp.json()
        except ValueError:
            print(f"  [push] 返回非 JSON: HTTP {resp.status_code} {resp.text[:120]}")
            return False
        ok = data.get("code") == 1000
        if not ok:
            print(f"  [push] WxPusher 返回异常: code={data.get('code')} "
                  f"msg={data.get('msg')}")
        return ok
    except Exception as exc:  # noqa: BLE001
        print(f"  [push] 请求失败: {exc}")
        return False


_BADGES = {
    "new": ("新发布", "#10b981"),
    "updated": ("内容更新", "#f59e0b"),
}


def build_message(new_items: list[dict], limit: int = 20) -> str:
    """把新增条目拼成 HTML 消息。new_items 元素含 source/title/url/date/kind。"""
    lines = ["<h4>软件站有更新啦</h4>"]
    for it in new_items[:limit]:
        title = escape(str(it.get("title", "")))
        url = safe_url(it.get("url", ""))
        date = escape(str(it.get("date", "")))
        source = escape(str(it.get("source", "")))
        badge = ""
        label = _BADGES.get(it.get("kind", ""))
        if label:
            text, color = label
            badge = (f"<span style='background:{color};color:#fff;"
                     f"font-size:11px;padding:1px 6px;border-radius:4px'>{text}</span> ")
        date_txt = f"（{date}）" if date else ""
        source_txt = (f"<span style='color:#888888;font-size:12px'>{source}</span>"
                      if source else "")
        link_txt = f"<a href='{escape(url, quote=True)}'>{escape(url)}</a><br/>" if url else ""
        lines.append(f"<p>{badge}<b>{title}</b>{date_txt}<br/>{link_txt}{source_txt}</p>")
    if len(new_items) > limit:
        lines.append(f"<p style='color:#888888'>……另有 {len(new_items) - limit} 条，"
                     f"共 {len(new_items)} 条新增</p>")
    return "".join(lines)


def _resolve_env(value: str) -> str:
    """${VAR_NAME} -> 环境变量值（不存在则返回空串）。"""
    if value.startswith("${") and value.endswith("}") and len(value) > 3:
        return os.environ.get(value[2:-1], "")
    return value
