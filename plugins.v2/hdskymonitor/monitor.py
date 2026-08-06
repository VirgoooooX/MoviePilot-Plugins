"""天空监控主流程。"""
import logging
from datetime import datetime
from typing import Callable, List, Optional
from .downloader import download_torrent_direct
from .library import check_library_complete
from .matcher import is_full_season, is_recent, torrent_id_key
from .metadata import get_backdrop_url, recognize_tmdb_media
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
    def __init__(self, state_path: str, notifier=None, history_callback=None, log_callback=None):
        """初始化监控流程及通知、历史和独立日志回调。"""
        self.state=MonitorState(state_path); self.notifier=notifier; self.history_callback=history_callback; self.log_callback=log_callback

    def _log(self, level: str, message: str, *args) -> None:
        """同时写入 MoviePilot 主日志和插件独立日志。"""
        getattr(logger, level)(message, *args)
        if self.log_callback:
            rendered = message % args if args else message
            self.log_callback(level, rendered)

    def run(
        self,
        test_mode=False,
        max_pages=1,
        days=2,
        limit=0,
        save_path="/downloadssd/local/",
        downloader=None,
        check_library=True,
    ):
        """使用指定下载路径和下载器执行一次监控。"""
        self._log("info", "天空种子监控启动")
        state=self.state.load(); processed=set(state.get("processed", [])); matched=[]; skipped=[]; scanned=0
        for page in range(1, max_pages + 1):
            results=search_page(page)
            if not results: break
            for item in results:
                ti=item.get("torrent_info", {}); key=torrent_id_key(ti.get("page_url", ""))
                if not key or key in processed or not is_recent(ti.get("pubdate", ""), days): continue
                scanned += 1
                if is_full_season(ti.get("title", ""), ti.get("description", "")):
                    matched.append({"item": item, "key": key})
        self._log("info", "扫描 %s 条, 匹配 %s 条", scanned, len(matched))
        if test_mode:
            for row in matched:
                ti=row["item"]["torrent_info"]; self._log("info", "  • %s (%s, 🌱%s)", ti.get("title"), format_size(ti.get("size",0)), ti.get("seeders",0))
            return 0
        downloaded=[]
        for row in matched:
            item=row["item"]; ti=item["torrent_info"]; mi=item["meta_info"]
            try:
                recognized = recognize_tmdb_media(ti.get("title", ""), ti.get("description", ""))
            except Exception as exc:
                self._log("warning", "TMDB 识别失败，将使用种子解析名称：%s", exc)
                recognized = {}
            if recognized.get("title"):
                mi["cn_name"] = recognized["title"]
                mi["name"] = recognized["title"]
                mi["year"] = recognized.get("year") or mi.get("year")
                mi["tmdb_id"] = recognized.get("tmdb_id")
                mi["backdrop"] = recognized.get("backdrop")
                mi["poster"] = recognized.get("poster")
                self._log("info", "TMDB 识别：%s (%s)", mi["cn_name"], mi.get("year") or "未知年份")
                # 下载前确认媒体库中是否已存在完整媒体，存在则跳过
                if check_library and recognized.get("tmdb_id"):
                    exists = check_library_complete(
                        tmdb_id=recognized.get("tmdb_id"),
                        media_type=recognized.get("type"),
                        title=recognized.get("title"),
                        year=recognized.get("year"),
                        season=recognized.get("season"),
                        season_info=recognized.get("season_info"),
                        number_of_seasons=recognized.get("number_of_seasons") or 0,
                        number_of_episodes=recognized.get("number_of_episodes") or 0,
                    )
                    if exists is True:
                        self._log("info", "⏭ 媒体库已存在完整媒体，跳过下载：%s (%s)", mi["cn_name"], mi.get("year") or "")
                        state.setdefault("processed", []).append(row["key"])
                        skipped.append(row)
                        continue
                    if exists is None:
                        self._log("warning", "媒体库检查不可用，按原逻辑继续下载：%s", mi["cn_name"])
            if limit > 0 and len(downloaded) >= limit: break
            item=row["item"]; ti=item["torrent_info"]; mi=item["meta_info"]; name=mi.get("cn_name") or mi.get("name") or ti.get("title", "")
            result=download_torrent_direct(
                ti.get("enclosure", ""),
                name,
                ti.get("description", ""),
                save_path=save_path or "/downloadssd/local/",
                downloader=downloader,
            )
            if result and result.get("success"):
                state.setdefault("processed", []).append(row["key"]); downloaded.append(row); self._log("info", "✓ %s", name)
            else: self._log("warning", "✗ %s: %s", name, (result or {}).get("message", "请求失败"))
        if self.history_callback: self.history_callback(matched, downloaded, skipped)
        for row in downloaded:
            item=row["item"]; ti=item["torrent_info"]; mi=item["meta_info"]; name=mi.get("cn_name") or mi.get("name") or ""; year=mi.get("year") or ""
            backdrop=mi.get("backdrop") or get_backdrop_url(name, year); text=build_torrent_text(item)
            if self.notifier:
                display=f"{name} ({year})" if year else name
                self.notifier(title=f"🤖 天空自动下载 | {display}", text=text, image=backdrop, link=ti.get("page_url"))
        self.state.save(state); self._log("info", "完成"); return len(downloaded)
