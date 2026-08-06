"""通过 MoviePilot/TMDB 识别媒体并选择通知元数据。"""
from typing import Optional

from .client import api_get


def _absolute_image(path: Optional[str], size: str = "original") -> Optional[str]:
    """将 TMDB 图片路径转换为完整地址。"""
    if not path:
        return None
    return path if path.startswith("http") else f"https://image.tmdb.org/t/p/{size}{path}"


def recognize_tmdb_media(title: str, subtitle: str = "") -> dict:
    """结合种子标题和描述识别 TMDB 媒体并返回简体中文元数据。"""
    result = api_get("/api/v1/media/recognize", {"title": title, "subtitle": subtitle})
    if not isinstance(result, dict):
        return {}
    media = result.get("media_info") or {}
    meta = result.get("meta_info") or {}
    if not media:
        return {}
    return {
        "title": media.get("title") or media.get("name") or "",
        "year": str(media.get("year") or meta.get("year") or ""),
        "type": media.get("type") or meta.get("type"),
        "tmdb_id": media.get("tmdb_id") or media.get("tmdbid"),
        "season": media.get("season") or meta.get("begin_season"),
        "season_info": media.get("season_info") or [],
        "number_of_seasons": media.get("number_of_seasons") or 0,
        "number_of_episodes": media.get("number_of_episodes") or 0,
        "poster": _absolute_image(media.get("poster_path")),
        "backdrop": _absolute_image(media.get("backdrop_path")),
    }


def get_poster_url(name: str, year: str = None) -> Optional[str]:
    """通过 TMDB 搜索获取竖版海报地址，供历史卡片使用。"""
    result = api_get("/api/v1/media/search", {"title": name})
    if not isinstance(result, list):
        return None
    candidates = result
    if year:
        exact = [item for item in result if str(item.get("year") or (item.get("release_date") or "")[:4]) == str(year)]
        candidates = exact or result
    for item in candidates:
        poster = _absolute_image(item.get("poster_path"), "w500")
        if poster:
            return poster
    return None


def get_backdrop_url(name: str, year: str = None) -> Optional[str]:
    """通过 TMDB 搜索获取横版背景图地址。"""
    result = api_get("/api/v1/media/search", {"title": name})
    if not isinstance(result, list):
        return None
    candidates = result
    if year:
        exact = [item for item in result if str(item.get("year") or (item.get("release_date") or "")[:4]) == str(year)]
        candidates = exact or result
    for item in candidates:
        backdrop = _absolute_image(item.get("backdrop_path"))
        if backdrop:
            return backdrop
    return None
