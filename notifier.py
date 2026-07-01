"""Discord への通知。Bot Token を使い REST API でチャンネルにメッセージ投稿する。

Gateway 常時接続は不要なので GitHub Actions の定期実行から呼べる。
（/list 等の対話コマンドが欲しくなったら別途 Gateway 常駐 Bot が必要。）
"""
from __future__ import annotations

import datetime
import os
import time

import requests

import sources  # parse_jp_deadline を流用（締切超過メッセージの掃除に使う）

API = "https://discord.com/api/v10"

SECTION_COLOR = {
    "受付中": 0x2ECC71,    # 緑
    "近日開始": 0xF1C40F,  # 黄
    "会員限定": 0x3498DB,  # 青
}
SECTION_EMOJI = {"受付中": "🟢", "近日開始": "🟡", "会員限定": "🔵"}

# タイトル別の通知ヘッダ（先頭メッセージに付く新着件数の見出し）
TITLE_HEADER = {
    "pokeca":   "🎴 ポケカ抽選 新着 {n}件",
    "onepiece": "🏴‍☠️ ワンピカ抽選 新着 {n}件",
    "yugioh":   "🔮 遊戯王抽選 新着 {n}件",
    "free":     "⚡ 【無料版】公式大型抽選 新着 {n}件",
}
# embed フッタ用のタイトルラベル
TITLE_LABEL = {"pokeca": "ポケカ", "onepiece": "ワンピ", "yugioh": "遊戯王"}

# タイトル→チャンネルID env 名
TITLE_CHANNEL_ENV = {
    "pokeca":   "DISCORD_CHANNEL_POKECA",
    "onepiece": "DISCORD_CHANNEL_ONEPIECE",
    "yugioh":   "DISCORD_CHANNEL_YUGIOH",
}
FREE_CHANNEL_ENV = "DISCORD_CHANNEL_FREE"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "pokeca-chusen-bot (https://github.com, 1.0)",
    }


