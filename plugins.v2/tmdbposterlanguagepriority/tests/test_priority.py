"""TMDB/Fanart 海报优先插件的纯逻辑测试。"""

import threading

from app.core.context import MediaInfo
from app.schemas.types import MediaType
from tmdbposterlanguagepriority import TmdbPosterLanguagePriority


def _plugin(priority):
    """构造不访问数据库和网络的插件测试实例。"""
    plugin = TmdbPosterLanguagePriority.__new__(TmdbPosterLanguagePriority)
    plugin._enabled = True
    plugin._priority = priority
    plugin._selection_cache = {}
    plugin._cache_lock = threading.RLock()
    return plugin


def _media(original_language="ja"):
    """构造测试媒体信息。"""
    return MediaInfo(
        tmdb_id=1,
        tvdb_id=2,
        type=MediaType.MOVIE,
        title="测试影片",
        year="2026",
        original_language=original_language,
    )


def test_default_priority_order():
    """默认顺序必须与需求约定一致。"""
    assert TmdbPosterLanguagePriority.DEFAULT_PRIORITY == [
        "tmdb_zh_cn",
        "tmdb_zh_sg",
        "fanart_chinese",
        "tmdb_original",
        "tmdb_en_us",
        "fanart_english",
        "tmdb_null",
    ]


def test_priority_normalization_keeps_order_and_removes_duplicates():
    """自定义顺序应保序、去重并忽略未知候选层。"""
    assert TmdbPosterLanguagePriority._normalize_priority(
        ["fanart_english", "tmdb_zh_cn", "fanart_english", "unknown"]
    ) == ["fanart_english", "tmdb_zh_cn"]


def test_strict_tmdb_language_matching_excludes_traditional_and_short_zh():
    """简体候选只能命中完整标签，不能混入 zh、zh-TW 或 zh-HK。"""
    images = [
        {"file_path": "/short.jpg", "iso_639_1": "zh", "vote_average": 10},
        {"file_path": "/tw.jpg", "iso_639_1": "zh-TW", "vote_average": 10},
        {"file_path": "/hk.jpg", "iso_639_1": "zh-HK", "vote_average": 10},
        {"file_path": "/cn.jpg", "iso_639_1": "zh-CN", "vote_average": 1},
        {"file_path": "/sg.jpg", "iso_639_1": "zh-SG", "vote_average": 2},
    ]
    cn = TmdbPosterLanguagePriority._pick_tmdb_priority(
        images, "tmdb_zh_cn", "ja"
    )
    sg = TmdbPosterLanguagePriority._pick_tmdb_priority(
        images, "tmdb_zh_sg", "ja"
    )
    assert cn["url"].endswith("/cn.jpg")
    assert sg["url"].endswith("/sg.jpg")


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
        "chinese": [{"url": "https://fanart/zh.jpg", "lang": "zh", "likes": "1"}],
        "english": [{"url": "https://fanart/en.jpg", "lang": "en", "likes": "9"}],
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
        "chinese": [{"url": "https://fanart/zh.jpg", "lang": "zh", "likes": "99"}],
        "english": [],
    }
    selected = plugin._select_images(_media("ja"))
    assert selected["priority_key"] == "tmdb_original"
    assert selected["poster_url"].endswith("/ja.jpg")


def test_no_candidate_returns_none_for_native_fallback():
    """全部候选缺失时应返回 None，让 MoviePilot 继续原生图片链路。"""
    plugin = _plugin(["tmdb_zh_cn"])
    plugin._get_tmdb_images = lambda *_: {
        "posters": [],
        "backdrops": [],
        "logos": [],
    }
    plugin._get_fanart_images = lambda *_: {"chinese": [], "english": []}
    assert plugin.obtain_images(_media()) is None
