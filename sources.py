"""抽選情報ソースのスクレイピング。

現状の通知トリガーは入荷Now (nyuka-now.com) に一本化している。
理由: 入荷Nowは時限抽選を構造化テーブルで持ち、ポケセン公式・主要店舗・
Tokyo Otaku Mode などを網羅しているため、1ソースで広くカバーできる。
torecamap は恒常ガイド記事のため通知トリガーには使わず、参考情報として扱う。
"""
from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass, asdict

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:  # zoneinfoが無い環境向けフォールバック
    JST = datetime.timezone(datetime.timedelta(hours=9))

# 「6月8日(月)16:59」のような日本語日付（時刻は任意）を拾う
_DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日(?:（[^）]*）|\([^)]*\))?\s*(?:(\d{1,2}):(\d{2}))?")

NYUKA_URL = "https://nyuka-now.com/archives/2459"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 pokeca-chusen-bot"

# 通知対象とするセクション見出し（部分一致）。エントリ見出しはこれらを含まない。
WANTED_SECTIONS = {
    "応募受付中のストア": "受付中",
    "近日受付開始予定": "近日開始",
    "会員限定": "会員限定",
}
# ここに来たら以降は通知対象外（先着・在庫・履歴・終了）
STOP_SECTIONS = ("在庫あり", "先着販売", "販売履歴", "通知履歴", "応募受付終了", "過去の")


@dataclass
class Lottery:
    source: str          # "入荷Now"
    section: str         # 受付中 / 近日開始 / 会員限定
    store: str           # ポケモンセンターオンライン 等
    product: str         # 対象商品
    start: str           # 開始日
    end: str             # 終了日
    result_date: str     # 当選発表
    sale_type: str       # 抽選形式 / 販売形式
    apply_url: str       # 応募/詳細ページURL
    source_url: str      # 取得元ページ

    @property
    def uid(self) -> str:
        """店舗+商品+開始日 で安定ID。締切等が後から更新されても再通知しない。"""
        raw = f"{self.store}|{self.product}|{self.start}".strip()
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _next_table_before_heading(el):
    """el の直後、次の見出しが来る前に現れる最初の <table> を返す。"""
    for sib in el.find_all_next():
        if sib.name in ("h2", "h3"):
            return None
        if sib.name == "table":
            return sib
    return None


def _parse_table(table) -> dict:
    """ラベル|値 の2セル行を辞書化。値内の最初のリンクも拾う。"""
    data: dict[str, str] = {}
    links: dict[str, str] = {}
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        key = _clean(cells[0].get_text())
        val = _clean(cells[1].get_text())
        if not key:
            continue
        data[key] = val
        a = cells[1].find("a", href=True)
        if a:
            links[key] = a["href"]
    data["__links__"] = links  # type: ignore[assignment]
    return data


def _section_of(text: str) -> str | None:
    for needle, label in WANTED_SECTIONS.items():
        if needle in text:
            return label
    return None


def fetch_nyuka(timeout: int = 25) -> list[Lottery]:
    resp = requests.get(NYUKA_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    results: list[Lottery] = []
    section: str | None = None

    for el in soup.find_all(["h2", "h3"]):
        text = _clean(el.get_text())
        if not text:
            continue

        # セクション見出し判定
        if any(s in text for s in STOP_SECTIONS):
            section = None
            continue
        matched = _section_of(text)
        if matched:
            section = matched
            continue

        # ここはエントリ見出し（店舗名）
        if section is None:
            continue
        table = _next_table_before_heading(el)
        if table is None:
            continue
        d = _parse_table(table)
        links = d.get("__links__", {})  # type: ignore[assignment]
        apply_url = (
            links.get("応募ページ")
            or links.get("詳細ページ")
            or links.get("商品ページ")
            or ""
        )
        product = d.get("対象商品", "")
        if not product:  # 対象商品が無いブロックは抽選エントリではない
            continue
        results.append(
            Lottery(
                source="入荷Now",
                section=section,
                store=text,
                product=product,
                start=d.get("開始日", ""),
                end=d.get("終了日", ""),
                result_date=d.get("当選発表", ""),
                sale_type=d.get("抽選形式") or d.get("販売形式", ""),
                apply_url=apply_url,
                source_url=NYUKA_URL,
            )
        )
    return results


def parse_jp_deadline(text: str, now: datetime.datetime) -> datetime.datetime | None:
    """「6月8日(月)16:59」等をJSTのdatetimeに変換。年は推定。解析不能ならNone。"""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    hour = int(m.group(3)) if m.group(3) else 23
    minute = int(m.group(4)) if m.group(4) else 59
    try:
        dt = datetime.datetime(now.year, month, day, hour, minute, tzinfo=JST)
    except ValueError:
        return None
    # 年跨ぎ補正: 現在より60日以上"過去"に見えるなら翌年扱い（12月→1月対策）
    if (now - dt).days > 60:
        try:
            dt = dt.replace(year=now.year + 1)
        except ValueError:
            pass
    return dt


def is_expired(lot: "Lottery", now: datetime.datetime) -> bool:
    """締切(終了日)が解析でき、かつ現在より過去なら True。
    終了日が無い/解析不能なもの（招待制などの恒常受付）は False（残す）。"""
    dt = parse_jp_deadline(lot.end, now)
    return dt is not None and dt < now


def fetch_all(now: datetime.datetime | None = None) -> list[Lottery]:
    """全ソースを集約し、締切が過ぎた抽選を除外する。"""
    now = now or datetime.datetime.now(JST)
    lotteries: list[Lottery] = []
    try:
        lotteries.extend(fetch_nyuka())
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 入荷Now の取得に失敗: {e}")
    kept = [lot for lot in lotteries if not is_expired(lot, now)]
    dropped = len(lotteries) - len(kept)
    if dropped:
        print(f"[info] 締切超過のため {dropped}件を除外")
    return kept


if __name__ == "__main__":
    for lot in fetch_all():
        print(f"[{lot.section}] {lot.store} / {lot.product}  ~{lot.end}  ({lot.uid})")
