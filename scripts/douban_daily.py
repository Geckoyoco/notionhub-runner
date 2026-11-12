"""Sync public Douban movie interests into the existing Notion template."""

import json
import os
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pendulum

from douban2notion.douban import fetch_subjects
from douban2notion.notion_helper import NotionHelper
from douban2notion import utils


ROOT_PAGE = "496eb242c4a083bfa3ce018cff11f615"
MOVIE_DATABASE = "e19eb242c4a0829f9f6101d5eb286f3f"
STATUS = {"mark": "想看", "doing": "在看", "done": "看过"}


class MovieTemplate(NotionHelper):
    def update_movie_database(self):
        # This template already has URL-typed IMDb and its own relations.
        self._get_database_properties(self.movie_database_id)


def subject_key(url):
    match = re.search(r"/subject/(\d+)", url or "")
    if not match:
        raise ValueError("电影缺少有效豆瓣 subject URL，已停止以避免重复导入")
    return match.group(1)


def configure():
    config = json.loads(os.environ["DOUBAN_FREE_CONFIG"])
    token = config["notion"]["targets"]["movie"]["token"]
    account = config["account"]["name"]
    if not token or not account:
        raise ValueError("现有配置缺少 Notion token 或豆瓣账号")
    os.environ["NOTION_TOKEN"] = token
    os.environ["NOTION_MOVIE_URL"] = f"https://www.notion.so/{ROOT_PAGE}"
    os.environ["MOVIE_DATABASE_ID"] = MOVIE_DATABASE
    return account


def movie_properties(item):
    subject = item["subject"]
    timestamp = pendulum.parse(item["create_time"], tz="Asia/Shanghai")
    timestamp = timestamp.replace(second=0, microsecond=0)
    stars = int((item.get("rating") or {}).get("value") or 0)
    properties = {
        "电影名": utils.get_title(subject["title"]),
        "豆瓣": {"url": subject["url"]},
        "状态": {"status": {"name": STATUS[item["status"]]}},
        "日期": {"date": {"start": timestamp.isoformat()}},
        "短评": {
            "rich_text": [{"text": {"content": (item.get("comment") or "")[:2000]}}]
        },
        "评分": {"select": {"name": "⭐️" * stars} if stars else None},
    }
    return properties, timestamp


def same_value(current, expected):
    kind = next(iter(expected))
    value = current.get(kind)
    target = expected[kind]
    if kind in {"title", "rich_text"}:
        return "".join(
            x.get("plain_text", x.get("text", {}).get("content", ""))
            for x in (value or [])
        ) == "".join(x.get("text", {}).get("content", "") for x in target)
    if kind in {"select", "status"}:
        return (value or {}).get("name") == (target or {}).get("name")
    if kind == "date":
        return bool(value) and pendulum.parse(value["start"]) == pendulum.parse(
            target["start"]
        )
    return value == target


def add_metadata(helper, properties, subject):
    if subject.get("type") in {"movie", "tv"}:
        properties["类型"] = {"select": {"name": subject["type"]}}
    year = str(subject.get("year") or "")
    if year.isdigit():
        properties["上映年份"] = {"number": int(year)}
    score = (subject.get("rating") or {}).get("value")
    if isinstance(score, (int, float)):
        properties["豆瓣评分"] = {"number": score}
    for name, source, database_id in (
        ("分类", "genres", helper.category_database_id),
        ("导演", "directors", helper.director_database_id),
        ("演员", "actors", helper.actor_database_id),
    ):
        relations = []
        for value in subject.get(source) or []:
            title = value.get("name") if isinstance(value, dict) else value
            if title and database_id:
                relations.append(
                    helper.get_relation_id(
                        title,
                        database_id,
                        "https://www.notion.so/icons/user-circle_gray.svg",
                    )
                )
        if relations:
            properties[name] = utils.get_relation(list(dict.fromkeys(relations))[:100])


def sync_movies(helper, interests):
    existing = {}
    for page in helper.query_all(helper.movie_database_id):
        url = (page["properties"].get("豆瓣") or {}).get("url")
        if not url:
            continue
        key = subject_key(url)
        if key in existing:
            raise ValueError(f"Notion 存在重复豆瓣电影 {key}，已停止以避免覆盖错误条目")
        existing[key] = page
    all_database = helper.database_id_dict.get("全部")
    all_relation = []
    if all_database:
        all_pages = helper.query_all(all_database)
        if len(all_pages) > 1:
            raise ValueError("全部统计数据库有多条记录，请先确定汇总记录")
        all_id = (
            all_pages[0]["id"]
            if all_pages
            else helper.get_relation_id(
                "全部", all_database, "https://www.notion.so/icons/bullseye_gray.svg"
            )
        )
        all_relation = [{"id": all_id}]
    created = updated = unchanged = 0
    seen = set()
    for item in interests:
        subject = item["subject"]
        key = subject_key(subject["url"])
        if key in seen:
            raise ValueError(f"豆瓣返回重复 subject {key}，请重试")
        seen.add(key)
        properties, timestamp = movie_properties(item)
        current = existing.get(key)
        if all_relation:
            properties["全部"] = {"relation": all_relation}
        if current and all(
            same_value(current["properties"].get(name, {}), value)
            for name, value in properties.items()
        ):
            unchanged += 1
            continue
        helper.get_date_relation(properties, timestamp)
        if current:
            helper.update_page(current["id"], properties)
            updated += 1
        else:
            add_metadata(helper, properties, subject)
            cover = (subject.get("pic") or {}).get("normal")
            page = helper.create_page(
                {"database_id": helper.movie_database_id},
                properties,
                utils.get_icon(cover) if cover else None,
            )
            existing[key] = page
            created += 1
    summary = f"豆瓣电影同步完成，共 {len(seen)} 条，新增 {created}，更新 {updated}，未变化 {unchanged}。"
    print(summary)
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as file:
        file.write(summary + "\n")


def render_heatmap(interests, year):
    counts = Counter()
    for item in interests:
        if item["status"] == "done":
            day = pendulum.parse(item["create_time"], tz="Asia/Shanghai").date()
            if day.year == year:
                counts[day] += 1
    first = date(year, 1, 1)
    origin = first - timedelta(days=first.weekday())
    last = date(year + 1, 1, 1)
    colors = ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 165"><style>text{{font:12px system-ui;fill:#555}}</style><text x="12" y="22">豆瓣电影 · {year} · 看过 {sum(counts.values())} 部</text>'
    ]
    current = first
    while current < last:
        week = (current - origin).days // 7
        x, y = 28 + week * 14, 54 + current.weekday() * 13
        count = counts[current]
        if current.day == 1:
            parts.append(f'<text x="{x}" y="43">{current.month}月</text>')
        parts.append(
            f'<rect x="{x}" y="{y}" width="11" height="11" rx="2" fill="{colors[min(count, 4)]}"><title>{current.isoformat()} · {count} 部</title></rect>'
        )
        current += timedelta(days=1)
    parts.append("</svg>")
    target = Path("site/douban")
    target.mkdir(parents=True, exist_ok=True)
    svg = "".join(parts)
    (target / "heatmap.svg").write_text(svg)
    (target / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>豆瓣电影热力图</title><style>body{margin:0;padding:8px;font-family:system-ui}svg{display:block;width:100%;height:auto}</style>'
        + svg
    )


def main():
    account = configure()
    helper = MovieTemplate("movie")
    interests = []
    for status in STATUS:
        interests.extend(fetch_subjects(account, "movie", status))
    sync_movies(helper, interests)
    render_heatmap(interests, pendulum.now("Asia/Shanghai").year)


if __name__ == "__main__":
    main()
