# -*- coding: utf-8 -*-
"""推送通知模块：WxPusher（微信推送）。

接口: POST https://wxpusher.zjiecode.com/api/send/message
推送配置支持 ${ENV_VAR} 模板，从环境变量读取，避免把密钥提交进仓库。
"""
import os

import requests

WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"


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
        target_value = int(target.split(":", 1)[1])
    else:
        target_type = "uids"
        target_value = [target]
    return {"token": token, "target_type": target_type, "target_value": target_value}


def send_wxpusher(cfg: dict, summary: str, html_content: str) -> bool:
    """发送 HTML 卡片消息，返回是否成功。"""
    token = cfg.get("token", "")
    if not token:
        return False
    payload = {
        "appToken": token,
        "summary": summary,
        "content": html_content,
        "contentType": 2,  # 2 = HTML
        cfg["target_type"]: cfg["target_value"],
    }
    try:
        resp = requests.post(WXPUSHER_API, json=payload, timeout=15)
        data = resp.json()
        ok = data.get("code") == 1000
        if not ok:
            print(f"  [push] WxPusher 返回异常: code={data.get('code')} msg={data.get('msg')}")
        return ok
    except Exception as exc:  # noqa: BLE001
        print(f"  [push] 请求失败: {exc}")
        return False


def build_message(new_items: list[dict]) -> str:
    """把新增条目拼成 HTML 消息。new_items 元素含 source/title/url/date。"""
    lines = ["<h4>软件站有更新啦</h4>"]
    for it in new_items[:20]:
        title = it["title"]
        url = it.get("url", "")
        date = it.get("date", "")
        source = it.get("source", "")
        date_txt = f"（{date}）" if date else ""
        source_txt = f"<span style='color:#888888;font-size:12px'>{source}</span>" if source else ""
        if url:
            lines.append(f"<p><b>{title}</b>{date_txt}<br/>"
                         f"<a href='{url}'>{url}</a><br/>{source_txt}</p>")
        else:
            lines.append(f"<p><b>{title}</b>{date_txt}<br/>{source_txt}</p>")
    if len(new_items) > 20:
        lines.append(f"<p style='color:#888888'>……另有 {len(new_items) - 20} 条，共 {len(new_items)} 条新增</p>")
    return "".join(lines)


def _resolve_env(value: str) -> str:
    """${VAR_NAME} -> 环境变量值（不存在则返回空串）。"""
    if value.startswith("${") and value.endswith("}") and len(value) > 3:
        return os.environ.get(value[2:-1], "")
    return value
