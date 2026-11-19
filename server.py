#!/usr/bin/env python3
"""MUSE diagnosis server.

Receives photos + answers from the app, calls Gemini, returns the diagnosis JSON.
The API key never lives in the app. On a real host, set GEMINI_API_KEY in the
environment. When run inside the Muse dev environment without that variable,
it falls back to the registered custom.gemini credential via surrogate.

Stdlib only. Photos are processed in memory and never stored.
"""
import json, os, sys, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GEMINI_HOST = "generativelanguage.googleapis.com"
MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-3-flash-preview,gemini-3.5-flash-lite").split(",") if m.strip()]
MAX_BODY = 12 * 1024 * 1024
RATE_PER_HOUR = int(os.environ.get("RATE_PER_HOUR", "10"))
ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp"}

_hits = {}  # ip -> [timestamps]

PROMPT_TEMPLATE = """あなたはヘアカラーの状態分析アシスタントです。写真と施術履歴から美容師への相談材料を作ることが仕事です。
添付された写真と以下の質問回答をもとに、決められたJSON形式だけで答えてください。JSON以外の文字は出さないでください。

写真から観察してよいこと（見た目に限る）:
- 根元と毛先の明るさの差、色ムラの有無と場所
- 黄み／オレンジみの見え方、色の抜け具合
- 根元の伸びの目立ち方
- ツヤ感・パサつきの「見え方」

禁止事項:
- 内部ダメージの断定
- 薬剤の安全性の断定（ブリーチしても大丈夫等）
- 具体的な薬剤の配合比・放置時間・オキシ濃度などの施術指示
- 医療的判断

表現ルール:
- 観察は「〜のように見えます」「写真では〜」と根拠付き・控えめに
- 写真で確認できないことは推測で断定せず、確認できないと正直に書く
- 履歴の影響は「可能性」として弱めに述べ、確認事項に回す
- 写真が暗い・ブレている・髪が写っていない等で判断できない場合は、無理に診断せず photo_quality で再撮影をお願いし、他の項目にもその旨を書く

質問回答:
{answers}

出力JSONの項目:
photo_features, current_state, direction, cautions, ask_stylist, stylist_message, photo_quality
すべて日本語の文字列で書いてください。"""

def format_answers(answers):
    if isinstance(answers, str):
        return answers[:4000]
    if isinstance(answers, dict):
        lines = []
        for k in sorted(answers.keys()):
            v = answers[k]
            if isinstance(v, list):
                v = "、".join(str(x) for x in v)
            lines.append(f"{k}: {v}")
        return "\n".join(lines)[:4000]
    return ""

def call_gemini(parts):
    last_err = None
    for model in MODELS:
        url = f"https://{GEMINI_HOST}/v1beta/models/{model}:generateContent"
        payload = {"contents": [{"parts": parts}],
                   "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        env_key = os.environ.get("GEMINI_API_KEY")
        if env_key:
            req.add_header("x-goog-api-key", env_key)
        else:
            try:
                sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
                from dynamic_credentials import add_surrogate_to_request
            except ImportError:
                raise RuntimeError("GEMINI_API_KEY is not set on the server")
            add_surrogate_to_request(req, "custom.gemini", allowed_hosts=[GEMINI_HOST])
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text), model, data.get("usageMetadata", {})
        except urllib.error.HTTPError as e:
            last_err = f"{model}: HTTP {e.code}"
            if e.code in (404, 503):
                continue
            raise RuntimeError(last_err)
        except Exception as e:
            last_err = f"{model}: {e}"
            continue
    raise RuntimeError(f"all models failed: {last_err}")

def rate_limited(ip):
    now = time.time()
    hits = [t for t in _hits.get(ip, []) if now - t < 3600]
    if len(hits) >= RATE_PER_HOUR:
        _hits[ip] = hits
        return True
    hits.append(now)
    _hits[ip] = hits
    return False

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"ok": True, "service": "muse-diagnosis"})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/diagnose":
            self.send_json(404, {"error": "not found"})
            return
        if rate_limited(self.client_address[0]):
            self.send_json(429, {"error": "rate_limited", "message": "診断の上限に達しました。時間を置いてお試しください。"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self.send_json(413, {"error": "body_too_large_or_empty"})
            return
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self.send_json(400, {"error": "invalid_json"})
            return
        if req.get("consent") is not True:
            self.send_json(400, {"error": "consent_required", "message": "AI分析のために写真を外部AIサービスへ送信することへの同意が必要です。"})
            return
        images = req.get("images") or []
        if not isinstance(images, list) or len(images) == 0 or len(images) > 4:
            self.send_json(400, {"error": "images_required_1_to_4"})
            return
        parts = [{"text": PROMPT_TEMPLATE.format(answers=format_answers(req.get("answers", "")))}]
        for img in images:
            mime = (img or {}).get("mime_type") or (img or {}).get("mime") or "image/jpeg"
            data = (img or {}).get("data_base64") or (img or {}).get("data") or ""
            if mime not in ALLOWED_MIMES or not data or len(data) > 8 * 1024 * 1024:
                self.send_json(400, {"error": "invalid_image"})
                return
            parts.append({"inline_data": {"mime_type": mime, "data": data}})
        try:
            diagnosis, model, usage = call_gemini(parts)
        except Exception as e:
            self.send_json(502, {"error": "ai_failed", "message": "AI分析に失敗しました。時間を置いてお試しください。", "detail": str(e)[:200]})
            return
        self.send_json(200, {"diagnosis": diagnosis, "model": model, "usage": usage})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
