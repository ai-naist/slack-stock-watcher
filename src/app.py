import hashlib
import hmac
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import boto3
import requests
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")
RUN_NEWS_COMMANDS = {
    "/run_news",
    "/run_news_dev",
    "/run_stock_news",
    "/run_stock_news_dev",
}
RUN_MASTER_COMMANDS = {
    "/run_master",
    "/run_master_dev",
    "/run_stock_master",
    "/run_stock_master_dev",
}
ADD_STOCK_COMMANDS = {
    "/add_stock",
    "/add_stock_dev",
}
MASTER_META_ID = "__META__"
MASTER_META_SNAPSHOT_KEY = "LATEST"
NEWS_SENT_PREFIX = "__NEWS__#"
NEWS_SENT_LATEST = "LATEST"


def get_env_int(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def parse_stock_input(text):
    normalized = " ".join(text.split())
    if not normalized:
        return "", ""

    parts = normalized.split(" ", 1)
    stock_code = normalize_stock_code(parts[0])
    stock_name = parts[1].strip() if len(parts) > 1 else ""
    return stock_code, stock_name


def normalize_stock_code(value):
    stripped = (value or "").strip().upper()
    if not stripped:
        return ""

    digits_only = "".join(ch for ch in stripped if ch.isdigit())
    if stripped == digits_only and len(digits_only) == 5 and digits_only.endswith("0"):
        return digits_only[:-1]
    return stripped


def normalize_text(text):
    return "".join((text or "").strip().lower().split())


def extract_search_candidates(text):
    normalized = " ".join((text or "").split())
    if not normalized:
        return []

    candidates = [normalized]
    first_token = normalized.split(" ", 1)[0]
    if first_token and first_token != normalized:
        candidates.insert(0, first_token)
    return candidates


def extract_stock_code_variants(value):
    stripped = (value or "").strip().upper()
    canonical = normalize_stock_code(stripped)
    if not canonical:
        return []

    variants = [canonical]
    if stripped and stripped not in variants:
        variants.append(stripped)

    digits_only = "".join(ch for ch in canonical if ch.isdigit())
    if digits_only:
        if digits_only not in variants:
            variants.append(digits_only)
        if len(digits_only) == 4:
            padded = f"{digits_only}0"
            if padded not in variants:
                variants.append(padded)
    return variants


def get_jquants_api_key():
    return os.environ.get("JQUANTS_API_KEY", "").strip()


def get_jquants_base_url():
    configured = os.environ.get("JQUANTS_BASE_URL", "https://api.jquants.com/v2").strip()
    if not configured:
        return "https://api.jquants.com/v2"

    normalized = configured.rstrip("/")
    if normalized == "https://api.jquants.com":
        return "https://api.jquants.com/v2"
    return normalized


def fetch_jquants_listed_info(api_key, timeout_seconds):
    if not api_key:
        return []

    base_url = get_jquants_base_url()
    endpoint = f"{base_url.rstrip('/')}/equities/master"
    headers = {"x-api-key": api_key}

    listed = []
    pagination_key = None
    while True:
        params = {}
        if pagination_key:
            params["pagination_key"] = pagination_key

        response = requests.get(
            endpoint, headers=headers, params=params, timeout=timeout_seconds
        )
        response.raise_for_status()
        payload = response.json()

        page_items = payload.get("data")
        if page_items is None:
            page_items = payload.get("info")
        if page_items is None:
            page_items = []

        listed.extend(page_items)
        pagination_key = payload.get("pagination_key")
        if not pagination_key:
            break

    return listed


def get_master_snapshot(table):
    response = table.get_item(
        Key={"StockCode": MASTER_META_ID, "SnapshotAt": MASTER_META_SNAPSHOT_KEY}
    )
    item = response.get("Item", {})
    return (item.get("CurrentSnapshotAt") or "").strip()


def list_master_records_for_snapshot(table, snapshot_at):
    if not snapshot_at:
        return []

    records = []
    scan_kwargs = {
        "FilterExpression": Attr("SnapshotAt").eq(snapshot_at)
        & Attr("StockCode").ne(MASTER_META_ID)
    }

    while True:
        response = table.scan(**scan_kwargs)
        records.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    return records


def find_stock_in_master(master_records, raw_query):
    candidates = []
    for candidate in extract_search_candidates(raw_query):
        if candidate not in candidates:
            candidates.append(candidate)

    if not candidates:
        return {"status": "not_found", "items": []}

    by_code = {}
    by_name_exact = {}
    by_name_contains = {}

    for query in candidates:
        code_variants = extract_stock_code_variants(query)
        norm_query = normalize_text(query)

        for item in master_records:
            code = get_master_row_code(item)
            if not code:
                continue

            co_name = (item.get("CoName") or item.get("StockName") or "").strip()
            co_name_en = (item.get("CoNameEn") or "").strip()
            norm_name = normalize_text(item.get("NormalizedCoName") or co_name)
            norm_name_en = normalize_text(item.get("NormalizedCoNameEn") or co_name_en)

            if code_variants:
                for variant in code_variants:
                    if code == variant or code.rstrip("0") == variant:
                        by_code[code] = {
                            "code": code,
                            "name": co_name,
                            "name_en": co_name_en,
                        }

            if not norm_query:
                continue

            if norm_query == norm_name or norm_query == norm_name_en:
                by_name_exact[code] = {
                    "code": code,
                    "name": co_name,
                    "name_en": co_name_en,
                }
            elif norm_query in norm_name or norm_query in norm_name_en:
                by_name_contains[code] = {
                    "code": code,
                    "name": co_name,
                    "name_en": co_name_en,
                }

    if len(by_code) == 1:
        return {"status": "single", "items": list(by_code.values())}
    if len(by_code) > 1:
        return {
            "status": "multiple",
            "items": sorted(by_code.values(), key=lambda x: x["code"])[:5],
        }

    if len(by_name_exact) == 1:
        return {"status": "single", "items": list(by_name_exact.values())}
    if len(by_name_exact) > 1:
        return {
            "status": "multiple",
            "items": sorted(by_name_exact.values(), key=lambda x: x["code"])[:5],
        }

    if len(by_name_contains) == 1:
        return {"status": "single", "items": list(by_name_contains.values())}
    if len(by_name_contains) > 1:
        return {
            "status": "multiple",
            "items": sorted(by_name_contains.values(), key=lambda x: x["code"])[:5],
        }

    return {"status": "not_found", "items": []}


def build_master_payload(row):
    co_name = (row.get("CoName") or "").strip()
    co_name_en = (row.get("CoNameEn") or "").strip()
    return {
        "CoName": co_name,
        "CoNameEn": co_name_en,
        "S17": str(row.get("S17") or ""),
        "S17Nm": str(row.get("S17Nm") or ""),
        "S33": str(row.get("S33") or ""),
        "S33Nm": str(row.get("S33Nm") or ""),
        "Mkt": str(row.get("Mkt") or ""),
        "MktNm": str(row.get("MktNm") or ""),
        "Mrgn": str(row.get("Mrgn") or ""),
        "MrgnNm": str(row.get("MrgnNm") or ""),
        "NormalizedCoName": normalize_text(co_name),
        "NormalizedCoNameEn": normalize_text(co_name_en),
    }


def get_master_row_code(row):
    return normalize_stock_code(row.get("Code") or row.get("StockCode") or "")


def write_master_snapshot(table, listed_info, snapshot_at):
    count = 0
    with table.batch_writer() as batch:
        for row in listed_info:
            code = get_master_row_code(row)
            if not code:
                continue

            item = {
                "StockCode": code,
                "SnapshotAt": snapshot_at,
            }
            item.update(build_master_payload(row))
            batch.put_item(Item=item)
            count += 1

        batch.put_item(
            Item={
                "StockCode": MASTER_META_ID,
                "SnapshotAt": MASTER_META_SNAPSHOT_KEY,
                "CurrentSnapshotAt": snapshot_at,
                "UpdatedAt": snapshot_at,
                "Count": count,
            }
        )

    return count


def clear_master_table(table):
    keys = []
    scan_kwargs = {
        "ProjectionExpression": "StockCode, SnapshotAt",
    }

    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            stock_code = (item.get("StockCode") or "").strip()
            snapshot_at = (item.get("SnapshotAt") or "").strip()
            if stock_code and snapshot_at:
                keys.append({"StockCode": stock_code, "SnapshotAt": snapshot_at})

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    if not keys:
        return 0

    with table.batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key=key)

    return len(keys)


