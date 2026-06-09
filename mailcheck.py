"""iCloudメールをIMAPでチェックし、抽選「当選」系メールを検知してDiscord通知する。

GitHub Actionsで定期実行。新規メールのみ対象（mail_state.jsonでUIDを記憶）。
初回実行時は既存メールを「処理済み」としてシードし、過去分は通知しない。

複数メール対応（設定されたアカウントだけ巡回）:
  ICLOUD_EMAIL / ICLOUD_APP_PASSWORD … iCloud（appleid.apple.comのアプリ専用パスワード）
  GMAIL_EMAIL  / GMAIL_APP_PASSWORD  … Gmail（myaccount.google.com/apppasswordsのアプリパスワード）
  DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID … 通知Botと同じ
任意:
  ICLOUD_IMAP_USER … iCloudログインユーザー名（既定はICLOUD_EMAIL）
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

# 監視するメールアカウント。email_env/pw_env が両方設定されているものだけ巡回する。
ACCOUNTS = [
    {"key": "iCloud", "host": "imap.mail.me.com",
     "email_env": "ICLOUD_EMAIL", "pw_env": "ICLOUD_APP_PASSWORD", "user_env": "ICLOUD_IMAP_USER"},
    {"key": "Gmail", "host": "imap.gmail.com",
     "email_env": "GMAIL_EMAIL", "pw_env": "GMAIL_APP_PASSWORD", "user_env": ""},
]
STATE_PATH = Path(os.environ.get("MAIL_STATE_PATH", "mail_state.json"))
LOOKBACK = 200  # 初回シード以降、安全のため直近この件数までを走査対象に含める

# 判定キーワード（LOSEを先に見て誤検知を防ぐ。「ご当選とはなりませんでした」対策）
# 落選（特定的な句。当選判定より先にチェックする＝「ご当選とはなりませんでした」対策）
LOSE = [
    "落選", "当選とはなりません", "ご当選とはなり", "ご当選されませんでした", "ご期待に添え",
    "抽選に外れ", "ご縁がなかった", "今回は見送", "ご用意できません",
]
LOSE_GENERIC = ["残念ながら"]  # 汎用的なので文脈ありの時だけ落選扱い
# 当選（特定的な句は文脈なしでも検知）
WIN_STRONG = [
    "ご当選", "当選されました", "当選しました", "当選のご案内", "ご当選おめでとう",
    "当選者様", "購入のご案内", "お支払い手続き", "お支払いお手続き",
]
WIN_WEAK = ["当選", "当選者", "購入権"]  # 汎用的なので文脈ありの時だけ当選扱い
CONTEXT = ["抽選", "ポケモン", "ポケカ", "カード", "予約", "BOX"]
# Amazon招待制の当選（件名「おめでとうございます。招待者に選ばれました」等）。
# 文脈ワードを含まないため専用トリガーとして扱う。
INVITE = ["招待者に選ばれました", "招待者に選出", "招待者に当選", "招待者に選定"]


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
    """当落を判定。'amazon_invite' / 'win' / 'lose' / None（結果メールでない）を返す。"""
    text = f"{subject}\n{body}"
    has_context = any(c in text for c in CONTEXT)

    # 1. Amazon招待制の当選（文脈ワードを含まないので最優先・文脈不要）
    if any(k in text for k in INVITE):
        return "amazon_invite"
    # 2. 落選（特定句は文脈不要。当選より先に判定し「ご当選とはなりません」を取りこぼさない）
    if any(k in text for k in LOSE):
        return "lose"
    # 3. 当選（特定句は文脈不要、汎用句は文脈ありの時のみ）
    if any(k in text for k in WIN_STRONG):
        return "win"
    if has_context and any(k in text for k in WIN_WEAK):
        return "win"
    # 4. 汎用的な落選句は文脈ありの時のみ
    if has_context and any(k in text for k in LOSE_GENERIC):
        return "lose"
    return None


def load_state() -> dict:
    """{"iCloud": {"last_uid": N}, "Gmail": {"last_uid": M}} 形式。旧形式は移行。"""
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    # 旧形式 {"last_uid": N} を iCloud のものとして移行
    if "last_uid" in data:
        return {"iCloud": {"last_uid": int(data["last_uid"])}}
    return data


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def process_account(spec: dict, email_addr: str, pw: str, user: str,
                    last_uid: int, first_run: bool, channel: str, token: str) -> int:
    """1アカウントを処理し、新しい last_uid を返す。"""
    key = spec["key"]
    M = imaplib.IMAP4_SSL(spec["host"], 993)
    try:
        M.login(user, pw)
        M.select("INBOX")
        _, data = M.uid("search", None, f"UID {last_uid + 1}:*")
        uids = [int(x) for x in (data[0].split() if data and data[0] else []) if int(x) > last_uid]
        uids.sort()
        print(f"[{key}] 新規UID候補: {len(uids)}件 (last_uid={last_uid})")

        if first_run:
            new_last = max(uids) if uids else last_uid
            notifier.notify_text(f"📧 当選メールの監視を開始しました（{key}: {email_addr}）。", channel, token)
            print(f"[{key}] 初回シード完了 last_uid={new_last}。過去分は通知しません。")
            return new_last

        hits = 0
        for uid in uids:
            _, msgdata = M.uid("fetch", str(uid), "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            msg = email.message_from_bytes(msgdata[0][1])
            subject = _decode_header(msg.get("Subject"))
            sender = _decode_header(msg.get("From"))
            body = _get_body(msg)
            outcome = _classify(subject, body)
            if outcome:
                snippet = re.sub(r"\s+", " ", body).strip()[:500]
                notifier.notify_email_result(subject, sender, snippet, outcome, channel, token, source=key)
                hits += 1
                print(f"[{key}][hit:{outcome}] UID {uid}: {subject[:50]}")

        print(f"[{key}] 結果メール検知: {hits}件 / 走査 {len(uids)}件")
        return max(uids) if uids else last_uid
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    channel = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not (channel and token):
        raise SystemExit("DISCORD_CHANNEL_ID / DISCORD_BOT_TOKEN を設定してください。")

    state = load_state()
    handled = 0
    for spec in ACCOUNTS:
        email_addr = os.environ.get(spec["email_env"], "").strip()
        pw = os.environ.get(spec["pw_env"], "").strip()
        if not (email_addr and pw):
            print(f"[{spec['key']}] 未設定のためスキップ")
            continue
        handled += 1
        user = (os.environ.get(spec["user_env"], "").strip() if spec["user_env"] else "") or email_addr
        acct = state.get(spec["key"], {})
        last_uid = int(acct.get("last_uid", 0))
        first_run = spec["key"] not in state
        try:
            new_last = process_account(spec, email_addr, pw, user, last_uid, first_run, channel, token)
            state[spec["key"]] = {"last_uid": new_last}
        except Exception as e:  # noqa: BLE001
            print(f"[{spec['key']}] 失敗（今回スキップ）: {type(e).__name__}: {e}")

    save_state(state)
    if handled == 0:
        raise SystemExit("監視対象メールが未設定です（ICLOUD_/GMAIL_ の各Secretを確認）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
