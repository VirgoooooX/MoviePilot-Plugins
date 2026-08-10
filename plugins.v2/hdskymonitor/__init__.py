import os
import json
import logging
from datetime import datetime
from typing import Any, List, Dict, Optional, Tuple

from app.plugins import _PluginBase
from app.schemas.types import NotificationType
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# 脚本路径
PLUGIN_DIR = "/config/local-plugins/plugins.v2/hdskymonitor"
STATE_FILE = os.path.join(PLUGIN_DIR, "hdsky_monitor_state.json")
LOG_FILE = "/config/logs/plugins/hdskymonitor.log"


class HdskyMonitor(_PluginBase):
    """天空种子监控插件 - 定时监控天空站点新发布种子，自动下载并通知"""

    plugin_name = "天空监控"
    plugin_desc = "定时监控天空站点新发布种子，自动下载并通知"
    plugin_icon = "signin.png"
    plugin_version = "2.4.1"
    plugin_label = "站点订阅"
    # 插件作者
    plugin_author = "Virgooooox"
    # 作者主页
    author_url = "https://github.com/VirgoooooX"
    plugin_config_prefix = "hdskymonitor_"
    plugin_order = 100
    auth_level = 1

    # 默认配置
    _enabled: bool = False
    _cron: str = "0 8,20 * * *"
    _days: int = 1
    _max_pages: int = 5
    _limit: int = 0
    _notify: bool = True
    _save_path: str = "/downloadssd/local/"
    _downloader: Optional[str] = None
    _check_library: bool = True
    _running: bool = False

    def init_plugin(self, config: dict = None) -> None:
        """初始化插件"""
        self.stop_service()

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._cron = config.get("cron", "0 8,20 * * *")
            self._days = int(config.get("days", 1))
            self._max_pages = int(config.get("max_pages", 1))
            self._limit = int(config.get("limit", 0))
            self._notify = bool(config.get("notify", True))
            self._save_path = str(config.get("save_path") or "/downloadssd/local/").strip()
            self._downloader = str(config.get("downloader") or "").strip() or None
            self._check_library = bool(config.get("check_library", True))

        if self._enabled:
            logger.info(f"天空监控已启用，计划：{self._cron}")

    def _save_history(self, matched: List[dict], downloaded: List[dict], skipped: List[dict] = None) -> None:
        """保存天空监控历史匹配记录。"""
        history = self.get_data("history") or []
        skipped_keys = {row.get("key") for row in (skipped or [])}
        downloaded_keys = {item.get("key") for item in downloaded}
        for raw_item in matched:
            item = raw_item.get("item", raw_item)
            ti = item.get("torrent_info", {})
            mi = item.get("meta_info", {})
            key = f"hdsky:{ti.get('page_url', '')}"
            if any(row.get("unique") == key for row in history):
                continue
            name = mi.get("cn_name") or mi.get("name") or mi.get("en_name") or ti.get("title", "")
            year = mi.get("year") or ""
            from .metadata import get_poster_url
            try:
                poster = get_poster_url(name, year)
            except Exception:
                poster = None
            if key in downloaded_keys:
                status = "success"
            elif key in skipped_keys:
                status = "skipped"
            else:
                status = "matched"
            history.append({
                "unique": key,
                "torrent_id": ti.get("page_url", "").split("id=")[-1].split("&")[0],
                "title": ti.get("title", ""),
                "name": name,
                "year": year,
                "season_episode": mi.get("season_episode") or "",
                "poster": poster,
                "size": ti.get("size", 0),
                "seeders": ti.get("seeders", 0),
                "grabs": ti.get("grabs", 0),
                "pubdate": ti.get("pubdate", ""),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": status,
                "page_url": ti.get("page_url", ""),
            })
        self.save_data("history", history[-200:])

    @staticmethod
    def _write_plugin_log(level: str, message: str) -> None:
        """按插件页面兼容格式追加独立运行日志。"""
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as log_file:
            log_file.write(f"【{level.upper()}】{timestamp} - hdsky_monitor - {message}\n")

    def _post_monitor_message(self, title: str, text: str, image: str = None, link: str = None) -> None:
        """通过 MoviePilot 统一通知链发送天空监控消息。"""
        self.post_message(
            mtype=NotificationType.Plugin,
            title=title,
            text=text,
            image=image,
            link=link,
        )

    def _run_script(self, extra_args: List[str] = None) -> dict:
        """在插件进程内执行模块化监控流程。"""
        if self._running:
            return {"success": False, "output": "", "error": "监控任务正在运行", "returncode": -2}
        self._running = True
        try:
            from .monitor import HdskyMonitorRunner
            test_mode = bool(extra_args and "--test" in extra_args)
            runner = HdskyMonitorRunner(
                state_path=STATE_FILE,
                notifier=None if test_mode or not self._notify else self._post_monitor_message,
                history_callback=None if test_mode else self._save_history,
                log_callback=self._write_plugin_log,
            )
            count = runner.run(
                test_mode=test_mode,
                max_pages=self._max_pages,
                days=self._days,
                limit=self._limit,
                save_path=self._save_path,
                downloader=self._downloader,
                check_library=self._check_library,
            )
            return {"success": True, "output": f"完成，成功下载 {count} 个资源", "error": "", "returncode": 0}
        except Exception as exc:
            logger.exception("执行天空监控失败")
            return {"success": False, "output": "", "error": str(exc), "returncode": -1}
        finally:
            self._running = False

    def _load_state(self) -> dict:
        """加载运行状态"""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"processed": [], "last_run": None}

    def _load_logs(self, lines: int = 50) -> List[str]:
        """加载最近的日志"""
        if not os.path.exists(LOG_FILE):
            return []
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
                return [line.rstrip() for line in reversed(all_lines[-lines:])]
        except Exception:
            return []

    def get_state(self) -> bool:
        """获取插件启用状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件命令列表"""
        return [
            {
                "cmd": "/hdsky_run",
                "event_type": "",
                "desc": "立即执行天空监控",
                "category": "天空监控",
                "data": {}
            },
            {
                "cmd": "/hdsky_test",
                "event_type": "",
                "desc": "测试模式运行（不下载）",
                "category": "天空监控",
                "data": {}
            },
            {
                "cmd": "/hdsky_clear",
                "event_type": "",
                "desc": "清除已处理记录",
                "category": "天空监控",
                "data": {}
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表"""
        return [
            {
                "path": "/hdsky_run",
                "endpoint": self.api_run_monitor,
                "methods": ["POST"],
                "summary": "执行监控",
                "description": "立即执行天空监控脚本",
                "auth": "bear"
            },
            {
                "path": "/hdsky_test",
                "endpoint": self.api_test_monitor,
                "methods": ["POST"],
                "summary": "测试运行",
                "description": "测试模式运行（不下载）",
                "auth": "bear"
            },
            {
                "path": "/hdsky_state",
                "endpoint": self.api_get_state,
                "methods": ["GET"],
                "summary": "获取状态",
                "description": "获取监控运行状态",
                "auth": "bear"
            },
            {
                "path": "/hdsky_logs",
                "endpoint": self.api_get_logs,
                "methods": ["GET"],
                "summary": "获取日志",
                "description": "获取最近的运行日志",
                "auth": "bear"
            },
            {
                "path": "/hdsky_clear",
                "endpoint": self.api_clear_state,
                "methods": ["POST"],
                "summary": "清除记录",
                "description": "清除已处理的种子记录",
                "auth": "bear"
            }
        ]

    def api_run_monitor(self) -> dict:
        """API: 立即执行监控"""
        result = self._run_script()
        return result

    def api_test_monitor(self) -> dict:
        """API: 测试模式运行"""
        result = self._run_script(["--test"])
        return result

    def get_service(self) -> List[Dict[str, Any]]:
        """注册 MoviePilot 定时监控服务。"""
        if not self._enabled or not self._cron:
            return []
        return [{
            "id": "HdskyMonitor",
            "name": "天空种子监控",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self._run_scheduled,
            "kwargs": {},
        }]

    def _run_scheduled(self) -> None:
        """执行定时天空监控任务。"""
        self._run_script()

    def api_get_state(self) -> dict:
        """API: 获取运行状态"""
        state = self._load_state()
        return {
            "success": True,
            "data": {
                "last_run": state.get("last_run"),
                "processed_count": len(state.get("processed", [])),
                "config": {
                    "cron": self._cron,
                    "days": self._days,
                    "max_pages": self._max_pages,
                    "limit": self._limit,
                    "notify": self._notify,
                    "save_path": self._save_path,
                    "downloader": self._downloader,
                    "check_library": self._check_library
                }
            }
        }

    def api_get_logs(self) -> dict:
        """API: 获取最近日志"""
        logs = self._load_logs(100)
        return {
            "success": True,
            "data": logs
        }

    def api_clear_state(self) -> dict:
        """API: 清除已处理记录"""
        try:
            state = self._load_state()
            state["processed"] = []
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            return {"success": True, "message": "已清除处理记录"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def _build_history_card(self, item: dict) -> Dict[str, Any]:
        """构建响应式历史资源卡片。"""
        name = item.get("name") or item.get("title") or "未知资源"
        status = item.get("status") or "matched"
        status_map = {"success": ("已下载", "success"), "matched": ("已匹配", "info"), "failed": ("失败", "error"), "skipped": ("已存在", "warning")}
        status_text, status_color = status_map.get(status, (status, "default"))
        meta = " · ".join(filter(None, [str(item.get("year") or ""), item.get("season_episode") or ""]))
        size = item.get("size") or 0
        size_text = f"{size / 1073741824:.1f} GB" if size else "未知大小"
        return {
            "component": "VCol",
            "props": {"cols": "4", "sm": "4", "md": "2", "lg": "2"},
            "content": [{
                "component": "VCard",
                "props": {"variant": "flat", "class": "h-100 border rounded-lg overflow-hidden"},
                "content": [
                    {"component": "VImg", "props": {"src": item.get("poster") or "", "aspect-ratio": 2 / 3, "contain": True, "class": "bg-surface-variant"}},
                    {"component": "VCardText", "props": {"class": "pa-3"}, "content": [
                        {"component": "div", "props": {"class": "text-body-2 font-weight-bold text-truncate mb-1", "title": name}, "text": name},
                        {"component": "div", "props": {"class": "d-flex align-center ga-1 mb-2 flex-wrap"}, "content": [
                            {"component": "VChip", "props": {"size": "x-small", "color": status_color, "variant": "tonal"}, "text": status_text},
                            {"component": "span", "props": {"class": "text-caption text-medium-emphasis"}, "text": meta or "天空资源"}
                        ]},
                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": f"{size_text} · 🌱 {item.get('seeders', 0)}"}
                    ]},
                    {"component": "VCardActions", "props": {"class": "pt-0 px-2 pb-2"}, "content": [
                        {"component": "VBtn", "props": {"href": item.get("page_url") or "", "target": "_blank", "size": "small", "variant": "text", "color": "primary", "block": True, "append-icon": "mdi-open-in-new"}, "text": "查看种子"}
                    ]}
                ]
            }]
        }

    def _build_overview_page(self, last_run: str, processed_count: int, log_text: str, history: List[dict]) -> List[Dict[str, Any]]:
        """构建层次清晰的响应式监控总览页面。"""
        stats = [
            ("运行状态", "已启用" if self._enabled else "已停用", "mdi-power", "success" if self._enabled else "grey"),
            ("上次运行", last_run or "从未运行", "mdi-clock-outline", "primary"),
            ("已处理", f"{processed_count} 个种子", "mdi-check-circle-outline", "info"),
            ("历史记录", f"{len(history)} 条", "mdi-history", "secondary"),
        ]
        stat_cards = [{"component": "VCol", "props": {"cols": "6", "md": "3"}, "content": [{"component": "VCard", "props": {"variant": "tonal", "color": color, "class": "h-100"}, "content": [{"component": "VCardText", "props": {"class": "pa-4"}, "content": [{"component": "VIcon", "props": {"icon": icon, "size": "24", "class": "mb-2"}}, {"component": "div", "props": {"class": "text-caption"}, "text": label}, {"component": "div", "props": {"class": "text-body-1 font-weight-bold text-truncate"}, "text": value}]}]}]} for label, value, icon, color in stats]
        actions = [
            ("立即执行", "mdi-play", "primary", "hdsky_run"),
            ("测试运行", "mdi-flask-outline", "warning", "hdsky_test"),
            ("清除去重", "mdi-delete-sweep-outline", "error", "hdsky_clear"),
        ]
        action_buttons = [{"component": "VBtn", "props": {"color": color, "variant": "tonal", "prepend-icon": icon, "class": "flex-grow-1"}, "events": {"click": {"api": f"plugin/HdskyMonitor/{api}", "method": "POST"}}, "text": label} for label, icon, color, api in actions]
        history_cards = [self._build_history_card(item) for item in history[:24]]
        return [
            {"component": "div", "props": {"class": "pa-2 pa-sm-4"}, "content": [
                {"component": "div", "props": {"class": "d-flex flex-column flex-sm-row align-sm-center justify-space-between ga-3 mb-5"}, "content": [
                    {"component": "div", "content": [{"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": "天空监控"}, {"component": "div", "props": {"class": "text-body-2 text-medium-emphasis mt-1"}, "text": "去头尾广告纯享版自动监控与下载"}]},
                    {"component": "VChip", "props": {"color": "success" if self._enabled else "grey", "variant": "tonal", "prepend-icon": "mdi-radar"}, "text": "监控中" if self._enabled else "已停用"}
                ]},
                {"component": "VRow", "props": {"dense": True, "class": "mb-4"}, "content": stat_cards},
                {"component": "VCard", "props": {"variant": "outlined", "class": "mb-5 rounded-lg"}, "content": [
                    {"component": "VCardText", "props": {"class": "d-flex flex-wrap align-center ga-2"}, "content": action_buttons + [{"component": "VSpacer"}, {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": f"计划 {self._cron} · 最近 {self._days} 天 · 最大 {self._max_pages} 页 · 通知{'开' if self._notify else '关'}"}]}
                ]},
                {"component": "div", "props": {"class": "d-flex align-center justify-space-between mb-3"}, "content": [{"component": "div", "props": {"class": "text-h6 font-weight-bold"}, "text": "历史匹配"}, {"component": "VChip", "props": {"size": "small", "variant": "tonal"}, "text": f"最近 {min(len(history), 24)} / {len(history)}"}]},
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-5", "text": "暂无历史记录，正式监控匹配到资源后会显示在这里。"}} if not history_cards else {"component": "VRow", "props": {"dense": True, "class": "mb-5"}, "content": history_cards},
                {"component": "VExpansionPanels", "props": {"variant": "accordion"}, "content": [{"component": "VExpansionPanel", "content": [{"component": "VExpansionPanelTitle", "props": {"class": "font-weight-medium"}, "text": "运行日志"}, {"component": "VExpansionPanelText", "content": [{"component": "div", "props": {"class": "pa-3 rounded bg-surface-variant text-caption", "style": "white-space:pre-wrap;font-family:monospace;max-height:360px;overflow-y:auto;line-height:1.6"}, "text": log_text}]}]}]}
            ]}
        ]

    def get_page(self) -> List[Dict[str, Any]]:
        """返回插件页面 - 监控面板"""
        state = self._load_state()
        last_run = state.get("last_run", "从未运行")
        processed_count = len(state.get("processed", []))
        logs = self._load_logs(50)
        log_text = "\n".join(logs) if logs else "暂无日志"
        history = sorted(self.get_data("history") or [], key=lambda x: x.get("time", ""), reverse=True)
        return self._build_overview_page(last_run, processed_count, log_text, history)

    def get_form(self) -> Tuple[list, dict]:
        """返回结构清晰的响应式插件配置表单。"""
        section_title = lambda icon, title, subtitle: {
            "component": "div",
            "props": {"class": "d-flex align-start ga-3 mb-4"},
            "content": [
                {"component": "VAvatar", "props": {"color": "primary", "variant": "tonal", "size": "40"}, "content": [{"component": "VIcon", "props": {"icon": icon}}]},
                {"component": "div", "content": [
                    {"component": "div", "props": {"class": "text-subtitle-1 font-weight-bold"}, "text": title},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": subtitle}
                ]}
            ]
        }
        return [{
            "component": "VForm",
            "content": [{
                "component": "div",
                "props": {"class": "pa-1 pa-sm-3"},
                "content": [
                    {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-4", "title": "天空纯享版监控", "text": "定时扫描天空站点的去头尾广告纯享版资源，匹配全集后自动提交下载。"}},
                    {"component": "VRow", "props": {"dense": True}, "content": [
                        {"component": "VCol", "props": {"cols": "12", "md": "6"}, "content": [{
                            "component": "VCard", "props": {"variant": "outlined", "class": "h-100 rounded-lg"}, "content": [
                                {"component": "VCardText", "content": [
                                    section_title("mdi-radar", "运行与计划", "控制插件状态和定时执行周期"),
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用天空监控", "color": "primary", "inset": True, "hide-details": True, "class": "mb-4"}},
                                    {"component": "VTextField", "props": {"model": "cron", "label": "Cron 表达式", "prepend-inner-icon": "mdi-calendar-clock", "placeholder": "0 8,20 * * *", "hint": "示例：0 */6 * * * 表示每 6 小时执行一次", "persistent-hint": True, "variant": "outlined", "density": "comfortable"}}
                                ]}
                            ]}]},
                        {"component": "VCol", "props": {"cols": "12", "md": "6"}, "content": [{
                            "component": "VCard", "props": {"variant": "outlined", "class": "h-100 rounded-lg"}, "content": [
                                {"component": "VCardText", "content": [
                                    section_title("mdi-bell-outline", "下载与通知", "控制单次下载数量及成功通知"),
                                    {"component": "VTextField", "props": {"model": "save_path", "label": "下载路径", "prepend-inner-icon": "mdi-folder-download-outline", "placeholder": "/downloadssd/local/", "hint": "必须位于 MoviePilot 已配置的下载目录内", "persistent-hint": True, "variant": "outlined", "density": "comfortable", "class": "mb-3"}},
                                    {"component": "VSelect", "props": {"model": "downloader", "label": "下载器", "prepend-inner-icon": "mdi-download-network-outline", "items": [{"title": "跟随站点或系统默认", "value": ""}, {"title": "QB-NAS", "value": "QB-NAS"}, {"title": "TR-NAS", "value": "TR-NAS"}, {"title": "QB-OC芝加哥", "value": "QB-OC芝加哥"}], "item-title": "title", "item-value": "value", "hint": "留空时跟随天空站点设置或使用系统默认下载器", "persistent-hint": True, "variant": "outlined", "density": "comfortable", "class": "mb-3"}},
                                    {"component": "VTextField", "props": {"model": "limit", "label": "单次下载上限", "type": "number", "min": 0, "max": 50, "suffix": "个", "prepend-inner-icon": "mdi-download-multiple", "hint": "填 0 表示不限制", "persistent-hint": True, "variant": "outlined", "density": "comfortable", "class": "mb-3"}},
                                    {"component": "VSwitch", "props": {"model": "notify", "label": "下载成功后发送通知", "color": "primary", "inset": True, "hint": "通过 MoviePilot 已启用的通知渠道发送", "persistent-hint": True}},
                                    {"component": "VSwitch", "props": {"model": "check_library", "label": "下载前检查媒体库", "color": "primary", "inset": True, "hint": "媒体库已存在完整媒体时跳过下载，避免重复入库", "persistent-hint": True}}
                                ]}
                            ]}]}
                    ]},
                    {"component": "VCard", "props": {"variant": "outlined", "class": "mt-3 rounded-lg"}, "content": [
                        {"component": "VCardText", "content": [
                            section_title("mdi-tune-variant", "扫描范围", "限制发布时间窗口和每次扫描深度"),
                            {"component": "VRow", "props": {"dense": True}, "content": [
                                {"component": "VCol", "props": {"cols": "12", "sm": "6"}, "content": [{"component": "VTextField", "props": {"model": "days", "label": "发布时间范围", "type": "number", "min": 1, "max": 30, "suffix": "天", "prepend-inner-icon": "mdi-calendar-range", "hint": "只处理最近指定天数发布的资源", "persistent-hint": True, "variant": "outlined", "density": "comfortable"}}]},
                                {"component": "VCol", "props": {"cols": "12", "sm": "6"}, "content": [{"component": "VTextField", "props": {"model": "max_pages", "label": "最大扫描页数", "type": "number", "min": 1, "max": 20, "suffix": "页", "prepend-inner-icon": "mdi-file-search-outline", "hint": "站点当前逻辑主要扫描第一页", "persistent-hint": True, "variant": "outlined", "density": "comfortable"}}]}
                            ]}
                        ]}
                    ]},
                    {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "density": "compact", "class": "mt-4", "text": "测试运行不会下载、不会发送通知，也不会写入历史记录。"}}
                ]
            }]
        }], {"enabled": False, "cron": "0 8,20 * * *", "days": 1, "max_pages": 5, "limit": 0, "notify": True, "save_path": "/downloadssd/local/", "downloader": "", "check_library": True}

    def stop_service(self) -> None:
        """停止插件服务"""
        pass
