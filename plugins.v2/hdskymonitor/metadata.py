"""媒体图片查询。"""
from .client import api_get

def get_poster_url(name, year=None):
    """通过 TMDB 搜索获取封面海报 URL (竖版，用于 Telegram)"""
    result = api_get("/api/v1/media/search", {"title": name})
    if not result or not isinstance(result, list) or not result:
        return None
    if year:
        for item in result:
            release_date = item.get("release_date") or ""
            if release_date and release_date[:4] == year:
                poster = item.get("poster_path")
                if poster:
                    return poster if poster.startswith("http") else f"https://image.tmdb.org/t/p/w500{poster}"
    for item in result:
        poster = item.get("poster_path")
        if poster:
            return poster if poster.startswith("http") else f"https://image.tmdb.org/t/p/w500{poster}"
    return None

def get_backdrop_url(name, year=None):
    """通过 TMDB 搜索获取横版背景图 URL (用于微信图文)"""
    result = api_get("/api/v1/media/search", {"title": name})
    if not result or not isinstance(result, list) or not result:
        return None
    if year:
        for item in result:
            release_date = item.get("release_date") or ""
            if release_date and release_date[:4] == year:
                bd = item.get("backdrop_path")
                if bd:
                    return bd if bd.startswith("http") else f"https://image.tmdb.org/t/p/w780{bd}"
    for item in result:
        bd = item.get("backdrop_path")
        if bd:
            return bd if bd.startswith("http") else f"https://image.tmdb.org/t/p/w780{bd}"
    return None
