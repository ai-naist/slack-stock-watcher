import os
import json
import hmac
import hashlib
import time
from urllib.parse import parse_qs
import boto3

dynamodb = boto3.resource('dynamodb')

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

    # スラッシュコマンドの場合は 'text' パラメータに引数が入る
    # 例: /add_stock AAPL -> text=["AAPL"]
    text_param = parsed_body.get('text', [''])[0].strip()

    if not text_param:
        return {
            'statusCode': 200,
            'body': 'StockIDを指定してください。'
        }

    table_name = os.environ.get('TABLE_NAME')
    table = dynamodb.Table(table_name)

    timestamp_str = str(int(time.time()))

    table.put_item(
        Item={
            'StockID': text_param,
            'Timestamp': timestamp_str
        }
    )

    return {
        'statusCode': 200,
        'body': f'StockID: {text_param} を登録しました'
    }

def handle_scheduler_event(event):
    """
    EventBridge Schedulerからの定期実行を受信する処理 (MVPとしてのモック)
    """
    print(f"Received scheduler event: {json.dumps(event)}")
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
