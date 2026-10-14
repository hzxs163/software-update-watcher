# 软件更新监控（Software Update Watcher）

定时监控多个软件站，检测到新发布/更新时通过 **WxPusher 推送微信提醒**。
内置可视化配置台（GitHub Pages）：聚合搜索各站软件、设置关注关键词、手动检查、把关键词一键写回仓库。

- 默认每 **6 小时** 由 GitHub Actions 自动检查一次（也可手动触发）
- 首次运行只建立基线、不推送；之后发现**命中关注关键词**的新条目才推送
- 更新检测为**双维度**：出现新详情页 URL = 新发布；同一 URL 标题发生变化（如版本号更新）= 内容更新
- **抓取失败不再误报**：某个源本轮抓不到时，旧基线原样保留、只累加失败计数，
  绝不会被写成空基线（否则下一轮恢复时会把老条目当成「一堆新发布」推给你）
- **源失效会主动告警**：同一个源连续 4 轮抓不到 → 推一条「源已失效」提醒；恢复后再推一条「已恢复正常」。
  全部源都失败时 Actions 直接标红，不再「静默成功」
- 源的解析规则集中在 `src/config.json`，配置台启动时会读取它作为唯一真源（内置规则仅作兜底）

## 目录结构

```
.
├── .github/workflows/check-updates.yml   # 每6小时定时任务（含失败告警、job 摘要、30 分钟超时）
├── .github/workflows/ci.yml              # 代码改动跑 ruff + pytest
├── index.html                            # 配置台前端（GitHub Pages）
├── requirements.txt / requirements-dev.txt
├── tests/test_logic.py                   # 关键词/日期/索引/基线保护/转义 单测
├── src/
│   ├── config.json                       # 关键词 + 源 + 推送配置（核心配置，公开）
│   ├── last_check.json                   # 最近一次检查结果（Actions 自动写，供网页端展示）
│   ├── state.json                        # 运行状态：基线 seen + 失败计数（自动维护，勿手改）
│   ├── index/<源id>.json                  # 各源索引快照（前端搜索加速）
│   ├── crawler.py                        # 通用列表页爬虫（CSS 选择器驱动，多通道）
│   ├── notify.py                         # WxPusher 推送（输出统一做 HTML 转义）
│   └── main.py                           # 主入口
```

## 快速开始

1. **Fork 或克隆本仓库**，开启 GitHub Pages：
   Settings → Pages → Source 选择 `Deploy from a branch` → `main` → `/ (root)`。
   访问 `https://<你的用户名>.github.io/<仓库名>/` 即配置台。

2. **设置关注关键词**：配置台右上角「设置 → ① 关注关键词」填写要跟踪的软件名并保存。
   关键词卡片上的「手动检查」可即时预览是否有更新。

   **推荐：前端一键保存到仓库**（免去手动导出/提交）：
   在「设置 → ② GitHub 自动保存」填一次仓库 Owner / 仓库名 / GitHub 令牌
   （建议 Fine-grained PAT，仅授权本仓库 `Contents: Read and write`；
   要用「立即触发检查」再加 `Workflows: Read and write`），勾选「保存关键词后自动推送到仓库」。

   > **令牌不会被写进仓库。** 导出走显式白名单（只有 `keywords` / `exclude_keywords` /
   > `sources` / `push` 四个键），提交前还会再扫一遍内容，发现 `ghp_` / `github_pat_` / `AT_`
   > 这类令牌字样直接拒绝提交。令牌只留在你浏览器的 localStorage 里。

   备选（不填令牌时）：点击「导出 config.json」，将内容覆盖仓库中 `src/config.json` 并提交。

3. **配置推送密钥**（推荐，避免密钥入库）：
   Settings → Secrets and variables → Actions → New repository secret：
   - `WXPUSHER_APP_TOKEN`：WxPusher 应用令牌（`AT_` 开头）
   - `WXPUSHER_UID`：你的微信用户 UID；若要推送到主题，填 `topic:主题ID`

   > config.json 中的 `push` 默认写作 `${WXPUSHER_APP_TOKEN}` / `${WXPUSHER_UID}`，
   > 运行时自动从环境变量（即上述 Secrets）读取。

4. **验证**：到 Actions 页面手动触发 `check-updates`（workflow_dispatch），
   首次运行建立基线；再次触发若页面有更新，微信会收到推送。

## config.json 说明

```jsonc
{
  "keywords": ["CorelDRAW", "Bandicam"],   // 全局关注关键词：所有源统一过滤；[] 表示关注全部
  "exclude_keywords": ["盗版", "被抓"],     // 可选：标题含任一词则忽略（挡掉新闻帖、广告）
  "sources": [
    {
      "id": "x6d-green",                     // 唯一标识
      "name": "小刀娱乐网-绿色软件",           // 显示名称
      "list_url": "https://www.x6d.com/html/23.html",  // 列表页（按时间倒序）
      "item_selector": "ul.list-soft li.layui-clear",  // 列表页每条内容的容器
      "title_selector": "a.soft-title",       // 标题链接
      "date_selector": "div.list-ca",         // 日期元素
      "date_prefix": "时间：",                 // 日期文本前缀
      "max_items": 30,
      "enabled": true,
      "pagination_tpl": "https://www.x6d.com/html/23-{n}.html", // 分页模板（索引快照用）
      "full_pages": 356,                      // 全站分页总数（--full-index 用）
      "search_url": "https://www.x6d.com/daowangsousuo?q={keyword}", // 站内搜索（覆盖全站历史）
      "search_item_selector": "ul.list-ul li.layui-clear",           // 搜索页结构常与列表页不同
      "search_title_selector": "a.soft-title",
      "search_date_selector": "div.list-ca",
      "search_date_prefix": "发布时间："
    }
  ],
  "push": {
    "provider": "wxpusher",
    "appToken": "${WXPUSHER_APP_TOKEN}",      // 支持 ${环境变量} 模板
    "target": "${WXPUSHER_UID}"               // UID 或 topic:数字
  }
}
```

