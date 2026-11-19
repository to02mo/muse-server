# MUSE 診断サーバー

アプリから写真＋質問回答を受け取り、Geminiで診断して結果を返す中継サーバー。
APIキーはこのサーバー側だけが持ち、アプリには入れない。

## 動かし方（本番ホスト）
- 環境変数 `GEMINI_API_KEY` に Gemini のAPIキーを設定する
- `python3 server.py` で起動（ポートは環境変数 `PORT`、既定 8787）
- 依存ライブラリなし（Python標準ライブラリのみ）

## 動かし方（Muse開発環境でのテスト）
- `GEMINI_API_KEY` 未設定の場合、登録済み custom.gemini のテスト用クレデンシャルで代替する（このフォールバックは開発環境専用）

## API
- `GET /health` → `{"ok": true}`
- `POST /diagnose` JSON:
  - `consent`: true 必須（AIへ写真を送る同意）
  - `answers`: 質問回答（文字列、または Q&A のオブジェクト）
  - `images`: 1〜4件 `{"mime_type": "image/jpeg", "data_base64": "..."}`
  - レスポンス: `{"diagnosis": {photo_features, current_state, direction, cautions, ask_stylist, stylist_message, photo_quality}, "model": ..., "usage": ...}`

## 仕様メモ
- 写真はメモリ上だけで処理し、保存しない
- 同一IPから1時間に `RATE_PER_HOUR` 回まで（既定10回）
- Geminiが混雑（503）やモデル終了（404）の場合は次のモデルに自動で切り替える
- CORSは試作段階では全許可。本番ではアプリのドメインに絞る
