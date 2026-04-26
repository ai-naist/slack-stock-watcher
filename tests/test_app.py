import os
import time
import hmac
import hashlib
from unittest.mock import patch, MagicMock

# テスト用のダミー環境変数（boto3のインポート前に設定する必要がある）
os.environ['AWS_DEFAULT_REGION'] = 'ap-northeast-1'
os.environ['SLACK_SIGNING_SECRET'] = 'test_secret'
os.environ['TABLE_NAME'] = 'TestStockSubscriptions'
os.environ['SLACK_WEBHOOK_URL'] = 'https://hooks.slack.test/mock'
os.environ['NEWS_API_KEY'] = 'test_news_api_key'

from src.app import lambda_handler, deduplicate_news

def generate_valid_slack_headers_and_body(body_str, secret):
    timestamp = str(int(time.time()))
    sig_basestring = f'v0:{timestamp}:{body_str}'
    my_signature = 'v0=' + hmac.new(
        secret.encode('utf-8'),
        sig_basestring.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    headers = {
        'x-slack-signature': my_signature,
        'x-slack-request-timestamp': timestamp
    }
    return headers, body_str

@patch('src.app.dynamodb')
def test_slack_event_valid_signature(mock_dynamodb):
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    body_str = 'token=dummy&team_id=T0001&command=/add_stock&text=AAPL'
    headers, body = generate_valid_slack_headers_and_body(body_str, 'test_secret')

    event = {
        'requestContext': {'http': {}}, # Function URL経由のリクエストをモック
        'headers': headers,
        'body': body,
        'isBase64Encoded': False
    }

    response = lambda_handler(event, None)

    assert response['statusCode'] == 200
    assert 'StockID: AAPL を登録しました' in response['body']
    mock_dynamodb.Table.assert_called_once_with('TestStockSubscriptions')
    mock_table.put_item.assert_called_once()

    call_args = mock_table.put_item.call_args[1]['Item']
    assert call_args['StockID'] == 'AAPL'
    assert call_args['StockCode'] == 'AAPL'
    assert call_args['StockName'] == ''
    assert call_args['Timestamp'] == 'LATEST'


@patch('src.app.dynamodb')
def test_slack_event_valid_signature_with_stock_name(mock_dynamodb):
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    body_str = 'token=dummy&team_id=T0001&command=/add_stock&text=7203%20%E3%83%88%E3%83%A8%E3%82%BF'
    headers, body = generate_valid_slack_headers_and_body(body_str, 'test_secret')

    event = {
        'requestContext': {'http': {}},
        'headers': headers,
        'body': body,
        'isBase64Encoded': False
    }

    response = lambda_handler(event, None)
    assert response['statusCode'] == 200

    call_args = mock_table.put_item.call_args[1]['Item']
    assert call_args['StockID'] == '7203'
    assert call_args['StockCode'] == '7203'
    assert call_args['StockName'] == 'トヨタ'

@patch('src.app.dynamodb')
def test_slack_event_invalid_signature(mock_dynamodb):
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    headers = {
        'x-slack-signature': 'v0=invalid_signature',
        'x-slack-request-timestamp': str(int(time.time()))
    }
    body_str = 'token=dummy&team_id=T0001&command=/add_stock&text=AAPL'

    event = {
        'requestContext': {'http': {}},
        'headers': headers,
        'body': body_str,
        'isBase64Encoded': False
    }

    response = lambda_handler(event, None)

    assert response['statusCode'] == 401
    assert response['body'] == 'Unauthorized'
    mock_table.put_item.assert_not_called()


@patch('src.app.execute_news_pipeline')
def test_scheduler_event_success(mock_execute_news_pipeline):
    mock_execute_news_pipeline.return_value = {
        'stocks': 1,
        'stock_articles': 2,
        'market_articles': 1,
        'posted': True
    }

    event = {
        'source': 'scheduler',
        'detail-type': 'Daily Execution'
    }

    response = lambda_handler(event, None)

    assert response['statusCode'] == 200
    assert response['body'] == 'Scheduler event processed successfully.'
    mock_execute_news_pipeline.assert_called_once_with('scheduler')


@patch('src.app.dynamodb')
@patch('src.app.execute_news_pipeline')
def test_slack_event_run_news_command(mock_execute_news_pipeline, mock_dynamodb):
    mock_execute_news_pipeline.return_value = {
        'stocks': 2,
        'stock_articles': 4,
        'market_articles': 3,
        'posted': True
    }

    body_str = 'token=dummy&team_id=T0001&command=/run_news&text='
    headers, body = generate_valid_slack_headers_and_body(body_str, 'test_secret')

    event = {
        'requestContext': {'http': {}},
        'headers': headers,
        'body': body,
        'isBase64Encoded': False
    }

    response = lambda_handler(event, None)

    assert response['statusCode'] == 200
    assert 'ニュース収集を実行しました．' in response['body']
    assert '対象銘柄: 2件' in response['body']
    mock_execute_news_pipeline.assert_called_once_with('slack:/run_news')
    mock_dynamodb.Table.assert_not_called()


def test_deduplicate_news_by_url_normalization():
    items = [
        {'title': 'A', 'url': 'https://example.com/path?utm_source=abc&id=1'},
        {'title': 'B', 'url': 'https://example.com/path?id=1&utm_medium=def'},
        {'title': 'C', 'url': 'https://example.com/path?id=2'}
    ]

    deduped = deduplicate_news(items)

    assert len(deduped) == 2
    assert deduped[0]['url'] == 'https://example.com/path?id=1'
    assert deduped[1]['url'] == 'https://example.com/path?id=2'