def initialize_stock_master(timeout_seconds):
    master_table_name = os.environ.get("MASTER_TABLE_NAME", "").strip()
    if not master_table_name:
        return {
            "synced": False,
            "mode": "init",
            "reason": "master table not configured",
            "count": 0,
            "upserted": 0,
            "deleted": 0,
        }

    api_key = get_jquants_api_key()
    if not api_key:
        return {
            "synced": False,
            "mode": "init",
            "reason": "api key not available",
            "count": 0,
            "upserted": 0,
            "deleted": 0,
        }

    listed_info = fetch_jquants_listed_info(api_key, timeout_seconds)
    if not listed_info:
        return {
            "synced": False,
            "mode": "init",
            "reason": "listed info empty",
            "count": 0,
            "upserted": 0,
            "deleted": 0,
        }

    snapshot_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    table = dynamodb.Table(master_table_name)
    deleted_count = clear_master_table(table)
    upserted_count = write_master_snapshot(table, listed_info, snapshot_at)

    return {
        "synced": True,
        "mode": "init",
        "reason": "ok",
        "count": upserted_count,
        "upserted": upserted_count,
        "deleted": deleted_count,
    }


def apply_stock_master_diff(timeout_seconds):
    master_table_name = os.environ.get("MASTER_TABLE_NAME", "").strip()
    if not master_table_name:
        return {
            "synced": False,
            "mode": "diff",
            "reason": "master table not configured",
            "count": 0,
            "upserted": 0,
            "deleted": 0,
        }

    api_key = get_jquants_api_key()
    if not api_key:
        return {
            "synced": False,
            "mode": "diff",
            "reason": "api key not available",
            "count": 0,
            "upserted": 0,
            "deleted": 0,
        }

    listed_info = fetch_jquants_listed_info(api_key, timeout_seconds)
    if not listed_info:
        return {
            "synced": False,
            "mode": "diff",
            "reason": "listed info empty",
            "count": 0,
            "upserted": 0,
            "deleted": 0,
        }

    table = dynamodb.Table(master_table_name)
    snapshot_at = get_master_snapshot(table)
    if not snapshot_at:
        return initialize_stock_master(timeout_seconds)

    existing_records = list_master_records_for_snapshot(table, snapshot_at)
    existing_map = {}
    for item in existing_records:
        code = get_master_row_code(item)
        if code:
            existing_map[code] = item

    latest_map = {}
    for row in listed_info:
        code = get_master_row_code(row)
        if not code:
            continue
        latest_map[code] = build_master_payload(row)

    upsert_items = []
    for code, payload in latest_map.items():
        existing = existing_map.get(code)
        if existing is None:
            upsert_items.append((code, payload))
            continue

        current_payload = build_master_payload(existing)
        if payload != current_payload:
            upsert_items.append((code, payload))

    delete_codes = []
    for code in existing_map.keys():
        if code not in latest_map:
            delete_codes.append(code)

    with table.batch_writer() as batch:
        for code, payload in upsert_items:
            item = {"StockCode": code, "SnapshotAt": snapshot_at}
            item.update(payload)
            batch.put_item(Item=item)

        for code in delete_codes:
            batch.delete_item(Key={"StockCode": code, "SnapshotAt": snapshot_at})

        updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        batch.put_item(
            Item={
                "StockCode": MASTER_META_ID,
                "SnapshotAt": MASTER_META_SNAPSHOT_KEY,
                "CurrentSnapshotAt": snapshot_at,
                "UpdatedAt": updated_at,
                "Count": len(latest_map),
                "LastMode": "diff",
                "Upserted": len(upsert_items),
                "Deleted": len(delete_codes),
            }
        )

    return {
        "synced": True,
        "mode": "diff",
        "reason": "ok",
        "count": len(latest_map),
        "upserted": len(upsert_items),
        "deleted": len(delete_codes),
    }


