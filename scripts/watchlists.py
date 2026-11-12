"""Merge the user's existing watchlists without duplicating movie pages."""

import json
import re

from douban2notion import utils

SOURCE_DATABASE = "b6fb00074d8444b49ea4f945e9855a88"
PROGRESS_DATABASE = "97f083001bbb4f0b9a16d5b5d9f34946"
PLAYLISTS = {"Marvel": "漫威", "Nolan": "诺兰"}


def progress_scope(name):
    if name == "Nolan":
        return "诺兰"
    return name.removeprefix("Marvel_")


def film_title(title):
    return re.sub(r"[（(]\d{4}[）)]$", "", title).strip()


def title_forms(title):
    title = film_title(title)
    return {
        re.sub(r"[^\w]", "", value).casefold()
        for value in (title, re.split(r"[:：]", title)[0])
    }


def matching_movies(pages, title, year):
    matches = []
    for page in pages:
        properties = page["properties"]
        other_title = utils.get_property_value(properties["电影名"])
        other_year = (properties.get("上映年份") or {}).get("number")
        if other_year and year and int(other_year) != int(year):
            continue
        if title_forms(title) & title_forms(other_title):
            matches.append(page)
    return matches


def build_plan(helper):
    sources = helper.query_all(SOURCE_DATABASE)
    targets = helper.query_all(helper.movie_database_id)
    plan = []
    target_ids = set()
    for source in sources:
        values = source["properties"]
        title = utils.get_property_value(values["电影名称"])
        match = re.search(r"[（(](\d{4})[）)]$", title)
        if not match:
            raise ValueError(f"片单电影缺少年份：{title}")
        year = int(match[1])
        playlist = utils.get_property_value(values.get("选择") or {})
        if playlist not in PLAYLISTS:
            raise ValueError(f"片单分类需要确认：{title} / {playlist}")
        source_url = f"https://www.notion.so/{source['id'].replace('-', '')}"
        candidates = [
            page
            for page in targets
            if (page["properties"].get("原片单记录") or {}).get("url") == source_url
        ] or matching_movies(targets, title, year)
        if len(candidates) > 1:
            raise ValueError(f"电影匹配不唯一：{title}")
        target = candidates[0] if candidates else None
        watched = values["已看"]["checkbox"]
        if target:
            if target["id"] in target_ids:
                raise ValueError(f"原片单中有多条记录匹配同一电影：{title}")
            target_ids.add(target["id"])
            status = target["properties"]["状态"]["status"]["name"]
            if watched and status != "看过":
                raise ValueError(f"已看状态冲突，需要确认：{title} / 豆瓣 {status}")
        plan.append(
            {
                "source": source,
                "source_url": source_url,
                "title": title,
                "year": year,
                "playlist": PLAYLISTS[playlist],
                "watched": watched,
                "target": target,
            }
        )
    source_ids = {row["source"]["id"] for row in plan}
    progress = helper.query_all(PROGRESS_DATABASE)
    for row in progress:
        for relation in row["properties"]["电影清单"]["relation"]:
            if relation["id"] not in source_ids:
                raise ValueError("原进度条引用了未读取的电影，已停止合并")
    print(
        json.dumps(
            {
                "片单数": len(plan),
                "合并已有电影": sum(row["target"] is not None for row in plan),
                "新增计划电影": sum(row["target"] is None for row in plan),
                "匹配": [
                    {
                        "片名": row["title"],
                        "已有": bool(row["target"]),
                        "已看": row["watched"],
                    }
                    for row in plan
                ],
            },
            ensure_ascii=False,
        )
    )
    return plan, progress


