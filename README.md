# 软件更新监控（Software Update Watcher）

定时监控多个软件站列表页，检测到新发布/更新时通过 **WxPusher 推送微信提醒**。
内置可视化配置台（GitHub Pages）：首页设置关注关键词生成卡片，可一键「手动检查」当前是否有更新；
源规则与推送配置收在右上角「设置」中。

- 默认每 **6 小时** 由 GitHub Actions 自动检查一次（也可手动触发）
- 首次运行只建立基线、不推送；之后发现**命中关注关键词**的新条目才推送
- 更新检测为**双维度**：出现新详情页 URL = 新发布；同一 URL 标题发生变化（如版本号更新）= 内容更新，
  两者都会推送提醒（前端「手动检查」逻辑一致）
- **关键词在配置台首页全局设置**（如 `CorelDRAW`），所有源统一按同一组关键词过滤；
  留空则关注全部更新
- 配置台支持**前端一键保存到仓库**：在「设置 → ① GitHub 自动保存」填一次令牌后，
  首页保存关键词即自动写入仓库 `src/config.json`，无需手动导出/提交
- 源的解析规则已内置写死（预置「小刀娱乐网-绿色软件」），后续新增源在 `src/config.json` 中陆续添加
- 推送失败或未配置密钥不会影响检查，日志中可见

## 目录结构

```
.
├── .github/workflows/check-updates.yml   # 每6小时定时任务
├── index.html                            # 配置台前端（GitHub Pages）
├── requirements.txt
├── src/
│   ├── config.json                       # 关键词 + 源 + 推送配置（核心配置）
│   ├── last_check.json                   # 最近一次检查结果（Actions 自动写，供网页端展示）
│   ├── state.json                        # 运行状态（自动维护，勿手改）
│   ├── crawler.py                        # 通用列表页爬虫（CSS 选择器驱动）
│   ├── notify.py                         # WxPusher 推送
│   └── main.py                           # 主入口
```

## 快速开始

1. **Fork 或克隆本仓库**，开启 GitHub Pages：
   Settings → Pages → Source 选择 `Deploy from a branch` → `main` → `/ (root)`。
   访问 `https://<你的用户名>.github.io/<仓库名>/` 即配置台。

2. **设置关注关键词与推送**：在配置台首页「关注关键词」填写要跟踪的软件名并保存（生成卡片，
   卡片上可点「手动检查」即时预览是否有更新）；右上角「设置」里填写 WxPusher 信息。

   **推荐：前端一键保存到仓库**（免去手动导出/提交）：
   在「设置 → ① GitHub 自动保存」填入仓库 Owner / 仓库名 / GitHub 令牌（建议 Fine-grained PAT，
   仅授权本仓库 `Contents: Read and write`），勾选「保存关键词后自动推送到仓库」。
   之后首页每次保存关键词都会自动写入仓库 `src/config.json`，下次定时检查即生效；
   还可点「立即触发检查」马上验证（该按钮需令牌含 `Workflows: Read and write` 权限）。

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
  "keywords": ["CorelDRAW", "Bandicam"],      // 全局关注关键词：所有源统一过滤；[] 表示关注全部
  "sources": [
    {
      "id": "x6d-green",                     // 唯一标识
      "name": "小刀娱乐网-绿色软件",           // 显示名称
      "list_url": "https://www.x6d.com/html/23.html", // 列表页（按时间倒序）
      "item_selector": "ul.list-soft li.layui-clear", // 每条内容的容器选择器（已写死，无需改）
      "title_selector": "a.soft-title",       // 标题链接选择器（已写死）
      "date_selector": "div.list-ca",         // 日期元素选择器（可选）
      "date_prefix": "时间：",                 // 日期文本前缀（可选，自动剥离）
      "max_items": 30,                        // 每次最多解析条数
      "enabled": true
    }
  ],
  "push": {
    "provider": "wxpusher",
    "appToken": "${WXPUSHER_APP_TOKEN}",      // 支持 ${环境变量} 模板
    "target": "${WXPUSHER_UID}"               // UID 或 topic:数字
  }
}
```

### 新增源（规则写死，后续陆续添加）

源的解析规则（选择器）已在仓库中写死，日常只需在配置台首页改关键词。新增源时在
`src/config.json` 的 `sources` 数组追加一条即可，字段见上文示例：

1. 复制一条现有源，改 `id`、`name`、`list_url`（新站的列表页地址）。
2. 不同网站 HTML 结构不同，需按页面调整 `item_selector` / `title_selector`：
   - 浏览器打开列表页按 `F12`，悬停任意一条内容，定位外层标签路径即 `item_selector`；
   - 条目内标题链接的选择器即 `title_selector`；日期元素选 `date_selector`、前缀填 `date_prefix`。
3. 保存并提交；下次运行日志会显示抓取数量，解析为空时说明选择器需要调整。

> 拿不准选择器时，直接告知豆包「站名 + 列表页地址」，豆包帮你写死配置。
> 抓取依赖目标站点的 HTML 结构，站点改版后需同步更新选择器（日志会提示解析为空）。

## 工作原理

```
GitHub Actions（每6小时 / 手动）
   └─ python src/main.py
        ├─ 读取 config.json（全局关键词 + 源列表）与 state.json（上次关键字）
        ├─ 逐源抓取列表页 → 按写死的选择器解析条目（标题/链接/日期）
        ├─ 按全局关键词过滤（如 CorelDRAW；无关键词则全部保留）
        ├─ 与上次关键字对比 → 找出命中关键词的新增条目
        ├─ 有新增 → WxPusher 推送 HTML 卡片到微信
        └─ 关键字集合变化时写回 state.json（避免无效提交）
