"""TMDB/Fanart 合集图片选择逻辑。

这个模块只负责把已经取得的图片候选按统一优先级选出 poster/logo，
不触碰 Emby 合集成员，也不负责网络请求或写入。这样合集图片功能可以
复用 ``TmdbPosterLanguagePriority`` 的配置，同时保持职责边界清晰。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional


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

PRIORITY_LABELS = {
    "tmdb_zh_cn": "TMDB 简体中文（zh-CN）",
    "tmdb_zh_sg": "TMDB 新加坡中文（zh-SG）",
    "tmdb_zh_tw": "TMDB 繁体中文（zh-TW）",
    "tmdb_zh_hk": "TMDB 香港中文（zh-HK）",
    "tmdb_zh": "TMDB 泛中文（zh）",
    "fanart_chinese": "Fanart Chinese（zh）",
    "tmdb_original": "TMDB 媒体源语言",
    "tmdb_en_us": "TMDB 英语（en-US）",
    "tmdb_en": "TMDB 泛英语（en）",
    "fanart_english": "Fanart English（en）",
    "tmdb_null": "TMDB 无文字（null）",
}


def normalize_language(value: Any) -> str:
    """统一语言标签大小写、下划线和空值。"""
    if value is None:
        return "null"
    normalized = str(value).strip().lower().replace("_", "-").replace("+", "-")
    return "null" if not normalized or normalized == "00" else normalized


def normalize_image_locale(language: Any, region: Any = None) -> str:
    """把 TMDB 图片的语言和地区字段规范为 ``zh-cn`` 形式。"""
    tag = normalize_language(language)
    if tag == "null":
        return "null"
    if "-" in tag:
        parts = tag.split("-")
        return f"{parts[0]}-{parts[1]}" if len(parts) >= 2 and parts[1] else tag
    country = normalize_language(region)
    if country != "null":
        country = country.split("-")[-1]
        if len(country) == 2 and country.isalpha():
            return f"{tag}-{country}"
    return tag


def is_traditional_locale(locale: str) -> bool:
    """判断是否为需要排除的繁体地区或繁体脚本。"""
    normalized = normalize_language(locale)
    return normalized in {"zh-tw", "zh-hk", "zh-hant"}


def normalize_priority(value: Any) -> List[str]:
    """校验并保留海报优先级数组顺序。"""
    if value is None:
        return DEFAULT_PRIORITY.copy()
    if isinstance(value, str):
        raw = [item.strip() for item in value.replace(",", "\n").splitlines()]
    elif isinstance(value, (list, tuple)):
        raw = [str(item).strip() for item in value]
    else:
        raw = []
    return list(dict.fromkeys(item for item in raw if item in PRIORITY_LABELS))


def priority_preview(value: Any) -> str:
    """生成页面上可读的当前优先级摘要。"""
    priority = normalize_priority(value)
    return " → ".join(PRIORITY_LABELS.get(item, item) for item in priority) or "未启用任何候选层"


def _matches_source_language(locale: str, source_language: str) -> bool:
    source = normalize_language(source_language)
    if not source or source == "null" or locale == "null":
        return False
    if source == "zh":
        return locale == "zh"
    if "-" in source:
        return locale == source
    return locale == source or locale.startswith(f"{source}-")


def _tmdb_match(image: dict, priority_key: str, source_language: str) -> bool:
    locale = normalize_image_locale(image.get("iso_639_1"), image.get("iso_3166_1"))
    # 合集图严格排除繁体地区，不因为用户旧配置仍包含 tw/hk 而选中它们。
    if is_traditional_locale(locale):
        return False
    if priority_key == "tmdb_zh_cn":
        return locale == "zh-cn"
    if priority_key == "tmdb_zh_sg":
        return locale == "zh-sg"
    if priority_key == "tmdb_zh_tw" or priority_key == "tmdb_zh_hk":
        return False
    if priority_key == "tmdb_zh":
        return locale == "zh"
    if priority_key == "tmdb_original":
        return _matches_source_language(locale, source_language)
    if priority_key == "tmdb_en_us":
        return locale == "en-us"
    if priority_key == "tmdb_en":
        return locale == "en"
    if priority_key == "tmdb_null":
        return locale == "null"
    return False


def pick_tmdb_image(
    images: Iterable[dict],
    priority_key: str,
    source_language: str = "",
    image_url: Optional[Callable[[str], str]] = None,
) -> Optional[Dict[str, Any]]:
    """从一个 TMDB 图片类型中选出指定优先级的最佳候选。"""
    candidates = [
        item
        for item in images or []
        if isinstance(item, dict) and _tmdb_match(item, priority_key, source_language)
    ]
    candidates = [item for item in candidates if item.get("file_path")]
    if not candidates:
        return None
    selected = sorted(
        candidates,
        key=lambda item: (
            _safe_float(item.get("vote_average")),
            _safe_int(item.get("vote_count")),
            _safe_int(item.get("width")),
        ),
        reverse=True,
    )[0]
    file_path = str(selected["file_path"])
    url = image_url(file_path) if image_url else file_path
    return {
        "url": url,
        "source": "TMDB",
        "language": normalize_image_locale(
            selected.get("iso_639_1"), selected.get("iso_3166_1")
        ),
        "priority_key": priority_key,
        "priority_label": PRIORITY_LABELS.get(priority_key, priority_key),
    }


def _fanart_group(value: str) -> str:
    language = normalize_language(value)
    if language in {"zh", "zh-cn", "zh-sg", "zh-tw", "zh-hk", "zho", "cmn"}:
        return "chinese"
    if language in {"en", "en-us", "eng"}:
        return "english"
    return ""


def pick_fanart_image(images: Iterable[dict]) -> Optional[Dict[str, Any]]:
    """在 Fanart 同语言候选中按 likes 及分辨率选图。"""
    candidates = [
        item for item in images or [] if isinstance(item, dict) and item.get("url")
    ]
    if not candidates:
        return None
    selected = sorted(
        candidates,
        key=lambda item: (
            _safe_int(item.get("likes")),
            _safe_int(item.get("width")),
            _safe_int(item.get("height")),
        ),
        reverse=True,
    )[0]
    language = normalize_language(selected.get("lang"))
    return {
        "url": str(selected["url"]),
        "source": "Fanart",
        "language": "zh" if _fanart_group(language) == "chinese" else "en",
    }


def _safe_int(value: Any) -> int:
    """兼容 Fanart 返回的字符串、空值和异常数字。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    """兼容 TMDB 返回的字符串、空值和异常评分。"""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def select_collection_images(
    tmdb_images: Optional[dict],
    fanart_images: Optional[dict],
    priority: Any,
    source_language: str = "",
    image_url: Optional[Callable[[str], str]] = None,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """分别为 poster/logo 选择图片，两个类型互不抢占候选。"""
    normalized_priority = normalize_priority(priority)
    tmdb_images = tmdb_images if isinstance(tmdb_images, dict) else {}
    fanart_images = fanart_images if isinstance(fanart_images, dict) else {}

    def choose(kind: str) -> Optional[Dict[str, Any]]:
        for priority_key in normalized_priority:
            if priority_key.startswith("tmdb_"):
                selected = pick_tmdb_image(
                    tmdb_images.get(f"{kind}s") or [],
                    priority_key,
                    source_language=source_language,
                    image_url=image_url,
                )
            else:
                group = "chinese" if priority_key == "fanart_chinese" else "english"
                selected = pick_fanart_image(
                    (fanart_images.get(group) or {}).get(kind) or []
                )
                if selected:
                    selected["priority_key"] = priority_key
                    selected["priority_label"] = PRIORITY_LABELS.get(
                        priority_key, priority_key
                    )
            if selected:
                return selected
        return None

    return {"poster": choose("poster"), "logo": choose("logo")}


def normalize_fanart_payload(payload: Any) -> Dict[str, Dict[str, List[dict]]]:
    """把 Fanart API 的 movieposter/movielogo 字段归一为选择器输入。"""
    groups: Dict[str, Dict[str, List[dict]]] = {
        "chinese": {"poster": [], "logo": []},
        "english": {"poster": [], "logo": []},
    }
    if not isinstance(payload, dict):
        return groups
    key_types = {
        "movieposter": "poster",
        "tvposter": "poster",
        "movielogo": "logo",
        "hdmovielogo": "logo",
        "tvlogo": "logo",
        "hdtvlogo": "logo",
    }
    for key, image_type in key_types.items():
        items = payload.get(key) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            group = _fanart_group(item.get("lang"))
            if group:
                groups[group][image_type].append(item)
    return groups
