"""Emby 中文角色同步的安全边界测试。"""

from __future__ import annotations

import importlib.util
import queue
import sys
import types
from enum import Enum
from pathlib import Path


_STUB_INSTALLED = False


def _install_moviepilot_stubs() -> None:
    """在没有 MoviePilot 宿主时安装最小可测试桩。"""
    global _STUB_INSTALLED
    if importlib.util.find_spec("app") is not None:
        return
    _STUB_INSTALLED = True

    app = types.ModuleType("app")
    app.__path__ = []
    core = types.ModuleType("app.core")
    core.__path__ = []
    cache = types.ModuleType("app.core.cache")
    config = types.ModuleType("app.core.config")
    event = types.ModuleType("app.core.event")
    helper = types.ModuleType("app.helper")
    helper.__path__ = []
    mediaserver = types.ModuleType("app.helper.mediaserver")
    log = types.ModuleType("app.log")
    modules = types.ModuleType("app.modules")
    modules.__path__ = []
    douban = types.ModuleType("app.modules.douban")
    themoviedb = types.ModuleType("app.modules.themoviedb")
    plugins = types.ModuleType("app.plugins")
    schemas = types.ModuleType("app.schemas")
    schemas.__path__ = []
    schema_types = types.ModuleType("app.schemas.types")
    utils = types.ModuleType("app.utils")
    utils.__path__ = []
    string_utils = types.ModuleType("app.utils.string")
    zhconv = types.ModuleType("app.utils.zhconv")
    apscheduler = types.ModuleType("apscheduler")
    apscheduler_triggers = types.ModuleType("apscheduler.triggers")
    apscheduler_cron = types.ModuleType("apscheduler.triggers.cron")
    apscheduler_schedulers = types.ModuleType("apscheduler.schedulers")
    apscheduler_background = types.ModuleType("apscheduler.schedulers.background")

    class _Logger:
        """收集测试中的错误日志。"""

        errors = []

        def info(self, *args, **kwargs):
            """忽略普通日志。"""

        def warning(self, *args, **kwargs):
            """忽略警告日志。"""

        def error(self, message, *args, **kwargs):
            """记录错误日志。"""
            self.errors.append(str(message))

    class _Cache:
        """提供无副作用的缓存桩。"""

        def __init__(self, *args, **kwargs):
            self.values = {}

        def clear(self, *args, **kwargs):
            """清空桩缓存。"""
            self.values.clear()

        def exists(self, *args, **kwargs):
            """返回桩缓存是否存在。"""
            return False

    class _Settings:
        """提供时区配置。"""

        TZ = "UTC"

    class _EventManager:
        """提供事件装饰器桩。"""

        @staticmethod
        def register(*args, **kwargs):
            """原样返回被装饰函数。"""
            return lambda func: func

    class _PluginBase:
        """提供插件测试所需的持久化桩。"""

        def update_config(self, config):
            """记录最后一次配置。"""
            self.last_config = config

        def get_data(self, key):
            """读取内存数据。"""
            return getattr(self, "_data", {}).get(key)

        def save_data(self, key, value):
            """保存内存数据。"""
            if not hasattr(self, "_data"):
                self._data = {}
            self._data[key] = value

    class _MediaType(Enum):
        """模拟 MoviePilot 媒体类型。"""

        MOVIE = "电影"
        TV = "电视剧"

    class _StringUtils:
        """提供中文检测桩。"""

        @staticmethod
        def is_chinese(value):
            """测试环境默认视为中文。"""
            return True

    class _ServiceInfo:
        """提供媒体服务类型标记。"""

    class _WebhookEventInfo:
        """提供 Webhook 类型标记。"""

    class _EventType(Enum):
        """模拟事件类型。"""

        WebhookMessage = "WebhookMessage"

    class _DoubanApi:
        """避免测试发出豆瓣请求。"""

    class _TmdbApi:
        """避免测试发出 TMDB 请求。"""

    class _MediaServerHelper:
        """避免测试访问实际媒体服务器。"""

        def get_services(self, *args, **kwargs):
            """返回空服务列表。"""
            return {}

        def get_configs(self):
            """返回空配置列表。"""
            return {}

    class _CronTrigger:
        """提供 CronTrigger 测试桩。"""

        @classmethod
        def from_crontab(cls, value):
            """返回可存储的 Cron 标记。"""
            return value

    class _BackgroundScheduler:
        """提供后台调度器测试桩。"""

        running = False

        def __init__(self, *args, **kwargs):
            self.jobs = []

        def add_job(self, *args, **kwargs):
            """记录调度任务。"""
            self.jobs.append((args, kwargs))

        def start(self):
            """标记调度器运行。"""
            self.running = True

        def shutdown(self, wait=False):
            """停止调度器。"""
            self.running = False

    config.settings = _Settings()
    cache.Cache = _Cache
    event.eventmanager = _EventManager()
    event.Event = object
    log.logger = _Logger()
    plugins._PluginBase = _PluginBase
    schema_types.MediaType = _MediaType
    schema_types.EventType = _EventType
    schemas.ServiceInfo = _ServiceInfo
    schemas.WebhookEventInfo = _WebhookEventInfo
    string_utils.StringUtils = _StringUtils
    zhconv.convert = lambda value, target: value
    douban.DoubanApi = _DoubanApi
    themoviedb.TmdbApi = _TmdbApi
    mediaserver.MediaServerHelper = _MediaServerHelper
    apscheduler_cron.CronTrigger = _CronTrigger
    apscheduler_background.BackgroundScheduler = _BackgroundScheduler

    core.cache = cache
    core.config = config
    core.event = event
    helper.mediaserver = mediaserver
    modules.douban = douban
    modules.themoviedb = themoviedb
    app.core = core
    app.helper = helper
    app.log = log
    app.modules = modules
    app.plugins = plugins
    app.schemas = schemas
    app.utils = utils
    utils.string = string_utils
    utils.zhconv = zhconv

    for name, module in {
        "app": app,
        "app.core": core,
        "app.core.cache": cache,
        "app.core.config": config,
        "app.core.event": event,
        "app.helper": helper,
        "app.helper.mediaserver": mediaserver,
        "app.log": log,
        "app.modules": modules,
        "app.modules.douban": douban,
        "app.modules.themoviedb": themoviedb,
        "app.plugins": plugins,
        "app.schemas": schemas,
        "app.schemas.types": schema_types,
        "app.utils": utils,
        "app.utils.string": string_utils,
        "app.utils.zhconv": zhconv,
        "apscheduler": apscheduler,
        "apscheduler.triggers": apscheduler_triggers,
        "apscheduler.triggers.cron": apscheduler_cron,
        "apscheduler.schedulers": apscheduler_schedulers,
        "apscheduler.schedulers.background": apscheduler_background,
    }.items():
        sys.modules.setdefault(name, module)