def sync_stock_master(timeout_seconds):
    return apply_stock_master_diff(timeout_seconds)


def enqueue_slack_command(command_name, text_param, response_url):
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
    if not function_name:
        return False

    payload = {
        "source": "internal-slack-command",
        "detail-type": "SlackCommand",
        "command_name": command_name,
        "text_param": text_param,
        "response_url": response_url,
    }
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    return True


def enqueue_master_command(action):
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
    if not function_name:
        return False

    payload = {
        "source": "internal-master-command",
        "detail-type": "MasterCommand",
        "master_action": action,
    }
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    return True


def enqueue_add_stock_command(command_name, text_param, response_url):
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
    if not function_name:
        return False

    payload = {
        "source": "internal-add-stock-command",
        "detail-type": "AddStockCommand",
        "command_name": command_name,
        "text_param": text_param,
        "response_url": response_url,
    }
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    return True


def execute_add_stock_command(text_param):
    if not text_param:
        return {
            "status": "invalid",
            "message": "検索キーを指定してください．例: /add_stock 7203 または /add_stock フィックスターズ",
        }

    stock_code = ""
    stock_name = ""
    stock_name_en = ""

    master_table_name = os.environ.get("MASTER_TABLE_NAME", "").strip()
    if master_table_name:
        master_table = dynamodb.Table(master_table_name)
        snapshot_at = get_master_snapshot(master_table)
        master_records = list_master_records_for_snapshot(master_table, snapshot_at)
        lookup = find_stock_in_master(master_records, text_param)

        if lookup["status"] == "multiple":
            lines = ["候補が複数あります．証券コードで指定してください．"]
            for item in lookup["items"]:
                label = f"{item['code']} {item['name']}"
                if item["name_en"]:
                    label = f"{label} ({item['name_en']})"
                lines.append(f"・{label}")
            return {"status": "multiple", "message": "\n".join(lines)}

        if lookup["status"] == "single":
            matched = lookup["items"][0]
            stock_code = normalize_stock_code(matched["code"])
            stock_name = matched["name"]
            stock_name_en = matched["name_en"]
        else:
            return {
                "status": "not_found",
                "message": "銘柄マスタに該当がありませんでした．定期同期後に再試行してください．",
            }
    else:
        parsed_code, parsed_name = parse_stock_input(text_param)
        if not parsed_code:
            return {
                "status": "invalid",
                "message": "検索キーを指定してください．",
            }
        stock_code = normalize_stock_code(parsed_code)
        stock_name = parsed_name

    table_name = os.environ.get("TABLE_NAME")
    table = dynamodb.Table(table_name)

    timestamp_str = "LATEST"

    existing_response = table.get_item(
        Key={
            "StockID": stock_code,
            "Timestamp": timestamp_str,
        }
    )
    existing = None
    if isinstance(existing_response, dict):
        existing = existing_response.get("Item")

    if existing:
        name_label = (existing.get("StockName") or stock_name or "（未設定）").strip() or "（未設定）"
        name_en_label = (existing.get("StockNameEn") or stock_name_en or "（未設定）").strip() or "（未設定）"
        return {
            "status": "already_registered",
            "message": (
                "銘柄はすでに登録済みです．"
                f"\n証券コード: {stock_code}"
                f"\n企業名: {name_label}"
                f"\n企業名(英語): {name_en_label}"
            ),
        }

    table.put_item(
        Item={
            "StockID": stock_code,
            "StockCode": stock_code,
            "StockName": stock_name,
            "StockNameEn": stock_name_en,
            "Timestamp": timestamp_str,
        }
    )

    name_label = stock_name if stock_name else "（未設定）"
    name_en_label = stock_name_en if stock_name_en else "（未設定）"
    return {
        "status": "ok",
        "message": (
            "銘柄を登録しました．"
            f"\n証券コード: {stock_code}"
            f"\n企業名: {name_label}"
            f"\n企業名(英語): {name_en_label}"
        ),
    }


