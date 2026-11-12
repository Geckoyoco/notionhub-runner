"""Sync public Douban movie interests into the existing Notion template."""

import json
import os
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pendulum
from notion_client import Client
from retrying import retry

from douban2notion.douban import fetch_subjects
from douban2notion import douban as source_api
from douban2notion.notion_helper import NotionHelper
from douban2notion import utils
from watchlists import matching_movies, merge_watchlists, sync_playlist_progress


ROOT_PAGE = "496eb242c4a083bfa3ce018cff11f615"
MOVIE_DATABASE = "e19eb242c4a0829f9f6101d5eb286f3f"
STATUS = {"mark": "想看", "doing": "在看", "done": "看过"}


class MovieTemplate(NotionHelper):
    def __init__(self, type):
        self.relation_ids = {}
        super().__init__(type)

    def update_movie_database(self):
        # This template already has URL-typed IMDb and its own relations.
        self._get_database_properties(self.movie_database_id)

    def get_relation_id(self, name, id, icon, properties=None):
        # Load each small related database once instead of querying every name.
        if id not in self.relation_ids:
            self.relation_ids[id] = {
                utils.get_property_value(page["properties"]["标题"]): page["id"]
                for page in self.query_all(id)
            }
        if name not in self.relation_ids[id]:
            values = dict(properties or {})
            values["标题"] = utils.get_title(name)
            page = self.client.pages.create(
                parent={"database_id": id},
                properties=values,
                icon=utils.get_icon(icon),
            )
            self.relation_ids[id][name] = page["id"]
        return self.relation_ids[id][name]


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
    # The library reads its environment at import time, before configure().
    source_api.AUTH_TOKEN = config["account"].get("authToken") or None
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


def poster_url(subject):
    picture = subject.get("pic") or {}
    return picture.get("large") or picture.get("normal") or subject.get("cover_url")


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def fill_poster(helper, page, subject):
    url = poster_url(subject)
    if not url:
        return False
    media = {}
    if not page.get("cover"):
        media["cover"] = utils.get_icon(url)
    if not page.get("icon"):
        media["icon"] = utils.get_icon(url)
    if media:
        saved = helper.client.pages.update(page_id=page["id"], **media)
        page.update({key: saved[key] for key in media})
    return "cover" in media


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def public_movie_request(path, **params):
    response = source_api.requests.get(
        f"https://{source_api.DOUBAN_API_HOST}/api/v2/{path}",
        headers=source_api._request_headers(True),
        params={**params, "apikey": source_api.DOUBAN_API_KEY},
        timeout=source_api.DOUBAN_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def sync_plan_posters(helper):
    filled = 0
    unresolved = []
    for page in helper.query_all(helper.movie_database_id):
        props = page["properties"]
        labels = [item["name"] for item in props["片单"]["multi_select"]]
        if page.get("cover") or not labels:
            continue
        url = props["豆瓣"].get("url")
        title = utils.get_property_value(props["电影名"])
        year = props["上映年份"].get("number")
        if url:
            subject = public_movie_request(f"movie/{subject_key(url)}")
        else:
            results = public_movie_request("search/movie", q=title)
            matches = []
            for result in results.get("items", []):
                candidate = result.get("target") or {}
                if result.get("target_type") != "movie" or not candidate.get("year"):
                    continue
                if not matching_movies(
                    [page], candidate.get("title", ""), candidate["year"]
                ):
                    continue
                if "诺兰" in labels and "克里斯托弗·诺兰" not in candidate.get(
                    "card_subtitle", ""
                ):
                    continue
                matches.append(candidate)
            if len(matches) != 1:
                unresolved.append(title)
                continue
            subject = public_movie_request(f"movie/{matches[0]['id']}")
            if not matching_movies(
                [page], subject.get("title", ""), subject.get("year")
            ):
                raise ValueError(f"封面电影详情不匹配：{title} / {year}")
            helper.update_page(page["id"], {"豆瓣": {"url": subject["url"]}})
        filled += int(fill_poster(helper, page, subject))
    print(
        f"计划片单封面补齐 {filled} 部，待确认 {len(unresolved)} 部：{', '.join(unresolved)}"
    )


def sync_movies(helper, interests):
    existing = {}
    local_plans = []
    for page in helper.query_all(helper.movie_database_id):
        url = (page["properties"].get("豆瓣") or {}).get("url")
        if not url:
            if (page["properties"].get("原片单记录") or {}).get("url") or (
                page["properties"].get("片单") or {}
            ).get("multi_select"):
                local_plans.append(page)
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
    created = updated = unchanged = covers = 0
    seen = set()
    for item in interests:
        subject = item["subject"]
        key = subject_key(subject["url"])
        if key in seen:
            raise ValueError(f"豆瓣返回重复 subject {key}，请重试")
        seen.add(key)
        properties, timestamp = movie_properties(item)
        current = existing.get(key)
        if not current:
            matches = matching_movies(
                local_plans, subject["title"], subject.get("year")
            )
            if len(matches) > 1:
                raise ValueError(f"本地计划电影匹配不唯一：{subject['title']}")
            if matches:
                current = matches[0]
                local_plans.remove(current)
                existing[key] = current
        if all_relation:
            properties["全部"] = {"relation": all_relation}
        helper.get_date_relation(properties, timestamp)
        if current:
            covers += int(fill_poster(helper, current, subject))
        if current and all(
            same_value(current["properties"].get(name, {}), value)
            for name, value in properties.items()
        ):
            unchanged += 1
            continue
        if current:
            helper.update_page(current["id"], properties)
            updated += 1
        else:
            add_metadata(helper, properties, subject)
            cover = poster_url(subject)
            page = helper.create_page(
                {"database_id": helper.movie_database_id},
                properties,
                utils.get_icon(cover) if cover else None,
            )
            existing[key] = page
            covers += int(fill_poster(helper, page, subject))
            created += 1
    summary = f"豆瓣电影同步完成，共 {len(seen)} 条，新增 {created}，更新 {updated}，未变化 {unchanged}，补齐封面 {covers}。"
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
    connection = Client(auth=os.environ["NOTION_TOKEN"]).users.me()
    print(f"使用现有 Notion 连接：{connection.get('name')}")
    helper = MovieTemplate("movie")
    merge_mode = os.getenv("MERGE_WATCHLISTS", "false")
    if merge_mode in {"preview", "apply"}:
        merge_watchlists(helper, apply=merge_mode == "apply")
        if merge_mode == "preview":
            return
    interests = []
    for status in STATUS:
        interests.extend(fetch_subjects(account, "movie", status))
    sync_movies(helper, interests)
    sync_plan_posters(helper)
    sync_playlist_progress(helper)
    render_heatmap(interests, pendulum.now("Asia/Shanghai").year)


if __name__ == "__main__":
    main()
