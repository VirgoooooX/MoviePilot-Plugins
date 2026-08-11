# Emby 中文角色同步

同步豆瓣中文演员姓名与角色到已有的 Emby Person 关系，支持媒体库白名单、队列去重和指定媒体同步。

## 安全开关

- `锁定人物中文姓名（Name）`：关闭时不会新增人物 Name 锁定。
- `锁定媒体演职人员（Cast）`：关闭时不会新增媒体、季度或单集 Cast 锁定。
- 插件只会记录当前运行实例新增的目标锁字段；关闭开关不会猜测性解锁旧版本或用户原有锁，已有锁需在 Emby 中人工确认。

## 只读预演

设置页选择媒体后点击“只读预演”，或调用 `POST /plugin/EmbyChineseRoleSync/preview_media`，请求体为：

```json
{"server": "已配置的 Emby 名称", "item_id": "Emby Item ID"}
```

预演只读取 Emby、TMDB 和豆瓣数据，返回 `would_change_count`、`would_lock_count` 及匹配摘要，不会调用任何 Emby 写入、锁定、图片刷新或历史写入接口。

## 事故维护工具

事故清理脚本位于仓库级 `tools/maintenance/embychineserolesync/`，不随运行时插件发布。脚本要求显式传入服务器和日志/映射文件，默认只生成计划；只有使用 `--apply --plan` 才会写入，并在写入前再次校验快照。

维护脚本依赖 MoviePilot 宿主的 `app.*` 模块（仅 `--help` 可在宿主外查看）。应用计划时还必须让 `plan.server` 与 `--server` 一致，并提供生成计划时输出的 `--confirm <plan_digest>`，防止误把计划用于其它 Emby 服务。
