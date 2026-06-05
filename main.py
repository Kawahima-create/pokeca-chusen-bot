"""エントリポイント: スクレイプ → 既知分と差分 → 新着をDiscord通知 → 状態保存。

state.json に通知済みの抽選ID(uid)を記録する。GitHub Actions では
このファイルをコミットして次回実行に引き継ぐ。

初回（state.json が無い）は全件を「既知」として記録し、個別通知はせず
監視開始メッセージだけ送る（過去分の一斉スパムを防止）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import notifier
import sources

STATE_PATH = Path(os.environ.get("STATE_PATH", "state.json"))
MAX_REMEMBERED = 500  # 肥大化防止


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[warn] state.json が壊れています。新規作成します。")
    return {}


def save_state(seen: dict) -> None:
    # 古いものから切り詰め（挿入順を保持）
    if len(seen) > MAX_REMEMBERED:
        seen = dict(list(seen.items())[-MAX_REMEMBERED:])
    STATE_PATH.write_text(
        json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    channel = token = ""
    if not dry_run:
        channel, token = notifier.get_credentials()

    state = load_state()
    seen: dict = state.get("seen", {})
    first_run = not STATE_PATH.exists() and not seen

    lotteries = sources.fetch_all()
    print(f"[info] 取得した抽選: {len(lotteries)}件")

    new = [lot for lot in lotteries if lot.uid not in seen]
    print(f"[info] 新着: {len(new)}件")

    # 状態更新（新着を既知に追加）
    for lot in lotteries:
        if lot.uid not in seen:
            seen[lot.uid] = {"store": lot.store, "product": lot.product, "end": lot.end}

    if dry_run:
        for lot in new:
            print(f"  + [{lot.section}] {lot.store} / {lot.product} ~{lot.end}")
        save_state({"seen": seen})
        return 0

    if first_run:
        print("[info] 初回実行: 既存抽選をシードし、個別通知はスキップします。")
        notifier.notify_text(
            f"🎴 ポケカ抽選監視を開始しました。現在 **{len(lotteries)}件** の抽選を監視中です。"
            "\n新しい抽選が追加されると、このチャンネルに通知します。",
            channel,
            token,
        )
    elif new:
        notifier.notify_lotteries(new, channel, token)
        print(f"[info] {len(new)}件を通知しました。")
    else:
        print("[info] 新着なし。通知はスキップしました。")

    save_state({"seen": seen})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
