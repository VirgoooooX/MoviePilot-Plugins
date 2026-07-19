"""MoviePilot 下载任务提交。"""
from .client import api_get, api_post

def download_torrent_direct(enclosure, title, description="", site_name="天空", save_path="/downloadssd"):
    """直接下载种子（构建完整的TorrentInfo对象）"""
    # 获取站点信息
    sites = api_get("/api/v1/site/")
    site_info = None
    if sites:
        for s in sites:
            if s.get("name") == site_name:
                site_info = s
                break
    
    if not site_info:
        return {"success": False, "message": f"未找到站点 {site_name}"}
    
    # 构建TorrentInfo对象
    torrent_in = {
        "title": title,
        "description": description,
        "enclosure": enclosure,
        "site_name": site_name,
        "site_ua": site_info.get("ua", ""),
        "site_cookie": site_info.get("cookie", ""),
        "site_proxy": site_info.get("proxy", 0),
        "site_order": site_info.get("pri", 0),
        "site_downloader": site_info.get("downloader")
    }
    
    result = api_post("/api/v1/download/add", {
        "torrent_in": torrent_in,
        "save_path": save_path
    })
    return result
