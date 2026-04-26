import os
import json
import hmac
import hashlib
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit, parse_qsl, urlencode
import xml.etree.ElementTree as ET
import boto3
import requests

dynamodb = boto3.resource('dynamodb')


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

        items.append({
            "source": source_name,
            "title": title,
            "url": url,
            "published_at": published_at,
            "stock_code": stock_code,
            "stock_name": stock_name
        })

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
    table_name = os.environ.get('TABLE_NAME')
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
                stock_map[code] = {
                    "code": code,
                    "name": name,
                    "timestamp": timestamp
                }

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    normalized_stocks = []
    for code in sorted(stock_map.keys()):
        normalized_stocks.append({
            "code": stock_map[code]["code"],
            "name": stock_map[code]["name"]
        })
    return normalized_stocks


def fetch_yahoo_finance_rss(stock, max_items, timeout_seconds):
    endpoint = "https://feeds.finance.yahoo.com/rss/2.0/headline"
    params = {
        "s": stock["code"],
        "region": "JP",
        "lang": "ja-JP"
    }

    response = requests.get(endpoint, params=params, timeout=timeout_seconds)
    response.raise_for_status()
    return parse_rss_items(response.text, "yahoo_finance_rss", stock["code"], stock["name"], max_items)


def fetch_google_news_rss(stock, max_items, timeout_seconds):
    endpoint = "https://news.google.com/rss/search"
    query = build_search_query(stock["code"], stock["name"])
    params = {
        "q": query,
        "hl": "ja",
        "gl": "JP",
        "ceid": "JP:ja"
    }

    response = requests.get(endpoint, params=params, timeout=timeout_seconds)
    response.raise_for_status()
    return parse_rss_items(response.text, "google_news_rss", stock["code"], stock["name"], max_items)


def fetch_newsapi_news(query, max_items, timeout_seconds, source_label, stock_code, stock_name):
    api_key = os.environ.get("NEWS_API_KEY", "").strip()
    if not api_key:
        return []

    endpoint = os.environ.get("NEWS_API_URL", "https://newsapi.org/v2/everything")
    params = {
        "q": query,
        "language": "ja",
        "sortBy": "publishedAt",
        "pageSize": max_items,
        "apiKey": api_key
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

        items.append({
            "source": source_label,
            "title": title,
            "url": url,
            "published_at": published_at,
            "stock_code": stock_code,
            "stock_name": stock_name
        })

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
        stock_name="全体関連"
    )