def execute_master_command(action):
    timeout_seconds = get_env_int("HTTP_TIMEOUT_SECONDS", 5)

    if action in {"init", "initialize", "reset"}:
        result = initialize_stock_master(timeout_seconds)
        return {
            "status": "ok",
            "message": (
                "銘柄マスタ初期化を実行しました．"
                f"\n結果: {result['reason']}"
                f"\n登録件数: {result['upserted']}件"
                f"\n削除件数: {result['deleted']}件"
            ),
        }

    if action in {"diff", "update", "patch"}:
        result = apply_stock_master_diff(timeout_seconds)
        return {
            "status": "ok",
            "message": (
                "銘柄マスタ差分適用を実行しました．"
                f"\n結果: {result['reason']}"
                f"\n適用件数: {result['upserted']}件"
                f"\n削除件数: {result['deleted']}件"
                f"\n有効件数: {result['count']}件"
            ),
        }

    return {
        "status": "invalid",
        "message": "実行方法: /run_master init または /run_master diff",
    }


def build_search_query(stock_code, stock_name):
    tokens = [f'"{stock_code}"']
    if stock_name:
        tokens.append(f'"{stock_name}"')
    return " OR ".join(tokens)


def parse_datetime_safe(value):
    text = (value or "").strip()
    if not text:
        return None

    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    iso_text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def is_recent_news(published_at, lookback_days, now_utc):
    if lookback_days <= 0:
        return True

    published_dt = parse_datetime_safe(published_at)
    if published_dt is None:
        return True

    cutoff = now_utc - timedelta(days=lookback_days)
    return published_dt >= cutoff


def parse_rss_items(
    xml_text,
    source_name,
    stock_code,
    stock_name,
    max_items=None,
    lookback_days=7,
    now_utc=None,
):
    items = []
    now_utc = now_utc or datetime.now(timezone.utc)
    root = ET.fromstring(xml_text)
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        published_at = (item.findtext("pubDate") or "").strip()

        if not title or not url:
            continue

        if not is_recent_news(published_at, lookback_days, now_utc):
            continue

        items.append(
            {
                "source": source_name,
                "title": title,
                "url": url,
                "published_at": published_at,
                "stock_code": stock_code,
                "stock_name": stock_name,
            }
        )

        if max_items is not None and max_items > 0 and len(items) >= max_items:
            break

    return items


def normalize_url(url):
    parsed = urlsplit(url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered_pairs = []
    for key, value in query_pairs:
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in {"gclid", "fbclid"}:
            continue
        filtered_pairs.append((key, value))

    normalized_query = urlencode(filtered_pairs, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, normalized_query, ""))


