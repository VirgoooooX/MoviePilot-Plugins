"""Emby 入库实时刮削与媒体图片审计插件。"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.media import MediaChain
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import RefreshMediaItem, ServiceInfo, WebhookEventInfo
from app.schemas.types import EventType, MediaType
from app.utils.system import SystemUtils


class EmbyMediaImageManager(_PluginBase):
    """处理 Emby 外部入库实时刮削与指定媒体库图片审计。"""

    plugin_name = "Emby媒体图片管理"
    plugin_desc = "Emby入库后实时刮削，并按目标媒体库周期审计简体中文图片。"
    plugin_icon = "image-search-outline"
    plugin_version = "1.0.0"
    plugin_author = "VirgoooooX"
    author_url = "https://github.com/VirgoooooX/MoviePilot-Plugins"
    plugin_label = "媒体服务器,元数据"
    plugin_config_prefix = "embymediaimagemanager_"
    plugin_order = 30
    auth_level = 1

    DEFAULT_AUDIT_CRON = "0 4 1 * *"
    SIMPLIFIED_IMAGE_PRIORITIES = {
        "tmdb_zh_cn",
        "tmdb_zh_sg",
        "fanart_chinese",
    }
    DATA_STATES = "states"

    _enabled = False
    _realtime_enabled = True
    _audit_enabled = False
    _movie_enabled = True
    _tv_enabled = True
    _delay_seconds = 60
    _aggregate_seconds = 90
    _audit_cron = DEFAULT_AUDIT_CRON
    _realtime_paths = ""
    _audit_paths = ""
    _exclude_paths = ""
    _mediaservers: List[str] = []
    _webhook_source = ""

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取并规范化配置，同时清理上一轮延迟任务。"""
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._realtime_enabled = bool(config.get("realtime_enabled", True))
        self._audit_enabled = bool(config.get("audit_enabled", False))
        self._movie_enabled = bool(config.get("movie_enabled", True))
        self._tv_enabled = bool(config.get("tv_enabled", True))
        self._delay_seconds = self._safe_nonnegative_int(
            config.get("delay_seconds"), 60, "入库延迟秒数"
        )
        self._aggregate_seconds = self._safe_nonnegative_int(
            config.get("aggregate_seconds"), 90, "电视剧聚合秒数"
        )
        self._audit_cron = self._safe_cron(config.get("audit_cron"))
        self._realtime_paths = str(config.get("realtime_paths") or "").strip()
        self._audit_paths = str(config.get("audit_paths") or "").strip()
        self._exclude_paths = str(config.get("exclude_paths") or "").strip()
        value = config.get("mediaservers") or []
        values = value if isinstance(value, list) else str(value).splitlines()
        self._mediaservers = list(
            dict.fromkeys(str(item).strip() for item in values if str(item).strip())
        )
        self._webhook_source = str(config.get("webhook_source") or "").strip()

        self._lock = threading.RLock()
        # 保留同一把审计锁，避免插件热重载时新旧审计并行。
        self._audit_lock = getattr(self, "_audit_lock", threading.Lock())
        self._stop_event = threading.Event()
        self._pending: Dict[str, dict] = {}
        self._timers: Dict[str, threading.Timer] = {}
        self._last_realtime_at = ""
        self._last_realtime_result = "尚未处理入库事件"
        self._last_audit_at = ""
        self._last_audit_result = "尚未执行图片审计"
        self._audit_running = False

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回分区清晰的实时刮削与图片审计配置表单。"""
        try:
            server_items = [
                {"title": config.name, "value": config.name}
                for config in MediaServerHelper().get_configs().values()
                if config.type == "emby"
            ]
        except Exception as err:
            logger.warning("读取 Emby 服务配置失败：%s", err)
            server_items = []

        defaults = {
            "enabled": False,
            "realtime_enabled": True,
            "audit_enabled": False,
            "movie_enabled": True,
            "tv_enabled": True,
            "delay_seconds": 60,
            "aggregate_seconds": 90,
            "audit_cron": self.DEFAULT_AUDIT_CRON,
            "mediaservers": [],
            "webhook_source": "",
            "realtime_paths": "",
            "audit_paths": "",
            "exclude_paths": "",
        }
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "title": "两条图片管理链路",
                            "text": "实时刮削负责新入库媒体；周期审计只检查指定存量目录。两者可独立启用。",
                            "class": "mb-4",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._form_col(
                                "enabled",
                                "启用插件",
                                "总开关；关闭后不会接收事件或运行审计。",
                                12,
                            ),
                            self._form_col(
                                "realtime_enabled",
                                "新入库实时刮削",
                                "收到 Emby library.new 后延迟处理。",
                                6,
                            ),
                            self._form_col(
                                "audit_enabled",
                                "存量图片周期审计",
                                "按计划检查指定目录中的简体中文图片候选。",
                                6,
                            ),
                            self._form_col(
                                "movie_enabled",
                                "处理电影",
                                "实时与审计流程均遵循此开关。",
                                6,
                            ),
                            self._form_col(
                                "tv_enabled",
                                "处理电视剧",
                                "同一剧集的连续事件会合并为一次。",
                                6,
                            ),
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-5"},
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-subtitle-1 font-weight-bold mb-1"},
                        "text": "实时刮削",
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-body-2 text-medium-emphasis mb-3"},
                        "text": "先限定事件来源，再设置等待窗口；剧集事件会聚合到整部剧目录。",
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "mediaservers",
                                            "label": "允许的 Emby 实例",
                                            "items": server_items,
                                            "item-title": "title",
                                            "item-value": "value",
                                            "multiple": True,
                                            "chips": True,
                                            "closable-chips": True,
                                            "variant": "outlined",
                                            "hint": "留空接收全部 Emby 实例；选择后同时限制 Webhook 与刷新目标。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "webhook_source",
                                            "label": "Webhook source（可选）",
                                            "variant": "outlined",
                                            "hint": "仅接收这个 source；通常留空，交由 MoviePilot 匹配实例。",
                                            "persistent-hint": True,
                                            "clearable": True,
                                        },
                                    }
                                ],
                            },
                            self._number_col(
                                "delay_seconds",
                                "电影等待时间（秒）",
                                "等待 Emby 完成文件扫描后再刮削。",
                            ),
                            self._number_col(
                                "aggregate_seconds",
                                "剧集静默窗口（秒）",
                                "最后一集事件后等待多久，再处理整部剧。",
                            ),
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "outlined",
                            "density": "compact",
                            "text": "在 Emby Webhook 插件中将地址指向 MoviePilot 的 /api/v1/webhook/ 并携带 API Token；这里不填写完整 URL。",
                            "class": "mb-4",
                        },
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "realtime_paths",
                            "label": "实时处理目录白名单",
                            "rows": 3,
                            "auto-grow": True,
                            "variant": "outlined",
                            "hint": "每行一个宿主可访问路径；留空表示不限制。",
                            "persistent-hint": True,
                        },
                    },
                    {"component": "VDivider", "props": {"class": "my-5"}},
                    {
                        "component": "div",
                        "props": {"class": "text-subtitle-1 font-weight-bold mb-1"},
                        "text": "周期审计",
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-body-2 text-medium-emphasis mb-3"},
                        "text": "只扫描明确列出的目录；已成功补齐简体图片的媒体会被记住并跳过。",
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "density": "compact",
                            "text": "审计候选语言由“TMDB/Fanart 海报优先”插件提供；未启用时只会记录等待，不会覆盖现有图片。",
                            "class": "mb-3",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "audit_cron",
                                            "label": "审计计划",
                                            "hint": "默认每月 1 日凌晨 4 点：0 4 1 * *",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 7},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "audit_paths",
                                            "label": "必须填写的审计目录",
                                            "rows": 3,
                                            "auto-grow": True,
                                            "variant": "outlined",
                                            "hint": "每行一个路径；留空时审计不会扫描任何目录。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "exclude_paths",
                            "label": "统一排除目录",
                            "rows": 2,
                            "auto-grow": True,
                            "variant": "outlined",
                            "hint": "每行一个路径，同时应用于实时刮削和周期审计。",
                            "persistent-hint": True,
                        },
                    },
                ],
            }
        ], defaults

    @staticmethod
    def _form_col(model: str, label: str, hint: str, md: int) -> dict:
        """生成响应式开关列。"""
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [
                {
                    "component": "VSwitch",
                    "props": {
                        "model": model,
                        "label": label,
                        "color": "primary",
                        "hint": hint,
                        "persistent-hint": True,
                        "inset": True,
                    },
                }
            ],
        }

    @staticmethod
    def _number_col(model: str, label: str, hint: str) -> dict:
        """生成非负秒数输入列。"""
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 6},
            "content": [
                {
                    "component": "VTextField",
                    "props": {
                        "model": model,
                        "label": label,
                        "type": "number",
                        "min": 0,
                        "step": 1,
                        "variant": "outlined",
                        "hint": hint,
                        "persistent-hint": True,
                    },
                }
            ],
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回简洁的运行状态与审计结果页面。"""
        with getattr(self, "_lock", threading.RLock()):
            pending_count = len(getattr(self, "_pending", {}))
        states = self.get_data(self.DATA_STATES) or {}
        if not isinstance(states, dict):
            states = {}
        fixed_count = sum(
            1 for item in states.values() if item.get("status") == "fixed_zh"
        )
        pending_audit = sum(
            1 for item in states.values() if item.get("status") == "pending"
        )
        enabled_text = "运行中" if self._enabled else "已停用"
        status_color = "success" if self._enabled else "grey"
        return [
            {
                "component": "VContainer",
                "props": {"fluid": True, "class": "pa-0"},
                "content": [
                    {
                        "component": "VCard",
                        "props": {"variant": "outlined", "class": "mb-4 rounded-lg"},
                        "content": [
                            {
                                "component": "VCardText",
                                "props": {
                                    "class": "d-flex flex-wrap align-center ga-2"
                                },
                                "content": [
                                    {
                                        "component": "VChip",
                                        "props": {
                                            "color": status_color,
                                            "variant": "tonal",
                                            "prepend-icon": "mdi-image-sync-outline",
                                        },
                                        "text": enabled_text,
                                    },
                                    {
                                        "component": "VChip",
                                        "props": {"variant": "tonal"},
                                        "text": f"实时 {'开启' if self._realtime_enabled else '关闭'}",
                                    },
                                    {
                                        "component": "VChip",
                                        "props": {"variant": "tonal"},
                                        "text": f"审计 {'开启' if self._audit_enabled else '关闭'}",
                                    },
                                    {"component": "VSpacer"},
                                    {
                                        "component": "div",
                                        "props": {
                                            "class": "text-caption text-medium-emphasis"
                                        },
                                        "text": f"计划：{self._audit_cron}",
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._stat_card(
                                "待处理事件", pending_count, "mdi-timer-sand", "primary"
                            ),
                            self._stat_card(
                                "已补齐简体",
                                fixed_count,
                                "mdi-check-decagram-outline",
                                "success",
                            ),
                            self._stat_card(
                                "等待中文图片",
                                pending_audit,
                                "mdi-image-search-outline",
                                "warning",
                            ),
                            self._stat_card(
                                "审计记录",
                                len(states),
                                "mdi-folder-search-outline",
                                "info",
                            ),
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info" if not self._audit_running else "warning",
                            "variant": "tonal",
                            "title": "最近运行",
                            "text": f"实时：{self._last_realtime_result}（{self._last_realtime_at or '—'}）\n审计：{self._last_audit_result}（{self._last_audit_at or '—'}）",
                            "class": "mt-2 white-space-pre-line",
                        },
                    },
                ],
            }
        ]

    @staticmethod
    def _stat_card(label: str, value: Any, icon: str, color: str) -> dict:
        """生成状态页统计卡片。"""
        return {
            "component": "VCol",
            "props": {"cols": 6, "md": 3},
            "content": [
                {
                    "component": "VCard",
                    "props": {
                        "variant": "tonal",
                        "color": color,
                        "class": "h-100 rounded-lg",
                    },
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {"class": "pa-4"},
                            "content": [
                                {
                                    "component": "VIcon",
                                    "props": {
                                        "icon": icon,
                                        "size": 22,
                                        "class": "mb-2",
                                    },
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-h6 font-weight-bold"},
                                    "text": str(value),
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption"},
                                    "text": label,
                                },
                            ],
                        }
                    ],
                }
            ],
        }

    def get_service(self) -> List[Dict[str, Any]]:
        """注册周期审计服务。"""
        if not self._enabled or not self._audit_enabled:
            return []
        return [
            {
                "id": "EmbyMediaImageManagerAudit",
                "name": "Emby媒体图片审计",
                "trigger": CronTrigger.from_crontab(self._audit_cron),
                "func": self.run_audit,
                "kwargs": {},
            }
        ]

    def stop_service(self) -> None:
        """停止延迟任务并通知进行中的审计尽快退出。"""
        stop_event = getattr(self, "_stop_event", None)
        if stop_event:
            stop_event.set()
        lock = getattr(self, "_lock", None)
        if not lock:
            return
        with lock:
            for timer in getattr(self, "_timers", {}).values():
                timer.cancel()
            getattr(self, "_timers", {}).clear()
            getattr(self, "_pending", {}).clear()

    @eventmanager.register(EventType.WebhookMessage)
    def on_webhook(self, event: Event) -> None:
        """接收 Emby library.new 事件并加入去重后的实时刮削队列。"""
        if not self._enabled or not self._realtime_enabled or self._stop_event.is_set():
            return
        info: WebhookEventInfo = getattr(event, "event_data", None)
        if (
            not info
            or str(info.channel).lower() != "emby"
            or str(info.event).lower() != "library.new"
        ):
            return
        server_name = str(info.server_name or "").strip()
        if self._webhook_source and server_name != self._webhook_source:
            return
        if self._mediaservers and server_name not in self._mediaservers:
            logger.debug(
                "忽略未选 Emby 实例的入库事件：%s", server_name or "未识别实例"
            )
            return

        item = (info.json_object or {}).get("Item") or {}
        item_type = str(item.get("Type") or info.item_type or "")
        if item_type not in {"Movie", "Episode", "Series"}:
            return
        raw_path = str(item.get("Path") or info.item_path or "").strip()
        if (
            not raw_path
            or self._is_excluded(raw_path)
            or not self._path_allowed(raw_path, self._realtime_paths)
        ):
            return
        if item_type == "Movie" and not self._movie_enabled:
            return
        if item_type in {"Episode", "Series"} and not self._tv_enabled:
            return

        target_path = self._realtime_target(Path(raw_path), item_type)
        identity = str(
            item.get("SeriesId") or item.get("Id") or info.item_id or target_path
        )
        key = f"{server_name.casefold()}::{item_type != 'Movie'}::{identity}"
        refresh_id = (
            item.get("SeriesId")
            if item_type == "Episode"
            else item.get("Id") or info.item_id
        )
        delay = (
            self._aggregate_seconds if item_type == "Episode" else self._delay_seconds
        )
        with self._lock:
            self._pending[key] = {
                "path": str(target_path),
                "server": server_name,
                "item_id": refresh_id,
            }
            old = self._timers.pop(key, None)
            if old:
                old.cancel()
            timer = threading.Timer(delay, self._process_pending, args=(key,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()
        logger.info(
            "Emby图片管理收到入库事件：%s，目标%s，延迟%s秒",
            raw_path,
            target_path,
            delay,
        )

    def _process_pending(self, key: str) -> None:
        """处理一个去重后的实时任务。"""
        with self._lock:
            task = self._pending.pop(key, None)
            self._timers.pop(key, None)
        if not task or self._stop_event.is_set() or not self._enabled:
            return
        path = Path(task["path"])
        if not path.exists():
            self._record_realtime(f"路径不存在：{path}")
            logger.warning("实时刮削路径不存在：%s", path)
            return
        if not self._scrape_path(path):
            self._record_realtime(f"刮削失败：{path.name}")
            return
        refreshed = self._refresh_emby(task.get("server"), task.get("item_id"), path)
        result = (
            f"已完成：{path.name}"
            if refreshed
            else f"刮削完成，Emby 刷新失败：{path.name}"
        )
        self._record_realtime(result)

    def _scrape_path(
        self, path: Path, chain: Optional[MediaChain] = None, mediainfo: Any = None
    ) -> bool:
        """调用 MoviePilot 识别和刮削指定文件或目录。"""
        try:
            chain = chain or MediaChain()
            if mediainfo is None:
                context = chain.recognize_by_path(str(path), obtain_images=True)
                mediainfo = context.media_info if context else None
            if not mediainfo:
                logger.warning("无法识别媒体：%s", path)
                return False
            item = schemas.FileItem(
                storage="local",
                type="dir" if path.is_dir() else "file",
                path=str(path),
                name=path.name,
                basename=path.stem,
                modify_time=path.stat().st_mtime,
            )
            chain.scrape_metadata(
                fileitem=item,
                mediainfo=mediainfo,
                overwrite=True,
                recursive=True,
            )
            logger.info("Emby图片管理刮削完成：%s", path)
            return True
        except Exception as err:
            logger.error("Emby图片管理刮削失败：%s - %s", path, err, exc_info=True)
            return False

    def run_audit(self) -> None:
        """扫描指定库，仅在发现简体中文候选后覆盖刮削。"""
        if not self._enabled or not self._audit_enabled or self._stop_event.is_set():
            return
        audit_lock = self._audit_lock
        stop_event = self._stop_event
        if not audit_lock.acquire(blocking=False):
            logger.warning("图片审计仍在运行，本次计划已跳过")
            return
        self._audit_running = True
        scanned = fixed = waiting = failed = 0
        skip_reason = ""
        states = self.get_data(self.DATA_STATES) or {}
        if not isinstance(states, dict):
            logger.warning("审计状态数据格式异常，已重建为空状态")
            states = {}
        try:
            roots = self._split_paths(self._audit_paths)
            if not roots:
                skip_reason = "未配置审计目录，未执行扫描"
                logger.warning("未配置审计目录，跳过图片审计")
                return
            for root in roots:
                if stop_event.is_set():
                    break
                root_path = Path(root)
                if not root_path.exists():
                    failed += 1
                    logger.warning("审计路径不存在：%s", root_path)
                    continue
                for media_path in self._discover_media_paths(root_path, stop_event):
                    if stop_event.is_set():
                        break
                    key = self._state_key(media_path)
                    if (states.get(key) or {}).get("status") == "fixed_zh":
                        continue
                    scanned += 1
                    try:
                        chain = MediaChain()
                        context = chain.recognize_by_path(
                            str(media_path), obtain_images=True
                        )
                        mediainfo = context.media_info if context else None
                        if not mediainfo or not self._media_type_allowed(mediainfo):
                            continue
                        selected = (
                            getattr(mediainfo, "_poster_priority_selection", {}) or {}
                        )
                        priority = str(selected.get("priority_key") or "")
                        now = datetime.now().isoformat(timespec="seconds")
                        if priority in self.SIMPLIFIED_IMAGE_PRIORITIES:
                            if self._scrape_path(
                                media_path, chain=chain, mediainfo=mediainfo
                            ):
                                states[key] = {
                                    "status": "fixed_zh",
                                    "last_audit": now,
                                    "priority": priority,
                                }
                                fixed += 1
                                self._refresh_emby(None, None, media_path)
                            else:
                                failed += 1
                        else:
                            states[key] = {
                                "status": "pending",
                                "last_audit": now,
                                "priority": priority or "none",
                            }
                            waiting += 1
                            logger.info(
                                "审计未发现简体图片，仅记录等待：%s", media_path
                            )
                    except Exception as err:
                        failed += 1
                        logger.warning(
                            "审计失败：%s - %s", media_path, err, exc_info=True
                        )
                    if scanned % 25 == 0:
                        self.save_data(self.DATA_STATES, states)
        finally:
            self.save_data(self.DATA_STATES, states)
            self._last_audit_at = self._now_text()
            stopped = stop_event.is_set()
            self._last_audit_result = skip_reason or (
                f"{'已中止' if stopped else '已完成'}：检查 {scanned}，补齐 {fixed}，等待 {waiting}，失败 {failed}"
            )
            self._audit_running = False
            audit_lock.release()

    def _discover_media_paths(
        self, root: Path, stop_event: Optional[threading.Event] = None
    ) -> List[Path]:
        """按媒体文件推断电影目录或电视剧根目录并稳定去重。"""
        found: Dict[str, Path] = {}
        for file_path in SystemUtils.list_files(root, settings.RMT_MEDIAEXT):
            if (stop_event and stop_event.is_set()) or self._is_excluded(
                str(file_path)
            ):
                continue
            path = Path(file_path)
            target = (
                self._realtime_target(path, "Episode")
                if self._season_index(path) is not None
                else path.parent
            )
            found.setdefault(self._state_key(target), target)
        return sorted(found.values(), key=lambda item: str(item).casefold())

    def _refresh_emby(
        self, server_name: Optional[str], item_id: Optional[str], path: Path
    ) -> bool:
        """刮削成功后刷新事件对应的 Emby 条目。"""
        try:
            filters = [server_name] if server_name else self._mediaservers
            services = MediaServerHelper().get_services(
                type_filter="emby", name_filters=filters or None
            )
            targets: List[ServiceInfo] = (
                [services.get(server_name)] if server_name else list(services.values())
            )
            targets = [service for service in targets if service and service.instance]
            if not targets:
                logger.warning(
                    "未找到可刷新的 Emby 实例：%s", server_name or "默认实例"
                )
                return False
            refreshed = 0
            for service in targets:
                try:
                    if item_id and hasattr(
                        service.instance, "_Emby__refresh_emby_library_by_id"
                    ):
                        service.instance._Emby__refresh_emby_library_by_id(item_id)
                    elif hasattr(service.instance, "refresh_library_by_items"):
                        service.instance.refresh_library_by_items(
                            [RefreshMediaItem(target_path=path)]
                        )
                    else:
                        continue
                    refreshed += 1
                except Exception as err:
                    logger.warning("刷新单个 Emby 实例失败：%s", err, exc_info=True)
            if not refreshed:
                logger.warning("Emby 实例不支持媒体刷新：%s", server_name or "默认实例")
                return False
            logger.info("Emby图片管理已请求刷新：%s", path)
            return True
        except Exception as err:
            logger.warning("刷新 Emby 失败：%s", err, exc_info=True)
            return False

    def _media_type_allowed(self, mediainfo: Any) -> bool:
        """判断识别出的媒体类型是否在配置范围内。"""
        media_type = getattr(mediainfo, "type", None)
        if media_type == MediaType.MOVIE or str(media_type).lower() in {
            "movie",
            "电影",
            "mediatype.movie",
        }:
            return self._movie_enabled
        if media_type == MediaType.TV or str(media_type).lower() in {
            "tv",
            "电视剧",
            "mediatype.tv",
        }:
            return self._tv_enabled
        return False

    @classmethod
    def _realtime_target(cls, path: Path, item_type: str) -> Path:
        """把剧集事件归并到剧目录，电影保持原事件路径。"""
        if item_type != "Episode":
            return path
        index = cls._season_index(path)
        if index is not None and index > 0:
            return Path(*path.parts[:index])
        return path.parent

    @staticmethod
    def _season_index(path: Path) -> Optional[int]:
        """返回路径中 Season/季目录的位置。"""
        markers = {"season", "specials", "特别篇", "特典"}
        for index, part in enumerate(path.parts):
            normalized = part.strip().casefold()
            if (
                normalized.startswith("season")
                or normalized.startswith("第")
                and normalized.endswith("季")
                or normalized in markers
            ):
                return index
        return None

    def _path_allowed(self, path: str, configured: str) -> bool:
        """判断路径是否位于配置的目录范围内。"""
        roots = self._split_paths(configured)
        return not roots or any(self._is_under(path, root) for root in roots)

    def _is_excluded(self, path: str) -> bool:
        """判断路径是否位于排除目录内。"""
        return any(
            self._is_under(path, root)
            for root in self._split_paths(self._exclude_paths)
        )

    @staticmethod
    def _is_under(path: str, root: str) -> bool:
        """以跨平台、忽略大小写的方式判断目录包含关系。"""
        if not root:
            return False
        try:
            return Path(path).resolve().is_relative_to(Path(root).resolve())
        except (OSError, RuntimeError, ValueError):
            normalized_path = str(path).replace("\\", "/").rstrip("/").casefold()
            normalized_root = str(root).replace("\\", "/").rstrip("/").casefold()
            return normalized_path == normalized_root or normalized_path.startswith(
                f"{normalized_root}/"
            )

    @staticmethod
    def _split_paths(value: str) -> List[str]:
        """解析多行路径配置，保序去重。"""
        return list(
            dict.fromkeys(
                line.strip() for line in str(value or "").splitlines() if line.strip()
            )
        )

    @staticmethod
    def _state_key(path: Path) -> str:
        """生成适合跨次审计复用的路径键。"""
        return str(path).replace("\\", "/").rstrip("/").casefold()

    @staticmethod
    def _safe_nonnegative_int(value: Any, default: int, label: str) -> int:
        """解析非负整数，非法配置回退默认值。"""
        try:
            return max(0, int(value if value not in (None, "") else default))
        except (TypeError, ValueError):
            logger.warning("%s配置无效：%r，已使用默认值 %s", label, value, default)
            return default

    @classmethod
    def _safe_cron(cls, value: Any) -> str:
        """校验五段式 cron，非法配置回退默认计划。"""
        cron = str(value or cls.DEFAULT_AUDIT_CRON).strip()
        try:
            CronTrigger.from_crontab(cron)
            return cron
        except (TypeError, ValueError) as err:
            logger.warning(
                "审计计划无效：%r（%s），已使用默认值 %s",
                cron,
                err,
                cls.DEFAULT_AUDIT_CRON,
            )
            return cls.DEFAULT_AUDIT_CRON

    def _record_realtime(self, result: str) -> None:
        """记录最近一次实时处理结果。"""
        self._last_realtime_at = self._now_text()
        self._last_realtime_result = result

    @staticmethod
    def _now_text() -> str:
        """返回适合页面展示的本地时间。"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
