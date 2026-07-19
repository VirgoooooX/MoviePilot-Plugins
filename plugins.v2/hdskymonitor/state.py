"""天空监控去重状态管理。"""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


class MonitorState:
    """管理监控去重状态并执行原子写入。"""
    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> dict:
        """加载状态，损坏时返回空状态。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"processed": [], "last_run": None}
        except (OSError, ValueError, TypeError):
            return {"processed": [], "last_run": None}

    def save(self, state: dict) -> None:
        """原子保存状态。"""
        state["processed"] = list(dict.fromkeys(state.get("processed", [])))[-1000:]
        state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
