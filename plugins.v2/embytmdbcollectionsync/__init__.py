import hashlib
import json
import threading
import time
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body

from app import schemas
from app.core.config import settings
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.modules.themoviedb import TmdbApi
from app.plugins import _PluginBase
from app.utils.http import RequestUtils


class CollectionMemberReadError(RuntimeError):
    """Emby 合集成员读取失败，禁止把失败误判为空集合。"""


class EmbyTmdbCollectionSync(_PluginBase):
    """按照 TMDB 官方合集整理 Emby 电影，并同步合集图片。"""

    plugin_name = "Emby TMDB 合集整理"
    plugin_desc = "按 TMDB 官方合集整理 Emby 电影。"
    plugin_icon = "TheMovieDb_A.png"
    plugin_version = "1.1.0"
    plugin_author = "VirgoooooX"
    author_url = "https://github.com/VirgoooooX/MoviePilot-Plugins"
    plugin_label = "媒体服务器,元数据"
    plugin_config_prefix = "embytmdbcollectionsync_"
    plugin_order = 47
    auth_level = 1

    DATA_STATE = "managed_collections"
    DATA_PLAN = "latest_plan"
    DATA_CACHE = "tmdb_cache"
    DATA_JOB = "job_status"
    CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
    CACHE_MAX_ENTRIES = 2000
    CACHE_FLUSH_INTERVAL = 100
    COLLECTION_IMAGE_LANGUAGES = "zh-CN,zh-SG,zh-TW,zh-HK,zh,en-US,en,null"

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取插件配置并初始化互斥状态。"""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._show_sidebar_nav = bool(config.get("show_sidebar_nav", True))
        self._server = str(config.get("server") or "")
        self._libraries = [str(item) for item in config.get("libraries") or [] if item]
        self._overwrite_images = bool(config.get("overwrite_images", True))
        self._delete_empty = bool(config.get("delete_empty", True))
        self._sync_logo = bool(config.get("sync_logo", True))
        self._lock = threading.RLock()
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._run_id = ""
        self._tmdb_zh_cn = TmdbApi(language="zh-CN")
        self._recover_stale_job()
        self._save_config()

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回远程命令列表。"""
        return []

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        """声明使用 Vue 联邦组件渲染。"""
        return "vue", "dist/assets"

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回 Vue 配置页面初始模型。"""
        return [], self._current_config()

    def get_page(self) -> List[dict]:
        """声明详情页由 Vue 组件渲染。"""
        return []

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        """返回插件侧栏入口。"""
        if not self.get_state() or not self._show_sidebar_nav:
            return []
        return [{
            "nav_key": "main",
            "title": "Emby 合集整理",
            "icon": "mdi-movie-filter",
            "section": "organize",
            "permission": "manage",
            "order": 47,
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册联邦页面调用的插件 API。"""
        return [
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"], "auth": "bear", "summary": "获取状态"},
            {"path": "/config", "endpoint": self.api_save_config, "methods": ["POST"], "auth": "bear", "summary": "保存配置"},
            {"path": "/scan", "endpoint": self.api_start_scan, "methods": ["POST"], "auth": "bear", "summary": "开始预演"},
            {"path": "/apply", "endpoint": self.api_start_apply, "methods": ["POST"], "auth": "bear", "summary": "执行计划"},
            {"path": "/cancel", "endpoint": self.api_cancel, "methods": ["POST"], "auth": "bear", "summary": "取消任务"},
        ]

    def stop_service(self) -> None:
        """请求停止后台任务，并把未结束任务记录为插件生命周期中断。"""
        with getattr(self, "_lock", threading.RLock()):
            worker = getattr(self, "_worker", None)
            stop_event = getattr(self, "_stop_event", None)
            if stop_event:
                stop_event.set()
            should_join = bool(worker and worker.is_alive() and worker is not threading.current_thread())
        # 不持有状态锁等待线程；线程退出时需要拿同一把锁写入最终 job 状态。
        if should_join:
            worker.join(timeout=3)
        with getattr(self, "_lock", threading.RLock()):
            if worker and worker.is_alive():
                # 保留线程引用，避免重载后误判空闲并启动并发任务。
                self._set_job(
                    running=True,
                    busy=True,
                    cancel_requested=True,
                    phase="cancelling",
                    message="正在等待后台任务退出，暂时不能启动新任务",
                    error="任务因插件停止或重载请求取消，请等待线程退出",
                )
                return
            if worker and getattr(self, "_worker", None) is worker:
                self._worker = None
            self._set_job(running=False, busy=False, cancel_requested=False)

    def _current_config(self) -> Dict[str, Any]:
        """返回当前配置快照。"""
        return {
            "enabled": self._enabled,
            "show_sidebar_nav": self._show_sidebar_nav,
            "server": self._server,
            "libraries": self._libraries,
            "overwrite_images": self._overwrite_images,
            "delete_empty": self._delete_empty,
            "sync_logo": self._sync_logo,
        }

    def _save_config(self) -> None:
        """持久化当前配置。"""
        self.update_config(self._current_config())

    @staticmethod
    def _member_snapshot(members: List[dict]) -> Tuple[List[str], str]:
        """把 Emby 合集成员规范化为稳定 ID 快照和 SHA-256 哈希。"""
        ids = sorted({
            str(item.get("Id") or item.get("id"))
            for item in members or []
            if isinstance(item, dict) and (item.get("Id") or item.get("id"))
        })
        digest = hashlib.sha256(json.dumps(ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        return ids, digest

    def _config_fingerprint(self) -> str:
        """返回当前配置的稳定指纹，供预演计划执行前做一致性校验。"""
        payload = self._current_config()
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _compact_tmdb_detail(cls, detail: dict) -> dict:
        """仅保留识别合集所需的 TMDB 电影字段，避免缓存无用大响应。"""
        collection = detail.get("belongs_to_collection") if isinstance(detail, dict) else None
        return {"belongs_to_collection": dict(collection)} if isinstance(collection, dict) else {"belongs_to_collection": {}}

    @classmethod
    def _cache_get(cls, cache: Dict[str, Any], key: str, now: Optional[float] = None) -> Optional[dict]:
        """读取带 TTL 的 TMDB 电影缓存，并兼容旧版本裸详情格式。"""
        entry = cache.get(key)
        if not isinstance(entry, dict):
            return None
        if isinstance(entry.get("data"), dict):
            fetched_at = float(entry.get("fetched_at") or 0)
            if fetched_at and (now or time.time()) - fetched_at > cls.CACHE_TTL_SECONDS:
                cache.pop(key, None)
                return None
            return entry["data"]
        # 旧版本缓存没有时间戳，下一次命中时转成紧凑格式并从当前时间重新计时。
        if "belongs_to_collection" in entry:
            cache[key] = {"fetched_at": now or time.time(), "data": cls._compact_tmdb_detail(entry)}
            return cache[key]["data"]
        cache.pop(key, None)
        return None

    @classmethod
    def _cache_set(cls, cache: Dict[str, Any], key: str, detail: dict, now: Optional[float] = None) -> None:
        """写入紧凑的带时间戳 TMDB 电影缓存。"""
        cache[key] = {"fetched_at": now or time.time(), "data": cls._compact_tmdb_detail(detail)}

    @classmethod
    def _prune_tmdb_cache(cls, cache: Dict[str, Any]) -> None:
        """清理无效、过期和超过上限的 TMDB 电影缓存条目。"""
        now = time.time()
        for key in list(cache):
            if not str(key).startswith("movie:"):
                cache.pop(key, None)
                continue
            cls._cache_get(cache, str(key), now)
        if len(cache) <= cls.CACHE_MAX_ENTRIES:
            return
        ordered = sorted(
            cache.items(),
            key=lambda pair: float((pair[1] or {}).get("fetched_at") or 0) if isinstance(pair[1], dict) else 0,
            reverse=True,
        )
        for key, _ in ordered[cls.CACHE_MAX_ENTRIES:]:
            cache.pop(key, None)

    def _services(self) -> Dict[str, Any]:
        """返回当前可用的 Emby 服务。"""
        services = MediaServerHelper().get_services(type_filter="emby") or {}
        return {
            name: service for name, service in services.items()
            if service and service.instance and not service.instance.is_inactive()
        }

    def _selected_service(self) -> Tuple[str, Any]:
        """返回用户选择的 Emby 服务，只有一个服务时自动采用。"""
        services = self._services()
        name = self._server or (next(iter(services)) if len(services) == 1 else "")
        return name, services.get(name)

    @staticmethod
    def _server_host(service: Any) -> str:
        """返回 Emby 外部访问地址。"""
        config = service.config.config if service and service.config else {}
        return str((config or {}).get("play_host") or (config or {}).get("host") or "").rstrip("/")

    def _library_options(self, services: Dict[str, Any]) -> List[dict]:
        """读取所有 Emby 电影库选项。"""
        rows: List[dict] = []
        for server_name, service in services.items():
            libraries = service.instance.get_librarys() or []
            for library in libraries:
                library_type = getattr(library, "type", None)
                if library_type != "电影":
                    continue
                rows.append({
                    "server": server_name,
                    "id": str(getattr(library, "id", "") or getattr(library, "item_id", "")),
                    "name": getattr(library, "name", ""),
                    "count": getattr(library, "item_count", 0),
                })
        return rows

    def _job(self) -> Dict[str, Any]:
        """返回任务状态。"""
        job = self.get_data(self.DATA_JOB) or {}
        if not isinstance(job, dict):
            job = {}
        worker = getattr(self, "_worker", None)
        busy = bool(worker and worker.is_alive())
        # 任务状态是持久化的，busy 则以当前线程事实为准，避免旧状态误导前端。
        job["busy"] = busy
        if busy:
            job["cancel_requested"] = bool(getattr(self, "_stop_event", None) and self._stop_event.is_set())
        elif job.get("cancel_requested") and job.get("running"):
            job["running"] = False
        return job

    def _set_job(self, **kwargs: Any) -> None:
        """更新任务状态并记录运行实例、线程和心跳信息。"""
        with self._lock:
            job = self._job()
            job.update(kwargs)
            if getattr(self, "_run_id", ""):
                job["run_id"] = self._run_id
            current = threading.current_thread()
            job["thread_name"] = current.name
            job["thread_id"] = current.ident
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")
            job["heartbeat_at"] = time.time()
            self.save_data(self.DATA_JOB, job)

    def _recover_stale_job(self) -> None:
        """插件初始化时把没有对应内存线程的遗留运行状态标记为中断。"""
        job = self._job()
        if not job.get("running"):
            return
        job.update({
            "running": False,
            "phase": "interrupted",
            "message": "上次任务因插件重载、线程退出或服务重启而中断",
            "error": "检测到持久化状态仍为运行，但当前插件实例没有对应工作线程，请重新生成预演",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "heartbeat_at": time.time(),
        })
        self.save_data(self.DATA_JOB, job)

    def _worker_entry(self, target: Any, name: str, run_id: str, args: Tuple[Any, ...]) -> None:
        """统一执行后台任务，保留目标函数写入的最终状态并记录完整异常。"""
        self._run_id = run_id
        failed = False
        try:
            logger.info(f"{self.plugin_name} 后台任务启动：phase={name}, run_id={run_id}, thread={threading.get_ident()}")
            self._set_job(running=True, phase=name, message="后台线程已启动", error="")
            target(*args)
        except BaseException as err:
            failed = True
            error_trace = traceback.format_exc()
            logger.error(
                f"{self.plugin_name} 后台任务异常退出：phase={name}, run_id={run_id}, "
                f"error={err.__class__.__name__}: {err}\n{error_trace}"
            )
            try:
                self._set_job(
                    running=False,
                    busy=False,
                    cancel_requested=False,
                    phase="cancelled" if self._stop_event.is_set() else "failed",
                    message="任务已取消" if self._stop_event.is_set() else "任务异常退出",
                    error="" if self._stop_event.is_set() else f"{err.__class__.__name__}: {err}",
                    traceback=error_trace[-8000:],
                )
            except BaseException:
                logger.error(
                    f"{self.plugin_name} 写入任务失败状态时再次异常：run_id={run_id}\n"
                    f"{traceback.format_exc()}"
                )
        finally:
            if not failed:
                logger.info(f"{self.plugin_name} 后台任务已由目标函数收口：phase={name}, run_id={run_id}")
            logger.info(f"{self.plugin_name} 后台任务退出：phase={name}, run_id={run_id}")

    def _start_worker(self, target: Any, name: str, *args: Any) -> Tuple[bool, str]:
        """启动一个互斥且具备完整退出收口的后台任务。"""
        with self._lock:
            if self._worker and self._worker.is_alive():
                job = self._job()
                if job.get("cancel_requested"):
                    return False, "已有任务正在取消，请等待线程退出"
                return False, "已有任务正在运行"
            if self._worker and not self._worker.is_alive():
                self._worker = None
            self._stop_event.clear()
            run_id = uuid.uuid4().hex[:12]
            self._run_id = run_id
            self._set_job(
                running=True,
                phase="starting",
                progress=0,
                message="正在创建后台线程",
                error="",
                run_id=run_id,
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._worker = threading.Thread(
                target=self._worker_entry,
                args=(target, name, run_id, args),
                daemon=True,
                name=f"{self.__class__.__name__}-{name}",
            )
            try:
                self._worker.start()
            except BaseException as err:
                self._set_job(running=False, phase="failed", message="后台线程启动失败", error=f"{err.__class__.__name__}: {err}")
                self._worker = None
                return False, f"后台线程启动失败：{err}"
        return True, f"任务已启动，run_id={run_id}"

    def api_status(self) -> schemas.Response:
        """返回配置、媒体库、预演计划及任务状态。"""
        services = self._services()
        plan = self.get_data(self.DATA_PLAN) or {}
        managed = self.get_data(self.DATA_STATE) or {}
        return schemas.Response(success=True, data={
            "config": self._current_config(),
            "servers": [{"title": name, "value": name} for name in services],
            "libraries": self._library_options(services),
            "plan": plan,
            "job": self._job(),
            "managed_count": len(managed) if isinstance(managed, dict) else 0,
        })

    def api_save_config(self, payload: dict = Body(...)) -> schemas.Response:
        """保存联邦配置页面提交的设置。"""
        with self._lock:
            worker = getattr(self, "_worker", None)
            if worker and worker.is_alive():
                return schemas.Response(success=False, message="后台任务运行中，暂时不能修改配置")
            # 运行期间配置不可变；所有字段在同一临界区内更新并落盘。
            self._enabled = bool(payload.get("enabled"))
            self._show_sidebar_nav = bool(payload.get("show_sidebar_nav", True))
            self._server = str(payload.get("server") or "")
            self._libraries = [str(item) for item in payload.get("libraries") or [] if item]
            self._overwrite_images = bool(payload.get("overwrite_images", True))
            self._delete_empty = bool(payload.get("delete_empty", True))
            self._sync_logo = bool(payload.get("sync_logo", True))
            self._save_config()
        return schemas.Response(success=True, message="配置已保存", data=self.api_status().data)

    def api_start_scan(self) -> schemas.Response:
        """启动只读扫描并生成预演计划。"""
        name, service = self._selected_service()
        if not service:
            return schemas.Response(success=False, message="请先选择可用的 Emby 服务器")
        if not self._libraries:
            return schemas.Response(success=False, message="请至少选择一个电影库")
        success, message = self._start_worker(self._scan_worker, "scan", name, service)
        return schemas.Response(success=success, message=message)

    def api_start_apply(self, payload: Optional[dict] = Body(default=None)) -> schemas.Response:
        """启动经用户审核的计划执行任务。"""
        payload = payload or {}
        plan = self.get_data(self.DATA_PLAN) or {}
        if not plan or not plan.get("collections"):
            return schemas.Response(success=False, message="没有可执行的预演计划")
        selected = [str(item) for item in payload.get("selected") or [] if item is not None]
        adopted = [str(item) for item in payload.get("adopted") or [] if item is not None]
        if not selected:
            return schemas.Response(success=False, message="请至少选择一个合集变更")
        plan_id = str(payload.get("plan_id") or "")
        current_plan_id = str(plan.get("plan_id") or "")
        if not plan_id or not current_plan_id or plan_id != current_plan_id:
            return schemas.Response(success=False, message="预演计划已换代或缺少计划 ID，请重新生成预演")
        known_keys = {str(row.get("key")) for row in plan.get("collections") or []}
        unknown_keys = sorted(set(selected) - known_keys)
        if unknown_keys:
            return schemas.Response(success=False, message=f"计划中不存在所选合集：{', '.join(unknown_keys[:8])}")
        unknown_adoptions = sorted(set(adopted) - set(selected))
        if unknown_adoptions:
            return schemas.Response(success=False, message="接管确认必须来自已选择的合集")
        name, service = self._selected_service()
        if not service or name != plan.get("server"):
            return schemas.Response(success=False, message="当前 Emby 服务器与预演计划不一致，请重新预演")
        success, message = self._start_worker(self._apply_worker, "apply", service, selected, adopted, plan_id)
        return schemas.Response(success=success, message=message)

    def api_cancel(self) -> schemas.Response:
        """请求取消当前扫描或执行任务。"""
        with self._lock:
            worker = self._worker
            if not worker or not worker.is_alive():
                self._worker = None
                return schemas.Response(success=False, message="当前没有正在运行的任务")
            self._stop_event.set()
            self._set_job(
                running=True,
                busy=True,
                cancel_requested=True,
                phase="cancelling",
                message="已请求取消，正在等待后台任务安全退出",
                error="",
            )
        return schemas.Response(success=True, message="已请求取消当前任务")

    def _scan_worker(self, server_name: str, service: Any) -> None:
        """后台扫描 Emby 与 TMDB 并构建变更计划。"""
        try:
            self._set_job(phase="loading_movies", progress=1, message="正在读取所选 Emby 电影库")
            movies = self._load_movies(service)
            if not movies:
                raise RuntimeError("选定电影库中没有可扫描的电影")
            self._check_stopped()
            self._set_job(
                phase="loading_collections",
                progress=4,
                current=0,
                total=len(movies),
                message=f"已读取 {len(movies)} 部电影，正在读取 Emby 合集",
            )
            boxsets = self._load_boxsets(service)
            managed = self.get_data(self.DATA_STATE) or {}
            cache = self.get_data(self.DATA_CACHE) or {}
            if not isinstance(cache, dict):
                cache = {}
            self._prune_tmdb_cache(cache)
            groups: Dict[str, dict] = {}
            anomalies: List[dict] = []
            total = len(movies)
            cache_hits = 0
            queried = 0
            cache_dirty = False
            new_since_flush = 0
            self._set_job(
                phase="recognizing_movies",
                progress=5,
                current=0,
                total=total,
                message=f"开始识别 0/{total} 部电影",
            )
            for index, movie in enumerate(movies, start=1):
                self._check_stopped()
                tmdb_id = str((movie.get("ProviderIds") or {}).get("Tmdb") or "")
                if not tmdb_id:
                    anomalies.append({"name": movie.get("Name"), "id": movie.get("Id"), "reason": "缺少 TMDB ID"})
                else:
                    cache_key = f"movie:{tmdb_id}"
                    detail = self._cache_get(cache, cache_key)
                    if isinstance(detail, dict):
                        cache_hits += 1
                    else:
                        queried += 1
                        detail, query_error = self._query_movie_detail(int(tmdb_id))
                        if detail is None:
                            anomalies.append({
                                "name": movie.get("Name"),
                                "id": movie.get("Id"),
                                "tmdb_id": tmdb_id,
                                "reason": f"TMDB 查询失败：{query_error}",
                            })
                            detail = {}
                        else:
                            self._cache_set(cache, cache_key, detail)
                            cache_dirty = True
                            new_since_flush += 1
                    collection = detail.get("belongs_to_collection") or {}
                    collection_id = str(collection.get("id") or "")
                    if collection_id:
                        group = groups.setdefault(collection_id, {"movies": [], "seed": collection})
                        group["movies"].append({"id": str(movie.get("Id")), "name": movie.get("Name"), "tmdb_id": tmdb_id})
                if index == 1 or index == total or index % 5 == 0:
                    progress = max(5, min(75, 5 + round(index * 70 / total)))
                    self._set_job(
                        phase="recognizing_movies",
                        progress=progress,
                        current=index,
                        total=total,
                        cache_hits=cache_hits,
                        queried=queried,
                        message=f"已识别 {index}/{total} 部电影（缓存 {cache_hits}，查询 {queried}）",
                    )
                if cache_dirty and new_since_flush >= self.CACHE_FLUSH_INTERVAL:
                    self.save_data(self.DATA_CACHE, cache)
                    cache_dirty = False
                    new_since_flush = 0
            self._prune_tmdb_cache(cache)
            self.save_data(self.DATA_CACHE, cache)

            rows = []
            boxsets_by_name = {str(item.get("Name") or "").casefold(): item for item in boxsets}
            boxsets_by_id = {str(item.get("Id")): item for item in boxsets}
            all_target_movie_ids = {movie["id"] for group in groups.values() for movie in group["movies"]}
            collection_total = len(groups)
            self._set_job(
                phase="building_plan",
                progress=76,
                current=0,
                total=collection_total,
                message=f"电影识别完成，正在生成 0/{collection_total} 个合集计划",
            )
            for collection_index, (collection_id, group) in enumerate(groups.items(), start=1):
                self._check_stopped()
                metadata = self._collection_metadata(int(collection_id), group["seed"])
                state_key = f"{server_name}:{collection_id}"
                state = managed.get(state_key) if isinstance(managed, dict) else None
                boxset = boxsets_by_id.get(str((state or {}).get("emby_id"))) if state else None
                candidate = None
                if not boxset:
                    candidate = boxsets_by_name.get(str(metadata["name"]).casefold())
                current_source = boxset or candidate
                current = self._load_boxset_members(service, str(current_source.get("Id"))) if current_source else []
                current_ids = {str(item.get("Id")) for item in current}
                desired_ids = {item["id"] for item in group["movies"]}
                current_member_ids, current_member_hash = self._member_snapshot(current)
                rows.append({
                    "key": collection_id,
                    "tmdb_id": int(collection_id),
                    "name": metadata["name"],
                    "emby_id": str(boxset.get("Id")) if boxset else "",
                    "candidate_emby_id": str(candidate.get("Id")) if candidate else "",
                    "candidate_name": candidate.get("Name") if candidate else "",
                    "managed": bool(boxset and state),
                    "requires_adoption": bool(candidate and not state),
                    "create": not boxset and not candidate,
                    "desired_movies": group["movies"],
                    "add": [item for item in group["movies"] if item["id"] not in current_ids],
                    "remove": [item for item in current if str(item.get("Id")) not in desired_ids] if (boxset and state) or candidate else [],
                    "poster": metadata.get("poster"),
                    "poster_language": metadata.get("poster_language"),
                    "logo": metadata.get("logo"),
                    "logo_language": metadata.get("logo_language"),
                    "current_member_ids": current_member_ids,
                    "current_member_hash": current_member_hash,
                })
                if collection_index == 1 or collection_index == collection_total or collection_index % 3 == 0:
                    progress = 76 if not collection_total else min(96, 76 + round(collection_index * 20 / collection_total))
                    self._set_job(
                        phase="building_plan",
                        progress=progress,
                        current=collection_index,
                        total=collection_total,
                        message=f"正在生成 {collection_index}/{collection_total} 个合集计划",
                    )

            represented = set(groups)
            if isinstance(managed, dict):
                for state_key, state in managed.items():
                    if not state_key.startswith(f"{server_name}:"):
                        continue
                    collection_id = state_key.split(":", 1)[1]
                    if collection_id in represented:
                        continue
                    boxset = boxsets_by_id.get(str((state or {}).get("emby_id")))
                    if not boxset:
                        continue
                    current = self._load_boxset_members(service, str(boxset.get("Id")))
                    selected_current = [item for item in current if str(item.get("Id")) in {str(movie.get("Id")) for movie in movies}]
                    if selected_current:
                        current_member_ids, current_member_hash = self._member_snapshot(current)
                        rows.append({
                            "key": collection_id,
                            "tmdb_id": int(collection_id),
                            "name": boxset.get("Name") or (state or {}).get("name") or collection_id,
                            "emby_id": str(boxset.get("Id")),
                            "managed": True,
                            "requires_adoption": False,
                            "create": False,
                            "desired_movies": [],
                            "add": [],
                            "remove": selected_current,
                            "poster": None,
                            "logo": None,
                            "current_member_ids": current_member_ids,
                            "current_member_hash": current_member_hash,
                        })

            summary = {
                "movies": len(movies),
                "collections": len(rows),
                "create": sum(1 for row in rows if row.get("create")),
                "adopt": sum(1 for row in rows if row.get("requires_adoption")),
                "add": sum(len(row.get("add") or []) for row in rows),
                "remove": sum(len(row.get("remove") or []) for row in rows),
                "anomalies": len(anomalies),
            }
            plan = {
                "plan_id": uuid.uuid4().hex[:16],
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "server": server_name,
                "libraries": list(self._libraries),
                "config_fingerprint": self._config_fingerprint(),
                "summary": summary,
                "collections": sorted(rows, key=lambda row: row.get("name") or ""),
                "anomalies": anomalies,
            }
            self._check_stopped()
            self.save_data(self.DATA_PLAN, plan)
            self._set_job(running=False, phase="done", progress=100, message="预演计划生成完成", error="")
        except BaseException as err:
            error_trace = traceback.format_exc()
            logger.error(
                f"Emby TMDB 合集预演失败：run_id={self._run_id}, "
                f"error={err.__class__.__name__}: {err}\n{error_trace}"
            )
            cancelled = self._stop_event.is_set()
            self._set_job(
                running=False,
                busy=False,
                cancel_requested=False,
                phase="cancelled" if cancelled else "failed",
                message="预演已取消" if cancelled else "预演失败",
                error="" if cancelled else f"{err.__class__.__name__}: {err}",
                traceback=error_trace[-8000:],
            )

    def _query_movie_detail(self, tmdb_id: int) -> Tuple[Optional[dict], str]:
        """绕过错误响应缓存查询电影详情，并对 TMDB 临时后端错误退避重试。"""
        url = f"https://{settings.TMDB_API_DOMAIN}/3/movie/{tmdb_id}"
        params = {
            "api_key": settings.TMDB_API_KEY,
            "language": "zh-CN",
            "append_to_response": "translations",
        }
        last_error = "未知错误"
        for attempt in range(1, 4):
            self._check_stopped()
            try:
                response = RequestUtils(
                    ua=settings.NORMAL_USER_AGENT,
                    proxies=settings.PROXY,
                    timeout=20,
                ).get_res(url, params=params)
                if response and response.status_code == 200:
                    data = response.json()
                    if isinstance(data, dict) and data.get("success") is not False:
                        return self._compact_tmdb_detail(data), ""
                    last_error = str((data or {}).get("status_message") or "TMDB 返回错误响应")
                elif response:
                    try:
                        data = response.json()
                    except Exception:
                        data = {}
                    last_error = f"HTTP {response.status_code}: {data.get('status_message') or response.text[:160]}"
                else:
                    last_error = "TMDB 请求无响应"
            except Exception as err:
                last_error = f"{err.__class__.__name__}: {err}"
            if attempt < 3:
                self._set_job(
                    phase="recognizing_movies",
                    message=f"TMDB 临时错误，{attempt}/3 次重试后继续",
                    last_tmdb_error=last_error,
                )
                if self._stop_event.wait(1.5 * attempt):
                    raise RuntimeError("任务因插件停止或重载而中断")
        return None, last_error

    def _check_stopped(self) -> None:
        """检查取消请求，并用持久 run_id 阻止旧线程继续写入。"""
        if self._stop_event.is_set():
            raise RuntimeError("任务因插件停止或重载而中断")
        expected_run_id = str(getattr(self, "_run_id", "") or "")
        job = self.get_data(self.DATA_JOB) or {}
        current_run_id = str(job.get("run_id") or "") if isinstance(job, dict) else ""
        if expected_run_id and current_run_id and current_run_id != expected_run_id:
            raise RuntimeError("任务 run_id 已过期，拒绝旧线程继续执行")

    def _load_movies(self, service: Any) -> List[dict]:
        """逐库读取所选 Emby 电影，并持续反馈加载进度。"""
        movies: List[dict] = []
        library_total = len(self._libraries)
        for library_index, library_id in enumerate(self._libraries, start=1):
            self._check_stopped()
            self._set_job(
                phase="loading_movies",
                progress=max(1, round((library_index - 1) * 3 / max(library_total, 1)) + 1),
                current=library_index - 1,
                total=library_total,
                message=f"正在读取电影库 {library_index}/{library_total}",
            )
            url = (f"[HOST]emby/Users/[USER]/Items?ParentId={library_id}&Recursive=true&"
                   "IncludeItemTypes=Movie&Fields=ProviderIds,ProductionYear,Path&Limit=100000&api_key=[APIKEY]")
            response = service.instance.get_data(url=url)
            if not response or response.status_code != 200:
                raise RuntimeError(f"读取 Emby 电影库 {library_id} 失败")
            movies.extend(response.json().get("Items") or [])
            self._set_job(
                phase="loading_movies",
                progress=min(4, round(library_index * 4 / max(library_total, 1))),
                current=library_index,
                total=library_total,
                message=f"已读取电影库 {library_index}/{library_total}，累计 {len(movies)} 部电影",
            )
        unique = {str(item.get("Id")): item for item in movies if item.get("Id")}
        return list(unique.values())

    @staticmethod
    def _load_boxsets(service: Any) -> List[dict]:
        """读取 Emby 中的全部合集。"""
        url = ("[HOST]emby/Users/[USER]/Items?Recursive=true&IncludeItemTypes=BoxSet&"
               "Fields=ProviderIds,ImageTags,LockedFields&Limit=100000&api_key=[APIKEY]")
        response = service.instance.get_data(url=url)
        if not response or response.status_code != 200:
            raise RuntimeError("读取 Emby 合集失败")
        return response.json().get("Items") or []

    @staticmethod
    def _load_boxset_members(service: Any, boxset_id: str) -> List[dict]:
        """读取一个 Emby 合集的电影成员；HTTP 或解析失败必须抛错。"""
        if not boxset_id:
            return []
        url = (f"[HOST]emby/Users/[USER]/Items?ParentId={boxset_id}&Recursive=true&"
               "IncludeItemTypes=Movie&Fields=ProviderIds,ProductionYear&Limit=100000&api_key=[APIKEY]")
        try:
            response = service.instance.get_data(url=url)
        except Exception as err:
            raise CollectionMemberReadError(f"读取 Emby 合集 {boxset_id} 成员请求异常：{err}") from err
        if not response:
            raise CollectionMemberReadError(f"读取 Emby 合集 {boxset_id} 成员失败：无响应")
        if response.status_code != 200:
            raise CollectionMemberReadError(f"读取 Emby 合集 {boxset_id} 成员失败：HTTP {response.status_code}")
        try:
            data = response.json()
        except Exception as err:
            raise CollectionMemberReadError(f"解析 Emby 合集 {boxset_id} 成员响应失败：{err}") from err
        if not isinstance(data, dict) or not isinstance(data.get("Items"), list):
            raise CollectionMemberReadError(f"解析 Emby 合集 {boxset_id} 成员响应失败：缺少 Items 列表")
        return data["Items"]

    def _collection_metadata(self, collection_id: int, seed: dict) -> Dict[str, Any]:
        """按 zh-CN、zh-SG、zh-TW、zh-HK、英文顺序解析合集名称和 TMDB 图片。"""
        try:
            translations = self._tmdb_zh_cn.collection.translations(collection_id) or []
        except Exception:
            translations = []
        translation_map = {
            self._normalize_translation_locale(item): item.get("data") or {}
            for item in translations if isinstance(item, dict)
        }
        name = next((
            translation_map.get(key, {}).get("title")
            for key in (
                ("zh", "CN"), ("zh", "SG"), ("zh", "TW"), ("zh", "HK"),
                ("en", "US"), ("en", "GB"),
            )
            if translation_map.get(key, {}).get("title")
        ), seed.get("name") or str(collection_id))
        images = self._query_collection_images(collection_id)
        poster_path, poster_language = self._pick_tmdb_image(images.get("posters") or [])
        logo_path, logo_language = self._pick_tmdb_image(images.get("logos") or [])
        poster = settings.TMDB_IMAGE_URL(poster_path) if poster_path else None
        logo = settings.TMDB_IMAGE_URL(logo_path) if logo_path else None
        return {
            "name": name,
            "poster": poster,
            "poster_language": poster_language,
            "logo": logo,
            "logo_language": logo_language,
        }

    @classmethod
    def _query_collection_images(cls, collection_id: int) -> Dict[str, Any]:
        """显式请求全部中文地区候选，避免 TMDB 客户端默认语言过滤掉回退层。"""
        url = f"https://{settings.TMDB_API_DOMAIN}/3/collection/{collection_id}/images"
        params = {
            "api_key": settings.TMDB_API_KEY,
            "language": "zh-CN",
            "include_image_language": cls.COLLECTION_IMAGE_LANGUAGES,
        }
        try:
            response = RequestUtils(
                ua=settings.NORMAL_USER_AGENT,
                proxies=settings.PROXY,
                timeout=20,
            ).get_res(url, params=params)
            if not response or response.status_code != 200:
                status = getattr(response, "status_code", "无响应")
                logger.warning(f"读取 TMDB 合集 {collection_id} 图片失败：HTTP {status}")
                return {}
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception as err:
            logger.warning(f"读取 TMDB 合集 {collection_id} 图片异常：{err}")
            return {}

    @staticmethod
    def _normalize_translation_locale(item: dict) -> Tuple[str, str]:
        """统一 TMDB translation 的语言大小写及下划线/连字符地区格式。"""
        if not isinstance(item, dict):
            return "", ""
        language = str(item.get("iso_639_1") or "").strip().replace("_", "-").lower()
        country = str(item.get("iso_3166_1") or "").strip().replace("_", "-").upper()
        if "-" in language:
            language, embedded_country = language.split("-", 1)
            country = embedded_country.upper() or country
        return language, country

    @staticmethod
    def _pick_tmdb_image(images: List[dict]) -> Tuple[Optional[str], Optional[str]]:
        """按 zh-CN、zh-SG、zh-TW、zh-HK、泛 zh、英文、无语言排序 TMDB 图片。"""
        def priority(image: dict) -> int:
            """返回兼容复合语言和独立地区字段的图片优先级。"""
            return EmbyTmdbCollectionSync._tmdb_image_language_priority(image)

        candidates = sorted(images, key=lambda image: (
            priority(image),
            -(image.get("vote_average") or 0),
            -(image.get("vote_count") or 0),
            -(image.get("width") or 0),
        ))
        if not candidates:
            return None, None
        chosen = candidates[0]
        label = EmbyTmdbCollectionSync._normalize_tmdb_image_language(chosen)
        return chosen.get("file_path"), label

    @staticmethod
    def _normalize_tmdb_image_language(image: dict) -> str:
        """规范化 TMDB 图片语言标签，兼容 zh-CN 与 zh+CN 两种返回格式。"""
        if not isinstance(image, dict):
            return "null"
        raw_language = str(image.get("iso_639_1") or "").strip().replace("_", "-").replace("+", "-")
        raw_country = str(image.get("iso_3166_1") or "").strip().upper()
        language = raw_language.lower()
        country = raw_country
        if "-" in language:
            language, composite_country = language.split("-", 1)
            embedded = composite_country.upper()
            # zh-Hans/zh-Hant 是文字脚本而非地区；已有有效地区字段时不能被脚本覆盖。
            if len(embedded) == 2 and embedded.isalpha():
                country = embedded
        elif len(language) > 2 and language.startswith("zh"):
            country = language[2:].upper() or country
            language = "zh"
        elif len(language) > 2 and language.startswith("en"):
            country = language[2:].upper() or country
            language = "en"
        if language == "zh" and country in {"CN", "SG", "TW", "HK"}:
            return f"zh-{country}"
        if language == "zh":
            return "zh"
        if language == "en":
            return "en"
        return "null" if not language else language

    @staticmethod
    def _tmdb_image_language_priority(image: dict) -> int:
        """返回 TMDB 图片地区优先级，未知语言排在英文和 null 之后。"""
        label = EmbyTmdbCollectionSync._normalize_tmdb_image_language(image)
        return {
            "zh-CN": 0,
            "zh-SG": 1,
            "zh-TW": 2,
            "zh-HK": 3,
            "zh": 4,
            "en": 5,
            "null": 6,
        }.get(label, 9)

    def _validate_plan_row(self, service: Any, row: dict) -> Tuple[Optional[str], str]:
        """在写入前立即重读单个合集，返回目标 ID 或陈旧原因。"""
        existing_boxsets = self._load_boxsets(service)
        by_id = {str(item.get("Id")): item for item in existing_boxsets if item.get("Id")}
        by_name = {str(item.get("Name") or "").casefold(): item for item in existing_boxsets if item.get("Name")}
        name = str(row.get("name") or row.get("key"))
        expected_hash = str(row.get("current_member_hash") or "")
        if not expected_hash:
            return None, f"{name}：预演计划缺少成员快照，请重新生成预演"
        target_id = str(row.get("emby_id") or row.get("candidate_emby_id") or "")
        if target_id:
            if target_id not in by_id:
                return None, f"{name}：Emby 合集已不存在，请重新生成预演"
            current = self._load_boxset_members(service, target_id)
        else:
            # 新建计划若在预演后出现同名合集，不能把它当作空集合覆盖。
            candidate = by_name.get(name.casefold())
            if candidate:
                return None, f"{name}：预演后出现同名 Emby 合集，请重新生成预演"
            current = []
        _, actual_hash = self._member_snapshot(current)
        if actual_hash != expected_hash:
            return None, f"{name}：成员已变化（预演 {expected_hash[:8]}，当前 {actual_hash[:8]}），已跳过"
        return target_id or None, ""

    def _validate_plan_rows(self, service: Any, plan: dict, rows: List[dict]) -> Tuple[List[dict], List[str]]:
        """首次执行前逐项重读合集成员，返回未变化行和陈旧计划错误。"""
        valid: List[dict] = []
        errors: List[str] = []
        for row in rows:
            _, error = self._validate_plan_row(service, row)
            if error:
                errors.append(error)
                continue
            valid.append(row)
        return valid, errors

    def _apply_worker(
        self,
        service: Any,
        selected: List[str],
        adopted: List[str],
        plan_id: Optional[str] = None,
    ) -> None:
        """后台执行用户审核通过的合集变更，并逐项持久化恢复状态。"""
        try:
            plan = self.get_data(self.DATA_PLAN) or {}
            managed = self.get_data(self.DATA_STATE) or {}
            expected_plan_id = str(plan.get("plan_id") or "")
            if not plan_id or not expected_plan_id or str(plan_id) != expected_plan_id:
                raise RuntimeError("预演计划已换代，拒绝执行旧计划")
            rows = [row for row in plan.get("collections") or [] if str(row.get("key")) in selected]
            if not rows:
                raise RuntimeError("所选合集不在当前预演计划中，请重新生成预演")
            expected_fingerprint = str(plan.get("config_fingerprint") or "")
            current_fingerprint = self._config_fingerprint()
            if not expected_fingerprint or expected_fingerprint != current_fingerprint:
                raise RuntimeError("插件配置已在预演后发生变化，请重新生成预演再执行")
            missing_adoptions = [
                str(row.get("name") or row.get("key"))
                for row in rows
                if row.get("requires_adoption") and str(row.get("key")) not in adopted
            ]
            if missing_adoptions:
                preview = "、".join(missing_adoptions[:8])
                suffix = f" 等 {len(missing_adoptions)} 个" if len(missing_adoptions) > 8 else ""
                raise RuntimeError(f"以下同名合集尚未确认接管：{preview}{suffix}")

            rows, stale_errors = self._validate_plan_rows(service, plan, rows)
            if not rows:
                raise RuntimeError("没有可执行的未变化合集：" + "；".join(stale_errors[:8]))
            existing_boxsets = self._load_boxsets(service)
            boxsets_by_name = {str(item.get("Name") or "").casefold(): item for item in existing_boxsets}
            total = max(len(rows), 1)
            errors: List[str] = list(stale_errors)
            success_count = 0
            for index, row in enumerate(rows, start=1):
                self._check_stopped()
                # 批量预校验后，在真正写入该行前再次读取，缩小 TOCTOU 窗口。
                target_id, stale_error = self._validate_plan_row(service, row)
                if stale_error:
                    errors.append(stale_error)
                    self._set_job(
                        phase="applying",
                        progress=round(index * 100 / total),
                        current=index,
                        total=len(rows),
                        message=f"已跳过陈旧合集 {index}/{len(rows)}",
                    )
                    continue
                key = str(row.get("key"))
                state_key = f"{plan.get('server')}:{key}"
                try:
                    self._check_stopped()
                    current_plan = self.get_data(self.DATA_PLAN) or {}
                    if str(current_plan.get("plan_id") or "") != expected_plan_id:
                        raise RuntimeError("预演计划已换代，停止执行旧计划")
                    emby_id = str(target_id or row.get("emby_id") or "")
                    if not emby_id and row.get("requires_adoption"):
                        emby_id = str(row.get("candidate_emby_id") or "")
                    if not emby_id:
                        existing = boxsets_by_name.get(str(row.get("name") or "").casefold())
                        if existing:
                            emby_id = str(existing.get("Id") or "")
                    if not emby_id:
                        self._check_stopped()
                        emby_id = self._create_boxset(
                            service,
                            row.get("name"),
                            [item["id"] for item in row.get("desired_movies") or []],
                        )
                        boxsets_by_name[str(row.get("name") or "").casefold()] = {
                            "Id": emby_id,
                            "Name": row.get("name"),
                        }
                    else:
                        self._check_stopped()
                        self._change_members(service, emby_id, row.get("add") or [], row.get("remove") or [])
                    if row.get("poster"):
                        self._check_stopped()
                        self._upload_image(service, emby_id, "Primary", row["poster"])
                    if self._sync_logo and row.get("logo"):
                        self._check_stopped()
                        self._upload_image(service, emby_id, "Logo", row["logo"])
                    self._check_stopped()
                    remaining = self._load_boxset_members(service, emby_id)
                    if not remaining and self._delete_empty:
                        self._check_stopped()
                        self._delete_boxset(service, emby_id)
                        managed.pop(state_key, None)
                    else:
                        managed[state_key] = {
                            "tmdb_id": int(key),
                            "emby_id": emby_id,
                            "name": row.get("name"),
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        }
                    self._check_stopped()
                    self.save_data(self.DATA_STATE, managed)
                    success_count += 1
                except Exception as err:
                    if isinstance(err, CollectionMemberReadError):
                        # 成员读取失败时必须停止整个 apply，不能继续处理后续行。
                        raise
                    error_trace = traceback.format_exc()
                    errors.append(f"{row.get('name')}: {err}")
                    logger.error(f"处理合集 {row.get('name')} 失败：{err}\n{error_trace}")
                self._set_job(
                    phase="applying",
                    progress=round(index * 100 / total),
                    current=index,
                    total=len(rows),
                    message=f"已处理 {index}/{len(rows)} 个合集",
                )
            message = f"执行完成，成功 {success_count}，失败 {len(errors)}"
            self._set_job(
                running=False,
                phase="done" if not errors else "partial",
                progress=100,
                message=message,
                error="\n".join(errors),
            )
        except Exception as err:
            error_trace = traceback.format_exc()
            logger.error(f"执行 Emby TMDB 合集计划失败：{err}\n{error_trace}")
            cancelled = self._stop_event.is_set()
            self._set_job(
                running=False,
                busy=False,
                cancel_requested=False,
                phase="cancelled" if cancelled else "failed",
                message="执行已取消" if cancelled else "执行失败",
                error="" if cancelled else f"{err.__class__.__name__}: {err}",
                traceback=error_trace[-8000:],
            )

    @staticmethod
    def _create_boxset(service: Any, name: str, movie_ids: List[str]) -> str:
        """创建 Emby 合集并返回 Item ID。"""
        from urllib.parse import quote
        ids = ",".join(movie_ids)
        url = f"[HOST]emby/Collections?Name={quote(str(name or ''))}&Ids={ids}&IsLocked=true&api_key=[APIKEY]"
        response = service.instance.post_data(url=url)
        if not response or response.status_code not in (200, 204):
            raise RuntimeError("创建 Emby 合集失败")
        data = response.json() if response.content else {}
        emby_id = str(data.get("Id") or data.get("id") or "")
        if not emby_id:
            raise RuntimeError("Emby 创建合集后未返回 Item ID")
        return emby_id

    @staticmethod
    def _change_members(service: Any, boxset_id: str, add: List[dict], remove: List[dict]) -> None:
        """增加和移除 Emby 合集成员。"""
        if add:
            ids = ",".join(str(item.get("id")) for item in add if item.get("id"))
            response = service.instance.post_data(url=f"[HOST]emby/Collections/{boxset_id}/Items?Ids={ids}&api_key=[APIKEY]")
            if not response or response.status_code not in (200, 204):
                raise RuntimeError("加入合集成员失败")
        if remove:
            ids = ",".join(str(item.get("Id") or item.get("id")) for item in remove if item.get("Id") or item.get("id"))
            config = service.config.config or {}
            host = str(config.get("host") or "").rstrip("/")
            apikey = str(config.get("apikey") or "")
            response = RequestUtils().delete_res(f"{host}/emby/Collections/{boxset_id}/Items", params={"Ids": ids, "api_key": apikey})
            if not response or response.status_code not in (200, 204):
                raise RuntimeError("移除错误合集成员失败")

    def _upload_image(self, service: Any, item_id: str, image_type: str, image_url: str) -> None:
        """下载 TMDB 图片并上传为 Emby 合集图片。"""
        if not self._overwrite_images:
            item = service.instance.get_data(url=f"[HOST]emby/Users/[USER]/Items/{item_id}?api_key=[APIKEY]")
            tags = (item.json().get("ImageTags") or {}) if item and item.status_code == 200 else {}
            if tags.get(image_type):
                return
        image = RequestUtils(proxies=settings.PROXY, timeout=30).get_res(image_url)
        if not image or image.status_code != 200 or not image.content:
            raise RuntimeError(f"下载 {image_type} 图片失败")
        content_type = image.headers.get("Content-Type") or "image/jpeg"
        response = service.instance.post_data(
            url=f"[HOST]emby/Items/{item_id}/Images/{image_type}?api_key=[APIKEY]",
            data=image.content,
            headers={"Content-Type": content_type},
        )
        if not response or response.status_code not in (200, 204):
            raise RuntimeError(f"上传 {image_type} 图片失败")

    @staticmethod
    def _delete_boxset(service: Any, item_id: str) -> None:
        """删除已清空且由插件管理的 Emby 合集。"""
        config = service.config.config or {}
        host = str(config.get("host") or "").rstrip("/")
        apikey = str(config.get("apikey") or "")
        response = RequestUtils().delete_res(f"{host}/emby/Items/{item_id}", params={"api_key": apikey})
        if not response or response.status_code not in (200, 204):
            raise RuntimeError("删除空合集失败")