_install_moviepilot_stubs()

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins.v2" / "embychineserolesync"
sys.path.insert(0, str(PLUGIN_DIR.parent))

from app.schemas.types import MediaType  # noqa: E402
from embychineserolesync import EmbyChineseRoleSync, SyncResult  # noqa: E402

if _STUB_INSTALLED:
    # 插件已绑定桩模块，移除 sys.modules 引用避免污染同一 pytest 进程中的其它插件测试。
    for _name in list(sys.modules):
        if _name == "app" or _name.startswith("app.") or _name == "apscheduler" or _name.startswith("apscheduler."):
            sys.modules.pop(_name, None)


class _Response:
    """提供最小 HTTP 响应对象。"""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload
        self.text = ""

    def json(self):
        """返回测试负载。"""
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _plugin() -> EmbyChineseRoleSync:
    """构造不启动服务的插件实例。"""
    plugin = EmbyChineseRoleSync.__new__(EmbyChineseRoleSync)
    plugin._enabled = True
    plugin._include_libraries = []
    plugin._state_lock = __import__("threading").RLock()
    plugin._inflight = set()
    plugin._managed_lock_fields = {}
    plugin._lock_person_name = False
    plugin._lock_media_cast = False
    return plugin


class _ApiPlugin(EmbyChineseRoleSync):
    """允许测试注入媒体服务字典的插件子类。"""

    @property
    def service_infos(self):
        """返回测试注入的服务。"""
        return self._services


def test_handle_media_returns_failure_and_logs_traceback():
    """单项异常应返回失败结果，且 LoggerManager 不调用 exception。"""
    plugin = _plugin()
    plugin._handle_media_impl = lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))
    service = types.SimpleNamespace(name="demo")
    result = plugin._handle_media(service, {"Id": "1", "Type": "Movie", "Name": "测试"})
    assert isinstance(result, SyncResult)
    assert not result.success
    assert "boom" in result.message


def test_library_whitelist_fails_closed_on_ancestor_error():
    """白名单 Ancestors 查询失败时必须拒绝处理。"""
    plugin = _plugin()
    plugin._include_libraries = ["电影"]
    service = types.SimpleNamespace(instance=types.SimpleNamespace(get_data=lambda **_: _Response(500)))
    assert plugin._is_library_allowed(service, {"Id": "1", "Name": "测试"}) is False


