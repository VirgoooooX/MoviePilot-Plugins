# MoviePilot Plugins

个人维护的 MoviePilot V2 插件仓库。

## 插件列表

### Emby角色同步增强（EmbyActorEnhanceLocal）

本地增强版 Emby 元数据插件，用于将豆瓣中文演员和角色信息同步到电视剧、季度及每集条目，并支持单集元数据刷新。

#### 主要功能

- 豆瓣中文演员和角色同步到 Emby 电视剧、季度及单集。
- 豆瓣角色为“演员”时保留 TMDB 原有的具体角色名。
- 支持整季、单集和多集入库场景。
- 按 Episode ID 记录成功状态，避免同一单集重复处理。
- Webhook 与定时扫描统一进入队列，降低并发重复更新。
- 支持同步后非递归刷新单集元数据。
- 支持清除插件缓存后重新运行。

#### 配置说明

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 启用 Emby 角色同步服务 |
| 清除缓存后运行 | 清除插件记录的单集成功状态 |
| 立即运行一次 | 立即扫描最近入库媒体 |
| 同步每集演职人员 | 将季度/电视剧演员角色同步到每集 |
| 同步后刷新单集 | 写入单集演职人员后调用 Emby 单集刷新 |
| 覆盖单集已有演职人员 | 是否覆盖单集现有 People 列表 |
| 最新入库天数 | 定时扫描的时间范围 |
| 执行周期 | 定时扫描 Cron 表达式 |

#### 版本

`v1.1.2-local`

### 天空监控（HdskyMonitor）

定时监控天空站点中符合规则的“去头尾广告纯享版”资源，匹配全集后自动提交到 MoviePilot 下载，并通过 MoviePilot 统一通知渠道发送下载结果。

#### 主要功能

- 使用 MoviePilot 定时服务，支持自定义 Cron 表达式
- 按发布时间范围扫描天空站点资源
- 排除单集资源，仅匹配全集、完结或 Complete 资源
- 自动提交 MoviePilot 下载任务
- 下载成功后通过 MoviePilot `post_message` 发送通知
- 支持 Telegram、企业微信等 MoviePilot 已启用通知渠道
- 响应式运行面板和设置页面
- 历史匹配资源海报墙
- 最近日志倒序展示
- 种子 ID 去重及状态原子写入
- 手动执行、测试运行和清除去重记录

#### 安装

在 MoviePilot 的插件市场设置中添加仓库：

```text
https://github.com/VirgoooooX/MoviePilot-Plugins
```

或者使用 V2 市场索引：

```text
https://raw.githubusercontent.com/VirgoooooX/MoviePilot-Plugins/main/package.v2.json
```

刷新插件市场后，搜索并安装 `天空监控`。

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
| 单次下载上限 | 每次最多提交的资源数量，`0` 表示不限制 |
| 下载成功通知 | 仅在真正成功提交下载后发送通知 |

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

`v2.0.0`

## 目录结构

```text
package.v2.json
plugins.v2/
└── hdskymonitor/
    ├── __init__.py
    ├── client.py
    ├── downloader.py
    ├── matcher.py
    ├── metadata.py
    ├── monitor.py
    ├── site.py
    ├── state.py
    └── tests/
```

## 免责声明

本仓库仅供个人学习和媒体管理自动化使用。请遵守相关站点规则及所在地法律法规，使用者自行承担因配置或使用不当产生的风险。
