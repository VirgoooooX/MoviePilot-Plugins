"""Emby 媒体图片管理插件的队列与安全逻辑测试。"""

from __future__ import annotations

import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path


def _module(name: str, package: bool = False) -> types.ModuleType:
    module = types.ModuleType(name)
    if package:
        module.__path__ = []
    sys.modules[name] = module
    return module


def _load_plugin_module():
    """在最小 MoviePilot 桩上加载插件模块。"""
    prefixes = ("app", "apscheduler")
    snapshots = {
        name: module
        for name, module in sys.modules.items()
        if name == prefixes[0]
        or name.startswith(f"{prefixes[0]}.")
        or name == prefixes[1]
        or name.startswith(f"{prefixes[1]}.")
    }
    app = _module("app", True)
    schemas = _module("app.schemas", True)
    chain = _module("app.chain", True)
    chain_media = _module("app.chain.media")
    core = _module("app.core", True)
    config = _module("app.core.config")
    event = _module("app.core.event")
    helper = _module("app.helper", True)
    mediaserver = _module("app.helper.mediaserver")
    log = _module("app.log")
    plugins = _module("app.plugins")
    schema_types = _module("app.schemas.types")
    utils = _module("app.utils", True)
    system = _module("app.utils.system")
    apscheduler = _module("apscheduler", True)
    triggers = _module("apscheduler.triggers", True)
    cron = _module("apscheduler.triggers.cron")

    class CronTrigger:
        @classmethod
        def from_crontab(cls, value):
            if len(str(value).split()) != 5:
                raise ValueError("invalid cron")
            return value

    class Logger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    class PluginBase:
        def get_data(self, key):
            return getattr(self, "_store", {}).get(key)

        def save_data(self, key, value):
            self._store = getattr(self, "_store", {})
            self._store[key] = value

    class EventManager:
        @staticmethod
        def register(_event_type):
            return lambda func: func

    class MediaType(Enum):
        MOVIE = "电影"
        TV = "电视剧"

    class FileItem:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class RefreshMediaItem:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MediaServerHelper:
        def get_configs(self):
            return {}

        def get_services(self, **_kwargs):
            return {}

    class SystemUtils:
        @staticmethod
        def list_files(_root, _extensions):
            return []

    class EventType:
        WebhookMessage = "WebhookMessage"

    cron.CronTrigger = CronTrigger
    log.logger = Logger()
    plugins._PluginBase = PluginBase
    event.Event = object
    event.eventmanager = EventManager()
    schema_types.EventType = EventType
    schema_types.MediaType = MediaType
    schemas.FileItem = FileItem
    schemas.RefreshMediaItem = RefreshMediaItem
    schemas.ServiceInfo = object
    schemas.WebhookEventInfo = object
    chain_media.MediaChain = object
    config.settings = types.SimpleNamespace(RMT_MEDIAEXT=[".mkv"])
    mediaserver.MediaServerHelper = MediaServerHelper
    system.SystemUtils = SystemUtils

    app.schemas = schemas
    app.chain = chain
    app.core = core
    app.helper = helper
    app.plugins = plugins
    app.utils = utils
    apscheduler.triggers = triggers

    plugin_file = (
        Path(__file__).resolve().parents[2]
        / "plugins.v2"
        / "embymediaimagemanager"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location(
        "embymediaimagemanager_test_module", plugin_file
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        current = {
            name
            for name in sys.modules
            if name == prefixes[0]
            or name.startswith(f"{prefixes[0]}.")
            or name == prefixes[1]
            or name.startswith(f"{prefixes[1]}.")
        }
        for name in current - snapshots.keys():
            sys.modules.pop(name, None)
        sys.modules.update(snapshots)
    return module


MODULE = _load_plugin_module()
EmbyMediaImageManager = MODULE.EmbyMediaImageManager


def _plugin(config=None):
    plugin = EmbyMediaImageManager()
    plugin.init_plugin(config or {"enabled": True})
    return plugin


def test_invalid_numbers_and_cron_fall_back_to_safe_defaults():
    plugin = _plugin(
        {
            "enabled": True,
            "delay_seconds": "abc",
            "aggregate_seconds": -10,
            "audit_cron": "bad cron",
        }
    )
    assert plugin._delay_seconds == 60
    assert plugin._aggregate_seconds == 0
    assert plugin._audit_cron == plugin.DEFAULT_AUDIT_CRON


def test_episode_event_targets_series_root():
    path = Path("/media/Show/Season 01/Show.S01E01.mkv")
    assert EmbyMediaImageManager._realtime_target(path, "Episode") == Path(
        "/media/Show"
    )
    assert EmbyMediaImageManager._realtime_target(path, "Movie") == path


def test_webhook_server_selection_is_an_actual_allowlist(monkeypatch):
    plugin = _plugin({"enabled": True, "mediaservers": ["LivingRoom"]})
    timers = []

    class FakeTimer:
        def __init__(self, delay, func, args):
            self.delay, self.func, self.args, self.cancelled = delay, func, args, False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(MODULE.threading, "Timer", FakeTimer)

    def event(server, episode):
        info = types.SimpleNamespace(
            channel="emby",
            event="library.new",
            server_name=server,
            item_type="Episode",
            item_path=f"/media/Show/Season 01/E{episode}.mkv",
            item_id=f"episode-{episode}",
            json_object={
                "Item": {
                    "Type": "Episode",
                    "Path": f"/media/Show/Season 01/E{episode}.mkv",
                    "Id": f"episode-{episode}",
                    "SeriesId": "series-1",
                }
            },
        )
        return types.SimpleNamespace(event_data=info)

    plugin.on_webhook(event("Bedroom", 1))
    assert plugin._pending == {}
    plugin.on_webhook(event("LivingRoom", 1))
    plugin.on_webhook(event("LivingRoom", 2))
    assert len(plugin._pending) == 1
    assert (
        next(iter(plugin._pending.values()))["path"].replace("\\", "/") == "/media/Show"
    )
    assert timers[0].cancelled is True


def test_selected_media_library_controls_realtime_scope(monkeypatch):
    class Library:
        id = "movies"
        name = "电影库"
        type = "电影"
        path = ["/media/movies"]
        item_count = 12

    class Instance:
        def get_librarys(self):
            return [Library()]

    class Service:
        instance = Instance()

    class Helper:
        def get_services(self, **_kwargs):
            return {"LivingRoom": Service()}

    monkeypatch.setattr(MODULE, "MediaServerHelper", Helper)
    plugin = _plugin(
        {
            "enabled": True,
            "mediaservers": ["LivingRoom"],
            "realtime_libraries": ["LivingRoom::movies"],
            "audit_libraries": ["LivingRoom::movies"],
        }
    )
    assert plugin._selected_library_paths("LivingRoom") == (
        True,
        ["/media/movies"],
    )
    assert plugin._selected_library_paths("Other") == (True, [])
    assert plugin._audit_roots() == [(Path("/media/movies"), ["LivingRoom"])]
    plugin._realtime_libraries = []
    assert plugin._selected_library_paths("LivingRoom") == (False, [])


def test_legacy_library_config_is_kept_as_audit_scope_only():
    plugin = _plugin({"enabled": True, "media_libraries": ["LivingRoom::foreign"]})
    assert plugin._realtime_libraries == []
    assert plugin._audit_libraries == ["LivingRoom::foreign"]


def test_empty_library_path_uses_item_ancestors_for_realtime_scope(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return [{"Id": "movies", "Name": "外语电影"}]

    class Library:
        id = "movies"
        name = "外语电影"
        type = "电影"
        path = []

    class Instance:
        def get_librarys(self):
            return [Library()]

        def get_data(self, **_kwargs):
            return Response()

    class Service:
        instance = Instance()

    class Helper:
        def get_services(self, **_kwargs):
            return {"LivingRoom": Service()}

    monkeypatch.setattr(MODULE, "MediaServerHelper", Helper)
    plugin = _plugin({"enabled": True, "realtime_libraries": ["LivingRoom::movies"]})
    item = {"Id": "movie-1", "Type": "Movie", "Path": "/mapped/movie.mkv"}
    info = types.SimpleNamespace(item_id="movie-1")
    assert plugin._event_matches_selected_libraries(
        "LivingRoom", "/mapped/movie.mkv", item, info
    )


def test_empty_library_path_uses_emby_items_for_audit_roots(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"Items": [{"Type": "Movie", "Path": "/media/movies/Foo/Foo.mkv"}]}

    class Library:
        id = "movies"
        name = "外语电影"
        type = "电影"
        path = []

    class Instance:
        def get_librarys(self):
            return [Library()]

        def get_data(self, **_kwargs):
            return Response()

    class Service:
        instance = Instance()

    class Helper:
        def get_services(self, **_kwargs):
            return {"LivingRoom": Service()}

    monkeypatch.setattr(MODULE, "MediaServerHelper", Helper)
    plugin = _plugin({"enabled": True, "audit_libraries": ["LivingRoom::movies"]})
    assert plugin._audit_roots() == [(Path("/media/movies/Foo"), ["LivingRoom"])]


def test_form_exposes_library_selection_and_removes_webhook_source(monkeypatch):
    class Helper:
        def get_services(self, **_kwargs):
            return {}

        def get_configs(self):
            return {}

    monkeypatch.setattr(MODULE, "MediaServerHelper", Helper)
    plugin = _plugin()
    form, defaults = plugin.get_form()
    assert defaults["realtime_libraries"] == []
    assert defaults["audit_libraries"] == []
    assert defaults["active_tab"] == "realtime"

    def walk(node):
        if isinstance(node, dict):
            yield node
            for child in node.get("content") or []:
                yield from walk(child)
        elif isinstance(node, list):
            for child in node:
                yield from walk(child)

    fields = [node.get("props", {}).get("model") for node in walk(form)]
    assert "realtime_libraries" in fields
    assert "audit_libraries" in fields
    assert "webhook_source" not in fields
    assert any(node.get("component") == "VTabs" for node in walk(form))
    assert any(node.get("component") == "VWindow" for node in walk(form))


def test_scrape_failure_does_not_refresh_emby(tmp_path):
    plugin = _plugin()
    plugin._pending["job"] = {"path": str(tmp_path), "server": "Emby", "item_id": "1"}
    plugin._scrape_path = lambda _path: False
    refreshes = []
    plugin._refresh_emby = lambda *args: refreshes.append(args)

    plugin._process_pending("job")

    assert refreshes == []
    assert "刮削失败" in plugin._last_realtime_result


def test_provided_media_info_avoids_duplicate_recognition(tmp_path):
    plugin = _plugin()
    calls = []

    class Chain:
        def recognize_by_path(self, *_args, **_kwargs):
            raise AssertionError("不应重复识别")

        def scrape_metadata(self, **kwargs):
            calls.append(kwargs)

    assert plugin._scrape_path(tmp_path, chain=Chain(), mediainfo=object()) is True
    assert len(calls) == 1


def test_audit_rejects_overlapping_run():
    plugin = _plugin({"enabled": True, "audit_enabled": True, "audit_paths": "/media"})
    assert plugin._audit_lock.acquire(blocking=False)
    try:
        plugin.run_audit()
        assert plugin._audit_running is False
    finally:
        plugin._audit_lock.release()