def test_api_reports_structured_failure():
    """手动 API 应把结构化失败结果返回给调用方。"""
    plugin = _ApiPlugin.__new__(_ApiPlugin)
    plugin._enabled = True
    plugin._include_libraries = []
    plugin._services = {}
    service = types.SimpleNamespace(instance=types.SimpleNamespace())
    plugin._services = {"demo": service}
    plugin._get_item_info = lambda *_: {"Id": "1", "Type": "Movie", "Name": "测试"}
    plugin._is_library_allowed = lambda *_: True
    plugin._handle_single_item = lambda *_: SyncResult.failed("1", "豆瓣匹配失败")
    result = plugin.api_sync_media("1", "demo")
    assert result["success"] is False
    assert "豆瓣匹配失败" in result["message"]


def test_preview_does_not_write_to_emby():
    """预演只读识别与匹配，不得调用 Emby post_data。"""
    writes = []
    plugin = _ApiPlugin.__new__(_ApiPlugin)
    plugin._include_libraries = []
    plugin._lock_person_name = False
    plugin._lock_media_cast = False
    service = types.SimpleNamespace(instance=types.SimpleNamespace(post_data=lambda **_: writes.append(True)))
    plugin._services = {"demo": service}
    plugin._get_item_info = lambda *_: {"Id": "1", "Type": "Movie", "Name": "测试", "People": [{"Id": "p1", "Name": "Old", "Role": ""}]}
    plugin._is_library_allowed = lambda *_: True
    plugin._get_douban_info = lambda *_: {"actors": [{"id": "d1", "name": "New", "character": "角色"}]}
    plugin._build_chinese_role_plan = lambda *_: {
        "existing_people_count": 1,
        "douban_actor_count": 1,
        "actions": [{"name_changed": True, "role_changed": True, "current_name": "Old", "target_name": "New", "current_role": "", "target_role": "角色"}],
        "unmatched": [],
        "ambiguous": [],
        "conflicts": [],
        "safe_to_apply": True,
    }
    result = plugin.api_preview_media({"item_id": "1", "server": "demo"})
    assert result["success"] is True
    assert result["data"]["dry_run"] is True
    assert result["data"]["would_change_count"] == 1
    assert writes == []


def test_managed_lock_preserves_other_fields_and_unknown_locks():
    """锁字段仅增删目标，保留其它锁并不猜测性解锁。"""
    plugin = _plugin()
    item = {"Id": "1", "LockedFields": ["Other"]}
    plugin._set_managed_lock(item, "Cast", True)
    assert item["LockedFields"] == ["Other", "Cast"]
    plugin._set_managed_lock(item, "Cast", False)
    assert item["LockedFields"] == ["Other"]

    existing = {"Id": "2", "LockedFields": ["Cast", "Other"]}
    plugin._set_managed_lock(existing, "Cast", False)
    assert existing["LockedFields"] == ["Cast", "Other"]

    missing = {"Id": "3", "Name": "未知锁字段"}
    plugin._set_managed_lock(missing, "Cast", True, "demo")
    assert "LockedFields" not in missing


def test_managed_lock_ownership_is_scoped_to_server():
    """相同 Item ID 在不同 Emby 服务上不得共享解锁归属。"""
    plugin = _plugin()
    first = {"Id": "same", "LockedFields": []}
    second = {"Id": "same", "LockedFields": ["Cast"]}
    plugin._set_managed_lock(first, "Cast", True, "server-a")
    plugin._set_managed_lock(second, "Cast", False, "server-b")
    assert second["LockedFields"] == ["Cast"]


def test_disabled_write_apis_fail_but_preview_is_allowed():
    """停用插件拒绝所有同步写 API，但不阻止只读预演入口。"""
    plugin = _ApiPlugin.__new__(_ApiPlugin)
    plugin._enabled = False
    plugin._include_libraries = []
    plugin._services = {}
    assert plugin.api_sync_media("1", "demo")["success"] is False
    assert plugin.api_sync_media_batch({"items": []})["success"] is False
    assert plugin.api_sync_media_by_name("测试")["success"] is False
    assert plugin.api_run_now()["success"] is False


