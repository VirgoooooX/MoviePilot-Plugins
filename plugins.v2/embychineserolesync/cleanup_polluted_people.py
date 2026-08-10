#!/usr/bin/env python3
"""清理 EmbyChineseRoleSync v1.8.8 补充演员功能造成的 People 关系污染。"""

import argparse
import copy
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from app.helper.mediaserver import MediaServerHelper
from app.modules.themoviedb import TmdbApi
from app.plugins.embychineserolesync import EmbyChineseRoleSync
from app.schemas.types import MediaType
from app.utils.zhconv import convert as zhconv_convert


LOG_PATH = Path("/config/logs/plugins/embychineserolesync.log")
BACKUP_ROOT = Path("/config/temp/embychineserolesync-cleanup")
ADDITION_PATTERN = re.compile(r"<(.+?)> 按开关补充豆瓣演员 \d+ 人：(.*)$")
SEASON_SUFFIX_PATTERN = re.compile(r"-第\s*\d+\s*季$")


def normalize_name(name: object) -> str:
    """规范化人物姓名用于关系清理匹配。"""
    text = str(name or "").strip().lower()
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    try:
        return zhconv_convert(text, "zh-hans")
    except Exception:
        return text


def provider_id(item: Optional[dict], provider: str) -> Optional[str]:
    """不区分大小写读取 ProviderIds。"""
    target = provider.lower()
    for key, value in ((item or {}).get("ProviderIds") or {}).items():
        if str(key).lower() == target and value:
            return str(value)
    return None


def parse_additions(log_path: Path) -> Dict[str, Set[str]]:
    """从事故日志中提取每部媒体被插件追加的人名。"""
    additions: Dict[str, Set[str]] = {}
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = ADDITION_PATTERN.search(line)
        if not match:
            continue
        title = SEASON_SUFFIX_PATTERN.sub("", match.group(1)).strip()
        names = {name.strip() for name in match.group(2).split("、") if name.strip()}
        additions.setdefault(title, set()).update(names)
    return additions


