"""天空监控状态测试。"""

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins.v2" / "hdskymonitor"
sys.path.insert(0, str(PLUGIN_DIR))

from state import MonitorState


def test_atomic_state_roundtrip(tmp_path):
    """状态应可原子保存并正确读取。"""
    store = MonitorState(str(tmp_path / "state.json"))
    store.save({"processed": ["1", "1", "2"]})
    assert store.load()["processed"] == ["1", "2"]
