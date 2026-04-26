import hashlib
import hmac
import os
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# テスト用のダミー環境変数（boto3のインポート前に設定する必要がある）
os.environ["AWS_DEFAULT_REGION"] = "ap-northeast-1"
os.environ["SLACK_SIGNING_SECRET"] = "test_secret"
os.environ["TABLE_NAME"] = "TestStockSubscriptions"
os.environ["NEWS_SENT_TABLE_NAME"] = "TestNewsSent"
os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.test/mock"
os.environ["NEWS_API_KEY"] = "test_news_api_key"

from src.app import (
    deduplicate_news,
    fetch_jquants_listed_info,
    fetch_registered_stocks,
    filter_unsent_news,
    find_stock_in_master,
    lambda_handler,
    mark_news_as_sent,
    parse_rss_items,
)


def generate_valid_slack_headers_and_body(body_str, secret):
    timestamp = str(int(time.time()))
    sig_basestring = f"v0:{timestamp}:{body_str}"
    my_signature = (
        "v0="
        + hmac.new(
            secret.encode("utf-8"), sig_basestring.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    )

    headers = {
        "x-slack-signature": my_signature,
        "x-slack-request-timestamp": timestamp,
    }
    return headers, body_str


@patch("src.app.dynamodb")
def test_slack_event_valid_signature(mock_dynamodb):
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    body_str = "token=dummy&team_id=T0001&command=/add_stock&text=AAPL"
    headers, body = generate_valid_slack_headers_and_body(body_str, "test_secret")

    event = {
        "requestContext": {"http": {}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "銘柄を登録しました．" in response["body"]
    assert "証券コード: AAPL" in response["body"]
    mock_dynamodb.Table.assert_called_once_with("TestStockSubscriptions")
    mock_table.put_item.assert_called_once()

    call_args = mock_table.put_item.call_args[1]["Item"]
    assert call_args["StockID"] == "AAPL"
    assert call_args["StockCode"] == "AAPL"
    assert call_args["StockName"] == ""
    assert call_args["Timestamp"] == "LATEST"


@patch("src.app.dynamodb")
def test_slack_event_valid_signature_with_stock_name(mock_dynamodb):
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    body_str = "token=dummy&team_id=T0001&command=/add_stock&text=7203%20%E3%83%88%E3%83%A8%E3%82%BF"
    headers, body = generate_valid_slack_headers_and_body(body_str, "test_secret")

    event = {
        "requestContext": {"http": {}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "証券コード: 7203" in response["body"]

    call_args = mock_table.put_item.call_args[1]["Item"]
    assert call_args["StockID"] == "7203"
    assert call_args["StockCode"] == "7203"
    assert call_args["StockName"] == "トヨタ"


@patch("src.app.dynamodb")
def test_slack_event_add_stock_already_registered(mock_dynamodb):
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table
    mock_table.get_item.return_value = {
        "Item": {
            "StockID": "AAPL",
            "StockCode": "AAPL",
            "StockName": "Apple",
            "StockNameEn": "Apple Inc.",
            "Timestamp": "LATEST",
        }
    }

    body_str = "token=dummy&team_id=T0001&command=/add_stock&text=AAPL"
    headers, body = generate_valid_slack_headers_and_body(body_str, "test_secret")

    event = {
        "requestContext": {"http": {}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "銘柄はすでに登録済みです．" in response["body"]
    assert "証券コード: AAPL" in response["body"]
    mock_table.put_item.assert_not_called()


@patch("src.app.dynamodb")
def test_slack_event_invalid_signature(mock_dynamodb):
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    headers = {
        "x-slack-signature": "v0=invalid_signature",
        "x-slack-request-timestamp": str(int(time.time())),
    }
    body_str = "token=dummy&team_id=T0001&command=/add_stock&text=AAPL"

    event = {
        "requestContext": {"http": {}},
        "headers": headers,
        "body": body_str,
        "isBase64Encoded": False,
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 401
    assert response["body"] == "Unauthorized"
    mock_table.put_item.assert_not_called()


@patch("src.app.execute_news_pipeline")
def test_scheduler_event_success(mock_execute_news_pipeline):
    mock_execute_news_pipeline.return_value = {
        "stocks": 1,
        "stock_articles": 2,
        "market_articles": 1,
        "posted": True,
    }

    event = {"source": "scheduler", "detail-type": "Daily Execution"}

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert response["body"] == "Scheduler event processed successfully."
    mock_execute_news_pipeline.assert_called_once_with("scheduler")


@patch("src.app.lambda_client")
@patch.dict(os.environ, {"AWS_LAMBDA_FUNCTION_NAME": "TestFunction"}, clear=False)
def test_slack_event_run_news_command_returns_immediate_ack(mock_lambda_client):
    mock_lambda_client.invoke.return_value = {"StatusCode": 202}

    body_str = "token=dummy&team_id=T0001&command=/run_news&text="
    headers, body = generate_valid_slack_headers_and_body(body_str, "test_secret")

    event = {
        "requestContext": {"http": {}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "コマンドを受け付けました" in response["body"]
    mock_lambda_client.invoke.assert_called_once()


@patch("src.app.lambda_client")
@patch.dict(os.environ, {"AWS_LAMBDA_FUNCTION_NAME": "TestFunction"}, clear=False)
def test_slack_event_run_master_init_command(mock_lambda_client):
    mock_lambda_client.invoke.return_value = {"StatusCode": 202}

    body_str = "token=dummy&team_id=T0001&command=/run_master&text=init"
    headers, body = generate_valid_slack_headers_and_body(body_str, "test_secret")

    event = {
        "requestContext": {"http": {}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "コマンドを受け付けました" in response["body"]
    mock_lambda_client.invoke.assert_called_once()


@patch("src.app.lambda_client")
@patch.dict(os.environ, {"AWS_LAMBDA_FUNCTION_NAME": "TestFunction"}, clear=False)
def test_slack_event_run_master_diff_command(mock_lambda_client):
    mock_lambda_client.invoke.return_value = {"StatusCode": 202}

    body_str = "token=dummy&team_id=T0001&command=/run_master&text=diff"
    headers, body = generate_valid_slack_headers_and_body(body_str, "test_secret")

    event = {
        "requestContext": {"http": {}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "コマンドを受け付けました" in response["body"]
    mock_lambda_client.invoke.assert_called_once()


@patch("src.app.apply_stock_master_diff")
def test_internal_master_event_diff_runs_successfully(mock_apply_stock_master_diff):
    mock_apply_stock_master_diff.return_value = {
        "synced": True,
        "mode": "diff",
        "reason": "ok",
        "count": 120,
        "upserted": 4,
        "deleted": 1,
    }

    event = {
        "source": "internal-master-command",
        "detail-type": "MasterCommand",
        "master_action": "diff",
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "銘柄マスタ差分適用を実行しました．" in response["body"]
    assert "適用件数: 4件" in response["body"]
    assert "削除件数: 1件" in response["body"]
    assert "有効件数: 120件" in response["body"]
    mock_apply_stock_master_diff.assert_called_once()


def test_deduplicate_news_by_url_normalization():
    items = [
        {"title": "A", "url": "https://example.com/path?utm_source=abc&id=1"},
        {"title": "B", "url": "https://example.com/path?id=1&utm_medium=def"},
        {"title": "C", "url": "https://example.com/path?id=2"},
    ]

    deduped = deduplicate_news(items)

    assert len(deduped) == 2
    assert deduped[0]["url"] == "https://example.com/path?id=1"
    assert deduped[1]["url"] == "https://example.com/path?id=2"


def test_find_stock_in_master_by_japanese_name_and_english_name():
    master_records = [
        {
            "StockCode": "36870",
            "CoName": "フィックスターズ",
            "CoNameEn": "Fixstars Corporation",
            "NormalizedCoName": "フィックスターズ",
            "NormalizedCoNameEn": "fixstarscorporation",
        }
    ]

    by_jp = find_stock_in_master(master_records, "フィックスターズ")
    by_en = find_stock_in_master(master_records, "fixstars")

    assert by_jp["status"] == "single"
    assert by_jp["items"][0]["code"] == "3687"
    assert by_en["status"] == "single"
    assert by_en["items"][0]["code"] == "3687"


@patch("src.app.requests.get")
@patch.dict(os.environ, {"JQUANTS_BASE_URL": "https://api.jquants.com"}, clear=False)
def test_fetch_jquants_listed_info_uses_v2_endpoint_when_base_url_has_no_version(
    mock_requests_get,
):
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": []}
    mock_response.raise_for_status.return_value = None
    mock_requests_get.return_value = mock_response

    fetch_jquants_listed_info("dummy_key", 5)

    called_url = mock_requests_get.call_args[0][0]
    assert called_url == "https://api.jquants.com/v2/equities/master"


@patch("src.app.dynamodb")
@patch.dict(os.environ, {"MASTER_TABLE_NAME": "TestStockMaster"}, clear=False)
def test_slack_event_add_stock_with_master_lookup_success(mock_dynamodb):
    subscription_table = MagicMock()
    master_table = MagicMock()

    mock_dynamodb.Table.side_effect = lambda name: (
        master_table if name == "TestStockMaster" else subscription_table
    )

    master_table.get_item.return_value = {
        "Item": {
            "StockCode": "__META__",
            "SnapshotAt": "LATEST",
            "CurrentSnapshotAt": "2026-01-30T00:00:00Z",
        }
    }
    master_table.scan.return_value = {
        "Items": [
            {
                "StockCode": "36870",
                "SnapshotAt": "2026-01-30T00:00:00Z",
                "CoName": "フィックスターズ",
                "CoNameEn": "Fixstars Corporation",
                "NormalizedCoName": "フィックスターズ",
                "NormalizedCoNameEn": "fixstarscorporation",
            }
        ]
    }

    body_str = "token=dummy&team_id=T0001&command=/add_stock&text=Fixstars"
    headers, body = generate_valid_slack_headers_and_body(body_str, "test_secret")
    event = {
        "requestContext": {"http": {}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "証券コード: 3687" in response["body"]
    assert "企業名: フィックスターズ" in response["body"]
    assert "企業名(英語): Fixstars Corporation" in response["body"]

    put_item = subscription_table.put_item.call_args[1]["Item"]
    assert put_item["StockCode"] == "3687"
    assert put_item["StockName"] == "フィックスターズ"
    assert put_item["StockNameEn"] == "Fixstars Corporation"


@patch("src.app.lambda_client")
@patch.dict(os.environ, {"AWS_LAMBDA_FUNCTION_NAME": "TestFunction"}, clear=False)
def test_slack_event_add_stock_dev_returns_immediate_ack(mock_lambda_client):
    mock_lambda_client.invoke.return_value = {"StatusCode": 202}

    body_str = (
        "token=dummy&team_id=T0001&command=/add_stock_dev&text=3687"
        "&response_url=https%3A%2F%2Fhooks.slack.test%2Fresponse"
    )
    headers, body = generate_valid_slack_headers_and_body(body_str, "test_secret")
    event = {
        "requestContext": {"http": {}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "コマンドを受け付けました" in response["body"]
    mock_lambda_client.invoke.assert_called_once()


@patch("src.app.post_to_slack_response_url")
@patch("src.app.execute_news_pipeline")
def test_internal_slack_command_event_posts_delayed_result(
    mock_execute_news_pipeline,
    mock_post_to_slack_response_url,
):
    mock_execute_news_pipeline.return_value = {
        "stocks": 2,
        "stock_articles": 4,
        "market_articles": 3,
        "posted": True,
    }

    event = {
        "source": "internal-slack-command",
        "detail-type": "SlackCommand",
        "command_name": "/run_news",
        "text_param": "",
        "response_url": "https://hooks.slack.test/response",
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "ニュース収集を実行しました．" in response["body"]
    mock_execute_news_pipeline.assert_called_once_with("slack:/run_news")
    mock_post_to_slack_response_url.assert_called_once()


@patch("src.app.dynamodb")
@patch.dict(os.environ, {"MASTER_TABLE_NAME": "TestStockMaster"}, clear=False)
def test_slack_event_add_stock_with_master_lookup_multiple_candidates(mock_dynamodb):
    subscription_table = MagicMock()
    master_table = MagicMock()

    mock_dynamodb.Table.side_effect = lambda name: (
        master_table if name == "TestStockMaster" else subscription_table
    )

    master_table.get_item.return_value = {
        "Item": {
            "StockCode": "__META__",
            "SnapshotAt": "LATEST",
            "CurrentSnapshotAt": "2026-01-30T00:00:00Z",
        }
    }
    master_table.scan.return_value = {
        "Items": [
            {
                "StockCode": "11110",
                "SnapshotAt": "2026-01-30T00:00:00Z",
                "CoName": "フィックス株式会社",
                "CoNameEn": "Fix One",
            },
            {
                "StockCode": "22220",
                "SnapshotAt": "2026-01-30T00:00:00Z",
                "CoName": "フィックスターズ",
                "CoNameEn": "Fixstars Corporation",
            },
        ]
    }

    body_str = "token=dummy&team_id=T0001&command=/add_stock&text=フィックス"
    headers, body = generate_valid_slack_headers_and_body(body_str, "test_secret")
    event = {
        "requestContext": {"http": {}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "候補が複数あります" in response["body"]
    subscription_table.put_item.assert_not_called()


def test_parse_rss_items_filters_outside_lookback_window():
    now_utc = datetime(2026, 4, 27, 0, 0, 0, tzinfo=timezone.utc)
    xml_text = (
        "<rss><channel>"
        "<item><title>new</title><link>https://example.com/new</link>"
        "<pubDate>Sat, 26 Apr 2026 12:00:00 GMT</pubDate></item>"
        "<item><title>old</title><link>https://example.com/old</link>"
        "<pubDate>Tue, 14 Apr 2026 12:00:00 GMT</pubDate></item>"
        "</channel></rss>"
    )

    items = parse_rss_items(
        xml_text,
        source_name="google_news_rss",
        stock_code="3687",
        stock_name="フィックスターズ",
        max_items=None,
        lookback_days=7,
        now_utc=now_utc,
    )

    assert len(items) == 1
    assert items[0]["title"] == "new"


@patch("src.app.dynamodb")
def test_filter_unsent_news_excludes_already_notified_items(mock_dynamodb):
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table
    mock_table.get_item.side_effect = [
        {"Item": {"StockID": "sent", "Timestamp": "LATEST"}},
        {},
    ]

    items = [
        {
            "source": "google_news_rss",
            "title": "already sent",
            "url": "https://example.com/already",
            "published_at": "2026-04-27T00:00:00Z",
            "stock_code": "3687",
            "stock_name": "フィックスターズ",
        },
        {
            "source": "google_news_rss",
            "title": "new item",
            "url": "https://example.com/new",
            "published_at": "2026-04-27T00:00:00Z",
            "stock_code": "3687",
            "stock_name": "フィックスターズ",
        },
    ]

    unsent = filter_unsent_news(items, "TestStockSubscriptions")

    assert len(unsent) == 1
    assert unsent[0]["title"] == "new item"


@patch("src.app.dynamodb")
def test_mark_news_as_sent_writes_dedup_records(mock_dynamodb):
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    mock_batch_writer = MagicMock()
    mock_table.batch_writer.return_value.__enter__.return_value = mock_batch_writer

    items = [
        {
            "source": "newsapi_stock",
            "title": "n1",
            "url": "https://example.com/a?utm_source=x",
            "published_at": "2026-04-27T00:00:00Z",
            "stock_code": "3687",
            "stock_name": "フィックスターズ",
        },
        {
            "source": "google_news_rss",
            "title": "n2",
            "url": "https://example.com/b",
            "published_at": "2026-04-27T01:00:00Z",
            "stock_code": "MARKET",
            "stock_name": "全体関連",
        },
    ]

    written = mark_news_as_sent(
        items,
        "TestStockSubscriptions",
        "2026-04-27T02:00:00Z",
    )

    assert written == 2
    assert mock_batch_writer.put_item.call_count == 2

    first_item = mock_batch_writer.put_item.call_args_list[0][1]["Item"]
    assert first_item["Type"] == "NewsSent"
    assert first_item["Timestamp"] == "LATEST"
    assert first_item["Url"] == "https://example.com/a"
    assert first_item["ExpiresAt"] == 1779847200


@patch("src.app.dynamodb")
def test_fetch_registered_stocks_returns_subscription_rows(mock_dynamodb):
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table
    mock_table.scan.return_value = {
        "Items": [
            {
                "StockID": "3687",
                "StockCode": "3687",
                "StockName": "フィックスターズ",
                "Timestamp": "LATEST",
            },
        ]
    }

    stocks = fetch_registered_stocks()

    assert len(stocks) == 1
    assert stocks[0]["code"] == "3687"
