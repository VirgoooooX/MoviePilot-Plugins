#!/usr/bin/env python3
"""清理已确认的中英文重复 Emby Person，并保留原 TMDB Person ID。"""

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

try:
    from app.helper.mediaserver import MediaServerHelper
except ImportError:  # 允许在宿主外执行 --help，实际操作仍需 MoviePilot 环境。
    MediaServerHelper = None


BACKUP_ROOT = Path("/config/temp/embychineserolesync-cleanup")


def plan_digest(plan: dict) -> str:
    """计算不包含自身摘要字段的稳定计划摘要。"""
    payload = {key: value for key, value in plan.items() if key != "plan_digest"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


class KnownDuplicateCleaner:
    """为已人工确认的重复人物生成快照并安全替换媒体关系。"""

    def __init__(self, server_name: str, mappings: Dict[str, dict]):
        """初始化媒体服务器。"""
        if MediaServerHelper is None:
            raise RuntimeError("该维护工具必须在 MoviePilot 宿主 Python 环境中运行")
        self.server_name = server_name
        self.mappings = mappings
        self.service = MediaServerHelper().get_services(name_filters=[server_name]).get(server_name)
        if not self.service:
            raise RuntimeError(f"未找到媒体服务器：{server_name}")

    def get_item(self, item_id: object) -> dict:
        """读取 Emby 条目详情。"""
        response = self.service.instance.get_data(
            url=f"[HOST]emby/Users/[USER]/Items/{item_id}?X-Emby-Token=[APIKEY]"
                "&Fields=People,ProviderIds,LockedFields&ExcludeFields=Chapters,MediaSources,MediaStreams,Subviews"
        )
        return response.json() if response and response.status_code == 200 else {}

    def query_items(self, url: str) -> List[dict]:
        """读取 Emby 条目列表。"""
        response = self.service.instance.get_data(url=url)
        return response.json().get("Items", []) if response and response.status_code == 200 else []

    def update_item(self, item_id: object, item: dict) -> bool:
        """写回 Emby 条目。"""
        response = self.service.instance.post_data(
            url=f"[HOST]emby/Items/{item_id}?reqformat=json&api_key=[APIKEY]",
            data=json.dumps(item, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
        )
        return bool(response and response.status_code in (200, 204))

    def collect_targets(self, series_id: str) -> List[dict]:
        """收集电视剧、季度及所有单集。"""
        targets = [self.get_item(series_id)]
        seasons = self.query_items(
            f"[HOST]emby/Users/[USER]/Items?ParentId={series_id}"
            "&IncludeItemTypes=Season&Recursive=true&api_key=[APIKEY]"
        )
        for season in seasons:
            targets.append(self.get_item(season.get("Id")))
            episodes = self.query_items(
                f"[HOST]emby/Users/[USER]/Items?ParentId={season.get('Id')}"
                "&IncludeItemTypes=Episode&Recursive=true&api_key=[APIKEY]"
            )
            targets.extend(self.get_item(episode.get("Id")) for episode in episodes)
        return [target for target in targets if target.get("Id")]

    @staticmethod
    def replace_people(people: List[dict], mapping: Dict[str, dict]) -> List[dict]:
        """替换重复 Person ID并按 Person ID与职务去重。"""
        merged: Dict[Tuple[str, str], dict] = {}
        order: List[Tuple[str, str]] = []
        for relation in people:
            updated = copy.deepcopy(relation)
            duplicate = mapping.get(str(updated.get("Id") or ""))
            if duplicate:
                updated["Id"] = duplicate["canonical_id"]
                updated["Name"] = duplicate["name"]
                updated.pop("PrimaryImageTag", None)
            # 原 Person 关系也统一使用目标中文名。
            for item in mapping.values():
                if str(updated.get("Id")) == str(item["canonical_id"]):
                    updated["Name"] = item["name"]
            key = (str(updated.get("Id") or updated.get("Name") or ""), str(updated.get("Type") or ""))
            if key not in merged:
                merged[key] = updated
                order.append(key)
            else:
                current = merged[key]
                if updated.get("Role") and not current.get("Role"):
                    current["Role"] = updated.get("Role")
                if updated.get("PrimaryImageTag") and not current.get("PrimaryImageTag"):
                    current["PrimaryImageTag"] = updated.get("PrimaryImageTag")
        return [merged[key] for key in order]

    def build_plan(self) -> dict:
        """生成只读快照和显式替换计划。"""
        plan = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "server": self.server_name,
            "person_updates": [],
            "targets": [],
            "errors": [],
        }
        for title, config in self.mappings.items():
            for duplicate_id, item in config["people"].items():
                canonical = self.get_item(item["canonical_id"])
                duplicate = self.get_item(duplicate_id)
                if not canonical or not duplicate:
                    plan["errors"].append({"title": title, "duplicate_id": duplicate_id, "error": "Person 不存在"})
                    continue
                provider_ids = canonical.get("ProviderIds") or {}
                if not any(str(key).lower() == "tmdb" and value for key, value in provider_ids.items()):
                    plan["errors"].append({"title": title, "canonical_id": item["canonical_id"], "error": "原 Person 缺少 TMDB ID"})
                    continue
                after = copy.deepcopy(canonical)
                after["Name"] = item["name"]
                locked = after.setdefault("LockedFields", [])
                if "Name" not in locked:
                    locked.append("Name")
                if after != canonical:
                    plan["person_updates"].append({
                        "title": title,
                        "canonical_id": item["canonical_id"],
                        "duplicate_id": duplicate_id,
                        "before": canonical,
                        "after": after,
                    })
            for target in self.collect_targets(config["series_id"]):
                before = target.get("People", []) or []
                after = self.replace_people(before, config["people"])
                if before != after:
                    plan["targets"].append({
                        "title": title,
                        "item_id": target.get("Id"),
                        "item_type": target.get("Type"),
                        "item_name": target.get("Name"),
                        "before_people": before,
                        "after_people": after,
                    })
        plan["plan_digest"] = plan_digest(plan)
        return plan

    def apply(self, plan: dict) -> dict:
        """比对快照后先更新原 Person，再替换媒体 People 关系。"""
        result = {"person_updated": [], "targets_updated": [], "skipped": [], "failed": []}
        blocked_titles = set()
        for update in plan.get("person_updates", []):
            current = self.get_item(update["canonical_id"])
            if current != update["before"]:
                result["skipped"].append({"id": update["canonical_id"], "reason": "Person 已变化"})
                blocked_titles.add(str(update.get("title") or ""))
            elif self.update_item(update["canonical_id"], update["after"]):
                result["person_updated"].append(update["canonical_id"])
            else:
                result["failed"].append(update["canonical_id"])
                blocked_titles.add(str(update.get("title") or ""))
        for target in plan.get("targets", []):
            if str(target.get("title") or "") in blocked_titles:
                result["skipped"].append({
                    "id": target["item_id"],
                    "reason": "依赖的 canonical Person 未安全更新",
                })
                continue
            current = self.get_item(target["item_id"])
            if (current.get("People", []) or []) != target["before_people"]:
                result["skipped"].append({"id": target["item_id"], "reason": "People 已变化"})
                continue
            current["People"] = target["after_people"]
            if self.update_item(target["item_id"], current):
                result["targets_updated"].append(target["item_id"])
            else:
                result["failed"].append(target["item_id"])
        return result


def save_json(path: Path, payload: dict) -> None:
    """原子保存 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main() -> int:
    """生成或应用已确认重复人物清理计划。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="MoviePilot 媒体服务器名称（必须显式指定）")
    parser.add_argument("--mapping", help="人工确认的映射 JSON 文件，生成计划时必须显式提供")
    parser.add_argument("--plan")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", help="应用计划时必须提供计划摘要（plan_digest）")
    args = parser.parse_args()
    if args.apply:
        if not args.plan:
            raise SystemExit("--apply 必须提供 --plan")
        plan_path = Path(args.plan)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan.get("errors"):
            raise SystemExit("计划存在错误，拒绝执行")
        if plan.get("server") != args.server:
            raise SystemExit("计划所属服务器与 --server 不一致，拒绝跨服应用")
        digest = plan_digest(plan)
        if not args.confirm or args.confirm != digest or plan.get("plan_digest") != digest:
            raise SystemExit(f"需要 --confirm {digest} 才能应用该计划")
        cleaner = KnownDuplicateCleaner(args.server, {})
        result = cleaner.apply(plan)
        result_path = plan_path.with_name(plan_path.stem + "-result.json")
        save_json(result_path, result)
        print(json.dumps({"result_path": str(result_path), **result}, ensure_ascii=False, indent=2))
        return 1 if result["failed"] or result["skipped"] else 0
    if not args.mapping:
        raise SystemExit("生成计划必须提供 --mapping，工具不会使用内置人物或服务器 ID")
    mapping_path = Path(args.mapping)
    mappings = json.loads(mapping_path.read_text(encoding="utf-8"))
    if not isinstance(mappings, dict) or not mappings:
        raise SystemExit("--mapping 必须是非空 JSON 对象")
    cleaner = KnownDuplicateCleaner(args.server, mappings)
    plan = cleaner.build_plan()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    plan_path = Path(args.plan) if args.plan else BACKUP_ROOT / f"known-duplicates-plan-{stamp}.json"
    save_json(plan_path, plan)
    print(json.dumps({
        "plan_path": str(plan_path),
        "person_updates": len(plan["person_updates"]),
        "target_updates": len(plan["targets"]),
        "errors": plan["errors"],
        "plan_digest": plan.get("plan_digest"),
    }, ensure_ascii=False, indent=2))
    return 1 if plan["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
