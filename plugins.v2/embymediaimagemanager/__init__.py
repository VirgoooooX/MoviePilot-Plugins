"""Emby 入库实时刮削、存量图片检查与合集图片管理插件。"""

from __future__ import annotations

import base64
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from apscheduler.triggers.cron import CronTrigger
from fastapi import Body

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

try:
    from app.utils.http import RequestUtils
except ImportError:  # pragma: no cover - 最小单元测试桩不提供 HTTP 模块
    RequestUtils = None

try:
    from .collection_artwork import (
        DEFAULT_PRIORITY as COLLECTION_DEFAULT_PRIORITY,
        normalize_fanart_payload,
        normalize_priority,
        priority_preview,
        select_collection_images,
    )
except (ImportError, ValueError):  # pragma: no cover - 直接按文件加载插件时使用
    import importlib.util

    _collection_artwork_path = Path(__file__).with_name("collection_artwork.py")
    _collection_artwork_spec = importlib.util.spec_from_file_location(
        "embymediaimagemanager_collection_artwork", _collection_artwork_path
    )
    if not _collection_artwork_spec or not _collection_artwork_spec.loader:
        raise ImportError("无法加载合集图片选择模块")
    _collection_artwork_module = importlib.util.module_from_spec(
        _collection_artwork_spec
    )
    _collection_artwork_spec.loader.exec_module(_collection_artwork_module)
    COLLECTION_DEFAULT_PRIORITY = _collection_artwork_module.DEFAULT_PRIORITY
    normalize_fanart_payload = _collection_artwork_module.normalize_fanart_payload
    normalize_priority = _collection_artwork_module.normalize_priority
    priority_preview = _collection_artwork_module.priority_preview
    select_collection_images = _collection_artwork_module.select_collection_images


