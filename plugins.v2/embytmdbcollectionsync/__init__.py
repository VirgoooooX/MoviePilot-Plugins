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


class EmbyTmdbCollectionSync(_PluginBase):
    """按照 TMDB 官方合集整理 Emby 电影，并同步合集图片。"""

    plugin_name = "Emby TMDB 合集整理"
    plugin_desc = "基于 TMDB 官方合集预演并校正 Emby 电影合集，支持成员审核、封面徽标同步和中断恢复。"
    plugin_icon = "mediaplay.png"
    plugin_version = "1.0.0"
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
        self._tmdb_zh_sg = TmdbApi(language="zh-SG")
        self._tmdb_en = TmdbApi(language="en-US")
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
        ]

    def stop_service(self) -> None:
        """请求停止后台任务，并把未结束任务记录为插件生命周期中断。"""
        worker = getattr(self, "_worker", None)
        stop_event = getattr(self, "_stop_event", None)
        if stop_event:
            stop_event.set()
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=3)
            if worker.is_alive():
                self._set_job(
                    running=False,
                    phase="interrupted",
                    message="插件重载时任务未在 3 秒内退出，已标记中断",
                    error="任务因插件停止或重载而中断，请重新生成预演",
                )
        self._worker = None

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
        return job if isinstance(job, dict) else {}

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
                    phase="failed",
                    message="任务异常退出",
                    error=f"{err.__class__.__name__}: {err}",
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
                return False, "已有任务正在运行"
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
        selected = [str(item) for item in payload.get("selected") or []]
        adopted = [str(item) for item in payload.get("adopted") or []]
        if not selected:
            return schemas.Response(success=False, message="请至少选择一个合集变更")
        name, service = self._selected_service()
        if not service or name != plan.get("server"):
            return schemas.Response(success=False, message="当前 Emby 服务器与预演计划不一致，请重新预演")
        success, message = self._start_worker(self._apply_worker, "apply", service, selected, adopted)
        return schemas.Response(success=success, message=message)

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
            groups: Dict[str, dict] = {}
            anomalies: List[dict] = []
            total = len(movies)
            cache_hits = 0
            queried = 0
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
                    detail = cache.get(f"movie:{tmdb_id}")
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
                            cache[f"movie:{tmdb_id}"] = detail
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
                if index == 1 or index % 25 == 0:
                    self.save_data(self.DATA_CACHE, cache)
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
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "server": server_name,
                "libraries": list(self._libraries),
                "summary": summary,
                "collections": sorted(rows, key=lambda row: row.get("name") or ""),
                "anomalies": anomalies,
            }
            self.save_data(self.DATA_PLAN, plan)
            self._set_job(running=False, phase="done", progress=100, message="预演计划生成完成", error="")
        except BaseException as err:
            error_trace = traceback.format_exc()
            logger.error(
                f"Emby TMDB 合集预演失败：run_id={self._run_id}, "
                f"error={err.__class__.__name__}: {err}\n{error_trace}"
            )
            self._set_job(
                running=False,
                phase="failed",
                message="预演失败",
                error=f"{err.__class__.__name__}: {err}",
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
                        return data, ""
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
        """在后台任务阶段边界检查插件停止请求。"""
        if self._stop_event.is_set():
            raise RuntimeError("任务因插件停止或重载而中断")

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
        """读取一个 Emby 合集的电影成员。"""
        if not boxset_id:
            return []
        url = (f"[HOST]emby/Users/[USER]/Items?ParentId={boxset_id}&Recursive=true&"
               "IncludeItemTypes=Movie&Fields=ProviderIds,ProductionYear&Limit=100000&api_key=[APIKEY]")
        response = service.instance.get_data(url=url)
        return response.json().get("Items") or [] if response and response.status_code == 200 else []

    def _collection_metadata(self, collection_id: int, seed: dict) -> Dict[str, Any]:
        """按 zh-CN、zh-SG、英文顺序解析合集名称、封面和徽标。"""
        try:
            translations = self._tmdb_zh_cn.collection.translations(collection_id) or []
        except Exception:
            translations = []
        translation_map = {
            (str(item.get("iso_639_1") or ""), str(item.get("iso_3166_1") or "")): item.get("data") or {}
            for item in translations if isinstance(item, dict)
        }
        name = next((
            translation_map.get(key, {}).get("title")
            for key in (("zh", "CN"), ("zh", "SG"), ("en", "US"), ("en", "GB"))
            if translation_map.get(key, {}).get("title")
        ), seed.get("name") or str(collection_id))
        try:
            images = self._tmdb_zh_cn.collection.images(collection_id) or {}
        except Exception:
            images = {}
        poster_path, poster_language = self._pick_tmdb_image(images.get("posters") or [])
        logo_path, logo_language = self._pick_tmdb_image(images.get("logos") or [])
        poster = settings.TMDB_IMAGE_URL(poster_path) if poster_path else None
        logo = settings.TMDB_IMAGE_URL(logo_path) if logo_path else None
        fanart = self._fanart_collection_images(collection_id)
        if not poster:
            poster, poster_language = self._pick_fanart_image(fanart.get("movieposter") or [])
        if not logo:
            logo, logo_language = self._pick_fanart_image(
                (fanart.get("hdmovielogo") or []) + (fanart.get("movielogo") or [])
            )
        return {
            "name": name,
            "poster": poster,
            "poster_language": poster_language,
            "logo": logo,
            "logo_language": logo_language,
        }

    @staticmethod
    def _pick_tmdb_image(images: List[dict]) -> Tuple[Optional[str], Optional[str]]:
        """按 zh-CN、zh-SG、英文、无语言及评分顺序选择 TMDB 图片。"""
        def priority(image: dict) -> int:
            """返回 TMDB 图片语言与地区优先级。"""
            language = image.get("iso_639_1")
            country = image.get("iso_3166_1")
            if language == "zh" and country == "CN":
                return 0
            if language == "zh" and country == "SG":
                return 1
            if language == "zh":
                return 2
            if language == "en":
                return 3
            if not language:
                return 4
            return 9

        candidates = sorted(images, key=lambda image: (
            priority(image),
            -(image.get("vote_average") or 0),
            -(image.get("vote_count") or 0),
            -(image.get("width") or 0),
        ))
        if not candidates:
            return None, None
        chosen = candidates[0]
        language = chosen.get("iso_639_1") or "null"
        country = chosen.get("iso_3166_1")
        label = f"{language}-{country}" if country else language
        return chosen.get("file_path"), label

    @staticmethod
    def _fanart_collection_images(collection_id: int) -> Dict[str, Any]:
        """按 TMDB 合集 ID 读取 Fanart.tv 合集图片。"""
        if not settings.FANART_ENABLE or not settings.FANART_API_KEY:
            return {}
        url = f"https://webservice.fanart.tv/v3/movies/{collection_id}"
        response = RequestUtils(proxies=settings.PROXY, timeout=20).get_res(
            url, params={"api_key": settings.FANART_API_KEY}
        )
        if not response or response.status_code != 200:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _pick_fanart_image(images: List[dict]) -> Tuple[Optional[str], Optional[str]]:
        """按中文、英文、无语言和点赞数选择 Fanart.tv 图片。"""
        language_order = {"zh": 0, "cn": 0, "en": 1, "00": 2, "": 2, None: 2}
        candidates = sorted(images, key=lambda image: (
            language_order.get(str(image.get("lang") or "").lower(), 9),
            -int(image.get("likes") or 0),
        ))
        if not candidates:
            return None, None
        chosen = candidates[0]
        language = str(chosen.get("lang") or "null")
        return chosen.get("url"), f"Fanart {language}"

    def _apply_worker(self, service: Any, selected: List[str], adopted: List[str]) -> None:
        """后台执行用户审核通过的合集变更，并逐项持久化恢复状态。"""
        try:
            plan = self.get_data(self.DATA_PLAN) or {}
            managed = self.get_data(self.DATA_STATE) or {}
            rows = [row for row in plan.get("collections") or [] if str(row.get("key")) in selected]
            missing_adoptions = [
                str(row.get("name") or row.get("key"))
                for row in rows
                if row.get("requires_adoption") and str(row.get("key")) not in adopted
            ]
            if missing_adoptions:
                preview = "、".join(missing_adoptions[:8])
                suffix = f" 等 {len(missing_adoptions)} 个" if len(missing_adoptions) > 8 else ""
                raise RuntimeError(f"以下同名合集尚未确认接管：{preview}{suffix}")

            existing_boxsets = self._load_boxsets(service)
            boxsets_by_name = {str(item.get("Name") or "").casefold(): item for item in existing_boxsets}
            total = max(len(rows), 1)
            errors: List[str] = []
            success_count = 0
            for index, row in enumerate(rows, start=1):
                self._check_stopped()
                key = str(row.get("key"))
                state_key = f"{plan.get('server')}:{key}"
                try:
                    emby_id = str(row.get("emby_id") or "")
                    if not emby_id and row.get("requires_adoption"):
                        emby_id = str(row.get("candidate_emby_id") or "")
                    if not emby_id:
                        existing = boxsets_by_name.get(str(row.get("name") or "").casefold())
                        if existing:
                            emby_id = str(existing.get("Id") or "")
                    if not emby_id:
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
                        self._change_members(service, emby_id, row.get("add") or [], row.get("remove") or [])
                    if row.get("poster"):
                        self._upload_image(service, emby_id, "Primary", row["poster"])
                    if self._sync_logo and row.get("logo"):
                        self._upload_image(service, emby_id, "Logo", row["logo"])
                    remaining = self._load_boxset_members(service, emby_id)
                    if not remaining and self._delete_empty:
                        self._delete_boxset(service, emby_id)
                        managed.pop(state_key, None)
                    else:
                        managed[state_key] = {
                            "tmdb_id": int(key),
                            "emby_id": emby_id,
                            "name": row.get("name"),
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        }
                    self.save_data(self.DATA_STATE, managed)
                    success_count += 1
                except Exception as err:
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
            self._set_job(
                running=False,
                phase="failed",
                message="执行失败",
                error=f"{err.__class__.__name__}: {err}",
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