内置三个源：`x6d-green`（小刀娱乐网-绿色软件）、`423down`、`gndown`（绿软小站），
均已验证可选择器可用（站内搜索 + 列表页两套规则）。

### 新增一个源

1. 复制一条现有源，改 `id` / `name` / `list_url`。
2. 浏览器打开列表页按 `F12`，悬停任意一条内容：外层标签路径即 `item_selector`，
   条目内标题链接即 `title_selector`，日期元素即 `date_selector`（前缀填 `date_prefix`）。
3. 若该站有站内搜索页，再补 `search_url` 与 `search_*` 选择器；**监控会优先用站内搜索**，
   只配 `list_url` 的话只能看到列表页前 `max_items` 条，冷门关键词可能长期命中不到。
4. 提交后跑一次 `python src/main.py --source <新id>`，日志会显示解析条数；解析为 0 说明选择器要调。

## 命令行

```bash
python src/main.py                       # 常规检查
python src/main.py --source gndown       # 只跑某个源
python src/main.py --no-index            # 只做更新检测，不更新索引快照
python src/main.py --full-index          # 全量重建索引（可反复运行，每轮与已有索引增量合并）
python src/main.py --full-index --pages 120   # 本轮最多抓 120 页，分批跑完，避免撞超时
python src/main.py --workers 6           # 同站内分页/关键词并发数（默认 4）
```

## 工作原理

```
GitHub Actions（每6小时 / 手动 / 配置台按钮）
   └─ python src/main.py
        ├─ 读 config.json（关键词 + 源）与 state.json（历史基线、失败计数）
        ├─ 逐源抓取：有 search_url 走站内搜索（按关键词，覆盖全站历史），否则抓列表页
        │    └─ 多通道：直连 → 公共 CORS 代理 → Jina Reader；直连失败按【域名】隔离降级
        ├─ 按关键词过滤（支持排除词）
        ├─ 与历史基线合并对比 → 新增 URL = 新发布；同 URL 标题变化 = 内容更新
        ├─ 有新增 → WxPusher 推送 HTML 卡片；源连续失败/恢复 → 推送健康告警
        ├─ 抓取失败的源：保留旧基线，只累加 fail_streak
        └─ 写 state.json / last_check.json / index/*.json（Actions 自动提交，提交信息带 [skip ci]
            以减少无谓构建；网页端是运行时从 raw.githubusercontent 拉这些数据的，
            所以是否重建 Pages 都不影响展示）
```

- 基线（`state.json` 的 `seen`）按 URL 合并、只增不删，超过 `SEEN_LIMIT=100` 条时按日期淘汰最旧的。
  合并而非覆盖是修正误报的关键：某轮窗口变小或个别关键词失败，历史条目不会因此丢失。
- 关键词匹配不区分大小写，标题含任一关键词即命中；`keywords` 留空（`[]`）表示关注全部。
- 日期统一归一化为 `YYYY-MM-DD`。站点只显示「月-日」时按年份推断，并保证不会推成未来日期。
- 时间字段（`last_check.json` 的 `checked_at`）为**北京时间**，由代码内 `zoneinfo` 换算，
  不依赖 runner 的 `TZ`；同时附带 `checked_at_tz` 字段。

## 本地调试

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest            # 26 个单测，覆盖关键词/日期/索引/基线保护/转义
python -m ruff check src tests
python src/crawler.py 423down   # 用仓库真实规则抓一个源，打印前 10 条
WXPUSHER_APP_TOKEN=AT_xxx WXPUSHER_UID=your_uid python src/main.py   # 带推送运行
```

## 常见问题

- **首次运行推送了一堆？** 不会。首次运行只记录基线（日志 `[init]`），不推送。
  新加关键词后同理：第一轮建基线，之后才推。
- **为什么日志显示抓取 0 条？** 站点改版或选择器不对，检查 `item_selector`/`title_selector`。
- **Actions 全绿但其实源挂了？** 现在不会了。单源失败会在日志与 job 摘要里列出、
  连续 4 轮触发微信告警；所有源都失败时 job 直接标红。
- **Actions 日志报 502 / 抓取失败？** x6d 等站点会对数据中心 IP（GitHub Actions）临时封锁，
  返回 502/302 验证页。爬虫内置多通道：直连 → 公共代理（allorigins/codetabs/cors.eu.org）→ Jina Reader，
  直连失败自动切换，日志打印 `[fetch] 通道: xxx`。降级计数按域名隔离，
  某个站封 IP 不会连累其他站；被封的域名每 25 次调用自动探测一次以恢复直连。
- **网页端「手动检查」或聚合搜索失败？** 前端没有后端，靠公共 CORS 代理抓页面，代理是否可用看运气。
  搜索会同时查「仓库索引缓存」和「各站站内搜索」并合并去重；代理不可用时，
  该源退回只显示索引缓存里的条目，并列出「XX(实时) 抓取失败」。
  以仓库 Actions 的定时结果为准更可靠。
- **想换推送服务？** 目前内置 WxPusher；可在 `src/notify.py` 扩展其他 provider。
- **时区**：定时表达式为 UTC，`0 */6 * * *` 对应北京时间 02:00 / 08:00 / 14:00 / 20:00
  （GitHub 调度可能有几十分钟延迟）。展示时间均为北京时间。

## 免责声明

本项目仅用于个人学习与软件更新提醒，抓取内容版权归原网站所有；
请遵守目标网站的服务条款，控制抓取频率（默认 6 小时一次）。
