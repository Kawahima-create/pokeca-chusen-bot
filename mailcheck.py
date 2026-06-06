"""iCloudメールをIMAPでチェックし、抽選「当選」系メールを検知してDiscord通知する。

GitHub Actionsで定期実行。新規メールのみ対象（mail_state.jsonでUIDを記憶）。
初回実行時は既存メールを「処理済み」としてシードし、過去分は通知しない。

必要な環境変数:
  ICLOUD_EMAIL         … iCloudメールアドレス（例 xxx@icloud.com）
  ICLOUD_APP_PASSWORD  … appleid.apple.comで発行したアプリ専用パスワード
  DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID … 通知Botと同じ
任意:
  ICLOUD_IMAP_USER     … ログインユーザー名（既定はICLOUD_EMAIL。@前だけが必要な場合に指定）
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import re
from email.header import decode_header
from pathlib import Path

import notifier

IMAP_HOST = "imap.mail.me.com"
STATE_PATH = Path(os.environ.get("MAIL_STATE_PATH", "mail_state.json"))
LOOKBACK = 200  # 初回シード以降、安全のため直近この件数までを走査対象に含める

# 判定キーワード（LOSEを先に見て誤検知を防ぐ。「ご当選とはなりませんでした」対策）
LOSE = [
    "落選", "当選とはなりません", "ご当選とはなり", "ご期待に添え", "抽選に外れ",
    "ご縁がなかった", "今回は見送", "ご用意できません", "残念ながら", "ご当選されませんでした",
]
WIN = [
    "当選", "ご当選", "当選されました", "当選しました", "当選者", "購入のご案内",
    "お支払い手続き", "お支払いお手続き", "購入権",
]
CONTEXT = ["抽選", "ポケモン", "ポケカ", "カード", "予約", "BOX"]


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    out = ""
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            try:
                out += text.decode(enc or "utf-8", errors="replace")
            except LookupError:
                out += text.decode("utf-8", errors="replace")
        else:
            out += text
    return out


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _get_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return _decode_part(part)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return re.sub(r"<[^>]+>", " ", _decode_part(part))
        return ""
    return _decode_part(msg)


def _classify(subject: str, body: str) -> str | None:
    """当落を判定。'win' / 'lose' / None（結果メールでない）を返す。"""
    text = f"{subject}\n{body}"
    if not any(c in text for c in CONTEXT):
        return None  # ポケカ抽選と無関係
    if any(k in text for k in LOSE):
        return "lose"
    if any(k in text for k in WIN):
        return "win"
    return None  # 当落どちらの語も無ければ結果メールではない（応募確認等は除外）


def load_state() -> int:
    if STATE_PATH.exists():
        try:
            return int(json.loads(STATE_PATH.read_text(encoding="utf-8")).get("last_uid", 0))
        except (json.JSONDecodeError, ValueError):
            return 0
    return 0


def save_state(last_uid: int) -> None:
    STATE_PATH.write_text(json.dumps({"last_uid": last_uid}, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    e = os.environ.get("ICLOUD_EMAIL", "").strip()
    pw = os.environ.get("ICLOUD_APP_PASSWORD", "").strip()
    user = os.environ.get("ICLOUD_IMAP_USER", "").strip() or e
    channel = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not all([e, pw, channel, token]):
        raise SystemExit("ICLOUD_EMAIL / ICLOUD_APP_PASSWORD / DISCORD_CHANNEL_ID / DISCORD_BOT_TOKEN を設定してください。")

    last_uid = load_state()
    first_run = not STATE_PATH.exists()

    M = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    try:
        M.login(user, pw)
        M.select("INBOX")
        typ, data = M.uid("search", None, f"UID {last_uid + 1}:*")
        uids = [int(x) for x in (data[0].split() if data and data[0] else []) if int(x) > last_uid]
        uids.sort()
        print(f"[info] 新規UID候補: {len(uids)}件 (last_uid={last_uid})")

        if first_run:
            new_last = max(uids) if uids else last_uid
            save_state(new_last)
            print(f"[info] 初回シード完了 last_uid={new_last}。過去分は通知しません。")
            notifier.notify_text("📧 当選メールの監視を開始しました（iCloud）。", channel, token)
            return 0

        hits = 0
        for uid in uids:
            typ, msgdata = M.uid("fetch", str(uid), "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            msg = email.message_from_bytes(msgdata[0][1])
            subject = _decode_header(msg.get("Subject"))
            sender = _decode_header(msg.get("From"))
            body = _get_body(msg)
            outcome = _classify(subject, body)
            if outcome:
                snippet = re.sub(r"\s+", " ", body).strip()[:500]
                notifier.notify_email_result(subject, sender, snippet, outcome, channel, token)
                hits += 1
                print(f"[hit:{outcome}] UID {uid}: {subject[:50]}")

        save_state(max(uids) if uids else last_uid)
        print(f"[info] 結果メール検知: {hits}件 / 走査 {len(uids)}件")
        return 0
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
