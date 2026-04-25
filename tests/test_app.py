import os
import time
import hmac
import hashlib
from unittest.mock import patch, MagicMock

# テスト用のダミー環境変数（boto3のインポート前に設定する必要がある）
os.environ['AWS_DEFAULT_REGION'] = 'ap-northeast-1'
os.environ['SLACK_SIGNING_SECRET'] = 'test_secret'
os.environ['TABLE_NAME'] = 'TestStockSubscriptions'

from src.app import lambda_handler, verify_slack_signature

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
    # 1. Slack署名が正しい場合の正常系
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

    # DynamoDBへの書き込みが呼ばれたことを確認
    mock_dynamodb.Table.assert_called_once_with('TestStockSubscriptions')
    mock_table.put_item.assert_called_once()

    # 書き込まれたアイテムの確認
    call_args = mock_table.put_item.call_args[1]['Item']
    assert call_args['StockID'] == 'AAPL'
    assert 'Timestamp' in call_args

@patch('src.app.dynamodb')
def test_slack_event_invalid_signature(mock_dynamodb):
    # 2. Slack署名が不正な場合の異常系
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

    # DynamoDBへの書き込みが呼ばれていないことを確認
    mock_table.put_item.assert_not_called()

def test_scheduler_event_success():
    # 3. Schedulerイベントの場合の正常系
    event = {
        'source': 'scheduler',
        'detail-type': 'Daily Execution'
    }

    response = lambda_handler(event, None)

    assert response['statusCode'] == 200
    assert response['body'] == 'Scheduler event processed successfully.'
