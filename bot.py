"""対話Bot（Discord Gatewayに常時接続）。/list で開催中の抽選を一覧表示する。

GitHub Actions の通知Botとは別プロセス。常時稼働ホスト（Railway等）で動かす。
通知Botと同じ Bot Token を使い回せる（REST投稿とGateway接続は併用可能）。

必要な環境変数:
  DISCORD_BOT_TOKEN … 通知Botと同じトークン
  GUILD_ID          … （任意）サーバーID。設定するとそのサーバーで即コマンド反映
"""
from __future__ import annotations

import asyncio
import os

import discord
from discord import app_commands

import notifier
import sources

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
GUILD_ID = os.environ.get("GUILD_ID", "").strip()

intents = discord.Intents.default()  # メッセージ内容インテントは不要（スラッシュコマンドのみ）


class PokecaBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
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


@client.event
async def on_ready() -> None:
    print(f"[bot] ログイン成功: {client.user}")


@client.tree.command(name="list", description="開催中のポケカ抽選を一覧表示します")
async def list_cmd(interaction: discord.Interaction) -> None:
    # スクレイプに数秒かかるので、まず「考え中…」で応答時間を確保
    await interaction.response.defer(thinking=True)

    lots = await asyncio.to_thread(sources.fetch_all)
    if not lots:
        await interaction.followup.send("いま受付中の抽選は見つかりませんでした。")
        return

    order = {"受付中": 0, "近日開始": 1, "会員限定": 2}
    lots.sort(key=lambda x: order.get(x.section, 9))

    # Discordは1メッセージにつき最大10 embed
    embeds = [discord.Embed.from_dict(notifier.lottery_embed(l)) for l in lots[:10]]
    extra = len(lots) - len(embeds)
    content = f"🎴 開催中の抽選 **{len(lots)}件**"
    if extra > 0:
        content += f"（先頭{len(embeds)}件を表示）"
    await interaction.followup.send(content=content, embeds=embeds)


def main() -> None:
    if not TOKEN:
        raise SystemExit("環境変数 DISCORD_BOT_TOKEN を設定してください。")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
