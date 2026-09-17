"""
大商いスクリーナー データ生成スクリプト

流れ:
  1. Nasdaq screener から全米上場「普通株」の一覧（時価総額・セクター・業種）を取得
  2. Polygon grouped daily で日足（終値・出来高）を取得し、cache/daily.parquet に差分追記
  3. 売買代金5日平均・規模区分・騰落・RelVol・RS Rating・業種RS を計算
  4. docs/data/YYYY-MM-DD.json と docs/data/index.json を書き出す

環境変数:
  POLYGON_API_KEY   必須
  BACKFILL_DAYS     初回のみ。過去N営業日をまとめて取得（RS計算には約270日必要）
                    無料枠は5リクエスト/分なので270日で約55分かかる
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache" / "daily"   # 日付ごとに1ファイル（追記のみ・上書きしない）
OUT_DIR = ROOT / "docs" / "data"
MAP_JA = json.loads((ROOT / "scripts" / "industry_ja.json").read_text(encoding="utf-8"))
THEME = json.loads((ROOT / "scripts" / "theme_overrides.json").read_text(encoding="utf-8"))

POLYGON_KEY = os.environ.get("POLYGON_API_KEY", "")
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "0") or 0)
TOP_N = 150
LARGE_CUT, MID_CUT = 150, 750  # 時価総額順位の区切り
RS_LOOKBACK = 252              # 12ヶ月
CACHE_KEEP = 300               # キャッシュに残す営業日数
POLYGON_SLEEP = 12.5           # 無料枠 5req/分 → 12秒間隔
MIN_DV = 1_000_000             # 売買代金がこれ未満の銘柄はキャッシュしない（容量削減）


# ---------------------------------------------------------------- Nasdaq ----
def fetch_universe() -> pd.DataFrame:
    """全米上場株（普通株のみ）: symbol, name, mcap, sector, industry"""
    url = "https://api.nasdaq.com/api/screener/stocks"
    params = {"tableonly": "false", "limit": "25000", "download": "true"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
    }
    r = requests.get(url, params=params, headers=headers, timeout=60)
    r.raise_for_status()
    rows = r.json()["data"]["rows"]
    df = pd.DataFrame(rows)
    df = df.rename(columns={"symbol": "symbol", "name": "name", "marketCap": "mcap",
                            "sector": "sector", "industry": "industry"})
    df["mcap"] = pd.to_numeric(df["mcap"].astype(str).str.replace(",", ""), errors="coerce")
    df = df.dropna(subset=["mcap"])
    df = df[df["mcap"] > 0]

    # 普通株以外（ワラント・ユニット・優先株・権利・受益証券など）を除外
    bad = r"(?i)\b(warrant|warrants|unit|units|preferred|pref\b|depositary|right|rights|notes|debenture|trust units|\bETF\b|fund\b)"
    df = df[~df["name"].astype(str).str.contains(bad, regex=True)]
    df = df[~df["symbol"].str.contains(r"[\^\+\=]", regex=True)]
    df = df[~df["symbol"].str.contains(r"/W$|/U$|/R$|/P|\.W$|\.U$|\.R$", regex=True)]
    df["symbol"] = df["symbol"].str.strip().str.replace("/", ".", regex=False)  # BRK/B -> BRK.B
    df = df.drop_duplicates("symbol")
    for c in ("sector", "industry"):
        df[c] = df[c].fillna("").replace("", "Unclassified")
    return df[["symbol", "name", "mcap", "sector", "industry"]].reset_index(drop=True)


# ---------------------------------------------------------------- Polygon ---
def polygon_grouped(day: date) -> pd.DataFrame | None:
    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}"
    for attempt in range(4):
        r = requests.get(url, params={"adjusted": "true", "apiKey": POLYGON_KEY}, timeout=60)
        if r.status_code == 429:
            time.sleep(60)
            continue
        r.raise_for_status()
        js = r.json()
        if js.get("resultsCount", 0) == 0:
            return None  # 休場日
        df = pd.DataFrame(js["results"])[["T", "c", "v", "o"]]
        df.columns = ["symbol", "close", "volume", "open"]
        df["date"] = pd.Timestamp(day)
        return df
    raise RuntimeError("Polygon rate limit: giving up")


def load_cache() -> pd.DataFrame:
    files = sorted(CACHE_DIR.glob("????-??-??.parquet"))[-CACHE_KEEP:]
    if not files:
        return pd.DataFrame(columns=["symbol", "close", "volume", "open", "date"])
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def save_day(df: pd.DataFrame, d: date) -> None:
    """1営業日分を1ファイルに保存。既存ファイルは書き換えないので Git 履歴が膨らまない。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df = df[df["close"] * df["volume"] >= MIN_DV]          # 低流動性を捨てて容量削減
    df.to_parquet(CACHE_DIR / f"{d.isoformat()}.parquet", index=False, compression="zstd")