```

- 新增判定：命中关键词的条目中，详情页地址（或 标题|日期）不在上次记录中即为新增。
- 关键词匹配不区分大小写，标题含任一关键词即命中；`keywords` 留空（`[]`）表示关注全部。
- 每个源最多保留最近 100 条关键字；长期不运行导致条目滑出记录时，可能重复提醒，属正常。
- 运行日志可在 Actions 中查看（含抓取数量、新增标题、推送结果）。

## 本地调试

```bash
pip install -r requirements.txt
python src/main.py                       # 首次运行建立基线
python src/main.py                       # 再跑一次看是否有新增
WXPUSHER_APP_TOKEN=AT_xxx WXPUSHER_UID=your_uid python src/main.py   # 带推送运行
```

## 常见问题

- **首次运行推送了一堆？** 不会。首次运行只记录基线（日志 `[init]`），不推送。
- **为什么日志显示抓取 0 条？** 站点改版或选择器不对，检查 `item_selector`/`title_selector`。
- **Actions 日志报 502 / 抓取失败？** x6d 等站点可能对数据中心 IP（GitHub Actions）临时封锁，
  返回 502/302 验证页。爬虫已内置多通道：**直连 → 公共代理（allorigins/codetabs/cors.eu.org/thingproxy）→ Jina Reader**，
  直连失败会自动切换，日志会打印 `[fetch] 通道: xxx` 便于排查；若全部通道失败，下次运行会自动重试。
- **网页端「手动检查」失败？** 手动检查是纯前端经公共 CORS 代理抓取列表页实现的即时预览。
  代理不可用时，页面会自动降级展示仓库最近一次 Actions 检查结果（`src/last_check.json`）；
  两者都不可用时再稍后重试，或以仓库 Actions 的定时结果为准。
- **想换推送服务？** 目前内置 WxPusher；可在 `src/notify.py` 扩展其他 provider。
- **时区**：定时表达式为 UTC，`0 */6 * * *` 对应北京时间 02:00 / 08:00 / 14:00 / 20:00。

## 免责声明

本项目仅用于个人学习与软件更新提醒，抓取内容版权归原网站所有；
请遵守目标网站的服务条款，控制抓取频率（默认 6 小时一次）。
