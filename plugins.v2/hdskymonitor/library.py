"""媒体库完整性检查：下载前确认媒体是否已完整存在于媒体库，避免重复下载。"""
import logging
from typing import Optional

from app.chain.mediaserver import MediaServerChain
from app.core.context import MediaInfo
from app.schemas.types import MediaType

logger = logging.getLogger(__name__)


def _resolve_media_type(media_type) -> Optional[MediaType]:
    """将识别结果的媒体类型转换为 MediaType 枚举，无法识别时返回 None。"""
    if media_type in (MediaType.TV, "电视剧", "tv", "TV"):
        return MediaType.TV
    if media_type in (MediaType.MOVIE, "电影", "movie", "MOVIE"):
        return MediaType.MOVIE
    return None


def _season_total_episodes(season_info: list, season: int) -> Optional[int]:
    """从识别结果中提取指定季的总集数，找不到时返回 None。"""
    for info in season_info or []:
        if str(info.get("season_number") or info.get("order") or "") == str(season):
            count = info.get("episode_count")
            if count:
                return int(count)
    return None


def check_library_complete(
    tmdb_id: Optional[int],
    media_type=None,
    title: str = "",
    year: Optional[str] = None,
    season: Optional[int] = None,
    season_info: Optional[list] = None,
    number_of_seasons: int = 0,
    number_of_episodes: int = 0,
) -> Optional[bool]:
    """
    检查媒体库中是否已存在完整的媒体。

    :param tmdb_id: TMDB ID
    :param media_type: 识别得到的媒体类型（电影/电视剧）
    :param title: 媒体标题
    :param year: 年份
    :param season: 目标季（电视剧，种子标题带季标记时存在）
    :param season_info: 识别结果的季详情列表（含 episode_count）
    :param number_of_seasons: 剧集总季数
    :param number_of_episodes: 剧集总集数
    :return: True=媒体库已完整存在；False=不存在或不完整；None=无法检查（未配置媒体服务器或信息不足）
    """
    if not tmdb_id or not title:
        return None
    mtype = _resolve_media_type(media_type)
    if not mtype:
        return None
    try:
        # 构造媒体信息并查询媒体服务器
        mediainfo = MediaInfo(
            tmdb_id=int(tmdb_id),
            type=mtype,
            title=title,
            year=year or None,
            season=season,
        )
        exists = MediaServerChain().media_exists(mediainfo=mediainfo)
        if not exists:
            return False
        # 电影存在即完整
        if mtype == MediaType.MOVIE:
            return True
        # 电视剧需要目标季完整存在
        seasons = exists.seasons or {}
        if not seasons:
            return False
        # 确定目标季：种子带季标记时使用该季，否则媒体库中只有一季时使用该季
        target_season = season
        if target_season is None and len(seasons) == 1:
            target_season = next(iter(seasons))
        if target_season is None:
            return False
        existed = seasons.get(target_season) or []
        if not existed:
            return False
        # 目标季总集数：优先使用季详情，单季剧时使用整剧集数兜底
        total = _season_total_episodes(season_info, target_season)
        if not total and int(number_of_seasons or 0) == 1:
            total = int(number_of_episodes or 0)
        if not total:
            # 无法确认总集数，保守处理为不完整，避免误判导致漏下载
            return False
        return len(existed) >= total
    except Exception as exc:
        logger.warning("媒体库完整性检查失败：%s", exc)
        return None