def prune_cache() -> None:
    """保持期間を超えた古いファイルを削除（Git からも消える）。"""
    for f in sorted(CACHE_DIR.glob("????-??-??.parquet"))[:-CACHE_KEEP]:
        f.unlink()


def update_cache(cache: pd.DataFrame) -> pd.DataFrame:
    """今日から遡って未取得の日を取りに行く。初回は BACKFILL_DAYS 分。"""
    have = set(pd.to_datetime(cache["date"]).dt.date) if len(cache) else set()
    today_utc = datetime.now(timezone.utc).date()
    # 米国市場の当日分は 米東部 20:00 頃に確定。JST 6:30 = ET 17:30 なので前日(ET)を最新とする
    latest = today_utc - timedelta(days=1)
    n_days = BACKFILL_DAYS if not have else 7  # 通常運転は直近7日を見て抜けを埋める
    want = []
    d = latest
    while len(want) < n_days and d > latest - timedelta(days=int(n_days * 1.6) + 10):
        if d.weekday() < 5 and d not in have:
            want.append(d)
        d -= timedelta(days=1)
    want.sort()
    frames = [cache] if len(cache) else []
    for i, d in enumerate(want):
        print(f"[polygon] {d} ({i+1}/{len(want)})", flush=True)
        df = polygon_grouped(d)
        if df is not None:
            save_day(df, d)
            frames.append(df[df["close"] * df["volume"] >= MIN_DV])
        if i < len(want) - 1:
            time.sleep(POLYGON_SLEEP)
    prune_cache()
    if not frames:
        return cache
    cache = pd.concat(frames, ignore_index=True)
    cache["date"] = pd.to_datetime(cache["date"])
    cache = cache.drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"])
    return cache


# ---------------------------------------------------------------- Compute ---
def percentile_rank(s: pd.Series) -> pd.Series:
    return (s.rank(pct=True) * 98 + 1).round().clip(1, 99)


