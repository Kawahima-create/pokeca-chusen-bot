"""エントリポイント: スクレイプ → 既知分と差分 → タイトル別チャンネルへ振り分け通知 → 状態保存。

state.json に通知済みの抽選ID(uid)と「どのチャンネルへ投稿済みか(posted)」を記録する。
GitHub Actions ではこのファイルをコミットして次回実行に引き継ぐ。

振り分け:
  - 各抽選を classify_titles でタイトル判定し、対応する有料チャンネルへ投稿。
    （混在行は複数チャンネルへ。タイトルチャンネル未設定なら警告してスキップ）
  - 公式大型抽選(is_official_big)は無料チャンネルにも投稿（撒き餌）。
  - dedup は (uid, channel) 単位。同一チャンネルへは二度投稿しない。

初回（state.json が無い）は全件を「各行き先へ投稿済み」としてシードし、個別通知はせず
監視開始メッセージだけ送る（過去分の一斉スパムを防止）。
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
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
        json.dumps({"seen": seen}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _migrate_posted(seen: dict, all_channels: set[str]) -> None:
    """旧形式エントリ（postedキー無し）を「全設定チャンネルへ投稿済み」とみなす。

    旧Botが既に告知済みの抽選を、チャンネル分割後に再爆撃しないための安全側移行。
    """
    for entry in seen.values():
        if "posted" not in entry:
            entry["posted"] = sorted(all_channels)


def _targets_for(lot, channel_map: dict[str, str], free_channel: str,
                 unknown_counter: Counter) -> list[tuple[str, str]]:
    """抽選の投稿先 [(channel_id, header_title), ...] を返す。"""
    targets: list[tuple[str, str]] = []
    for t in sources.classify_titles(lot):
        if t == "unknown":
            unknown_counter["unknown"] += 1
            continue
        ch = channel_map.get(t)
        if ch is None:
            print(f"[warn] {t} 用チャンネル未設定。スキップ: {lot.store} / {lot.product}")
            continue
        targets.append((ch, t))
    if sources.is_official_big(lot) and free_channel:
        targets.append((free_channel, "free"))
    return targets


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    channel_map: dict[str, str] = {}
    free_channel = ""
    token = ""
    if dry_run:
        # 振り分け確認用に env があれば読む（無ければ仮IDで表示）
        channel_map = {t: os.environ.get(e, t).strip() or t
                       for t, e in notifier.TITLE_CHANNEL_ENV.items()}
        free_channel = os.environ.get(notifier.FREE_CHANNEL_ENV, "free").strip() or "free"
    else:
        channel_map, free_channel, token = notifier.get_channel_map()

    all_channels = set(channel_map.values()) | ({free_channel} if free_channel else set())

    state = load_state()
    seen: dict = state.get("seen", {})
    first_run = not STATE_PATH.exists() and not seen
    if not first_run:
        _migrate_posted(seen, all_channels)

    lotteries = sources.fetch_all()
    print(f"[info] 取得した抽選: {len(lotteries)}件")

    # チャンネルごとの投稿リストを組み立て（(uid,channel) で重複排除）
    to_post: dict[tuple[str, str], list] = defaultdict(list)
    unknown_counter: Counter = Counter()
    new_count = 0
    for lot in lotteries:
        targets = _targets_for(lot, channel_map, free_channel, unknown_counter)
        entry = seen.setdefault(
            lot.uid,
            {"store": lot.store, "product": lot.product, "end": lot.end,
             "title": lot.title, "posted": []},
        )
        posted = set(entry.get("posted", []))
        item_new = False
        for ch, htitle in targets:
            if ch in posted:
                continue
            to_post[(ch, htitle)].append(lot)
            item_new = True
        if item_new:
            new_count += 1

    total_new = sum(len(v) for v in to_post.values())
    print(f"[info] 新規投稿対象: {new_count}抽選 / 延べ{total_new}投稿")
    if unknown_counter:
        print(f"[info] タイトル判定不能(unknown): {unknown_counter['unknown']}件（投稿せず記録のみ）")

    if dry_run:
        for (ch, htitle), lots in to_post.items():
            print(f"  → [{htitle}] ch={ch} : {len(lots)}件")
            for lot in lots:
                print(f"      + [{lot.section}] {lot.store} / {lot.product} ~{lot.end}")
        # dry-run でも posted をシードして次回の差分確認に使えるようにする
        for (ch, _), lots in to_post.items():
            for lot in lots:
                seen[lot.uid]["posted"].append(ch)
        save_state(seen)
        return 0

    if first_run:
        print("[info] 初回実行: 既存抽選を各行き先へシードし、個別通知はスキップします。")
        # 監視開始メッセージを設定済み各チャンネルへ
        for title, ch in channel_map.items():
            label = notifier.TITLE_LABEL.get(title, title)
            notifier.notify_text(
                f"🎴 {label}抽選の監視を開始しました。新しい抽選が出るとここに通知します。",
                ch, token,
            )
        if free_channel:
            notifier.notify_text(
                "⚡ 無料版・公式大型抽選の監視を開始しました。誰でも応募できる公式抽選をここに通知します。",
                free_channel, token,
            )
        # 行き先へ投稿済みとしてシード
        for (ch, _), lots in to_post.items():
            for lot in lots:
                seen[lot.uid]["posted"].append(ch)
    else:
        for (ch, htitle), lots in to_post.items():
            notifier.notify_lotteries(lots, ch, token, title=htitle)
            for lot in lots:
                seen[lot.uid]["posted"].append(ch)
        if total_new:
            print(f"[info] 延べ{total_new}投稿を送信しました。")
        else:
            print("[info] 新着なし。通知はスキップしました。")

    # 締切が過ぎた過去の通知メッセージを各チャンネルから掃除する
    for ch in all_channels:
        try:
            notifier.sweep_expired(ch, token)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 締切超過メッセージの掃除に失敗 (ch={ch}): {e}")

    save_state(seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