def _post(channel_id: str, token: str, payload: dict) -> dict:
    url = f"{API}/channels/{channel_id}/messages"
    for attempt in range(5):
        r = requests.post(url, headers=_headers(token), json=payload, timeout=20)
        if r.status_code == 429:  # レート制限
            retry = float(r.json().get("retry_after", 1.0))
            print(f"[discord] 429 rate limited, retry in {retry}s")
            time.sleep(retry + 0.5)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Discord API {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError("Discord への投稿がレート制限で失敗しました")


def _delete_message(channel_id: str, token: str, message_id: str) -> bool:
    """メッセージを削除。成功/既に無い(404)なら True。Bot自身の投稿は追加権限不要。"""
    url = f"{API}/channels/{channel_id}/messages/{message_id}"
    for attempt in range(5):
        r = requests.delete(url, headers=_headers(token), timeout=20)
        if r.status_code == 429:
            retry = float(r.json().get("retry_after", 1.0))
            print(f"[discord] 429 (delete) retry in {retry}s")
            time.sleep(retry + 0.5)
            continue
        if r.status_code == 404:
            return True  # 既に消えている
        if r.status_code >= 400:
            print(f"[discord] 削除失敗 {r.status_code}: {r.text[:200]}")
            return False
        return True
    return False


def _get_bot_user_id(token: str) -> str | None:
    try:
        r = requests.get(f"{API}/users/@me", headers=_headers(token), timeout=20)
        if r.status_code == 200:
            return str(r.json().get("id") or "") or None
    except Exception as e:  # noqa: BLE001
        print(f"[discord] /users/@me 取得失敗: {e}")
    return None


def _fetch_messages(channel_id: str, token: str, limit: int = 100, before: str | None = None) -> list[dict]:
    url = f"{API}/channels/{channel_id}/messages"
    params: dict[str, str] = {"limit": str(limit)}
    if before:
        params["before"] = before
    for attempt in range(5):
        r = requests.get(url, headers=_headers(token), params=params, timeout=20)
        if r.status_code == 429:
            retry = float(r.json().get("retry_after", 1.0))
            print(f"[discord] 429 (history) retry in {retry}s")
            time.sleep(retry + 0.5)
            continue
        if r.status_code >= 400:
            print(f"[discord] 履歴取得失敗 {r.status_code}: {r.text[:200]}")
            return []
        return r.json()
    return []


def _embed_is_expired(emb: dict, now: datetime.datetime) -> bool:
    """embed が「締切超過」か。締切フィールドが無い/未来/解析不能なら False（残す扱い）。

    恒常受付（締切なし）や当落通知・テキストのembedは False を返すので消されない。
    """
    has_deadline = False
    for f in emb.get("fields", []) or []:
        if f.get("name") == "締切":
            dt = sources.parse_jp_deadline(f.get("value", ""), now)
            if dt is not None:
                has_deadline = True
                if dt >= now:
                    return False  # 1つでも未来の締切があれば残す
    return has_deadline  # 締切があり、全て過去のときだけ True


def _message_is_expired(msg: dict, now: datetime.datetime) -> bool:
    """メッセージ単位の削除判定。embed が1つ以上あり、その全てが締切超過なら True。

    バッチ投稿（複数embed）に締切なし/未来の抽選が1つでも含まれれば、
    そのメッセージは丸ごと残す（巻き添え削除を防ぐ安全側の判定）。
    """
    embeds = msg.get("embeds", []) or []
    if not embeds:
        return False
    return all(_embed_is_expired(e, now) for e in embeds)


def sweep_expired(
    channel_id: str, token: str, now: datetime.datetime | None = None, max_pages: int = 3
) -> int:
    """チャンネル内のBot自身の投稿のうち、締切が過ぎたものを削除する。

    判定: メッセージ内に解析可能な「締切」が1つ以上あり、その全てが現在より過去なら削除。
    （一部だけ期限切れのバッチ投稿は誤って消さない安全側の判定）。
    締切フィールドが無い投稿（恒常受付・監視開始/見出しテキスト・当落通知）は対象外。
    """
    now = now or datetime.datetime.now(sources.JST)
    bot_id = _get_bot_user_id(token)
    if not bot_id:
        print("[discord] Bot ユーザーID不明のため掃除をスキップ")
        return 0

    deleted = 0
    before: str | None = None
    for _ in range(max_pages):
        msgs = _fetch_messages(channel_id, token, limit=100, before=before)
        if not msgs:
            break
        for msg in msgs:
            author = (msg.get("author") or {}).get("id")
            if str(author) != bot_id:
                continue
            if _message_is_expired(msg, now):
                if _delete_message(channel_id, token, str(msg.get("id"))):
                    deleted += 1
                    emb0 = (msg.get("embeds") or [{}])[0]
                    print(f"[discord] 締切超過を削除: {emb0.get('title','?')} / {emb0.get('description','')}")
        before = str(msgs[-1].get("id"))
        if len(msgs) < 100:
            break
    if deleted:
        print(f"[info] 締切超過メッセージを {deleted}件 削除しました")
    return deleted


def lottery_embed(lot) -> dict:
    emoji = SECTION_EMOJI.get(lot.section, "🎴")
    no_shrink = getattr(lot, "no_shrink", False)
    fields = []
    if no_shrink:
        fields.append(
            {"name": "⚠️ 状態", "value": "**シュリンクなし**（未開封フィルムなし）", "inline": False}
        )
    if lot.sale_type:
        fields.append({"name": "形式", "value": lot.sale_type, "inline": True})
    if lot.start:
        fields.append({"name": "開始", "value": lot.start, "inline": True})
    if lot.end:
        fields.append({"name": "締切", "value": f"**{lot.end}**", "inline": True})
    if lot.result_date:
        fields.append({"name": "当選発表", "value": lot.result_date, "inline": False})
    title = f"{emoji} {lot.store}"
    if no_shrink:
        title += "　⚠️シュリンクなし"
    label = TITLE_LABEL.get(getattr(lot, "title", ""), "")
    footer = f"{lot.source}・{lot.section}" + (f"・{label}" if label else "")
    embed = {
        "title": title,
        "description": f"**{lot.product}**",
        "color": SECTION_COLOR.get(lot.section, 0x95A5A6),
        "fields": fields,
        "footer": {"text": footer},
    }
    if lot.apply_url:
        embed["url"] = lot.apply_url
        embed["fields"].append(
            {"name": "応募ページ", "value": f"[ここから応募]({lot.apply_url})", "inline": False}
        )
    return embed


def notify_lotteries(lots: list, channel_id: str, token: str, *, title: str = "pokeca") -> None:
    """新着抽選を1件=1メッセージで投稿する。

    1メッセージ1抽選にしておくと、締切が過ぎた抽選だけを後から個別に
    削除できる（sweep_expired）。先頭メッセージにタイトル別の新着件数見出しを付ける。
    title は "pokeca"/"onepiece"/"yugioh"/"free" のいずれか。
    """
    if not lots:
        return
    # 受付中→近日開始→会員限定 の順で並べる
    order = {"受付中": 0, "近日開始": 1, "会員限定": 2}
    lots = sorted(lots, key=lambda x: order.get(x.section, 9))
    header = TITLE_HEADER.get(title, "🎴 抽選 新着 {n}件").format(n=len(lots))
    for i, lot in enumerate(lots):
        payload: dict = {"embeds": [lottery_embed(lot)]}
        if i == 0:
            payload["content"] = f"**{header}**"
        _post(channel_id, token, payload)


def notify_text(message: str, channel_id: str, token: str) -> None:
    _post(channel_id, token, {"content": message})


_RESULT_STYLE = {
    "win": ("🎉 当選メールを検知しました！", 0xFFD700, "@here 当選かも！メールを確認してください 🎴"),
    "lose": ("😢 落選メールを検知しました", 0x95A5A6, "抽選結果（落選の可能性）が届きました"),
    "amazon_invite": ("🎊 Amazon 招待当選！", 0xFF9900, "@here Amazonの招待に選ばれました！購入手続きを確認してください 🛒"),
}


def notify_email_result(
    subject: str, sender: str, snippet: str, outcome: str, channel_id: str, token: str,
    source: str = "iCloud",
) -> None:
    """当落メール検知の通知。outcome は 'win' / 'lose' / 'amazon_invite'。source は検知元メール。"""
    title, color, content = _RESULT_STYLE.get(
        outcome, ("📋 抽選結果メールを検知しました", 0x3498DB, "抽選結果メールが届きました")
    )
    embed = {
        "title": title,
        "color": color,
        "fields": [
            {"name": "件名", "value": (subject or "(件名なし)")[:1024], "inline": False},
            {"name": "差出人", "value": (sender or "(不明)")[:1024], "inline": False},
            {"name": "本文（抜粋）", "value": (snippet or "")[:1000] or "(本文なし)", "inline": False},
        ],
        "footer": {"text": f"メール監視 ({source})"},
    }
    _post(channel_id, token, {"content": content, "embeds": [embed]})


def get_credentials() -> tuple[str, str]:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not token or not channel:
        raise SystemExit(
            "環境変数 DISCORD_BOT_TOKEN と DISCORD_CHANNEL_ID を設定してください。"
        )
    return channel, token


def get_channel_map() -> tuple[dict[str, str], str, str]:
    """({title: channel_id}, free_channel_id, token) を返す（タイトル別振り分け用）。

    後方互換: 新varが1つも無く旧 DISCORD_CHANNEL_ID のみのときは pokeca にマップし、
    従来通り単一チャンネル運用になる。タイトルチャンネル未設定はマップから除外する
    （呼び側が該当タイトルを警告してスキップ）。トークン欠如は SystemExit。
    """
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("環境変数 DISCORD_BOT_TOKEN を設定してください。")

    channel_map: dict[str, str] = {}
    for title, env_name in TITLE_CHANNEL_ENV.items():
        cid = os.environ.get(env_name, "").strip()
        if cid:
            channel_map[title] = cid
    free = os.environ.get(FREE_CHANNEL_ENV, "").strip()

    if not channel_map and not free:
        legacy = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
        if legacy:
            channel_map["pokeca"] = legacy

    if not channel_map and not free:
        raise SystemExit(
            "通知先チャンネルが未設定です。DISCORD_CHANNEL_POKECA / ONEPIECE / YUGIOH / FREE "
            "（または旧 DISCORD_CHANNEL_ID）のいずれかを設定してください。"
        )
    return channel_map, free, token
