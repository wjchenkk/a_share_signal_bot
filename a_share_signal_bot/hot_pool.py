#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hot-list driven trading-pool builder.

Daily workflow:
  1. After close, cache the day's hot lists:
     .venv/bin/python hot_pool.py --mode cache
  2. Next trading day, build a bounded trading pool without mutating stock_pool.csv:
     .venv/bin/python hot_pool.py --mode build

The generated trading pool keeps the core pool first, then fills remaining
slots from cached hot-list rankings. By default only A-share main-board codes
are eligible.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

try:
    import akshare as ak
except Exception:  # pragma: no cover - handled at runtime
    ak = None

from .scanner import normalize_code, read_stock_pool


CN_TZ = timezone(timedelta(hours=8))
MAINBOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")


SOURCE_WEIGHTS = {
    "eastmoney_hot_rank": 130,
    "eastmoney_hot_up": 120,
    "xueqiu_tweet_hot": 100,
    "xueqiu_follow_hot": 95,
    "xueqiu_deal_hot": 90,
    "ths_lxsz": 75,
    "ths_cxg_year": 70,
    "ths_xstp_20ma": 65,
}


@dataclass
class HotFetchResult:
    raw: pd.DataFrame
    ranked: pd.DataFrame
    errors: pd.DataFrame


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def ymd(d: date | datetime | str | None = None) -> str:
    if d is None:
        d = now_cn().date()
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, date):
        return d.strftime("%Y%m%d")
    s = str(d).strip().replace("-", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"日期格式应为 YYYYMMDD: {d}")
    return s


def parse_ymd(s: str) -> date:
    return datetime.strptime(ymd(s), "%Y%m%d").date()


def is_weekday_ymd(s: str) -> bool:
    return parse_ymd(s).weekday() < 5


def next_weekday(s: str) -> str:
    d = parse_ymd(s) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y%m%d")


def normalize_hot_code(value: Any) -> str:
    raw = str(value).strip().upper()
    raw = raw.replace("SH", "").replace("SZ", "").replace("BJ", "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    try:
        return normalize_code(digits)
    except Exception:
        return ""


def is_mainboard_code(code: Any) -> bool:
    code = normalize_hot_code(code)
    return bool(code and code.startswith(MAINBOARD_PREFIXES))


def code_to_em_symbol(code: str) -> str:
    code = normalize_hot_code(code)
    if code.startswith(("600", "601", "603", "605")):
        return f"SH{code}"
    return f"SZ{code}"


def eastmoney_secid(sc: str) -> str:
    sc = str(sc).upper().strip()
    code = normalize_hot_code(sc)
    market = "1" if sc.startswith("SH") or code.startswith(("600", "601", "603", "605")) else "0"
    return f"{market}.{code}"


def request_json_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff: float = 1.0,
    **kwargs: Any,
) -> Dict[str, Any]:
    last_exc: Optional[Exception] = None
    for i in range(retries):
        try:
            resp = session.request(method, url, timeout=kwargs.pop("timeout", 12), **kwargs)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            if i < retries - 1:
                time.sleep(backoff * (i + 1))
    raise RuntimeError(f"{url} 请求失败: {last_exc}")


def quote_eastmoney(session: requests.Session, rank_df: pd.DataFrame) -> pd.DataFrame:
    if rank_df.empty or "sc" not in rank_df.columns:
        return pd.DataFrame()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    }
    rows: List[pd.DataFrame] = []
    tmp = rank_df.copy()
    tmp["code"] = tmp["sc"].map(normalize_hot_code)
    tmp["secid"] = tmp["sc"].map(eastmoney_secid)
    for start in range(0, len(tmp), 30):
        chunk = tmp.iloc[start : start + 30].copy()
        params = {
            "ut": "f057cbcbce2a86e2866ab8877db1d059",
            "fltt": "2",
            "invt": "2",
            "fields": "f14,f3,f12,f2",
            "secids": ",".join(chunk["secid"].astype(str)),
        }
        try:
            data_json = request_json_with_retry(
                session,
                "GET",
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                headers=headers,
                params=params,
                retries=2,
                backoff=0.8,
            )
            diff = pd.DataFrame((data_json.get("data") or {}).get("diff") or [])
            if diff.empty:
                continue
            if "f12" in diff.columns:
                diff = diff.rename(columns={"f12": "code", "f14": "name", "f2": "price", "f3": "pct_chg"})
            else:
                diff.columns = ["name", "pct_chg", "code", "price"][: len(diff.columns)]
            rows.append(diff[[c for c in ["code", "name", "price", "pct_chg"] if c in diff.columns]].copy())
        except Exception:
            continue
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["code", "name", "price", "pct_chg"])