def compute(universe: pd.DataFrame, cache: pd.DataFrame) -> dict:
    dates = sorted(cache["date"].unique())
    if len(dates) < 6:
        sys.exit("日足キャッシュが不足しています。BACKFILL_DAYS を指定して初回取得してください。")
    asof = pd.Timestamp(dates[-1])
    prev = pd.Timestamp(dates[-2])

    close = cache.pivot(index="date", columns="symbol", values="close").sort_index()
    vol = cache.pivot(index="date", columns="symbol", values="volume").sort_index()
    dollar = (close * vol)

    last = pd.DataFrame({
        "close": close.loc[asof],
        "prev_close": close.loc[prev],
        "volume": vol.loc[asof],
        "dv5": dollar.tail(5).mean(),
        "vol63": vol.tail(63).mean(),
    })
    last.index.name = "symbol"
    last = last.reset_index()

    # RS Rating（IBD近似: 3ヶ月に2倍の重み）
    def ret(n):
        if len(close) > n:
            return close.iloc[-1] / close.iloc[-1 - n] - 1
        return pd.Series(np.nan, index=close.columns)
    rs_raw = 0.4 * ret(63) + 0.2 * ret(126) + 0.2 * ret(189) + 0.2 * ret(252)
    rs_ok = len(close) > 252

    df = universe.merge(last, on="symbol", how="inner")
    df = df.dropna(subset=["close", "prev_close", "dv5"])
    df["rs_raw"] = df["symbol"].map(rs_raw)
    df["rs"] = percentile_rank(df["rs_raw"]) if rs_ok else np.nan

    # 規模 = 時価総額順位
    df["mcap_rank"] = df["mcap"].rank(ascending=False, method="first").astype(int)
    df["size"] = np.where(df["mcap_rank"] <= LARGE_CUT, "large",
                 np.where(df["mcap_rank"] <= MID_CUT, "mid", "small"))

    # 業種RS = 業種内の中央値RSを業種間でパーセンタイル化
    if rs_ok:
        ind_med = df.groupby("industry")["rs_raw"].median()
        ind_rs = percentile_rank(ind_med)
        df["ind_rs"] = df["industry"].map(ind_rs)
    else:
        df["ind_rs"] = np.nan

    df["chg"] = (df["close"] / df["prev_close"] - 1) * 100
    df["relvol"] = df["volume"] / df["vol63"]

    # 日本語分類（原分類 + テーマ反映）
    df["sector_ja"] = df["sector"].map(lambda s: MAP_JA["sector"].get(s, s))
    df["industry_ja"] = df["industry"].map(lambda s: MAP_JA["industry"].get(s, s))
    df["theme_ja"] = df.apply(
        lambda r: THEME.get(r["symbol"], {}).get("industry", r["industry_ja"]), axis=1)
    df["theme_sector_ja"] = df.apply(
        lambda r: THEME.get(r["symbol"], {}).get("sector", r["sector_ja"]), axis=1)

    top = df.sort_values("dv5", ascending=False).head(TOP_N).reset_index(drop=True)

    def f(x, nd=2):
        return None if pd.isna(x) else round(float(x), nd)

    rows = []
    for i, r in top.iterrows():
        rows.append({
            "rank": i + 1, "ticker": r["symbol"], "name": r["name"], "size": r["size"],
            "dv5": f(r["dv5"], 0), "price": f(r["close"]), "volume": int(r["volume"]),
            "chg": f(r["chg"]), "mcap": f(r["mcap"], 0), "relvol": f(r["relvol"], 1),
            "rs": None if pd.isna(r["rs"]) else int(r["rs"]),
            "ind_rs": None if pd.isna(r["ind_rs"]) else int(r["ind_rs"]),
            "sector": r["sector_ja"], "industry": r["industry_ja"],
            "theme_sector": r["theme_sector_ja"], "theme_industry": r["theme_ja"],
            "sector_en": r["sector"], "industry_en": r["industry"],
        })

    return {
        "date": asof.strftime("%Y-%m-%d"),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe_count": int(len(df)),
        "rs_available": bool(rs_ok),
        "cuts": {"large": LARGE_CUT, "mid": MID_CUT},
        "rows": rows,
    }


# ---------------------------------------------------------------- Main ------
def main():
    if not POLYGON_KEY:
        sys.exit("POLYGON_API_KEY が未設定です")
    print("[nasdaq] fetching universe", flush=True)
    universe = fetch_universe()
    print(f"[nasdaq] {len(universe)} common stocks", flush=True)
    cache = update_cache(load_cache())
    print(f"[cache] {cache['date'].nunique()} trading days cached", flush=True)
    result = compute(universe, cache)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{result['date']}.json").write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    dates = sorted((p.stem for p in OUT_DIR.glob("????-??-??.json")), reverse=True)
    (OUT_DIR / "index.json").write_text(json.dumps({"dates": dates}), encoding="utf-8")
    print(f"[done] {result['date']}  top={len(result['rows'])}  rs={result['rs_available']}")


if __name__ == "__main__":
    main()
