"""Emby TMDB 合集整理的无宿主核心逻辑测试。"""

from __future__ import annotations

import importlib.util
import importlib.machinery
import sys
import threading
import time
import types
from enum import Enum
from pathlib import Path

import pytest


def _install_stubs() -> None:
    """安装导入插件所需的最小 MoviePilot 桩，不发起网络请求。"""
    try:
        app_spec = importlib.util.find_spec("app")
    except (ImportError, ValueError):
        app_spec = None
    if app_spec is not None:
        return
    app = types.ModuleType("app")
    app.__path__ = []
    app.__spec__ = importlib.machinery.ModuleSpec("app", loader=None, is_package=True)
    core = types.ModuleType("app.core")
    core.__path__ = []
    cache = types.ModuleType("app.core.cache")
    config = types.ModuleType("app.core.config")
    context = types.ModuleType("app.core.context")
    event = types.ModuleType("app.core.event")
    mediaserver = types.ModuleType("app.helper.mediaserver")
    helper = types.ModuleType("app.helper")
    helper.__path__ = []
    log = types.ModuleType("app.log")
    modules = types.ModuleType("app.modules")
    themoviedb = types.ModuleType("app.modules.themoviedb")
    douban = types.ModuleType("app.modules.douban")
    utils = types.ModuleType("app.utils")
    utils.__path__ = []
    http = types.ModuleType("app.utils.http")
    string_utils = types.ModuleType("app.utils.string")
    zhconv = types.ModuleType("app.utils.zhconv")
    plugins = types.ModuleType("app.plugins")
    schemas = types.ModuleType("app.schemas")
    schemas.__path__ = []
    schema_types = types.ModuleType("app.schemas.types")
    fastapi = types.ModuleType("fastapi")
    fastapi.__path__ = []
    fastapi_concurrency = types.ModuleType("fastapi.concurrency")
    apscheduler = types.ModuleType("apscheduler")
    apscheduler_triggers = types.ModuleType("apscheduler.triggers")
    apscheduler_cron = types.ModuleType("apscheduler.triggers.cron")
    apscheduler_schedulers = types.ModuleType("apscheduler.schedulers")
    apscheduler_background = types.ModuleType("apscheduler.schedulers.background")

    class Settings:
        TMDB_API_DOMAIN = "api.themoviedb.org"
        TMDB_API_KEY = ""
        NORMAL_USER_AGENT = "pytest"
        PROXY = None

        @staticmethod
        def TMDB_IMAGE_URL(path, size="original"):
            return f"https://image.tmdb.test/{size}{path}"

    class Logger:
        def info(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

    class TmdbApi:
        def __init__(self, *args, **kwargs):
            self.collection = types.SimpleNamespace(translations=lambda *_: [], images=lambda *_: {})

    class RequestUtils:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            return None

        def get_res(self, *args, **kwargs):
            return None

    class Cache:
        def __init__(self, *args, **kwargs):
            self.values = {}

        def clear(self, *args, **kwargs):
            self.values.clear()

        def exists(self, *args, **kwargs):
            return False

    class Event:
        pass

    class EventManager:
        @staticmethod
        def register(*args, **kwargs):
            return lambda func: func

    class CronTrigger:
        @classmethod
        def from_crontab(cls, value):
            return value

    class BackgroundScheduler:
        running = False

        def __init__(self, *args, **kwargs):
            self.jobs = []

        def add_job(self, *args, **kwargs):
            self.jobs.append((args, kwargs))

        def start(self):
            self.running = True

        def shutdown(self, wait=False):
            self.running = False

    class StringUtils:
        @staticmethod
        def is_chinese(value):
            return True

    class DoubanApi:
        pass

    class MediaType(Enum):
        MOVIE = "电影"
        TV = "电视剧"

    class MediaInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def get_res(self, *args, **kwargs):
            return None

        def delete_res(self, *args, **kwargs):
            return None

    class MediaServerHelper:
        def get_services(self, *args, **kwargs):
            return {}

    class Response:
        def __init__(self, success=True, message="", data=None, **kwargs):
            self.success, self.message, self.data = success, message, data

    class PluginBase:
        def __init__(self, *args, **kwargs):
            self._store = {}

        def get_data(self, key):
            return getattr(self, "_store", {}).get(key)

        def save_data(self, key, value):
            self._store[key] = value

        def update_config(self, value):
            self._config = value

    config.settings = Settings()
    log.logger = Logger()
    themoviedb.TmdbApi = TmdbApi
    http.RequestUtils = RequestUtils
    mediaserver.MediaServerHelper = MediaServerHelper
    plugins._PluginBase = PluginBase
    schemas.Response = Response
    schemas.WebhookEventInfo = type("WebhookEventInfo", (), {})
    schemas.ServiceInfo = type("ServiceInfo", (), {})
    context.MediaInfo = MediaInfo
    schema_types.MediaType = MediaType
    schema_types.EventType = type("EventType", (), {"WebhookMessage": "WebhookMessage"})
    fastapi.Body = lambda default=None, **kwargs: default
    fastapi.Query = lambda default=None, **kwargs: default
    async def run_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)
    fastapi_concurrency.run_in_threadpool = run_in_threadpool
    cache.Cache = Cache
    event.eventmanager = EventManager()
    event.Event = Event
    douban.DoubanApi = DoubanApi
    string_utils.StringUtils = StringUtils
    zhconv.convert = lambda value, target: value
    apscheduler_cron.CronTrigger = CronTrigger
    apscheduler_background.BackgroundScheduler = BackgroundScheduler
    helper.mediaserver = mediaserver
    modules.themoviedb, modules.douban = themoviedb, douban
    utils.http = http
    utils.string, utils.zhconv = string_utils, zhconv
    core.config, core.context, core.cache, core.event = config, context, cache, event
    schemas.types = schema_types
    app.core, app.helper, app.log, app.modules, app.plugins, app.schemas = core, helper, log, modules, plugins, schemas
    modules_to_install = {
        "app": app,
        "app.core": core,
        "app.core.config": config,
        "app.core.context": context,
        "app.core.cache": cache,
        "app.core.event": event,
        "app.helper": helper,
        "app.helper.mediaserver": mediaserver,
        "app.log": log,
        "app.modules": modules,
        "app.modules.themoviedb": themoviedb,
        "app.modules.douban": douban,
        "app.plugins": plugins,
        "app.schemas": schemas,
        "app.schemas.types": schema_types,
        "app.utils": utils,
        "app.utils.http": http,
        "app.utils.string": string_utils,
        "app.utils.zhconv": zhconv,
        "fastapi": fastapi,
        "fastapi.concurrency": fastapi_concurrency,
        "apscheduler": apscheduler,
        "apscheduler.triggers": apscheduler_triggers,
        "apscheduler.triggers.cron": apscheduler_cron,
        "apscheduler.schedulers": apscheduler_schedulers,
        "apscheduler.schedulers.background": apscheduler_background,
    }
    for name, module in modules_to_install.items():
        sys.modules.setdefault(name, module)
    existing_app = sys.modules.get("app")
    if existing_app is not None and getattr(existing_app, "__spec__", None) is None:
        existing_app.__spec__ = importlib.machinery.ModuleSpec("app", loader=None, is_package=True)
    # 另一个插件测试可能已安装较小的 app 桩；只补齐本测试依赖，不覆盖已有设置。
    existing_settings = getattr(sys.modules.get("app.core.config"), "settings", None)
    if existing_settings is None:
        sys.modules["app.core.config"].settings = Settings()
    else:
        for key in ("TMDB_API_DOMAIN", "TMDB_API_KEY", "NORMAL_USER_AGENT", "PROXY"):
            if not hasattr(existing_settings, key):
                setattr(existing_settings, key, getattr(Settings, key))
        if not hasattr(existing_settings, "TMDB_IMAGE_URL"):
            existing_settings.TMDB_IMAGE_URL = Settings.TMDB_IMAGE_URL
    existing_logger = getattr(sys.modules.get("app.log"), "logger", None)
    if existing_logger is not None and not hasattr(existing_logger, "error"):
        existing_logger.error = lambda *args, **kwargs: None
    elif existing_logger is None:
        sys.modules["app.log"].logger = Logger()
    existing_logger = getattr(sys.modules.get("app.log"), "logger", None)
    if existing_logger is not None and not hasattr(existing_logger, "warning"):
        existing_logger.warning = lambda *args, **kwargs: None
    if not hasattr(sys.modules["app.schemas"], "Response"):
        sys.modules["app.schemas"].Response = Response
    # 其他插件测试的 TMDB 桩可能不接受 language 参数；统一成可构造的空实现。
    tmdb_module = sys.modules.get("app.modules.themoviedb")
    tmdb_class = getattr(tmdb_module, "TmdbApi", None)
    if tmdb_class is not None:
        try:
            tmdb_class(language="zh-CN")
        except TypeError:
            class CompatibleTmdbApi(tmdb_class):
                def __init__(self, *args, **kwargs):
                    self.collection = types.SimpleNamespace(translations=lambda *_: [], images=lambda *_: {})
            tmdb_module.TmdbApi = CompatibleTmdbApi


