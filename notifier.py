"""Discord への通知。Bot Token を使い REST API でチャンネルにメッセージ投稿する。

Gateway 常時接続は不要なので GitHub Actions の定期実行から呼べる。
（/list 等の対話コマンドが欲しくなったら別途 Gateway 常駐 Bot が必要。）
"""
from __future__ import annotations

import os
import time

import requests

API = "https://discord.com/api/v10"

SECTION_COLOR = {
    "受付中": 0x2ECC71,    # 緑
    "近日開始": 0xF1C40F,  # 黄
    "会員限定": 0x3498DB,  # 青
}
SECTION_EMOJI = {"受付中": "🟢", "近日開始": "🟡", "会員限定": "🔵"}


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "pokeca-chusen-bot (https://github.com, 1.0)",
    }


def _post(channel_id: str, token: str, payload: dict) -> None:
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
        return
    raise RuntimeError("Discord への投稿がレート制限で失敗しました")


def lottery_embed(lot) -> dict:
    emoji = SECTION_EMOJI.get(lot.section, "🎴")
    fields = []
    if lot.sale_type:
        fields.append({"name": "形式", "value": lot.sale_type, "inline": True})
    if lot.start:
        fields.append({"name": "開始", "value": lot.start, "inline": True})
    if lot.end:
        fields.append({"name": "締切", "value": f"**{lot.end}**", "inline": True})
    if lot.result_date:
        fields.append({"name": "当選発表", "value": lot.result_date, "inline": False})
    embed = {
        "title": f"{emoji} {lot.store}",
        "description": f"**{lot.product}**",
        "color": SECTION_COLOR.get(lot.section, 0x95A5A6),
        "fields": fields,
        "footer": {"text": f"{lot.source}・{lot.section}"},
    }
    if lot.apply_url:
        embed["url"] = lot.apply_url
        embed["fields"].append(
            {"name": "応募ページ", "value": f"[ここから応募]({lot.apply_url})", "inline": False}
        )
    return embed


def notify_lotteries(lots: list, channel_id: str, token: str) -> None:
    """新着抽選を最大10件/メッセージのembedでまとめて投稿。"""
    if not lots:
        return
    # 受付中→近日開始→会員限定 の順で並べる
    order = {"受付中": 0, "近日開始": 1, "会員限定": 2}
    lots = sorted(lots, key=lambda x: order.get(x.section, 9))
    for i in range(0, len(lots), 10):
        chunk = lots[i : i + 10]
        header = f"🎴 **ポケカ抽選 新着 {len(lots)}件**" if i == 0 else None
        payload = {"embeds": [lottery_embed(l) for l in chunk]}
        if header:
            payload["content"] = header
        _post(channel_id, token, payload)


def notify_text(message: str, channel_id: str, token: str) -> None:
    _post(channel_id, token, {"content": message})


_RESULT_STYLE = {
    "win": ("🎉 当選メールを検知しました！", 0xFFD700, "@here 当選かも！メールを確認してください 🎴"),
    "lose": ("😢 落選メールを検知しました", 0x95A5A6, "抽選結果（落選の可能性）が届きました"),
    "amazon_invite": ("🎊 Amazon 招待当選！", 0xFF9900, "@here Amazonの招待に選ばれました！購入手続きを確認してください 🛒"),
}


def notify_email_result(
    subject: str, sender: str, snippet: str, outcome: str, channel_id: str, token: str
) -> None:
    """当落メール検知の通知。outcome は 'win' / 'lose'。"""
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
        "footer": {"text": "メール監視 (iCloud)"},
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