class Cleaner:
    """以只读计划、完整备份和显式应用三阶段清理媒体 People 关系。"""

    def __init__(self, server_name: str):
        """初始化 Emby 服务和 TMDB 客户端。"""
        self.server_name = server_name
        self.service = MediaServerHelper().get_services(name_filters=[server_name]).get(server_name)
        if not self.service:
            raise RuntimeError(f"未找到可用媒体服务器：{server_name}")
        self.tmdb = TmdbApi()
        self.plugin = EmbyChineseRoleSync()
        self.person_cache: Dict[str, dict] = {}
        self.tmdb_person_cache: Dict[str, dict] = {}

    def get_item(self, item_id: object) -> dict:
        """读取 Emby 条目完整信息。"""
        response = self.service.instance.get_data(
            url=f"[HOST]emby/Users/[USER]/Items/{item_id}?X-Emby-Token=[APIKEY]"
                "&Fields=ChannelMappingInfo&ExcludeFields=Chapters,MediaSources,MediaStreams,Subviews"
        )
        return response.json() if response and response.status_code == 200 else {}

    def query_items(self, url: str) -> List[dict]:
        """执行 Emby 列表查询。"""
        response = self.service.instance.get_data(url=url)
        if not response or response.status_code != 200:
            return []
        return response.json().get("Items", []) or []

    def find_media(self, title: str) -> dict:
        """按准确标题查找被污染的电影或电视剧。"""
        items = self.query_items(
            f"[HOST]emby/Users/[USER]/Items?SearchTerm={title}"
            "&IncludeItemTypes=Movie,Series&Recursive=true&Fields=ProviderIds,ProductionYear&Limit=50&api_key=[APIKEY]"
        )
        exact = [item for item in items if str(item.get("Name") or "").strip() == title]
        if len(exact) != 1:
            raise RuntimeError(f"{title} 精确匹配数量为 {len(exact)}，拒绝自动处理")
        return self.get_item(exact[0].get("Id"))

    def get_person(self, person_id: object) -> dict:
        """读取并缓存 Emby Person 实体。"""
        key = str(person_id or "")
        if not key:
            return {}
        if key not in self.person_cache:
            self.person_cache[key] = self.get_item(key)
        return self.person_cache[key]

    def update_item(self, item_id: object, item: dict) -> bool:
        """写回 Emby 媒体条目。"""
        response = self.service.instance.post_data(
            url=f"[HOST]emby/Items/{item_id}?reqformat=json&api_key=[APIKEY]",
            data=json.dumps(item, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
        )
        return bool(response and response.status_code in (200, 204))

    def collect_tv_targets(self, series: dict) -> List[dict]:
        """收集电视剧总条目、季度及所有单集。"""
        targets = [series]
        seasons = self.query_items(
            f"[HOST]emby/Users/[USER]/Items?ParentId={series.get('Id')}"
            "&IncludeItemTypes=Season&Recursive=true&api_key=[APIKEY]"
        )
        for season in seasons:
            full_season = self.get_item(season.get("Id"))
            targets.append(full_season)
            episodes = self.query_items(
                f"[HOST]emby/Users/[USER]/Items?ParentId={season.get('Id')}"
                "&IncludeItemTypes=Episode&Recursive=true&api_key=[APIKEY]"
            )
            targets.extend(self.get_item(episode.get("Id")) for episode in episodes)
        return [item for item in targets if item.get("Id")]

    def tmdb_movie_cast_ids(self, movie: dict) -> Set[str]:
        """查询电影 TMDB 演员身份基线。"""
        tmdb_id = provider_id(movie, "tmdb")
        if not tmdb_id:
            return set()
        credits = self.tmdb.movie.credits(movie_id=tmdb_id) or {}
        return {str(cast.get("id")) for cast in credits.get("cast", []) if cast.get("id")}

    def tmdb_season_cast_ids(self, series: dict) -> Set[str]:
        """查询电视剧各季度 TMDB 演员身份基线。"""
        tmdb_id = provider_id(series, "tmdb")
        if not tmdb_id:
            return set()
        cast_ids: Set[str] = set()
        seasons = self.query_items(
            f"[HOST]emby/Users/[USER]/Items?ParentId={series.get('Id')}"
            "&IncludeItemTypes=Season&Recursive=true&api_key=[APIKEY]"
        )
        for season in seasons:
            season_number = season.get("IndexNumber")
            if season_number is None:
                continue
            credits = self.tmdb.season_obj.credits(tv_id=tmdb_id, season_num=season_number) or {}
            cast_ids.update(str(cast.get("id")) for cast in credits.get("cast", []) if cast.get("id"))
        return cast_ids

    def choose_tv_baseline(self, targets: List[dict], cast_ids: Set[str]) -> Dict[str, dict]:
        """从当前各层关系中选出带 TMDB 身份的原始演员关系。"""
        candidates: Dict[str, List[dict]] = {}
        for target in targets:
            for relation in target.get("People", []) or []:
                if relation.get("Type") != "Actor" or not relation.get("Id"):
                    continue
                person = self.get_person(relation.get("Id"))
                tmdb_id = provider_id(person, "tmdb")
                if tmdb_id and tmdb_id in cast_ids:
                    candidates.setdefault(tmdb_id, []).append(relation)
        baseline: Dict[str, dict] = {}
        for tmdb_id, relations in candidates.items():
            # 同一 TMDB 身份优先保留带图片、角色和较早 Person ID的关系。
            best = sorted(
                relations,
                key=lambda relation: (
                    not bool(relation.get("PrimaryImageTag")),
                    not bool(relation.get("Role")),
                    int(relation.get("Id")) if str(relation.get("Id")).isdigit() else 10**18,
                ),
            )[0]
            baseline[tmdb_id] = copy.deepcopy(best)
        return baseline

    def clean_tv_people(self, target: dict, baseline: Dict[str, dict], added_names: Set[str]) -> Tuple[List[dict], List[dict]]:
        """按 TMDB 身份基线和事故日志清理电视剧层级 People。"""
        original = target.get("People", []) or []
        non_actors = [copy.deepcopy(item) for item in original if item.get("Type") != "Actor"]
        kept: List[dict] = []
        removed: List[dict] = []
        seen_tmdb: Set[str] = set()
        baseline_ids = {str(item.get("Id")) for item in baseline.values() if item.get("Id")}
        logged_names = {normalize_name(name) for name in added_names}
        for relation in original:
            if relation.get("Type") != "Actor":
                continue
            person = self.get_person(relation.get("Id")) if relation.get("Id") else {}
            tmdb_id = provider_id(person, "tmdb")
            relation_id = str(relation.get("Id") or "")
            is_logged = normalize_name(relation.get("Name")) in logged_names
            if target.get("Type") in ("Season", "Episode") and is_logged:
                # v1.8.8 的追加发生在季，随后覆盖广播到每一集；这些层级可按日志精确撤销。
                removed.append(copy.deepcopy(relation))
            elif tmdb_id in baseline and tmdb_id not in seen_tmdb:
                canonical = copy.deepcopy(baseline[tmdb_id])
                # 当前层若已有同一原 Person ID的角色，优先保留该层角色。
                if relation_id == str(canonical.get("Id")) and relation.get("Role"):
                    canonical["Role"] = relation.get("Role")
                kept.append(canonical)
                seen_tmdb.add(tmdb_id)
            elif relation_id in baseline_ids:
                # 已由同一 TMDB 身份保留，当前是重复关系。
                removed.append(copy.deepcopy(relation))
            elif normalize_name(relation.get("Name")) in logged_names:
                removed.append(copy.deepcopy(relation))
            else:
                # 不在事故日志且无法证明是重复的关系保守保留。
                kept.append(copy.deepcopy(relation))
        return non_actors + kept, removed

    def tmdb_person(self, tmdb_id: object) -> dict:
        """读取并缓存 TMDB 人物详情。"""
        key = str(tmdb_id or "")
        if not key:
            return {}
        if key not in self.tmdb_person_cache:
            self.tmdb_person_cache[key] = self.tmdb.get_person_detail(key) or {}
        return self.tmdb_person_cache[key]

    def build_douban_identity_map(self, media: dict) -> Dict[str, str]:
        """把豆瓣中英文名映射到同一豆瓣人物身份。"""
        media_type = MediaType.TV if media.get("Type") == "Series" else MediaType.MOVIE
        douban_info = self.plugin._get_douban_info(media_type, media, None) or {}
        mapping: Dict[str, str] = {}
        for actor in douban_info.get("actors", []) or []:
            actor_id = str(actor.get("id") or "")
            if not actor_id:
                continue
            for name in (actor.get("name"), actor.get("latin_name")):
                key = normalize_name(name)
                if key and key not in mapping:
                    mapping[key] = actor_id
        return mapping

    def relation_identity(self, relation: dict, douban_map: Dict[str, str]) -> str:
        """为媒体人物关系构造 TMDB、豆瓣或 Emby 稳定身份。"""
        person = self.get_person(relation.get("Id")) if relation.get("Id") else {}
        tmdb_id = provider_id(person, "tmdb")
        if tmdb_id:
            tmdb_person = self.tmdb_person(tmdb_id)
            for name in [relation.get("Name"), person.get("Name"), tmdb_person.get("name"), *(tmdb_person.get("also_known_as") or [])]:
                douban_id = douban_map.get(normalize_name(name))
                if douban_id:
                    return f"douban:{douban_id}"
            return f"tmdb:{tmdb_id}"
        douban_id = douban_map.get(normalize_name(relation.get("Name")))
        if douban_id:
            return f"douban:{douban_id}"
        return f"emby:{relation.get('Id')}" if relation.get("Id") else f"name:{normalize_name(relation.get('Name'))}"

    def merge_identity_relations(
        self,
        relations: List[dict],
        douban_map: Dict[str, str],
        added_names: Set[str],
        allow_remove_unique: bool,
    ) -> Tuple[List[dict], List[dict]]:
        """只合并已证实同一身份的重复关系，谨慎处理唯一新增关系。"""
        logged_names = {normalize_name(name) for name in added_names}
        groups: Dict[Tuple[str, str], List[dict]] = {}
        order: List[Tuple[str, str]] = []
        non_actors = []
        for relation in relations:
            if relation.get("Type") != "Actor":
                non_actors.append(copy.deepcopy(relation))
                continue
            key = (self.relation_identity(relation, douban_map), str(relation.get("Type") or "").lower())
            if key not in groups:
                order.append(key)
            groups.setdefault(key, []).append(relation)

        kept, removed = [], []
        for key in order:
            items = groups[key]
            if len(items) > 1:
                # 同一身份优先保留有 TMDB/IMDb、图片和较早 Emby Person ID的关系。
                ranked = sorted(
                    items,
                    key=lambda relation: (
                        not bool(provider_id(self.get_person(relation.get("Id")), "tmdb")),
                        not bool(provider_id(self.get_person(relation.get("Id")), "imdb")),
                        not bool(relation.get("PrimaryImageTag")),
                        int(relation.get("Id")) if str(relation.get("Id")).isdigit() else 10**18,
                    ),
                )
                canonical = copy.deepcopy(ranked[0])
                for duplicate in ranked[1:]:
                    if duplicate.get("Role") and not canonical.get("Role"):
                        canonical["Role"] = duplicate.get("Role")
                    removed.append(copy.deepcopy(duplicate))
                kept.append(canonical)
                continue
            relation = items[0]
            is_logged = normalize_name(relation.get("Name")) in logged_names
            person = self.get_person(relation.get("Id")) if relation.get("Id") else {}
            has_external_id = bool(provider_id(person, "tmdb") or provider_id(person, "imdb"))
            if allow_remove_unique and is_logged and not has_external_id:
                removed.append(copy.deepcopy(relation))
            else:
                kept.append(copy.deepcopy(relation))
        return non_actors + kept, removed

    def canonicalize_tv_relations(
        self,
        relations: List[dict],
        baseline: Dict[str, dict],
        douban_map: Dict[str, str],
    ) -> List[dict]:
        """把已确认同一豆瓣身份的中文副本关系替换回原 TMDB Person ID。"""
        canonical: Dict[str, dict] = {}
        for relation in baseline.values():
            identity = self.relation_identity(relation, douban_map)
            if identity.startswith("douban:"):
                canonical[identity] = relation
        normalized = []
        for relation in relations:
            if relation.get("Type") != "Actor":
                normalized.append(copy.deepcopy(relation))
                continue
            identity = self.relation_identity(relation, douban_map)
            source = canonical.get(identity)
            if not source or str(source.get("Id")) == str(relation.get("Id")):
                normalized.append(copy.deepcopy(relation))
                continue
            replacement = copy.deepcopy(source)
            # 保留当前层级已经正确同步的中文角色。
            if relation.get("Role"):
                replacement["Role"] = relation.get("Role")
            normalized.append(replacement)
        return normalized

    def clean_movie_people(
        self,
        movie: dict,
        added_names: Set[str],
        douban_map: Dict[str, str],
        cast_ids: Set[str],
    ) -> Tuple[List[dict], List[dict]]:
        """电影保留 TMDB 原演员，只移除事故日志明确追加且不属于原基线的关系。"""
        merged, duplicate_removed = self.merge_identity_relations(
            movie.get("People", []) or [], douban_map, added_names, allow_remove_unique=False
        )
        logged_names = {normalize_name(name) for name in added_names}
        kept, removed = [], list(duplicate_removed)
        for relation in merged:
            if relation.get("Type") != "Actor":
                kept.append(relation)
                continue
            person = self.get_person(relation.get("Id")) if relation.get("Id") else {}
            tmdb_id = provider_id(person, "tmdb")
            is_logged = normalize_name(relation.get("Name")) in logged_names
            if is_logged and (not tmdb_id or tmdb_id not in cast_ids):
                removed.append(copy.deepcopy(relation))
            else:
                kept.append(relation)
        return kept, removed

    def build_plan(self, additions: Dict[str, Set[str]]) -> dict:
        """生成不写入 Emby 的完整清理计划和写前快照。"""
        plan = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "server": self.server_name,
            "source_log": str(LOG_PATH),
            "media": [],
            "errors": [],
        }
        for title in sorted(additions):
            try:
                media = self.find_media(title)
                targets = self.collect_tv_targets(media) if media.get("Type") == "Series" else [media]
                douban_map = self.build_douban_identity_map(media)
                baseline = {}
                if media.get("Type") == "Series":
                    cast_ids = self.tmdb_season_cast_ids(media)
                    baseline = self.choose_tv_baseline(targets, cast_ids)
                    if not cast_ids or not baseline:
                        raise RuntimeError("缺少可靠 TMDB 季演员基线")
                else:
                    cast_ids = self.tmdb_movie_cast_ids(media)
                    if not cast_ids:
                        raise RuntimeError("缺少可靠 TMDB 电影演员基线")
                media_plan = {
                    "title": title,
                    "media_id": media.get("Id"),
                    "type": media.get("Type"),
                    "logged_added_names": sorted(additions[title]),
                    "baseline_tmdb_count": len(baseline),
                    "targets": [],
                }
                for target in targets:
                    if media.get("Type") == "Series":
                        # 先按可靠 TMDB 季基线回收事故广播关系，再按豆瓣身份合并中文/英文重复 Person。
                        baseline_cleaned, baseline_removed = self.clean_tv_people(target, baseline, additions[title])
                        merged_cleaned, duplicate_removed = self.merge_identity_relations(
                            baseline_cleaned, douban_map, additions[title], allow_remove_unique=True
                        )
                        cleaned = self.canonicalize_tv_relations(merged_cleaned, baseline, douban_map)
                        removed = baseline_removed + duplicate_removed
                    else:
                        cleaned, removed = self.clean_movie_people(target, additions[title], douban_map, cast_ids)
                    if cleaned == (target.get("People", []) or []):
                        continue
                    media_plan["targets"].append({
                        "item_id": target.get("Id"),
                        "item_type": target.get("Type"),
                        "item_name": target.get("Name"),
                        "before_people": target.get("People", []) or [],
                        "after_people": cleaned,
                        "removed_people": removed,
                        "locked_fields": target.get("LockedFields", []) or [],
                    })
                plan["media"].append(media_plan)
            except Exception as error:
                plan["errors"].append({"title": title, "error": str(error)})
        return plan

    def apply_plan(self, plan: dict) -> dict:
        """在重新比对写前快照后应用清理计划。"""
        result = {"updated": [], "skipped": [], "failed": []}
        for media in plan.get("media", []):
            for target in media.get("targets", []):
                current = self.get_item(target.get("item_id"))
                if (current.get("People", []) or []) != target.get("before_people", []):
                    result["skipped"].append({
                        "item_id": target.get("item_id"),
                        "reason": "People 已在计划生成后发生变化",
                    })
                    continue
                current["People"] = target.get("after_people", [])
                if self.update_item(target.get("item_id"), current):
                    result["updated"].append(target.get("item_id"))
                else:
                    result["failed"].append(target.get("item_id"))
        return result