_install_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins.v2"))

from embytmdbcollectionsync import EmbyTmdbCollectionSync  # noqa: E402


def _plugin():
    plugin = EmbyTmdbCollectionSync.__new__(EmbyTmdbCollectionSync)
    plugin._lock = threading.RLock()
    plugin._stop_event = threading.Event()
    plugin._worker = None
    plugin._server = "emby"
    plugin._libraries = ["movies"]
    plugin._enabled = True
    plugin._show_sidebar_nav = True
    plugin._overwrite_images = True
    plugin._delete_empty = True
    plugin._sync_logo = True
    plugin._store = {}
    plugin.get_data = lambda key: plugin._store.get(key)
    plugin.save_data = lambda key, value: plugin._store.__setitem__(key, value)
    plugin.update_config = lambda value: plugin._store.__setitem__("config", value)
    return plugin


def test_tmdb_image_language_supports_composite_and_split_regions():
    images = [
        {"file_path": "/generic.jpg", "iso_639_1": "zh"},
        {"file_path": "/sg.jpg", "iso_639_1": "zh", "iso_3166_1": "SG"},
        {"file_path": "/tw.jpg", "iso_639_1": "zh-TW"},
        {"file_path": "/cn.jpg", "iso_639_1": "zh", "iso_3166_1": "CN"},
    ]
    assert EmbyTmdbCollectionSync._normalize_tmdb_image_language(images[1]) == "zh-SG"
    assert EmbyTmdbCollectionSync._normalize_tmdb_image_language(images[2]) == "zh-TW"
    assert EmbyTmdbCollectionSync._pick_tmdb_image(images) == ("/cn.jpg", "zh-CN")


