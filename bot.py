"""対話Bot（Discord Gatewayに常時接続）。/list で開催中の抽選を一覧表示する。

GitHub Actions の通知Botとは別プロセス。常時稼働ホスト（Railway等）で動かす。
通知Botと同じ Bot Token を使い回せる（REST投稿とGateway接続は併用可能）。

必要な環境変数:
  DISCORD_BOT_TOKEN … 通知Botと同じトークン
  GUILD_ID          … （任意）サーバーID。設定するとそのサーバーで即コマンド反映
"""
from __future__ import annotations

import asyncio
import datetime
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

import notifier
import prices
import sources
import store

HEARTBEAT_PATH = Path(__file__).with_name("bot_heartbeat")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
GUILD_ID = os.environ.get("GUILD_ID", "").strip()

intents = discord.Intents.default()  # メッセージ内容インテントは不要（スラッシュコマンドのみ）


# ---- 申込ボタン（Bot再起動後も動くよう DynamicItem を使用）----
class ApplyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"apply:(?P<uid>[0-9a-f]{6,})",
):
    def __init__(self, uid: str, applied: bool) -> None:
        self.uid = uid
        super().__init__(
            discord.ui.Button(
                label="✅ 申込済（取消）" if applied else "申し込んだ",
                style=discord.ButtonStyle.secondary if applied else discord.ButtonStyle.success,
                custom_id=f"apply:{uid}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        uid = match["uid"]
        return cls(uid, store.is_applied(uid))

    async def callback(self, interaction: discord.Interaction) -> None:
        emb = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        base_title = (emb.title or "").removeprefix("✅ ")
        product = (emb.description or "").strip("*")
        end = next((f.value.strip("*") for f in emb.fields if f.name == "締切"), "")
        at = datetime.datetime.now(sources.JST).isoformat(timespec="minutes")

        applied = store.toggle(self.uid, base_title, product, end, at)
        emb.title = ("✅ " + base_title) if applied else base_title
        await interaction.response.edit_message(embed=emb, view=apply_view(self.uid, applied))


def apply_view(uid: str, applied: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ApplyButton(uid, applied))
    return view


class PokecaBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # 申込ボタンを再起動後も動くよう登録
        self.add_dynamic_items(ApplyButton)
        # GUILD_ID があればそのサーバーへ即同期（グローバル同期は反映に最大1時間かかる）
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"[bot] コマンドをサーバー {GUILD_ID} に同期しました")
        else:
            await self.tree.sync()
            print("[bot] コマンドをグローバル同期しました（反映に時間がかかる場合あり）")


client = PokecaBot()


@tasks.loop(seconds=60)
async def heartbeat() -> None:
    # 接続が健全なときだけ更新。固まる/切断時は更新が止まりウォッチドッグが再起動する
    if client.is_ready():
        HEARTBEAT_PATH.write_text(str(int(time.time())))


@client.event
async def on_ready() -> None:
    print(f"[bot] ログイン成功: {client.user}")
    HEARTBEAT_PATH.write_text(str(int(time.time())))
    if not heartbeat.is_running():
        heartbeat.start()


# 買取価格がこの額以上、または価格データ無し（新弾）のBOXを「投機性あり」とみなす
SPECULATIVE_THRESHOLD = int(os.environ.get("SPECULATIVE_THRESHOLD", "8000"))


def _is_speculative(product: str) -> bool:
    """投機性（転売価値）があるか。価格データ無しは新弾の可能性があるため表示扱い。"""
    try:
        results = prices.search(product)
    except Exception:  # noqa: BLE001
        return True
    if not results:
        return True
    return max(r["price"] for r in results) >= SPECULATIVE_THRESHOLD


def _filter_speculative(lots: list) -> list:
    return [lot for lot in lots if _is_speculative(lot.product)]


@client.tree.command(name="list", description="開催中のポケカ抽選（投機性BOXのみ）を一覧表示します")
async def list_cmd(interaction: discord.Interaction) -> None:
    # スクレイプに数秒かかるので、まず「考え中…」で応答時間を確保
    await interaction.response.defer(thinking=True)

    lots = await asyncio.to_thread(sources.fetch_all)
    lots = await asyncio.to_thread(_filter_speculative, lots)
    if not lots:
        await interaction.followup.send(
            "いま投機性のある（転売価値が見込める）BOXの抽選は見つかりませんでした。"
        )
        return

    order = {"受付中": 0, "近日開始": 1, "会員限定": 2}
    lots.sort(key=lambda x: order.get(x.section, 9))

    shown = lots[:10]  # 申込ボタンを個別に付けるため1抽選=1メッセージ
    extra = len(lots) - len(shown)
    head = f"💎 投機性BOXの抽選 **{len(lots)}件**"
    if extra > 0:
        head += f"（先頭{len(shown)}件を表示）"
    await interaction.followup.send(content=head)

    for lot in shown:
        applied = store.is_applied(lot.uid)
        data = notifier.lottery_embed(lot)
        if applied:
            data["title"] = "✅ " + data["title"]
        emb = discord.Embed.from_dict(data)
        await interaction.followup.send(embed=emb, view=apply_view(lot.uid, applied))


@client.tree.command(name="applied", description="申し込み済みの抽選を一覧表示します")
async def applied_cmd(interaction: discord.Interaction) -> None:
    data = store.list_applied()
    if not data:
        await interaction.response.send_message(
            "まだ申込済みの抽選はありません。`/list` から「申し込んだ」を押すと記録されます。",
            ephemeral=True,
        )
        return
    lines = []
    for v in data.values():
        title = v.get("label", "?")
        prod = v.get("product", "")
        end = v.get("end") or "-"
        lines.append(f"✅ {title} … {prod}（締切: {end}）")
    await interaction.response.send_message(
        f"**申込済みの抽選（{len(lines)}件）**\n" + "\n".join(lines),
        ephemeral=True,
    )


@client.tree.command(name="price", description="BOX名から買取価格を比較（高い順）")
@app_commands.describe(box="BOX名（例: アビスアイ / イーブイヒーローズ / スタートデッキ100）")
async def price_cmd(interaction: discord.Interaction, box: str) -> None:
    await interaction.response.defer(thinking=True)
    results = await asyncio.to_thread(prices.search, box)
    if not results:
        await interaction.followup.send(
            f"「{box}」に一致する買取価格が見つかりませんでした。BOX名を変えて試してください。"
        )
        return

    lines = []
    for i, r in enumerate(results):
        mark = "👑 " if i == 0 else "・"
        url = r.get("url", "")
        shop = f"[{r['shop']}]({url})" if url else r["shop"]
        lines.append(f"{mark}**{shop}**: ¥{r['price']:,}　（{r['name']}）")

    top = results[0]
    emb = discord.Embed(
        title=f"💰 「{box}」の買取価格",
        description="\n".join(lines),
        color=0x2ECC71,
    )
    emb.set_footer(text=f"一番高いのは {top['shop']}（¥{top['price']:,}）／価格は変動します")
    await interaction.followup.send(embed=emb)


def main() -> None:
    if not TOKEN:
        raise SystemExit("環境変数 DISCORD_BOT_TOKEN を設定してください。")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
