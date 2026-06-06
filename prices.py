"""BOX買取価格の比較。複数ソースを横断し、高い順にランキングする。

ソース（後から追加しやすいよう SOURCES に関数を足すだけ）:
  - おたちゅう秋葉原 : 実店舗の買取価格（JAN付き・約250BOX・日次更新）
  - ポケカチ        : 買取相場の代表値（約186BOX）

/price コマンドから search(query) を呼ぶ。10分間メモリキャッシュ。
"""
from __future__ import annotations

import difflib
import re
import time
import unicodedata

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 pokeca-bot"}

OTACHU_URL = "https://otachu-akiba.com/1gocard/buying_price/pokemon-card-game/"
POKEKACHI_URL = "https://altema.jp/pokemoncard/mikaihubox"

_HEADER_WORDS = {"画像/名前", "商品名", "買取金額", "買取価格", "名前"}


def _price_to_int(s: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", s or "")
    return int(digits) if digits else None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"\s+", "", s).lower()
    return s


def fetch_otachu() -> list[dict]:
    r = requests.get(OTACHU_URL, headers=UA, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    out: list[dict] = []
    for t in soup.find_all("table"):
        for row in t.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            name, price = cells[0], _price_to_int(cells[1])
            if not price or name in _HEADER_WORDS:
                continue
            jan = cells[3] if len(cells) > 3 else ""
            out.append({"shop": "おたちゅう秋葉原", "name": name, "price": price, "jan": jan, "url": OTACHU_URL})
    return out


def fetch_pokekachi() -> list[dict]:
    r = requests.get(POKEKACHI_URL, headers=UA, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    out: list[dict] = []
    for t in soup.find_all("table"):
        for row in t.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2 or "円" not in cells[1]:
                continue
            name, price = cells[0], _price_to_int(cells[1])
            if not price or name in _HEADER_WORDS:
                continue
            out.append({"shop": "買取相場(ポケカチ)", "name": name, "price": price, "jan": "", "url": POKEKACHI_URL})
    return out


SOURCES = [fetch_otachu, fetch_pokekachi]

_cache: dict = {"t": 0.0, "data": None}


def _all_entries() -> list[dict]:
    if _cache["data"] is not None and (time.time() - _cache["t"]) < 600:
        return _cache["data"]
    entries: list[dict] = []
    for fn in SOURCES:
        try:
            entries.extend(fn())
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {fn.__name__} 失敗: {e}")
    _cache["t"], _cache["data"] = time.time(), entries
    return entries


def _score(query: str, name: str) -> float:
    nq, nn = _norm(query), _norm(name)
    if not nq or not nn:
        return 0.0
    if nq in nn or nn in nq:
        return 0.9 + 0.1 * (min(len(nq), len(nn)) / max(len(nq), len(nn)))
    return difflib.SequenceMatcher(None, nq, nn).ratio()


def search(query: str, threshold: float = 0.55) -> list[dict]:
    """各ショップでquery最一致のBOXを1件ずつ選び、買取価格の高い順に返す。"""
    best_by_shop: dict[str, tuple[float, dict]] = {}
    for e in _all_entries():
        s = _score(query, e["name"])
        if s < threshold:
            continue
        # シュリンクなしは本命でないので少し下げる
        adj = s - (0.15 if "シュリンクなし" in e["name"] else 0)
        cur = best_by_shop.get(e["shop"])
        if cur is None or adj > cur[0]:
            best_by_shop[e["shop"]] = (adj, e)
    results = [e for _, e in best_by_shop.values()]
    results.sort(key=lambda x: x["price"], reverse=True)
    return results


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "アビスアイ"
    for r in search(q):
        print(f"  {r['shop']}: ¥{r['price']:,}  ({r['name']})")
