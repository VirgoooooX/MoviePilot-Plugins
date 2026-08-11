"""Emby 入库实时刮削与媒体图片审计插件。"""

from __future__ import annotations

import threading
import time
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
    plugin_version = "1.2.1"
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
    _realtime_libraries: List[str] = []
    _audit_libraries: List[str] = []

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
        # 旧版只有一个 media_libraries，迁移到审计范围；实时范围按新约定默认全量。
        legacy_libraries = (
            config.get("media_libraries", config.get("libraries", [])) or []
        )
        realtime_libraries = config.get("realtime_libraries", []) or []
        audit_libraries = config.get("audit_libraries", legacy_libraries) or []
        realtime_values = (
            realtime_libraries
            if isinstance(realtime_libraries, list)
            else str(realtime_libraries).splitlines()
        )
        audit_values = (
            audit_libraries
            if isinstance(audit_libraries, list)
            else str(audit_libraries).splitlines()
        )
        self._realtime_libraries = list(
            dict.fromkeys(
                str(item).strip() for item in realtime_values if str(item).strip()
            )
        )
        self._audit_libraries = list(
            dict.fromkeys(
                str(item).strip() for item in audit_values if str(item).strip()
            )
        )
        self._lock = threading.RLock()
        # 保留同一把审计锁，避免插件热重载时新旧审计并行。
        self._audit_lock = getattr(self, "_audit_lock", threading.Lock())
        self._stop_event = threading.Event()
        self._pending: Dict[str, dict] = {}
        self._timers: Dict[str, threading.Timer] = {}
        self._library_catalog: Dict[str, dict] = {}
        self._library_catalog_at = 0.0
        self._last_realtime_at = ""
        self._last_realtime_result = "尚未处理入库事件"
        self._last_audit_at = ""
        self._last_audit_result = "尚未执行图片审计"
        self._audit_running = False

    def _load_library_catalog(self, force: bool = False) -> Dict[str, dict]:
        """读取 Emby 媒体库及其物理路径，短时缓存避免每个 Webhook 都请求服务。"""
        now = time.monotonic()
        if not force and now - getattr(self, "_library_catalog_at", 0.0) < 300:
            return getattr(self, "_library_catalog", {})

        catalog: Dict[str, dict] = {}
        try:
            services = (
                MediaServerHelper().get_services(
                    type_filter="emby", name_filters=self._mediaservers or None
                )
                or {}
            )
            for server_name, service in services.items():
                instance = getattr(service, "instance", None)
                if not instance or not hasattr(instance, "get_librarys"):
                    continue
                try:
                    libraries = instance.get_librarys() or []
                except Exception as err:
                    logger.warning(
                        "读取 Emby 实例 %s 的媒体库失败：%s", server_name, err
                    )
                    continue
                for library in libraries:
                    library_id = self._library_field(
                        library, "id"
                    ) or self._library_field(library, "item_id")
                    name = str(self._library_field(library, "name") or "").strip()
                    if not library_id or not name:
                        continue
                    key = self._library_key(server_name, library_id)
                    catalog[key] = {
                        "server": str(server_name),
                        "id": str(library_id),
                        "name": name,
                        "type": str(self._library_field(library, "type") or "媒体库"),
                        "item_count": self._library_field(library, "item_count"),
                        "paths": self._normalize_library_paths(
                            self._library_field(library, "path")
                        ),
                    }
        except Exception as err:
            logger.warning("读取 Emby 媒体库失败：%s", err, exc_info=True)
        self._library_catalog = catalog
        self._library_catalog_at = now
        return catalog

    def _library_options(self, selected: Optional[List[str]] = None) -> List[dict]:
        """生成配置页的媒体库选择项。"""
        catalog = self._load_library_catalog(force=True)
        options = []
        for key, library in sorted(
            catalog.items(),
            key=lambda item: (item[1]["server"].casefold(), item[1]["name"].casefold()),
        ):
            if library["type"].casefold() not in {
                "电影",
                "电视剧",
                "movie",
                "series",
                "tv",
            }:
                continue
            count = self._library_field(library, "item_count")
            suffix = f" · {count} 项" if count not in (None, "", 0) else ""
            options.append(
                {
                    "title": f"{library['server']} / {library['name']}（{library['type']}）{suffix}",
                    "value": key,
                }
            )
        known = {item["value"] for item in options}
        selected_keys = selected or list(
            dict.fromkeys(self._realtime_libraries + self._audit_libraries)
        )
        for key in selected_keys:
            if key not in known:
                options.append(
                    {"title": f"已保存的媒体库（暂时无法读取）：{key}", "value": key}
                )
        return options

    def _selected_library_paths(
        self, server_name: str, selected: Optional[List[str]] = None
    ) -> Tuple[bool, List[str]]:
        """返回当前实例是否启用了媒体库筛选及其路径。"""
        selected_libraries = (
            selected if selected is not None else self._realtime_libraries
        )
        if not selected_libraries:
            return False, []
        catalog = self._load_library_catalog()
        paths: List[str] = []
        for key in selected_libraries:
            library = catalog.get(key)
            if library and library.get("server") == server_name:
                paths.extend(library.get("paths") or [])
        return True, self._split_paths("\n".join(paths))

    def _event_matches_selected_libraries(
        self,
        server_name: str,
        raw_path: str,
        item: dict,
        info: WebhookEventInfo,
    ) -> bool:
        """按路径优先、库 ID/祖先关系兜底判断实时事件所属媒体库。"""
        selected = self._realtime_libraries
        if not selected:
            return True

        catalog = self._load_library_catalog()
        records = [
            catalog[key]
            for key in selected
            if key in catalog and catalog[key].get("server") == server_name
        ]
        selected_ids = {
            self._library_id_from_key(key)
            for key in selected
            if self._library_server_from_key(key) == server_name
        }
        selected_ids.discard("")
        selected_names = {
            str(record.get("name") or "").casefold()
            for record in records
            if record.get("name")
        }
        roots = [path for record in records for path in record.get("paths") or []]
        if roots and any(self._is_under(raw_path, root) for root in roots):
            return True

        event_ids = {
            str(item.get(field) or "").strip()
            for field in ("LibraryId", "TopParentId", "ParentId", "ParentItemId")
        }
        event_ids.update(
            str(getattr(info, field, "") or "").strip()
            for field in ("library_id", "top_parent_id", "parent_id")
            if hasattr(info, field)
        )
        if selected_ids.intersection(event_ids):
            logger.debug(
                "实时事件通过 Emby 媒体库 ID 匹配：server=%s, item=%s",
                server_name,
                item.get("Id") or info.item_id,
            )
            return True

        event_library_names = {
            str(item.get(field) or "").strip().casefold()
            for field in ("LibraryName", "TopParentName", "ParentName")
        }
        if selected_names.intersection(event_library_names):
            return True

        item_id = str(item.get("Id") or info.item_id or "").strip()
        if not item_id or not selected_ids:
            logger.warning(
                "实时事件无法确认媒体库，已跳过：server=%s, path=%s, selected=%s",
                server_name,
                raw_path,
                selected,
            )
            return False
        if self._emby_item_in_selected_libraries(
            server_name, item_id, selected_ids, selected_names
        ):
            logger.debug(
                "实时事件通过 Emby Ancestors 兜底匹配：server=%s, item=%s",
                server_name,
                item_id,
            )
            return True
        logger.warning(
            "实时事件不属于已选媒体库，已跳过：server=%s, item=%s, path=%s",
            server_name,
            item_id,
            raw_path,
        )
        return False

    def _emby_item_in_selected_libraries(
        self,
        server_name: str,
        item_id: str,
        selected_ids: set[str],
        selected_names: set[str],
    ) -> bool:
        """通过 Emby Ancestors API 确认条目顶级媒体库。"""
        try:
            services = (
                MediaServerHelper().get_services(
                    type_filter="emby", name_filters=[server_name]
                )
                or {}
            )
            service = services.get(server_name)
            instance = getattr(service, "instance", None) if service else None
            if not instance or not hasattr(instance, "get_data"):
                return False
            response = instance.get_data(
                url=f"[HOST]emby/Items/{item_id}/Ancestors?api_key=[APIKEY]"
            )
            status_code = getattr(response, "status_code", 200)
            if status_code != 200:
                return False
            payload = response.json() if hasattr(response, "json") else response
            if isinstance(payload, dict):
                payload = payload.get("Items") or payload.get("Ancestors") or []
            if not isinstance(payload, list):
                return False
            for ancestor in payload:
                if not isinstance(ancestor, dict):
                    continue
                ancestor_id = str(ancestor.get("Id") or ancestor.get("id") or "")
                ancestor_name = str(
                    ancestor.get("Name") or ancestor.get("name") or ""
                ).casefold()
                if ancestor_id in selected_ids or ancestor_name in selected_names:
                    return True
        except Exception as err:
            logger.warning(
                "查询 Emby 媒体库祖先关系失败：server=%s, item=%s, error=%s",
                server_name,
                item_id,
                err,
            )
        return False

    @staticmethod
    def _library_server_from_key(value: str) -> str:
        """从媒体库配置值中取出服务器名。"""
        return str(value).partition("::")[0].strip()

    @staticmethod
    def _library_id_from_key(value: str) -> str:
        """从媒体库配置值中取出 Emby 媒体库 ID。"""
        return str(value).partition("::")[2].strip()

    def _audit_roots(
        self, stop_event: Optional[threading.Event] = None
    ) -> List[Tuple[Path, List[str]]]:
        """返回审计根目录及对应实例，库路径为空时按媒体库 ID 查询条目路径。"""
        roots: Dict[str, Dict[str, Any]] = {}
        if self._audit_libraries:
            catalog = self._load_library_catalog()
            for key in self._audit_libraries:
                library = catalog.get(key) or {
                    "server": self._library_server_from_key(key),
                    "id": self._library_id_from_key(key),
                    "paths": [],
                }
                paths = list(library.get("paths") or [])
                recovered_paths = []
                if not paths or not any(Path(path).exists() for path in paths):
                    recovered_paths = self._emby_library_item_paths(
                        library["server"], library["id"], stop_event
                    )
                paths = list(dict.fromkeys(paths + recovered_paths))
                for path in paths:
                    state = roots.setdefault(
                        self._state_key(Path(path)), {"path": path, "servers": set()}
                    )
                    state["servers"].add(library["server"])
        else:
            for path in self._split_paths(self._audit_paths):
                roots.setdefault(
                    self._state_key(Path(path)), {"path": path, "servers": set()}
                )
        return [
            (Path(state["path"]), sorted(state["servers"])) for state in roots.values()
        ]

    def _emby_library_item_paths(
        self,
        server_name: str,
        library_id: str,
        stop_event: Optional[threading.Event] = None,
    ) -> List[str]:
        """媒体库对象没有 path 时，从 Emby 条目列表恢复本地媒体路径。"""
        if not library_id or (stop_event and stop_event.is_set()):
            return []
        include_types = []
        if self._movie_enabled:
            include_types.append("Movie")
        if self._tv_enabled:
            include_types.append("Series")
        if not include_types:
            return []
        try:
            services = (
                MediaServerHelper().get_services(
                    type_filter="emby", name_filters=[server_name]
                )
                or {}
            )
            service = services.get(server_name)
            instance = getattr(service, "instance", None) if service else None
            if not instance or not hasattr(instance, "get_data"):
                return []
            response = instance.get_data(
                url=(
                    f"[HOST]emby/Users/[USER]/Items?ParentId={library_id}"
                    f"&Recursive=true&IncludeItemTypes={','.join(include_types)}"
                    "&Fields=Path,Type&Limit=100000&api_key=[APIKEY]"
                )
            )
            status_code = getattr(response, "status_code", 200)
            if status_code != 200:
                logger.warning(
                    "读取 Emby 媒体库条目失败：server=%s, library=%s, status=%s",
                    server_name,
                    library_id,
                    status_code,
                )
                return []
            payload = response.json() if hasattr(response, "json") else response
            items = payload.get("Items", []) if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                return []
            paths: Dict[str, str] = {}
            for item in items:
                if stop_event and stop_event.is_set():
                    break
                if not isinstance(item, dict):
                    continue
                target = self._library_item_target_path(item)
                if target:
                    paths.setdefault(self._state_key(Path(target)), target)
            logger.info(
                "从 Emby 条目恢复媒体库路径：server=%s, library=%s, count=%s",
                server_name,
                library_id,
                len(paths),
            )
            return list(paths.values())
        except Exception as err:
            logger.warning(
                "从 Emby 条目恢复媒体库路径失败：server=%s, library=%s, error=%s",
                server_name,
                library_id,
                err,
            )
            return []

    @staticmethod
    def _library_item_target_path(item: dict) -> str:
        """把 Emby 电影/剧集条目的 Path 归一为可扫描目录。"""
        raw_path = str(item.get("Path") or item.get("path") or "").strip()
        if not raw_path:
            return ""
        item_type = str(item.get("Type") or item.get("type") or "").casefold()
        path = Path(raw_path)
        extensions = {
            str(extension).casefold()
            for extension in getattr(settings, "RMT_MEDIAEXT", [])
        }
        if item_type == "movie" and path.suffix.casefold() in extensions:
            path = path.parent
        return str(path)

    @staticmethod
    def _library_field(value: Any, key: str, default: Any = None) -> Any:
        """兼容 Pydantic 媒体库对象和旧版字典返回值。"""
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    @staticmethod
    def _normalize_library_paths(value: Any) -> List[str]:
        """把媒体库的单路径/多路径字段统一为多行路径。"""
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple, set)):
            values = list(value)
        else:
            values = []
        return list(
            dict.fromkeys(str(item).strip() for item in values if str(item).strip())
        )

    @staticmethod
    def _library_key(server_name: str, library_id: Any) -> str:
        """生成跨媒体服务器不冲突的媒体库配置值。"""
        return f"{str(server_name).strip()}::{str(library_id).strip()}"

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
        """返回实时刮削与周期审计分离的 Tab 配置表单。"""
        try:
            server_items = [
                {"title": config.name, "value": config.name}
                for config in MediaServerHelper().get_configs().values()
                if config.type == "emby"
            ]
        except Exception as err:
            logger.warning("读取 Emby 服务配置失败：%s", err)
            server_items = []

        library_items = self._library_options()
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
            "realtime_libraries": [],
            "audit_libraries": [],
            "realtime_paths": "",
            "audit_paths": "",
            "exclude_paths": "",
            "active_tab": "realtime",
        }
        realtime_content = [
            {
                "component": "VAlert",
                "props": {
                    "type": "success",
                    "variant": "tonal",
                    "title": "实时处理全部新入库媒体",
                    "text": "默认覆盖所选 Emby 实例的全部媒体库；如果只想限制范围，再选择具体媒体库。",
                    "class": "mb-4",
                },
            },
            self._form_col(
                "realtime_enabled",
                "启用新入库实时刮削",
                "收到 MoviePilot 的 Emby 入库事件后延迟处理。",
                12,
            ),
            self._library_select(
                "realtime_libraries",
                "实时处理媒体库（可选）",
                "留空表示处理所选 Emby 实例的全部媒体库。",
                library_items,
            ),
            {
                "component": "VRow",
                "content": [
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
            self._path_field(
                "realtime_paths",
                "实时目录兜底（可选）",
                "只有没有选择实时媒体库时才使用；每行一个宿主可访问路径。",
            ),
        ]
        audit_content = [
            {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "title": "只审计明确选择的外语媒体库",
                    "text": "建议只选择外语电影库、外语剧库；不选择媒体库时不会扫描，除非填写审计目录兜底。",
                    "class": "mb-4",
                },
            },
            self._form_col(
                "audit_enabled",
                "启用周期图片审计",
                "按计划检查所选审计媒体库中的简体中文图片候选。",
                12,
            ),
            self._library_select(
                "audit_libraries",
                "周期审计媒体库（建议必选）",
                "只选择需要补中文图片的外语库；留空且未填写目录兜底时不会扫描。",
                library_items,
            ),
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
                    self._path_field(
                        "audit_paths",
                        "审计目录兜底（可选）",
                        "只有没有选择审计媒体库时才使用；每行一个宿主可访问路径。",
                        md=7,
                    ),
                ],
            },
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "title": "实时与审计各管各的",
                            "text": "实时刮削面向全部新入库媒体；周期审计面向少量需要补中文图片的外语库。两套媒体库选择互不影响。",
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
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
                                            "hint": "留空接收全部 Emby 实例；选择后同时限制实时事件和审计刷新目标。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            self._form_col(
                                "movie_enabled",
                                "处理电影",
                                "同时应用于实时刮削和周期审计。",
                                6,
                            ),
                            self._form_col(
                                "tv_enabled",
                                "处理电视剧",
                                "同一剧集的连续入库事件会合并为一次。",
                                6,
                            ),
                        ],
                    },
                    {
                        "component": "VTabs",
                        "props": {
                            "model": "active_tab",
                            "color": "primary",
                            "grow": True,
                            "class": "mt-3",
                        },
                        "content": [
                            {
                                "component": "VTab",
                                "props": {
                                    "value": "realtime",
                                    "prepend-icon": "mdi-flash-outline",
                                },
                                "text": "实时刮削",
                            },
                            {
                                "component": "VTab",
                                "props": {
                                    "value": "audit",
                                    "prepend-icon": "mdi-calendar-search-outline",
                                },
                                "text": "周期审计",
                            },
                        ],
                    },
                    {
                        "component": "VWindow",
                        "props": {"model": "active_tab", "class": "mt-4"},
                        "content": [
                            {
                                "component": "VWindowItem",
                                "props": {"value": "realtime"},
                                "content": realtime_content,
                            },
                            {
                                "component": "VWindowItem",
                                "props": {"value": "audit"},
                                "content": audit_content,
                            },
                        ],
                    },
                    {
                        "component": "VExpansionPanels",
                        "props": {"variant": "accordion", "class": "mt-4"},
                        "content": [
                            {
                                "component": "VExpansionPanel",
                                "content": [
                                    {
                                        "component": "VExpansionPanelTitle",
                                        "text": "高级：公共排除目录",
                                    },
                                    {
                                        "component": "VExpansionPanelText",
                                        "content": [
                                            {
                                                "component": "div",
                                                "props": {
                                                    "class": "text-body-2 text-medium-emphasis mb-3"
                                                },
                                                "text": "媒体库选择之外的统一排除项，同时应用于实时刮削和周期审计；通常可以留空。",
                                            },
                                            self._path_field(
                                                "exclude_paths",
                                                "排除目录（可选）",
                                                "每行一个宿主可访问路径。",
                                            ),
                                        ],
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
        ], defaults

    @staticmethod
    def _library_select(model: str, label: str, hint: str, items: List[dict]) -> dict:
        """生成媒体库多选控件。"""
        return {
            "component": "VCol",
            "props": {"cols": 12},
            "content": [
                {
                    "component": "VSelect",
                    "props": {
                        "model": model,
                        "label": label,
                        "items": items,
                        "item-title": "title",
                        "item-value": "value",
                        "multiple": True,
                        "chips": True,
                        "closable-chips": True,
                        "variant": "outlined",
                        "hint": hint,
                        "persistent-hint": True,
                        "no-data-text": "未读取到可用 Emby 媒体库，请先检查实例连接。",
                    },
                }
            ],
        }

    @staticmethod
    def _path_field(
        model: str, label: str, hint: str, md: Optional[int] = None
    ) -> dict:
        """生成路径兜底文本域，可嵌入响应式列。"""
        field = {
            "component": "VTextarea",
            "props": {
                "model": model,
                "label": label,
                "rows": 3,
                "auto-grow": True,
                "variant": "outlined",
                "hint": hint,
                "persistent-hint": True,
            },
        }
        if md:
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": md},
                "content": [field],
            }
        return field

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
        realtime_scope = (
            f"实时库 {len(self._realtime_libraries)} 个"
            if self._realtime_libraries
            else "实时库：全部"
        )
        audit_scope = (
            f"审计库 {len(self._audit_libraries)} 个"
            if self._audit_libraries
            else "审计库：未选择"
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
                                    {
                                        "component": "VChip",
                                        "props": {"variant": "tonal"},
                                        "text": realtime_scope,
                                    },
                                    {
                                        "component": "VChip",
                                        "props": {"variant": "tonal"},
                                        "text": audit_scope,
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
        in_legacy_paths = self._path_allowed(raw_path, self._realtime_paths)
        if (
            not raw_path
            or self._is_excluded(raw_path)
            or (
                self._realtime_libraries
                and not self._event_matches_selected_libraries(
                    server_name, raw_path, item, info
                )
            )
            or (not self._realtime_libraries and not in_legacy_paths)
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
            roots = self._audit_roots(stop_event)
            if not roots:
                skip_reason = "未配置媒体库或审计目录，未执行扫描"
                logger.warning("未配置媒体库或审计目录，跳过图片审计")
                return
            for root_path, root_servers in roots:
                if stop_event.is_set():
                    break
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
                                for server_name in root_servers or [None]:
                                    self._refresh_emby(server_name, None, media_path)
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
