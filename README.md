# Serverless Stock Application (MVP)

このプロジェクトは、AWS SAMを用いたサーバーレスアプリケーションの最小構成（MVP）です。
Slackからのスラッシュコマンドを受信し、EventBridgeのスケジュールルールから定期的に処理を実行する機能を提供します。

## アーキテクチャ

* **AWS Lambda (Python 3.12)**: エントリーポイントであり、SlackとSchedulerのイベントを捌きます。
  * `requestContext.http` の有無により、Slack（Function URL経由）とScheduler（直接起動）のイベントを判別します。
  * Slackからのリクエストについては、別関数で署名検証（`X-Slack-Signature`）を行い、不正なリクエストをブロックします。
* **Lambda Function URLs**: `AUTH_TYPE: NONE` でインターネットに公開され、Slackからのリクエストを受け付けます。
* **Amazon DynamoDB**: `StockSubscriptions` テーブル。Partition Keyは `StockID` (String)、Sort Keyは `Timestamp` (String) です。
* **Amazon EventBridge ルール (スケジュール)**: 1日1回 (`rate(1 day)`) 定期実行を行います。
  * 定期実行時には `{"source": "scheduler", "detail-type": "Daily Execution"}` を入力としてLambdaに渡します。

## デプロイ方法

このプロジェクトはGitHub Actions経由で自動デプロイされます。

### 初期設定

1. GitHubのRepository Secretsに以下を登録してください。
  * `AWS_OIDC_ROLE_ARN`: AWS IAM Identity Provider (OIDC) 用に設定されたデプロイ用ロールのARN
  * `SLACK_SIGNING_SECRET`: Slack Appの設定から取得できるSigning Secret
2. 認証情報はGitHub Actionsから自動注入されるため、`template.yaml` や `src/app.py`、`.github/workflows/deploy.yml` にシークレット値を直接記述しないでください。
3. `main` ブランチへPushすると、`.github/workflows/deploy.yml` に定義されたデプロイジョブが実行されます。
