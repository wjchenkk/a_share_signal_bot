# -*- coding: utf-8 -*-
from __future__ import annotations

from .base import *
from .market_data import safe_float


FUND_CLASS_LABELS = {
    "broad_index": "宽基/指数",
    "active_equity": "主动权益",
    "balanced": "均衡混合",
    "bond": "债券稳健",
    "qdii": "海外/QDII",
}


RETURN_COLUMNS = {
    "ret_1w": ["近1周", "近1星期", "1周"],
    "ret_1m": ["近1月", "近1个月", "1月"],
    "ret_3m": ["近3月", "近3个月", "3月"],
    "ret_6m": ["近6月", "近6个月", "6月"],
    "ret_1y": ["近1年", "近一年", "1年"],
    "ret_2y": ["近2年", "近二年", "2年"],
    "ret_3y": ["近3年", "近三年", "3年"],
    "ret_ytd": ["今年来", "今年以来"],
    "ret_since": ["成立来", "成立以来"],
}


def _pick_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    lower = {str(c).strip().lower(): c for c in df.columns}
    for item in candidates:
        if item.lower() in lower:
            return lower[item.lower()]
    return None


def _safe_number(value: Any) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return np.nan
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "nan", "None"}:
        return np.nan
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except Exception:
        return np.nan


def _rank_pct(values: pd.Series, higher_is_better: bool = True) -> pd.Series:
    s = pd.to_numeric(values, errors="coerce")
    if s.notna().sum() <= 1:
        return pd.Series(0.5, index=values.index)
    return s.rank(pct=True, ascending=higher_is_better).fillna(0.5)


def classify_fund_class(name: Any, source_type: Any = "") -> str:
    text = f"{name or ''} {source_type or ''}"
    lower = text.lower()
    if "qdii" in lower or any(k in text for k in ["纳斯达克", "标普", "海外", "全球", "美国", "港股", "恒生", "日经"]):
        return "qdii"
    if any(k in text for k in ["债", "固收", "纯债", "可转债"]):
        return "bond"
    if any(k in text for k in ["指数", "ETF联接", "联接", "增强"]):
        return "broad_index"
    if any(k in text for k in ["平衡", "稳健", "偏债", "灵活配置", "均衡"]):
        return "balanced"
    if any(k in text for k in ["股票", "混合", "偏股"]):
        return "active_equity"
    return "balanced"


def normalize_fund_rank(df: pd.DataFrame, source_type: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_col = _pick_column(df, ["基金代码", "代码", "fund_code", "code"])
    name_col = _pick_column(df, ["基金简称", "基金名称", "名称", "name"])
    date_col = _pick_column(df, ["日期", "净值日期", "date"])
    nav_col = _pick_column(df, ["单位净值", "最新净值", "净值", "nav"])
    fee_col = _pick_column(df, ["手续费", "费率", "申购费率"])
    if code_col is None:
        raise ValueError(f"{source_type}基金排行缺少基金代码列")
    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        raw_code = str(row.get(code_col, "")).strip()
        m = re.search(r"(\d{6})", raw_code)
        if not m:
            continue
        code = m.group(1)
        name = "" if name_col is None or pd.isna(row.get(name_col)) else str(row.get(name_col)).strip()
        rec: Dict[str, Any] = {
            "code": code,
            "name": name,
            "source_type": source_type,
            "fund_class": classify_fund_class(name, source_type),
            "date": "" if date_col is None or pd.isna(row.get(date_col)) else str(row.get(date_col)).strip(),
            "nav": _safe_number(row.get(nav_col)) if nav_col else np.nan,
            "fee": "" if fee_col is None or pd.isna(row.get(fee_col)) else str(row.get(fee_col)).strip(),
        }
        for out_col, candidates in RETURN_COLUMNS.items():
            col = _pick_column(df, candidates)
            rec[out_col] = _safe_number(row.get(col)) if col else np.nan
        rows.append(rec)
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)