def save_json(path: Path, payload: dict) -> None:
    """原子写入 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def main() -> int:
    """运行只读计划或显式应用清理。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="115", help="MoviePilot 媒体服务器名称")
    parser.add_argument("--log", default=str(LOG_PATH), help="事故日志路径")
    parser.add_argument("--plan", help="计划/备份 JSON 路径")
    parser.add_argument("--apply", action="store_true", help="应用已有计划，默认只生成计划")
    args = parser.parse_args()

    cleaner = Cleaner(args.server)
    if args.apply:
        if not args.plan:
            raise SystemExit("--apply 必须同时提供 --plan")
        plan_path = Path(args.plan)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan.get("errors"):
            raise SystemExit("计划包含错误，拒绝应用")
        result = cleaner.apply_plan(plan)
        result_path = plan_path.with_name(plan_path.stem + "-result.json")
        save_json(result_path, result)
        print(json.dumps({"result_path": str(result_path), **result}, ensure_ascii=False, indent=2))
        return 1 if result.get("failed") or result.get("skipped") else 0

    log_path = Path(args.log)
    additions = parse_additions(log_path)
    if len(additions) != 19:
        raise SystemExit(f"事故媒体数量应为 19，实际为 {len(additions)}，拒绝生成可执行计划")
    plan = cleaner.build_plan(additions)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    plan_path = Path(args.plan) if args.plan else BACKUP_ROOT / f"cleanup-plan-{timestamp}.json"
    save_json(plan_path, plan)
    summary = {
        "plan_path": str(plan_path),
        "media_count": len(plan.get("media", [])),
        "error_count": len(plan.get("errors", [])),
        "target_count": sum(len(media.get("targets", [])) for media in plan.get("media", [])),
        "removed_relation_count": sum(
            len(target.get("removed_people", []))
            for media in plan.get("media", [])
            for target in media.get("targets", [])
        ),
        "errors": plan.get("errors", []),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if plan.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