def fetch_eastmoney_hot(endpoint: str, source: str, top_n: int) -> pd.DataFrame:
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://guba.eastmoney.com",
        "Referer": "https://guba.eastmoney.com/rank/",
    }
    payload = {
        "appId": "appId01",
        "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "",
        "pageNo": 1,
        "pageSize": int(top_n),
    }
    data_json = request_json_with_retry(
        session,
        "POST",
        f"https://emappdata.eastmoney.com/stockrank/{endpoint}",
        headers=headers,
        json=payload,
        retries=3,
        backoff=1.0,
    )
    df = pd.DataFrame(data_json.get("data") or [])
    if df.empty:
        return pd.DataFrame()
    df = df.head(top_n).copy()
    df["code"] = df["sc"].map(normalize_hot_code)
    quotes = quote_eastmoney(session, df)
    if not quotes.empty:
        df = df.merge(quotes, on="code", how="left")
    else:
        df["name"] = ""
        df["price"] = pd.NA
        df["pct_chg"] = pd.NA
    df["source"] = source
    df["source_rank"] = pd.to_numeric(df.get("rk"), errors="coerce")
    df["rank_change"] = pd.to_numeric(df.get("hrc", df.get("hisRc", pd.NA)), errors="coerce")
    return df[["code", "name", "source", "source_rank", "rank_change", "price", "pct_chg"]].copy()


def require_akshare() -> Any:
    if ak is None:
        raise RuntimeError("akshare 未安装，无法拉取雪球/同花顺榜单")
    return ak


def fetch_ak_hot(
    source: str,
    fn: Callable[[], pd.DataFrame],
    *,
    top_n: int,
    code_col: str = "股票代码",
    name_col: str = "股票简称",
    extra_cols: Iterable[str] = (),
) -> pd.DataFrame:
    df = fn()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.head(top_n).copy()
    out = pd.DataFrame()
    out["code"] = df[code_col].map(normalize_hot_code)
    out["name"] = df[name_col].astype(str).str.strip()
    out["source"] = source
    out["source_rank"] = range(1, len(out) + 1)
    for c in extra_cols:
        if c in df.columns:
            out[c] = df[c].values
    return out


def fetch_hot_lists(top_n: int, include_sources: Iterable[str]) -> HotFetchResult:
    include = set(include_sources)
    frames: List[pd.DataFrame] = []
    errors: List[Dict[str, str]] = []

    def collect(source: str, call: Callable[[], pd.DataFrame]) -> None:
        if include and source not in include:
            return
        try:
            df = call()
            if df is not None and not df.empty:
                frames.append(df)
            print(f"[热榜] {source}: {0 if df is None else len(df)} 条")
        except Exception as exc:
            msg = f"{type(exc).__name__}: {str(exc)[:240]}"
            errors.append({"source": source, "error": msg})
            print(f"[热榜] {source} 失败：{msg}")

    collect("eastmoney_hot_rank", lambda: fetch_eastmoney_hot("getAllCurrentList", "eastmoney_hot_rank", top_n))
    collect("eastmoney_hot_up", lambda: fetch_eastmoney_hot("getAllHisRcList", "eastmoney_hot_up", top_n))

    aks = require_akshare()
    collect(
        "xueqiu_tweet_hot",
        lambda: fetch_ak_hot(
            "xueqiu_tweet_hot",
            lambda: aks.stock_hot_tweet_xq(symbol="最热门"),
            top_n=top_n,
            extra_cols=["关注", "最新价"],
        ),
    )
    collect(
        "xueqiu_follow_hot",
        lambda: fetch_ak_hot(
            "xueqiu_follow_hot",
            lambda: aks.stock_hot_follow_xq(symbol="最热门"),
            top_n=top_n,
            extra_cols=["关注", "最新价"],
        ),
    )
    collect(
        "xueqiu_deal_hot",
        lambda: fetch_ak_hot(
            "xueqiu_deal_hot",
            lambda: aks.stock_hot_deal_xq(symbol="最热门"),
            top_n=top_n,
            extra_cols=["关注", "最新价"],
        ),
    )
    collect(
        "ths_lxsz",
        lambda: fetch_ak_hot(
            "ths_lxsz",
            aks.stock_rank_lxsz_ths,
            top_n=top_n,
            extra_cols=["收盘价", "连涨天数", "连续涨跌幅", "累计换手率", "所属行业"],
        ),
    )
    collect(
        "ths_cxg_year",
        lambda: fetch_ak_hot(
            "ths_cxg_year",
            lambda: aks.stock_rank_cxg_ths(symbol="一年新高"),
            top_n=top_n,
            extra_cols=["最新价", "涨跌幅", "换手率"],
        ),
    )
    collect(
        "ths_xstp_20ma",
        lambda: fetch_ak_hot(
            "ths_xstp_20ma",
            lambda: aks.stock_rank_xstp_ths(symbol="20日均线"),
            top_n=top_n,
            extra_cols=["最新价", "涨跌幅", "换手率"],
        ),
    )

    raw = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if raw.empty:
        return HotFetchResult(raw, pd.DataFrame(), pd.DataFrame(errors))
    raw["code"] = raw["code"].map(normalize_hot_code)
    raw = raw[raw["code"].ne("")].copy()
    raw["source_rank"] = pd.to_numeric(raw["source_rank"], errors="coerce").fillna(top_n + 1).astype(int)
    raw["source_weight"] = raw["source"].map(SOURCE_WEIGHTS).fillna(50)
    raw["rank_points"] = (top_n + 1 - raw["source_rank"].clip(upper=top_n)).clip(lower=0)
    raw["source_score"] = raw["source_weight"] + raw["rank_points"]

    ranked_rows: List[Dict[str, Any]] = []
    for code, g in raw.groupby("code", sort=False):
        g = g.copy()
        best = g.sort_values(["source_score", "source_weight"], ascending=False).iloc[0]
        name = ""
        if "name" in g.columns:
            for value in g["name"].fillna("").astype(str).str.strip():
                if value and value.lower() not in {"nan", "none", "null"}:
                    name = value
                    break
        ranked_rows.append(
            {
                "code": code,
                "name": name,
                "hot_score": round(float(g["source_score"].sum()), 2),
                "source_count": int(g["source"].nunique()),
                "sources": ",".join(g.sort_values(["source_score", "source_rank"], ascending=[False, True])["source"].astype(str).unique()),
                "best_rank": int(g["source_rank"].min()),
            }
        )
    ranked = pd.DataFrame(ranked_rows).sort_values(
        ["hot_score", "source_count", "best_rank"],
        ascending=[False, False, True],
    )
    ranked = ranked.reset_index(drop=True)
    ranked.insert(0, "hot_rank", range(1, len(ranked) + 1))
    return HotFetchResult(raw, ranked, pd.DataFrame(errors))


