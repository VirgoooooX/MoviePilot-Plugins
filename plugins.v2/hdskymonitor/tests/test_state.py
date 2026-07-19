"""天空监控状态测试。"""
from plugins.v2.hdskymonitor.state import MonitorState


def test_atomic_state_roundtrip(tmp_path):
    """状态应可原子保存并正确读取。"""
    store = MonitorState(str(tmp_path / "state.json"))
    store.save({"processed": ["1", "1", "2"]})
    assert store.load()["processed"] == ["1", "2"]
