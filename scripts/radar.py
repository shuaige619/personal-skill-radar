#!/usr/bin/env python3
"""Public signal collection and evidence-checked shortlists. Python standard library only."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

UTC = timezone.utc
WEIGHTS = {"personal_fit": 35, "repeatability": 25, "feasibility": 25, "differentiation": 15}


def dump(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def canonical_url(url):
    p = urlsplit(url.strip())
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
        raise ValueError("来源必须是无内嵌凭据的 HTTP(S) URL")
    host = p.netloc.lower()
    if host in ("www.skills.sh", "skills.sh"):
        host = "skills.sh"
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("ref", "ref_src")]
    return urlunsplit((p.scheme.lower(), host, p.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


def signal_id(url):
    return "sig-" + hashlib.sha256(canonical_url(url).encode()).hexdigest()[:12]


def plain(text):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


class SkillLinks(HTMLParser):
    """Read rendered leaderboard rows, never execute page scripts."""
    def __init__(self):
        super().__init__()
        self.current = None
        self.rows = []
        self.field = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if re.fullmatch(r"/[^/]+/[^/]+/[^/?#]+", href):
                self.current = {"href": href, "parts": [], "title": [], "provider": []}
        if self.current is not None and tag in ("h3", "p"):
            self.field = "title" if tag == "h3" else "provider"

    def handle_data(self, data):
        if self.current is not None and data.strip():
            self.current["parts"].append(data.strip())
            if self.field:
                self.current[self.field].append(data.strip())

    def handle_endtag(self, tag):
        if tag in ("h3", "p"):
            self.field = None
        if tag == "a" and self.current is not None:
            row = self.current
            self.current = None
            if row["parts"] and row["parts"][0].isdigit() and row["title"]:
                self.rows.append(row)


def parse_skills(body, source):
    parser = SkillLinks()
    parser.feed(body)
    if not parser.rows:
        raise ValueError("榜单页面没有可解析的行；页面可能发生变化")
    records = []
    for row in parser.rows:
        title, provider = " ".join(row["title"]), " ".join(row["provider"])
        keywords = source.get("keywords", [])
        if keywords and not any(k.lower() in title.lower() for k in keywords):
            continue
        url = urljoin("https://skills.sh", row["href"])
        tail = row["parts"][-2:] if source["view"] == "hot" else row["parts"][-1:]
        records.append({
            "id": signal_id(url), "url": canonical_url(url), "title": title,
            "kind": "supply", "author": provider, "actor_key": None,
            "created_at": None, "updated_at": None,
            "summary": "技能榜单记录，不能据此推断任务效果或付费需求。",
            "metrics": {"view": source["view"], "rank": int(row["parts"][0]),
                        "display": " ".join(tail),
                        "meaning": "榜单原始显示值；保留缩写，不折算为精确数量或跨榜相加"},
        })
        if len(records) >= source["limit"]:
            break
    return records


def parse_github(body):
    data = json.loads(body)
    if "items" not in data:
        raise ValueError("GitHub 返回格式异常")
    records = []
    for item in data["items"]:
        if "pull_request" in item:
            continue
        url = item["html_url"]
        author = (item.get("user") or {}).get("login")
        records.append({
            "id": signal_id(url), "url": canonical_url(url), "title": item["title"],
            "kind": "feedback", "author": author,
            "actor_key": "github:" + author.lower() if author else None,
            "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
            "summary": item.get("body") or "", "state": item.get("state"),
            "labels": [x["name"] for x in item.get("labels", [])],
            "metrics": {"comments": item.get("comments"), "reactions": (item.get("reactions") or {}).get("total_count")},
        })
    return records


def parse_hn(body):
    data = json.loads(body)
    if "hits" not in data:
        raise ValueError("HN 返回格式异常")
    records = []
    for item in data["hits"]:
        url = "https://news.ycombinator.com/item?id=" + str(item["objectID"])
        title = item.get("title") or item.get("story_title") or "HN discussion"
        launch = title.lower().startswith(("show hn", "launch hn"))
        records.append({
            "id": signal_id(url), "url": canonical_url(url), "title": title,
            "kind": "launch" if launch else "discussion", "author": item.get("author"),
            "actor_key": "hn:" + item["author"].lower() if item.get("author") else None,
            "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
            "summary": plain(item.get("story_text") or item.get("comment_text")),
            "product_url": item.get("url"),
            "metrics": {"points": item.get("points"), "comments": item.get("num_comments")},
        })
    return records


def source_url(source, since):
    if source["type"] == "skills_html":
        return source["url"]
    if source["type"] == "github_issues":
        return "https://api.github.com/search/issues?" + urlencode({
            "q": source["query"].replace("{since}", since.date().isoformat()),
            "sort": "updated", "order": "desc", "per_page": source["limit"]})
    if source["type"] == "hn_search":
        return "https://hn.algolia.com/api/v1/search_by_date?" + urlencode({
            "query": source["query"], "tags": source.get("tags", "story"),
            "numericFilters": "created_at_i>=" + str(int(since.timestamp())),
            "hitsPerPage": source["limit"]})
    raise ValueError("不支持的数据源类型：" + source["type"])


def request_bytes(req, result):
    """Bounded retries for transient public endpoints; never hide a failed attempt."""
    attempts = result.setdefault("attempts", [])
    for attempt in range(1, 4):
        try:
            with urlopen(req, timeout=25) as response:
                raw = response.read(5_000_001)
                if len(raw) > 5_000_000:
                    raise ValueError("响应超过 5 MB 限制")
                result["final_url"] = response.url
                result["http_status"] = response.status
            attempts.append({"attempt": attempt, "status": "ok", "http_status": response.status})
            return raw
        except (HTTPError, URLError, TimeoutError) as error:
            code = error.code if isinstance(error, HTTPError) else None
            transient = code in (408, 429, 500, 502, 503, 504) or code is None
            transient |= code == 403 and "rate limit" in str(error).lower()
            headers = (error.headers or {}) if isinstance(error, HTTPError) else {}
            delay = float(attempt)
            retry_after = headers.get("Retry-After")
            reset = headers.get("X-RateLimit-Reset")
            try:
                if retry_after:
                    delay = max(0, float(retry_after)) if retry_after.isdigit() else max(0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                elif code == 403 and reset:
                    delay = max(0, float(reset) - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
            retry = transient and attempt < 3 and delay <= 10
            attempts.append({"attempt": attempt, "status": "error", "http_status": code, "error": str(error), "retry": retry, "wait_seconds": delay if retry else None})
            if not retry:
                result["retryable"] = transient
                if transient and delay > 10:
                    result["retry_after_seconds"] = round(delay)
                raise
            time.sleep(delay)
    raise RuntimeError("请求重试未返回结果")


def fetch_source(source, since, run):
    result = {"source_id": source["id"], "type": source["type"]}
    try:
        url = source_url(source, since)
        result["requested_url"] = url
        req = Request(url, headers={"User-Agent": "PersonalSkillRadar/0.1",
                                   "Accept": "application/json,text/html"})
        raw = request_bytes(req, result)
        captured = datetime.now(UTC).isoformat()
        result["captured_at"] = captured
        suffix = ".html" if source["type"] == "skills_html" else ".json"
        raw_path = run / "raw" / (source["id"] + suffix)
        raw_path.write_bytes(raw)
        result["raw_path"] = "raw/" + raw_path.name
        result["sha256"] = hashlib.sha256(raw).hexdigest()
        body = raw.decode("utf-8")
        if source["type"] == "skills_html":
            records = parse_skills(body, source)
        elif source["type"] == "github_issues":
            records = parse_github(body)
            if json.loads(body).get("incomplete_results"):
                result["warning"] = "GitHub 标记搜索结果不完整"
        else:
            records = parse_hn(body)
        for record in records:
            record["observations"] = [{
                "source_id": source["id"], "captured_at": captured,
                "metrics": record.pop("metrics"), "raw_path": result["raw_path"],
            }]
        result.update(status="ok", count=len(records))
        return result, records
    except Exception as error:
        result.update(status="error", error=str(error))
        return result, []


def merge_signals(records):
    merged = {}
    for record in records:
        key = canonical_url(record["url"])
        if key not in merged:
            merged[key] = record
        else:
            merged[key]["observations"].extend(record["observations"])
    return list(merged.values())


def md(text):
    return str(text).replace("\n", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def previous_source(source, output, current_run, maximum_age_seconds=86400):
    """Reuse a recent successful snapshot of the exact same query, with its original time."""
    for path in sorted(Path(output).glob("*/manifest.json"), reverse=True):
        if path.parent == current_run:
            continue
        try:
            manifest = load(path)
            configured = next((s for s in manifest["config"]["sources"] if s["id"] == source["id"]), None)
            if configured != source:
                continue
            previous = next((s for s in manifest["sources"] if s["source_id"] == source["id"] and s["status"] == "ok"), None)
            if not previous:
                continue
            captured = datetime.fromisoformat(previous["captured_at"])
            age = (datetime.now(UTC) - captured).total_seconds()
            if age < 0 or age > maximum_age_seconds:
                continue
            raw_path = (path.parent / previous["raw_path"]).resolve()
            if not raw_path.is_relative_to(path.parent.resolve()):
                continue
            if hashlib.sha256(raw_path.read_bytes()).hexdigest() != previous["sha256"]:
                continue
            records = load(path.parent / "signals.json")
            cache_path = current_run / "raw" / ("cached-" + source["id"] + raw_path.suffix)
            cache_path.write_bytes(raw_path.read_bytes())
            cached = []
            for record in records:
                observations = [o for o in record.get("observations", []) if o.get("source_id") == source["id"]]
                if observations:
                    value = dict(record)
                    value["observations"] = [dict(o, raw_path="raw/" + cache_path.name, cached=True, source_run=path.parent.name) for o in observations]
                    cached.append(value)
            return {"from_run": path.parent.name, "captured_at": previous["captured_at"], "age_seconds": round(age), "count": len(cached), "raw_path": "raw/" + cache_path.name, "sha256": previous["sha256"]}, cached
        except (OSError, ValueError, TypeError, KeyError, StopIteration):
            continue
    return None, []


def collect(config_path, output):
    config = load(config_path)
    if not isinstance(config.get("window_days"), int) or not 1 <= config["window_days"] <= 365:
        raise ValueError("window_days 必须为 1–365")
    ids = [s["id"] for s in config["sources"]]
    if not ids or len(set(ids)) != len(ids) or not all(re.fullmatch(r"[a-z0-9-]+", x) for x in ids):
        raise ValueError("source id 必须唯一，只能使用小写字母、数字和连字符")
    for source in config["sources"]:
        if not isinstance(source.get("limit"), int) or not 1 <= source["limit"] <= 100:
            raise ValueError("每个数据源 limit 必须为 1–100")
    now = datetime.now(UTC)
    since = now - timedelta(days=config["window_days"])
    run = Path(output).resolve() / now.strftime("%Y%m%dT%H%M%S%fZ")
    (run / "raw").mkdir(parents=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        fetched = list(pool.map(lambda source: fetch_source(source, since, run), config["sources"]))
    sources = [x[0] for x in fetched]
    cache_count = 0
    for index, (state, records) in enumerate(fetched):
        if state["status"] != "ok":
            cache, cached = previous_source(config["sources"][index], Path(output).resolve(), run)
            if cache is not None:
                state["cache"] = cache
                records.extend(cached)
                cache_count += len(cached)
    signals = merge_signals([record for _, records in fetched for record in records])
    manifest = {"schema_version": 1, "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "focus": config["focus"], "started_at": now.isoformat(),
                "window_since": since.isoformat(), "config": config, "sources": sources}
    dump(run / "manifest.json", manifest)
    dump(run / "signals.json", signals)
    lines = ["# 公开信号收件箱", "", config["focus"], "",
             "采集时间：" + now.isoformat(),
             "反馈按最近更新筛选，可能包含较早创建的问题；榜单使用其自身统计窗口。",
             "这是候选线索。尚未把问题归类为真实需求，也未验证市场规模。", "",
             "| 数据源 | 状态 | 条数 | 说明 |", "|---|---|---:|---|"]
    for source in sources:
        note = source.get('error', source.get('warning', ''))
        if source.get("cache"):
            note += f"；复用 {source['cache']['captured_at']} 的校验快照 {source['cache']['count']} 条，未当作新采集成功"
        lines.append(f"| {source['source_id']} | {source['status']} | {source.get('count', 0)} | {md(note)} |")
    lines += ["", "| ID | 类型 | 线索 | 创建日期 |", "|---|---|---|---|"]
    for record in signals:
        lines.append(f"| {record['id']} | {record['kind']} | [{md(record['title'])}]({record['url']}) | {record.get('created_at') or '未知'} |")
    (run / "inbox.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    errors = sum(s["status"] != "ok" for s in sources)
    print(json.dumps({"run": str(run), "signals": len(signals), "sources_ok": len(sources)-errors,
                      "sources_failed": errors, "cached_records": cache_count}, ensure_ascii=False))
    return 0 if not errors else (2 if signals else 3)


def assess_card(card, signals):
    required = ("id", "title", "user", "scenario", "input", "output", "gap",
                "differentiation", "cost", "next_step", "evidence", "ratings", "success_checks")
    missing = [k for k in required if k not in card or not card[k]]
    if missing:
        raise ValueError(f"{card.get('id', 'card')} 缺字段：{', '.join(missing)}")
    if not re.fullmatch(r"[a-z0-9-]+", card["id"]):
        raise ValueError("需求卡 ID 只能使用小写字母、数字和连字符")
    if not isinstance(card["success_checks"], list) or not all(isinstance(x, str) and x for x in card["success_checks"]):
        raise ValueError("success_checks 必须是非空字符串列表")
    score = 0
    for key, weight in WEIGHTS.items():
        rating = card["ratings"].get(key, {})
        n = rating.get("score")
        if type(n) is not int or not 1 <= n <= 5 or not rating.get("reason"):
            raise ValueError(f"{card['id']} 的 {key} 需要 1–5 分和理由")
        score += weight * n / 5
    actors, seen = set(), set()
    direct = 0
    for evidence in card["evidence"]:
        sid = evidence["signal_id"]
        if sid not in signals:
            raise ValueError("证据 ID 不存在：" + sid)
        if sid in seen:
            raise ValueError("同一需求卡中重复引用证据：" + sid)
        seen.add(sid)
        relation = evidence.get("relationship")
        if relation not in ("direct_request", "supporting_supply", "context", "resolved_feedback"):
            raise ValueError("未知证据关系：" + str(relation))
        if not evidence.get("note"):
            raise ValueError("每条证据必须说明如何支持或限制需求")
        record = signals[sid]
        if relation == "direct_request":
            if record["kind"] not in ("feedback", "discussion"):
                raise ValueError("安装榜单或产品发布不能直接算作用户需求")
            if record.get("state") == "closed":
                raise ValueError("已关闭反馈需重新核实，不能直接算作未满足需求")
            direct += 1
            actor = record.get("actor_key")
            if actor:
                actors.add(actor)
    readiness = ("重复反馈，待任务验证" if len(actors) >= 2 else
                 "未获得多账号证据，待补证据" if direct else "探索方向，需求未验证")
    return {"score": round(score, 1), "public_accounts": len(actors),
            "direct_reports": direct, "readiness": readiness}


def rank(signals_path, cards_path, output):
    records = load(signals_path)
    signals = {r["id"]: r for r in records}
    if len(signals) != len(records):
        raise ValueError("signals 中存在重复 ID")
    cards = load(cards_path)
    if not isinstance(cards, list) or not cards:
        raise ValueError("需求卡必须是非空列表")
    if len({c["id"] for c in cards}) != len(cards):
        raise ValueError("需求卡 ID 重复")
    reviewed = [(card, assess_card(card, signals)) for card in cards]
    reviewed.sort(key=lambda x: x[1]["score"], reverse=True)
    lines = ["# 个人技能候选", "",
             "评分是研究者对个人适用性的判断：匹配 35%、复用 25%、可实现 25%、差异化 15%。",
             "分数不代表热度、市场规模或付费意愿。不同榜单与平台指标不相加。", "",
             "| 排序 | 候选 | 工作优先级 / 100 | 证据状态 |", "|---:|---|---:|---|"]
    for index, (card, result) in enumerate(reviewed, 1):
        lines.append(f"| {index} | {md(card['title'])} | {result['score']} | {result['readiness']} |")
    for card, result in reviewed:
        lines += ["", "## " + card["title"], "", "**ID：** " + card["id"]]
        for field, label in (("user", "用户"), ("scenario", "场景"), ("input", "输入"),
                             ("output", "输出"), ("gap", "现有方案缺口"),
                             ("differentiation", "个人差异化"), ("cost", "成本与依赖"),
                             ("next_step", "下一步")):
            lines.append(f"- **{label}：** {card[field]}")
        lines += ["", "验收标准：", ""]
        lines += ["- " + check for check in card["success_checks"]]
        lines += ["", "证据与限制：", ""]
        for evidence in card["evidence"]:
            r = signals[evidence["signal_id"]]
            lines.append(f"- [{md(r['title'])}]({r['url']}) · {evidence['relationship']} · 创建 {r.get('created_at') or '未知'}，更新 {r.get('updated_at') or '未知'}。{evidence['note']}")
        lines += ["", "评分理由：", ""]
        for key, rating in card["ratings"].items():
            lines.append(f"- {key}：{rating['score']}/5。{rating['reason']}")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    dump(Path(output).with_suffix(".json"), [
        {"id": card["id"], "title": card["title"], **result} for card, result in reviewed])
    print(json.dumps({"report": str(Path(output).resolve()), "cards": len(cards)}, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect", help="采集公开榜单、GitHub 反馈和 HN 讨论")
    c.add_argument("--config", required=True)
    c.add_argument("--output", default="data/runs")
    r = sub.add_parser("rank", help="校验证据和人工需求卡，生成候选报告")
    r.add_argument("--signals", required=True)
    r.add_argument("--cards", required=True)
    r.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        return collect(args.config, args.output) if args.command == "collect" else rank(args.signals, args.cards, args.output)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print("错误：" + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