def update_hot_meta(meta_path: Path, ranked: pd.DataFrame, cache_date: str) -> pd.DataFrame:
    if meta_path.exists() and meta_path.stat().st_size > 0:
        meta = pd.read_csv(meta_path, dtype=str, encoding="utf-8-sig")
    else:
        meta = pd.DataFrame(columns=[
            "code", "name", "first_seen", "last_seen", "times_seen",
            "best_hot_rank", "last_hot_rank", "last_hot_score", "last_sources",
        ])
    meta["code"] = meta.get("code", pd.Series(dtype=str)).astype(str).str.zfill(6)
    by_code = {str(r.get("code", "")).zfill(6): dict(r) for _, r in meta.iterrows()}
    for _, r in ranked.iterrows():
        code = normalize_hot_code(r.get("code", ""))
        if not code:
            continue
        rec = by_code.get(code, {"code": code, "first_seen": cache_date, "times_seen": "0"})
        rec["name"] = str(r.get("name", rec.get("name", ""))).strip()
        rec["last_seen"] = cache_date
        rec["times_seen"] = str(int(float(rec.get("times_seen", 0) or 0)) + 1)
        hot_rank = int(r.get("hot_rank", 999999))
        prev_best = int(float(rec.get("best_hot_rank", hot_rank) or hot_rank))
        rec["best_hot_rank"] = str(min(prev_best, hot_rank))
        rec["last_hot_rank"] = str(hot_rank)
        rec["last_hot_score"] = str(r.get("hot_score", ""))
        rec["last_sources"] = str(r.get("sources", ""))
        by_code[code] = rec
    out = pd.DataFrame(by_code.values()).sort_values(["last_seen", "best_hot_rank"], ascending=[False, True])
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(meta_path, index=False, encoding="utf-8-sig")
    return out


