import hashlib
import hmac
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import boto3
import requests
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource("dynamodb")
RUN_NEWS_COMMANDS = {
    "/run_news",
    "/run_news_dev",
    "/run_stock_news",
    "/run_stock_news_dev",
}
MASTER_META_ID = "__META__"
MASTER_META_SNAPSHOT_KEY = "LATEST"


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
    stock_code = parts[0].upper()
    stock_name = parts[1].strip() if len(parts) > 1 else ""
    return stock_code, stock_name


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
    if not stripped:
        return []

    variants = [stripped]
    digits_only = "".join(ch for ch in stripped if ch.isdigit())
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


def fetch_jquants_listed_info(api_key, timeout_seconds):
    if not api_key:
        return []

    base_url = os.environ.get("JQUANTS_BASE_URL", "https://api.jquants.com/v2").strip()
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
            code = (item.get("StockCode") or item.get("Code") or "").strip().upper()
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


def sync_stock_master(timeout_seconds):
    master_table_name = os.environ.get("MASTER_TABLE_NAME", "").strip()
    if not master_table_name:
        return {"synced": False, "reason": "master table not configured", "count": 0}

    api_key = get_jquants_api_key()
    if not api_key:
        return {"synced": False, "reason": "api key not available", "count": 0}

    listed_info = fetch_jquants_listed_info(api_key, timeout_seconds)
    if not listed_info:
        return {"synced": False, "reason": "listed info empty", "count": 0}

    snapshot_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    table = dynamodb.Table(master_table_name)

    count = 0
    with table.batch_writer() as batch:
        for row in listed_info:
            code = (row.get("Code") or row.get("StockCode") or "").strip().upper()
            if not code:
                continue

            co_name = (row.get("CoName") or "").strip()
            co_name_en = (row.get("CoNameEn") or "").strip()

            batch.put_item(
                Item={
                    "StockCode": code,
                    "SnapshotAt": snapshot_at,
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
            )
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

    return {"synced": True, "reason": "ok", "count": count}


def build_search_query(stock_code, stock_name):
    tokens = [f'"{stock_code}"']
    if stock_name:
        tokens.append(f'"{stock_name}"')
    return " OR ".join(tokens)


def parse_rss_items(xml_text, source_name, stock_code, stock_name, max_items):
    items = []
    root = ET.fromstring(xml_text)
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        published_at = (item.findtext("pubDate") or "").strip()

        if not title or not url:
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

        if len(items) >= max_items:
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


def deduplicate_news(items):
    deduped = []
    seen = set()

    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue

        normalized = normalize_url(url)
        if normalized in seen:
            continue

        seen.add(normalized)
        copied = dict(item)
        copied["url"] = normalized
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
            code = (item.get("StockCode") or item.get("StockID") or "").strip().upper()
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
        response.text, "yahoo_finance_rss", stock["code"], stock["name"], max_items
    )


def fetch_google_news_rss(stock, max_items, timeout_seconds):
    endpoint = "https://news.google.com/rss/search"
    query = build_search_query(stock["code"], stock["name"])
    params = {"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"}

    response = requests.get(endpoint, params=params, timeout=timeout_seconds)
    response.raise_for_status()
    return parse_rss_items(
        response.text, "google_news_rss", stock["code"], stock["name"], max_items
    )


def fetch_newsapi_news(
    query, max_items, timeout_seconds, source_label, stock_code, stock_name
):
    api_key = os.environ.get("NEWS_API_KEY", "").strip()
    if not api_key:
        return []

    endpoint = os.environ.get("NEWS_API_URL", "https://newsapi.org/v2/everything")
    params = {
        "q": query,
        "language": "ja",
        "sortBy": "publishedAt",
        "pageSize": max_items,
        "apiKey": api_key,
    }

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

        if len(items) >= max_items:
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


