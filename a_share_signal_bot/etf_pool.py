# -*- coding: utf-8 -*-
from __future__ import annotations

from .base import *
from .etf_rotation import classify_asset_class


ASSET_CLASS_LABELS = {
    "broad": "宽基",
    "sector": "行业",
    "cross_border": "跨境",
    "defensive": "防守",
    "commodity": "商品",
}


THEME_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("沪深300", ["沪深300", "300ETF", "300指数"]),
    ("中证A500", ["A500", "中证A500"]),
    ("中证500", ["中证500", "500ETF"]),
    ("中证1000", ["中证1000", "1000ETF"]),
    ("创业板", ["创业板", "创业"]),
    ("科创50", ["科创50", "科创板50"]),
    ("上证50", ["上证50", "上证50ETF"]),
    ("红利", ["红利", "股息"]),
    ("证券", ["证券", "券商"]),
    ("银行", ["银行"]),
    ("保险", ["保险"]),
    ("白酒", ["白酒", "酒"]),
    ("消费", ["消费", "食品饮料"]),
    ("医药", ["医药", "医疗", "创新药", "生物"]),
    ("半导体", ["半导体", "芯片", "集成电路"]),
    ("新能源", ["新能源", "电池", "锂电"]),
    ("光伏", ["光伏"]),
    ("军工", ["军工", "国防"]),
    ("传媒", ["传媒", "游戏", "动漫"]),
    ("人工智能", ["人工智能", "AI", "智能"]),
    ("计算机", ["计算机", "软件", "云计算", "数据"]),
    ("有色", ["有色", "稀土", "金属"]),
    ("煤炭", ["煤炭"]),
    ("钢铁", ["钢铁"]),
    ("电力", ["电力", "能源"]),
    ("房地产", ["地产", "房地产"]),
    ("农业", ["农业", "畜牧", "养殖"]),
    ("黄金", ["黄金"]),
    ("原油", ["原油", "石油", "油气"]),
    ("纳指", ["纳指", "纳斯达克"]),
    ("标普500", ["标普", "S&P", "sp500", "标普500"]),
    ("恒生", ["恒生", "港股", "香港"]),
    ("中概", ["中概", "中国互联", "互联网"]),
    ("日经", ["日经", "日本"]),
    ("国债", ["国债", "政金债", "利率债"]),
    ("信用债", ["信用债", "公司债", "企业债"]),
    ("可转债", ["转债", "可转债"]),
]


def _pick_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    lower = {str(c).strip().lower(): c for c in df.columns}
    for item in candidates:
        if item.lower() in lower:
            return lower[item.lower()]
    return None


