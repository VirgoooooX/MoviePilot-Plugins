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
- **只读预演与安全锁定**：同步前可查看预计姓名、角色和锁字段变更；人物姓名与媒体 Cast 锁定改为独立开关，新安装默认不锁定。

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
| 锁定人物中文姓名 | 是否为本次新增的人物中文姓名写入 `LockedFields.Name`（新安装默认关闭） |
| 锁定媒体演职人员 | 是否为本次更新的媒体写入 `LockedFields.Cast`（新安装默认关闭） |
| 指定影视剧检索同步 | 输入剧名或 Item ID 实时检索作品，勾选后点击【同步所选媒体】 |
| 只读预演 | 对当前选中媒体执行识别与匹配，仅统计预计变更，不写入 Emby |

#### 当前版本

`v1.1.0`

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
- **中文地区标签兼容**：同时识别 TMDB 的复合地区标签与语言/地区分字段返回，支持 `zh-CN`、`zh-SG`、`zh-TW`、`zh-HK` 及旧版泛 `zh` 回退。
- **背景图与 Logo 多源联动**：不仅支持 TMDB 背景图与标志图，还可解析 Fanart 中的 Logo（`movielogo`/`tvlogo`）及背景图（`moviebackground`/`showbackground`）并按语言优先级选择。
- **中文源语言智能回退**：对于中文源语言媒体，在配置缺省时自动插入 `tmdb_zh` 回退层，确保最佳中文画面匹配。
- **线程池与选择缓存**：支持异步调用与单轮 LRU 缓存机制，保证高并发入库时不阻塞识别且避免重复 API 请求。
- **运行日志与全链路诊断**：提供完善的同步/异步运行日志、跳过/未命中原因及详细候选匹配结果输出，方便追踪与定位问题。

#### 配置说明

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 控制入库前海报优先选择服务是否运行 |
| 海报候选优先级 | 配置海报匹配层级的顺序与启用项（默认依次为 TMDB `zh-CN`、`zh-SG`、`zh-TW`、`zh-HK`、泛 `zh`，再到 Fanart 中文、源语言、TMDB `en-US`、泛 `en`、Fanart 英文和 `null`） |

#### 当前版本

`v1.3.0`

### Emby TMDB 合集整理（EmbyTmdbCollectionSync）

按 TMDB 官方合集整理 Emby 电影。

> 维护者：[VirgoooooX](https://github.com/VirgoooooX) · [源码仓库](https://github.com/VirgoooooX/MoviePilot-Plugins)

#### 主要功能

- **TMDB 官方合集比对预演**：读取 Emby 电影库中的 TMDB ID，按 TMDB 官方 Collection 规则自动预演与组建合集。
- **合集缺片一键订阅**：对比 TMDB 官方合集完整片单与所选 Emby 电影库，在合集卡片和详情中展示缺片，并可一键加入 MoviePilot 电影订阅。
- **人工合集优先保护**：插件接管后持续记录成员与名称基线；识别到用户手工改名、增删或合并成员时自动锁定，保护其中电影不再被后续 TMDB 扫描移动或重复分配，也可手动恢复 TMDB 管理。
- **持久化审核决定**：支持永久忽略不需要的 TMDB 合集、把现有 Emby 合集确认为手工合集，以及删除插件管理的合集并阻止以后重建；忽略记录可随时恢复。
- **单片合集暂缓**：准备新建且当前仅入库一部电影时，若 TMDB 不足两部常规电影已经上映，则暂缓创建，等系列成熟或第二部入库后再评估。
- **接管审核与安全执行**：支持逐合集预演、成员校正与接管审核；执行前绑定计划 ID、配置指纹并复核 Emby 成员快照，过期计划不会继续写入。
- **封面与徽标自动同步**：按海报优先插件同样的规则检索并上传 TMDB 合集海报（Poster）与徽标（Logo），优先简体中文并排除 `zh-TW`、`zh-HK`、`zh-Hant` 繁体候选，支持配置覆盖已有图片。
- **空合集自动清理**：支持清理整理后不包含任何电影的空合集，保持 Emby 合集列表干练整洁。
- **响应式合集工作台**：合集由单行列表升级为 2:3 竖版海报卡片网格，按屏幕宽度显示 2 至 6 列，并以详情对话框承载完整增删清单。
- **任务互斥与取消**：扫描、执行和配置保存互相保护，支持查看真实忙碌状态并安全请求取消后台任务。

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

`v1.3.1`

### Emby媒体图片管理（EmbyMediaImageManager）

实时刮削 Emby 新入库媒体、检查指定存量目录的简体中文图片，并刷新现有合集封面与徽标。

> 维护者：[VirgoooooX](https://github.com/VirgoooooX) · [源码仓库](https://github.com/VirgoooooX/MoviePilot-Plugins)

#### 主要功能

- **入库事件聚合**：接收 Emby `library.new` Webhook，电影按延迟处理，连续剧集事件按整部剧静默聚合，避免重复刮削。
- **媒体库范围分离**：实时刮削默认覆盖所选实例的全部媒体库；存量图片检查单独选择少量外语电影库、剧库，两个范围互不影响。
- **简体图片检查**：按检查计划扫描所选媒体库，发现简体中文候选后覆盖刮削并刷新 Emby。
- **合集图片刷新**：新增“合集图片”Tab，只读取现有 Emby BoxSet，按 TMDB/Fanart 海报优先级分别更新 poster 和 Logo，不创建、删除或改动合集成员；上传后回读 ImageTags 验证。
- **安全运行状态**：存量检查和合集图片任务互斥执行、分批保存进度；详情页展示待处理事件、已补齐媒体、合集成功/跳过/失败和最近结果。

#### 使用前提

1. 启用“TMDB/Fanart 海报优先”插件后，存量图片检查和合集图片刷新会直接读取其当前优先级配置。
2. MoviePilot 必须能够访问所选媒体库对应的物理目录；目录兜底仅用于未选择媒体库或兼容旧路径配置。

#### 媒体库选择建议

| 配置页 | 推荐设置 |
| --- | --- |
| 实时刮削 | 实时媒体库留空，表示覆盖所选 Emby 实例的全部媒体库 |
| 存量图片检查 | 只选择需要补中文图片的外语电影库、外语剧库 |
| 合集图片 | 选择处理范围、覆盖 poster/Logo 选项，保存后点击“开始刷新合集图片” |
| 路径兜底 | 正常情况下留空，仅在 MoviePilot 无法读取媒体库路径时使用 |

#### 当前版本

`v1.3.0`

## 目录结构

```text
package.v2.json
.github/
├── scripts/check_plugin_metadata.py
└── workflows/plugin-validation.yml
plugins.v2/
├── embychineserolesync/
│   └── __init__.py
├── embymediaimagemanager/
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
├── embychineserolesync/
├── embymediaimagemanager/
├── embytmdbcollectionsync/
├── hdskymonitor/
└── tmdbposterlanguagepriority/
tools/
└── maintenance/embychineserolesync/
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

插件核心逻辑测试会安装最小宿主桩并在独立仓库环境实际执行；发布前仍应在 MoviePilot 宿主中做一次安装、页面加载与 API 冒烟验证。

## 免责声明

本仓库仅供个人学习和媒体管理自动化使用。请遵守相关站点规则及所在地法律法规，使用者自行承担因配置或使用不当产生的风险。