def normalize_title(title):
    normalized = " ".join((title or "").strip().split())
    if not normalized:
        return ""
    # 末尾の区切り記号以降が短い場合は媒体名の付与とみなして削除する
    return re.sub(r"\s+[|\-｜－—–]\s+[^|\-｜－—–]{1,20}$", "", normalized)


def is_similar_title(a, b, threshold=0.88):
    if not a or not b:
        return False

    if a == b:
        return True

    a_norm = a.casefold()
    b_norm = b.casefold()

    shorter = min(len(a_norm), len(b_norm))
    longer = max(len(a_norm), len(b_norm))
    if shorter < 16:
        return False
    if longer == 0 or (longer - shorter) / longer > 0.25:
        return False

    return SequenceMatcher(None, a_norm, b_norm).ratio() >= threshold


def deduplicate_news(items):
    deduped = []
    seen_urls = set()
    seen_titles = []

    for item in items:
        title = item.get("title") or ""
        normalized_title = normalize_title(title)

        url = (item.get("url") or "").strip()
        normalized_url = normalize_url(url)

        is_duplicate = False

        if normalized_url and normalized_url in seen_urls:
            is_duplicate = True

        if not is_duplicate and normalized_title:
            for existing_title in seen_titles:
                if is_similar_title(normalized_title, existing_title):
                    is_duplicate = True
                    break

        if not is_duplicate and not normalized_title and not normalized_url:
            continue

        if is_duplicate:
            continue

        if normalized_url:
            seen_urls.add(normalized_url)
        if normalized_title:
            seen_titles.append(normalized_title)

        copied = dict(item)
        copied["url"] = normalized_url
        deduped.append(copied)

    return deduped


def fetch_registered_stocks():
    table_name = os.environ.get("TABLE_NAME")
    if not table_name:
        return []

    table = dynamodb.Table(table_name)
    stock_map = {}
    scan_kwargs = {}

    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            code = normalize_stock_code(item.get("StockCode") or item.get("StockID") or "")
            if not code:
                continue

            name = (item.get("StockName") or "").strip()
            timestamp = str(item.get("Timestamp") or "")
            existing = stock_map.get(code)

            if existing is None or timestamp >= existing.get("timestamp", ""):
                stock_map[code] = {"code": code, "name": name, "timestamp": timestamp}

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    normalized_stocks = []
    for code in sorted(stock_map.keys()):
        normalized_stocks.append(
            {"code": stock_map[code]["code"], "name": stock_map[code]["name"]}
        )
    return normalized_stocks


def fetch_yahoo_finance_rss(stock, max_items, timeout_seconds):
    endpoint = "https://feeds.finance.yahoo.com/rss/2.0/headline"
    params = {"s": stock["code"], "region": "JP", "lang": "ja-JP"}

    response = requests.get(endpoint, params=params, timeout=timeout_seconds)
    response.raise_for_status()
    return parse_rss_items(
        response.text,
        "yahoo_finance_rss",
        stock["code"],
        stock["name"],
        max_items=max_items,
    )


def fetch_google_news_rss(stock, max_items, timeout_seconds):
    endpoint = "https://news.google.com/rss/search"
    query = build_search_query(stock["code"], stock["name"])
    params = {"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"}

    response = requests.get(endpoint, params=params, timeout=timeout_seconds)
    response.raise_for_status()
    return parse_rss_items(
        response.text,
        "google_news_rss",
        stock["code"],
        stock["name"],
        max_items=max_items,
    )


