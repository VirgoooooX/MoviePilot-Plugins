import re
import time
import pytz
import json
import queue
import difflib
import threading
from dateutil.parser import isoparse
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.background import BackgroundScheduler

from app.log import logger
from app.core.cache import Cache
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.plugins import _PluginBase
from app.utils.string import StringUtils
from app.modules.douban import DoubanApi
from app.modules.themoviedb import TmdbApi
from app.schemas import WebhookEventInfo, ServiceInfo
from app.schemas.types import EventType, MediaType
from app.helper.mediaserver import MediaServerHelper


class EmbyChineseRoleSync(_PluginBase):
    # 插件名称
    plugin_name = "Emby中文角色同步"
    # 插件描述
    plugin_desc = "同步豆瓣中文演员与角色到电视剧/季/集，支持媒体库白名单过滤与手动精准同步。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/xiaoQQya/MoviePilot-Plugins/refs/heads/main/icons/actor.png"
    # 插件版本
    plugin_version = "1.8.3"
    # 插件作者
    plugin_author = "xiaoQQya, Virgooooox"
    # 作者主页
    author_url = "https://github.com/virgooooox"
    # 插件配置项ID前缀
    plugin_config_prefix = "embychineserolesync_"
    # 加载顺序
    plugin_order = 100
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _clearcache = False
    _onlyonce = False
    _mediaservers = None
    _include_libraries = None
    _num = None
    _cron = None
    _sync_episodes = False
    _refresh_episodes = True
    _overwrite_episode_people = False
    _search_keyword = ""

    # 前端交互脚本：在开启的媒体库内检索匹配（接收当前表单 model，供输入防抖、回车与搜索按钮共用）
    _SEARCH_BODY_JS = """async (model) => {
  try {
    const kw = String(model.search_keyword || '').trim();
    model.search_matches.splice(0, model.search_matches.length);
    model.search_matches_selected.splice(0, model.search_matches_selected.length);
    if (!kw) {
      model.search_feedback = '请输入剧名或 Item ID 后再搜索';
      return;
    }
    model.search_feedback = '正在检索…';
    const res = await window.MoviePilotAPI.get('plugin/EmbyChineseRoleSync/search_media', { params: { keyword: kw } });
    const items = (res && Array.isArray(res.data)) ? res.data : [];
    const list = items.map(function (it) {
      return {
        title: (it.name || '') + '（' + (it.year || '未知年份') + '）[' + (it.type || '') + ' · ' + (it.library || '未知库') + ' · ' + (it.server || '') + ']',
        value: String(it.server) + ':' + String(it.id)
      };
    });
    model.search_matches.splice(0, model.search_matches.length, ...list);
    model.search_feedback = list.length ? ('匹配到 ' + list.length + ' 部作品，勾选后点击【同步所选媒体】') : ('未在开启的媒体库中检索到「' + kw + '」');
  } catch (err) {
    model.search_feedback = '搜索失败：' + ((err && err.message) ? err.message : String(err));
  }
}"""
    # 前端交互脚本：输入框防抖自动检索（显式把当前 model 传给检索函数，避免复用旧缓存）
    _SEARCH_AUTO_JS = """async (event) => {
  model.search_keyword = event;
  if (window.__easTimer) window.clearTimeout(window.__easTimer);
  window.__easTimer = window.setTimeout(async () => {
    await (__SEARCH_BODY__)(model);
  }, 600);
}"""
    # 前端交互脚本：回车立即检索（显式把当前 model 传给检索函数）
    _SEARCH_ENTER_JS = """async (event) => {
  if (event && event.key === 'Enter') {
    await (__SEARCH_BODY__)(model);
  }
}"""
    # 前端交互脚本：搜索按钮立即检索（显式把当前 model 传给检索函数）
    _SEARCH_BTN_JS = """async (event) => {
  await (__SEARCH_BODY__)(model);
}"""
    # 前端交互脚本：同步所选媒体
    _SYNC_BTN_JS = """async (event) => {
  const sel = model.search_matches_selected || [];
  if (!sel.length) {
    model.search_feedback = '请先在匹配结果中勾选要同步的媒体';
    return;
  }
  const items = sel.map(function (v) {
    const s = String(v);
    const idx = s.indexOf(':');
    return idx > 0 ? { server: s.slice(0, idx), item_id: s.slice(idx + 1) } : { server: '', item_id: s };
  });
  model.search_feedback = '正在同步 ' + items.length + ' 部作品，请稍候…';
  try {
    const res = await window.MoviePilotAPI.post('plugin/EmbyChineseRoleSync/sync_media_batch', { items: items });
    model.search_feedback = (res && res.message) ? res.message : '同步完成';
  } catch (err) {
    model.search_feedback = '同步失败：' + ((err && err.message) ? err.message : String(err));
  }
}"""

    _scheduler = None
    _tmdbapi = TmdbApi()
    _doubanapi = DoubanApi()
    _cache = Cache("ttl", 2000, 7 * 24 * 60 * 60)
    _queue = queue.Queue()
    _state_lock = threading.RLock()
    _inflight = set()

    def init_plugin(self, config: Optional[dict] = None):
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._clearcache = config.get("clearcache")
            self._onlyonce = config.get("onlyonce")
            self._mediaservers = config.get("mediaservers") or []
            self._include_libraries = config.get("include_libraries") or []
            self._num = config.get("num")
            self._cron = config.get("cron") or "0 6 * * *"
            self._sync_episodes = bool(config.get("sync_episodes", False))
            self._refresh_episodes = bool(config.get("refresh_episodes", True))
            self._overwrite_episode_people = bool(config.get("overwrite_episode_people", False))
            self._search_keyword = str(config.get("search_keyword") or "").strip()

        if self._clearcache:
            logger.info("Emby 演职人员缓存清除")
            self._cache.clear(self.plugin_config_prefix.rstrip("_"))
            self._clearcache = False

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        self._scheduler.add_job(
            func=self.handle_hook,
            trigger="date",
            run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=1),
            name="Emby 演职人员增强媒体入库处理"
        )
        self._scheduler.start()

        if self._onlyonce:
            logger.info("Emby 演职人员增强服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.run,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=1),
                name="Emby 演职人员增强"
            )
            self._onlyonce = False

        self.update_config({
            "enabled": self._enabled,
            "clearcache": False,
            "onlyonce": False,
            "mediaservers": self._mediaservers,
            "include_libraries": self._include_libraries,
            "num": self._num,
            "cron": self._cron,
            "sync_episodes": self._sync_episodes,
            "refresh_episodes": self._refresh_episodes,
            "overwrite_episode_people": self._overwrite_episode_people,
            "search_keyword": self._search_keyword
        })

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled and self._cron:
            return [{
                "id": "EmbyChineseRoleSync",
                "name": self.plugin_name,
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.run,
                "kwargs": {}
            }]
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册插件 API
        """
        return [
            {
                "path": "/libraries",
                "endpoint": self.api_libraries,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取Emby媒体库列表",
                "description": "获取所有已连接Emby服务器的顶级媒体库列表"
            },
            {
                "path": "/search_media",
                "endpoint": self.api_search_media,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "搜索Emby媒体库中的影视剧",
                "description": "按名称搜索Emby库中的电影和电视剧"
            },
            {
                "path": "/sync_media",
                "endpoint": self.api_sync_media,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "手动同步单部影视剧演职人员",
                "description": "按Emby Item ID单独同步指定影视剧的角色与演职人员"
            },
            {
                "path": "/sync_media_batch",
                "endpoint": self.api_sync_media_batch,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "批量手动同步多部影视剧演职人员",
                "description": "接收设置页勾选的多部影视剧 server/item_id 列表，逐部同步演职人员与中文角色"
            },
            {
                "path": "/sync_media_by_name",
                "endpoint": self.api_sync_media_by_name,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "按输入的名称搜索并立即同步",
                "description": "接收输入的影视剧名称，在Emby库中检索匹配并立刻同步演职人员"
            },
            {
                "path": "/run_now",
                "endpoint": self.api_run_now,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行一次同步",
                "description": "触发后台运行全量同步任务"
            },
            {
                "path": "/clear_cache",
                "endpoint": self.api_clear_cache,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清除去重缓存",
                "description": "清除插件演职人员处理缓存"
            },
            {
                "path": "/clear_history",
                "endpoint": self.api_clear_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空同步历史",
                "description": "清空展示的历史匹配记录"
            }
        ]

    def _get_emby_libraries(self) -> List[Dict[str, str]]:
        """获取已连接 Emby 服务器的所有顶级媒体库选项"""
        libraries = []
        seen = set()
        services = self.service_infos
        if not services:
            return []
        for name, service in services.items():
            url = "[HOST]emby/Users/[USER]/Views?api_key=[APIKEY]"
            res = service.instance.get_data(url=url)
            if res and res.status_code == 200:
                items = res.json().get("Items", [])
                for item in items:
                    lib_name = item.get("Name")
                    if lib_name and lib_name not in seen:
                        seen.add(lib_name)
                        libraries.append({"title": lib_name, "value": lib_name})
        return libraries

    def _get_emby_media_options(self) -> List[Dict[str, str]]:
        """获取所有 Emby 库中具体的电影和电视剧下拉选项（仅包含开启媒体库中的项目）"""
        options = []
        services = self.service_infos
        if not services:
            return []

        for s_name, service in services.items():
            url = "[HOST]emby/Users/[USER]/Items?IncludeItemTypes=Movie,Series&Recursive=true&Fields=ProductionYear,ParentId&Limit=2000&api_key=[APIKEY]"
            res = service.instance.get_data(url=url)
            if res and res.status_code == 200:
                items = res.json().get("Items", [])
                for item in items:
                    if not self._is_library_allowed(service, item):
                        continue
                    name = item.get("Name")
                    item_id = item.get("Id")
                    m_type = "电视剧" if item.get("Type") == "Series" else "电影"
                    year = item.get("ProductionYear") or ""
                    year_str = f"({year})" if year else ""

                    options.append({
                        "title": f"{name} {year_str} [{m_type} · {s_name}]",
                        "value": f"{s_name}:{item_id}"
                    })
        return options

    def _is_library_allowed(self, mediaserver: ServiceInfo, media: dict) -> bool:
        """检查媒体条目是否在允许的媒体库中"""
        if not self._include_libraries:
            return True
        item_id = media.get("Id")
        if not item_id:
            return True
        url = f"[HOST]emby/Items/{item_id}/Ancestors?api_key=[APIKEY]"
        res = mediaserver.instance.get_data(url=url)
        if res and res.status_code == 200:
            ancestors = res.json()
            for anc in ancestors:
                if anc.get("Name") in self._include_libraries:
                    return True
            if media.get("Name") in self._include_libraries:
                return True
            return False
        return True

    def api_libraries(self) -> Dict[str, Any]:
        """API: 获取媒体库列表"""
        return {"success": True, "data": self._get_emby_libraries()}

    def _get_server_web_url(self, service: ServiceInfo) -> str:
        """获取媒体服务器对外可访问的地址（优先播放地址，其次服务地址），用于拼接海报图链接"""
        if not service or not service.config:
            return ""
        conf = service.config.config or {}
        if not isinstance(conf, dict):
            return ""
        return str(conf.get("play_host") or conf.get("host") or "").strip().rstrip("/")

    def _build_search_item(self, service: ServiceInfo, server_name: str, item: dict) -> dict:
        """将 Emby 媒体条目组装为前端搜索下拉选项数据"""
        item_id = item.get("Id")
        library_name = "未知媒体库"
        anc_res = service.instance.get_data(url=f"[HOST]emby/Items/{item_id}/Ancestors?api_key=[APIKEY]")
        if anc_res and anc_res.status_code == 200:
            ancestors = anc_res.json()
            if ancestors:
                # 去掉 root 等聚合根节点，取最后一个有效祖先作为媒体库名称
                library_ancestors = [anc for anc in ancestors if anc.get("Type") != "AggregateFolder"]
                if library_ancestors:
                    library_name = library_ancestors[-1].get("Name") or library_name

        poster_tag = (item.get("ImageTags") or {}).get("Primary")
        poster_url = ""
        server_host = self._get_server_web_url(service)
        if poster_tag and server_host:
            poster_url = f"{server_host}/emby/Items/{item_id}/Images/Primary?tag={poster_tag}&quality=90"

        return {
            "id": item_id,
            "name": item.get("Name"),
            "type": "电视剧" if item.get("Type") == "Series" else "电影",
            "type_raw": item.get("Type"),
            "year": item.get("ProductionYear"),
            "library": library_name,
            "server": server_name,
            "poster": poster_url,
            "overview": item.get("Overview", "")
        }

    def api_search_media(self, keyword: str, server: Optional[str] = None) -> Dict[str, Any]:
        """API: 搜索 Emby 中的电影/电视剧（限制在允许勾选的媒体库内，支持名称或 Item ID）"""
        if not keyword:
            return {"success": False, "message": "请输入搜索关键词"}
        services = self.service_infos
        if not services:
            return {"success": False, "message": "尚未配置已连接的 Emby 媒体服务器"}

        results = []
        seen_ids = set()
        for s_name, service in services.items():
            if server and s_name != server:
                continue

            # 纯数字关键词按 Emby Item ID 精确匹配
            if keyword.isdigit():
                item_info = self._get_item_info(service, int(keyword))
                if item_info and self._is_library_allowed(service, item_info):
                    key = (s_name, item_info.get("Id"))
                    if key not in seen_ids:
                        seen_ids.add(key)
                        results.append(self._build_search_item(service, s_name, item_info))
                continue

            url = f"[HOST]emby/Users/[USER]/Items?SearchTerm={keyword}&IncludeItemTypes=Movie,Series&Recursive=true&Fields=PrimaryImageAspectRatio,ProductionYear,Overview,Path,ParentId,ProviderIds,ImageTags&Limit=30&api_key=[APIKEY]"
            res = service.instance.get_data(url=url)
            if res and res.status_code == 200:
                items = res.json().get("Items", [])
                for item in items:
                    if not self._is_library_allowed(service, item):
                        continue
                    key = (s_name, item.get("Id"))
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    results.append(self._build_search_item(service, s_name, item))
        return {"success": True, "data": results}

    def api_sync_media(self, item_id: str, server: str) -> Dict[str, Any]:
        """API: 按 Emby Item ID 同步指定剧集演职人员（受到允许媒体库白名单限制）"""
        if not item_id or not server:
            return {"success": False, "message": "缺少必要参数 item_id 或 server"}
        service = self.service_infos.get(server) if self.service_infos else None
        if not service:
            return {"success": False, "message": f"未找到已连接的服务器 {server}"}

        item_info = self._get_item_info(service, item_id)
        if not item_info:
            return {"success": False, "message": f"未找到 ID 为 {item_id} 的媒体项目"}

        if not self._is_library_allowed(service, item_info):
            return {"success": False, "message": f"作品 <{item_info.get('Name')}> 不在开启同步的媒体库中，已被排除"}

        try:
            self._handle_single_item(service, item_info)
            return {"success": True, "message": f"<{item_info.get('Name')}> 演职人员角色中文同步成功！"}
        except Exception as e:
            logger.exception(f"手动同步 {item_info.get('Name')} 失败")
            return {"success": False, "message": f"同步失败: {str(e)}"}

    def api_sync_media_batch(self, payload: Dict[str, Any] = None) -> Dict[str, Any]:
        """API: 批量同步设置页勾选的多部影视剧演职人员（受到允许媒体库白名单限制）"""
        items = (payload or {}).get("items") or []
        if not items:
            return {"success": False, "message": "未选择任何媒体"}
        services = self.service_infos
        if not services:
            return {"success": False, "message": "尚未配置已连接的 Emby 媒体服务器"}

        ok_items, fail_items = [], []
        for entry in items:
            server = str((entry or {}).get("server") or "").strip()
            item_id = str((entry or {}).get("item_id") or "").strip()
            if not server or not item_id:
                fail_items.append({"name": "未知媒体", "reason": "缺少 server 或 item_id 参数"})
                continue
            service = services.get(server)
            if not service:
                fail_items.append({"name": item_id, "reason": f"未找到已连接的服务器 {server}"})
                continue
            item_info = self._get_item_info(service, item_id)
            if not item_info:
                fail_items.append({"name": item_id, "reason": "未获取到媒体信息"})
                continue
            if not self._is_library_allowed(service, item_info):
                fail_items.append({"name": item_info.get("Name") or item_id, "reason": "不在允许处理的媒体库中，已排除"})
                continue
            try:
                self._handle_single_item(service, item_info)
                ok_items.append(item_info.get("Name") or item_id)
            except Exception as e:
                logger.exception(f"批量同步 <{item_info.get('Name') or item_id}> 失败")
                fail_items.append({"name": item_info.get("Name") or item_id, "reason": str(e)})

        message = f"🎯 同步完成：成功 {len(ok_items)} 部"
        if fail_items:
            message += f"，失败 {len(fail_items)} 部：{'；'.join([f"{it['name']}（{it['reason']}）" for it in fail_items])}"
        if ok_items:
            message += f"\n成功：{'、'.join(ok_items)}"
        return {
            "success": len(fail_items) == 0,
            "message": message,
            "data": {"success": ok_items, "failed": fail_items}
        }

    def api_sync_media_by_name(self, search_keyword: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """API: 接收输入框中的剧名，在开启的 Emby 库中搜索匹配并立刻同步演职人员"""
        keyword = search_keyword or kwargs.get("search_keyword") or self._search_keyword
        keyword = str(keyword or "").strip()
        if not keyword:
            return {"success": False, "message": "请在输入框中填入影视剧名称或 ID 后再点击同步！"}

        services = self.service_infos
        if not services:
            return {"success": False, "message": "尚未配置已连接的 Emby 媒体服务器"}

        matched_list = []
        for s_name, service in services.items():
            if keyword.isdigit():
                item_info = self._get_item_info(service, int(keyword))
                if item_info and self._is_library_allowed(service, item_info):
                    matched_list.append((s_name, service, {
                        "id": keyword,
                        "name": item_info.get("Name"),
                        "year": item_info.get("ProductionYear", ""),
                        "library": "Emby"
                    }))
            else:
                res = self.api_search_media(keyword=keyword, server=s_name)
                items = res.get("data") or []
                for item in items:
                    matched_list.append((s_name, service, item))

        if not matched_list:
            lib_info = f"在开启的媒体库（{', '.join(self._include_libraries)}）" if self._include_libraries else "在 Emby 库"
            return {"success": False, "message": f"未{lib_info}中检索到匹配 <{keyword}> 的影视作品（不符合白名单库的作品已被自动排除）"}

        s_name, service, best_match = matched_list[0]
        item_id = best_match["id"]
        item_name = best_match["name"]
        year = best_match.get("year", "")
        lib = best_match.get("library", "")

        item_info = self._get_item_info(service, item_id)
        if not item_info:
            return {"success": False, "message": f"获取作品信息失败 (ID: {item_id})"}

        try:
            self._handle_single_item(service, item_info)
            return {
                "success": True,
                "message": f"🎯 已在开启的 [{lib}] 中匹配到 <{item_name}> ({year})，演职人员与中文角色已同步成功！"
            }
        except Exception as e:
            logger.exception(f"精准同步 <{item_name}> 失败")
            return {"success": False, "message": f"匹配到 <{item_name}> 但同步失败: {str(e)}"}

    def api_run_now(self) -> Dict[str, Any]:
        """API: 立即触发一次后台同步"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        if self._scheduler:
            self._scheduler.add_job(
                func=self.run,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=1),
                name="Emby 演职人员增强手动触发"
            )
            return {"success": True, "message": "全量同步任务已提交后台运行"}
        return {"success": False, "message": "调度服务未准备好"}

    def api_clear_cache(self) -> Dict[str, Any]:
        """API: 清除去重缓存"""
        self._cache.clear(self.plugin_config_prefix.rstrip("_"))
        return {"success": True, "message": "已知去重缓存已成功清除"}

    def api_clear_history(self) -> Dict[str, Any]:
        """API: 清空历史记录卡片"""
        self.save_data("history", [])
        return {"success": True, "message": "历史同步卡片已清空"}

    def _record_history(self, mediaserver: ServiceInfo, item_id: str, title: str, media_type_str: str, year: Any, library: str, roles_count: int = 0):
        """记录同步历史到持久化数据中心"""
        history = self.get_data("history") or []
        poster_url = ""
        try:
            item_info = self._get_item_info(mediaserver, item_id)
            if item_info:
                poster_tag = item_info.get("ImageTags", {}).get("Primary")
                server_host = self._get_server_web_url(mediaserver)
                if poster_tag and server_host:
                    poster_url = f"{server_host}/emby/Items/{item_id}/Images/Primary?tag={poster_tag}&quality=80"
        except Exception:
            pass

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "id": str(item_id),
            "title": title,
            "type": media_type_str,
            "year": str(year or ""),
            "time": now_str,
            "library": library or "Emby库",
            "server": mediaserver.name,
            "poster": poster_url,
            "roles_count": roles_count,
            "status": "同步成功"
        }
        history.insert(0, record)
        history = history[:60]
        self.save_data("history", history)

    def _handle_single_item(self, mediaserver: ServiceInfo, item_info: dict):
        """处理单部电影或整部电视剧的手动精准同步"""
        item_type = item_info.get("Type")
        if item_type == "Movie":
            self._handle_media(mediaserver, item_info)
        elif item_type == "Series":
            series_id = item_info.get("Id")
            url = f"[HOST]emby/Users/[USER]/Items?ParentId={series_id}&api_key=[APIKEY]&IncludeItemTypes=Season&Recursive=true"
            res = mediaserver.instance.get_data(url=url)
            seasons = res.json().get("Items", []) if res and res.status_code == 200 else []
            if not seasons:
                pseudo_media = {"Id": series_id, "Type": "Episode", "SeriesId": series_id, "SeriesName": item_info.get("Name"), "Name": item_info.get("Name")}
                self._handle_media(mediaserver, pseudo_media)
            else:
                for season in seasons:
                    season_id = season.get("Id")
                    ep_url = f"[HOST]emby/Users/[USER]/Items?ParentId={season_id}&api_key=[APIKEY]&IncludeItemTypes=Episode&Recursive=true&Limit=1"
                    ep_res = mediaserver.instance.get_data(url=ep_url)
                    episodes = ep_res.json().get("Items", []) if ep_res and ep_res.status_code == 200 else []
                    if episodes:
                        media = episodes[0]
                        self._handle_media(mediaserver, media)
                    else:
                        pseudo_media = {
                            "Id": season_id,
                            "Type": "Episode",
                            "SeriesId": series_id,
                            "SeasonId": season_id,
                            "SeriesName": item_info.get("Name"),
                            "SeasonName": season.get("Name"),
                            "Name": season.get("Name")
                        }
                        self._handle_media(mediaserver, pseudo_media)

    @eventmanager.register(EventType.WebhookMessage)
    def hook(self, event: Event):
        """
        监听媒体入库事件
        """
        if not self._enabled:
            return

        event_info: WebhookEventInfo = event.event_data
        if not event_info:
            return

        if "emby" != event_info.channel:
            return

        if "library.new" != event_info.event:
            return

        mediaserver: ServiceInfo = self.service_infos.get(event_info.server_name)
        if not mediaserver:
            return

        media = (event_info.json_object or {}).get("Item")
        if not isinstance(media, dict) or not media.get("Id"):
            logger.warning("Webhook 缺少有效媒体条目")
            return
        if media.get("Type") not in ("Episode", "Movie"):
            return

        if not self._is_library_allowed(mediaserver, media):
            logger.info(f"<{media.get('Name')}> 不在允许的媒体库列表中，跳过处理")
            return

        self._queue.put((mediaserver, media))

    def handle_hook(self):
        """
        处理媒体入库事件
        """
        logger.info("媒体入库事件 webhook 处理启动")
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    break
                mediaserver, media = item
                self._handle_media(mediaserver, media)
            except Exception:
                logger.exception("处理媒体入库任务异常")
            finally:
                self._queue.task_done()
        logger.info("媒体入库事件 webhook 处理停止")

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        """
        服务信息
        """
        if not self._mediaservers:
            logger.warning("尚未配置媒体服务器，请检查配置")
            return {}

        services = MediaServerHelper().get_services(name_filters=self._mediaservers)
        if not services:
            logger.warning("获取媒体服务器实例失败，请检查配置")
            return {}

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"媒体服务器 {service_name} 未连接，请检查配置")
            else:
                active_services[service_name] = service_info

        if not active_services:
            logger.warning("没有已连接的媒体服务器，请检查配置")
            return {}

        return active_services

    def run(self) -> None:
        if not self.service_infos:
            return

        for name, service in self.service_infos.items():
            logger.info(f"开始获取媒体服务器 {name} 最近 {self._num} 天的媒体数据")
            medias = self._get_latest_medias(service)
            logger.info(f"获取媒体服务器 {name} 最近 {self._num} 天的媒体数据共 {len(medias)} 条")

            filtered_count = 0
            for media in medias:
                if self._is_library_allowed(service, media):
                    self._queue.put((service, media))
                    filtered_count += 1

            logger.info(f"媒体服务器 {name} 过滤后入队处理条数: {filtered_count}")
            self._queue.join()

            logger.info(f"媒体服务器 {name} 演职人员增强完成")

    def _handle_media(self, mediaserver: ServiceInfo, media: dict):
        """统一处理单个媒体事件并确保同一任务释放互斥状态。"""
        task_key = f"{mediaserver.name}:{media.get('SeasonId') or media.get('SeriesId') or media.get('Id')}"
        with self._state_lock:
            if task_key in self._inflight:
                logger.info(f"<{media.get('Name')}> 任务正在处理中，跳过重复触发")
                return
            self._inflight.add(task_key)
        try:
            self._handle_media_impl(mediaserver, media)
        except Exception:
            logger.exception(f"<{media.get('Name')}> 媒体处理失败")
        finally:
            with self._state_lock:
                self._inflight.discard(task_key)

    def _handle_media_impl(self, mediaserver: ServiceInfo, media: dict):
        """执行单个媒体事件的实际处理流程。"""
        media_type = MediaType("电视剧" if media.get("Type") == "Episode" else "电影")
        series_id = media["SeriesId"] if media_type == MediaType.TV else media["Id"]
        season_id = media.get("SeasonId", None)
        series_name = media.get("SeriesName") if media_type == MediaType.TV else media.get("Name")
        season_name = media.get("SeasonName", None)
        media_name = f"{series_name}-{season_name}-{media['Name']}" if media_type == MediaType.TV else f"{series_name}"

        # 刷新媒体元信息
        self._auto_refresh_item(mediaserver, media)

        key = f"{mediaserver.name}:handled_episodes"
        region = self.plugin_config_prefix.rstrip("_")
        episode_id = media.get("Id") if media_type == MediaType.TV else None
        if self._sync_episodes and episode_id and self._cache.exists(key, region):
            handled_episodes = self._cache.get(key, region) or []
            if str(episode_id) in {str(item) for item in handled_episodes}:
                logger.info(f"<{media_name}> 单集已成功处理，跳过重复同步")
                return

        # 获取系列元信息
        series_info = self._get_item_info(mediaserver, series_id)
        if not series_info:
            logger.warning(f"<{series_name}> 获取系列元信息失败，请检查配置")
            return

        # 获取季元信息
        season_info = None
        if media_type == MediaType.TV and season_id:
            season_info = self._get_item_info(mediaserver, season_id)
            if not season_info:
                logger.warning(f"<{series_name}-{season_name}> 获取季元信息失败，请检查配置")
                return

            season_info = self._update_season_credits(mediaserver, series_info, season_info)
            if not season_info:
                return

        # 演职人员角色信息中文
        series_info, season_info = self._update_chinese_role(mediaserver, media_type, series_info, season_info)
        if not series_info:
            return

        # 更新系列演职人员信息
        if media_type == MediaType.TV and season_info:
            series_info = self._update_tv_credits(mediaserver, series_info, season_info)

        roles_synced = len(series_info.get("People") or [])

        # 可选：同步到单集
        if media_type == MediaType.TV and self._sync_episodes:
            sync_result = self._sync_series_episodes(mediaserver, series_info, season_info)
            if sync_result:
                handled_episodes = self._cache.get(key, region) if self._cache.exists(key, region) else []
                handled_episodes = handled_episodes or []
                for synced_id in sync_result.get("succeeded", []):
                    if str(synced_id) not in {str(item) for item in handled_episodes}:
                        handled_episodes.append(synced_id)
                self._cache.set(key, handled_episodes, None, region)

        # 记录到历史匹配与海报面板
        self._record_history(
            mediaserver=mediaserver,
            item_id=series_id,
            title=series_name,
            media_type_str="电视剧" if media_type == MediaType.TV else "电影",
            year=series_info.get("ProductionYear"),
            library=media.get("SeasonName") or "Emby库",
            roles_count=roles_synced
        )

        time.sleep(3)

    def _sync_series_episodes(self, mediaserver: ServiceInfo, series_info: dict, season_info: Optional[dict]):
        """将电视剧或当前季度的中文演职人员同步到单集并按需刷新。"""
        series_id = series_info.get("Id")
        season_id = season_info.get("Id") if season_info else None
        parent_id = season_id or series_id
        url = f"[HOST]emby/Users/[USER]/Items?ParentId={parent_id}&api_key=[APIKEY]&IncludeItemTypes=Episode&Recursive=true&Fields=People,LockedFields,ProviderIds,Overview"
        res = mediaserver.instance.get_data(url=url)
        if not res or res.status_code != 200:
            status = getattr(res, "status_code", "无响应")
            body = getattr(res, "text", "")[:200] if res else ""
            logger.warning(f"<{series_info.get('Name')}> 获取单集列表失败，HTTP {status}：{body}")
            return {"succeeded": [], "failed": []}
        episodes = res.json().get("Items", [])
        logger.info(f"<{series_info.get('Name')}> 开始逐集同步，共 {len(episodes)} 集")
        result = {"succeeded": [], "failed": []}
        for episode in episodes:
            episode_info = self._get_item_info(mediaserver, episode.get("Id"))
            if not episode_info:
                continue
            source_people = season_info.get("People", []) if season_info else series_info.get("People", [])
            current_people = episode_info.get("People") or []
            if self._overwrite_episode_people or not current_people:
                episode_info["People"] = [dict(person) for person in source_people]
            else:
                by_name = {person.get("Name"): person for person in current_people}
                for person in source_people:
                    target = by_name.get(person.get("Name"))
                    if target and person.get("Role") and (self._overwrite_episode_people or not target.get("Role")):
                        target["Role"] = person.get("Role")
                episode_info["People"] = list(by_name.values())
            locked = episode_info.setdefault("LockedFields", [])
            if "Cast" not in locked:
                locked.append("Cast")
            episode_id = episode_info.get("Id")
            if self._update_item_info(mediaserver, episode_id, episode_info):
                result["succeeded"].append(episode_id)
                logger.info(f"<{series_info.get('Name')}> 单集 {episode_info.get('IndexNumber')} 演职人员同步成功，角色数 {len(episode_info.get('People') or [])}")
                if self._refresh_episodes:
                    self._refresh_item_info(mediaserver, episode_id, False, False, recursive=False)
            else:
                result["failed"].append(episode_id)
                logger.warning(f"<{series_info.get('Name')}> 单集 {episode_info.get('IndexNumber')} 演职人员同步失败")
        return result

    def _get_latest_medias(self, mediaserver: ServiceInfo):
        """
        获取最新媒体数据
        """
        url = "[HOST]emby/Users/[USER]/Items?Limit=1000&api_key=[APIKEY]&SortBy=DateCreated,SortName&SortOrder=Descending&IncludeItemTypes=Episode,Movie&Recursive=true&Fields=DateCreated,Overview,PrimaryImageAspectRatio,ProductionYear"
        res = mediaserver.instance.get_data(url=url)
        if res and res.status_code == 200:
            items = res.json().get("Items", [])
            medias = []
            update_date = datetime.now(tz=pytz.utc) - timedelta(days=int(self._num or 3))
            for item in items:
                item_date = item.get("DateCreated")
                item_date = isoparse(item_date)
                if item_date > update_date:
                    medias.append(item)
                else:
                    break
            return medias
        return []

    def _get_item_info(self, mediaserver: ServiceInfo, item_id: int):
        """
        获取单个项目详情
        """
        url = f"[HOST]emby/Users/[USER]/Items/{item_id}?X-Emby-Token=[APIKEY]&Fields=ChannelMappingInfo&ExcludeFields=Chapters,MediaSources,MediaStreams,Subviews"
        res = mediaserver.instance.get_data(url=url)
        if res and res.status_code == 200:
            return res.json()
        return None

    def _auto_refresh_item(self, mediaserver: ServiceInfo, media: dict):
        """
        自动刷新单个项目信息
        """
        item_id = media["Id"]
        type = MediaType("电视剧" if media.get("Type") == "Episode" else "电影")
        series_name = media.get("SeriesName") if type == MediaType.TV else media.get("Name")
        season_name = media.get("SeasonName", None)
        episode_name = media["Name"]
        media_name = f"{series_name}-{season_name}-{episode_name}" if type == MediaType.TV else f"{series_name}-{episode_name}"
        overview = media.get("Overview")
        image = media.get("ImageTags", {}).get("Primary")

        pattern = re.compile(r'第\s*([0-9]|[十|一|二|三|四|五|六|七|八|九|零])+\s*集')
        refresh_meta = bool(pattern.search(episode_name)) or not overview or not StringUtils.is_chinese(episode_name) or not StringUtils.is_chinese(overview)
        refresh_image = not image

        if refresh_meta or refresh_image:
            if self._refresh_item_info(mediaserver, item_id, refresh_meta, refresh_image):
                logger.info(f"<{media_name}> 媒体元信息刷新成功")
            else:
                logger.warning(f"<{media_name}> 媒体元信息刷新失败，请检查配置")
        else:
            logger.info(f"<{media_name}> 媒体元信息无需刷新")

    def _refresh_item_info(self, mediaserver: ServiceInfo, item_id: int, refresh_meta: bool = True, refresh_image: bool = True, recursive: bool = True):
        """
        刷新单个项目信息
        """
        url = f"[HOST]emby/Items/{item_id}/Refresh?Recursive={'true' if recursive else 'false'}&MetadataRefreshMode=FullRefresh&ImageRefreshMode={'FullRefresh' if refresh_image else 'Default'}&ReplaceAllMetadata={refresh_meta}&ReplaceAllImages={refresh_image}&ReplaceThumbnailImages=false&api_key=[APIKEY]"
        res = mediaserver.instance.post_data(url=url)
        if res and res.status_code in [200, 204]:
            return True
        return False

    def _update_season_credits(self, mediaserver: ServiceInfo, series_info: dict, season_info: dict):
        """
        更新季演职人员
        """
        item_id = season_info["Id"]
        series_name = series_info["Name"]
        season_name = season_info["Name"]
        media_name = f"{series_name}-{season_name}"
        if len(season_info.get("People") or []) > 0:
            logger.info(f"<{media_name}> 季演职人员已存在，跳过更新演职人员")
            return season_info

        tmdb_id = series_info.get("ProviderIds", {}).get("Tmdb")
        season = season_info.get("IndexNumber")
        if not tmdb_id:
            logger.warning(f"<{media_name}> 媒体未获取到 TMDB ID，跳过更新演职人员")
            return None
        credits = self._tmdbapi.season_obj.credits(tv_id=tmdb_id, season_num=season)
        if not credits or len(credits.get("cast", [])) == 0:
            logger.warning(f"<{media_name}> 媒体未找到季演职人员信息，跳过更新演职人员")
            return None

        peoples = []
        for cast in credits.get("cast", []):
            people = {"Name": cast.get("name"), "Role": cast.get("character")}
            if cast.get("known_for_department") == "Acting":
                people["Type"] = "Actor"
            elif cast.get("known_for_department") == "Directing":
                people["Type"] = "Director"
            elif cast.get("known_for_department") == "Writing":
                people["Type"] = "Writer"
            else:
                continue
            peoples.append(people)
        season_info["People"] = peoples
        if "Cast" not in season_info.setdefault("LockedFields", []):
            season_info["LockedFields"].append("Cast")

        if self._update_item_info(mediaserver, item_id, season_info):
            logger.info(f"<{media_name}> 季演职人员信息更新成功")
        else:
            logger.warning(f"<{media_name}> 季演职人员信息更新失败")
            return None

        # 刮削演职人员信息
        casts = {cast.get("name"): cast.get("id") for cast in credits.get("cast", [])}
        updated_season_info = self._get_item_info(mediaserver, item_id)
        if not updated_season_info:
            logger.warning(f"<{media_name}> 季演职人员信息刷新失败")
            return None
        peoples = updated_season_info.get("People", [])
        for people in peoples:
            people_id = people.get("Id", None)
            people_name = people.get("Name", None)
            people_info = self._get_item_info(mediaserver, people_id)
            if not people_info:
                logger.warning(f"<{media_name}> 季演职人员 {people_name} 信息刷新失败")
                continue

            people_tmdb_id = people_info.get("ProviderIds", {}).get("Tmdb")
            people_overview = people_info.get("Overview")
            people_image = people_info.get("ImageTags", {}).get("Primary")
            if not people_overview or not people_image:
                if not people_tmdb_id:
                    people_info.setdefault("ProviderIds", {})["Tmdb"] = casts.get(people_name)
                    self._update_item_info(mediaserver, people_id, people_info)
                if self._refresh_item_info(mediaserver, people_id, not people_overview, not people_image):
                    logger.info(f"<{media_name}> 季演职人员 <{people_name}> 信息刷新成功")
                else:
                    logger.warning(f"<{media_name}> 季演职人员 <{people_name}> 信息刷新失败")

        return updated_season_info

    def _update_tv_credits(self, mediaserver: ServiceInfo, series_info: dict, season_info: dict):
        """
        更新系列演职人员
        """
        item_id = series_info["Id"]
        series_name = series_info["Name"]

        series_peoples = {people["Name"]: people for people in series_info.get("People") or []}
        season_peoples = {people["Name"]: people for people in season_info.get("People") or []}
        updated_series_peoples = []
        for series_people in series_peoples.values():
            series_people["Role"] = season_peoples.get(
                series_people["Name"], {}).get("Role", series_people.get("Role"))
            updated_series_peoples.append(series_people)
        for season_people in season_peoples.values():
            if season_people["Name"] not in series_peoples:
                updated_series_peoples.append(season_people)

        series_info["People"] = updated_series_peoples
        if "Cast" not in series_info.setdefault("LockedFields", []):
            series_info["LockedFields"].append("Cast")
        if self._update_item_info(mediaserver, item_id, series_info):
            logger.info(f"<{series_name}> 系列演职人员信息更新成功")
            return series_info
        else:
            logger.warning(f"<{series_name}> 系列演职人员信息更新失败")
            return None

    def _update_item_info(self, mediaserver: ServiceInfo, item_id: int, item_info: dict):
        """
        更新媒体信息
        """
        url = f"[HOST]emby/Items/{item_id}?reqformat=json&api_key=[APIKEY]"
        headers = {"Content-Type": "application/json"}
        res = mediaserver.instance.post_data(url=url, data=json.dumps(item_info), headers=headers)
        if res and res.status_code in [200, 204]:
            return True
        return False

    def _update_chinese_role(self, mediaserver: ServiceInfo, media_type: MediaType, series_info: dict, season_info: Optional[dict]):
        """
        更新演职人员角色中文
        """
        if media_type == MediaType.TV and season_info:
            peoples = season_info.get("People") or []
            media_name = f"{series_info.get('Name')}-{season_info.get('Name')}"
        else:
            peoples = series_info.get("People") or []
            media_name = series_info.get("Name")

        douban_info = self._get_douban_info(media_type, series_info, season_info)
        if not douban_info:
            logger.warning(f"<{media_name}> 获取豆瓣媒体信息失败，请检查配置")
            return None, None
        douban_peoples = {}
        for people in (douban_info.get("actors", [])):
            douban_peoples[people["name"]] = people
            if people.get("latin_name"):
                douban_peoples[people["latin_name"]] = people
        for people in peoples:
            original_role = people.get("Role") or ""
            if not StringUtils.is_chinese(people.get("Name")) or not StringUtils.is_chinese(people.get("Role")):
                if people.get("Name") in douban_peoples:
                    douban_person = douban_peoples[people["Name"]]
                    douban_role = douban_person.get("character") or ""
                    people["Name"] = douban_person["name"]
                    if douban_role.strip() != "演员":
                        people["Role"] = douban_role
                    else:
                        people["Role"] = original_role
                else:
                    people_info = self._get_item_info(mediaserver, people.get("Id"))
                    if people_info and people_info.get("ProviderIds", {}).get("Tmdb"):
                        people_tmdb_info = self._tmdbapi.get_person_detail(
                            people_info.get("ProviderIds", {}).get("Tmdb"))
                        also_known_as = people_tmdb_info.get("also_known_as", []) if people_tmdb_info else []
                        for name in also_known_as:
                            if name in douban_peoples:
                                douban_person = douban_peoples[name]
                                douban_role = douban_person.get("character") or ""
                                people["Name"] = douban_person["name"]
                                if douban_role.strip() != "演员":
                                    people["Role"] = douban_role
                                else:
                                    people["Role"] = original_role
                                break
                    else:
                        logger.warning(f"人员 <{people.get('Name')}> 未获取到 tmdbid，跳过更新演职人员角色中文")
            role = people.get("Role", "")
            role = re.sub(r"饰\s+", "", role)
            role = re.sub(r"饰演\s+", "", role)
            role = re.sub(r"配\s+", "（配音）", role)
            role = re.sub(r"配音\s+", "（配音）", role)
            role = re.sub(r"演员", "", role)
            role = re.sub(r"自己", "", role)
            role = re.sub(r"\s*[（(]?\s*\bvoice\b\s*[）)]?\s*", "（配音）", role, flags=re.IGNORECASE)
            role = re.sub(r"\s*[（(]?\s*\bdirector\b\s*[）)]?\s*", "（导演）", role, flags=re.IGNORECASE)
            people["Role"] = role

        if media_type == MediaType.TV and season_info:
            if "Cast" not in season_info.setdefault("LockedFields", []):
                season_info["LockedFields"].append("Cast")
            updated = self._update_item_info(mediaserver, season_info["Id"], season_info)
        else:
            if "Cast" not in series_info.setdefault("LockedFields", []):
                series_info["LockedFields"].append("Cast")
            updated = self._update_item_info(mediaserver, series_info["Id"], series_info)
        if updated:
            logger.info(f"<{media_name}> 媒体演职人员角色中文更新成功")
            return series_info, season_info
        else:
            logger.warning(f"<{media_name}> 媒体演职人员角色中文更新失败")
            return None, None

    def _get_douban_info(self, media_type: MediaType, series_info: dict, season_info: Optional[dict]):
        """
        匹配豆瓣媒体信息（已增加零崩溃防御）
        """
        series_name = series_info.get("Name") or ""
        season_name = season_info.get("Name") if season_info else ""

        premieredate = (season_info.get("PremiereDate") if season_info else None) or series_info.get("PremiereDate")
        if premieredate and len(premieredate) >= 4:
            year = premieredate[:4]
        else:
            production_year = (season_info.get("ProductionYear") if season_info else None) or series_info.get("ProductionYear")
            year = str(production_year) if production_year else ""

        result = self._doubanapi.search(series_name)
        if not result or not result.get("items"):
            return None

        douban_id = None
        for item in result["items"]:
            if item.get("type_name") != media_type.value:
                continue
            target = item.get("target") or {}
            if year and target.get("year") and str(target.get("year")) != str(year):
                continue

            item_name = target.get("title", "")
            score_series = self.sequence_matcher(item_name, series_name)
            score_season = self.sequence_matcher(item_name, season_name)
            score_all = self.sequence_matcher(item_name, series_name + season_name)
            score = max(score_series, score_season, score_all)
            if score < 0.8:
                continue

            douban_id = target.get("id")
            break

        if not douban_id:
            return None

        douban_info = self.chain.douban_info(douban_id, media_type)
        if not douban_info:
            return None
        return douban_info

    @staticmethod
    def sequence_matcher(s1: str, s2: str) -> float:
        def normalize(text):
            if text is None:
                return ""

            text = text.lower()
            text = text.replace(" ", "")

            zh_num_map = {
                "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10
            }
            for k, v in zh_num_map.items():
                text = text.replace(k, str(v))
            return text

        return difflib.SequenceMatcher(None, normalize(s1), normalize(s2)).ratio()

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装重构后的响应式插件配置表单
        """
        section_title = lambda icon, title, subtitle: {
            "component": "div",
            "props": {"class": "d-flex align-start ga-3 mb-4"},
            "content": [
                {"component": "VAvatar", "props": {"color": "primary", "variant": "tonal", "size": "40"}, "content": [{"component": "VIcon", "props": {"icon": icon}}]},
                {"component": "div", "content": [
                    {"component": "div", "props": {"class": "text-subtitle-1 font-weight-bold"}, "text": title},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": subtitle}
                ]}
            ]
        }

        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'div',
                        'props': {'class': 'pa-1 pa-sm-3'},
                        'content': [
                            {
                                'component': 'VRow',
                                'props': {'dense': True, 'class': 'mb-4'},
                                'content': [
                                    {
                                        'component': 'VCol',
                                        'props': {'cols': 12, 'md': 6},
                                        'content': [
                                            {
                                                'component': 'VCard',
                                                'props': {'variant': 'outlined', 'class': 'h-100 rounded-lg'},
                                                'content': [
                                                    {
                                                        'component': 'VCardText',
                                                        'content': [
                                                            section_title("mdi-power", "运行与控制", "管理插件基础开关与全量运行策略"),
                                                            {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件', 'color': 'primary', 'inset': True, 'class': 'mb-2'}},
                                                            {'component': 'VSwitch', 'props': {'model': 'clearcache', 'label': '清除缓存后运行', 'color': 'primary', 'inset': True, 'class': 'mb-2'}},
                                                            {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即全量运行一次', 'color': 'primary', 'inset': True}}
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {'cols': 12, 'md': 6},
                                        'content': [
                                            {
                                                'component': 'VCard',
                                                'props': {'variant': 'outlined', 'class': 'h-100 rounded-lg'},
                                                'content': [
                                                    {
                                                        'component': 'VCardText',
                                                        'content': [
                                                            section_title("mdi-tune-vertical", "同步与刷新策略", "控制演职人员及单集细节刷新"),
                                                            {'component': 'VSwitch', 'props': {'model': 'sync_episodes', 'label': '同步每集演职人员', 'color': 'primary', 'inset': True, 'class': 'mb-2'}},
                                                            {'component': 'VSwitch', 'props': {'model': 'refresh_episodes', 'label': '同步后刷新单集', 'color': 'primary', 'inset': True, 'class': 'mb-2'}},
                                                            {'component': 'VSwitch', 'props': {'model': 'overwrite_episode_people', 'label': '覆盖单集已有演职人员', 'color': 'primary', 'inset': True}}
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'VRow',
                                'props': {'dense': True, 'class': 'mb-4'},
                                'content': [
                                    {
                                        'component': 'VCol',
                                        'props': {'cols': 12, 'md': 6},
                                        'content': [
                                            {
                                                'component': 'VCard',
                                                'props': {'variant': 'outlined', 'class': 'h-100 rounded-lg'},
                                                'content': [
                                                    {
                                                        'component': 'VCardText',
                                                        'content': [
                                                            section_title("mdi-server-network", "作用服务器与媒体库过滤", "选择生效的 Emby 实例并白名单过滤库"),
                                                            {
                                                                'component': 'VSelect',
                                                                'props': {
                                                                    'multiple': True,
                                                                    'chips': True,
                                                                    'clearable': True,
                                                                    'model': 'mediaservers',
                                                                    'label': '选择媒体服务器',
                                                                    'variant': 'outlined',
                                                                    'density': 'comfortable',
                                                                    'class': 'mb-3',
                                                                    'items': [{"title": config.name, "value": config.name}
                                                                              for config in MediaServerHelper().get_configs().values()
                                                                              if config.type == "emby"]
                                                                }
                                                            },
                                                            {
                                                                'component': 'VSelect',
                                                                'props': {
                                                                    'multiple': True,
                                                                    'chips': True,
                                                                    'clearable': True,
                                                                    'model': 'include_libraries',
                                                                    'label': '允许处理的媒体库（留空默认处理全部库）',
                                                                    'variant': 'outlined',
                                                                    'density': 'comfortable',
                                                                    'items': self._get_emby_libraries()
                                                                }
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {'cols': 12, 'md': 6},
                                        'content': [
                                            {
                                                'component': 'VCard',
                                                'props': {'variant': 'outlined', 'class': 'h-100 rounded-lg'},
                                                'content': [
                                                    {
                                                        'component': 'VCardText',
                                                        'content': [
                                                            section_title("mdi-clock-outline", "定时周期与入库时间窗", "管理全量更新的范围和调度"),
                                                            {
                                                                'component': 'VTextField',
                                                                'props': {
                                                                    'model': 'num',
                                                                    'label': '最新入库天数（天）',
                                                                    'placeholder': '365',
                                                                    'variant': 'outlined',
                                                                    'density': 'comfortable',
                                                                    'class': 'mb-3'
                                                                }
                                                            },
                                                            {
                                                                'component': 'VCronField',
                                                                'props': {
                                                                    'model': 'cron',
                                                                    'label': 'Cron 定时周期（默认每日 6 点）',
                                                                    'placeholder': '0 6 * * *',
                                                                    'variant': 'outlined',
                                                                    'density': 'comfortable'
                                                                }
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'VCard',
                                'props': {'variant': 'outlined', 'color': 'primary', 'class': 'rounded-lg mb-2'},
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'content': [
                                            section_title("mdi-movie-search-outline", "🎯 指定影视剧精准手动同步", "输入名称自动检索匹配，勾选后一键同步所选媒体"),
                                            {
                                                'component': 'VAlert',
                                                'props': {
                                                    'type': 'info',
                                                    'variant': 'tonal',
                                                    'class': 'mb-4',
                                                    'text': f"安全检索提醒：检索与同步范围严格限制在上面选择的开启媒体库（当前：{', '.join(self._include_libraries) if self._include_libraries else '全部媒体库'}）。不在白名单列表中的综艺/动画等作品将被自动屏蔽。"
                                                }
                                            },
                                            {
                                                'component': 'VRow',
                                                'props': {'dense': True, 'class': 'align-center mb-2'},
                                                'content': [
                                                    {
                                                        'component': 'VCol',
                                                        'props': {'cols': 12, 'md': 8},
                                                        'content': [
                                                            {
                                                                'component': 'VTextField',
                                                                'props': {
                                                                    'model': 'search_keyword',
                                                                    'label': '输入剧名（或 Item ID），自动检索匹配',
                                                                    'placeholder': '例如：百年孤独',
                                                                    'prepend-inner-icon': 'mdi-magnify',
                                                                    'clearable': True,
                                                                    'variant': 'outlined',
                                                                    'density': 'comfortable',
                                                                    'hide-details': True,
                                                                    'onUpdate:modelValue': self._SEARCH_AUTO_JS.replace("__SEARCH_BODY__", self._SEARCH_BODY_JS),
                                                                    'onKeyup': self._SEARCH_ENTER_JS.replace("__SEARCH_BODY__", self._SEARCH_BODY_JS)
                                                                }
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        'component': 'VCol',
                                                        'props': {'cols': 12, 'md': 4},
                                                        'content': [
                                                            {
                                                                'component': 'VBtn',
                                                                'props': {
                                                                    'color': 'primary',
                                                                    'variant': 'tonal',
                                                                    'block': True,
                                                                    'height': '44',
                                                                    'prepend-icon': 'mdi-magnify',
                                                                    'onClick': self._SEARCH_BTN_JS.replace("__SEARCH_BODY__", self._SEARCH_BODY_JS)
                                                                },
                                                                'text': '搜索匹配'
                                                            }
                                                        ]
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VRow',
                                                'props': {'dense': True, 'class': 'align-center mb-2'},
                                                'content': [
                                                    {
                                                        'component': 'VCol',
                                                        'props': {'cols': 12, 'md': 9},
                                                        'content': [
                                                            {
                                                                'component': 'VSelect',
                                                                'props': {
                                                                    'model': 'search_matches_selected',
                                                                    'multiple': True,
                                                                    'chips': True,
                                                                    'clearable': True,
                                                                    'items': 'search_matches',
                                                                    'item-title': 'title',
                                                                    'item-value': 'value',
                                                                    'label': '匹配结果（可多选勾选）',
                                                                    'placeholder': '输入关键词后自动列出匹配结果',
                                                                    'no-data-text': '暂无匹配结果，请先在上方输入关键词搜索',
                                                                    'variant': 'outlined',
                                                                    'density': 'comfortable',
                                                                    'hide-details': True
                                                                }
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        'component': 'VCol',
                                                        'props': {'cols': 12, 'md': 3},
                                                        'content': [
                                                            {
                                                                'component': 'VBtn',
                                                                'props': {
                                                                    'color': 'success',
                                                                    'variant': 'elevated',
                                                                    'block': True,
                                                                    'height': '44',
                                                                    'prepend-icon': 'mdi-sync',
                                                                    'onClick': self._SYNC_BTN_JS
                                                                },
                                                                'text': '同步所选媒体'
                                                            }
                                                        ]
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VTextField',
                                                'props': {
                                                    'model': 'search_feedback',
                                                    'label': '操作状态',
                                                    'readonly': True,
                                                    'variant': 'plain',
                                                    'density': 'compact',
                                                    'prepend-inner-icon': 'mdi-information-outline'
                                                }
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "clearcache": False,
            "onlyonce": False,
            "mediaservers": [],
            "include_libraries": [],
            "num": 3,
            "cron": "0 6 * * *",
            "sync_episodes": False,
            "refresh_episodes": True,
            "overwrite_episode_people": False,
            "search_keyword": "",
            "search_matches": [],
            "search_matches_selected": [],
            "search_feedback": ""
        }

    def _build_poster_card(self, item: dict) -> dict:
        """为单部影视构建带海报壁纸的匹配卡片"""
        poster = item.get("poster")
        title = item.get("title", "未知标题")
        media_type = item.get("type", "媒体")
        year = item.get("year", "")
        sync_time = item.get("time", "")
        library = item.get("library", "")
        roles = item.get("roles_count", 0)

        card_content = []
        if poster:
            card_content.append({
                "component": "VImg",
                "props": {
                    "src": poster,
                    "height": "200",
                    "cover": True,
                    "class": "rounded-t-lg bg-surface-variant"
                }
            })
        else:
            card_content.append({
                "component": "div",
                "props": {
                    "class": "d-flex align-center justify-center rounded-t-lg bg-surface-variant",
                    "style": "height:120px;"
                },
                "content": [
                    {"component": "VIcon", "props": {"icon": "mdi-movie-open-outline", "size": "48", "color": "grey"}}
                ]
            })

        card_content.append({
            "component": "VCardText",
            "props": {"class": "pa-3"},
            "content": [
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center justify-space-between mb-1"},
                    "content": [
                        {"component": "span", "props": {"class": "text-subtitle-2 font-weight-bold text-truncate"}, "text": title},
                        {"component": "VChip", "props": {"size": "x-small", "color": "primary", "variant": "tonal"}, "text": media_type}
                    ]
                },
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center justify-space-between text-caption text-medium-emphasis mb-2"},
                    "content": [
                        {"component": "span", "text": f"年份: {year}" if year else "年份: 未知"},
                        {"component": "span", "text": library}
                    ]
                },
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center justify-space-between text-caption"},
                    "content": [
                        {"component": "VChip", "props": {"size": "x-small", "color": "success", "variant": "flat"}, "text": f"已更新 {roles} 演职员"},
                        {"component": "span", "props": {"class": "text-medium-emphasis"}, "text": sync_time[-8:] if len(sync_time) >= 8 else sync_time}
                    ]
                }
            ]
        })

        return {
            "component": "VCol",
            "props": {"cols": "12", "sm": "6", "md": "4", "lg": "3"},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "h-100 rounded-lg border"},
                    "content": card_content
                }
            ]
        }

    def get_page(self) -> Optional[List[dict]]:
        """
        拼装插件详情页面：提供高级海报墙、数据仪表盘及快捷控制面板
        """
        history = self.get_data("history") or []
        today_date_str = datetime.now().strftime("%Y-%m-%d")

        today_count = sum(1 for item in history if item.get("time", "").startswith(today_date_str))
        total_count = len(history)
        allowed_libs_count = len(self._include_libraries) if self._include_libraries else "全库"

        stats = [
            ("插件状态", "已启用" if self._enabled else "已停用", "mdi-power", "success" if self._enabled else "grey"),
            ("今日处理", f"{today_count} 部影视", "mdi-calendar-today", "info"),
            ("累计同步历史", f"{total_count} 条记录", "mdi-history", "primary"),
            ("生效媒体库", f"{allowed_libs_count}", "mdi-folder-multiple-outline", "warning")
        ]

        stat_cards = [
            {
                "component": "VCol",
                "props": {"cols": "6", "md": "3"},
                "content": [
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "color": color, "class": "h-100"},
                        "content": [
                            {
                                "component": "VCardText",
                                "props": {"class": "pa-4"},
                                "content": [
                                    {"component": "VIcon", "props": {"icon": icon, "size": "24", "class": "mb-2"}},
                                    {"component": "div", "props": {"class": "text-caption"}, "text": label},
                                    {"component": "div", "props": {"class": "text-body-1 font-weight-bold text-truncate"}, "text": value}
                                ]
                            }
                        ]
                    }
                ]
            }
            for label, value, icon, color in stats
        ]

        actions = [
            ("立即全量同步", "mdi-play", "primary", "run_now"),
            ("清除去重缓存", "mdi-refresh", "warning", "clear_cache"),
            ("清空历史记录", "mdi-delete-sweep", "error", "clear_history")
        ]

        action_buttons = [
            {
                "component": "VBtn",
                "props": {"color": color, "variant": "tonal", "prepend-icon": icon, "class": "flex-grow-1"},
                "events": {"click": {"api": f"plugin/EmbyChineseRoleSync/{api_path}", "method": "POST"}},
                "text": label
            }
            for label, icon, color, api_path in actions
        ]

        history_cards = [self._build_poster_card(item) for item in history[:24]]

        return [
            {
                "component": "div",
                "props": {"class": "pa-2 pa-sm-4"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "d-flex flex-column flex-sm-row align-sm-center justify-space-between ga-3 mb-5"},
                        "content": [
                            {
                                "component": "div",
                                "content": [
                                    {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": "Emby 角色同步增强（本地增强版）"},
                                    {"component": "div", "props": {"class": "text-body-2 text-medium-emphasis mt-1"}, "text": "自动同步豆瓣演职人员与中文角色，支持指定媒体库过滤及精准同步 API。"}
                                ]
                            },
                            {
                                "component": "VChip",
                                "props": {"color": "success" if self._enabled else "grey", "variant": "tonal", "prepend-icon": "mdi-account-sync"},
                                "text": "实时监控中" if self._enabled else "已停用"
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "props": {"dense": True, "class": "mb-4"},
                        "content": stat_cards
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "outlined", "class": "mb-5 rounded-lg"},
                        "content": [
                            {
                                "component": "VCardText",
                                "props": {"class": "d-flex flex-wrap align-center ga-2"},
                                "content": action_buttons + [
                                    {"component": "VSpacer"},
                                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": f"定时: {self._cron or '0 6 * * *'} · 最新入库: {self._num or 3} 天"}
                                ]
                            }
                        ]
                    },
                    {
                        "component": "div",
                        "props": {"class": "d-flex align-center justify-space-between mb-3"},
                        "content": [
                            {"component": "div", "props": {"class": "text-h6 font-weight-bold"}, "text": "🖼️ 演职人员增强同步海报墙"},
                            {"component": "VChip", "props": {"size": "small", "variant": "tonal"}, "text": f"显示最新 {min(len(history), 24)} / {len(history)} 条"}
                        ]
                    },
                    (
                        {
                            "component": "VAlert",
                            "props": {
                                "type": "info",
                                "variant": "tonal",
                                "class": "mb-5",
                                "text": "暂无同步记录。媒体入库或点击【立即全量同步】后，已增强的影视剧海报封面和同步数据会呈现在这里。"
                            }
                        }
                        if not history_cards else
                        {
                            "component": "VRow",
                            "props": {"dense": True, "class": "mb-5"},
                            "content": history_cards
                        }
                    )
                ]
            }
        ]

    def stop_service(self):
        """
        退出插件
        """
        if self._scheduler and self._scheduler.running:
            self._queue.put(None)
            self._scheduler.shutdown()

        self._queue = queue.Queue()
        with self._state_lock:
            self._inflight.clear()
        self._scheduler = None