def score_fund_candidates(raw: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    dca_cfg = cfg.get("fund_dca", {})
    exclude_keywords = [str(x) for x in dca_cfg.get("exclude_keywords", []) if str(x).strip()]
    exclude_share_classes = [str(x).upper() for x in dca_cfg.get("exclude_share_classes", []) if str(x).strip()]
    allowed_classes = set(dca_cfg.get("allowed_classes", FUND_CLASS_LABELS.keys()))
    min_score = float(dca_cfg.get("min_score", 58.0))
    min_ret_1y = float(dca_cfg.get("min_ret_1y", -25.0))
    min_ret_3y = float(dca_cfg.get("min_ret_3y", -45.0))
    out = raw.copy()
    out["code"] = out["code"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
    out["name"] = out["name"].astype(str).str.strip()
    if "fund_class" not in out.columns:
        out["fund_class"] = out.apply(lambda r: classify_fund_class(r.get("name", ""), r.get("source_type", "")), axis=1)
    for col in RETURN_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out["eligible"] = True
    out["exclude_reason"] = ""
    disallowed = ~out["fund_class"].astype(str).isin(allowed_classes)
    out.loc[disallowed, "eligible"] = False
    out.loc[disallowed, "exclude_reason"] += "类别不在定投范围；"
    if exclude_keywords:
        excluded = out["name"].apply(lambda n: any(k in str(n) for k in exclude_keywords))
        out.loc[excluded, "eligible"] = False
        out.loc[excluded, "exclude_reason"] += "名称排除；"
    if exclude_share_classes:
        pattern = r"(?:" + "|".join(re.escape(x) for x in exclude_share_classes) + r")$"
        share_class_excluded = out["name"].str.upper().str.replace(" ", "", regex=False).str.contains(pattern, regex=True, na=False)
        out.loc[share_class_excluded, "eligible"] = False
        out.loc[share_class_excluded, "exclude_reason"] += "份额类别排除；"
    weak_1y = out["ret_1y"].notna() & (out["ret_1y"] < min_ret_1y)
    weak_3y = out["ret_3y"].notna() & (out["ret_3y"] < min_ret_3y)
    no_return = out[["ret_6m", "ret_1y", "ret_3y"]].isna().all(axis=1)
    out.loc[weak_1y | weak_3y | no_return, "eligible"] = False
    out.loc[weak_1y, "exclude_reason"] += f"近1年低于{min_ret_1y:.0f}%；"
    out.loc[weak_3y, "exclude_reason"] += f"近3年低于{min_ret_3y:.0f}%；"
    out.loc[no_return, "exclude_reason"] += "收益数据不足；"

    consistency = (
        (out["ret_3m"] > 0).astype(int)
        + (out["ret_6m"] > 0).astype(int)
        + (out["ret_1y"] > 0).astype(int)
        + (out["ret_3y"] > 0).astype(int)
    )
    out["score"] = (
        24.0 * _rank_pct(out["ret_3y"])
        + 24.0 * _rank_pct(out["ret_1y"])
        + 18.0 * _rank_pct(out["ret_6m"])
        + 12.0 * _rank_pct(out["ret_3m"])
        + 12.0 * (consistency / 4.0)
        + 10.0 * _rank_pct(out["ret_1m"])
    )
    overheated = (out["ret_1m"] > 18) | (out["ret_3m"] > 35)
    out.loc[overheated, "score"] -= 6.0
    out.loc[out["ret_6m"] < -18, "score"] -= 8.0
    out.loc[out["fund_class"].eq("broad_index"), "score"] += 3.0
    out.loc[out["fund_class"].eq("bond"), "score"] += 2.0
    out["score"] = out["score"].clip(lower=0, upper=100)
    low_score = out["score"] < min_score
    out.loc[low_score, "eligible"] = False
    out.loc[low_score, "exclude_reason"] += f"评分低于{min_score:.0f}；"
    out["fund_class_label"] = out["fund_class"].map(lambda x: FUND_CLASS_LABELS.get(str(x), str(x)))
    out["score_detail"] = out.apply(
        lambda r: (
            f"近3月{safe_float(r.get('ret_3m'), 0):.1f}%，"
            f"近6月{safe_float(r.get('ret_6m'), 0):.1f}%，"
            f"近1年{safe_float(r.get('ret_1y'), 0):.1f}%，"
            f"近3年{safe_float(r.get('ret_3y'), 0):.1f}%"
        ),
        axis=1,
    )
    return out.sort_values(["eligible", "fund_class", "score"], ascending=[False, True, False]).reset_index(drop=True)


def select_dca_funds(candidates: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    if candidates is None or candidates.empty:
        return pd.DataFrame()
    dca_cfg = cfg.get("fund_dca", {})
    max_funds = int(dca_cfg.get("max_funds", 6))
    class_quotas = dict(dca_cfg.get("class_quotas", {}))
    class_weights = dict(dca_cfg.get("class_weights", {}))
    eligible = candidates[candidates["eligible"].fillna(False)].copy()
    if eligible.empty:
        return pd.DataFrame()
    selected = []
    selected_codes = set()
    counts: Dict[str, int] = {}
    class_order = sorted(unique_nonempty(eligible["fund_class"].astype(str).tolist()), key=lambda x: float(class_weights.get(x, 0.0)), reverse=True)
    for cls in class_order:
        quota = int(class_quotas.get(cls, max_funds))
        if quota <= 0 or len(selected) >= max_funds:
            continue
        part = eligible[eligible["fund_class"].astype(str).eq(cls)].sort_values("score", ascending=False)
        if part.empty:
            continue
        row = part.iloc[0]
        code = str(row.get("code", ""))
        selected.append(row)
        selected_codes.add(code)
        counts[cls] = counts.get(cls, 0) + 1
        if len(selected) >= max_funds:
            break
    for _, row in eligible.sort_values(["score"], ascending=False).iterrows():
        code = str(row.get("code", ""))
        if code in selected_codes:
            continue
        cls = str(row.get("fund_class", ""))
        quota = int(class_quotas.get(cls, max_funds))
        if counts.get(cls, 0) >= quota:
            continue
        selected.append(row)
        selected_codes.add(code)
        counts[cls] = counts.get(cls, 0) + 1
        if len(selected) >= max_funds:
            break
    return pd.DataFrame(selected).reset_index(drop=True) if selected else pd.DataFrame()


def _round_amount(value: float, step: float) -> float:
    if not np.isfinite(value) or value <= 0:
        return 0.0
    return math.floor(value / step) * step


def build_dca_plan(selected: pd.DataFrame, cfg: Dict[str, Any], monthly_budget: float) -> pd.DataFrame:
    if selected is None or selected.empty or monthly_budget <= 0:
        return pd.DataFrame()
    dca_cfg = cfg.get("fund_dca", {})
    class_weights = dict(dca_cfg.get("class_weights", {}))
    period_by_class = dict(dca_cfg.get("period_by_class", {}))
    weekday_by_class = dict(dca_cfg.get("weekday_by_class", {}))
    installments = dict(dca_cfg.get("installments_per_month", {}))
    min_installment = float(dca_cfg.get("min_installment", 100.0))
    amount_step = float(dca_cfg.get("amount_step", 10.0))
    selected = selected.copy()
    active_classes = unique_nonempty(selected["fund_class"].astype(str).tolist())
    weight_sum = sum(float(class_weights.get(cls, 0.0)) for cls in active_classes)
    if weight_sum <= 0:
        weight_sum = float(len(active_classes))
        class_weights = {cls: 1.0 for cls in active_classes}
    rows = []
    for cls in active_classes:
        part = selected[selected["fund_class"].astype(str).eq(cls)].sort_values("score", ascending=False).copy()
        if part.empty:
            continue
        class_budget = monthly_budget * float(class_weights.get(cls, 0.0)) / weight_sum
        score_sum = pd.to_numeric(part["score"], errors="coerce").clip(lower=1.0).sum()
        if score_sum <= 0:
            score_sum = float(len(part))
            part["_weight"] = 1.0 / score_sum
        else:
            part["_weight"] = pd.to_numeric(part["score"], errors="coerce").clip(lower=1.0) / score_sum
        for _, row in part.iterrows():
            monthly_amount = _round_amount(class_budget * safe_float(row.get("_weight"), 0.0), amount_step)
            period = str(period_by_class.get(cls, "monthly"))
            times = max(1, int(installments.get(period, 1)))
            per_amount = _round_amount(monthly_amount / times, amount_step)
            if per_amount < min_installment:
                period = "monthly"
                times = 1
                per_amount = max(min_installment, _round_amount(monthly_amount, amount_step))
                monthly_amount = per_amount
            rows.append({
                **row.to_dict(),
                "monthly_amount": monthly_amount,
                "period": period,
                "period_cn": {"weekly": "每周", "biweekly": "每两周", "monthly": "每月"}.get(period, period),
                "installments_per_month": times,
                "per_installment_amount": per_amount,
                "weekday": weekday_by_class.get(cls, "周二"),
                "plan_reason": f"{FUND_CLASS_LABELS.get(cls, cls)}配置，{row.get('score_detail', '')}",
            })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = out[pd.to_numeric(out["monthly_amount"], errors="coerce") > 0].copy()
    return out.sort_values(["fund_class", "monthly_amount"], ascending=[True, False]).reset_index(drop=True)


class FundDcaFetcher:
    def __init__(self, cfg: Dict[str, Any], cache_dir: str | Path = "cache/fund_dca", refresh: bool = False):
        self.cfg = cfg
        self.refresh = refresh
        self.cache_dir = ensure_dir(cache_dir)
        self.cache_hours = float(cfg.get("fund_dca", {}).get("cache_hours", 24))

    def _cache_path(self, source_type: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", source_type)
        return self.cache_dir / f"fund_rank_{safe}_{today_yyyymmdd()}.csv"

    def _read_cache(self, source_type: str, ignore_freshness: bool = False) -> Optional[pd.DataFrame]:
        path = self._cache_path(source_type)
        if path.exists() and (ignore_freshness or ((not self.refresh) and is_cache_fresh(path, self.cache_hours))):
            try:
                return pd.read_csv(path, dtype=str)
            except Exception:
                return None
        return None

    def _fallback_stale_cache(self, source_type: str) -> Optional[pd.DataFrame]:
        if not bool(self.cfg.get("fund_dca", {}).get("allow_stale_cache_on_error", True)):
            return None
        safe = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", source_type)
        files = sorted(self.cache_dir.glob(f"fund_rank_{safe}_*.csv"), key=lambda x: x.stat().st_mtime, reverse=True)
        for path in files:
            try:
                cached = pd.read_csv(path, dtype=str)
            except Exception:
                cached = None
            if cached is not None and not cached.empty:
                out = normalize_fund_rank(cached, source_type)
                out.attrs["data_provider"] = "stale_cache"
                out.attrs["data_warning"] = f"基金排行数据源失败，使用本地旧缓存：{path.name}"
                return out
        return None

    def _write_cache(self, source_type: str, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        df.to_csv(self._cache_path(source_type), index=False, encoding="utf-8-sig")

    def fetch_source(self, source_type: str) -> pd.DataFrame:
        cached = self._read_cache(source_type)
        if cached is not None and not cached.empty:
            out = normalize_fund_rank(cached, source_type)
            out.attrs["data_provider"] = "cache"
            return out
        import akshare as ak
        try:
            raw = ak.fund_open_fund_rank_em(symbol=source_type)
            self._write_cache(source_type, raw)
            out = normalize_fund_rank(raw, source_type)
            out.attrs["data_provider"] = "eastmoney"
            return out
        except Exception:
            stale = self._fallback_stale_cache(source_type)
            if stale is not None:
                return stale
            raise

    def fetch_all(self, source_types: List[str]) -> Tuple[pd.DataFrame, List[Dict[str, str]]]:
        frames = []
        errors: List[Dict[str, str]] = []
        warnings = []
        for source_type in source_types:
            try:
                df = self.fetch_source(str(source_type).strip())
                if not df.empty:
                    frames.append(df)
                    warning = str(df.attrs.get("data_warning", "")).strip()
                    if warning:
                        warnings.append(warning)
            except Exception as exc:
                errors.append({"source": str(source_type), "error": str(exc)})
        if not frames:
            return pd.DataFrame(), errors
        out = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
        if warnings:
            out["data_warning"] = "；".join(unique_nonempty(warnings))
            out.attrs["data_warning"] = out["data_warning"].iloc[0]
        return out, errors


def to_plan_chinese(df: pd.DataFrame) -> pd.DataFrame:
    mapping = {
        "code": "基金代码",
        "name": "基金名称",
        "fund_class_label": "类别",
        "source_type": "来源分类",
        "score": "定投评分",
        "monthly_amount": "每月金额",
        "period_cn": "定投周期",
        "weekday": "执行日",
        "per_installment_amount": "每期金额",
        "ret_3m": "近3月%",
        "ret_6m": "近6月%",
        "ret_1y": "近1年%",
        "ret_3y": "近3年%",
        "score_detail": "评分依据",
        "plan_reason": "计划理由",
    }
    if df is None or df.empty:
        return pd.DataFrame(columns=list(mapping.values()))
    cols = [c for c in mapping if c in df.columns] + [c for c in df.columns if c not in mapping]
    out = df[cols].rename(columns=mapping).copy()
    for col in ["定投评分", "近3月%", "近6月%", "近1年%", "近3年%"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").map(lambda x: f"{x:.1f}" if pd.notna(x) else "")
    for col in ["每月金额", "每期金额"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").map(lambda x: f"{x:,.0f}" if pd.notna(x) else "")
    return out


def format_fund_dca_report(plan: pd.DataFrame, candidates: pd.DataFrame, errors: List[Dict[str, str]], cfg: Dict[str, Any], monthly_budget: float) -> str:
    lines = [f"# 基金定投计划 {now_cn().strftime('%Y-%m-%d %H:%M')}", ""]
    lines.append(f"- 月度预算：{monthly_budget:,.0f} 元")
    lines.append(f"- 候选基金：{0 if candidates is None else len(candidates)}")
    if candidates is not None and not candidates.empty and "eligible" in candidates.columns:
        lines.append(f"- 符合定投过滤：{int(candidates['eligible'].fillna(False).sum())}")
    lines.append(f"- 入选基金：{0 if plan is None else len(plan)}")
    quality_note = format_data_quality_summary(candidates)
    if quality_note:
        lines.append(f"- {quality_note}")
    if errors:
        lines.append(f"- 数据源错误：{len(errors)}")
    if plan is not None and not plan.empty:
        lines.append("")
        lines.append("## 定投明细")
        for _, r in plan.iterrows():
            lines.append(
                f"- {r.get('code')} {r.get('name')}：{r.get('fund_class_label')}，"
                f"{r.get('period_cn')}{r.get('weekday')}，每期{safe_float(r.get('per_installment_amount'), 0):,.0f}元，"
                f"约每月{safe_float(r.get('monthly_amount'), 0):,.0f}元，评分{safe_float(r.get('score'), 0):.1f}"
            )
        lines.append("")
        lines.append("## 执行纪律")
        lines.append("- 只按计划定投，不因单日涨跌临时追买；基金更换以月度复盘为主。")
        lines.append("- 单只基金连续跌破过滤阈值或数据缺失时，先暂停新增，保留人工确认。")
    else:
        lines.append("")
        lines.append("没有生成定投基金，请检查基金排行数据源或放宽配置过滤。")
    lines.append("")
    lines.append("提示：本计划只做基金筛选和金额分配，不自动申购；执行前需确认费率、限购、风险等级和个人现金流。")
    return "\n".join(lines)


def run_fund_dca(
    cfg: Dict[str, Any],
    out_dir: str | Path,
    monthly_budget: float,
    refresh: bool = False,
    sources: Optional[List[str]] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, Path]:
    dca_cfg = cfg.setdefault("fund_dca", {})
    out_path = ensure_dir(out_dir or dca_cfg.get("out_dir", "fund_output"))
    source_types = sources or list(dca_cfg.get("sources", ["股票型", "混合型", "指数型", "债券型", "QDII"]))
    fetcher = FundDcaFetcher(cfg, dca_cfg.get("cache_dir", "cache/fund_dca"), refresh=refresh)
    raw, errors = fetcher.fetch_all(source_types)
    candidates = score_fund_candidates(raw, cfg)
    selected = select_dca_funds(candidates, cfg)
    plan = build_dca_plan(selected, cfg, monthly_budget)

    run_date = now_cn().strftime("%Y%m%d_%H%M%S")
    candidates.to_csv(out_path / f"fund_dca_candidates_{run_date}.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(out_path / "latest_fund_dca_candidates.csv", index=False, encoding="utf-8-sig")
    to_plan_chinese(plan).to_csv(out_path / f"fund_dca_plan_{run_date}.csv", index=False, encoding="utf-8-sig")
    to_plan_chinese(plan).to_csv(out_path / "latest_fund_dca_plan.csv", index=False, encoding="utf-8-sig")
    plan.to_csv(out_path / "latest_fund_dca_plan_raw.csv", index=False, encoding="utf-8-sig")
    write_or_clear_error_csv(out_path / "latest_fund_dca_errors.csv", errors)
    report = format_fund_dca_report(plan, candidates, errors, cfg, monthly_budget)
    report_path = out_path / "latest_fund_dca_report.md"
    report_path.write_text(report, encoding="utf-8")
    (out_path / f"fund_dca_report_{run_date}.md").write_text(report, encoding="utf-8")
    (out_path / "latest_fund_dca_message.txt").write_text("\n".join(report.splitlines()[:45]), encoding="utf-8")
    if raw is None or raw.empty:
        detail = "；".join(f"{e.get('source')}: {e.get('error')}" for e in errors) or "无候选数据"
        raise RuntimeError(f"基金定投计划失败：未获取到基金排行数据。{detail}")
    return plan, candidates, report_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="基金定投计划生成器")
    p.add_argument("--config", default="", help="配置文件 YAML/JSON，可选")
    p.add_argument("--out", default="", help="输出目录，默认读取 fund_dca.out_dir")
    p.add_argument("--budget", type=float, default=0.0, help="月度定投预算；默认读取 fund_dca.monthly_budget")
    p.add_argument("--sources", default="", help="基金排行分类，逗号分隔，默认读取配置")
    p.add_argument("--refresh", action="store_true", help="忽略缓存，强制刷新基金排行")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    dca_cfg = cfg.setdefault("fund_dca", {})
    monthly_budget = float(args.budget or dca_cfg.get("monthly_budget", 5000.0))
    sources = [x.strip() for x in str(args.sources).split(",") if x.strip()] if args.sources else None
    out_dir = args.out or dca_cfg.get("out_dir", "fund_output")
    _, _, report_path = run_fund_dca(cfg, out_dir, monthly_budget, refresh=bool(args.refresh), sources=sources)
    print(Path(report_path).read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
