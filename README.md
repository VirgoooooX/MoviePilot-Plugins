# MoviePilot Plugins

个人维护的 MoviePilot V2 插件仓库。

- 维护者：[VirgoooooX](https://github.com/VirgoooooX)
- 源码仓库：[VirgoooooX/MoviePilot-Plugins](https://github.com/VirgoooooX/MoviePilot-Plugins)

## 插件列表

### Emby中文角色同步（EmbyChineseRoleSync）

同步豆瓣中文演员姓名与角色信息到 Emby。

> 原作者：xiaoQQya · 维护者：[VirgoooooX](https://github.com/VirgoooooX) · [源码仓库](https://github.com/VirgoooooX/MoviePilot-Plugins)

#### 主要功能

- **豆瓣中文角色同步**：将豆瓣中文演员与角色名匹配同步到 Emby 电影、电视剧、季度及单集。
- **人像匹配与防污染**：仅更新 Emby 已有 Person 的中文姓名与豆瓣角色，不再补充豆瓣独有演员，避免污染 Emby 人员库。
- **智能保留与预演演示**：当豆瓣角色标注为通用“演员”时自动保留 TMDB 原有具体角色名；支持身份冲突检测与只读预演（Dry Run）能力。
- **媒体库白名单**：支持选择生效的顶级媒体库，自动过滤不符合白名单的影视作品。
- **精准检索与批量同步**：在设置页按剧名或 Item ID 实时检索作品，下拉框多选勾选后一键批量同步。
- **数据仪表盘与海报墙**：提供独立插件详情页面，展示运行状态、统计指标、快捷控制及 2:3 竖版比例（每行 6 张）的演职员海报墙。
- **单集同步与刷新策略**：支持将演职人员同步至每集，可配置覆盖已有演职人员及同步后非递归刷新单集元数据。
- **队列解耦与并发防重**：Webhook 实时入库与定时扫描统一进入后台队列，配合去重缓存与锁机制避免重复处理。
- **便捷控制与 REST API**：提供全量同步、精准同步、清去重缓存与清历史记录等 API 及快捷按钮。

#### 配置说明

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 启用 Emby 中文角色同步服务 |
| 清除缓存后运行 | 清空插件记录的单集去重缓存 |
| 立即运行一次 | 保存配置后立即触发一次后台全量同步 |
| 媒体服务器 | 选择要生效的 Emby 媒体服务器实例 |
| 生效媒体库 | 选择允许同步的顶级媒体库（留空表示对所有库生效） |
| 最新入库天数 | 定时扫描的时间范围（天） |
| 执行周期 | 定时扫描 Cron 表达式（默认 `0 6 * * *`） |
| 同步每集演职人员 | 将电视剧/季度的演员角色同步到各单集 |
| 同步后刷新单集 | 写入演职人员后调用 Emby 单集刷新 |
| 覆盖单集已有演职人员 | 是否覆盖单集现有演职人员列表 |
| 指定影视剧检索同步 | 输入剧名或 Item ID 实时检索作品，勾选后点击【同步所选媒体】 |

#### 当前版本

`v1.0.1`

### 天空监控（HdskyMonitor）

监控天空剧集资源并自动下载。

> 维护者：[VirgoooooX](https://github.com/VirgoooooX) · [源码仓库](https://github.com/VirgoooooX/MoviePilot-Plugins)

#### 主要功能

- **智能全集匹配与过滤**：定时扫描天空站点资源，排除单集，仅匹配全集、完结或 Complete 资源。
- **TMDB 识别与美化通知**：下载前结合标题、描述与年份自动识别 TMDB，提取标准中文剧名与横版海报封面发送通知。
- **媒体库重复检查**：下载前自动检查本地媒体库完整度，已存在完整媒体时自动跳过下载（记录为 `已存在` 状态），避免重复入库。
- **自定义下载路径与下载器**：支持配置独立保存目录（如 SSD 目录）以及指定具体下载器（如 QB-NAS、TR-NAS 等）。
- **响应式控制面板与海报墙**：提供运行总览、2:3 竖版历史匹配海报墙（调整为每行 6 张，支持显示已下载/已匹配/已存在/失败等状态）、日志倒序展示与一键控制。
- **去重防重与测试模式**：种子 ID 原子写入去重，支持无损测试模式（仅输出匹配结果，不写入历史、不提交下载、不发通知）。

#### 安装

在 MoviePilot 的插件市场设置中添加仓库：

```text
https://github.com/VirgoooooX/MoviePilot-Plugins
```

或者使用 V2 市场索引：

```text
https://raw.githubusercontent.com/VirgoooooX/MoviePilot-Plugins/main/package.v2.json
```

刷新插件市场后，搜索并安装相应插件。

#### 使用前提

1. 已在 MoviePilot 中配置并启用天空站点。
2. 天空站点 Cookie 有效。
3. 已配置可用下载器。
4. 如需通知，已在 MoviePilot 中配置通知渠道，并开启“插件”通知类型。

#### 配置说明

| 配置项 | 说明 |
| --- | --- |
| 启用天空监控 | 控制定时监控服务是否运行 |
| Cron 表达式 | 定时执行周期，例如 `0 */6 * * *` 表示每 6 小时执行一次 |
| 发布时间范围 | 仅处理最近指定天数发布的资源 |
| 最大扫描页数 | 每次运行允许扫描的最大页数 |
| 下载路径 | 自定义保存目录（如 `/downloadssd/local/`，需在已配置下载目录内） |
| 下载器 | 指定具体下载器（如 QB-NAS、TR-NAS 等，留空跟随站点或默认） |
| 单次下载上限 | 每次最多提交的资源数量，`0` 表示不限制 |
| 下载成功通知 | 仅在真正成功提交下载后发送通知 |
| 下载前检查媒体库 | 媒体库已存在完整媒体时自动跳过下载，避免重复入库 |

#### 测试模式

测试运行只扫描并输出匹配结果：

- 不提交下载
- 不发送通知
- 不写入历史记录
- 不加入已处理去重记录

#### 远程命令

```text
/hdsky_run    立即执行天空监控
/hdsky_test   测试模式运行
/hdsky_clear  清除已处理去重记录
```

#### 数据说明

- 海报墙历史使用 MoviePilot 插件数据存储。
- `hdsky_monitor_state.json` 是本地运行时去重数据，不会发布到仓库。
- 插件不会在代码中保存站点 Cookie、MoviePilot Token 或通知凭据。

#### 当前版本

`v1.0.1`

### 媒体库服务器通知增强版（MediaServerMsgLocal）

聚合发送媒体服务器播放与入库通知。

> 原作者：jxxghp · 维护者：[VirgoooooX](https://github.com/VirgoooooX) · [源码仓库](https://github.com/VirgoooooX/MoviePilot-Plugins)

#### 主要功能

- **多媒体服务器通知**：支持 Emby、Jellyfin、Plex 等媒体服务器的各种 Webhook 事件通知。
- **原始条目精准去重**：根据媒体服务器原始条目 ID 精准去重，防止重复触发入库提醒。
- **电视剧入库智能聚合**：针对剧集连续入库事件，每次新增动态刷新静默窗口，连续无新事件后仅汇总发送一次通知。
- **重载安全防护**：优化聚合缓存与定时器，修复插件重载或重启时提前触发未完成聚合通知的问题。

#### 配置说明

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 控制媒体服务器通知服务是否运行 |
| 媒体服务器类型 | 选择生效的媒体服务器（Emby / Jellyfin / Plex） |
| 聚合时间窗口 | 剧集入库连续静默时间（秒），在此时间内无新集入库后统一汇总推送 |

#### 当前版本

`v1.0.1`

### TMDB/Fanart 海报优先（TmdbPosterLanguagePriority）

按来源与语言优先级选择媒体海报。

> 维护者：[VirgoooooX](https://github.com/VirgoooooX) · [源码仓库](https://github.com/VirgoooooX/MoviePilot-Plugins)

#### 主要功能

- **按优先级选海报**：在元数据写入前拦截图片补全流程，根据自定义的来源与语言顺序筛选最佳主海报。
- **可配置候选层与自由禁用**：支持在 Vuetify 配置界面中灵活调整候选层顺序或直接移除禁用特定候选层。
- **背景图与 Logo 联动**：在选择主海报的同时，自动选择同次 TMDB 请求所得的背景图（backdrop）与标志图（logo）。
- **线程池与选择缓存**：支持异步调用与单轮选择缓存机制，保证高并发入库时不阻塞识别且避免重复 API 请求。

#### 配置说明

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 控制入库前海报优先选择服务是否运行 |
| 海报候选优先级 | 配置海报匹配层级的顺序与启用项（默认 `TMDB zh-CN` → `TMDB zh-SG` → `Fanart Chinese` → `TMDB 源语言` → `TMDB en-US` → `Fanart English` → `TMDB null`） |

#### 当前版本

`v1.0.1`

### Emby TMDB 合集整理（EmbyTmdbCollectionSync）

按 TMDB 官方合集整理 Emby 电影。

> 维护者：[VirgoooooX](https://github.com/VirgoooooX) · [源码仓库](https://github.com/VirgoooooX/MoviePilot-Plugins)

#### 主要功能

- **TMDB 官方合集比对预演**：读取 Emby 电影库中的 TMDB ID，按 TMDB 官方 Collection 规则自动预演与组建合集。
- **接管审核与手动调整**：支持逐合集预演预览、成员校正与接管审核，并能自动复用或新建同名合集。
- **封面与徽标自动同步**：检索并上传中文优先的 TMDB 合集海报（Poster）与徽标（Logo），支持配置覆盖已有图片。
- **空合集自动清理**：支持清理整理后不包含任何电影的空合集，保持 Emby 合集列表干练整洁。
- **独立 Vue 面板与侧栏导航**：提供独立的 Vue 动态渲染界面与侧边栏“Emby 合集整理”入口，支持实时任务进度监控与阶段心跳展示。

#### 配置说明

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 控制 Emby 合集整理服务及侧栏入口 |
| 显示侧栏导航 | 是否在 MoviePilot 侧边栏显示“Emby 合集整理”快捷入口 |
| 媒体服务器 | 选择要生效的 Emby 媒体服务器实例 |
| 生效媒体库 | 选择允许扫描整理的电影媒体库（留空表示所有电影库） |
| 覆盖已有封面与徽标 | 是否覆盖合集已有海报图片与 Logo 徽标 |
| 清理空合集 | 是否自动删除整理后不含任何电影的空合集 |
| 同步 Logo 徽标 | 是否在生成/校正合集时同步 Logo 徽标 |

#### 当前版本

`v1.0.1`

## 目录结构

```text
package.v2.json
.github/
├── scripts/check_plugin_metadata.py
└── workflows/plugin-validation.yml
plugins.v2/
├── embychineserolesync/
│   └── __init__.py
├── embytmdbcollectionsync/
│   ├── __init__.py
│   ├── dist/
│   ├── index.html
│   ├── package.json
│   ├── src/
│   └── vite.config.js
├── hdskymonitor/
│   ├── __init__.py
│   ├── client.py
│   ├── downloader.py
│   ├── matcher.py
│   ├── metadata.py
│   ├── monitor.py
│   ├── site.py
│   └── state.py
├── mediaservermsglocal/
│   └── __init__.py
└── tmdbposterlanguagepriority/
    └── __init__.py
tests/
├── hdskymonitor/
└── tmdbposterlanguagepriority/
```

## 开发校验

本仓库按 MoviePilot V2 插件开发指南维护元数据、运行时属性和前端产物。提交前执行：

```bash
python .github/scripts/check_plugin_metadata.py package.v2.json
python -m compileall -q plugins.v2
python -m pytest -q
npm ci --prefix plugins.v2/embytmdbcollectionsync
npm run build --prefix plugins.v2/embytmdbcollectionsync
git diff --check
```

`TmdbPosterLanguagePriority` 的完整测试依赖 MoviePilot 宿主；独立仓库环境会自动跳过该测试模块，宿主环境中应再运行一次真实加载验证。

## 免责声明

本仓库仅供个人学习和媒体管理自动化使用。请遵守相关站点规则及所在地法律法规，使用者自行承担因配置或使用不当产生的风险。