def test_member_snapshot_is_order_independent_and_detects_changes():
    first = [{"Id": "2"}, {"Id": "1"}]
    second = [{"Id": "1"}, {"Id": "2"}]
    _, hash_one = EmbyTmdbCollectionSync._member_snapshot(first)
    _, hash_two = EmbyTmdbCollectionSync._member_snapshot(second)
    _, hash_three = EmbyTmdbCollectionSync._member_snapshot(first + [{"Id": "3"}])
    assert hash_one == hash_two
    assert hash_one != hash_three


def test_managed_collection_member_or_name_change_is_detected_as_customization():
    _, baseline_hash = EmbyTmdbCollectionSync._member_snapshot([{"Id": "movie-1"}])
    _, changed_hash = EmbyTmdbCollectionSync._member_snapshot([{"Id": "movie-1"}, {"Id": "movie-2"}])
    state = {"member_hash": baseline_hash, "emby_name": "哥斯拉合集"}
    assert "成员" in EmbyTmdbCollectionSync._customization_reason(state, changed_hash, "哥斯拉合集")
    assert "名称" in EmbyTmdbCollectionSync._customization_reason(state, baseline_hash, "哥斯拉全系列")
    assert EmbyTmdbCollectionSync._customization_reason(state, baseline_hash, "哥斯拉合集") == ""


def test_legacy_cross_tmdb_members_are_evidence_of_manual_merge():
    official = {"godzilla-1": "10", "godzilla-2": "20", "unmatched": ""}
    assert EmbyTmdbCollectionSync._cross_collection_members(
        ["godzilla-1", "godzilla-2", "unmatched"], official, "10"
    ) == ["godzilla-2"]


