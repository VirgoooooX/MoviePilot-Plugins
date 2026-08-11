"""Emby 中文角色同步事故维护工具的安全边界测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


TOOLS_DIR = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "maintenance"
    / "embychineserolesync"
)


def _load_tool(module_name: str, filename: str):
    """按文件加载维护工具，避免依赖其成为运行时插件模块。"""
    spec = importlib.util.spec_from_file_location(module_name, TOOLS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_polluted_cleaner_requests_people_and_lock_fields():
    """事故清理读取条目时必须显式获取计划和快照所需字段。"""
    module = _load_tool("cleanup_polluted_people_test", "cleanup_polluted_people.py")
    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"Id": "item-1", "People": [], "LockedFields": []}

    cleaner = module.Cleaner.__new__(module.Cleaner)
    cleaner.service = SimpleNamespace(
        instance=SimpleNamespace(get_data=lambda url: calls.append(url) or Response())
    )
    cleaner.get_item("item-1")
    assert calls
    assert "Fields=ChannelMappingInfo,People,ProviderIds,LockedFields" in calls[0]


def test_duplicate_cleanup_blocks_relations_when_canonical_person_changed():
    """canonical Person 快照失效后不能继续替换依赖它的媒体关系。"""
    module = _load_tool(
        "cleanup_known_person_duplicates_test",
        "cleanup_known_person_duplicates.py",
    )
    cleaner = module.KnownDuplicateCleaner.__new__(module.KnownDuplicateCleaner)
    updates = []
    cleaner.get_item = lambda item_id: (
        {"Id": "person-1", "Name": "已被外部修改"}
        if item_id == "person-1"
        else {"Id": "series-1", "People": [{"Id": "duplicate-1"}]}
    )
    cleaner.update_item = lambda item_id, item: updates.append((item_id, item)) or True
    plan = {
        "person_updates": [{
            "title": "测试剧",
            "canonical_id": "person-1",
            "before": {"Id": "person-1", "Name": "旧名"},
            "after": {"Id": "person-1", "Name": "中文名"},
        }],
        "targets": [{
            "title": "测试剧",
            "item_id": "series-1",
            "before_people": [{"Id": "duplicate-1"}],
            "after_people": [{"Id": "person-1"}],
        }],
    }
    result = cleaner.apply(plan)
    assert updates == []
    assert any("canonical Person" in item["reason"] for item in result["skipped"])