def test_preview_movie_and_series_paths_are_write_free():
    """电影和电视剧预演均不得触发 Emby 更新、刷新或插件数据写入。"""
    writes = []
    service = types.SimpleNamespace(
        name="demo",
        instance=types.SimpleNamespace(
            get_data=lambda **kwargs: _Response(200, {"Items": [{"Id": "season-1", "Name": "第一季"}]}),
            post_data=lambda **kwargs: (_ for _ in ()).throw(AssertionError("preview attempted write")),
        ),
    )
    plugin = _ApiPlugin.__new__(_ApiPlugin)
    plugin._enabled = False
    plugin._include_libraries = []
    plugin._lock_person_name = False
    plugin._lock_media_cast = False
    plugin._services = {"demo": service}
    plugin._is_library_allowed = lambda *_: True
    plugin._get_douban_info = lambda *_: {"actors": [{"id": "d1", "name": "New", "character": "角色"}]}
    plugin._build_chinese_role_plan = lambda *_: {
        "existing_people_count": 1,
        "douban_actor_count": 1,
        "actions": [{"name_changed": True, "role_changed": True, "current_name": "Old", "target_name": "New", "current_role": "", "target_role": "角色"}],
        "unmatched": [],
        "ambiguous": [],
        "conflicts": [],
        "safe_to_apply": True,
    }
    records = {
        "movie-1": {"Id": "movie-1", "Type": "Movie", "Name": "电影", "People": [{"Id": "p1"}]},
        "series-1": {"Id": "series-1", "Type": "Series", "Name": "剧集", "People": [{"Id": "p1"}]},
        "season-1": {"Id": "season-1", "Type": "Season", "Name": "第一季", "People": [{"Id": "p1"}]},
    }
    plugin._get_item_info = lambda _service, item_id: records.get(str(item_id))
    plugin._update_item_info = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("preview attempted update"))
    plugin._refresh_item_info = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("preview attempted refresh"))
    plugin.save_data = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("preview attempted save"))

    movie = plugin.api_preview_media({"item_id": "movie-1", "server": "demo"})
    series = plugin.api_preview_media({"item_id": "series-1", "server": "demo"})
    assert movie["success"] is True
    assert series["success"] is True
    assert movie["data"]["would_change_count"] == 1
    assert series["data"]["would_change_count"] == 1
    assert writes == []


def test_disabled_plugin_does_not_start_scheduler_or_worker():
    """停用配置不应启动 APScheduler 或队列线程。"""
    plugin = EmbyChineseRoleSync()
    plugin.init_plugin({"enabled": False})
    assert plugin._scheduler is None
    assert plugin._worker_thread is None


def test_hook_and_worker_cancel_tasks_during_stop():
    """停止阶段 Webhook 不再入队，worker 会取消残留任务并完成 task_done。"""
    plugin = _ApiPlugin.__new__(_ApiPlugin)
    plugin._enabled = True
    plugin._include_libraries = []
    plugin._services = {"demo": types.SimpleNamespace(name="demo")}
    plugin._queue = queue.Queue()
    plugin._queue_put_lock = __import__("threading").Lock()
    plugin._worker_stop_event = __import__("threading").Event()
    plugin._scan_stop_event = __import__("threading").Event()
    plugin._worker_stop_event.set()
    event = types.SimpleNamespace(
        event_data=types.SimpleNamespace(
            channel="emby",
            event="library.new",
            server_name="demo",
            json_object={"Item": {"Id": "1", "Type": "Movie", "Name": "测试"}},
        )
    )
    plugin._is_library_allowed = lambda *_: True
    plugin.hook(event)
    assert plugin._queue.empty()

    plugin._queue.put((plugin._services["demo"], {"Id": "2", "Type": "Movie"}))
    plugin._queue.put(None)
    plugin._handle_media = lambda *_: (_ for _ in ()).throw(AssertionError("stopping worker processed task"))
    plugin.handle_hook()
    assert plugin._queue.unfinished_tasks == 0


def test_runtime_registry_exposes_old_worker_to_reloaded_instance():
    """新实例必须发现旧实例仍存活的 worker，不能清除停止信号后并发启动。"""
    module = sys.modules[EmbyChineseRoleSync.__module__]
    runtime = module._EMBY_ROLE_RUNTIME

    class StuckWorker:
        def __init__(self):
            self.join_called = False

        @staticmethod
        def is_alive():
            return True

        def join(self, timeout=None):
            self.join_called = True

    worker = StuckWorker()
    previous_worker = runtime.get("worker_thread")
    previous_scheduler = runtime.get("scheduler")
    runtime["worker_thread"] = worker
    runtime["scheduler"] = None
    plugin = EmbyChineseRoleSync.__new__(EmbyChineseRoleSync)
    try:
        plugin._bind_runtime_state()
        assert plugin._worker_thread is worker
        plugin.stop_service()
        assert worker.join_called is True
        assert runtime["worker_thread"] is worker
        assert runtime["worker_stop_event"].is_set()
    finally:
        runtime["worker_thread"] = previous_worker
        runtime["scheduler"] = previous_scheduler
        runtime["worker_stop_event"].clear()
        runtime["scan_stop_event"].clear()
        while True:
            try:
                runtime["queue"].get_nowait()
            except queue.Empty:
                break
            else:
                runtime["queue"].task_done()
