# -*- coding: utf-8 -*-
"""核心逻辑单测：关键词/日期/索引/基线保护/推送内容转义。

这些正是历史上出过问题的地方，改坏会立刻导致「误报一堆新发布」或「密钥进仓库」。
"""
import json
from pathlib import Path

import pytest

import crawler
import main as suw
import notify


# ---------- 关键词匹配 ----------
def test_match_keywords_case_insensitive():
    assert crawler.match_keywords("CorelDRAW 2026 v27", ["coreldraw"])
    assert not crawler.match_keywords("Photoshop 2026", ["coreldraw"])


def test_match_keywords_empty_means_match_all():
    assert crawler.match_keywords("随便什么都算", [])


def test_match_keywords_exclude_wins():
    """排除词用于挡掉「网友卖CorelDraw盗版软件」这类新闻帖。"""
    t = "网友卖CorelDraw盗版软件，睿派克站长被抓"
    assert crawler.match_keywords(t, ["CorelDRAW"])
    assert not crawler.match_keywords(t, ["CorelDRAW"], ["盗版软件", "被抓"])


# ---------- 日期归一化 ----------
@pytest.mark.parametrize("raw,expect", [
    ("2026-09-25", "2026-09-25"),
    ("2026/9/5", "2026-09-05"),
    ("2026年09月25日", "2026-09-25"),
    ("时间：2026-09-25 10:00", "2026-09-25"),
    ("2026-09-25T10:00:00+08:00", "2026-09-25"),
    ("09-20", "2019-09-20"),
    ("", ""),
    ("暂无", ""),
])
def test_normalize_date(raw, expect):
    assert crawler.normalize_date(raw, fallback_year=2019) == expect


def test_sort_items_puts_missing_date_last():
    items = [{"date": "", "title": "b"}, {"date": "2026-01-01", "title": "a"},
             {"date": "2025-12-31", "title": "c"}]
    assert [x["title"] for x in suw.sort_items(items)] == ["a", "c", "b"]


def test_month_day_year_inference_never_lands_in_the_future(monkeypatch):
    """站点只写「月-日」时年份靠推断，但推断结果不能是未来。

    例：今天是 2026-03-05，看到 11-19 只能是去年的 2025-11-19。
    """
    import datetime as _dt

    class FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 3, 5)

    monkeypatch.setattr(crawler, "datetime", FakeDT)
    assert crawler.normalize_date("11-19") == "2025-11-19"
    assert crawler.normalize_date("02-28") == "2026-02-28"
    # 标题里的版本年份优先（CorelDRAW 2019 那条按 2019 年算），未来则退回上一年
    assert crawler.normalize_date("11-19", fallback_year=2019) == "2019-11-19"


# ---------- 分页 URL 去重（423down 的 list_url 与 n=2 完全重复）----------
def test_pagination_urls_no_duplicate_first_page():
    src = {"list_url": "https://www.423down.com/page/2",
           "pagination_tpl": "https://www.423down.com/page/{n}"}
    urls = crawler.pagination_urls(src, 3)
    assert urls == ["https://www.423down.com/page/2", "https://www.423down.com/page/3"]


# ---------- 基线合并 ----------
def test_merge_seen_keeps_history_and_updates_title():
    old = {"u1": "CorelDRAW 2026 v27.1", "u2": "旧条目"}
    matched = [{"key": "u1", "title": "CorelDRAW 2026 v27.2", "date": "2026-10-01"}]
    merged = suw.merge_seen(old, matched)
    assert merged["u1"] == "CorelDRAW 2026 v27.2"
    assert "u2" in merged, "本轮没出现的条目必须留在基线里，否则下轮会被当成新发布"


def test_merge_seen_trims_to_limit_by_date():
    old = {f"u{i}": f"t{i}" for i in range(120)}
    matched = [{"key": f"n{i}", "title": f"new{i}", "date": f"2026-10-{i:02d}"}
               for i in range(1, 11)]
    merged = suw.merge_seen(old, matched, limit=100)
    assert len(merged) == 100
    assert all(f"n{i}" in merged for i in range(1, 11)), "本轮新条目应当保留"


