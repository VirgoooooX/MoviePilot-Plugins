"""天空资源匹配规则。"""
import re
from datetime import datetime, timedelta

FULL_SEASON_PATTERN = re.compile(r"(全|共)\d+(集|期)|Complete|S\d+(?!\d*E)\b", re.IGNORECASE)
EPISODE_ONLY_PATTERN = re.compile(r"S\d+E\d+", re.IGNORECASE)

def torrent_id_key(page_url: str):
    """从详情链接提取种子 ID。"""
    match = re.search(r"id=(\d+)", page_url or "")
    return match.group(1) if match else None

def is_recent(pubdate: str, days: int) -> bool:
    """判断发布时间是否位于扫描范围内。"""
    if days <= 0 or not pubdate:
        return True
    try:
        return datetime.strptime(pubdate, "%Y-%m-%d %H:%M:%S") >= datetime.now() - timedelta(days=days)
    except ValueError:
        return False

def is_full_season(title: str, description: str) -> bool:
    """判断资源是否为全集而非单集。"""
    if EPISODE_ONLY_PATTERN.search(title or ""):
        return False
    return bool(FULL_SEASON_PATTERN.search(f"{title or ''} {description or ''}"))
