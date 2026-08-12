"""合集图片候选选择逻辑测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "plugins.v2"
        / "embymediaimagemanager"
        / "collection_artwork.py"
    )
    spec = importlib.util.spec_from_file_location("collection_artwork_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def test_collection_selection_keeps_poster_and_logo_independent():
    selected = MODULE.select_collection_images(
        {
            "posters": [
                {
                    "file_path": "/cn.jpg",
                    "iso_639_1": "zh",
                    "iso_3166_1": "CN",
                    "vote_average": 8,
                }
            ],
            "logos": [
                {
                    "file_path": "/en.png",
                    "iso_639_1": "en",
                    "iso_3166_1": "US",
                    "vote_average": 9,
                }
            ],
        },
        {},
        MODULE.DEFAULT_PRIORITY,
        image_url=lambda path: f"https://img.test{path}",
    )
    assert selected["poster"]["language"] == "zh-cn"
    assert selected["poster"]["priority_key"] == "tmdb_zh_cn"
    assert selected["logo"]["language"] == "en-us"
    assert selected["logo"]["priority_key"] == "tmdb_en_us"


def test_traditional_regions_are_never_selected():
    selected = MODULE.pick_tmdb_image(
        [
            {
                "file_path": "/tw.jpg",
                "iso_639_1": "zh",
                "iso_3166_1": "TW",
                "vote_average": 10,
            },
            {
                "file_path": "/cn.jpg",
                "iso_639_1": "zh",
                "iso_3166_1": "CN",
                "vote_average": 7,
            },
        ],
        "tmdb_zh_cn",
        image_url=lambda path: path,
    )
    assert selected["url"] == "/cn.jpg"


def test_fanart_payload_is_grouped_by_language_and_type():
    groups = MODULE.normalize_fanart_payload(
        {
            "movieposter": [
                {"url": "zh.jpg", "lang": "zh", "likes": 2},
                {"url": "en.jpg", "lang": "en", "likes": 9},
            ],
            "movielogo": [{"url": "logo.png", "lang": "en", "likes": 4}],
        }
    )
    assert groups["chinese"]["poster"][0]["url"] == "zh.jpg"
    assert groups["english"]["poster"][0]["url"] == "en.jpg"
    assert groups["english"]["logo"][0]["url"] == "logo.png"
