# -*- coding: utf-8 -*-
from __future__ import annotations

from .base import *
from .market_data import add_indicators, normalize_stock_hist, safe_float


def read_etf_pool(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"ETF池文件不存在: {path}")

    if p.suffix.lower() in {".xlsx", ".xls"}:
        df = pd.read_excel(p, dtype=str)
    else:
        try:
            df = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(p, dtype=str, encoding="gbk")
        except pd.errors.ParserError:
            df = pd.read_csv(p, dtype=str, header=None, encoding="utf-8-sig")

    if df.empty:
        raise ValueError("ETF池为空")

    cols_lower = {str(c).strip().lower(): c for c in df.columns}
    code_col = None
    for candidate in ["code", "symbol", "ticker", "etf_code", "基金代码", "etf代码", "证券代码", "代码"]:
        if candidate.lower() in cols_lower:
            code_col = cols_lower[candidate.lower()]
            break
    if code_col is None:
        code_col = df.columns[0]

    name_col = None
    for candidate in ["name", "fund_name", "etf_name", "基金名称", "etf名称", "证券简称", "名称", "简称"]:
        if candidate.lower() in cols_lower:
            name_col = cols_lower[candidate.lower()]
            break

    category_col = None
    for candidate in ["category", "theme", "index", "track_index", "type", "类别", "类型", "主题", "跟踪指数", "指数"]:
        if candidate.lower() in cols_lower:
            category_col = cols_lower[candidate.lower()]
            break

    rows: List[Dict[str, str]] = []
    for _, row in df.iterrows():
        raw = row.get(code_col)
        if pd.isna(raw):
            continue
        try:
            code = normalize_code(raw)
        except Exception:
            continue
        name = "" if name_col is None or pd.isna(row.get(name_col)) else str(row.get(name_col)).strip()
        category = "" if category_col is None or pd.isna(row.get(category_col)) else str(row.get(category_col)).strip()
        rows.append({"code": code, "name": name, "category": category or "未分组"})

    out = pd.DataFrame(rows).drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
    if out.empty:
        raise ValueError("ETF池没有可识别的6位代码")
    return out