class EmbyMediaImageManager(_PluginBase):
    """处理 Emby 外部入库实时刮削、存量图片检查与合集图片。"""

    plugin_name = "Emby媒体图片管理"
    plugin_desc = "Emby入库后实时刮削、检查存量图片，并刷新现有合集封面与徽标。"
    plugin_icon = "image-search-outline"
    plugin_version = "1.3.3"
    plugin_author = "VirgoooooX"
    author_url = "https://github.com/VirgoooooX/MoviePilot-Plugins"
    plugin_label = "媒体服务器,元数据"
    plugin_config_prefix = "embymediaimagemanager_"
    plugin_order = 30
    auth_level = 1

    _AUDIT_NOW_JS = """async (event) => {
  model.audit_feedback = '正在启动存量图片检查…';
  try {
    const res = await window.MoviePilotAPI.post('plugin/__PLUGIN_ID__/image-check/scan', {});
    model.audit_feedback = (res && res.message) ? res.message : '存量图片检查已开始';
  } catch (err) {
    model.audit_feedback = '启动失败：' + ((err && err.message) ? err.message : String(err));
  }
}"""
    _COLLECTION_SCAN_JS = """async (event) => {
  model.collection_feedback = '正在启动合集图片刷新…';
  try {
    const res = await window.MoviePilotAPI.post('plugin/__PLUGIN_ID__/collection-artwork/scan', {
      server: model.collection_artwork_server || '',
      scope: model.collection_artwork_scope || 'all',
      libraries: model.collection_artwork_libraries || [],
      collections: model.collection_artwork_collections || [],
      overwrite_poster: !!model.collection_artwork_overwrite_poster,
      overwrite_logo: !!model.collection_artwork_overwrite_logo
    });
    model.collection_feedback = (res && res.message) ? res.message : '合集图片刷新已开始';
  } catch (err) {
    model.collection_feedback = '启动失败：' + ((err && err.message) ? err.message : String(err));
  }
}"""
    _COLLECTION_CANCEL_JS = """async (event) => {
  try {
    const res = await window.MoviePilotAPI.post('plugin/__PLUGIN_ID__/collection-artwork/cancel', {});
    model.collection_feedback = (res && res.message) ? res.message : '已请求取消';
  } catch (err) {
    model.collection_feedback = '取消失败：' + ((err && err.message) ? err.message : String(err));
  }
}"""

    DEFAULT_AUDIT_CRON = "0 4 1 * *"
    SIMPLIFIED_IMAGE_PRIORITIES = {
        "tmdb_zh_cn",
        "tmdb_zh_sg",
        "fanart_chinese",
    }
    DATA_STATES = "states"
    DATA_COLLECTION_JOB = "collection_artwork_job"
    COLLECTION_IMAGE_LANGUAGES = "zh-CN,zh-SG,zh,en-US,en,null"

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
    _emby_path_prefix = ""
    _local_path_prefix = ""
    _collection_server = ""
    _collection_scope = "all"
    _collection_libraries: List[str] = []
    _collection_ids: List[str] = []
    _collection_overwrite_poster = False
    _collection_overwrite_logo = False

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
        self._emby_path_prefix = str(config.get("emby_path_prefix") or "").strip()
        self._local_path_prefix = str(config.get("local_path_prefix") or "").strip()
        self._collection_server = str(
            config.get("collection_artwork_server")
            or config.get("collection_server")
            or ""
        ).strip()
        scope = str(
            config.get("collection_artwork_scope")
            or config.get("collection_scope")
            or "all"
        ).strip().lower()
        self._collection_scope = scope if scope in {"all", "libraries", "collections"} else "all"
        collection_libraries = config.get("collection_artwork_libraries") or []
        collection_ids = config.get("collection_artwork_collections") or config.get(
            "collection_artwork_ids"
        ) or []
        self._collection_libraries = self._dedupe_values(collection_libraries)
        self._collection_ids = self._dedupe_values(collection_ids)
        self._collection_overwrite_poster = bool(
            config.get("collection_artwork_overwrite_poster", False)
        )
        self._collection_overwrite_logo = bool(
            config.get("collection_artwork_overwrite_logo", False)
        )
        value = config.get("mediaservers") or []
        values = value if isinstance(value, list) else str(value).splitlines()
        self._mediaservers = list(
            dict.fromkeys(str(item).strip() for item in values if str(item).strip())
        )
        # 旧版只有一个 media_libraries，迁移到存量图片检查范围；实时范围按新约定默认全量。
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
        # 保留同一把检查锁，避免插件热重载时新旧检查并行。
        self._audit_lock = getattr(self, "_audit_lock", threading.Lock())
        self._stop_event = threading.Event()
        self._pending: Dict[str, dict] = {}
        self._timers: Dict[str, threading.Timer] = {}
        self._library_catalog: Dict[str, dict] = {}
        self._library_catalog_at = 0.0
        self._last_realtime_at = ""
        self._last_realtime_result = "尚未处理入库事件"
        self._last_audit_at = ""
        self._last_audit_result = "尚未执行存量图片检查"
        self._audit_running = False
        previous_audit_worker = getattr(self, "_audit_worker", None)
        if previous_audit_worker and previous_audit_worker.is_alive():
            # 热重载时保留仍在退出中的线程引用，避免立即启动第二次存量检查。
            self._audit_worker = previous_audit_worker
        else:
            self._audit_worker = None
        self._collection_lock = getattr(self, "_collection_lock", threading.RLock())
        previous_worker = getattr(self, "_collection_worker", None)
        previous_stop_event = getattr(self, "_collection_stop_event", None)
        if previous_worker and previous_worker.is_alive():
            # 热重载等待超时后不能丢掉旧线程引用，否则新配置可能再启动第二个写入任务。
            self._collection_worker = previous_worker
            self._collection_stop_event = previous_stop_event or threading.Event()
        else:
            self._collection_worker = None
            self._collection_stop_event = threading.Event()

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
        """返回存量检查根目录及对应实例，库路径为空时按媒体库 ID 查询条目路径。"""
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
                for raw_path in paths:
                    path = self._map_emby_path(raw_path)
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
        return [
            {
                "path": "/image-check/scan",
                "endpoint": self.api_start_image_check,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即检查存量图片",
                "description": "立即后台执行一次存量图片检查，与定时检查共用互斥锁。",
            },
            {
                "path": "/collection-artwork/scan",
                "endpoint": self.api_start_collection_artwork_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "刷新合集图片",
                "description": "只更新现有 Emby 合集的 poster 和 Logo，不改合集成员。",
            },
            {
                "path": "/collection-artwork/status",
                "endpoint": self.api_collection_artwork_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取合集图片任务状态",
            },
            {
                "path": "/collection-artwork/cancel",
                "endpoint": self.api_cancel_collection_artwork,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "取消合集图片任务",
            },
        ]

    @staticmethod
    def _dedupe_values(value: Any) -> List[str]:
        """把配置中的字符串/数组规范成稳定去重的字符串数组。"""
        values = value if isinstance(value, (list, tuple, set)) else str(value or "").splitlines()
        return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))

    def _collection_services(self) -> Dict[str, Any]:
        """返回可用 Emby 实例，合集图片任务只在这些实例中读取现有合集。"""
        try:
            services = MediaServerHelper().get_services(type_filter="emby") or {}
        except Exception as err:
            logger.warning("读取合集图片的 Emby 实例失败：%s", err)
            return {}
        result = {}
        for name, service in services.items():
            instance = getattr(service, "instance", None)
            try:
                inactive = bool(instance and instance.is_inactive())
            except Exception:
                inactive = False
            if service and instance and not inactive:
                result[str(name)] = service
        return result

    def _resolve_collection_service(
        self, server_name: Optional[str] = None
    ) -> Tuple[str, Any]:
        """按请求、配置或唯一实例解析合集图片任务目标。"""
        services = self._collection_services()
        selected = str(server_name or self._collection_server or "").strip()
        if not selected and len(services) == 1:
            selected = next(iter(services))
        return selected, services.get(selected)

    def _collection_priority(self) -> List[str]:
        """读取 TMDB/Fanart 海报优先插件的真实配置，不复制一份配置。"""
        priority = None
        configured = False
        try:
            config = self.get_config("TmdbPosterLanguagePriority")
            if isinstance(config, dict):
                configured = "priority" in config
                priority = config.get("priority")
        except Exception as err:
            logger.debug("读取 TMDB/Fanart 海报优先配置失败，使用默认顺序：%s", err)
        normalized = normalize_priority(priority)
        order = normalized if configured else COLLECTION_DEFAULT_PRIORITY.copy()
        # 合集图片固定排除繁体地区，避免旧版海报优先配置中的 tw/hk 影响合集封面。
        return [item for item in order if item not in {"tmdb_zh_tw", "tmdb_zh_hk"}]

    def _collection_priority_text(self) -> str:
        """返回配置页使用的当前图片优先级预览。"""
        return f"{priority_preview(self._collection_priority())}（合集图片自动排除 zh-TW、zh-HK 繁体地区）"

    def _collection_boxset_options(
        self, server_name: Optional[str] = None, selected: Optional[List[str]] = None
    ) -> List[dict]:
        """生成现有 Emby 合集选择项；读取失败时保留已保存的 ID。"""
        name, service = self._resolve_collection_service(server_name)
        options: List[dict] = []
        try:
            boxsets = self._load_collection_boxsets(service) if service else []
        except Exception as err:
            logger.warning("读取 Emby 合集选项失败：%s", err)
            boxsets = []
        for item in sorted(
            boxsets,
            key=lambda row: str(row.get("Name") or row.get("Id") or "").casefold(),
        ):
            item_id = str(item.get("Id") or item.get("id") or "").strip()
            if not item_id:
                continue
            tmdb_id = self._collection_tmdb_id(item) or "未识别 TMDB"
            options.append(
                {
                    "title": f"{item.get('Name') or item_id} · TMDB {tmdb_id}",
                    "value": item_id,
                }
            )
        known = {item["value"] for item in options}
        for item_id in selected or self._collection_ids:
            if item_id not in known:
                options.append(
                    {"title": f"已保存的合集（当前无法读取）：{item_id}", "value": item_id}
                )
        if name and not options and selected:
            return [
                {"title": f"已保存的合集（{name}）：{item_id}", "value": item_id}
                for item_id in selected
            ]
        return options

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回实时处理、存量图片检查与合集图片分离的 Tab 配置表单。"""
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
        plugin_id = self.__class__.__name__
        audit_now_js = self._AUDIT_NOW_JS.replace("__PLUGIN_ID__", plugin_id)
        collection_scan_js = self._COLLECTION_SCAN_JS.replace("__PLUGIN_ID__", plugin_id)
        collection_cancel_js = self._COLLECTION_CANCEL_JS.replace("__PLUGIN_ID__", plugin_id)
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
            "emby_path_prefix": "",
            "local_path_prefix": "",
            "collection_artwork_server": "",
            "collection_artwork_scope": "all",
            "collection_artwork_libraries": [],
            "collection_artwork_collections": [],
            "collection_artwork_overwrite_poster": False,
            "collection_artwork_overwrite_logo": False,
            "audit_feedback": "",
            "collection_feedback": "",
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
                    "title": "只检查明确选择的外语媒体库",
                    "text": "建议只选择外语电影库、外语剧库；不选择媒体库时不会检查，除非填写目录兜底。",
                    "class": "mb-4",
                },
            },
            self._form_col(
                "audit_enabled",
                "启用存量图片检查",
                "按计划检查所选外语媒体库中的简体中文图片候选。",
                12,
            ),
            self._library_select(
                "audit_libraries",
                "存量图片检查媒体库（建议必选）",
                "只选择需要补中文图片的外语库；留空且未填写目录兜底时不会检查。",
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
                                    "label": "检查计划",
                                    "hint": "默认每月 1 日凌晨 4 点：0 4 1 * *",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
            self._path_field(
                "audit_paths",
                "检查目录兜底（可选）",
                "只有没有选择媒体库时才使用；每行一个宿主可访问路径。",
                md=7,
            ),
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mt-4"},
                "content": [
                    {
                        "component": "VCardText",
                        "props": {"class": "d-flex flex-wrap align-center ga-3"},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "elevated",
                                    "prepend-icon": "mdi-play-circle-outline",
                                    "onClick": audit_now_js,
                                },
                                "text": "立即检查一次",
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-body-2 text-medium-emphasis"},
                                "text": "按当前存量检查媒体库和目录配置立即执行；正在检查时不会重复启动。",
                            },
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "audit_feedback",
                                    "label": "操作状态",
                                    "readonly": True,
                                    "variant": "plain",
                                    "hide-details": True,
                                },
                            },
                        ],
                    }
                ],
            },
        ]
        collection_library_items = [
            item
            for item in library_items
            if str(item.get("value") or "").partition("::")[0]
            == str(self._collection_server or "").strip()
        ] or library_items
        collection_items = self._collection_boxset_options(
            self._collection_server, self._collection_ids
        )
        collection_content = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "title": "只刷新现有合集图片",
                    "text": "这里只读取已有 Emby BoxSet，更新 poster 和 Logo；不会创建、删除、改名，也不会增删合集成员。",
                    "class": "mb-4",
                },
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
                                    "model": "collection_artwork_server",
                                    "label": "处理 Emby 服务",
                                    "items": server_items,
                                    "item-title": "title",
                                    "item-value": "value",
                                    "clearable": True,
                                    "variant": "outlined",
                                    "hint": "只有一个 Emby 时可留空；多个实例时建议明确选择。",
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
                                "component": "VSelect",
                                "props": {
                                    "model": "collection_artwork_scope",
                                    "label": "处理范围",
                                    "items": [
                                        {"title": "全部现有合集", "value": "all"},
                                        {"title": "指定媒体库中的合集", "value": "libraries"},
                                        {"title": "指定合集", "value": "collections"},
                                    ],
                                    "item-title": "title",
                                    "item-value": "value",
                                    "variant": "outlined",
                                    "hint": "范围只影响读取和刷新图片，不会改变合集成员。",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
                    self._form_col(
                        "collection_artwork_overwrite_poster",
                        "覆盖已有 poster",
                        "关闭时已有 poster 会跳过；建议先关闭确认候选，再按需打开。",
                        6,
                    ),
                    self._form_col(
                        "collection_artwork_overwrite_logo",
                        "覆盖已有 Logo",
                        "关闭时已有 Logo 会跳过；poster 和 Logo 独立判断。",
                        6,
                    ),
                ],
            },
            self._library_select(
                "collection_artwork_libraries",
                "指定媒体库",
                "仅在处理范围选择“指定媒体库中的合集”时使用。",
                collection_library_items,
            ),
            {
                "component": "VCol",
                "props": {"cols": 12},
                "content": [
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "collection_artwork_collections",
                            "label": "指定合集",
                            "items": collection_items,
                            "item-title": "title",
                            "item-value": "value",
                            "multiple": True,
                            "chips": True,
                            "closable-chips": True,
                            "variant": "outlined",
                            "hint": "仅在处理范围选择“指定合集”时使用；列表来自 Emby 现有 BoxSet。",
                            "persistent-hint": True,
                            "no-data-text": "未读取到现有 Emby 合集，请先检查实例连接。",
                        },
                    }
                ],
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "success",
                    "variant": "tonal",
                    "title": "当前图片优先级",
                    "text": self._collection_priority_text(),
                    "class": "mt-2",
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "合集图片会分别为 poster 和 Logo 选择候选；上传后还会重新读取 Emby ImageTags 验证，成功不只看 HTTP 状态码。",
                    "class": "mt-3",
                },
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mt-4"},
                "content": [
                    {
                        "component": "VCardText",
                        "props": {"class": "d-flex flex-wrap align-center ga-3"},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "elevated",
                                    "prepend-icon": "mdi-image-sync-outline",
                                    "onClick": collection_scan_js,
                                },
                                "text": "开始刷新合集图片",
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "warning",
                                    "variant": "tonal",
                                    "prepend-icon": "mdi-stop-circle-outline",
                                    "onClick": collection_cancel_js,
                                },
                                "text": "取消任务",
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-body-2 text-medium-emphasis"},
                                "text": "任务进度、成功/失败/跳过数量和每个合集的回读结果可在插件状态页查看。",
                            },
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "collection_feedback",
                                    "label": "操作状态",
                                    "readonly": True,
                                    "variant": "plain",
                                    "hide-details": True,
                                },
                            },
                        ],
                    }
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
                            "title": "实时处理、存量检查、合集图片各管各的",
                            "text": "实时刮削面向新入库媒体；存量图片检查面向少量外语库；合集图片只处理已有 BoxSet 的封面和徽标。三套范围互不影响。",
                            "class": "mb-4",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._form_col(
                                "enabled",
                                "启用插件",
                                "总开关；关闭后不会接收事件或运行存量检查。",
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
                                            "hint": "留空接收全部 Emby 实例；选择后同时限制实时事件和存量检查刷新目标。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            self._form_col(
                                "movie_enabled",
                                "处理电影",
                                "同时应用于实时刮削和存量图片检查。",
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
                                "text": "存量图片检查",
                            },
                            {
                                "component": "VTab",
                                "props": {
                                    "value": "collection",
                                    "prepend-icon": "mdi-image-multiple-outline",
                                },
                                "text": "合集图片",
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
                            {
                                "component": "VWindowItem",
                                "props": {"value": "collection"},
                                "content": collection_content,
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
                                                "text": "媒体库选择之外的统一排除项，同时应用于实时刮削和存量图片检查；通常可以留空。",
                                            },
                                            self._path_field(
                                                "exclude_paths",
                                                "排除目录（可选）",
                                                "每行一个宿主可访问路径。",
                                            ),
                                        ],
                                    },
                                ],
                            },
                            {
                                "component": "VExpansionPanel",
                                "content": [
                                    {
                                        "component": "VExpansionPanelTitle",
                                        "text": "高级：Emby 路径映射",
                                    },
                                    {
                                        "component": "VExpansionPanelText",
                                        "content": [
                                            {
                                                "component": "VAlert",
                                                "props": {
                                                    "type": "info",
                                                    "variant": "tonal",
                                                    "density": "compact",
                                                    "text": "只有 Emby 事件路径和 MoviePilot 实际挂载路径不一致时才填写。例如事件是 /media/media/...，而 MoviePilot 实际目录是 /media/...：Emby 前缀填 /media/media，MoviePilot 前缀填 /media。",
                                                    "class": "mb-3",
                                                },
                                            },
                                            {
                                                "component": "VRow",
                                                "content": [
                                                    self._path_prefix_field(
                                                        "emby_path_prefix",
                                                        "Emby 路径前缀",
                                                        "填写日志中收到的路径前缀，例如 /media/media。",
                                                    ),
                                                    self._path_prefix_field(
                                                        "local_path_prefix",
                                                        "MoviePilot 实际路径前缀",
                                                        "填写 MoviePilot 容器/主机内真实存在的前缀，例如 /media。",
                                                    ),
                                                ],
                                            },
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        ], defaults

    @staticmethod
    def _response_payload(response: Any) -> Any:
        """兼容 requests 响应和测试桩，安全取出 JSON。"""
        if response is None:
            return None
        try:
            return response.json() if hasattr(response, "json") else response
        except Exception:
            return None

    @staticmethod
    def _response_ok(response: Any) -> bool:
        """判断 HTTP 响应是否为成功状态。"""
        return bool(response) and getattr(response, "status_code", 200) in (200, 201, 204)

    def _load_collection_boxsets(self, service: Any) -> List[dict]:
        """读取现有 BoxSet；这个方法绝不创建或修改合集。"""
        instance = getattr(service, "instance", None) if service else None
        if not instance or not hasattr(instance, "get_data"):
            return []
        response = instance.get_data(
            url=(
                "[HOST]emby/Users/[USER]/Items?Recursive=true&"
                "IncludeItemTypes=BoxSet&Fields=ProviderIds,ImageTags,LibraryId,"
                "ParentId,TopParentId,Name&Limit=100000&api_key=[APIKEY]"
            )
        )
        if not self._response_ok(response):
            raise RuntimeError(
                f"读取 Emby 合集失败：HTTP {getattr(response, 'status_code', '无响应')}"
            )
        payload = self._response_payload(response)
        items = payload.get("Items", []) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise RuntimeError("读取 Emby 合集失败：响应缺少 Items 列表")
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _collection_tmdb_id(item: Optional[dict]) -> str:
        """从 Emby BoxSet 的 ProviderIds 中兼容提取 TMDB 合集 ID。"""
        if not isinstance(item, dict):
            return ""
        providers = item.get("ProviderIds") or item.get("provider_ids") or {}
        if not isinstance(providers, dict):
            providers = {}
        for key in ("Tmdb", "TMDB", "tmdb", "TmdbCollection", "tmdb_collection"):
            value = providers.get(key)
            if value not in (None, ""):
                return str(value).strip()
        for key in ("Tmdb", "TMDB", "tmdb", "TmdbCollectionId"):
            value = item.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    def _load_collection_members(self, service: Any, boxset_id: str) -> List[dict]:
        """只读查询合集成员，用于“指定媒体库”范围判断，不写入成员。"""
        if not boxset_id:
            return []
        instance = getattr(service, "instance", None) if service else None
        if not instance or not hasattr(instance, "get_data"):
            return []
        response = instance.get_data(
            url=(
                f"[HOST]emby/Users/[USER]/Items?ParentId={boxset_id}&Recursive=true&"
                "IncludeItemTypes=Movie,Series&Fields=LibraryId,ParentId,TopParentId,"
                "Path,Name&Limit=100000&api_key=[APIKEY]"
            )
        )
        if not self._response_ok(response):
            return []
        payload = self._response_payload(response)
        items = payload.get("Items", []) if isinstance(payload, dict) else payload
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def _collection_matches_libraries(
        self, server_name: str, service: Any, boxset: dict, selected: List[str]
    ) -> bool:
        """按合集字段或只读成员关系判断合集是否属于所选媒体库。"""
        if not selected:
            return False
        selected_ids = set()
        selected_keys = set()
        for item in selected:
            raw = str(item or "").strip()
            selected_server, separator, selected_id = raw.partition("::")
            if separator:
                if selected_server and selected_server != server_name:
                    continue
                if selected_id:
                    selected_ids.add(selected_id)
                    selected_keys.add(raw)
            elif raw:
                # 兼容旧配置直接保存媒体库 ID 的情况。
                selected_ids.add(raw)
                selected_keys.add(self._library_key(server_name, raw))
        selected_ids.discard("")
        catalog = self._load_library_catalog()
        selected_names = {
            str((catalog.get(item) or {}).get("name") or "").casefold()
            for item in selected_keys
            if item in catalog and catalog[item].get("server") == server_name
        }
        direct_values = {
            str(boxset.get(field) or "").strip()
            for field in ("LibraryId", "TopParentId", "ParentId")
        }
        if selected_ids.intersection(direct_values):
            return True
        member_ids = self._load_collection_members(
            service, str(boxset.get("Id") or boxset.get("id") or "")
        )
        instance = getattr(service, "instance", None) if service else None
        for member in member_ids:
            values = {
                str(member.get(field) or "").strip()
                for field in ("LibraryId", "TopParentId", "ParentId")
            }
            if selected_ids.intersection(values):
                return True
            names = {
                str(member.get(field) or "").strip().casefold()
                for field in ("LibraryName", "TopParentName")
            }
            if selected_names.intersection(names):
                return True
            member_id = str(member.get("Id") or member.get("id") or "").strip()
            if member_id and instance and hasattr(instance, "get_data"):
                # 某些 Emby 版本的成员列表不给 LibraryId，只能通过祖先库判断。
                try:
                    response = instance.get_data(
                        url=f"[HOST]emby/Items/{member_id}/Ancestors?api_key=[APIKEY]"
                    )
                    ancestors = self._response_payload(response)
                    if isinstance(ancestors, dict):
                        ancestors = ancestors.get("Items") or ancestors.get("Ancestors") or []
                    for ancestor in ancestors or []:
                        if not isinstance(ancestor, dict):
                            continue
                        ancestor_id = str(ancestor.get("Id") or ancestor.get("id") or "")
                        ancestor_name = str(
                            ancestor.get("Name") or ancestor.get("name") or ""
                        ).casefold()
                        if ancestor_id in selected_ids or ancestor_name in selected_names:
                            return True
                except Exception as err:
                    logger.debug("读取合集成员媒体库祖先失败：%s", err)
        # 最后兼容没有成员字段的 Emby：直接从所选库的条目 CollectionIds 反查。
        if instance and hasattr(instance, "get_data"):
            for library_id in selected_ids:
                try:
                    response = instance.get_data(
                        url=(
                            f"[HOST]emby/Users/[USER]/Items?ParentId={library_id}"
                            "&Recursive=true&IncludeItemTypes=Movie,Series"
                            "&Fields=CollectionIds&Limit=100000&api_key=[APIKEY]"
                        )
                    )
                    payload = self._response_payload(response)
                    items = payload.get("Items", []) if isinstance(payload, dict) else payload
                    for item in items or []:
                        collection_ids = item.get("CollectionIds") or item.get("collection_ids") or []
                        if not isinstance(collection_ids, list):
                            collection_ids = [collection_ids]
                        if str(boxset.get("Id") or boxset.get("id") or "") in {
                            str(value) for value in collection_ids
                        }:
                            return True
                except Exception as err:
                    logger.debug("按媒体库条目反查合集失败：%s", err)
        return False

    def _request_json(
        self, url: str, params: Optional[dict] = None, timeout: int = 20
    ) -> dict:
        """发起外部图片 API 请求并统一返回字典。"""
        request_cls = RequestUtils
        if request_cls is None:
            from app.utils.http import RequestUtils as request_cls
        kwargs = {
            "proxies": getattr(settings, "PROXY", None),
            "timeout": timeout,
        }
        user_agent = getattr(settings, "NORMAL_USER_AGENT", None)
        if user_agent:
            kwargs["ua"] = user_agent
        try:
            response = request_cls(**kwargs).get_res(url, params=params or {})
            if not response or getattr(response, "status_code", 200) != 200:
                return {}
            payload = self._response_payload(response)
            return payload if isinstance(payload, dict) else {}
        except Exception as err:
            logger.warning("请求合集图片候选失败：%s - %s", url, err)
            return {}

    def _query_collection_tmdb(
        self, collection_id: str
    ) -> Tuple[dict, dict]:
        """获取合集详情和图片，显式保留当前优先级需要的语言候选。"""
        domain = str(getattr(settings, "TMDB_API_DOMAIN", "api.themoviedb.org")).rstrip("/")
        api_key = str(getattr(settings, "TMDB_API_KEY", "") or "")
        detail = self._request_json(
            f"https://{domain}/3/collection/{collection_id}",
            {"api_key": api_key, "language": "zh-CN"},
        )
        images = self._request_json(
            f"https://{domain}/3/collection/{collection_id}/images",
            {
                "api_key": api_key,
                "language": "zh-CN",
                "include_image_language": self.COLLECTION_IMAGE_LANGUAGES,
            },
        )
        return detail, images

    def _query_collection_fanart(self, collection_id: str, detail: Optional[dict] = None) -> dict:
        """查询 Fanart 候选。

        Fanart 的公开 v3 接口主要按电影 ID 提供图片；部分部署提供
        ``collections`` 路由，因此先尝试合集路由，再兼容电影路由，任何
        失败都只会让该来源没有候选，不影响 TMDB 选图。
        """
        api_key = str(getattr(settings, "FANART_API_KEY", "") or "")
        if not api_key:
            return {}
        for query_type in ("collections",):
            payload = self._request_json(
                f"https://webservice.fanart.tv/v3/{query_type}/{collection_id}",
                {"api_key": api_key},
                timeout=30,
            )
            if payload:
                return normalize_fanart_payload(payload)
        # Fanart 官方 v3 没有稳定的合集路由时，按 TMDB 合集成员的电影 ID
        # 聚合候选，仍然只用于挑选图片，不会修改合集成员。
        merged: Dict[str, Dict[str, List[dict]]] = {
            "chinese": {"poster": [], "logo": []},
            "english": {"poster": [], "logo": []},
        }
        part_ids = []
        for part in (detail or {}).get("parts") or []:
            if isinstance(part, dict) and part.get("id"):
                part_ids.append(str(part["id"]))
        for movie_id in part_ids[:20]:
            payload = self._request_json(
                f"https://webservice.fanart.tv/v3/movies/{movie_id}",
                {"api_key": api_key},
                timeout=30,
            )
            normalized = normalize_fanart_payload(payload)
            for group in merged:
                for kind in merged[group]:
                    seen = {
                        str(item.get("url")) for item in merged[group][kind] if item.get("url")
                    }
                    for item in normalized[group][kind]:
                        if str(item.get("url")) not in seen:
                            merged[group][kind].append(item)
                            seen.add(str(item.get("url")))
        return merged

    @staticmethod
    def _collection_image_url(path: str) -> str:
        """转换 TMDB 相对图片路径为原图地址。"""
        if str(path).startswith("http"):
            return str(path)
        builder = getattr(settings, "TMDB_IMAGE_URL", None)
        if callable(builder):
            return str(builder(path, "original"))
        return f"https://image.tmdb.org/t/p/original{path}"

    @staticmethod
    def _collection_source_language(detail: Optional[dict]) -> str:
        """从合集详情或其电影分段推断 TMDB 源语言。"""
        if not isinstance(detail, dict):
            return ""
        direct = str(detail.get("original_language") or "").strip()
        if direct:
            return direct
        languages = [
            str(part.get("original_language") or "").strip()
            for part in detail.get("parts") or []
            if isinstance(part, dict) and part.get("original_language")
        ]
        if not languages:
            return ""
        # 合集可能跨语言；仅在所有成员一致时使用源语言层，避免误选。
        return languages[0] if len(set(languages)) == 1 else ""

    def _collection_job(self) -> dict:
        """读取合集图片任务状态，并以当前线程事实修正 busy。"""
        job = self.get_data(self.DATA_COLLECTION_JOB) or {}
        if not isinstance(job, dict):
            job = {}
        worker = getattr(self, "_collection_worker", None)
        busy = bool(worker and worker.is_alive())
        job["busy"] = busy
        if busy:
            job["cancel_requested"] = bool(
                getattr(self, "_collection_stop_event", None)
                and self._collection_stop_event.is_set()
            )
        elif job.get("running"):
            job["running"] = False
        return job

    def _persist_collection_config(self) -> None:
        """把合集图片字段写回插件配置，兼容设置页保存后的新旧字段。"""
        try:
            current = self.get_config() or {}
            if not isinstance(current, dict):
                current = {}
            current.update(
                {
                    "collection_artwork_server": self._collection_server,
                    "collection_artwork_scope": self._collection_scope,
                    "collection_artwork_libraries": self._collection_libraries,
                    "collection_artwork_collections": self._collection_ids,
                    "collection_artwork_overwrite_poster": self._collection_overwrite_poster,
                    "collection_artwork_overwrite_logo": self._collection_overwrite_logo,
                }
            )
            self.update_config(current)
        except Exception as err:
            logger.debug("保存合集图片配置快照失败：%s", err)

    def _set_collection_job(self, **updates: Any) -> dict:
        """更新并持久化合集图片任务状态。"""
        with getattr(self, "_collection_lock", threading.RLock()):
            job = self._collection_job()
            job.update(updates)
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.save_data(self.DATA_COLLECTION_JOB, job)
            return job

    def api_collection_artwork_status(self) -> schemas.Response:
        """返回合集图片任务进度和最近处理明细。"""
        return schemas.Response(success=True, data=self._collection_job())

    def api_start_collection_artwork_scan(
        self, payload: Optional[dict] = Body(default=None)
    ) -> schemas.Response:
        """启动一次互斥的合集图片刷新任务。"""
        if not self._enabled:
            return schemas.Response(success=False, message="请先启用插件")
        payload = payload if isinstance(payload, dict) else {}
        server_name, service = self._resolve_collection_service(payload.get("server"))
        if not service:
            return schemas.Response(success=False, message="请选择可用的 Emby 服务")
        scope = str(payload.get("scope") or self._collection_scope or "all").lower()
        if scope not in {"all", "libraries", "collections"}:
            return schemas.Response(success=False, message="合集图片处理范围无效")
        selected_libraries = self._dedupe_values(
            payload.get("libraries")
            if "libraries" in payload
            else self._collection_libraries
        )
        selected_ids = self._dedupe_values(
            payload.get("collections")
            if "collections" in payload
            else self._collection_ids
        )
        if scope == "all":
            selected_libraries = []
            selected_ids = []
        if scope == "libraries" and not selected_libraries:
            return schemas.Response(success=False, message="请选择至少一个媒体库")
        if scope == "collections" and not selected_ids:
            return schemas.Response(success=False, message="请选择至少一个合集")
        overwrite_poster = bool(
            payload.get("overwrite_poster", self._collection_overwrite_poster)
        )
        overwrite_logo = bool(
            payload.get("overwrite_logo", self._collection_overwrite_logo)
        )
        self._collection_server = server_name
        self._collection_scope = scope
        self._collection_libraries = selected_libraries
        self._collection_ids = selected_ids
        self._collection_overwrite_poster = overwrite_poster
        self._collection_overwrite_logo = overwrite_logo
        self._persist_collection_config()
        with self._collection_lock:
            worker = getattr(self, "_collection_worker", None)
            if worker and worker.is_alive():
                return schemas.Response(success=False, message="合集图片任务正在运行")
            self._collection_stop_event.clear()
            run_id = uuid4().hex[:12]
            self._set_collection_job(
                running=True,
                busy=True,
                run_id=run_id,
                phase="starting",
                progress=0,
                current=0,
                total=0,
                success=0,
                failed=0,
                skipped=0,
                poster_success=0,
                logo_success=0,
                details=[],
                error="",
                message="正在启动合集图片刷新",
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._collection_worker = threading.Thread(
                target=self._collection_artwork_worker,
                args=(
                    server_name,
                    service,
                    scope,
                    selected_libraries,
                    selected_ids,
                    overwrite_poster,
                    overwrite_logo,
                ),
                daemon=True,
                name=f"{self.__class__.__name__}-collection-artwork",
            )
            self._collection_worker.start()
        return schemas.Response(success=True, message="合集图片刷新任务已启动", data=self._collection_job())

    def api_cancel_collection_artwork(self) -> schemas.Response:
        """请求取消合集图片后台任务。"""
        worker = getattr(self, "_collection_worker", None)
        if not worker or not worker.is_alive():
            return schemas.Response(success=False, message="当前没有运行中的合集图片任务")
        self._collection_stop_event.set()
        self._set_collection_job(
            cancel_requested=True, phase="cancelling", message="正在取消合集图片任务"
        )
        return schemas.Response(success=True, message="已请求取消合集图片任务")

    def _collection_artwork_worker(
        self,
        server_name: str,
        service: Any,
        scope: str,
        selected_libraries: List[str],
        selected_ids: List[str],
        overwrite_poster: bool,
        overwrite_logo: bool,
    ) -> None:
        """读取现有合集并逐项刷新 poster/logo，上传后做 ImageTags 回读校验。"""
        errors: List[str] = []
        details: List[dict] = []
        success = failed = skipped = poster_success = logo_success = 0
        try:
            boxsets = self._load_collection_boxsets(service)
            targets: List[dict] = []
            for boxset in boxsets:
                boxset_id = str(boxset.get("Id") or boxset.get("id") or "").strip()
                if not boxset_id:
                    continue
                if scope == "collections" and boxset_id not in selected_ids:
                    continue
                if scope == "libraries" and not self._collection_matches_libraries(
                    server_name, service, boxset, selected_libraries
                ):
                    continue
                targets.append(boxset)
            total = len(targets)
            self._set_collection_job(
                phase="refreshing", total=total, current=0, progress=0,
                message=f"已读取 {total} 个现有合集",
            )
            for index, boxset in enumerate(targets, start=1):
                if self._collection_stop_event.is_set():
                    break
                boxset_id = str(boxset.get("Id") or boxset.get("id") or "")
                name = str(boxset.get("Name") or boxset_id)
                row = {
                    "id": boxset_id,
                    "name": name,
                    "tmdb_id": self._collection_tmdb_id(boxset),
                    "status": "skipped",
                    "poster": None,
                    "logo": None,
                    "message": "",
                }
                try:
                    tmdb_id = row["tmdb_id"]
                    if not tmdb_id:
                        row["message"] = "缺少 TMDB 合集 ID"
                        skipped += 1
                    else:
                        detail, tmdb_images = self._query_collection_tmdb(tmdb_id)
                        fanart_images = self._query_collection_fanart(tmdb_id, detail)
                        source_language = self._collection_source_language(detail)
                        selected = select_collection_images(
                            tmdb_images,
                            fanart_images,
                            self._collection_priority(),
                            source_language=source_language,
                            image_url=self._collection_image_url,
                        )
                        row["poster"] = self._collection_selection_summary(selected.get("poster"))
                        row["logo"] = self._collection_selection_summary(selected.get("logo"))
                        current = boxset
                        if not (current.get("ImageTags") or current.get("image_tags")):
                            current = self._load_collection_item(service, boxset_id) or current
                        tags = current.get("ImageTags") or current.get("image_tags") or {}
                        changed = False
                        skipped_types = 0
                        for image_type, key, overwrite in (
                            ("Primary", "poster", overwrite_poster),
                            ("Logo", "logo", overwrite_logo),
                        ):
                            candidate = selected.get(key)
                            if not candidate:
                                skipped_types += 1
                                continue
                            if tags.get(image_type) and not overwrite:
                                row[key]["status"] = "skipped"
                                row[key]["verification"] = "已有图片，按设置跳过"
                                skipped_types += 1
                                continue
                            result = self._upload_collection_image(
                                service, boxset_id, image_type, candidate["url"]
                            )
                            row[key].update(result)
                            changed = True
                            if image_type == "Primary":
                                poster_success += 1
                            else:
                                logo_success += 1
                        if changed:
                            success += 1
                            row["status"] = "success"
                            row["message"] = "至少一种图片已上传并回读验证"
                        else:
                            skipped += 1
                            row["message"] = "没有需要更新的图片候选"
                except Exception as err:
                    failed += 1
                    row["status"] = "failed"
                    row["message"] = str(err)
                    errors.append(f"{name}（TMDB {row['tmdb_id'] or '无'}）：{err}")
                    logger.warning("刷新合集图片失败：%s - %s", name, err, exc_info=True)
                details.append(row)
                details = details[-100:]
                self._set_collection_job(
                    phase="refreshing",
                    current=index,
                    total=total,
                    progress=round(index * 100 / max(total, 1)),
                    success=success,
                    failed=failed,
                    skipped=skipped,
                    poster_success=poster_success,
                    logo_success=logo_success,
                    details=details,
                    error="\n".join(errors[-20:]),
                    message=f"正在处理合集 {index}/{total}：{name}",
                )
            cancelled = self._collection_stop_event.is_set()
            phase = "cancelled" if cancelled else ("partial" if errors else "done")
            self._set_collection_job(
                running=False,
                busy=False,
                cancel_requested=False,
                phase=phase,
                progress=100 if not cancelled else self._collection_job().get("progress", 0),
                success=success,
                failed=failed,
                skipped=skipped,
                poster_success=poster_success,
                logo_success=logo_success,
                details=details,
                error="\n".join(errors[-20:]),
                message=(
                    "合集图片刷新已取消"
                    if cancelled
                    else f"合集图片刷新完成：成功 {success}，跳过 {skipped}，失败 {failed}"
                ),
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
        except Exception as err:
            logger.error("合集图片后台任务异常：%s", err, exc_info=True)
            self._set_collection_job(
                running=False,
                busy=False,
                cancel_requested=False,
                phase="cancelled" if self._collection_stop_event.is_set() else "failed",
                message="合集图片任务已取消" if self._collection_stop_event.is_set() else "合集图片任务失败",
                error="" if self._collection_stop_event.is_set() else str(err),
                details=details,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
        finally:
            with getattr(self, "_collection_lock", threading.RLock()):
                if getattr(self, "_collection_worker", None) is threading.current_thread():
                    self._collection_worker = None

    @staticmethod
    def _collection_selection_summary(selection: Optional[dict]) -> Optional[dict]:
        """把选择器结果压缩成可直接展示/持久化的来源摘要。"""
        if not selection:
            return None
        return {
            "source": selection.get("source"),
            "language": selection.get("language"),
            "priority_key": selection.get("priority_key"),
            "priority_label": selection.get("priority_label"),
            "status": "pending",
        }

    def _load_collection_item(self, service: Any, item_id: str) -> dict:
        """读取单个合集的图片标签。"""
        instance = getattr(service, "instance", None) if service else None
        if not instance or not hasattr(instance, "get_data"):
            return {}
        response = instance.get_data(
            url=f"[HOST]emby/Users/[USER]/Items/{item_id}?Fields=ImageTags,ProviderIds&api_key=[APIKEY]"
        )
        payload = self._response_payload(response)
        return payload if isinstance(payload, dict) else {}

    def _upload_collection_image(
        self, service: Any, item_id: str, image_type: str, image_url: str
    ) -> dict:
        """下载并上传图片，随后回读 ImageTags 确认 Emby 已接受。"""
        request_cls = RequestUtils
        if request_cls is None:
            from app.utils.http import RequestUtils as request_cls
        image = request_cls(
            proxies=getattr(settings, "PROXY", None), timeout=30
        ).get_res(image_url)
        if not image or getattr(image, "status_code", 0) != 200 or not getattr(image, "content", None):
            raise RuntimeError(f"下载 {image_type} 图片失败")
        content_type = (getattr(image, "headers", {}) or {}).get("Content-Type") or "image/jpeg"
        instance = getattr(service, "instance", None) if service else None
        if not instance or not hasattr(instance, "post_data"):
            raise RuntimeError("Emby 实例不支持图片上传")
        upload_url = f"[HOST]emby/Items/{item_id}/Images/{image_type}?api_key=[APIKEY]"
        # 不同 Emby 版本/适配器对图片体的约定不同：优先尝试插件生态中使用的
        # Base64 文本体，失败后回退到官方二进制体；两种方式都必须通过回读校验。
        payloads = [
            (
                base64.b64encode(image.content).decode("ascii"),
                # Emby 通过 MIME 类型判断图片扩展名，body 则是 Base64 文本。
                {"Content-Type": content_type},
            ),
            (image.content, {"Content-Type": content_type}),
        ]
        last_status: Any = "无响应"
        for data, headers in payloads:
            response = instance.post_data(url=upload_url, data=data, headers=headers)
            last_status = getattr(response, "status_code", "无响应")
            if not self._response_ok(response):
                continue
            verified = self._load_collection_item(service, item_id)
            tags = verified.get("ImageTags") or verified.get("image_tags") or {}
            if tags.get(image_type):
                return {
                    "status": "uploaded",
                    "verification": "Emby ImageTags 回读成功",
                }
        raise RuntimeError(f"上传 {image_type} 后回读 ImageTags 未确认（HTTP {last_status}）")

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
    def _path_prefix_field(model: str, label: str, hint: str) -> dict:
        """生成路径映射前缀输入列。"""
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 6},
            "content": [
                {
                    "component": "VTextField",
                    "props": {
                        "model": model,
                        "label": label,
                        "variant": "outlined",
                        "clearable": True,
                        "hint": hint,
                        "persistent-hint": True,
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
        """返回实时与存量图片检查状态页面。"""
        with getattr(self, "_lock", threading.RLock()):
            pending_count = len(getattr(self, "_pending", {}))
        collection_job = self._collection_job()
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
            f"检查库 {len(self._audit_libraries)} 个"
            if self._audit_libraries
            else "检查库：未选择"
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
                                        "text": f"存量检查 {'开启' if self._audit_enabled else '关闭'}",
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
                        "props": {
                            "class": "flex-nowrap overflow-x-auto ga-2",
                        },
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
                                "存量检查记录",
                                len(states),
                                "mdi-folder-search-outline",
                                "info",
                            ),
                            self._stat_card(
                                "合集图片成功",
                                collection_job.get("success", 0),
                                "mdi-image-check-outline",
                                "success",
                            ),
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info" if not self._audit_running else "warning",
                            "variant": "tonal",
                            "title": "最近运行",
                            "text": f"实时：{self._last_realtime_result}（{self._last_realtime_at or '—'}）\n存量检查：{self._last_audit_result}（{self._last_audit_at or '—'}）",
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
            "props": {
                "cols": 12,
                "sm": 6,
                "md": 2,
                "class": "flex-grow-1",
                "style": "flex: 1 1 0; max-width: 20%; min-width: 180px;",
            },
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
        """注册存量图片检查服务。"""
        if not self._enabled or not self._audit_enabled:
            return []
        return [
            {
                "id": "EmbyMediaImageManagerAudit",
                "name": "Emby媒体图片存量检查",
                "trigger": CronTrigger.from_crontab(self._audit_cron),
                "func": self.run_audit,
                "kwargs": {},
            }
        ]

    def stop_service(self) -> None:
        """停止延迟任务，并通知存量检查/合集图片任务尽快退出。"""
        stop_event = getattr(self, "_stop_event", None)
        if stop_event:
            stop_event.set()
        collection_stop_event = getattr(self, "_collection_stop_event", None)
        if collection_stop_event:
            collection_stop_event.set()
        collection_worker = getattr(self, "_collection_worker", None)
        if (
            collection_worker
            and collection_worker.is_alive()
            and collection_worker is not threading.current_thread()
        ):
            collection_worker.join(timeout=3)
        if collection_worker and not collection_worker.is_alive():
            self._collection_worker = None
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
        mapped_path = self._map_emby_path(raw_path)
        in_legacy_paths = self._path_allowed(mapped_path, self._realtime_paths)
        if (
            not raw_path
            or self._is_excluded(mapped_path)
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

        target_path = self._realtime_target(Path(mapped_path), item_type)
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
            "Emby图片管理收到入库事件：%s，映射目标%s，延迟%s秒",
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

    def api_start_image_check(self) -> schemas.Response:
        """立即后台执行一次存量图片检查，不要求开启定时计划。"""
        if not self._enabled:
            return schemas.Response(success=False, message="请先启用插件")
        with self._lock:
            worker = getattr(self, "_audit_worker", None)
            if worker and worker.is_alive():
                return schemas.Response(success=False, message="存量图片检查正在运行")
            self._stop_event.clear()
            self._audit_worker = threading.Thread(
                target=self._run_manual_audit,
                daemon=True,
                name=f"{self.__class__.__name__}-image-check",
            )
            self._audit_worker.start()
        return schemas.Response(success=True, message="存量图片检查已开始")

    def _run_manual_audit(self) -> None:
        """后台线程入口，确保立即检查结束后释放线程引用。"""
        try:
            self.run_audit(manual=True)
        finally:
            with self._lock:
                if getattr(self, "_audit_worker", None) is threading.current_thread():
                    self._audit_worker = None

    def run_audit(self, manual: bool = False) -> None:
        """扫描指定库，仅在发现简体中文候选后覆盖刮削。"""
        if not self._enabled or (not self._audit_enabled and not manual) or self._stop_event.is_set():
            return
        audit_lock = self._audit_lock
        stop_event = self._stop_event
        if not audit_lock.acquire(blocking=False):
            logger.warning("存量图片检查仍在运行，本次计划已跳过")
            return
        self._audit_running = True
        scanned = fixed = waiting = failed = 0
        skip_reason = ""
        states = self.get_data(self.DATA_STATES) or {}
        if not isinstance(states, dict):
            logger.warning("存量检查状态数据格式异常，已重建为空状态")
            states = {}
        try:
            roots = self._audit_roots(stop_event)
            if not roots:
                skip_reason = "未配置媒体库或检查目录，未执行扫描"
                logger.warning("未配置媒体库或检查目录，跳过存量图片检查")
                return
            for root_path, root_servers in roots:
                if stop_event.is_set():
                    break
                if not root_path.exists():
                    failed += 1
                    logger.warning("存量检查路径不存在：%s", root_path)
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
                                "存量检查未发现简体图片，仅记录等待：%s", media_path
                            )
                    except Exception as err:
                        failed += 1
                        logger.warning(
                            "存量图片检查失败：%s - %s", media_path, err, exc_info=True
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

    def _map_emby_path(self, path: str) -> str:
        """将 Emby 事件路径映射为 MoviePilot 本地可访问路径。"""
        source = self._normalize_path_prefix(self._emby_path_prefix)
        target = self._normalize_path_prefix(self._local_path_prefix)
        if not source or not target:
            return str(path)
        normalized = str(path).replace("\\", "/")
        source_fold = source.casefold()
        normalized_fold = normalized.casefold()
        if normalized_fold == source_fold:
            return target
        prefix = f"{source}/"
        if normalized_fold.startswith(prefix.casefold()):
            return f"{target}/{normalized[len(prefix) :]}".replace("//", "/")
        return str(path)

    @staticmethod
    def _normalize_path_prefix(value: Any) -> str:
        """规范化路径映射前缀，保留根路径 `/`。"""
        normalized = str(value or "").strip().replace("\\", "/")
        if not normalized:
            return ""
        if normalized != "/":
            normalized = normalized.rstrip("/")
        return normalized

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
        """生成适合跨次存量检查复用的路径键。"""
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
                "存量图片检查计划无效：%r（%s），已使用默认值 %s",
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