def test_collection_movies_are_compact_deduplicated_and_sorted():
    movies = EmbyTmdbCollectionSync._normalize_collection_movies([
        {"id": 2, "title": "续集", "release_date": "2024-05-01", "poster_path": "/two.jpg"},
        {"id": 1, "title": "首部", "release_date": "2020-01-01"},
        {"id": 1, "title": "重复记录", "release_date": "2020-01-01"},
    ])
    assert [item["tmdb_id"] for item in movies] == ["1", "2"]
    assert movies[1]["year"] == "2024"
    assert movies[1]["poster"].endswith("/two.jpg")


def test_subscribe_endpoint_only_accepts_missing_movies_from_current_plan():
    plugin = _plugin()
    plugin._store[plugin.DATA_PLAN] = {
        "plan_id": "plan-1",
        "collections": [{"key": "10", "missing_movies": [{"tmdb_id": "100", "title": "缺片"}]}],
    }
    started = []
    plugin._start_worker = lambda *args: (started.append(args) or True, "started")
    response = plugin.api_start_subscribe({"plan_id": "plan-1", "tmdb_ids": ["100"]})
    assert response.success is True
    assert started and started[0][1] == "subscribe"
    rejected = plugin.api_start_subscribe({"plan_id": "plan-1", "tmdb_ids": ["999"]})
    assert rejected.success is False


def test_stale_plan_row_is_rejected_before_apply():
    plugin = _plugin()
    plugin._load_boxsets = lambda service: [{"Id": "box-1", "Name": "测试合集"}]
    plugin._load_boxset_members = lambda service, item_id: [{"Id": "movie-2"}]
    _, expected_hash = plugin._member_snapshot([{"Id": "movie-1"}])
    valid, errors = plugin._validate_plan_rows(
        object(),
        {"server": "emby"},
        [{"key": "1", "name": "测试合集", "emby_id": "box-1", "current_member_hash": expected_hash}],
    )
    assert valid == []
    assert "成员已变化" in errors[0]


def test_job_reports_busy_while_worker_is_alive():
    plugin = _plugin()
    worker = threading.Thread(target=lambda: time.sleep(0.2))
    plugin._worker = worker
    worker.start()
    try:
        assert plugin._job()["busy"] is True
    finally:
        worker.join()
    assert plugin._job()["busy"] is False


def test_plan_id_mismatch_is_rejected_before_starting_apply():
    plugin = _plugin()
    plugin._store[plugin.DATA_PLAN] = {"plan_id": "current", "server": "emby", "collections": [{"key": "1"}]}
    plugin._selected_service = lambda: ("emby", object())
    response = plugin.api_start_apply({"plan_id": "old", "selected": ["1"]})
    assert response.success is False
    assert "计划" in response.message


def test_member_api_failure_is_not_treated_as_empty_members():
    class Response:
        status_code = 503

    class Instance:
        def get_data(self, **kwargs):
            return Response()

    service = types.SimpleNamespace(instance=Instance())
    with pytest.raises(RuntimeError, match="HTTP 503"):
        EmbyTmdbCollectionSync._load_boxset_members(service, "box-1")


def test_apply_does_not_delete_when_remaining_member_read_fails():
    class Response:
        def __init__(self, status_code, data):
            self.status_code = status_code
            self._data = data

        def json(self):
            return self._data

    class Instance:
        def get_data(self, url):
            if "ParentId=" in url:
                return Response(503, {})
            return Response(200, {"Items": []})

    plugin = _plugin()
    _, empty_hash = plugin._member_snapshot([])
    plugin._store[plugin.DATA_PLAN] = {
        "plan_id": "plan-1",
        "server": "emby",
        "config_fingerprint": plugin._config_fingerprint(),
        "collections": [{"key": "1", "name": "新合集", "create": True, "desired_movies": [{"id": "m1"}], "current_member_hash": empty_hash}],
    }
    plugin._store[plugin.DATA_STATE] = {}
    plugin._create_boxset = lambda service, name, movie_ids: "box-created"
    deleted = []
    plugin._delete_boxset = lambda service, item_id: deleted.append(item_id)
    plugin._apply_worker(types.SimpleNamespace(instance=Instance()), ["1"], [], "plan-1")
    assert deleted == []
    assert "HTTP 503" in plugin._store[plugin.DATA_JOB]["error"]