class EtfFetcher:
    def __init__(self, cfg: Dict[str, Any], refresh: bool = False):
        self.cfg = cfg
        self.etf_cfg = cfg.get("etf", {})
        self.refresh = refresh
        self.cache_dir = ensure_dir(self.etf_cfg.get("cache_dir", "cache/etf"))
        self.cache_hours = float(self.etf_cfg.get("cache_hours", cfg.get("data", {}).get("cache_hours", 24)))
        self.sleep_seconds = float(self.etf_cfg.get("sleep_seconds", cfg.get("data", {}).get("sleep_seconds", 0.2)))
        self.request_retries = int(self.etf_cfg.get("request_retries", cfg.get("data", {}).get("request_retries", 2)))
        self.retry_backoff_seconds = list(self.etf_cfg.get("retry_backoff_seconds", cfg.get("data", {}).get("retry_backoff_seconds", [1.0, 3.0])))
        self.allow_stale_cache_on_error = bool(self.etf_cfg.get("allow_stale_cache_on_error", cfg.get("data", {}).get("allow_stale_cache_on_error", True)))
        self.use_stale_cache_on_network_error = bool(self.etf_cfg.get("use_stale_cache_on_network_error", cfg.get("data", {}).get("use_stale_cache_on_network_error", True)))
        self.fail_fast_network_errors = bool(self.etf_cfg.get("fail_fast_network_errors", cfg.get("data", {}).get("fail_fast_network_errors", True)))
        providers = self.etf_cfg.get("hist_providers", ["eastmoney", "sina"])
        if isinstance(providers, str):
            providers = [x.strip() for x in providers.split(",") if x.strip()]
        self.hist_providers = [str(x).strip().lower() for x in providers if str(x).strip()] or ["eastmoney", "sina"]
        self._network_error_seen = False
        self._failed_providers: set[str] = set()

    def _read_cache(self, path: Path, ignore_freshness: bool = False) -> Optional[pd.DataFrame]:
        if path.exists() and (ignore_freshness or ((not self.refresh) and is_cache_fresh(path, self.cache_hours))):
            try:
                return pd.read_csv(path, dtype=str)
            except Exception:
                return None
        return None

    def _write_cache(self, df: pd.DataFrame, path: Path) -> None:
        try:
            df.to_csv(path, index=False, encoding="utf-8-sig")
        except Exception:
            pass

    def _is_network_error(self, exc: Exception) -> bool:
        text = repr(exc)
        markers = [
            "NameResolutionError",
            "Failed to resolve",
            "Name or service not known",
            "Temporary failure in name resolution",
            "getaddrinfo failed",
            "Network is unreachable",
            "ConnectionError",
            "ConnectTimeout",
            "ReadTimeout",
            "Read timed out",
            "timed out",
            "Max retries exceeded",
            "ProxyError",
        ]
        return any(m in text for m in markers)

    def _mark_network_error(self, exc: Exception) -> bool:
        is_network_error = self._is_network_error(exc)
        if is_network_error:
            self._network_error_seen = True
        return is_network_error

    def _with_retry(self, label: str, func) -> pd.DataFrame:
        last_exc: Optional[Exception] = None
        attempts = max(1, self.request_retries)
        for attempt in range(attempts):
            try:
                df = func()
                if df is None or df.empty:
                    raise ValueError(f"{label} 返回空数据")
                return df
            except Exception as exc:
                last_exc = exc
                if self.fail_fast_network_errors and self._mark_network_error(exc):
                    raise exc
                if attempt < attempts - 1:
                    wait = self.retry_backoff_seconds[min(attempt, len(self.retry_backoff_seconds) - 1)] if self.retry_backoff_seconds else 1.0
                    time.sleep(float(wait))
        raise RuntimeError(f"{label} 获取失败: {last_exc}")

    def _fallback_stale_cache(self, code: str, adjust_key: str) -> Optional[pd.DataFrame]:
        if not self.allow_stale_cache_on_error:
            return None
        patterns = [
            f"etf_*_{code}_{adjust_key}_*.csv",
            f"etf_*_{code}_none_*.csv",
            f"etf_{code}_{adjust_key}_*.csv",
        ]
        files: List[Path] = []
        for pattern in patterns:
            files.extend(self.cache_dir.glob(pattern))
        files = sorted(set(files), key=lambda x: x.stat().st_mtime, reverse=True)
        for f in files:
            cached = self._read_cache(f, ignore_freshness=True)
            if cached is None or cached.empty:
                continue
            df = normalize_stock_hist(cached)
            if df.empty:
                continue
            df.attrs["data_provider"] = "stale_cache"
            return df
        return None

    def _sina_symbol(self, code: str) -> str:
        code = normalize_code(code)
        if code.startswith(("5", "6", "9")):
            return "sh" + code
        return "sz" + code

    def _fetch_eastmoney(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        import akshare as ak

        return self._with_retry(
            f"ETF {code} eastmoney",
            lambda: ak.fund_etf_hist_em(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            ),
        )

    def _fetch_sina(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        if adjust:
            raise ValueError("sina ETF日线不支持复权")
        import akshare as ak

        raw = self._with_retry(
            f"ETF {code} sina",
            lambda: ak.fund_etf_hist_sina(symbol=self._sina_symbol(code)),
        )
        df = normalize_stock_hist(raw)
        start_ts = pd.to_datetime(start_date, errors="coerce")
        end_ts = pd.to_datetime(end_date, errors="coerce")
        if pd.notna(start_ts):
            df = df[df["date"] >= start_ts]
        if pd.notna(end_ts):
            df = df[df["date"] <= end_ts]
        return df

    def etf_hist(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        code = normalize_code(code)
        adjust_key = adjust or "none"
        stale = self._fallback_stale_cache(code, adjust_key) if self.use_stale_cache_on_network_error else None
        errors: List[str] = []

        for provider in self.hist_providers:
            provider = provider.lower().strip()
            if not provider:
                continue
            if self.fail_fast_network_errors and provider in self._failed_providers:
                errors.append(f"{provider}: 已跳过此前失败的数据源")
                continue
            cache_path = self.cache_dir / f"etf_{provider}_{code}_{adjust_key}_{start_date}_{end_date}.csv"
            cached = self._read_cache(cache_path)
            if cached is not None:
                df = normalize_stock_hist(cached)
                df.attrs["data_provider"] = f"{provider}_cache"
                return df
            try:
                if provider in {"eastmoney", "em", "akshare_em"}:
                    raw = self._fetch_eastmoney(code, start_date, end_date, adjust)
                    df = normalize_stock_hist(raw)
                elif provider == "sina":
                    df = self._fetch_sina(code, start_date, end_date, adjust)
                else:
                    raise ValueError(f"未知ETF历史数据源: {provider}")
                time.sleep(self.sleep_seconds)
                if df.empty:
                    raise ValueError(f"{provider} 返回空数据")
                df.attrs["data_provider"] = provider
                self._write_cache(df, cache_path)
                return df
            except Exception as exc:
                errors.append(f"{provider}: {exc}")
                if self._mark_network_error(exc):
                    self._failed_providers.add(provider)
                continue

        if stale is not None:
            return stale
        raise RuntimeError("所有ETF历史K线数据源均失败：" + " | ".join(errors))


def _score_etf_setup(last: pd.Series, etf_cfg: Dict[str, Any]) -> Tuple[float, str, str, List[str], List[str]]:
    setup_cfg = etf_cfg.get("setup", {})
    close = safe_float(last.get("close"))
    pct_chg = safe_float(last.get("pct_chg"), 0.0)
    ma20 = safe_float(last.get("ma20"))
    ma60 = safe_float(last.get("ma60"))
    high60_prev = safe_float(last.get("high60_prev"))
    high120_prev = safe_float(last.get("high120_prev"))
    amount_ratio20 = safe_float(last.get("amount_ratio20"))
    amount_dryup20 = safe_float(last.get("amount_dryup20"))
    close_pos60 = safe_float(last.get("close_pos60"))
    close_pos120 = safe_float(last.get("close_pos120"))
    blockers: List[str] = []

    max_chase = float(setup_cfg.get("max_chase_day_pct", 5.5))
    min_day_pct = float(setup_cfg.get("min_day_pct", -4.5))
    breakout_min = float(setup_cfg.get("breakout_amount_min", 1.05))
    breakout_max = float(setup_cfg.get("breakout_amount_max", 2.60))
    pullback_dist = float(setup_cfg.get("pullback_ma20_distance_pct", 0.035))
    pullback_dryup_max = float(setup_cfg.get("pullback_amount_dryup_max", 1.05))

    if np.isfinite(pct_chg) and pct_chg > max_chase:
        blockers.append(f"单日涨幅{pct_chg:.1f}%高于追价上限{max_chase:.1f}%")
    if np.isfinite(pct_chg) and pct_chg < min_day_pct:
        blockers.append(f"单日跌幅{pct_chg:.1f}%低于承接下限{min_day_pct:.1f}%")

    breakout60 = (
        np.isfinite(close)
        and np.isfinite(high60_prev)
        and close > high60_prev
        and np.isfinite(amount_ratio20)
        and breakout_min <= amount_ratio20 <= breakout_max
        and pct_chg <= max_chase
    )
    breakout120 = (
        np.isfinite(close)
        and np.isfinite(high120_prev)
        and close > high120_prev
        and np.isfinite(amount_ratio20)
        and breakout_min <= amount_ratio20 <= breakout_max
        and pct_chg <= max_chase
    )
    pullback = (
        np.isfinite(close)
        and np.isfinite(ma20)
        and np.isfinite(ma60)
        and close >= ma20
        and close >= ma60
        and abs(close / ma20 - 1.0) <= pullback_dist
        and np.isfinite(amount_dryup20)
        and amount_dryup20 <= pullback_dryup_max
        and pct_chg >= min_day_pct
    )
    trend_follow = (
        np.isfinite(close)
        and np.isfinite(ma20)
        and np.isfinite(ma60)
        and close > ma20 > ma60
        and safe_float(last.get("ret20"), 0.0) > 0
        and safe_float(last.get("amount_ratio20"), 1.0) <= breakout_max
        and safe_float(last.get("close_pos60"), 0.0) >= 0.55
        and min_day_pct <= pct_chg <= max_chase
    )

    reasons: List[str] = []
    if breakout120:
        reasons.append("突破120日平台且量能温和放大")
        return 25.0, "breakout_120d", "突破120日平台", reasons, blockers
    if breakout60:
        reasons.append("突破60日平台且量能温和放大")
        return 23.0, "breakout_60d", "突破60日平台", reasons, blockers
    if pullback:
        reasons.append("站回MA20/MA60且回踩缩量")
        return 22.0, "pullback_ma20", "趋势回踩", reasons, blockers
    if trend_follow:
        reasons.append("短中期趋势延续，价格处于60日强势区")
        return 18.0, "trend_follow", "趋势跟随", reasons, blockers

    if np.isfinite(close_pos60) and close_pos60 < 0.55:
        blockers.append("60日区间位置偏低")
    if np.isfinite(close_pos120) and close_pos120 < 0.50:
        blockers.append("120日区间位置偏低")
    if not np.isfinite(amount_ratio20):
        blockers.append("缺少20日量能比")
    return 0.0, "none", "无买点", reasons, blockers


def compute_etf_metrics(code: str, name: str, category: str, hist: pd.DataFrame, cfg: Dict[str, Any]) -> Dict[str, Any]:
    etf_cfg = cfg.get("etf", {})
    risk_cfg = etf_cfg.get("risk", {})
    min_history_days = int(etf_cfg.get("min_history_days", 180))
    min_amount_ma20 = float(etf_cfg.get("min_amount_ma20", 20_000_000))
    atr_period = int(risk_cfg.get("atr_period", 14))

    base = {"code": normalize_code(code), "name": name, "category": category or "未分组", "ok_base": False}
    if hist is None or hist.empty:
        base["filter_reason"] = "数据为空"
        return base
    if len(hist) < min_history_days:
        base["filter_reason"] = f"历史数据不足：{len(hist)}<{min_history_days}"
        return base

    ind = add_indicators(hist, atr_period=atr_period)
    last = ind.iloc[-1]
    close = safe_float(last.get("close"))
    amount_ma20 = safe_float(last.get("amount_ma20"))
    atr = safe_float(last.get("atr"))
    atr_pct = safe_float(last.get("atr_pct"))
    ma20 = safe_float(last.get("ma20"))
    ma60 = safe_float(last.get("ma60"))
    ma120 = safe_float(last.get("ma120"))
    pct_chg = safe_float(last.get("pct_chg"), 0.0)

    blockers: List[str] = []
    positives: List[str] = []
    if not np.isfinite(close) or close <= 0:
        blockers.append("收盘价无效")
    if not np.isfinite(amount_ma20) or amount_ma20 < min_amount_ma20:
        blockers.append(f"20日均成交额不足{min_amount_ma20:,.0f}")
    max_atr_pct = float(risk_cfg.get("max_atr_pct", 0.085))
    if np.isfinite(atr_pct) and atr_pct > max_atr_pct:
        blockers.append(f"ATR波动率{atr_pct:.2%}过高")
    max_drawdown120 = float(risk_cfg.get("max_drawdown120", -0.24))
    drawdown120 = safe_float(last.get("drawdown120"))
    if np.isfinite(drawdown120) and drawdown120 < max_drawdown120:
        blockers.append(f"距120日高点回撤{drawdown120:.2%}过深")

    trend_score = 0.0
    if np.isfinite(close) and np.isfinite(ma20) and close > ma20:
        trend_score += 8.0; positives.append("站上MA20")
    else:
        blockers.append("未站上MA20")
    if np.isfinite(close) and np.isfinite(ma60) and close > ma60:
        trend_score += 8.0; positives.append("站上MA60")
    else:
        blockers.append("未站上MA60")
    if np.isfinite(close) and np.isfinite(ma120) and close > ma120:
        trend_score += 6.0; positives.append("站上MA120")
    if np.isfinite(ma20) and np.isfinite(ma60) and np.isfinite(ma120) and ma20 > ma60 > ma120:
        trend_score += 7.0; positives.append("MA20>MA60>MA120")
    if safe_float(last.get("ma20_slope10"), 0.0) > 0:
        trend_score += 3.0; positives.append("MA20斜率向上")
    if safe_float(last.get("ma60_slope20"), 0.0) > 0:
        trend_score += 3.0; positives.append("MA60斜率向上")
    trend_score = min(trend_score, 35.0)

    momentum_score = 0.0
    ret20 = safe_float(last.get("ret20"))
    ret60 = safe_float(last.get("ret60"))
    ret120 = safe_float(last.get("ret120"))
    close_pos60 = safe_float(last.get("close_pos60"))
    close_pos120 = safe_float(last.get("close_pos120"))
    if np.isfinite(ret20) and ret20 > 0:
        momentum_score += 5.0
    if np.isfinite(ret20) and ret20 > 0.03:
        momentum_score += 3.0; positives.append("20日动量为正")
    if np.isfinite(ret60) and ret60 > 0.04:
        momentum_score += 6.0; positives.append("60日动量较强")
    if np.isfinite(ret120) and ret120 > 0.08:
        momentum_score += 5.0; positives.append("120日中期趋势占优")
    if np.isfinite(close_pos60) and close_pos60 >= 0.65:
        momentum_score += 3.0
    if np.isfinite(close_pos120) and close_pos120 >= 0.60:
        momentum_score += 3.0
    momentum_score = min(momentum_score, 25.0)

    setup_score, setup_type, setup_label, setup_reasons, setup_blockers = _score_etf_setup(last, etf_cfg)
    positives.extend(setup_reasons)
    blockers.extend(setup_blockers)

    atr_mult = float(risk_cfg.get("atr_mult", 2.2))
    min_stop_pct = float(risk_cfg.get("min_stop_pct", 0.025))
    max_stop_pct = float(risk_cfg.get("max_stop_pct", 0.10))
    stop_candidates = []
    if np.isfinite(ma20):
        stop_candidates.append(ma20 * 0.97)
    if np.isfinite(ma60):
        stop_candidates.append(ma60 * 0.985)
    if np.isfinite(atr) and np.isfinite(close):
        stop_candidates.append(close - atr_mult * atr)
    raw_stop = max(stop_candidates) if stop_candidates else close * (1 - min_stop_pct)
    min_gap_stop = close * (1 - min_stop_pct) if np.isfinite(close) else np.nan
    max_gap_stop = close * (1 - max_stop_pct) if np.isfinite(close) else np.nan
    stop_loss = raw_stop
    if np.isfinite(stop_loss) and np.isfinite(min_gap_stop) and stop_loss > min_gap_stop:
        stop_loss = min_gap_stop
    if np.isfinite(stop_loss) and np.isfinite(max_gap_stop) and stop_loss < max_gap_stop:
        stop_loss = max_gap_stop
    risk_pct = (close - stop_loss) / close if np.isfinite(close) and close > 0 and np.isfinite(stop_loss) else np.nan

    risk_score = 0.0
    if np.isfinite(amount_ma20) and amount_ma20 >= min_amount_ma20:
        risk_score += 5.0; positives.append("流动性达标")
    if np.isfinite(atr_pct) and 0.006 <= atr_pct <= max_atr_pct:
        risk_score += 4.0
    if np.isfinite(drawdown120) and drawdown120 >= max_drawdown120:
        risk_score += 3.0
    if np.isfinite(risk_pct) and min_stop_pct <= risk_pct <= max_stop_pct:
        risk_score += 3.0; positives.append("止损距离可控")
    risk_score = min(risk_score, 15.0)

    score = min(100.0, trend_score + momentum_score + setup_score + risk_score)
    score_threshold = float(etf_cfg.get("score_threshold", 70.0))
    min_trend_score = float(etf_cfg.get("trend", {}).get("min_trend_score", 20.0))
    min_setup_score = float(etf_cfg.get("setup", {}).get("min_setup_score", 16.0))
    ok_base = len(blockers) == 0
    if trend_score < min_trend_score:
        ok_base = False
        blockers.append(f"趋势分不足{min_trend_score:.0f}")
    setup_ok = setup_score >= min_setup_score
    is_signal = bool(ok_base and setup_ok and score >= score_threshold)
    if not setup_ok:
        blockers.append("买点未成型")
    if score < score_threshold:
        blockers.append(f"综合分{score:.1f}低于阈值{score_threshold:.1f}")

    return {
        **base,
        "date": pd.to_datetime(last.get("date")).strftime("%Y-%m-%d"),
        "close": close,
        "pct_chg": pct_chg,
        "score": score,
        "trend_score": trend_score,
        "momentum_score": momentum_score,
        "setup_score": setup_score,
        "risk_score": risk_score,
        "setup_type": setup_type,
        "setup_label": setup_label,
        "setup_ok": setup_ok,
        "ok_base": ok_base,
        "is_signal": is_signal,
        "stop_loss": stop_loss,
        "risk_pct": risk_pct,
        "take_profit_1": close + 1.5 * (close - stop_loss) if np.isfinite(close) and np.isfinite(stop_loss) else np.nan,
        "take_profit_2": close + 3.0 * (close - stop_loss) if np.isfinite(close) and np.isfinite(stop_loss) else np.nan,
        "ret20": ret20,
        "ret60": ret60,
        "ret120": ret120,
        "close_pos60": close_pos60,
        "close_pos120": close_pos120,
        "drawdown120": drawdown120,
        "amount_ma20": amount_ma20,
        "amount_ratio20": safe_float(last.get("amount_ratio20")),
        "amount_dryup20": safe_float(last.get("amount_dryup20")),
        "atr_pct": atr_pct,
        "data_provider": hist.attrs.get("data_provider", ""),
        "positive_factors": "；".join(unique_nonempty(positives)),
        "filter_reason": "；".join(unique_nonempty(blockers)),
        "score_detail": f"趋势{trend_score:.1f}+动量{momentum_score:.1f}+买点{setup_score:.1f}+风控{risk_score:.1f}",
    }


def allocate_etf_positions(signals: pd.DataFrame, cfg: Dict[str, Any], account: float) -> pd.DataFrame:
    if signals is None or signals.empty:
        return pd.DataFrame()
    etf_cfg = cfg.get("etf", {})
    max_positions = int(etf_cfg.get("max_positions", 5))
    total_exposure = float(etf_cfg.get("total_exposure", 0.90))
    max_position_pct = float(etf_cfg.get("max_position_pct", 0.25))
    min_lot = int(etf_cfg.get("min_lot", 100))

    out = signals.sort_values(["score", "trend_score", "momentum_score"], ascending=[False, False, False]).head(max_positions).copy()
    risk = pd.to_numeric(out.get("risk_pct", pd.Series(index=out.index, dtype=float)), errors="coerce").clip(lower=0.025)
    inv_risk = 1.0 / risk.replace(0, np.nan)
    if inv_risk.notna().sum() == 0:
        raw_weights = pd.Series(1.0 / max(1, len(out)), index=out.index)
    else:
        raw_weights = inv_risk / inv_risk.sum()
    out["target_weight"] = (raw_weights * total_exposure).clip(upper=max_position_pct)
    out["target_cash"] = out["target_weight"] * float(account)
    close = pd.to_numeric(out["close"], errors="coerce")
    shares = np.floor(out["target_cash"] / close / min_lot) * min_lot
    shares = shares.replace([np.inf, -np.inf], np.nan).fillna(0).astype(int)
    out["target_shares"] = shares
    out["actual_weight_by_lot"] = np.where(account > 0, shares * close / float(account), 0.0)
    return out.reset_index(drop=True)


def to_chinese_columns(df: pd.DataFrame) -> pd.DataFrame:
    mapping = {
        "date": "日期",
        "code": "ETF代码",
        "name": "ETF名称",
        "category": "类别/跟踪指数",
        "close": "收盘价",
        "pct_chg": "当日涨跌幅%",
        "score": "综合分",
        "trend_score": "趋势分",
        "momentum_score": "动量分",
        "setup_score": "买点分",
        "risk_score": "风控分",
        "setup_type": "买点类型",
        "setup_label": "买点名称",
        "ok_base": "基础过滤通过",
        "setup_ok": "买点通过",
        "is_signal": "是否买入信号",
        "target_weight": "目标仓位",
        "target_cash": "建议买入金额",
        "target_shares": "建议份额",
        "actual_weight_by_lot": "按整手实际仓位",
        "stop_loss": "止损价",
        "risk_pct": "止损幅度",
        "take_profit_1": "止盈1_1.5R",
        "take_profit_2": "止盈2_3R",
        "ret20": "20日收益",
        "ret60": "60日收益",
        "ret120": "120日收益",
        "close_pos60": "60日区间位置",
        "close_pos120": "120日区间位置",
        "drawdown120": "距120日高点",
        "amount_ma20": "20日均成交额",
        "amount_ratio20": "成交额/20日均额",
        "amount_dryup20": "短期缩量比",
        "atr_pct": "ATR波动率",
        "data_provider": "K线数据源",
        "positive_factors": "加分项",
        "filter_reason": "过滤/扣分原因",
        "score_detail": "评分拆解",
    }
    preferred = [
        "date", "code", "name", "category", "is_signal", "close", "pct_chg", "score", "score_detail",
        "trend_score", "momentum_score", "setup_score", "risk_score", "setup_label", "target_weight",
        "target_cash", "target_shares", "actual_weight_by_lot", "stop_loss", "risk_pct", "take_profit_1",
        "take_profit_2", "ret20", "ret60", "ret120", "close_pos60", "close_pos120", "drawdown120",
        "amount_ma20", "amount_ratio20", "amount_dryup20", "atr_pct", "data_provider", "positive_factors",
        "filter_reason",
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=[mapping.get(c, c) for c in preferred])
    cols = [c for c in preferred if c in df.columns] + [c for c in df.columns if c not in preferred]
    out = df[cols].copy().rename(columns=mapping)
    for c in ["目标仓位", "按整手实际仓位", "止损幅度", "20日收益", "60日收益", "120日收益", "60日区间位置", "120日区间位置", "距120日高点", "成交额/20日均额", "短期缩量比", "ATR波动率"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.2%}" if pd.notna(x) else "")
    for c in ["收盘价", "止损价", "止盈1_1.5R", "止盈2_3R"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.3f}" if pd.notna(x) else "")
    for c in ["综合分", "趋势分", "动量分", "买点分", "风控分"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.1f}" if pd.notna(x) else "")
    if "当日涨跌幅%" in out.columns:
        out["当日涨跌幅%"] = pd.to_numeric(out["当日涨跌幅%"], errors="coerce").map(lambda x: f"{x:.2f}" if pd.notna(x) else "")
    for c in ["建议买入金额", "20日均成交额"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:,.0f}" if pd.notna(x) else "")
    return out


def format_etf_message(signals: pd.DataFrame, candidates: pd.DataFrame, account: float) -> str:
    lines = [f"ETF策略信号 {now_cn().strftime('%Y-%m-%d %H:%M')}"]
    n_total = 0 if candidates is None else len(candidates)
    n_signal = int(candidates["is_signal"].fillna(False).sum()) if candidates is not None and not candidates.empty and "is_signal" in candidates.columns else 0
    lines.append(f"ETF池扫描：{n_total}只；买入候选：{n_signal}只；最终配置：{0 if signals is None or signals.empty else len(signals)}只。")
    lines.append("模型：ETF独立趋势动量 + 平台突破/缩量回踩 + ATR止损 + 风险平价仓位。")
    lines.append("")
    if signals is None or signals.empty:
        lines.append("今日无ETF买入配置。")
    else:
        total_weight = float(signals["target_weight"].sum()) if "target_weight" in signals.columns else 0.0
        lines.append(f"ETF买入配置 {len(signals)} 只，建议合计仓位约 {total_weight:.0%}：")
        for _, r in signals.iterrows():
            code = str(r.get("code", ""))
            name = str(r.get("name", ""))
            close = safe_float(r.get("close"))
            score = safe_float(r.get("score"))
            weight = safe_float(r.get("target_weight"), 0.0)
            cash = safe_float(r.get("target_cash"), np.nan)
            shares = int(safe_float(r.get("target_shares"), 0))
            stop = safe_float(r.get("stop_loss"))
            setup = str(r.get("setup_label", ""))
            if account > 0 and np.isfinite(cash):
                pos = f"仓位{weight:.1%}，约{cash:,.0f}元，{shares}份"
            else:
                pos = f"仓位{weight:.1%}"
            lines.append(f"- {code} {name}：{setup}，收盘{close:.3f}，分数{score:.1f}，{pos}，止损{stop:.3f}")
    lines.append("")
    lines.append("已生成：latest_etf_signals.csv、latest_etf_candidates.csv、latest_etf_report.md。")
    lines.append("提示：ETF策略与个股策略独立运行，不自动下单。")
    return "\n".join(lines)


def format_etf_report(candidates: pd.DataFrame, signals: pd.DataFrame, account: float) -> str:
    lines = [f"# ETF策略报告 {now_cn().strftime('%Y-%m-%d %H:%M')}"]
    lines.append("")
    lines.append("策略独立于个股扫描器：不使用个股板块主线、个股风险闸门或股票池热榜逻辑。")
    lines.append("")
    lines.append(format_etf_message(signals, candidates, account))
    if candidates is not None and not candidates.empty:
        lines.append("")
        lines.append("## 候选明细")
        show = candidates.sort_values(["is_signal", "score"], ascending=[False, False]).head(30)
        for _, r in show.iterrows():
            status = "买入候选" if bool(r.get("is_signal", False)) else "未触发"
            lines.append(
                f"- {r.get('code', '')} {r.get('name', '')} [{status}] "
                f"分数{safe_float(r.get('score'), 0):.1f}，{r.get('setup_label', '')}，"
                f"{r.get('score_detail', '')}；{r.get('filter_reason', '') or r.get('positive_factors', '')}"
            )
    return "\n".join(lines)


def scan_etf(
    pool_path: str,
    cfg: Dict[str, Any],
    out_dir: str,
    account: float,
    refresh: bool = False,
    limit: int = 0,
) -> Tuple[pd.DataFrame, pd.DataFrame, Path]:
    out_path = ensure_dir(out_dir)
    removed_old = cleanup_output_dir(out_path, cfg)
    if removed_old:
        print(f"[清理] ETF输出目录已清理历史文件 {len(removed_old)} 个")
    etf_cfg = cfg.get("etf", {})
    start_date = etf_cfg.get("start_date") or cfg.get("data", {}).get("start_date") or "20200101"
    end_date = etf_cfg.get("end_date") or cfg.get("data", {}).get("end_date") or today_yyyymmdd()
    adjust = etf_cfg.get("adjust", "")

    pool = read_etf_pool(pool_path)
    if limit and limit > 0:
        pool = pool.head(limit).copy()

    fetcher = EtfFetcher(cfg, refresh=refresh)
    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    total = len(pool)
    for n, (_, r) in enumerate(pool.iterrows(), start=1):
        code = str(r["code"])
        name = str(r.get("name", ""))
        category = str(r.get("category", "未分组"))
        try:
            hist = fetcher.etf_hist(code, start_date, end_date, adjust)
            row = compute_etf_metrics(code, name, category, hist, cfg)
            rows.append(row)
        except Exception as exc:
            errors.append({"code": code, "name": name, "error": str(exc)})
            rows.append({"code": code, "name": name, "category": category, "ok_base": False, "is_signal": False, "score": 0.0, "filter_reason": f"数据错误：{exc}"})
        if n % 10 == 0 or n == total:
            print(f"[ETF进度] 已扫描 {n}/{total}")

    candidates = pd.DataFrame(rows)
    if not candidates.empty:
        if "is_signal" not in candidates.columns:
            candidates["is_signal"] = False
        if "score" not in candidates.columns:
            candidates["score"] = 0.0
        candidates = candidates.sort_values(["is_signal", "score"], ascending=[False, False], na_position="last").reset_index(drop=True)
    signals = candidates[candidates.get("is_signal", False) == True].copy() if not candidates.empty else pd.DataFrame()
    allocated = allocate_etf_positions(signals, cfg, account)

    run_date = now_cn().strftime("%Y%m%d_%H%M%S")
    candidates_path = out_path / f"etf_candidates_{run_date}.csv"
    signals_path = out_path / f"etf_signals_{run_date}.csv"
    report_path = out_path / f"etf_report_{run_date}.md"
    errors_path = out_path / f"etf_errors_{run_date}.csv"

    to_chinese_columns(candidates).to_csv(candidates_path, index=False, encoding="utf-8-sig")
    to_chinese_columns(allocated).to_csv(signals_path, index=False, encoding="utf-8-sig")
    candidates.to_csv(out_path / f"etf_candidates_raw_{run_date}.csv", index=False, encoding="utf-8-sig")
    allocated.to_csv(out_path / f"etf_signals_raw_{run_date}.csv", index=False, encoding="utf-8-sig")
    to_chinese_columns(candidates).to_csv(out_path / "latest_etf_candidates.csv", index=False, encoding="utf-8-sig")
    to_chinese_columns(allocated).to_csv(out_path / "latest_etf_signals.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(out_path / "latest_etf_candidates_raw.csv", index=False, encoding="utf-8-sig")
    allocated.to_csv(out_path / "latest_etf_signals_raw.csv", index=False, encoding="utf-8-sig")
    if errors:
        pd.DataFrame(errors).to_csv(errors_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(errors).to_csv(out_path / "latest_etf_errors.csv", index=False, encoding="utf-8-sig")

    report = format_etf_report(candidates, allocated, account)
    report_path.write_text(report, encoding="utf-8")
    (out_path / "latest_etf_report.md").write_text(report, encoding="utf-8")
    msg = format_etf_message(allocated, candidates, account)
    msg_path = out_path / f"etf_message_{run_date}.txt"
    msg_path.write_text(msg, encoding="utf-8")
    (out_path / "latest_etf_message.txt").write_text(msg, encoding="utf-8")

    print(f"[ETF输出] {signals_path}")
    print(f"[ETF输出] {candidates_path}")
    print(f"[ETF输出] {report_path}")
    return allocated, candidates, msg_path


def send_webhook(msg: str, url: str, webhook_type: str = "wecom") -> None:
    if not url:
        print("[ETF推送] webhook_url 为空，跳过推送")
        return
    webhook_type = (webhook_type or "wecom").lower()
    headers = {"Content-Type": "application/json"}
    if webhook_type in {"wecom", "dingtalk", "generic"}:
        payload = {"msgtype": "text", "text": {"content": msg}}
    elif webhook_type == "feishu":
        payload = {"msg_type": "text", "content": {"text": msg}}
    else:
        payload = {"text": msg}
    resp = requests.post(url, headers=headers, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f"ETF推送失败: HTTP {resp.status_code} {resp.text[:200]}")
    print("[ETF推送] 已发送 webhook")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A股ETF独立交易策略扫描器")
    parser.add_argument("--pool", default="", help="ETF池文件，默认读取配置 etf.pool")
    parser.add_argument("--config", default="", help="配置文件 YAML/JSON，可选")
    parser.add_argument("--out", default="", help="ETF输出目录，默认读取配置 etf.out_dir")
    parser.add_argument("--account", type=float, default=100000.0, help="账户权益，用于换算买入金额/份额，默认 100000")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新拉取ETF数据")
    parser.add_argument("--limit", type=int, default=0, help="只扫描前 N 只，测试用")
    parser.add_argument("--start-date", default="", help="覆盖 ETF 日线开始日期 YYYYMMDD")
    parser.add_argument("--end-date", default="", help="覆盖 ETF 日线结束日期 YYYYMMDD")
    parser.add_argument("--adjust", default=None, help="复权口径：空字符串/ qfq / hfq；ETF默认不复权")
    parser.add_argument("--send", action="store_true", help="扫描后发送 webhook")
    parser.add_argument("--webhook-url", default="", help="企业微信/钉钉/飞书 webhook URL；也可用环境变量 SIGNAL_WEBHOOK_URL")
    parser.add_argument("--webhook-type", default="", choices=["", "wecom", "dingtalk", "feishu", "generic"], help="webhook 类型")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    etf_cfg = cfg.setdefault("etf", {})
    if args.start_date:
        etf_cfg["start_date"] = args.start_date
    if args.end_date:
        etf_cfg["end_date"] = args.end_date
    if args.adjust is not None:
        etf_cfg["adjust"] = args.adjust
    pool_path = args.pool or etf_cfg.get("pool", "etf_pool.csv")
    out_dir = args.out or etf_cfg.get("out_dir", "etf_output")

    allocated, candidates, msg_path = scan_etf(
        pool_path,
        cfg,
        out_dir,
        account=float(args.account),
        refresh=bool(args.refresh),
        limit=int(args.limit or 0),
    )
    msg = Path(msg_path).read_text(encoding="utf-8")
    print(msg)
    webhook_url = args.webhook_url or os.environ.get("SIGNAL_WEBHOOK_URL", "")
    webhook_type = args.webhook_type or cfg.get("notify", {}).get("webhook_type", "wecom")
    if args.send:
        send_webhook(msg, webhook_url, webhook_type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