def normalize_etf_spot(df: pd.DataFrame, source: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_col = _pick_column(df, ["代码", "基金代码", "symbol", "code"])
    name_col = _pick_column(df, ["名称", "基金简称", "基金名称", "name"])
    price_col = _pick_column(df, ["最新价", "现价", "市价", "price"])
    amount_col = _pick_column(df, ["成交额", "amount", "成交金额"])
    volume_col = _pick_column(df, ["成交量", "volume"])
    pct_col = _pick_column(df, ["涨跌幅", "涨幅", "增长率", "pct_chg"])
    if code_col is None:
        raise ValueError(f"{source} ETF列表缺少代码列")
    rows = []
    for _, row in df.iterrows():
        try:
            code = normalize_code(row.get(code_col, ""))
        except Exception:
            continue
        name = "" if name_col is None or pd.isna(row.get(name_col)) else str(row.get(name_col)).strip()
        amount = safe_number(row.get(amount_col)) if amount_col else np.nan
        volume = safe_number(row.get(volume_col)) if volume_col else np.nan
        price = safe_number(row.get(price_col)) if price_col else np.nan
        pct_chg = safe_number(row.get(pct_col)) if pct_col else np.nan
        rows.append(
            {
                "code": code,
                "name": name,
                "price": price,
                "pct_chg": pct_chg,
                "amount": amount,
                "volume": volume,
                "source": source,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["amount"] = pd.to_numeric(out["amount"], errors="coerce")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    # 新浪/同花顺有些接口成交额单位不稳定；若缺成交额但有成交量和价格，用成交量*价格估算。
    amount_missing = out["amount"].isna() | (out["amount"] <= 0)
    est_amount = out["volume"] * out["price"]
    out.loc[amount_missing & est_amount.notna(), "amount"] = est_amount
    return out.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)


def safe_number(value: Any) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return np.nan
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "nan", "None"}:
        return np.nan
    mult = 1.0
    if text.endswith("亿"):
        mult = 100_000_000.0
        text = text[:-1]
    elif text.endswith("万"):
        mult = 10_000.0
        text = text[:-1]
    elif text.endswith("%"):
        text = text[:-1]
    try:
        return float(text) * mult
    except Exception:
        return np.nan


def theme_from_name(name: Any, asset_class: str = "") -> str:
    text = str(name or "").strip()
    lower = text.lower()
    for theme, keys in THEME_KEYWORDS:
        for key in keys:
            if key.lower() in lower or key in text:
                return theme
    cleaned = re.sub(r"(ETF|LOF|基金|指数|联接|增强|发起式|场内|交易型|开放式|证券投资|指数型|[\s\-_/]+)", "", text, flags=re.IGNORECASE)
    cleaned = cleaned.replace("易方达", "").replace("华夏", "").replace("南方", "").replace("嘉实", "").replace("博时", "")
    cleaned = cleaned.replace("广发", "").replace("富国", "").replace("招商", "").replace("国泰", "").replace("鹏华", "")
    cleaned = cleaned.replace("天弘", "").replace("汇添富", "").replace("工银瑞信", "").replace("华宝", "")
    if cleaned:
        return cleaned[:12]
    return ASSET_CLASS_LABELS.get(asset_class, asset_class or "其他")


def enrich_etf_pool_candidates(raw: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    pool_cfg = cfg.get("etf", {}).get("pool_builder", {})
    exclude_keywords = list(pool_cfg.get("exclude_keywords", []))
    min_amount = float(pool_cfg.get("min_amount", 30_000_000))
    min_amount_by_asset_class = dict(pool_cfg.get("min_amount_by_asset_class", {}))
    allow_missing_amount_asset_classes = set(pool_cfg.get("allow_missing_amount_asset_classes", []))
    min_price = float(pool_cfg.get("min_price", 0.2))
    out = raw.copy()
    out["code"] = out["code"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
    out["name"] = out["name"].astype(str).str.strip()
    out["amount"] = pd.to_numeric(out.get("amount"), errors="coerce")
    out["price"] = pd.to_numeric(out.get("price"), errors="coerce")
    out["asset_class"] = out.apply(lambda r: classify_asset_class(r.get("name", ""), ""), axis=1)
    out["theme"] = out.apply(lambda r: theme_from_name(r.get("name", ""), r.get("asset_class", "")), axis=1)
    out["category"] = out.apply(lambda r: f"{ASSET_CLASS_LABELS.get(r.get('asset_class'), r.get('asset_class'))}/{r.get('theme')}", axis=1)
    out["tracking_key"] = out["asset_class"].astype(str) + "/" + out["theme"].astype(str)
    out["eligible"] = True
    out["exclude_reason"] = ""
    out["min_amount_required"] = out["asset_class"].map(lambda x: float(min_amount_by_asset_class.get(str(x), min_amount)))
    missing_amount_allowed = out["amount"].isna() & out["asset_class"].astype(str).isin(allow_missing_amount_asset_classes)
    low_amount = (out["amount"].isna() | (out["amount"] < out["min_amount_required"])) & (~missing_amount_allowed)
    out.loc[low_amount, "eligible"] = False
    out.loc[low_amount, "exclude_reason"] = out.loc[low_amount, "exclude_reason"].astype(str) + "成交额不足；"
    low_price = out["price"].notna() & (out["price"] < min_price)
    out.loc[low_price, "eligible"] = False
    out.loc[low_price, "exclude_reason"] = out.loc[low_price, "exclude_reason"].astype(str) + f"价格<{min_price:.2f}；"
    if exclude_keywords:
        pattern = "|".join(re.escape(str(x)) for x in exclude_keywords if str(x).strip())
        if pattern:
            excluded = out["name"].apply(lambda name: any((str(kw).strip() in str(name)) and not (str(kw).strip() == "现金" and "现金流" in str(name)) for kw in exclude_keywords if str(kw).strip()))
            out.loc[excluded, "eligible"] = False
            out.loc[excluded, "exclude_reason"] = out.loc[excluded, "exclude_reason"].astype(str) + "名称排除；"
    out["liquidity_rank"] = out["amount"].rank(ascending=False, method="min")
    out["liquidity_score"] = np.log1p(out["amount"].fillna(0.0))
    return out.sort_values(["eligible", "liquidity_score"], ascending=[False, False]).reset_index(drop=True)


def select_etf_pool(candidates: pd.DataFrame, cfg: Dict[str, Any], max_size: Optional[int] = None) -> pd.DataFrame:
    if candidates is None or candidates.empty:
        return pd.DataFrame(columns=["code", "name", "category"])
    pool_cfg = cfg.get("etf", {}).get("pool_builder", {})
    quotas = dict(pool_cfg.get("asset_class_quotas", {}))
    max_per_theme = int(pool_cfg.get("max_per_theme", 2))
    max_size = int(max_size or pool_cfg.get("max_size", 40))
    eligible = candidates[candidates["eligible"].fillna(False)].copy()
    if eligible.empty:
        return pd.DataFrame(columns=["code", "name", "category"])
    eligible = eligible.sort_values(["liquidity_score", "amount"], ascending=[False, False])
    selected_rows = []
    counts: Dict[str, int] = {}
    theme_counts: Dict[str, int] = {}
    for _, row in eligible.iterrows():
        asset_class = str(row.get("asset_class", "sector"))
        quota = int(quotas.get(asset_class, max_size))
        if counts.get(asset_class, 0) >= quota:
            continue
        theme_key = str(row.get("tracking_key", ""))
        if theme_counts.get(theme_key, 0) >= max_per_theme:
            continue
        selected_rows.append(row)
        counts[asset_class] = counts.get(asset_class, 0) + 1
        theme_counts[theme_key] = theme_counts.get(theme_key, 0) + 1
        if len(selected_rows) >= max_size:
            break
    if not selected_rows:
        return pd.DataFrame(columns=["code", "name", "category"])
    selected = pd.DataFrame(selected_rows).copy()
    selected["amount"] = pd.to_numeric(selected["amount"], errors="coerce")
    selected = selected.sort_values(["asset_class", "liquidity_score"], ascending=[True, False])
    cols = ["code", "name", "category", "asset_class", "theme", "amount", "price", "pct_chg", "source", "tracking_key"]
    return selected[[c for c in cols if c in selected.columns]].reset_index(drop=True)


class EtfPoolFetcher:
    def __init__(self, cfg: Dict[str, Any], cache_dir: str | Path = "cache/etf_pool", refresh: bool = False):
        self.cfg = cfg
        self.refresh = refresh
        self.cache_dir = ensure_dir(cache_dir)
        self.cache_hours = float(cfg.get("etf", {}).get("pool_builder", {}).get("cache_hours", 24))

    def _cache_path(self, source: str) -> Path:
        return self.cache_dir / f"etf_spot_{source}_{today_yyyymmdd()}.csv"

    def _read_cache(self, source: str) -> Optional[pd.DataFrame]:
        path = self._cache_path(source)
        if path.exists() and (not self.refresh) and is_cache_fresh(path, self.cache_hours):
            try:
                return pd.read_csv(path, dtype=str)
            except Exception:
                return None
        latest = self.cache_dir / f"latest_etf_spot_{source}.csv"
        if latest.exists() and not self.refresh:
            try:
                return pd.read_csv(latest, dtype=str)
            except Exception:
                return None
        return None

    def _write_cache(self, source: str, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        df.to_csv(self._cache_path(source), index=False, encoding="utf-8-sig")
        df.to_csv(self.cache_dir / f"latest_etf_spot_{source}.csv", index=False, encoding="utf-8-sig")

    def fetch_source(self, source: str) -> pd.DataFrame:
        source = source.lower().strip()
        cached = self._read_cache(source)
        if cached is not None and not cached.empty:
            return normalize_etf_spot(cached, source)
        import akshare as ak

        if source in {"eastmoney", "em"}:
            raw = ak.fund_etf_spot_em()
            df = normalize_etf_spot(raw, "eastmoney")
        elif source == "ths":
            raw = ak.fund_etf_spot_ths()
            df = normalize_etf_spot(raw, "ths")
        else:
            raise ValueError(f"未知ETF列表数据源: {source}")
        self._write_cache(source, df)
        return df

    def fetch_all(self, sources: List[str]) -> Tuple[pd.DataFrame, List[Dict[str, str]]]:
        rows = []
        errors: List[Dict[str, str]] = []
        for source in sources:
            try:
                df = self.fetch_source(source)
                if not df.empty:
                    rows.append(df)
            except Exception as exc:
                errors.append({"source": source, "error": str(exc)})
        if not rows:
            return pd.DataFrame(), errors
        raw = pd.concat(rows, ignore_index=True)
        raw["amount"] = pd.to_numeric(raw.get("amount"), errors="coerce")
        raw = raw.sort_values("amount", ascending=False, na_position="last").drop_duplicates(subset=["code"], keep="first")
        return raw.reset_index(drop=True), errors


def format_pool_report(pool: pd.DataFrame, candidates: pd.DataFrame, errors: List[Dict[str, str]], cfg: Dict[str, Any]) -> str:
    lines = [f"# ETF池构建报告 {now_cn().strftime('%Y-%m-%d %H:%M')}", ""]
    lines.append(f"- 候选ETF：{0 if candidates is None else len(candidates)}")
    lines.append(f"- 入池ETF：{0 if pool is None else len(pool)}")
    if candidates is not None and not candidates.empty and "eligible" in candidates.columns:
        lines.append(f"- 可入池候选：{int(candidates['eligible'].fillna(False).sum())}")
    if errors:
        lines.append(f"- 数据源错误：{len(errors)}")
    if pool is not None and not pool.empty:
        lines.append("")
        lines.append("## 入池ETF")
        for _, r in pool.iterrows():
            amount = safe_number(r.get("amount"))
            amount_text = "" if not np.isfinite(amount) else f"，成交额{amount:,.0f}"
            lines.append(f"- {r.get('code')} {r.get('name')}：{r.get('category')}{amount_text}")
    return "\n".join(lines)


def build_etf_pool(args: argparse.Namespace) -> Tuple[pd.DataFrame, pd.DataFrame, Path]:
    cfg = load_config(args.config)
    pool_cfg = cfg.setdefault("etf", {}).setdefault("pool_builder", {})
    if args.min_amount is not None:
        pool_cfg["min_amount"] = float(args.min_amount)
    if args.max_size is not None:
        pool_cfg["max_size"] = int(args.max_size)
    sources = [x.strip() for x in str(args.sources or ",".join(pool_cfg.get("sources", ["eastmoney", "ths"]))).split(",") if x.strip()]
    out_dir = ensure_dir(args.out or cfg.get("etf", {}).get("out_dir", "etf_output"))
    cache_dir = args.cache_dir or pool_cfg.get("cache_dir", "cache/etf_pool")
    fetcher = EtfPoolFetcher(cfg, cache_dir, refresh=bool(args.refresh))
    raw, errors = fetcher.fetch_all(sources)
    candidates = enrich_etf_pool_candidates(raw, cfg)
    pool = select_etf_pool(candidates, cfg, max_size=args.max_size)

    run_date = now_cn().strftime("%Y%m%d_%H%M%S")
    candidates.to_csv(out_dir / f"etf_pool_candidates_{run_date}.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(out_dir / "latest_etf_pool_candidates.csv", index=False, encoding="utf-8-sig")
    pool.to_csv(out_dir / f"etf_pool_selected_{run_date}.csv", index=False, encoding="utf-8-sig")
    pool.to_csv(out_dir / "latest_etf_pool_selected.csv", index=False, encoding="utf-8-sig")
    write_or_clear_error_csv(out_dir / "latest_etf_pool_errors.csv", errors)
    report = format_pool_report(pool, candidates, errors, cfg)
    report_path = out_dir / "latest_etf_pool_report.md"
    report_path.write_text(report, encoding="utf-8")
    (out_dir / "latest_etf_pool_message.txt").write_text("\n".join(report.splitlines()[:40]), encoding="utf-8")

    if raw is None or raw.empty:
        detail = "；".join(f"{e.get('source')}: {e.get('error')}" for e in errors) or "无候选数据"
        raise RuntimeError(f"ETF池构建失败：未获取到ETF列表，不覆盖本地ETF池。{detail}")
    if pool.empty:
        raise RuntimeError("ETF池构建失败：没有符合条件的ETF，不覆盖本地ETF池；请查看 latest_etf_pool_candidates.csv")

    pool_path = Path(args.pool_out or cfg.get("etf", {}).get("pool", "etf_pool.csv"))
    pool_path.parent.mkdir(parents=True, exist_ok=True)
    pool[["code", "name", "category"]].to_csv(pool_path, index=False, encoding="utf-8-sig")
    print(f"[ETF池] 已生成 {pool_path}，入池 {len(pool)} 只")
    print(f"[ETF池] {report_path}")
    return pool, candidates, report_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ETF池自动构建器")
    p.add_argument("--config", default="", help="配置文件 YAML/JSON，可选")
    p.add_argument("--out", default="", help="输出目录，默认读取 etf.out_dir")
    p.add_argument("--pool-out", default="", help="生成的ETF池路径，默认读取 etf.pool")
    p.add_argument("--cache-dir", default="", help="ETF列表缓存目录，默认读取 etf.pool_builder.cache_dir")
    p.add_argument("--sources", default="", help="ETF列表来源，逗号分隔，默认读取配置")
    p.add_argument("--max-size", type=int, default=None, help="ETF池最大数量")
    p.add_argument("--min-amount", type=float, default=None, help="最低成交额过滤")
    p.add_argument("--refresh", action="store_true", help="忽略缓存，强制刷新ETF列表")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    _, _, report_path = build_etf_pool(args)
    print(Path(report_path).read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