def cache_hot_lists(args: argparse.Namespace) -> Optional[pd.DataFrame]:
    cache_date = ymd(args.cache_date)
    if not args.force and not is_weekday_ymd(cache_date):
        print(f"[热榜] {cache_date} 非工作日，跳过缓存；如需强制执行请加 --force")
        return None
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    include_sources = [x.strip() for x in str(args.sources or "").split(",") if x.strip()]
    result = fetch_hot_lists(int(args.top), include_sources)
    if result.ranked.empty:
        raise RuntimeError("热榜数据为空，未生成缓存")

    ranked = result.ranked.copy()
    raw = result.raw.copy()
    if args.mainboard_only:
        before = len(ranked)
        ranked = ranked[ranked["code"].map(is_mainboard_code)].copy().reset_index(drop=True)
        ranked["hot_rank"] = range(1, len(ranked) + 1)
        raw = raw[raw["code"].map(is_mainboard_code)].copy()
        print(f"[主板过滤] 热榜聚合 {before} → {len(ranked)}")

    raw_path = cache_dir / f"hot_raw_{cache_date}.csv"
    rank_path = cache_dir / f"hot_rank_{cache_date}.csv"
    error_path = cache_dir / f"hot_errors_{cache_date}.csv"
    raw.to_csv(raw_path, index=False, encoding="utf-8-sig")
    ranked.to_csv(rank_path, index=False, encoding="utf-8-sig")
    if not result.errors.empty:
        result.errors.to_csv(error_path, index=False, encoding="utf-8-sig")
    update_hot_meta(cache_dir / "pool_meta.csv", ranked, cache_date)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ranked.head(int(args.top)).to_csv(out_dir / "latest_hot_pool_top100.csv", index=False, encoding="utf-8-sig")
    print(f"[热榜缓存] {rank_path}，共 {len(ranked)} 只")
    return ranked


def list_hot_cache_dates(cache_dir: Path) -> List[str]:
    dates = []
    for p in cache_dir.glob("hot_rank_*.csv"):
        s = p.stem.replace("hot_rank_", "")
        if len(s) == 8 and s.isdigit():
            dates.append(s)
    return sorted(set(dates))


def select_hot_cache(cache_dir: Path, for_date: str, allow_same_day: bool = False, hot_date: str = "") -> Tuple[str, Path]:
    if hot_date:
        d = ymd(hot_date)
        p = cache_dir / f"hot_rank_{d}.csv"
        if not p.exists():
            raise FileNotFoundError(f"指定热榜缓存不存在: {p}")
        return d, p
    dates = list_hot_cache_dates(cache_dir)
    if not dates:
        raise FileNotFoundError(f"未找到热榜缓存: {cache_dir}/hot_rank_*.csv")
    cutoff = ymd(for_date)
    eligible = [d for d in dates if d <= cutoff] if allow_same_day else [d for d in dates if d < cutoff]
    if not eligible:
        raise FileNotFoundError(f"没有早于 {cutoff} 的热榜缓存；可先运行 --mode cache，或加 --allow-same-day-hot")
    d = eligible[-1]
    return d, cache_dir / f"hot_rank_{d}.csv"


def read_hot_rank(path: Path) -> pd.DataFrame:
    hot = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    if hot.empty:
        return hot
    hot["code"] = hot["code"].map(normalize_hot_code)
    for c in ["hot_rank", "hot_score", "source_count", "best_rank"]:
        if c in hot.columns:
            hot[c] = pd.to_numeric(hot[c], errors="coerce")
    return hot[hot["code"].ne("")].copy()


