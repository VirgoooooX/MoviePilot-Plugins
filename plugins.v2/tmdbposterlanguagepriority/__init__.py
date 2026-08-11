"""TMDB 与 Fanart 海报语言优先插件。"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.context import MediaInfo
from app.log import logger
from app.modules.themoviedb import TmdbApi
from app.plugins import _PluginBase
from app.schemas.types import MediaType
from app.utils.http import RequestUtils


class TmdbPosterLanguagePriority(_PluginBase):
    """在入库前按可配置优先级选择 TMDB/Fanart 主海报。"""

    plugin_name = "TMDB/Fanart 海报优先"
    plugin_desc = "按来源与语言优先级选择媒体海报。"
    plugin_icon = "fullscreenposterwall.png"
    plugin_version = "1.1.0"
    plugin_label = "元数据,海报"
    plugin_author = "VirgoooooX"
    author_url = "https://github.com/VirgoooooX/MoviePilot-Plugins"
    plugin_config_prefix = "tmdbposterlanguagepriority_"
    plugin_order = 18
    auth_level = 1

    DEFAULT_PRIORITY = [
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
    TMDB_IMAGE_LANGUAGES = (
        "zh-CN",
        "zh-SG",
        "zh-TW",
        "zh-HK",
        "zh",
        "en-US",
        "en",
        "null",
    )
    SELECTION_CACHE_MAX_SIZE = 256
    PRIORITY_OPTIONS = [
        {"title": "TMDB 简体中文（zh-CN）", "value": "tmdb_zh_cn"},
        {"title": "TMDB 新加坡中文（zh-SG）", "value": "tmdb_zh_sg"},
        {"title": "TMDB 繁体中文（zh-TW）", "value": "tmdb_zh_tw"},
        {"title": "TMDB 香港中文（zh-HK）", "value": "tmdb_zh_hk"},
        {"title": "TMDB 泛中文（zh）", "value": "tmdb_zh"},
        {"title": "Fanart Chinese（zh）", "value": "fanart_chinese"},
        {"title": "TMDB 媒体源语言", "value": "tmdb_original"},
        {"title": "TMDB 英语（en-US）", "value": "tmdb_en_us"},
        {"title": "TMDB 泛英语（en）", "value": "tmdb_en"},
        {"title": "Fanart English（en）", "value": "fanart_english"},
        {"title": "TMDB 无文字（null）", "value": "tmdb_null"},
    ]
    PRIORITY_LABELS = {item["value"]: item["title"] for item in PRIORITY_OPTIONS}

    _enabled = False
    _priority: List[str] = DEFAULT_PRIORITY.copy()
    _tmdb: Optional[TmdbApi] = None

    def init_plugin(self, config: dict = None) -> None:
        """初始化插件配置并清空本轮图片选择缓存。"""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._priority = self._normalize_priority(
            config["priority"] if "priority" in config else None
        )
        self._tmdb = TmdbApi(language="zh-CN")
        self._selection_cache: OrderedDict[Tuple[str, int, str], Dict[str, Any]] = (
            OrderedDict()
        )
        self._cache_lock = threading.RLock()

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回 Vuetify JSON 配置表单和默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用入库前海报优先选择",
                                            "color": "primary",
                                            "hint": "仅影响后续经过 MoviePilot 图片补全流程的新媒体，不扫描或刷新存量媒体。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "priority",
                                            "label": "海报候选优先级",
                                            "items": self.PRIORITY_OPTIONS,
                                            "item-title": "title",
                                            "item-value": "value",
                                            "multiple": True,
                                            "chips": True,
                                            "closable-chips": True,
                                            "return-object": False,
                                            "variant": "outlined",
                                            "hint": "已选项的数组顺序就是匹配顺序；移除某项即禁用该候选层。保存后重新打开可核对实际顺序。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "默认顺序：TMDB zh-CN → TMDB zh-SG → TMDB zh-TW → TMDB zh-HK → TMDB 泛中文 → Fanart Chinese → TMDB 源语言 → TMDB en-US → TMDB 泛英语 → Fanart English → TMDB null。移除候选层会禁用该层。",
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ], {"enabled": False, "priority": self.DEFAULT_PRIORITY.copy()}

    def get_page(self) -> Optional[List[dict]]:
        """返回当前策略摘要页面。"""
        order_text = " → ".join(
            self.PRIORITY_LABELS.get(item, item) for item in self._priority
        )
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if self._enabled else "info",
                    "variant": "tonal",
                    "title": "当前海报策略",
                    "text": order_text if self._enabled else "插件未启用，不会接管图片补全流程。",
                },
            }
        ]

    def get_module(self) -> Dict[str, Any]:
        """声明入库前同步和异步图片获取模块。"""
        if not self._enabled:
            return {}
        return {
            "obtain_images": self.obtain_images,
            "async_obtain_images": self.async_obtain_images,
        }

    def stop_service(self) -> None:
        """停止插件并清理图片选择缓存。"""
        cache_lock = getattr(self, "_cache_lock", None)
        if cache_lock:
            with cache_lock:
                getattr(self, "_selection_cache", {}).clear()
        self._tmdb = None

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """在入库前选择主海报，并补齐同次请求可得的背景和标志图。"""
        if not self._enabled or not self._is_supported_media(mediainfo):
            return None

        selected = self._select_images(mediainfo)
        if not selected.get("poster_url"):
            logger.info("%s 未命中插件海报候选，交回 MoviePilot 原生图片链路", mediainfo.title_year)
            return None

        mediainfo.poster_path = selected["poster_url"]
        if not mediainfo.backdrop_path and selected.get("backdrop_url"):
            mediainfo.backdrop_path = selected["backdrop_url"]
        if not mediainfo.logo_path and selected.get("logo_url"):
            mediainfo.logo_path = selected["logo_url"]
        setattr(mediainfo, "_poster_priority_selection", selected)
        logger.info(
            "%s 入库前海报选择：%s（%s）",
            mediainfo.title_year,
            selected.get("priority_label") or "未知层级",
            selected.get("poster_language") or "未知语言",
        )
        return mediainfo

    async def async_obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """在线程池中执行入库前图片选择，避免阻塞异步识别流程。"""
        return await run_in_threadpool(self.obtain_images, mediainfo)

    @staticmethod
    def _is_supported_media(mediainfo: Optional[MediaInfo]) -> bool:
        """判断媒体是否具备 TMDB/Fanart 图片选择所需的类型和 ID。"""
        return bool(
            mediainfo
            and mediainfo.tmdb_id
            and mediainfo.type in (MediaType.MOVIE, MediaType.TV)
        )

    @classmethod
    def _normalize_priority(cls, priority: Any) -> List[str]:
        """校验、去重并保留用户配置的候选层顺序。"""
        if priority is None:
            return cls.DEFAULT_PRIORITY.copy()
        if isinstance(priority, str):
            raw_items = [item.strip() for item in priority.replace(",", "\n").splitlines()]
        elif isinstance(priority, list):
            raw_items = [str(item).strip() for item in priority]
        else:
            raw_items = []

        valid = set(cls.PRIORITY_LABELS)
        normalized: List[str] = []
        for item in raw_items:
            if item in valid and item not in normalized:
                normalized.append(item)
        return normalized

    def _select_images(self, mediainfo: MediaInfo) -> Dict[str, Any]:
        """读取一次 TMDB 候选并按配置按需读取 Fanart 后选择图片。"""
        source_language = self._normalize_language(mediainfo.original_language)
        cache_key = (
            str(mediainfo.type.value),
            int(mediainfo.tmdb_id),
            source_language,
        )
        with self._cache_lock:
            cached = self._selection_cache.get(cache_key)
            if cached is not None and hasattr(self._selection_cache, "move_to_end"):
                self._selection_cache.move_to_end(cache_key)
        if cached:
            return dict(cached)

        if not self._priority:
            selected = {
                "poster_url": None,
                "poster_language": None,
                "priority_key": None,
                "priority_label": None,
                "backdrop_url": None,
                "logo_url": None,
            }
            self._cache_selection(cache_key, selected)
            return selected

        tmdb_images = self._get_tmdb_images(mediainfo, source_language)
        fanart_images: Optional[Dict[str, List[dict]]] = None
        poster_url = None
        poster_language = None
        selected_priority = None

        for priority_key in self._priority:
            if priority_key.startswith("tmdb_"):
                candidate = self._pick_tmdb_priority(
                    tmdb_images.get("posters") or [],
                    priority_key,
                    source_language,
                )
            else:
                if fanart_images is None:
                    fanart_images = self._get_fanart_images(mediainfo)
                fanart_group = (
                    fanart_images.get("chinese")
                    if priority_key == "fanart_chinese"
                    else fanart_images.get("english")
                )
                candidate = self._pick_fanart_image(fanart_group)

            if candidate:
                poster_url = candidate.get("url")
                poster_language = candidate.get("language")
                selected_priority = priority_key
                break

        selected = {
            "poster_url": poster_url,
            "poster_language": poster_language,
            "priority_key": selected_priority,
            "priority_label": self.PRIORITY_LABELS.get(selected_priority or ""),
            "backdrop_url": self._pick_supporting_tmdb_image(
                tmdb_images.get("backdrops") or [], source_language
            ),
            "logo_url": self._pick_supporting_tmdb_image(
                tmdb_images.get("logos") or [], source_language
            ),
        }
        self._cache_selection(cache_key, selected)
        return selected

    def _cache_selection(
        self, cache_key: Tuple[str, int, str], selected: Dict[str, Any]
    ) -> None:
        """写入图片选择缓存并淘汰最久未使用的条目。"""
        with self._cache_lock:
            self._selection_cache[cache_key] = dict(selected)
            if hasattr(self._selection_cache, "move_to_end"):
                self._selection_cache.move_to_end(cache_key)
                while len(self._selection_cache) > self.SELECTION_CACHE_MAX_SIZE:
                    self._selection_cache.popitem(last=False)
                return

            # 兼容旧测试或外部调用手工注入普通 dict 的情况。
            while len(self._selection_cache) > self.SELECTION_CACHE_MAX_SIZE:
                oldest_key = next(iter(self._selection_cache))
                del self._selection_cache[oldest_key]

    def _get_tmdb_images(
        self, mediainfo: MediaInfo, source_language: str
    ) -> Dict[str, Any]:
        """通过一次 TMDB images 请求获取所有启用语言层的候选。"""
        language_by_priority = {
            "tmdb_zh_cn": "zh-CN",
            "tmdb_zh_sg": "zh-SG",
            "tmdb_zh_tw": "zh-TW",
            "tmdb_zh_hk": "zh-HK",
            "tmdb_zh": "zh",
            "tmdb_original": source_language,
            "tmdb_en_us": "en-US",
            "tmdb_en": "en",
            "tmdb_null": "null",
        }
        include_languages: List[str] = []
        for priority_key in self._priority:
            language = language_by_priority.get(priority_key)
            if not language:
                continue
            canonical_language = self._canonical_tmdb_image_language(language)
            if canonical_language and canonical_language not in include_languages:
                include_languages.append(canonical_language)
        # null 是无文字海报的必要候选，保留原有请求兼容性并去重。
        if "null" not in include_languages:
            include_languages.append("null")

        try:
            api = self._tmdb or TmdbApi(language="zh-CN")
            include_value = ",".join(include_languages)
            if mediainfo.type == MediaType.MOVIE:
                return api.movie.images(
                    movie_id=int(mediainfo.tmdb_id),
                    include_image_language=include_value,
                ) or {}
            return api.tv.images(
                tv_id=int(mediainfo.tmdb_id),
                include_image_language=include_value,
            ) or {}
        except Exception as err:
            logger.warning("%s 获取 TMDB 图片候选失败：%s", mediainfo.title_year, err)
            return {}

    @classmethod
    def _canonical_tmdb_image_language(cls, language: Any) -> str:
        """把 TMDB 图片请求语言规范化为 API 需要的大小写格式。"""
        normalized = cls._normalize_language(language)
        canonical = {
            item.lower(): item for item in cls.TMDB_IMAGE_LANGUAGES
        }
        return canonical.get(normalized, normalized)

    @classmethod
    def _pick_tmdb_priority(
        cls,
        images: List[dict],
        priority_key: str,
        source_language: str,
    ) -> Optional[Dict[str, str]]:
        """从 TMDB 海报中严格匹配一个配置层并选择评分最佳项。"""
        candidates: List[dict] = []
        for image in images:
            image_language = cls._normalize_image_locale(
                image.get("iso_639_1"), image.get("iso_3166_1")
            )
            if priority_key == "tmdb_zh_cn" and image_language == "zh-cn":
                candidates.append(image)
            elif priority_key == "tmdb_zh_sg" and image_language == "zh-sg":
                candidates.append(image)
            elif priority_key == "tmdb_zh_tw" and image_language == "zh-tw":
                candidates.append(image)
            elif priority_key == "tmdb_zh_hk" and image_language == "zh-hk":
                candidates.append(image)
            elif priority_key == "tmdb_zh" and image_language == "zh":
                candidates.append(image)
            elif priority_key == "tmdb_original" and cls._matches_source_language(
                image_language, source_language
            ):
                candidates.append(image)
            elif priority_key == "tmdb_en_us" and image_language == "en-us":
                candidates.append(image)
            elif priority_key == "tmdb_en" and image_language == "en":
                candidates.append(image)
            elif priority_key == "tmdb_null" and image_language == "null":
                candidates.append(image)

        if not candidates:
            return None
        selected = sorted(
            candidates,
            key=lambda item: (
                float(item.get("vote_average") or 0),
                int(item.get("vote_count") or 0),
            ),
            reverse=True,
        )[0]
        file_path = selected.get("file_path")
        if not file_path:
            return None
        return {
            "url": cls._tmdb_image_url(file_path),
            "language": cls._normalize_image_locale(
                selected.get("iso_639_1"), selected.get("iso_3166_1")
            ),
        }

    @classmethod
    def _matches_source_language(
        cls, image_language: str, source_language: str
    ) -> bool:
        """判断 TMDB 图片标签是否与媒体源语言一致。"""
        if not image_language or not source_language or image_language == "null":
            return False
        if source_language == "zh":
            return image_language == "zh"
        if "-" in source_language:
            return image_language == source_language
        return image_language == source_language or image_language.startswith(
            source_language + "-"
        )

    @classmethod
    def _pick_supporting_tmdb_image(
        cls, images: List[dict], source_language: str
    ) -> Optional[str]:
        """为背景和标志图选择无文字、源语言或最高评分候选。"""
        if not images:
            return None
        ranked = sorted(
            images,
            key=lambda item: (
                2
                if cls._normalize_image_locale(
                    item.get("iso_639_1"), item.get("iso_3166_1")
                )
                == "null"
                else 0,
                1
                if cls._matches_source_language(
                    cls._normalize_image_locale(
                        item.get("iso_639_1"), item.get("iso_3166_1")
                    ),
                    source_language,
                )
                else 0,
                float(item.get("vote_average") or 0),
                int(item.get("vote_count") or 0),
            ),
            reverse=True,
        )
        file_path = ranked[0].get("file_path")
        return cls._tmdb_image_url(file_path) if file_path else None

    def _get_fanart_images(self, mediainfo: MediaInfo) -> Dict[str, List[dict]]:
        """获取 Fanart 原始主海报并拆分 Chinese 与 English 候选。"""
        if not settings.FANART_API_KEY:
            return {"chinese": [], "english": []}
        if mediainfo.type == MediaType.MOVIE:
            query_type = "movies"
            query_id = mediainfo.tmdb_id
        else:
            query_type = "tv"
            query_id = mediainfo.tvdb_id
        if not query_id:
            return {"chinese": [], "english": []}

        url = (
            f"https://webservice.fanart.tv/v3/{query_type}/{query_id}"
            f"?api_key={settings.FANART_API_KEY}"
        )
        try:
            response = RequestUtils(
                proxies=settings.PROXY, timeout=10
            ).get_res(url, raise_exception=True)
            payload = response.json() if response else {}
        except Exception as err:
            logger.warning("%s 获取 Fanart 图片候选失败：%s", mediainfo.title_year, err)
            return {"chinese": [], "english": []}

        poster_items: List[dict] = []
        for key in ("movieposter", "tvposter"):
            items = payload.get(key) or []
            if isinstance(items, list):
                poster_items.extend(items)
        return {
            "chinese": [
                item
                for item in poster_items
                if self._normalize_fanart_language(item.get("lang")) == "zh"
                and item.get("url")
            ],
            "english": [
                item
                for item in poster_items
                if self._normalize_fanart_language(item.get("lang")) == "en"
                and item.get("url")
            ],
        }

    @classmethod
    def _pick_fanart_image(
        cls, images: Optional[List[dict]]
    ) -> Optional[Dict[str, str]]:
        """在 Fanart 同语言海报中按 likes 选择图片。"""
        if not images:
            return None
        selected = sorted(
            images,
            key=lambda item: int(item.get("likes") or 0),
            reverse=True,
        )[0]
        if not selected.get("url"):
            return None
        return {
            "url": selected["url"],
            "language": cls._normalize_fanart_language(selected.get("lang")),
        }

    @classmethod
    def _normalize_fanart_language(cls, language: Any) -> str:
        """统一 Fanart 语言标签并归类中英文。"""
        tag = cls._normalize_language(language)
        if tag in {"zh", "zh-cn", "zh-sg", "zh-tw", "zh-hk", "zho", "cmn"}:
            return "zh"
        if tag in {"en", "en-us", "eng"}:
            return "en"
        return tag

    @staticmethod
    def _normalize_language(language: Any) -> str:
        """统一语言标签大小写、下划线和空值表示。"""
        if language is None:
            return "null"
        tag = str(language).strip().lower().replace("_", "-").replace("+", "-")
        return "null" if not tag or tag == "00" else tag

    @classmethod
    def _normalize_image_locale(
        cls, image_language: Any, image_region: Any = None
    ) -> str:
        """规范化 TMDB 图片语言和地区，优先使用复合语言标签。"""
        language = cls._normalize_language(image_language)
        if language == "null":
            return "null"

        # TMDB 新接口可能直接返回 zh-CN；复合标签优先于独立地区字段。
        # zh-Hans/zh-Hant 只表示文字脚本，不在此猜测为某个地区。
        if "-" in language:
            parts = language.split("-")
            if len(parts) >= 2 and parts[0] and parts[1]:
                return f"{parts[0]}-{parts[1]}"
            return language

        region = cls._normalize_language(image_region)
        if region != "null":
            region = region.split("-")[-1]
            if len(region) == 2 and region.isalpha():
                return f"{language}-{region}"
        # 没有地区字段的 zh 必须保留为泛中文，不能猜成某一地区。
        return language

    @staticmethod
    def _tmdb_image_url(image_path: Optional[str]) -> Optional[str]:
        """把 TMDB 相对图片路径转换成原图地址。"""
        if not image_path:
            return None
        if str(image_path).startswith("http"):
            return str(image_path)
        return settings.TMDB_IMAGE_URL(str(image_path), "original")
