# Serverless Stock Application (MVP)

このプロジェクトは、AWS SAMを用いたサーバーレスアプリケーションの最小構成（MVP）です。
Slackからのスラッシュコマンドを受信し、EventBridge Schedulerから定期的に処理を実行する機能を提供します。

## アーキテクチャ

* **AWS Lambda (Python 3.12)**: エントリーポイントであり、SlackとSchedulerのイベントを捌きます。
  * `requestContext.http` の有無により、Slack（Function URL経由）とScheduler（直接起動）のイベントを判別します。
  * Slackからのリクエストについては、別関数で署名検証（`X-Slack-Signature`）を行い、不正なリクエストをブロックします。
* **Lambda Function URLs**: `AUTH_TYPE: NONE` でインターネットに公開され、Slackからのリクエストを受け付けます。
* **Amazon DynamoDB**: `StockSubscriptions` テーブル。Partition Keyは `StockID` (String)、Sort Keyは `Timestamp` (String) です。
* **Amazon EventBridge Scheduler**: 1日1回 (`rate(1 days)`) 定期実行を行います。
  * Schedulerのイベントは `{"source": "scheduler", "detail-type": "Daily Execution"}` のような形式で送出される想定です。

## デプロイ方法

このプロジェクトはGitHub Actions経由で自動デプロイされます。

### 初期設定

1. GitHubのRepository SecretsにAWSデプロイ用のIAMロールARNとSlackのSigning Secretを設定する必要がありますが、本プロジェクトではプレースホルダーとしています。
2. `.github/workflows/deploy.yml` 内の以下の値を変更してください。
   * `<YOUR_OIDC_ROLE_ARN>`: AWS IAM Identity Provider (OIDC) 用に設定されたデプロイ用ロールのARN
   * `<YOUR_SLACK_SIGNING_SECRET>`: Slack Appの設定から取得できるSigning Secret（デプロイコマンドの引数）
