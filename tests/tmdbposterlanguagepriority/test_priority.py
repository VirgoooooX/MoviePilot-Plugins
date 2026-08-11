"""TMDB/Fanart 海报优先插件的纯逻辑测试。"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import sys
import threading
import types
from collections import OrderedDict
from enum import Enum
from pathlib import Path


def _install_moviepilot_stubs():
    """临时增量补齐宿主桩，并返回导入后恢复全局模块的函数。"""
    try:
        importlib.util.find_spec("app")
    except (ImportError, ValueError):
        # 其它插件测试可能已放入没有 __spec__ 的 app 桩，继续增量补齐。
        pass

    try:
        from app.core.config import settings as _settings
        from app.core.context import MediaInfo as _media_info
        from app.log import logger as _logger
        from app.modules.themoviedb import TmdbApi as _tmdb_api
        from app.plugins import _PluginBase as _plugin_base
        from app.schemas.types import MediaType as _media_type
        from app.utils.http import RequestUtils as _request_utils
        from fastapi.concurrency import run_in_threadpool as _run_in_threadpool
    except (ImportError, AttributeError, ValueError):
        pass
    else:
        return lambda: None

    snapshots = {
        name: (module, dict(module.__dict__))
        for name, module in sys.modules.items()
        if name == "app"
        or name.startswith("app.")
        or name == "fastapi"
        or name.startswith("fastapi.")
    }

    def ensure_module(name: str, package: bool = False):
        """获取模块桩并补齐包属性。"""
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            sys.modules[name] = module
        if package and not hasattr(module, "__path__"):
            module.__path__ = []
        if not hasattr(module, "__spec__"):
            module.__spec__ = importlib.machinery.ModuleSpec(
                name, loader=None, is_package=package
            )
        return module

    app = ensure_module("app", package=True)
    core = ensure_module("app.core", package=True)
    config = ensure_module("app.core.config")
    context = ensure_module("app.core.context")
    log = ensure_module("app.log")
    modules = ensure_module("app.modules", package=True)
    themoviedb = ensure_module("app.modules.themoviedb")
    utils = ensure_module("app.utils", package=True)
    http = ensure_module("app.utils.http")
    plugins = ensure_module("app.plugins")
    schemas = ensure_module("app.schemas", package=True)
    schema_types = ensure_module("app.schemas.types")
    fastapi = ensure_module("fastapi", package=True)
    fastapi_concurrency = ensure_module("fastapi.concurrency")

    class _Settings:
        """提供图片 URL 转换与 Fanart 配置的测试设置。"""

        PROXY = None
        FANART_API_KEY = None

        @staticmethod
        def TMDB_IMAGE_URL(path: str, size: str) -> str:
            """返回可断言的测试图片地址。"""
            return f"https://image.tmdb.test/{size}{path}"

    class _Logger:
        """提供插件日志调用的空实现。"""

        def info(self, *args, **kwargs):
            """忽略 info 日志。"""

        def warning(self, *args, **kwargs):
            """忽略 warning 日志。"""

        def error(self, *args, **kwargs):
            """忽略 error 日志。"""

    class _MediaType(Enum):
        """模拟 MoviePilot 媒体类型枚举。"""

        MOVIE = "电影"
        TV = "电视剧"

    class _MediaInfo:
        """模拟 MoviePilot MediaInfo，允许测试按需传入字段。"""

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _TmdbApi:
        """避免测试意外发出 TMDB 请求。"""

        def __init__(self, *args, **kwargs):
            self.movie = types.SimpleNamespace(images=lambda **kwargs: {})
            self.tv = types.SimpleNamespace(images=lambda **kwargs: {})

    class _RequestUtils:
        """避免测试意外发出 Fanart 请求。"""

        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            """返回空响应。"""
            return None

    async def _run_in_threadpool(func, *args, **kwargs):
        """在无 FastAPI 依赖时同步执行线程池调用。"""
        return func(*args, **kwargs)

    class _PluginBase:
        """提供插件基类空实现。"""

    def set_if_missing(module, name, value):
        """只在既有桩缺少属性时补齐，避免覆盖其它插件测试桩。"""
        if not hasattr(module, name):
            setattr(module, name, value)

    existing_settings = getattr(config, "settings", None)
    if existing_settings is None or not hasattr(existing_settings, "TMDB_IMAGE_URL"):
        settings_proxy = _Settings()
        if existing_settings is not None:
            for name in ("PROXY", "FANART_API_KEY", "TMDB_API_KEY", "TZ"):
                if hasattr(existing_settings, name):
                    setattr(settings_proxy, name, getattr(existing_settings, name))
        config.settings = settings_proxy
    set_if_missing(context, "MediaInfo", _MediaInfo)
    set_if_missing(log, "logger", _Logger())

    existing_tmdb_api = getattr(themoviedb, "TmdbApi", None)
    try:
        tmdb_signature = inspect.signature(existing_tmdb_api)
        tmdb_accepts_language = "language" in tmdb_signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in tmdb_signature.parameters.values()
        )
    except (TypeError, ValueError):
        tmdb_accepts_language = True
    if existing_tmdb_api is None or not tmdb_accepts_language:
        themoviedb.TmdbApi = _TmdbApi

    set_if_missing(http, "RequestUtils", _RequestUtils)
    set_if_missing(plugins, "_PluginBase", _PluginBase)
    set_if_missing(schema_types, "MediaType", _MediaType)
    set_if_missing(fastapi_concurrency, "run_in_threadpool", _run_in_threadpool)

    set_if_missing(app, "core", core)
    set_if_missing(app, "log", log)
    set_if_missing(app, "modules", modules)
    set_if_missing(app, "plugins", plugins)
    set_if_missing(app, "schemas", schemas)
    set_if_missing(core, "config", config)
    set_if_missing(core, "context", context)
    set_if_missing(modules, "themoviedb", themoviedb)
    set_if_missing(utils, "http", http)
    set_if_missing(schemas, "types", schema_types)
    set_if_missing(fastapi, "concurrency", fastapi_concurrency)

    def restore():
        """恢复导入插件前的 sys.modules 与桩模块属性。"""
        current_names = {
            name
            for name in sys.modules
            if name == "app"
            or name.startswith("app.")
            or name == "fastapi"
            or name.startswith("fastapi.")
        }
        for name in current_names - snapshots.keys():
            sys.modules.pop(name, None)
        for name, (module, attributes) in snapshots.items():
            if sys.modules.get(name) is not module:
                sys.modules[name] = module
            module.__dict__.clear()
            module.__dict__.update(attributes)

    return restore


_restore_stubs = _install_moviepilot_stubs()

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins.v2" / "tmdbposterlanguagepriority"
sys.path.insert(0, str(PLUGIN_DIR.parent))

try:
    from app.core.context import MediaInfo  # noqa: E402
    from app.schemas.types import MediaType  # noqa: E402
    from tmdbposterlanguagepriority import TmdbPosterLanguagePriority  # noqa: E402
finally:
    _restore_stubs()


def _plugin(priority):
    """构造不访问数据库和网络的插件测试实例。"""
    plugin = TmdbPosterLanguagePriority.__new__(TmdbPosterLanguagePriority)
    plugin._enabled = True
    plugin._priority = priority
    plugin._selection_cache = OrderedDict()
    plugin._cache_lock = threading.RLock()
    return plugin


def _media(original_language="ja", tmdb_id=1):
    """构造测试媒体信息。"""
    return MediaInfo(
        tmdb_id=tmdb_id,
        tvdb_id=2,
        type=MediaType.MOVIE,
        title="测试影片",
        title_year="测试影片 (2026)",
        year="2026",
        original_language=original_language,
    )


def test_default_priority_order():
    """默认顺序包含四个 TMDB 中文地区层。"""
    assert TmdbPosterLanguagePriority.DEFAULT_PRIORITY == [
        "tmdb_zh_cn",
        "tmdb_zh_sg",
        "tmdb_zh_tw",
        "tmdb_zh_hk",
        "tmdb_zh",
        "fanart_chinese",
        "tmdb_original",
        "tmdb_en_us",
        "tmdb_en",
        "fanart_english",
        "tmdb_null",
    ]


def test_priority_normalization_keeps_order_and_removes_duplicates():
    """自定义顺序应保序、去重并忽略未知候选层。"""
    assert TmdbPosterLanguagePriority._normalize_priority(
        ["fanart_english", "tmdb_zh_cn", "fanart_english", "unknown"]
    ) == ["fanart_english", "tmdb_zh_cn"]


def test_empty_priority_is_explicitly_disabled_but_missing_uses_default():
    """显式清空优先级应禁用候选，缺失配置才使用默认顺序。"""
    assert TmdbPosterLanguagePriority._normalize_priority([]) == []
    assert TmdbPosterLanguagePriority._normalize_priority("") == []
    assert TmdbPosterLanguagePriority._normalize_priority(None) == (
        TmdbPosterLanguagePriority.DEFAULT_PRIORITY
    )
    plugin = TmdbPosterLanguagePriority.__new__(TmdbPosterLanguagePriority)
    plugin.init_plugin({"enabled": True, "priority": []})
    assert plugin._priority == []
    plugin.init_plugin({"enabled": True})
    assert plugin._priority == TmdbPosterLanguagePriority.DEFAULT_PRIORITY


def test_image_locale_normalization_supports_composite_and_region_fields():
    """图片语言规范化支持复合标签与独立地区字段，并保留泛中文。"""
    normalize = TmdbPosterLanguagePriority._normalize_image_locale
    assert normalize("ZH_CN") == "zh-cn"
    assert normalize("zh+CN") == "zh-cn"
    assert normalize("zh", "CN") == "zh-cn"
    assert normalize("zh", "tw") == "zh-tw"
    assert normalize("zh-TW", "CN") == "zh-tw"
    assert normalize("zh") == "zh"
    assert normalize(None, "CN") == "null"


def test_tmdb_regional_matching_accepts_both_response_shapes():
    """简繁地区层兼容复合 iso_639_1 和独立 iso_3166_1。"""
    images = [
        {"file_path": "/generic.jpg", "iso_639_1": "zh", "vote_average": 10},
        {"file_path": "/cn.jpg", "iso_639_1": "zh", "iso_3166_1": "CN", "vote_average": 1},
        {"file_path": "/sg.jpg", "iso_639_1": "zh_SG", "vote_average": 2},
        {"file_path": "/tw.jpg", "iso_639_1": "zh-TW", "vote_average": 3},
        {"file_path": "/hk.jpg", "iso_639_1": "zh", "iso_3166_1": "HK", "vote_average": 4},
    ]
    pick = TmdbPosterLanguagePriority._pick_tmdb_priority
    assert pick(images, "tmdb_zh_cn", "ja")["url"].endswith("/cn.jpg")
    assert pick(images, "tmdb_zh_sg", "ja")["url"].endswith("/sg.jpg")
    assert pick(images, "tmdb_zh_tw", "ja")["url"].endswith("/tw.jpg")
    assert pick(images, "tmdb_zh_hk", "ja")["url"].endswith("/hk.jpg")
    assert pick(images, "tmdb_zh", "ja")["url"].endswith("/generic.jpg")
    assert pick(images, "tmdb_zh_cn", "ja")["language"] == "zh-cn"


def test_generic_language_layers_do_not_match_regional_images():
    """泛中文和泛英语层必须严格排除带地区的图片。"""
    images = [
        {"file_path": "/zh-cn.jpg", "iso_639_1": "zh-CN", "vote_average": 10},
        {"file_path": "/en-us.jpg", "iso_639_1": "en-US", "vote_average": 10},
        {"file_path": "/en.jpg", "iso_639_1": "en", "vote_average": 1},
    ]
    pick = TmdbPosterLanguagePriority._pick_tmdb_priority
    assert pick(images, "tmdb_zh", "ja") is None
    assert pick(images, "tmdb_en", "ja")["url"].endswith("/en.jpg")


def test_tmdb_include_languages_follows_enabled_priority_and_deduplicates():
    """TMDB images 请求只包含启用层，并规范化大小写、下划线与重复项。"""
    plugin = _plugin(
        ["tmdb_zh_cn", "tmdb_original", "tmdb_zh", "tmdb_en_us", "tmdb_en", "tmdb_null"]
    )
    calls = []

    class _Images:
        """记录 images 请求参数的测试对象。"""

        def __call__(self, **kwargs):
            calls.append(kwargs)
            return {}

    plugin._tmdb = types.SimpleNamespace(
        movie=types.SimpleNamespace(images=_Images()),
        tv=types.SimpleNamespace(images=_Images()),
    )
    plugin._get_tmdb_images(_media(original_language="en_US"), "en-us")
    assert len(calls) == 1
    assert calls[0]["movie_id"] == 1
    assert calls[0]["include_image_language"] == "zh-CN,en-US,zh,en,null"


def test_default_order_selects_fanart_chinese_before_source_language():
    """没有 TMDB 简体图时，Fanart 中文必须先于 TMDB 源语言。"""
    plugin = _plugin(TmdbPosterLanguagePriority.DEFAULT_PRIORITY.copy())
    plugin._get_tmdb_images = lambda *_: {
        "posters": [
            {"file_path": "/ja.jpg", "iso_639_1": "ja", "vote_average": 9},
            {"file_path": "/en.jpg", "iso_639_1": "en-US", "vote_average": 9},
            {"file_path": "/null.jpg", "iso_639_1": None, "vote_average": 9},
        ],
        "backdrops": [],
        "logos": [],
    }
    plugin._get_fanart_images = lambda *_: {
        "chinese": {"poster": [{"url": "https://fanart/zh.jpg", "lang": "zh", "likes": "1"}]},
        "english": {"poster": [{"url": "https://fanart/en.jpg", "lang": "en", "likes": "9"}]},
    }
    selected = plugin._select_images(_media("ja"))
    assert selected["priority_key"] == "fanart_chinese"
    assert selected["poster_url"] == "https://fanart/zh.jpg"


def test_custom_order_is_applied():
    """用户调整后的优先级必须覆盖默认顺序。"""
    plugin = _plugin(["tmdb_original", "fanart_chinese", "tmdb_null"])
    plugin._get_tmdb_images = lambda *_: {
        "posters": [
            {"file_path": "/ja.jpg", "iso_639_1": "ja", "vote_average": 1},
            {"file_path": "/null.jpg", "iso_639_1": None, "vote_average": 10},
        ],
        "backdrops": [],
        "logos": [],
    }
    plugin._get_fanart_images = lambda *_: {
        "chinese": {"poster": [{"url": "https://fanart/zh.jpg", "lang": "zh", "likes": "99"}]},
        "english": {"poster": []},
    }
    selected = plugin._select_images(_media("ja"))
    assert selected["priority_key"] == "tmdb_original"
    assert selected["poster_url"].endswith("/ja.jpg")


def test_selection_cache_is_bounded_lru():
    """图片选择缓存应限制容量并淘汰最久未使用条目。"""
    plugin = _plugin(["tmdb_null"])
    plugin._get_tmdb_images = lambda *_: {
        "posters": [{"file_path": "/poster.jpg", "iso_639_1": None}],
        "backdrops": [],
        "logos": [],
    }
    for tmdb_id in range(plugin.SELECTION_CACHE_MAX_SIZE):
        plugin._select_images(_media(tmdb_id=tmdb_id))
    # 访问最早条目后，它应成为最新条目而不是被下一次写入淘汰。
    plugin._select_images(_media(tmdb_id=0))
    plugin._select_images(_media(tmdb_id=plugin.SELECTION_CACHE_MAX_SIZE))
    assert len(plugin._selection_cache) == plugin.SELECTION_CACHE_MAX_SIZE
    assert ("电影", 0, "ja") in plugin._selection_cache
    assert ("电影", 1, "ja") not in plugin._selection_cache
    assert ("电影", plugin.SELECTION_CACHE_MAX_SIZE, "ja") in plugin._selection_cache


def test_old_config_keys_remain_compatible():
    """旧版只配置 zh-CN/zh-SG 时应原样保留，不自动插入新层。"""
    old_priority = ["tmdb_zh_cn", "tmdb_zh_sg", "tmdb_original"]
    assert TmdbPosterLanguagePriority._normalize_priority(old_priority) == old_priority


def test_empty_priority_returns_native_fallback():
    """清空优先级后不应接管原生图片流程。"""
    plugin = _plugin([])
    assert plugin.obtain_images(_media()) is None


def test_no_candidate_returns_none_for_native_fallback():
    """全部候选缺失时应返回 None，让 MoviePilot 继续原生图片链路。"""
    plugin = _plugin(["tmdb_zh_cn"])
    plugin._get_tmdb_images = lambda *_: {"posters": [], "backdrops": [], "logos": []}
    plugin._get_fanart_images = lambda *_: {"chinese": [], "english": []}
    assert plugin.obtain_images(_media()) is None
