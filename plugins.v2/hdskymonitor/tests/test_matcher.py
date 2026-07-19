"""天空资源匹配规则测试。"""
from plugins.v2.hdskymonitor.matcher import is_full_season, torrent_id_key


def test_extract_torrent_id():
    """应从详情地址提取种子 ID。"""
    assert torrent_id_key("https://hdsky.me/details.php?id=123&hit=1") == "123"


def test_full_season_and_episode():
    """应保留全集并排除单集资源。"""
    assert is_full_season("Demo S01 Complete", "")
    assert is_full_season("Demo S01", "全10集")
    assert not is_full_season("Demo S01E01", "全10集")
