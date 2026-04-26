# Serverless Stock Application (MVP)

このプロジェクトは、AWS SAMを用いたサーバーレスアプリケーションの最小構成（MVP）です。
Slackからのスラッシュコマンドを受信し、EventBridgeの定期トリガーで処理を実行する機能を提供します。

## アーキテクチャ

* **AWS Lambda (Python 3.12)**: エントリーポイントであり、SlackとSchedulerのイベントを捌きます。
  * `requestContext.http` の有無により、Slack（Function URL経由）とScheduler（直接起動）のイベントを判別します。
  * Slackからのリクエストについては、別関数で署名検証（`X-Slack-Signature`）を行い、不正なリクエストをブロックします。
* **Lambda Function URLs**: `AUTH_TYPE: NONE` でインターネットに公開され、Slackからのリクエストを受け付けます。
* **Amazon DynamoDB**: `StockSubscriptions` テーブル。Partition Keyは `StockID` (String)、Sort Keyは `Timestamp` (String) です。
* **Amazon EventBridge の定期トリガー**: 1日1回 (`rate(1 day)`) 定期実行を行います。
  * 定期実行時には `{"source": "scheduler", "detail-type": "Daily Execution"}` を入力としてLambdaに渡します。

## デプロイ方法

このプロジェクトはGitHub Actions経由で自動デプロイされます。

### 初期設定

1. GitHubのRepository Secretsに以下を登録してください。
  * `AWS_OIDC_ROLE_ARN`: AWS IAM Identity Provider (OIDC) 用に設定されたデプロイ用ロールのARN
  * `SLACK_SIGNING_SECRET`: Slack Appの設定から取得できるSigning Secret
2. 認証情報はGitHub Actionsから自動注入されるため、`template.yaml` や `src/app.py`、`.github/workflows/deploy.yml` にシークレット値を直接記述しないでください。
3. `main` ブランチへPushすると、`.github/workflows/deploy.yml` に定義されたデプロイジョブが実行されます。

### 追加設定（ニュース収集・通知）

ニュース収集機能とSlack自動通知を利用する場合は，以下のSecretsを追加してください．

* `SLACK_WEBHOOK_URL`: 本番環境のSlack Incoming Webhook URL
* `SLACK_WEBHOOK_URL_DEV`: 開発環境のSlack Incoming Webhook URL
* `NEWS_API_KEY`: 本番環境のNewsAPIキー
* `NEWS_API_KEY_DEV`: 開発環境のNewsAPIキー

`main` ブランチでは本番用Secrets，`dev` ブランチでは開発用Secretsが自動的に使い分けられます．

## 現在のニュース収集仕様

定期実行時に，DynamoDBに登録された監視銘柄を読み取り，以下3系統からニュースを取得します．

* Yahoo Finance RSS
* Google News RSS
* NewsAPI

取得時の検索クエリは「証券コード OR 銘柄名」です．
複数ソースで同じ記事が見つかった場合は，URL正規化後に重複排除してからSlackに投稿します．

## Slackコマンド仕様

監視銘柄の登録は以下形式です．

```text
/add_stock <証券コード> [銘柄名]
```

例:

```text
/add_stock 7203 トヨタ
/add_stock AAPL
```

開発環境のSlack Appでは，設定済みの開発用コマンド（例: `/add_stock_dev`）を利用してください．