def post_to_slack(webhook_url, message, timeout_seconds):
    if not webhook_url:
        print("SLACK_WEBHOOK_URL is not set.")
        return False

    response = requests.post(
        webhook_url, json={"text": message}, timeout=timeout_seconds
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
    max_per_stock = get_env_int("MAX_NEWS_PER_STOCK", 5)
    max_market_news = get_env_int("MAX_MARKET_NEWS", 8)
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

    try:
        sync_result = sync_stock_master(timeout_seconds)
        print(
            "Stock master sync result: "
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
                fetch_yahoo_finance_rss(stock, max_per_stock, timeout_seconds)
            )
        except Exception as ex:
            print(f"Yahoo RSS fetch failed for {stock['code']}: {str(ex)}")

        try:
            combined.extend(
                fetch_google_news_rss(stock, max_per_stock, timeout_seconds)
            )
        except Exception as ex:
            print(f"Google RSS fetch failed for {stock['code']}: {str(ex)}")

        try:
            query = build_search_query(stock["code"], stock["name"])
            combined.extend(
                fetch_newsapi_news(
                    query=query,
                    max_items=max_per_stock,
                    timeout_seconds=timeout_seconds,
                    source_label="newsapi_stock",
                    stock_code=stock["code"],
                    stock_name=stock["name"],
                )
            )
        except Exception as ex:
            print(f"NewsAPI fetch failed for {stock['code']}: {str(ex)}")

        deduped = deduplicate_news(combined)
        stock_news_map[stock["code"]] = {
            "name": stock["name"],
            "items": deduped[:max_per_stock],
        }

    try:
        market_news = deduplicate_news(
            fetch_market_overview_news(stocks, max_market_news, timeout_seconds)
        )[:max_market_news]
    except Exception as ex:
        print(f"Market overview fetch failed: {str(ex)}")
        market_news = []

    message = format_slack_message(stock_news_map, market_news)
    posted = False
    try:
        posted = post_to_slack(webhook_url, message, timeout_seconds)
    except Exception as ex:
        print(f"Slack post failed: {str(ex)}")

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

    if command_name in RUN_NEWS_COMMANDS:
        result = execute_news_pipeline(f"slack:{command_name}")
        return {
            "statusCode": 200,
            "body": (
                f"ニュース収集を実行しました．"
                f"対象銘柄: {result['stocks']}件，"
                f"銘柄ニュース: {result['stock_articles']}件，"
                f"全体ニュース: {result['market_articles']}件．"
            ),
        }

    if not text_param:
        return {
            "statusCode": 200,
            "body": "検索キーを指定してください．例: /add_stock 7203 または /add_stock フィックスターズ",
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
            return {"statusCode": 200, "body": "\n".join(lines)}

        if lookup["status"] == "single":
            matched = lookup["items"][0]
            stock_code = matched["code"]
            stock_name = matched["name"]
            stock_name_en = matched["name_en"]
        else:
            return {
                "statusCode": 200,
                "body": "銘柄マスタに該当がありませんでした．定期同期後に再試行してください．",
            }
    else:
        parsed_code, parsed_name = parse_stock_input(text_param)
        if not parsed_code:
            return {
                "statusCode": 200,
                "body": "検索キーを指定してください．",
            }
        stock_code = parsed_code
        stock_name = parsed_name

    table_name = os.environ.get("TABLE_NAME")
    table = dynamodb.Table(table_name)

    timestamp_str = "LATEST"

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
        "statusCode": 200,
        "body": (
            "銘柄を登録しました．"
            f"\n証券コード: {stock_code}"
            f"\n企業名: {name_label}"
            f"\n企業名(英語): {name_en_label}"
        ),
    }


def handle_scheduler_event(event):
    print(f"Received scheduler event: {json.dumps(event, ensure_ascii=False)}")
    execute_news_pipeline("scheduler")

    return {"statusCode": 200, "body": "Scheduler event processed successfully."}


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
    else:
        # Schedulerなどからの直接呼び出し
        return handle_scheduler_event(event)