# ---------- 索引快照 ----------
def test_update_index_merges_and_skips_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(suw, "INDEX_DIR", tmp_path)
    src = {"id": "demo"}
    items = [{"title": "B", "url": "https://x/2", "date": "2026-01-02"},
             {"title": "A", "url": "https://x/1", "date": "2026-01-01"}]
    assert suw.update_index(src, items) is True
    again = json.loads((tmp_path / "demo.json").read_text(encoding="utf-8"))["items"]
    assert [i["url"] for i in again] == ["https://x/2", "https://x/1"]
    assert suw.update_index(src, items) is False, "内容无变化不应重复写文件"


# ---------- 最关键的一条：抓取失败不得清空基线 ----------
@pytest.fixture
def env(tmp_path, monkeypatch):
    """搭一个隔离的运行环境，并用假爬虫替代真实网络。"""
    cfg = {
        "keywords": ["CorelDRAW"],
        "sources": [{"id": "demo", "name": "Demo站", "list_url": "https://demo/list",
                     "search_url": "https://demo/search/{keyword}",
                     "item_selector": "li", "title_selector": "a", "enabled": True}],
        "push": {"provider": "wxpusher", "appToken": "", "target": ""},
    }
    (tmp_path / "config.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                          encoding="utf-8")
    monkeypatch.setattr(suw, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(suw, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(suw, "LAST_CHECK_PATH", tmp_path / "last_check.json")
    monkeypatch.setattr(suw, "INDEX_DIR", tmp_path / "index")
    (tmp_path / "index").mkdir()
    good = [{"key": "https://demo/1", "title": "CorelDRAW 2026 v27",
             "url": "https://demo/1", "date": "2026-10-01"},
            {"key": "https://demo/2", "title": "CorelDRAW 2025 v26",
             "url": "https://demo/2", "date": "2025-09-01"}]
    monkeypatch.setattr(suw, "parse_source_pages", lambda s, n=3, w=4: list(good))
    return {"cfg": cfg, "good": good, "tmp": tmp_path, "monkeypatch": monkeypatch}


def _state(tmp):
    return json.loads((tmp / "state.json").read_text(encoding="utf-8"))


def _last(tmp):
    return json.loads((tmp / "last_check.json").read_text(encoding="utf-8"))


def test_first_run_builds_baseline_without_push(env):
    env["monkeypatch"].setattr(suw, "parse_keyword_search",
                               lambda s, k, w=4: list(env["good"]))
    assert suw.main([]) == 0
    assert len(_state(env["tmp"])["sources"]["demo"]["seen"]) == 2
    assert _last(env["tmp"])["new_total"] == 0


def test_failure_keeps_baseline_and_exits_one(env):
    """第 1 轮建基线 → 第 2 轮全失败：基线必须原样保留，且退出码为 1。"""
    env["monkeypatch"].setattr(suw, "parse_keyword_search",
                               lambda s, k, w=4: list(env["good"]))
    suw.main([])

    def boom(*a, **kw):
        raise RuntimeError("所有抓取通道均失败: 直连: HTTP 502")
    env["monkeypatch"].setattr(suw, "parse_keyword_search", boom)
    assert suw.main([]) == 1
    st = _state(env["tmp"])["sources"]["demo"]
    assert len(st["seen"]) == 2, "抓取失败绝不能把基线写成空"
    assert st["fail_streak"] == 1
    assert _last(env["tmp"])["all_sources_failed"] is True


def test_recovery_after_failure_does_not_report_false_new(env):
    """失败一轮后再恢复：老条目不应被重新报成「新发布」（这正是原来的误报路径）。"""
    env["monkeypatch"].setattr(suw, "parse_keyword_search",
                               lambda s, k, w=4: list(env["good"]))
    suw.main([])

    def boom(*a, **kw):
        raise RuntimeError("代理全部不可用")
    env["monkeypatch"].setattr(suw, "parse_keyword_search", boom)
    suw.main([])
    env["monkeypatch"].setattr(suw, "parse_keyword_search",
                               lambda s, k, w=4: list(env["good"]))
    assert suw.main([]) == 0
    assert _last(env["tmp"])["new_total"] == 0, "恢复后不应出现误报"
    assert _state(env["tmp"])["sources"]["demo"]["fail_streak"] == 0


def test_title_change_is_reported_as_updated(env):
    env["monkeypatch"].setattr(suw, "parse_keyword_search",
                               lambda s, k, w=4: list(env["good"]))
    suw.main([])
    changed = [dict(env["good"][0], title="CorelDRAW 2026 v27.2"), env["good"][1]]
    env["monkeypatch"].setattr(suw, "parse_keyword_search",
                               lambda s, k, w=4: list(changed))
    suw.main([])
    lc = _last(env["tmp"])
    assert lc["new_total"] == 1 and "1 条内容更新" in lc["message"]


def test_health_alert_fires_once_per_episode(env):
    """连续失败到阈值推一次告警；再次恢复推一条恢复通知；期间不重复轰炸。"""
    sent = []
    env["monkeypatch"].setattr(suw, "resolve_push_config",
                               lambda c: {"token": "AT_x", "target_type": "uids",
                                          "target_value": ["u"]})
    env["monkeypatch"].setattr(suw, "send_wxpusher",
                               lambda cfg, summary, content: (sent.append(summary), True)[1])
    env["monkeypatch"].setattr(suw, "parse_keyword_search",
                               lambda s, k, w=4: list(env["good"]))
    suw.main([])

    def boom(*a, **kw):
        raise RuntimeError("挂了")
    env["monkeypatch"].setattr(suw, "parse_keyword_search", boom)
    for _ in range(suw.FAIL_ALERT_STREAK + 2):
        suw.main([])
    assert sum("源健康告警" in s for s in sent) == 1, "达到阈值告警一次，之后不重复轰炸"

    env["monkeypatch"].setattr(suw, "parse_keyword_search",
                               lambda s, k, w=4: list(env["good"]))
    suw.main([])
    assert sum("源健康告警" in s for s in sent) == 2, "恢复时应补一条恢复通知"


# ---------- 推送内容安全 ----------
def test_build_message_escapes_title_and_blocks_bad_scheme():
    items = [{"title": "危险 <script>x</script> 标题", "url": "javascript:alert(1)",
              "date": "", "source": "s'", "kind": "new"}]
    html = notify.build_message(items)
    assert "<script>x</script>" not in html
    assert "javascript:alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_resolve_push_config_bad_topic_is_safe():
    cfg = notify.resolve_push_config(
        {"push": {"appToken": "AT_1", "target": "topic:abc"}})
    assert cfg["target_value"] == []
    assert notify.send_wxpusher(cfg, "s", "c") is False


def test_parse_markdown_items_skips_assets_and_strips_md_title():
    """Jina/markdown 通道实测会给出配图链接和 [标题](URL "尾部标题")，
    直接收进基线会产生 *.png 与带引号的脏 key。"""
    md = (
        '![Image 1: CorelDRAW Graphics Suite 2026](https://x.com/wp/a.png "cover")\n'
        '[CorelDRAW 2026 (v27.2.0.135) 中文直装版](https://x.com/13364.html "详情")\n'
        '[样式](https://x.com/theme.css)\n'
        '[锚点](#top)\n'
    )
    items = crawler._parse_markdown_items(md, "https://x.com/list", 30)
    assert [i["url"] for i in items] == ["https://x.com/13364.html"]
    assert items[0]["key"] == "https://x.com/13364.html"
    assert items[0]["title"].startswith("CorelDRAW 2026")


# ---------- 仓库里的真实 config.json 形状检查（防手改打错） ----------
def _repo_config():
    return json.loads((Path(__file__).resolve().parents[1] / "src" /
                       "config.json").read_text(encoding="utf-8"))


def test_repo_config_shape():
    cfg = _repo_config()
    ids = [s["id"] for s in cfg["sources"]]
    assert len(ids) == len(set(ids)), "源 id 必须唯一"
    for s in cfg["sources"]:
        assert s["list_url"].startswith("https://")
        assert s.get("item_selector") and s.get("title_selector")
        if s.get("search_url"):
            assert "{keyword}" in s["search_url"]
            assert s["search_url"].startswith("https://")


def test_repo_config_contains_no_secret():
    """配置文件是公开的，绝不能混进令牌（导出逻辑已白名单，这里是第二道锁）。"""
    import re
    text = json.dumps(_repo_config(), ensure_ascii=False)
    assert not re.search(r"gh[pousr]_\w{6,}|github_pat_\w{6,}|AT_\w{6,}", text), \
        "src/config.json 里出现了疑似令牌"
    assert "github" not in _repo_config(), "导出内容不应包含前端本地设置"