def build_trading_pool(args: argparse.Namespace) -> pd.DataFrame:
    for_date = ymd(args.for_date)
    cache_dir = Path(args.cache_dir)
    hot_date, hot_path = select_hot_cache(cache_dir, for_date, bool(args.allow_same_day_hot), args.hot_date)
    hot = read_hot_rank(hot_path)
    if args.mainboard_only:
        hot = hot[hot["code"].map(is_mainboard_code)].copy()

    core = read_stock_pool(args.pool)
    core["code"] = core["code"].map(normalize_hot_code)
    core = core[core["code"].ne("")].copy()
    excluded_core = pd.DataFrame()
    if args.mainboard_only:
        excluded_core = core[~core["code"].map(is_mainboard_code)].copy()
        core = core[core["code"].map(is_mainboard_code)].copy()

    max_size = int(args.max_size)
    if max_size <= 0:
        raise ValueError("--max-size 必须大于 0")

    core = core.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
    if len(core) >= max_size:
        trading = core.head(max_size).copy()
        hot_add = pd.DataFrame()
        over_core = len(core) - max_size
        if over_core > 0:
            print(f"[交易池] 核心池主板股票 {len(core)} 只，超过上限 {max_size}，仅保留前 {max_size} 只")
    else:
        slots = max_size - len(core)
        core_codes = set(core["code"].astype(str))
        hot_add = hot[~hot["code"].isin(core_codes)].copy()
        hot_add = hot_add.sort_values(["hot_rank", "hot_score"], ascending=[True, False]).head(slots).copy()
        trading = pd.concat(
            [
                core.assign(origin="core", hot_rank=pd.NA, hot_score=pd.NA, hot_sources=""),
                pd.DataFrame(
                    {
                        "code": hot_add["code"],
                        "name": hot_add["name"].fillna(""),
                        "sector": "",
                        "origin": "hot",
                        "hot_rank": hot_add.get("hot_rank", pd.Series(dtype=float)),
                        "hot_score": hot_add.get("hot_score", pd.Series(dtype=float)),
                        "hot_sources": hot_add.get("sources", pd.Series(dtype=str)),
                    }
                ),
            ],
            ignore_index=True,
        )

    if "origin" not in trading.columns:
        trading["origin"] = "core"
    for c in ["hot_rank", "hot_score", "hot_sources"]:
        if c not in trading.columns:
            trading[c] = ""
    trading = trading[["code", "name", "sector", "origin", "hot_rank", "hot_score", "hot_sources"]].copy()
    trading = trading.drop_duplicates(subset=["code"], keep="first").head(max_size).reset_index(drop=True)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"trading_pool_{for_date}.csv"
    latest_path = out_dir / "latest_trading_pool.csv"
    trading.to_csv(out_path, index=False, encoding="utf-8-sig")
    trading.to_csv(latest_path, index=False, encoding="utf-8-sig")

    if not excluded_core.empty:
        excluded_path = out_dir / f"trading_pool_excluded_core_{for_date}.csv"
        excluded_core.to_csv(excluded_path, index=False, encoding="utf-8-sig")
        print(f"[主板过滤] 核心池排除非主板 {len(excluded_core)} 只：{excluded_path}")

    summary = {
        "for_date": for_date,
        "hot_date": hot_date,
        "max_size": max_size,
        "core_kept": int((trading["origin"] == "core").sum()),
        "hot_added": int((trading["origin"] == "hot").sum()),
        "trading_pool_size": int(len(trading)),
        "mainboard_only": bool(args.mainboard_only),
        "hot_cache": str(hot_path),
        "output": str(out_path),
    }
    with (out_dir / f"trading_pool_summary_{for_date}.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with (out_dir / "latest_trading_pool_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(
        f"[交易池] {for_date}: 核心 {summary['core_kept']} 只，热榜补充 {summary['hot_added']} 只，"
        f"合计 {summary['trading_pool_size']} / {max_size}；使用热榜缓存 {hot_date}"
    )
    print(f"[交易池] {latest_path}")
    return trading


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="热榜缓存与次日交易池生成器")
    parser.add_argument("--mode", choices=["cache", "build", "all"], default="build", help="cache=收盘后缓存热榜；build=生成交易池；all=缓存今日并生成下一工作日交易池")
    parser.add_argument("--pool", default="stock_pool.csv", help="核心股票池路径，不会被本脚本写回")
    parser.add_argument("--out", default="output", help="输出目录")
    parser.add_argument("--cache-dir", default="cache/hot_pool", help="热榜缓存目录")
    parser.add_argument("--top", type=int, default=100, help="每个来源最多抓取前 N，默认 100")
    parser.add_argument("--max-size", type=int, default=150, help="交易池最大数量，默认 150")
    parser.add_argument("--cache-date", default="", help="缓存日期 YYYYMMDD，默认今天")
    parser.add_argument("--for-date", default="", help="交易池日期 YYYYMMDD；build 默认今天，all 默认下一工作日")
    parser.add_argument("--hot-date", default="", help="指定使用某天热榜缓存 YYYYMMDD")
    parser.add_argument("--allow-same-day-hot", action="store_true", help="build 时允许使用交易池日期当天缓存；默认只用更早缓存")
    parser.add_argument("--sources", default="", help="逗号分隔来源白名单；默认全部")
    parser.add_argument("--mainboard-only", dest="mainboard_only", action="store_true", default=True, help="只保留沪深主板，默认开启")
    parser.add_argument("--include-non-mainboard", dest="mainboard_only", action="store_false", help="允许创业板/科创板/北交所进入交易池")
    parser.add_argument("--force", action="store_true", help="非工作日也强制缓存")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not args.cache_date:
        args.cache_date = ymd()
    if args.mode == "cache":
        cache_hot_lists(args)
        return 0
    if args.mode == "build":
        if not args.for_date:
            args.for_date = ymd()
        build_trading_pool(args)
        return 0
    if args.mode == "all":
        if not args.for_date:
            args.for_date = next_weekday(ymd(args.cache_date))
        cache_hot_lists(args)
        build_trading_pool(args)
        return 0
    raise ValueError(f"未知 mode: {args.mode}")


if __name__ == "__main__":
    raise SystemExit(main())