def test_each_row_is_rechecked_and_changed_row_is_skipped_before_write():
    plugin = _plugin()
    _, empty_hash = plugin._member_snapshot([])
    row = {"key": "1", "name": "变化合集", "create": True, "desired_movies": [{"id": "m1"}], "current_member_hash": empty_hash}
    plugin._store[plugin.DATA_PLAN] = {
        "plan_id": "plan-2", "server": "emby", "config_fingerprint": plugin._config_fingerprint(), "collections": [row]
    }
    plugin._store[plugin.DATA_STATE] = {}
    plugin._load_boxsets = lambda service: []
    plugin._validate_plan_rows = lambda service, plan, rows: (rows, [])
    plugin._validate_plan_row = lambda service, value: (None, "变化合集：成员已变化，已跳过")
    writes = []
    plugin._create_boxset = lambda *args: writes.append(args)
    plugin._apply_worker(object(), ["1"], [], "plan-2")
    assert writes == []
    assert "成员已变化" in plugin._store[plugin.DATA_JOB]["error"]


def test_save_config_rejects_busy_worker_without_mutating_instance():
    plugin = _plugin()
    plugin._server = "before"
    worker = threading.Thread(target=lambda: time.sleep(0.2))
    plugin._worker = worker
    worker.start()
    try:
        response = plugin.api_save_config({"server": "after", "libraries": ["new"]})
        assert response.success is False
        assert plugin._server == "before"
    finally:
        worker.join()


def test_stop_service_releases_lock_while_join_waits():
    class ControlledWorker:
        def __init__(self):
            self.join_started = threading.Event()
            self.release = threading.Event()
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            self.join_started.set()
            self.release.wait(timeout=1)
            self.alive = False

    plugin = _plugin()
    worker = ControlledWorker()
    plugin._worker = worker
    stopper = threading.Thread(target=plugin.stop_service)
    stopper.start()
    assert worker.join_started.wait(1)
    acquired = plugin._lock.acquire(timeout=0.2)
    if acquired:
        plugin._lock.release()
    worker.release.set()
    stopper.join(timeout=2)
    assert acquired is True


def test_run_id_fencing_rejects_old_worker():
    plugin = _plugin()
    plugin._run_id = "run-current"
    plugin._store[plugin.DATA_JOB] = {"run_id": "run-old"}
    with pytest.raises(RuntimeError, match="run_id"):
        plugin._check_stopped()


def test_locale_parser_normalizes_case_and_separators():
    assert EmbyTmdbCollectionSync._normalize_tmdb_image_language({"iso_639_1": "ZH_cn"}) == "zh-CN"
    assert EmbyTmdbCollectionSync._normalize_tmdb_image_language({"iso_639_1": "zh+CN"}) == "zh-CN"
    assert EmbyTmdbCollectionSync._normalize_translation_locale({"iso_639_1": "ZH_cn"}) == ("zh", "CN")


def test_collection_images_explicitly_requests_all_chinese_regions(monkeypatch):
    """合集图片请求必须显式包含所有地区候选，不能依赖客户端默认语言。"""
    calls = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"posters": []}

    class Request:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def get_res(self, url, params):
            calls["url"] = url
            calls["params"] = params
            return Response()

    module = sys.modules[EmbyTmdbCollectionSync.__module__]
    monkeypatch.setattr(module, "RequestUtils", Request)
    assert EmbyTmdbCollectionSync._query_collection_images(550) == {"posters": []}
    assert calls["params"]["include_image_language"] == EmbyTmdbCollectionSync.COLLECTION_IMAGE_LANGUAGES
    assert calls["params"]["language"] == "zh-CN"


def test_script_tag_does_not_override_explicit_image_region():
    """zh-Hans/zh-Hant 是脚本标签，独立地区字段应保持有效。"""
    normalize = EmbyTmdbCollectionSync._normalize_tmdb_image_language
    assert normalize({"iso_639_1": "zh-Hans", "iso_3166_1": "CN"}) == "zh-CN"
    assert normalize({"iso_639_1": "zh-Hant", "iso_3166_1": "TW"}) == "zh-TW"
    assert normalize({"iso_639_1": "zh-Hant"}) == "zh"
