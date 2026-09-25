# 软件更新监控（Software Update Watcher）

定时监控多个软件站列表页，检测到新发布/更新时通过 **WxPusher 推送微信提醒**。
内置可视化配置台（GitHub Pages），无需改代码即可增删关注的软件站源。

- 默认每 **6 小时** 由 GitHub Actions 自动检查一次（也可手动触发）
- 首次运行只建立基线、不推送；之后每次发现新条目才推送
- 推送失败或未配置密钥不会影响检查，日志中可见

## 目录结构

```
.
├── .github/workflows/check-updates.yml   # 每6小时定时任务
├── index.html                            # 配置台前端（GitHub Pages）
├── requirements.txt
├── src/
│   ├── config.json                       # 关注的源 + 推送配置（核心配置）
│   ├── state.json                        # 运行状态（自动维护，勿手改）
│   ├── crawler.py                        # 通用列表页爬虫（CSS 选择器驱动）
│   ├── notify.py                         # WxPusher 推送
│   └── main.py                           # 主入口
```

## 快速开始

1. **Fork 或克隆本仓库**，开启 GitHub Pages：
   Settings → Pages → Source 选择 `Deploy from a branch` → `main` → `/ (root)`。
   访问 `https://<你的用户名>.github.io/<仓库名>/` 即配置台。

2. **配置关注的源与推送**：在配置台页面添加/编辑源、填写 WxPusher 信息，
   点击「导出 config.json」，将内容覆盖仓库中 `src/config.json` 并提交。

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
  "sources": [
    {
      "id": "x6d-green",                     // 唯一标识
      "name": "小刀娱乐网-绿色软件",           // 显示名称
      "list_url": "https://www.x6d.com/html/23.html", // 列表页（按时间倒序）
      "item_selector": "ul.list-soft li.layui-clear", // 每条内容的容器选择器
      "title_selector": "a.soft-title",       // 标题链接选择器（取文本+href）
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

### 添加新源（如何找选择器）

以任意按时间倒序的软件列表页为例：

1. 浏览器打开列表页，按 `F12` 打开开发者工具。
2. 鼠标悬停任意一条内容，在 Elements 面板定位它的外层标签（`<li>`、`<div>` 等），
   该路径即 `item_selector`，如 `ul.list-soft li`。
3. 条目里的标题链接对应的选择器即 `title_selector`，如 `a.soft-title`。
4. 日期所在元素选择器为 `date_selector`，日期前的固定文字填 `date_prefix`。
5. 在配置台添加并导出，或直接编辑 `src/config.json` 提交。

> 抓取依赖目标站点的 HTML 结构，站点改版后需同步更新选择器（日志会提示解析为空）。

## 工作原理

```
GitHub Actions（每6小时 / 手动）
   └─ python src/main.py
        ├─ 读取 config.json（源列表）与 state.json（上次关键字）
        ├─ 逐源抓取列表页 → 按 CSS 选择器解析条目（标题/链接/日期）
        ├─ 与上次关键字对比 → 找出新增条目
        ├─ 有新增 → WxPusher 推送 HTML 卡片到微信
        └─ 关键字集合变化时写回 state.json（避免无效提交）
```

- 新增判定：条目详情页地址（或 标题|日期）不在上次记录中即为新增。
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
- **想换推送服务？** 目前内置 WxPusher；可在 `src/notify.py` 扩展其他 provider。
- **时区**：定时表达式为 UTC，`0 */6 * * *` 对应北京时间 02:00 / 08:00 / 14:00 / 20:00。

## 免责声明

本项目仅用于个人学习与软件更新提醒，抓取内容版权归原网站所有；
请遵守目标网站的服务条款，控制抓取频率（默认 6 小时一次）。