def merge_watchlists(helper, apply):
    plan, progress = build_plan(helper)
    if not apply:
        return
    all_rows = helper.query_all(helper.database_id_dict["全部"])
    if len(all_rows) != 1:
        raise ValueError("全部统计记录不唯一")
    mapping = {}
    for row in plan:
        source_values = row["source"]["properties"]
        target = row["target"]
        playlists = [
            value["name"]
            for value in ((target or {}).get("properties", {}).get("片单") or {}).get(
                "multi_select", []
            )
        ]
        properties = {
            "片单": {
                "multi_select": [
                    {"name": name}
                    for name in dict.fromkeys(playlists + [row["playlist"]])
                ]
            },
            "片单顺序": {
                "number": source_values["上映顺序"].get("number") or row["year"]
            },
            "原片单记录": {"url": row["source_url"]},
        }
        phase = utils.get_property_value(source_values.get("阶段") or {})
        if phase:
            properties["阶段"] = {"select": {"name": phase}}
        if target:
            helper.update_page(target["id"], properties)
        else:
            properties.update(
                {
                    "电影名": utils.get_title(film_title(row["title"])),
                    "上映年份": {"number": row["year"]},
                    "类型": {"select": {"name": "movie"}},
                    "状态": {"status": {"name": "看过" if row["watched"] else "想看"}},
                    "全部": utils.get_relation([all_rows[0]["id"]]),
                }
            )
            watched_date = (source_values.get("Date") or {}).get("date")
            if watched_date:
                properties["日期"] = {"date": watched_date}
            target = helper.create_page(
                {"database_id": helper.movie_database_id}, properties, None
            )
        mapping[row["source"]["id"]] = target["id"]
    original_scopes = {
        progress_scope(utils.get_property_value(row["properties"]["范围"]))
        for row in progress
        if row["properties"]["电影清单"]["relation"]
    }
    for row in progress:
        source_relations = row["properties"]["电影清单"]["relation"]
        if not source_relations:
            scope = progress_scope(utils.get_property_value(row["properties"]["范围"]))
            if scope in original_scopes:
                helper.client.pages.update(page_id=row["id"], archived=True)
            continue
        target_ids = [mapping[value["id"]] for value in source_relations]
        helper.client.pages.update(
            page_id=row["id"],
            properties={"统一电影清单": utils.get_relation(target_ids)},
        )
    merged = helper.query_all(helper.movie_database_id)
    for row in plan:
        pages = [page for page in merged if page["id"] == mapping[row["source"]["id"]]]
        if len(pages) != 1:
            raise ValueError(f"合并后记录缺失：{row['title']}")
        values = pages[0]["properties"]
        if row["playlist"] not in [
            value["name"] for value in values["片单"]["multi_select"]
        ]:
            raise ValueError(f"合并后标签缺失：{row['title']}")
        if row["watched"] and values["状态"]["status"]["name"] != "看过":
            raise ValueError(f"合并后已看状态丢失：{row['title']}")
    print(f"片单合并验证通过：{len(plan)} 条，原清单与原进度关联均已保留。")


def sync_playlist_progress(helper):
    groups = {}
    for page in helper.query_all(helper.movie_database_id):
        properties = page["properties"]
        phase = (properties.get("阶段") or {}).get("select")
        for tag in (properties.get("片单") or {}).get("multi_select", []):
            scope = tag["name"]
            if scope == "漫威" and phase:
                scope = phase["name"].split("：")[0]
            groups.setdefault(scope, []).append(page["id"])
    rows = helper.query_all(PROGRESS_DATABASE)
    existing = {}
    for row in rows:
        scope = progress_scope(utils.get_property_value(row["properties"]["范围"]))
        if scope in existing:
            raise ValueError(f"进度分组名称重复：{scope}")
        existing[scope] = row
    order = max(
        (row["properties"]["阶段顺序"].get("number") or 0 for row in rows), default=0
    )
    for scope, row in existing.items():
        if scope not in groups and row["properties"]["统一电影清单"]["relation"]:
            helper.client.pages.update(
                page_id=row["id"], properties={"统一电影清单": {"relation": []}}
            )
    for scope, movie_ids in groups.items():
        if len(movie_ids) > 100:
            raise ValueError(f"片单 {scope} 超过单次关联上限，需要拆分进度分组")
        properties = {"统一电影清单": utils.get_relation(movie_ids)}
        row = existing.get(scope)
        if row:
            previous_ids = {
                value["id"] for value in row["properties"]["统一电影清单"]["relation"]
            }
            if previous_ids != set(movie_ids):
                helper.client.pages.update(page_id=row["id"], properties=properties)
        else:
            order += 1
            properties.update(
                {"范围": utils.get_title(scope), "阶段顺序": {"number": order}}
            )
            helper.client.pages.create(
                parent={"database_id": PROGRESS_DATABASE}, properties=properties
            )
    print(f"片单进度关联已核对：{len(groups)} 组。")