def fetch_newsapi_news(
    query,
    max_items,
    timeout_seconds,
    source_label,
    stock_code,
    stock_name,
    lookback_days=7,
    now_utc=None,
):
    api_key = os.environ.get("NEWS_API_KEY", "").strip()
    if not api_key:
        return []

    endpoint = os.environ.get("NEWS_API_URL", "https://newsapi.org/v2/everything")
    now_utc = now_utc or datetime.now(timezone.utc)
    from_date = (now_utc - timedelta(days=lookback_days)).date().isoformat()
    params = {
        "q": query,
        "language": "ja",
        "sortBy": "publishedAt",
        "from": from_date,
        "apiKey": api_key,
    }
    if max_items is not None and max_items > 0:
        params["pageSize"] = max_items

    response = requests.get(endpoint, params=params, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()

    items = []
    for article in payload.get("articles", []):
        title = (article.get("title") or "").strip()
        url = (article.get("url") or "").strip()
        published_at = (article.get("publishedAt") or "").strip()
        if not title or not url:
            continue

        if not is_recent_news(published_at, lookback_days, now_utc):
            continue

        items.append(
            {
                "source": source_label,
                "title": title,
                "url": url,
                "published_at": published_at,
                "stock_code": stock_code,
                "stock_name": stock_name,
            }
        )

        if max_items is not None and max_items > 0 and len(items) >= max_items:
            break

    return items


def fetch_market_overview_news(stocks, max_items, timeout_seconds):
    tokens = []
    for stock in stocks[:5]:
        tokens.append(f'"{stock["code"]}"')
        if stock["name"]:
            tokens.append(f'"{stock["name"]}"')

    if tokens:
        query = " OR ".join(tokens)
    else:
        query = "株式 OR 市場 OR 決算"

    return fetch_newsapi_news(
        query=query,
        max_items=max_items,
        timeout_seconds=timeout_seconds,
        source_label="newsapi_market",
        stock_code="MARKET",
        stock_name="全体関連",
    )


def build_news_dedupe_id(item):
    stock_code = (item.get("stock_code") or "").strip().upper()
    title = item.get("title") or ""
    normalized_title = normalize_title(title)

    if normalized_title:
        key_text = f"{stock_code}|TITLE:{normalized_title}"
    else:
        normalized_url = normalize_url((item.get("url") or "").strip())
        if not normalized_url:
            return ""
        key_text = f"{stock_code}|URL:{normalized_url}"

    digest = hashlib.sha256(key_text.encode("utf-8")).hexdigest()
    return f"{NEWS_SENT_PREFIX}{digest}"


def filter_unsent_news(items, table_name):
    if not table_name:
        return items

    table = dynamodb.Table(table_name)
    unsent = []
    for item in items:
        dedupe_id = build_news_dedupe_id(item)
        if not dedupe_id:
            continue

        response = table.get_item(Key={"StockID": dedupe_id, "Timestamp": NEWS_SENT_LATEST})
        if "Item" in response:
            continue

        unsent.append(item)

    return unsent


def mark_news_as_sent(items, table_name, notified_at):
    if not table_name or not items:
        return 0

    retention_days = get_env_int("NEWS_SENT_RETENTION_DAYS", 30)
    notified_dt = parse_datetime_safe(notified_at) or datetime.now(timezone.utc)
    expires_at = None
    if retention_days > 0:
        expires_at = int((notified_dt + timedelta(days=retention_days)).timestamp())

    table = dynamodb.Table(table_name)
    written = 0
    with table.batch_writer() as batch:
        for item in items:
            dedupe_id = build_news_dedupe_id(item)
            if not dedupe_id:
                continue

            record = {
                "StockID": dedupe_id,
                "Timestamp": NEWS_SENT_LATEST,
                "Type": "NewsSent",
                "Url": normalize_url((item.get("url") or "").strip()),
                "Source": (item.get("source") or "").strip(),
                "NewsStockCode": (item.get("stock_code") or "").strip(),
                "PublishedAt": (item.get("published_at") or "").strip(),
                "NotifiedAt": notified_at,
            }
            if expires_at is not None:
                record["ExpiresAt"] = expires_at

            batch.put_item(Item=record)
            written += 1

    return written


def post_to_slack(webhook_url, message, timeout_seconds):
    if not webhook_url:
        print("SLACK_WEBHOOK_URL is not set.")
        return False

    response = requests.post(
        webhook_url, json={"text": message}, timeout=timeout_seconds
    )
    response.raise_for_status()
    return True


def post_to_slack_response_url(response_url, message, timeout_seconds):
    if not response_url:
        return False

    response = requests.post(
        response_url,
        json={"response_type": "ephemeral", "replace_original": False, "text": message},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return True


def format_slack_message(stock_news_map, market_news):
    lines = ["日次ニュース通知です．", ""]

    if not stock_news_map:
        lines.append("登録銘柄が見つかりませんでした．")
    else:
        for code in sorted(stock_news_map.keys()):
            payload = stock_news_map[code]
            name = payload["name"]
            label = f"{code} {name}".strip()
            lines.append(f"【{label}】")

            if not payload["items"]:
                lines.append("・該当ニュースはありませんでした．")
            else:
                for item in payload["items"]:
                    lines.append(f"・{item['title']}")
                    lines.append(f"  {item['url']}")

            lines.append("")

    lines.append("【全体関連ニュース】")
    if not market_news:
        lines.append("・該当ニュースはありませんでした．")
    else:
        for item in market_news:
            lines.append(f"・{item['title']}")
            lines.append(f"  {item['url']}")

    return "\n".join(lines)


def execute_news_pipeline(trigger_source):
    print(f"Start news pipeline: {trigger_source}")

    timeout_seconds = get_env_int("HTTP_TIMEOUT_SECONDS", 5)
    recent_news_days = get_env_int("RECENT_NEWS_DAYS", 7)
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    news_sent_table_name = os.environ.get("NEWS_SENT_TABLE_NAME", "").strip()
    now_utc = datetime.now(timezone.utc)

    try:
        sync_result = apply_stock_master_diff(timeout_seconds)
        print(
            "Stock master sync result: "
            f"mode={sync_result.get('mode', 'unknown')} "
            f"synced={sync_result['synced']} reason={sync_result['reason']} count={sync_result['count']}"
        )
    except Exception as ex:
        print(f"Stock master sync failed: {str(ex)}")

    try:
        stocks = fetch_registered_stocks()
    except Exception as ex:
        print(f"Failed to load stocks from DynamoDB: {str(ex)}")
        stocks = []

    stock_news_map = {}
    for stock in stocks:
        combined = []
        try:
            combined.extend(
                fetch_yahoo_finance_rss(stock, None, timeout_seconds)
            )
        except Exception as ex:
            print(f"Yahoo RSS fetch failed for {stock['code']}: {str(ex)}")

        try:
            combined.extend(
                fetch_google_news_rss(stock, None, timeout_seconds)
            )
        except Exception as ex:
            print(f"Google RSS fetch failed for {stock['code']}: {str(ex)}")

        try:
            query = build_search_query(stock["code"], stock["name"])
            combined.extend(
                fetch_newsapi_news(
                    query=query,
                    max_items=None,
                    timeout_seconds=timeout_seconds,
                    source_label="newsapi_stock",
                    stock_code=stock["code"],
                    stock_name=stock["name"],
                    lookback_days=recent_news_days,
                    now_utc=now_utc,
                )
            )
        except Exception as ex:
            print(f"NewsAPI fetch failed for {stock['code']}: {str(ex)}")

        deduped = deduplicate_news(combined)
        recents = [
            item
            for item in deduped
            if is_recent_news(item.get("published_at"), recent_news_days, now_utc)
        ]
        unsent = filter_unsent_news(recents, news_sent_table_name)
        stock_news_map[stock["code"]] = {
            "name": stock["name"],
            "items": unsent,
        }

    try:
        market_news = deduplicate_news(
            fetch_market_overview_news(stocks, None, timeout_seconds)
        )
        market_news = [
            item
            for item in market_news
            if is_recent_news(item.get("published_at"), recent_news_days, now_utc)
        ]
        market_news = filter_unsent_news(market_news, news_sent_table_name)
    except Exception as ex:
        print(f"Market overview fetch failed: {str(ex)}")
        market_news = []

    message = format_slack_message(stock_news_map, market_news)
    posted = False
    try:
        posted = post_to_slack(webhook_url, message, timeout_seconds)
    except Exception as ex:
        print(f"Slack post failed: {str(ex)}")

    if posted:
        sent_items = []
        for payload in stock_news_map.values():
            sent_items.extend(payload.get("items", []))
        sent_items.extend(market_news)

        try:
            mark_news_as_sent(
                sent_items,
                news_sent_table_name,
                now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        except Exception as ex:
            print(f"Failed to mark sent news: {str(ex)}")

    stock_article_count = 0
    for payload in stock_news_map.values():
        stock_article_count += len(payload["items"])

    return {
        "stocks": len(stock_news_map),
        "stock_articles": stock_article_count,
        "market_articles": len(market_news),
        "posted": posted,
    }


def verify_slack_signature(headers, body, secret):
    """
    Slackからのリクエストに対する署名検証を行う
    """
    # ヘッダー名が小文字で来る場合も考慮
    headers_lower = {k.lower(): v for k, v in headers.items()}

    slack_signature = headers_lower.get("x-slack-signature")
    slack_request_timestamp = headers_lower.get("x-slack-request-timestamp")

    if not slack_signature or not slack_request_timestamp:
        return False

    # タイムスタンプが5分以上古い場合はリプレイ攻撃とみなす
    if abs(time.time() - int(slack_request_timestamp)) > 60 * 5:
        return False

    sig_basestring = f"v0:{slack_request_timestamp}:{body}"
    my_signature = (
        "v0="
        + hmac.new(
            secret.encode("utf-8"), sig_basestring.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    )

    return hmac.compare_digest(my_signature, slack_signature)


def handle_slack_event(event):
    """
    Slackのコマンドを受信してDynamoDBに書き込む処理
    """
    headers = event.get("headers", {})
    body = event.get("body", "")
    is_base64_encoded = event.get("isBase64Encoded", False)

    if is_base64_encoded:
        import base64

        body = base64.b64decode(body).decode("utf-8")

    secret = os.environ.get("SLACK_SIGNING_SECRET", "")

    if not verify_slack_signature(headers, body, secret):
        return {"statusCode": 401, "body": "Unauthorized"}

    # URLエンコードされたフォームデータをパース
    parsed_body = parse_qs(body)

    command_name = parsed_body.get("command", [""])[0].strip()
    text_param = parsed_body.get("text", [""])[0].strip()
    response_url = parsed_body.get("response_url", [""])[0].strip()
    supported_commands = RUN_NEWS_COMMANDS | RUN_MASTER_COMMANDS | ADD_STOCK_COMMANDS

    if command_name and command_name not in supported_commands:
        return {
            "statusCode": 200,
            "body": f"未対応のコマンドです．{command_name}",
        }

    try:
        queued = enqueue_slack_command(command_name, text_param, response_url)
    except Exception as ex:
        return {
            "statusCode": 500,
            "body": f"コマンドのキュー投入に失敗しました．{str(ex)}",
        }

    if not queued:
        result = execute_slack_command(command_name, text_param)
        return {"statusCode": 200, "body": result["message"]}

    return {
        "statusCode": 200,
        "body": (
            "コマンドを受け付けました．"
            "\nバックグラウンドで処理を開始します．"
            "\n完了後に結果を通知します．"
        ),
    }


def execute_slack_command(command_name, text_param):
    if command_name in RUN_NEWS_COMMANDS:
        result = execute_news_pipeline(f"slack:{command_name}")
        return {
            "status": "ok",
            "message": (
                "ニュース収集を実行しました．"
                f"\n対象銘柄: {result['stocks']}件"
                f"\n銘柄ニュース: {result['stock_articles']}件"
                f"\n全体ニュース: {result['market_articles']}件"
            ),
        }

    if command_name in RUN_MASTER_COMMANDS:
        action = text_param.strip().lower() if text_param else "diff"
        return execute_master_command(action)

    if command_name in ADD_STOCK_COMMANDS:
        return execute_add_stock_command(text_param)

    return {
        "status": "invalid",
        "message": f"未対応のコマンドです．{command_name}",
    }


def handle_internal_slack_command_event(event):
    command_name = (event.get("command_name") or "").strip()
    text_param = (event.get("text_param") or "").strip()
    response_url = (event.get("response_url") or "").strip()

    result = execute_slack_command(command_name, text_param)
    timeout_seconds = get_env_int("HTTP_TIMEOUT_SECONDS", 5)

    if response_url:
        try:
            post_to_slack_response_url(response_url, result["message"], timeout_seconds)
        except Exception as ex:
            print(f"Slack delayed response failed: {str(ex)}")

    status_code = (
        200
        if result["status"]
        in {"ok", "already_registered", "multiple", "not_found", "invalid"}
        else 400
    )
    return {"statusCode": status_code, "body": result["message"]}


def handle_add_stock_command_event(event):
    text_param = (event.get("text_param") or "").strip()
    response_url = (event.get("response_url") or "").strip()

    result = execute_add_stock_command(text_param)
    timeout_seconds = get_env_int("HTTP_TIMEOUT_SECONDS", 5)

    if response_url:
        try:
            post_to_slack_response_url(response_url, result["message"], timeout_seconds)
        except Exception as ex:
            print(f"Add stock delayed response failed: {str(ex)}")

    status_code = (
        200
        if result["status"]
        in {"ok", "already_registered", "multiple", "not_found", "invalid"}
        else 400
    )
    return {"statusCode": status_code, "body": result["message"]}


def handle_scheduler_event(event):
    print(f"Received scheduler event: {json.dumps(event, ensure_ascii=False)}")
    execute_news_pipeline("scheduler")

    return {"statusCode": 200, "body": "Scheduler event processed successfully."}


def handle_master_command_event(event):
    action = (event.get("master_action") or "diff").strip().lower()
    result = execute_master_command(action)
    print(
        "Handled internal master command: "
        f"action={action} status={result['status']}"
    )
    if result["status"] == "ok":
        return {"statusCode": 200, "body": result["message"]}
    return {"statusCode": 400, "body": result["message"]}


def lambda_handler(event, context):
    """
    Lambdaのエントリーポイント
    """
    # EventBridge Schedulerは requestContext を持たないか、
    # Function URL由来の http リクエストとは異なる構造を持つ。
    # Function URL経由(Slack)の場合は requestContext.http が存在する。

    if "requestContext" in event and "http" in event["requestContext"]:
        # Slackからのリクエスト (Function URL)
        return handle_slack_event(event)
    if event.get("source") == "internal-slack-command":
        return handle_internal_slack_command_event(event)
    if event.get("source") == "internal-master-command":
        return handle_master_command_event(event)
    if event.get("source") == "internal-add-stock-command":
        return handle_add_stock_command_event(event)
    else:
        # Schedulerなどからの直接呼び出し
        return handle_scheduler_event(event)