def post_to_slack(webhook_url, message, timeout_seconds):
    if not webhook_url:
        print("SLACK_WEBHOOK_URL is not set.")
        return False

    response = requests.post(
        webhook_url,
        json={"text": message},
        timeout=timeout_seconds
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

def verify_slack_signature(headers, body, secret):
    """
    Slackからのリクエストに対する署名検証を行う
    """
    # ヘッダー名が小文字で来る場合も考慮
    headers_lower = {k.lower(): v for k, v in headers.items()}

    slack_signature = headers_lower.get('x-slack-signature')
    slack_request_timestamp = headers_lower.get('x-slack-request-timestamp')

    if not slack_signature or not slack_request_timestamp:
        return False

    # タイムスタンプが5分以上古い場合はリプレイ攻撃とみなす
    if abs(time.time() - int(slack_request_timestamp)) > 60 * 5:
        return False

    sig_basestring = f'v0:{slack_request_timestamp}:{body}'
    my_signature = 'v0=' + hmac.new(
        secret.encode('utf-8'),
        sig_basestring.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(my_signature, slack_signature)

def handle_slack_event(event):
    """
    Slackのコマンドを受信してDynamoDBに書き込む処理
    """
    headers = event.get('headers', {})
    body = event.get('body', '')
    is_base64_encoded = event.get('isBase64Encoded', False)

    if is_base64_encoded:
        import base64
        body = base64.b64decode(body).decode('utf-8')

    secret = os.environ.get('SLACK_SIGNING_SECRET', '')

    if not verify_slack_signature(headers, body, secret):
        return {
            'statusCode': 401,
            'body': 'Unauthorized'
        }

    # URLエンコードされたフォームデータをパース
    parsed_body = parse_qs(body)

    text_param = parsed_body.get('text', [''])[0].strip()

    if not text_param:
        return {
            'statusCode': 200,
            'body': '証券コードを指定してください．例: /add_stock 7203 トヨタ'
        }

    stock_code, stock_name = parse_stock_input(text_param)
    if not stock_code:
        return {
            'statusCode': 200,
            'body': '証券コードを指定してください．'
        }

    table_name = os.environ.get('TABLE_NAME')
    table = dynamodb.Table(table_name)

    timestamp_str = 'LATEST'

    table.put_item(
        Item={
            'StockID': stock_code,
            'StockCode': stock_code,
            'StockName': stock_name,
            'Timestamp': timestamp_str
        }
    )

    return {
        'statusCode': 200,
        'body': f'StockID: {stock_code} を登録しました'
    }

def handle_scheduler_event(event):
    print(f"Received scheduler event: {json.dumps(event, ensure_ascii=False)}")

    timeout_seconds = get_env_int("HTTP_TIMEOUT_SECONDS", 5)
    max_per_stock = get_env_int("MAX_NEWS_PER_STOCK", 5)
    max_market_news = get_env_int("MAX_MARKET_NEWS", 8)
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

    try:
        stocks = fetch_registered_stocks()
    except Exception as ex:
        print(f"Failed to load stocks from DynamoDB: {str(ex)}")
        stocks = []

    stock_news_map = {}
    for stock in stocks:
        combined = []
        try:
            combined.extend(fetch_yahoo_finance_rss(stock, max_per_stock, timeout_seconds))
        except Exception as ex:
            print(f"Yahoo RSS fetch failed for {stock['code']}: {str(ex)}")

        try:
            combined.extend(fetch_google_news_rss(stock, max_per_stock, timeout_seconds))
        except Exception as ex:
            print(f"Google RSS fetch failed for {stock['code']}: {str(ex)}")

        try:
            query = build_search_query(stock["code"], stock["name"])
            combined.extend(fetch_newsapi_news(
                query=query,
                max_items=max_per_stock,
                timeout_seconds=timeout_seconds,
                source_label="newsapi_stock",
                stock_code=stock["code"],
                stock_name=stock["name"]
            ))
        except Exception as ex:
            print(f"NewsAPI fetch failed for {stock['code']}: {str(ex)}")

        deduped = deduplicate_news(combined)
        stock_news_map[stock["code"]] = {
            "name": stock["name"],
            "items": deduped[:max_per_stock]
        }

    try:
        market_news = deduplicate_news(
            fetch_market_overview_news(stocks, max_market_news, timeout_seconds)
        )[:max_market_news]
    except Exception as ex:
        print(f"Market overview fetch failed: {str(ex)}")
        market_news = []

    message = format_slack_message(stock_news_map, market_news)

    try:
        post_to_slack(webhook_url, message, timeout_seconds)
    except Exception as ex:
        print(f"Slack post failed: {str(ex)}")

    return {
        'statusCode': 200,
        'body': 'Scheduler event processed successfully.'
    }

def lambda_handler(event, context):
    """
    Lambdaのエントリーポイント
    """
    # EventBridge Schedulerは requestContext を持たないか、
    # Function URL由来の http リクエストとは異なる構造を持つ。
    # Function URL経由(Slack)の場合は requestContext.http が存在する。

    if 'requestContext' in event and 'http' in event['requestContext']:
        # Slackからのリクエスト (Function URL)
        return handle_slack_event(event)
    else:
        # Schedulerなどからの直接呼び出し
        return handle_scheduler_event(event)
