"""天空监控主流程。"""
import logging
from datetime import datetime
from typing import Callable, List, Optional
from .downloader import download_torrent_direct
from .matcher import is_full_season, is_recent, torrent_id_key
from .metadata import get_backdrop_url, get_poster_url
from .site import search_page
from .state import MonitorState

logger = logging.getLogger(__name__)

def format_size(size_bytes):
    """格式化字节大小。"""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}PB"

def build_torrent_text(item: dict) -> str:
    """构建与旧版本一致的通知正文。"""
    ti=item.get("torrent_info", {}); mi=item.get("meta_info", {})
    title=mi.get("cn_name") or mi.get("name") or ti.get("title", "")
    lines=[f"🎬 {title}", f"💾 {format_size(ti.get('size', 0))}  🌱 {ti.get('seeders', 0)}  ⬇️ {ti.get('grabs', 0)}"]
    if ti.get("pubdate"): lines.append(f"📅 {ti['pubdate']}")
    if ti.get("description"): lines.append(f"\n📝 {ti['description'][:150]}")
    return "\n".join(lines)

class HdskyMonitorRunner:
    """编排天空资源扫描、下载、历史和通知。"""
    def __init__(self, state_path: str, notifier=None, history_callback=None):
        self.state=MonitorState(state_path); self.notifier=notifier; self.history_callback=history_callback

    def run(self, test_mode=False, max_pages=1, days=2, limit=0, save_path=None):
        """执行一次监控并返回成功下载数。"""
        logger.info("天空种子监控启动")
        state=self.state.load(); processed=set(state.get("processed", [])); matched=[]; scanned=0
        for page in range(1, max_pages + 1):
            results=search_page(page)
            if not results: break
            for item in results:
                ti=item.get("torrent_info", {}); key=torrent_id_key(ti.get("page_url", ""))
                if not key or key in processed or not is_recent(ti.get("pubdate", ""), days): continue
                scanned += 1
                if is_full_season(ti.get("title", ""), ti.get("description", "")):
                    matched.append({"item": item, "key": key})
        logger.info("扫描 %s 条, 匹配 %s 条", scanned, len(matched))
        if test_mode:
            for row in matched:
                ti=row["item"]["torrent_info"]; logger.info("  • %s (%s, 🌱%s)", ti.get("title"), format_size(ti.get("size",0)), ti.get("seeders",0))
            return 0
        downloaded=[]
        for row in matched:
            if limit > 0 and len(downloaded) >= limit: break
            item=row["item"]; ti=item["torrent_info"]; mi=item["meta_info"]; name=mi.get("cn_name") or mi.get("name") or ti.get("title", "")
            result=download_torrent_direct(ti.get("enclosure", ""), name, ti.get("description", ""), save_path=save_path or "")
            if result and result.get("success"):
                state.setdefault("processed", []).append(row["key"]); downloaded.append(row); logger.info("✓ %s", name)
            else: logger.warning("✗ %s: %s", name, (result or {}).get("message", "请求失败"))
        if self.history_callback: self.history_callback(matched, downloaded)
        for row in downloaded:
            item=row["item"]; ti=item["torrent_info"]; mi=item["meta_info"]; name=mi.get("cn_name") or mi.get("name") or ""; year=mi.get("year") or ""
            poster=get_poster_url(name, year); backdrop=get_backdrop_url(name, year); text=build_torrent_text(item); full_text="🤖 天空脚本自动下载\n\n"+text
            if self.notifier:
                display=f"{name} ({year})" if year else name
                self.notifier(title=f"🤖 天空自动下载 | {display}", text=full_text, image=poster or backdrop, link=ti.get("page_url"))
        self.state.save(state); logger.info("完成"); return len(downloaded)
