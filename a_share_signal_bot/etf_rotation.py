# -*- coding: utf-8 -*-
from __future__ import annotations

from .base import *
from .etf_strategy import EtfFetcher, read_etf_pool
from .market_data import add_indicators, safe_float

ETF_ROTATION_STATE_COLUMNS = [
    "code", "name", "status", "target_weight", "last_selected_date", "cooldown_until", "updated_at",
]


def classify_asset_class(name: Any, category: Any) -> str:
    text = f"{name or ''} {category or ''}".lower()
    cn_text = f"{name or ''} {category or ''}"
    if any(k in cn_text for k in ["债", "货币", "国债", "政金债", "短融"]):
        return "defensive"
    if any(k in cn_text for k in ["黄金", "商品", "原油", "豆粕", "有色"]):
        return "commodity"
    if any(k in cn_text for k in ["纳指", "标普", "恒生", "港股", "香港", "中概", "中国互联", "日经", "亚太", "韩国", "中韩", "印度", "德国", "法国", "美国", "海外", "全球", "QDII"]) or "qdii" in text:
        return "cross_border"
    if any(k in cn_text for k in ["证券", "券商", "银行", "保险", "白酒", "消费", "医药", "医疗", "创新药", "生物", "半导体", "芯片", "通信", "人工智能", "计算机", "软件", "新能源", "光伏", "军工", "传媒", "游戏", "电力", "电网", "煤炭", "钢铁", "房地产", "机器人", "卫星", "化工", "设备"]):
        return "sector"
    if any(k in cn_text for k in ["沪深300", "中证A500", "中证500", "中证1000", "上证50", "创业板ETF", "创业板50", "科创50", "科创板50", "深证100", "A500", "宽基"]):
        return "broad"
    return "sector"


def _rank_pct(values: pd.Series, higher_is_better: bool = True) -> pd.Series:
    s = pd.to_numeric(values, errors="coerce")
    if s.notna().sum() <= 1:
        return pd.Series(0.5, index=values.index)
    ranked = s.rank(pct=True, ascending=not higher_is_better)
    return ranked.fillna(0.5)


def _hist_until(hist: pd.DataFrame, as_of: Optional[pd.Timestamp]) -> pd.DataFrame:
    if hist is None or hist.empty:
        return pd.DataFrame()
    out = hist.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date"]).sort_values("date")
    if as_of is not None:
        out = out[out["date"] <= as_of]
    return out.reset_index(drop=True)


def _read_rotation_state(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=ETF_ROTATION_STATE_COLUMNS)
    try:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, dtype=str, encoding="gbk")
    if df is None or df.empty:
        return pd.DataFrame(columns=ETF_ROTATION_STATE_COLUMNS)
    out = df.copy()
    for col in ETF_ROTATION_STATE_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    out["code"] = out["code"].map(normalize_code)
    out = out[out["code"].astype(str).str.len() == 6].copy()
    out["target_weight"] = pd.to_numeric(out["target_weight"], errors="coerce").fillna(0.0)
    out["status"] = out["status"].replace("", np.nan).fillna("ACTIVE")
    return out[ETF_ROTATION_STATE_COLUMNS].reset_index(drop=True)


def _state_current_weights(state: pd.DataFrame) -> Dict[str, float]:
    if state is None or state.empty:
        return {}
    active = state[state["status"].astype(str).str.upper().eq("ACTIVE")].copy()
    weights: Dict[str, float] = {}
    for _, row in active.iterrows():
        code = normalize_code(row.get("code", ""))
        weight = safe_float(row.get("target_weight"), 0.0)
        if code and weight > 0:
            weights[code] = weight
    return weights


def _state_cooldown_until(state: pd.DataFrame) -> Dict[str, pd.Timestamp]:
    if state is None or state.empty:
        return {}
    out: Dict[str, pd.Timestamp] = {}
    for _, row in state.iterrows():
        code = normalize_code(row.get("code", ""))
        until = pd.to_datetime(row.get("cooldown_until", ""), errors="coerce")
        if code and pd.notna(until):
            out[code] = pd.Timestamp(until)
    return out


def _rotation_state_path(out_dir: Path, cfg: Dict[str, Any]) -> Path:
    rot_cfg = cfg.get("etf", {}).get("rotation", {})
    raw = str(rot_cfg.get("state_path", "") or "").strip()
    if not raw:
        return out_dir / "etf_rotation_state.csv"
    path = Path(raw)
    return path if path.is_absolute() else Path(raw)


def _latest_candidate_date(candidates: pd.DataFrame) -> pd.Timestamp:
    if candidates is not None and not candidates.empty and "date" in candidates.columns:
        dates = pd.to_datetime(candidates["date"], errors="coerce").dropna()
        if not dates.empty:
            return pd.Timestamp(dates.max()).normalize()
    return pd.Timestamp(now_cn().date())


def _cooldown_expiry_from_date(as_of: pd.Timestamp, cooldown_days: int) -> str:
    if cooldown_days <= 0:
        return ""
    return (pd.Timestamp(as_of).normalize() + pd.tseries.offsets.BDay(cooldown_days)).strftime("%Y-%m-%d")


def _write_rotation_state(
    path: Path,
    previous_state: pd.DataFrame,
    positions: pd.DataFrame,
    rel_cfg: Dict[str, Any],
    as_of: pd.Timestamp,
) -> None:
    cooldown_days = int(rel_cfg.get("cooldown_days", 0) or 0)
    previous_weights = _state_current_weights(previous_state)
    previous_cooldown = _state_cooldown_until(previous_state)
    selected_codes = set()
    rows: List[Dict[str, Any]] = []
    updated_at = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    as_of = pd.Timestamp(as_of).normalize()

    if positions is not None and not positions.empty:
        for _, row in positions.iterrows():
            code = normalize_code(row.get("code", ""))
            if not code:
                continue
            selected_codes.add(code)
            rows.append({
                "code": code,
                "name": str(row.get("name", "")),
                "status": "ACTIVE",
                "target_weight": safe_float(row.get("target_weight"), 0.0),
                "last_selected_date": as_of.strftime("%Y-%m-%d"),
                "cooldown_until": "",
                "updated_at": updated_at,
            })

    expiry = _cooldown_expiry_from_date(as_of, cooldown_days)
    previous_lookup = previous_state.set_index("code").to_dict("index") if previous_state is not None and not previous_state.empty else {}
    for code in sorted(set(previous_weights) - selected_codes):
        if cooldown_days <= 0:
            continue
        old = previous_lookup.get(code, {})
        rows.append({
            "code": code,
            "name": str(old.get("name", "")),
            "status": "COOLDOWN",
            "target_weight": 0.0,
            "last_selected_date": str(old.get("last_selected_date", "")),
            "cooldown_until": expiry,
            "updated_at": updated_at,
        })

    if cooldown_days > 0:
        for code, until in previous_cooldown.items():
            if code in selected_codes or code in previous_weights:
                continue
            if pd.Timestamp(until).normalize() < as_of:
                continue
            old = previous_lookup.get(code, {})
            rows.append({
                "code": code,
                "name": str(old.get("name", "")),
                "status": "COOLDOWN",
                "target_weight": 0.0,
                "last_selected_date": str(old.get("last_selected_date", "")),
                "cooldown_until": pd.Timestamp(until).strftime("%Y-%m-%d"),
                "updated_at": updated_at,
            })

    out = pd.DataFrame(rows, columns=ETF_ROTATION_STATE_COLUMNS).drop_duplicates("code", keep="first")
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False, encoding="utf-8-sig")


def compute_rotation_candidates(
    pool: pd.DataFrame,
    hist_map: Dict[str, pd.DataFrame],
    cfg: Dict[str, Any],
    as_of: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    etf_cfg = cfg.get("etf", {})
    rot_cfg = etf_cfg.get("rotation", {})
    risk_cfg = etf_cfg.get("risk", {})
    min_history_days = int(rot_cfg.get("min_history_days", etf_cfg.get("min_history_days", 180)))
    min_amount_ma20 = float(rot_cfg.get("min_amount_ma20", etf_cfg.get("min_amount_ma20", 20_000_000)))
    atr_period = int(risk_cfg.get("atr_period", 14))
    rows: List[Dict[str, Any]] = []

    for _, r in pool.iterrows():
        code = normalize_code(r.get("code", ""))
        name = str(r.get("name", ""))
        category = str(r.get("category", "未分组") or "未分组")
        hist = _hist_until(hist_map.get(code, pd.DataFrame()), as_of)
        base = {
            "date": "" if as_of is None else pd.Timestamp(as_of).strftime("%Y-%m-%d"),
            "code": code,
            "name": name,
            "category": category,
            "asset_class": classify_asset_class(name, category),
            "tradable": False,
            "rotation_score": 0.0,
            "rotation_reason": "",
            "filter_reason": "",
        }
        if len(hist) < min_history_days:
            base["filter_reason"] = f"历史数据不足：{len(hist)}<{min_history_days}"
            rows.append(base)
            continue

        ind = add_indicators(hist, atr_period=atr_period)
        last = ind.iloc[-1]
        close = safe_float(last.get("close"))
        amount_ma20 = safe_float(last.get("amount_ma20"))
        atr_pct = safe_float(last.get("atr_pct"))
        ma20 = safe_float(last.get("ma20"))
        ma60 = safe_float(last.get("ma60"))
        ma120 = safe_float(last.get("ma120"))
        row = {
            **base,
            "date": pd.to_datetime(last.get("date")).strftime("%Y-%m-%d"),
            "close": close,
            "pct_chg": safe_float(last.get("pct_chg"), 0.0),
            "ret20": safe_float(last.get("ret20")),
            "ret60": safe_float(last.get("ret60")),
            "ret120": safe_float(last.get("ret120")),
            "close_pos60": safe_float(last.get("close_pos60")),
            "close_pos120": safe_float(last.get("close_pos120")),
            "drawdown120": safe_float(last.get("drawdown120")),
            "amount_ma20": amount_ma20,
            "amount_ratio20": safe_float(last.get("amount_ratio20")),
            "amount_dryup20": safe_float(last.get("amount_dryup20")),
            "atr_pct": atr_pct,
            "ma20": ma20,
            "ma60": ma60,
            "ma120": ma120,
            "ma20_slope10": safe_float(last.get("ma20_slope10")),
            "ma60_slope20": safe_float(last.get("ma60_slope20")),
            "above_ma20": bool(np.isfinite(close) and np.isfinite(ma20) and close > ma20),
            "above_ma60": bool(np.isfinite(close) and np.isfinite(ma60) and close > ma60),
            "above_ma120": bool(np.isfinite(close) and np.isfinite(ma120) and close > ma120),
            "data_provider": hist.attrs.get("data_provider", ""),
        }
        blockers = []
        if not np.isfinite(close) or close <= 0:
            blockers.append("价格无效")
        if not np.isfinite(amount_ma20) or amount_ma20 < min_amount_ma20:
            blockers.append(f"20日均成交额不足{min_amount_ma20:,.0f}")
        row["tradable"] = len(blockers) == 0
        row["filter_reason"] = "；".join(blockers)
        rows.append(row)

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    out["trend_component"] = 0.0
    out.loc[out["above_ma20"].fillna(False), "trend_component"] += 6.0
    out.loc[out["above_ma60"].fillna(False), "trend_component"] += 8.0
    out.loc[out["above_ma120"].fillna(False), "trend_component"] += 6.0
    out.loc[pd.to_numeric(out.get("ma20_slope10"), errors="coerce") > 0, "trend_component"] += 3.0
    out.loc[pd.to_numeric(out.get("ma60_slope20"), errors="coerce") > 0, "trend_component"] += 2.0

    out["momentum_component"] = (
        _rank_pct(out.get("ret20", pd.Series(index=out.index)), True) * 12.0
        + _rank_pct(out.get("ret60", pd.Series(index=out.index)), True) * 18.0
        + _rank_pct(out.get("ret120", pd.Series(index=out.index)), True) * 10.0
    )
    out["risk_component"] = (
        _rank_pct(out.get("atr_pct", pd.Series(index=out.index)), False) * 8.0
        + _rank_pct(out.get("drawdown120", pd.Series(index=out.index)), True) * 8.0
        + _rank_pct(out.get("amount_ma20", pd.Series(index=out.index)), True) * 4.0
    )
    close_pos60 = pd.to_numeric(out.get("close_pos60", pd.Series(index=out.index)), errors="coerce").clip(0, 1).fillna(0.5)
    close_pos120 = pd.to_numeric(out.get("close_pos120", pd.Series(index=out.index)), errors="coerce").clip(0, 1).fillna(0.5)
    out["timing_component"] = close_pos60 * 7.0 + close_pos120 * 8.0
    out["rotation_score"] = (
        out["trend_component"]
        + out["momentum_component"]
        + out["risk_component"]
        + out["timing_component"]
    ).clip(0, 100)

    if str(rot_cfg.get("model", "balanced")) == "relative_momentum":
        rel_cfg = rot_cfg.get("relative_momentum", {})
        score_weights = dict(rel_cfg.get("score_weights", {}))
        w20 = float(score_weights.get("ret20", 0.35))
        w60 = float(score_weights.get("ret60", 0.45))
        w120 = float(score_weights.get("ret120", 0.20))
        total_w = max(1e-9, abs(w20) + abs(w60) + abs(w120))
        w20, w60, w120 = w20 / total_w, w60 / total_w, w120 / total_w
        out["momentum_signal"] = (
            pd.to_numeric(out.get("ret20"), errors="coerce").fillna(-1.0) * w20
            + pd.to_numeric(out.get("ret60"), errors="coerce").fillna(-1.0) * w60
            + pd.to_numeric(out.get("ret120"), errors="coerce").fillna(-1.0) * w120
        )
        out["rotation_score"] = _rank_pct(out["momentum_signal"], True) * 100.0
        min_rel_ret60 = float(rel_cfg.get("min_ret60", 0.0))
        allow_defensive = bool(rel_cfg.get("allow_defensive", False))
        defensive_assets = set(rot_cfg.get("defensive_asset_classes", ["defensive", "commodity"]))
        reasons = []
        candidates = []
        filters = []
        for _, r in out.iterrows():
            local_filters = split_reason_text(r.get("filter_reason", ""))
            asset_class = str(r.get("asset_class", ""))
            if (not allow_defensive) and asset_class in defensive_assets:
                local_filters.append("相对动量模式排除防守资产")
            if safe_float(r.get("ret60"), -1.0) < min_rel_ret60:
                local_filters.append(f"60日动量低于{min_rel_ret60:.1%}")
            candidates.append(len(local_filters) == 0)
            filters.append("；".join(unique_nonempty(local_filters)))
            reasons.append(
                f"20/60/120日动量加权{safe_float(r.get('momentum_signal'), 0.0):.2%}；"
                f"60日收益{safe_float(r.get('ret60'), 0.0):.2%}"
            )
        out["is_rotation_candidate"] = candidates
        out["filter_reason"] = filters
        out["rotation_reason"] = reasons
        return out.sort_values(["is_rotation_candidate", "momentum_signal"], ascending=[False, False]).reset_index(drop=True)

    score_threshold = float(rot_cfg.get("score_threshold", 55.0))
    min_ret60 = float(rot_cfg.get("min_ret60", -0.03))
    require_ma60 = bool(rot_cfg.get("require_ma60", True))
    defensive_assets = set(rot_cfg.get("defensive_asset_classes", ["defensive", "commodity"]))
    reasons: List[str] = []
    candidates: List[bool] = []
    filters: List[str] = []
    for _, r in out.iterrows():
        local_filters = split_reason_text(r.get("filter_reason", ""))
        asset_class = str(r.get("asset_class", ""))
        is_defensive = asset_class in defensive_assets
        if not bool(r.get("tradable", False)):
            pass
        if safe_float(r.get("rotation_score"), 0.0) < score_threshold:
            local_filters.append(f"轮动分低于{score_threshold:.1f}")
        if safe_float(r.get("ret60"), -1.0) < min_ret60 and not is_defensive:
            local_filters.append(f"60日动量低于{min_ret60:.1%}")
        if require_ma60 and (not bool(r.get("above_ma60", False))) and not is_defensive:
            local_filters.append("未站上MA60")
        candidates.append(len(local_filters) == 0)
        filters.append("；".join(unique_nonempty(local_filters)))
        reason_bits = []
        if bool(r.get("above_ma60", False)):
            reason_bits.append("站上MA60")
        if safe_float(r.get("ret60"), 0.0) > 0:
            reason_bits.append(f"60日收益{safe_float(r.get('ret60')):.2%}")
        if safe_float(r.get("drawdown120"), -1.0) > -0.10:
            reason_bits.append("回撤较浅")
        reasons.append("；".join(reason_bits))
    out["is_rotation_candidate"] = candidates
    out["filter_reason"] = filters
    out["rotation_reason"] = reasons
    return out.sort_values(["is_rotation_candidate", "rotation_score"], ascending=[False, False]).reset_index(drop=True)


def market_regime_from_candidates(candidates: pd.DataFrame, cfg: Dict[str, Any]) -> Dict[str, Any]:
    rot_cfg = cfg.get("etf", {}).get("rotation", {})
    if candidates is None or candidates.empty:
        return {"regime": "weak", "target_exposure": float(rot_cfg.get("weak_total_exposure", 0.35)), "summary": "ETF池无有效候选"}
    broad = candidates[candidates["asset_class"].eq("broad")].copy()
    if broad.empty:
        broad = candidates[~candidates["asset_class"].isin(["defensive", "commodity"])].copy()
    if broad.empty:
        broad = candidates.copy()
    avg_ret60 = float(pd.to_numeric(broad.get("ret60"), errors="coerce").mean())
    above_rate = float(broad.get("above_ma60", pd.Series(dtype=bool)).fillna(False).mean())
    if str(rot_cfg.get("model", "balanced")) == "relative_momentum":
        exposure = float(rot_cfg.get("relative_momentum", {}).get("target_exposure", 1.0))
        return {
            "regime": "relative_momentum",
            "target_exposure": exposure,
            "avg_ret60": avg_ret60,
            "above_ma60_rate": above_rate,
            "summary": f"ETF相对动量：权益ETF 60日均收益{avg_ret60:.2%}，目标仓位{exposure:.0%}",
        }
    if avg_ret60 >= float(rot_cfg.get("strong_ret60", 0.04)) and above_rate >= float(rot_cfg.get("strong_above_ma60_rate", 0.60)):
        regime = "strong"
        exposure = float(rot_cfg.get("strong_total_exposure", 0.90))
    elif avg_ret60 >= float(rot_cfg.get("neutral_ret60", 0.00)) and above_rate >= float(rot_cfg.get("neutral_above_ma60_rate", 0.45)):
        regime = "neutral"
        exposure = float(rot_cfg.get("neutral_total_exposure", 0.65))
    else:
        regime = "weak"
        exposure = float(rot_cfg.get("weak_total_exposure", 0.35))
    return {
        "regime": regime,
        "target_exposure": exposure,
        "avg_ret60": avg_ret60,
        "above_ma60_rate": above_rate,
        "summary": f"ETF市场{regime}：宽基/权益ETF 60日均收益{avg_ret60:.2%}，站上MA60比例{above_rate:.0%}",
    }


def close_return_frame(hist_map: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    series = {}
    for code, hist in hist_map.items():
        if hist is None or hist.empty:
            continue
        h = hist.copy()
        h["date"] = pd.to_datetime(h["date"], errors="coerce")
        h["close"] = pd.to_numeric(h["close"], errors="coerce")
        h = h.dropna(subset=["date", "close"]).sort_values("date")
        if not h.empty:
            series[normalize_code(code)] = h.set_index("date")["close"]
    if not series:
        return pd.DataFrame()
    closes = pd.DataFrame(series).sort_index()
    return closes.pct_change().replace([np.inf, -np.inf], np.nan)


def _max_selected_corr(code: str, selected: List[str], returns: pd.DataFrame, lookback: int) -> float:
    if not selected or returns.empty or code not in returns.columns:
        return 0.0
    sub = returns[[c for c in [code] + selected if c in returns.columns]].tail(lookback)
    if len(sub) < 30:
        return 0.0
    corr = sub.corr()[code].drop(labels=[code], errors="ignore").abs()
    if corr.empty:
        return 0.0
    return float(corr.max())


def _asset_class_caps(rot_cfg: Dict[str, Any], regime: str) -> Dict[str, float]:
    caps_cfg = rot_cfg.get("asset_class_caps", {})
    if not isinstance(caps_cfg, dict):
        return {}
    raw = caps_cfg.get(regime, caps_cfg.get("default", {}))
    if not isinstance(raw, dict):
        return {}
    caps: Dict[str, float] = {}
    for k, v in raw.items():
        try:
            caps[str(k)] = max(0.0, min(1.0, float(v)))
        except Exception:
            continue
    return caps


def _distribute_with_caps(
    rows: pd.DataFrame,
    preferences: pd.Series,
    target_exposure: float,
    max_position_pct: float,
    asset_caps: Dict[str, float],
) -> pd.Series:
    weights = pd.Series(0.0, index=rows.index, dtype=float)
    prefs = pd.to_numeric(preferences, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(lower=0.0)
    if prefs.sum() <= 0:
        prefs = pd.Series(1.0, index=rows.index, dtype=float)
    target = max(0.0, min(1.0, float(target_exposure)))
    max_pos = max(0.0, min(1.0, float(max_position_pct)))
    remaining = target
    active = set(rows.index.tolist())
    eps = 1e-9
    for _ in range(len(rows) + 4):
        if remaining <= eps or not active:
            break
        capacities = {}
        for idx in list(active):
            asset_class = str(rows.at[idx, "asset_class"]) if "asset_class" in rows.columns else ""
            class_cap = float(asset_caps.get(asset_class, 1.0))
            class_used = float(weights[rows["asset_class"].astype(str).eq(asset_class)].sum()) if "asset_class" in rows.columns else 0.0
            cap = min(max_pos - weights.at[idx], class_cap - class_used)
            if cap <= eps:
                active.remove(idx)
            else:
                capacities[idx] = cap
        if not capacities:
            break
        pref_sum = float(prefs.loc[list(capacities.keys())].sum())
        if pref_sum <= eps:
            pref_share = pd.Series(1.0 / len(capacities), index=list(capacities.keys()))
        else:
            pref_share = prefs.loc[list(capacities.keys())] / pref_sum
        added = 0.0
        saturated = []
        for idx, cap in capacities.items():
            want = remaining * float(pref_share.at[idx])
            asset_class = str(rows.at[idx, "asset_class"]) if "asset_class" in rows.columns else ""
            class_cap = float(asset_caps.get(asset_class, 1.0))
            class_used_now = float(weights[rows["asset_class"].astype(str).eq(asset_class)].sum()) if "asset_class" in rows.columns else 0.0
            cap_now = max(0.0, min(cap, class_cap - class_used_now))
            add = min(want, cap_now)
            weights.at[idx] += add
            added += add
            if cap_now - add <= eps:
                saturated.append(idx)
        remaining -= added
        for idx in saturated:
            active.discard(idx)
        if added <= eps:
            break
    return weights


def _apply_weak_defensive_floor(
    rows: pd.DataFrame,
    weights: pd.Series,
    preferences: pd.Series,
    max_position_pct: float,
    asset_caps: Dict[str, float],
    defensive_assets: set,
    floor_pct: float,
) -> pd.Series:
    floor = max(0.0, min(1.0, float(floor_pct)))
    if floor <= 0 or rows.empty or "asset_class" not in rows.columns:
        return weights
    defensive_mask = rows["asset_class"].astype(str).isin(defensive_assets)
    if not bool(defensive_mask.any()):
        return weights
    current = float(weights[defensive_mask].sum())
    if current >= floor:
        return weights
    nondef_mask = ~defensive_mask
    available = float(weights[nondef_mask].sum())
    if available <= 0:
        return weights
    deficit = min(floor - current, available)
    out = weights.copy()
    out.loc[nondef_mask] -= out.loc[nondef_mask] / available * deficit
    defensive_rows = rows[defensive_mask].copy()
    add = _distribute_with_caps(
        defensive_rows,
        preferences.loc[defensive_rows.index],
        current + deficit,
        max_position_pct,
        asset_caps,
    )
    out.loc[defensive_rows.index] = add
    return out.clip(lower=0.0)


def select_rotation_positions(
    candidates: pd.DataFrame,
    hist_map: Dict[str, pd.DataFrame],
    cfg: Dict[str, Any],
    account: float,
    regime: Optional[Dict[str, Any]] = None,
    as_of: Optional[pd.Timestamp] = None,
    current_weights: Optional[Dict[str, float]] = None,
    cooldown_until: Optional[Dict[str, pd.Timestamp]] = None,
) -> pd.DataFrame:
    if candidates is None or candidates.empty:
        return pd.DataFrame()
    etf_cfg = cfg.get("etf", {})
    rot_cfg = etf_cfg.get("rotation", {})
    model = str(rot_cfg.get("model", "balanced"))
    regime = regime or market_regime_from_candidates(candidates, cfg)
    rel_cfg = rot_cfg.get("relative_momentum", {}) if model == "relative_momentum" else {}
    max_positions = int(rel_cfg.get("top_n", rot_cfg.get("max_positions", etf_cfg.get("max_positions", 5))))
    max_per_category = int(rot_cfg.get("max_per_category", 2))
    max_position_pct = float(rot_cfg.get("max_position_pct", etf_cfg.get("max_position_pct", 0.25)))
    min_lot = int(etf_cfg.get("min_lot", 100))
    max_corr = float(rot_cfg.get("max_correlation", 0.92))
    corr_lookback = int(rot_cfg.get("correlation_lookback", 120))
    weak_defensive_bonus = float(rot_cfg.get("weak_defensive_bonus", 15.0))
    defensive_assets = set(rot_cfg.get("defensive_asset_classes", ["defensive", "commodity"]))
    target_exposure = float(regime.get("target_exposure", rot_cfg.get("neutral_total_exposure", 0.65)))
    regime_name = str(regime.get("regime", "neutral"))
    asset_caps = _asset_class_caps(rot_cfg, regime_name)

    pool = candidates[candidates["is_rotation_candidate"].fillna(False)].copy()
    if pool.empty:
        return pd.DataFrame()
    if model == "relative_momentum" and not bool(rel_cfg.get("allow_defensive", False)):
        pool = pool[~pool["asset_class"].astype(str).isin(defensive_assets)].copy()
        if pool.empty:
            return pd.DataFrame()
    current_codes = {
        normalize_code(code)
        for code, weight in (current_weights or {}).items()
        if normalize_code(code) and safe_float(weight, 0.0) > 0
    }
    if model == "relative_momentum" and int(rel_cfg.get("cooldown_days", 0) or 0) > 0 and cooldown_until:
        effective_as_of = pd.Timestamp(as_of).normalize() if as_of is not None else pd.Timestamp(now_cn().date())
        blocked_codes = set()
        for code, until in cooldown_until.items():
            code = normalize_code(code)
            until_ts = pd.to_datetime(until, errors="coerce")
            if code and pd.notna(until_ts) and code not in current_codes and effective_as_of <= pd.Timestamp(until_ts).normalize():
                blocked_codes.add(code)
        if blocked_codes:
            pool = pool[~pool["code"].map(normalize_code).isin(blocked_codes)].copy()
            if pool.empty:
                return pd.DataFrame()
    score_col = "momentum_signal" if model == "relative_momentum" and "momentum_signal" in pool.columns else "rotation_score"
    pool["selection_score"] = pd.to_numeric(pool[score_col], errors="coerce").fillna(0.0)
    if model == "relative_momentum":
        turnover_cfg = rel_cfg.get("turnover_penalty", {})
        if bool(turnover_cfg.get("enabled", False)) and current_codes:
            min_advantage = float(turnover_cfg.get("min_score_advantage", 0.0) or 0.0)
            if min_advantage > 0:
                pool.loc[pool["code"].map(normalize_code).isin(current_codes), "selection_score"] += min_advantage
    if regime.get("regime") == "weak":
        pool.loc[pool["asset_class"].isin(defensive_assets), "selection_score"] += weak_defensive_bonus
    pool = pool.sort_values(["selection_score", "rotation_score"], ascending=[False, False])
    corr_hist_map = {code: _hist_until(hist, as_of) for code, hist in hist_map.items()} if as_of is not None else hist_map
    returns = close_return_frame(corr_hist_map)
    selected: List[pd.Series] = []
    selected_codes: List[str] = []
    category_counts: Dict[str, int] = {}
    asset_class_counts: Dict[str, int] = {}
    skip_notes: Dict[str, str] = {}

    core_broad_regimes = set(str(x) for x in rot_cfg.get("core_broad_regimes", []))
    core_broad_min_score = float(rot_cfg.get("core_broad_min_score", 55.0))
    if model != "relative_momentum" and regime_name in core_broad_regimes:
        broad_pool = pool[
            pool["asset_class"].astype(str).eq("broad")
            & (pd.to_numeric(pool["selection_score"], errors="coerce").fillna(0.0) >= core_broad_min_score)
        ].copy()
        if not broad_pool.empty:
            broad_row = broad_pool.sort_values(["selection_score", "rotation_score"], ascending=[False, False]).iloc[0]
            selected.append(broad_row)
            code = normalize_code(broad_row.get("code", ""))
            selected_codes.append(code)
            category = str(broad_row.get("category", "未分组") or "未分组")
            category_counts[category] = category_counts.get(category, 0) + 1
            asset_class = str(broad_row.get("asset_class", ""))
            asset_class_counts[asset_class] = asset_class_counts.get(asset_class, 0) + 1

    max_per_asset_class = int(rel_cfg.get("max_per_asset_class", 0) or 0)
    for _, row in pool.iterrows():
        code = normalize_code(row.get("code", ""))
        if code in selected_codes:
            continue
        category = str(row.get("category", "未分组") or "未分组")
        if category_counts.get(category, 0) >= max_per_category:
            skip_notes[code] = f"同类别{category}已达上限"
            continue
        asset_class = str(row.get("asset_class", ""))
        if max_per_asset_class > 0 and asset_class_counts.get(asset_class, 0) >= max_per_asset_class:
            skip_notes[code] = f"同资产类别{asset_class}已达上限"
            continue
        corr = _max_selected_corr(code, selected_codes, returns, corr_lookback)
        if corr > max_corr:
            skip_notes[code] = f"与已选ETF相关性{corr:.2f}高于{max_corr:.2f}"
            continue
        selected.append(row)
        selected_codes.append(code)
        category_counts[category] = category_counts.get(category, 0) + 1
        asset_class_counts[asset_class] = asset_class_counts.get(asset_class, 0) + 1
        if len(selected) >= max_positions:
            break
    if not selected:
        return pd.DataFrame()

    out = pd.DataFrame(selected).copy()
    if model == "relative_momentum" and str(rel_cfg.get("allocation", "equal_weight")) == "equal_weight":
        equal_weight = min(max_position_pct, target_exposure / max(1, len(out)))
        out["target_weight"] = equal_weight
    else:
        risk = pd.to_numeric(out.get("atr_pct"), errors="coerce").clip(lower=float(rot_cfg.get("min_risk_vol", 0.008)))
        score_power = float(rot_cfg.get("score_weight_power", 1.0))
        score_pref = (pd.to_numeric(out.get("selection_score", out.get("rotation_score")), errors="coerce").fillna(0.0).clip(lower=1.0) / 100.0) ** score_power
        preferences = score_pref / risk.replace(0, np.nan)
        out["target_weight"] = _distribute_with_caps(out, preferences, target_exposure, max_position_pct, asset_caps)
        if regime_name == "weak":
            out["target_weight"] = _apply_weak_defensive_floor(
                out,
                out["target_weight"],
                preferences,
                max_position_pct,
                asset_caps,
                defensive_assets,
                float(rot_cfg.get("weak_min_defensive_pct", 0.0)),
            )
    out["target_cash"] = out["target_weight"] * float(account)
    close = pd.to_numeric(out.get("close"), errors="coerce")
    shares = np.floor(out["target_cash"] / close / min_lot) * min_lot
    out["target_shares"] = shares.replace([np.inf, -np.inf], np.nan).fillna(0).astype(int)
    out["actual_weight_by_lot"] = np.where(account > 0, out["target_shares"] * close / float(account), 0.0)
    out["regime"] = regime_name
    out["regime_summary"] = regime.get("summary", "")
    out["allocation_note"] = f"目标总仓位{target_exposure:.0%}；单只上限{max_position_pct:.0%}"
    if model == "relative_momentum":
        turnover_cfg = rel_cfg.get("turnover_penalty", {})
        if bool(turnover_cfg.get("enabled", False)):
            out["allocation_note"] += f"；持仓保留阈值{float(turnover_cfg.get('min_score_advantage', 0.0) or 0.0):.1%}"
        cooldown_days = int(rel_cfg.get("cooldown_days", 0) or 0)
        if cooldown_days > 0:
            out["allocation_note"] += f"；卖出冷却{cooldown_days}个交易日"
    if asset_caps:
        out["allocation_note"] += "；资产类别上限 " + ",".join(f"{k}:{v:.0%}" for k, v in sorted(asset_caps.items()))
    return out.reset_index(drop=True)


def fetch_rotation_histories(pool: pd.DataFrame, cfg: Dict[str, Any], refresh: bool = False, limit: int = 0) -> Tuple[Dict[str, pd.DataFrame], List[Dict[str, Any]]]:
    etf_cfg = cfg.get("etf", {})
    start_date = etf_cfg.get("start_date") or cfg.get("data", {}).get("start_date") or "20200101"
    end_date = etf_cfg.get("end_date") or cfg.get("data", {}).get("end_date") or today_yyyymmdd()
    adjust = etf_cfg.get("adjust", "")
    fetcher = EtfFetcher(cfg, refresh=refresh)
    hist_map: Dict[str, pd.DataFrame] = {}
    errors: List[Dict[str, Any]] = []
    use_pool = pool.head(limit).copy() if limit and limit > 0 else pool
    total = len(use_pool)
    for n, (_, r) in enumerate(use_pool.iterrows(), start=1):
        code = normalize_code(r.get("code", ""))
        try:
            hist_map[code] = fetcher.etf_hist(code, start_date, end_date, adjust)
        except Exception as exc:
            errors.append({"code": code, "name": r.get("name", ""), "error": str(exc)})
        if n % 10 == 0 or n == total:
            print(f"[ETF轮动数据] 已获取 {n}/{total}")
    return hist_map, errors


def validate_history_coverage(pool: pd.DataFrame, hist_map: Dict[str, pd.DataFrame], cfg: Dict[str, Any], context: str) -> Tuple[int, int]:
    total = 0 if pool is None else len(pool)
    if total <= 0:
        raise RuntimeError(f"{context}失败：ETF池为空")
    success = 0
    for _, row in pool.iterrows():
        try:
            code = normalize_code(row.get("code", ""))
        except Exception:
            continue
        hist = hist_map.get(code)
        if hist is not None and not hist.empty:
            success += 1
    max_error_rate = float(cfg.get("etf", {}).get("max_error_rate_for_valid_run", 0.20))
    min_success = int(math.ceil(total * max(0.0, 1.0 - max_error_rate)))
    if success < min_success:
        raise RuntimeError(f"{context}失败：ETF历史数据成功率过低，成功 {success}/{total}，要求至少 {min_success}/{total}；不生成结果")
    return success, total


def to_rotation_chinese(df: pd.DataFrame) -> pd.DataFrame:
    mapping = {
        "date": "日期",
        "code": "ETF代码",
        "name": "ETF名称",
        "category": "类别/跟踪指数",
        "asset_class": "资产类别",
        "rotation_score": "轮动分",
        "selection_score": "选择分",
        "is_rotation_candidate": "是否轮动候选",
        "rotation_reason": "轮动依据",
        "filter_reason": "过滤原因",
        "allocation_note": "仓位约束说明",
        "regime": "市场状态",
        "target_weight": "目标仓位",
        "actual_weight_by_lot": "按整手实际仓位",
        "target_cash": "建议金额",
        "target_shares": "建议份额",
        "close": "收盘价",
        "pct_chg": "当日涨跌幅%",
        "ret20": "20日收益",
        "ret60": "60日收益",
        "ret120": "120日收益",
        "close_pos120": "120日区间位置",
        "drawdown120": "距120日高点",
        "amount_ma20": "20日均成交额",
        "atr_pct": "ATR波动率",
        "trend_component": "趋势分项",
        "momentum_component": "动量分项",
        "risk_component": "风险分项",
        "timing_component": "位置分项",
    }
    preferred = [
        "date", "code", "name", "category", "asset_class", "regime", "target_weight", "target_cash",
        "target_shares", "actual_weight_by_lot", "rotation_score", "selection_score", "is_rotation_candidate",
        "close", "pct_chg", "ret20", "ret60", "ret120", "close_pos120", "drawdown120", "amount_ma20",
        "atr_pct", "trend_component", "momentum_component", "risk_component", "timing_component",
        "rotation_reason", "allocation_note", "filter_reason",
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=[mapping.get(c, c) for c in preferred])
    cols = [c for c in preferred if c in df.columns] + [c for c in df.columns if c not in preferred]
    out = df[cols].copy().rename(columns=mapping)
    for c in ["目标仓位", "按整手实际仓位", "20日收益", "60日收益", "120日收益", "120日区间位置", "距120日高点", "ATR波动率"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.2%}" if pd.notna(x) else "")
    for c in ["轮动分", "选择分", "趋势分项", "动量分项", "风险分项", "位置分项"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.1f}" if pd.notna(x) else "")
    for c in ["收盘价"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.3f}" if pd.notna(x) else "")
    for c in ["建议金额", "20日均成交额"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:,.0f}" if pd.notna(x) else "")
    if "当日涨跌幅%" in out.columns:
        out["当日涨跌幅%"] = pd.to_numeric(out["当日涨跌幅%"], errors="coerce").map(lambda x: f"{x:.2f}" if pd.notna(x) else "")
    return out


def max_drawdown(series: pd.Series) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return np.nan
    peak = s.cummax()
    dd = s / peak - 1.0
    return float(dd.min())


def summarize_equity(equity: pd.DataFrame, turnover: float = 0.0) -> Dict[str, Any]:
    if equity.empty:
        return {}
    eq = pd.to_numeric(equity["equity"], errors="coerce").dropna()
    if len(eq) < 2:
        return {}
    daily = eq.pct_change().dropna()
    days = max(1, (pd.to_datetime(equity["date"].iloc[-1]) - pd.to_datetime(equity["date"].iloc[0])).days)
    total_return = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    annual_return = float((1.0 + total_return) ** (365.0 / days) - 1.0)
    annual_vol = float(daily.std() * np.sqrt(252)) if len(daily) > 1 else np.nan
    sharpe = float(annual_return / annual_vol) if np.isfinite(annual_vol) and annual_vol > 0 else np.nan
    return {
        "start_date": str(pd.to_datetime(equity["date"].iloc[0]).date()),
        "end_date": str(pd.to_datetime(equity["date"].iloc[-1]).date()),
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": max_drawdown(eq),
        "annual_volatility": annual_vol,
        "sharpe": sharpe,
        "turnover": turnover,
    }


def backtest_rotation(
    pool: pd.DataFrame,
    hist_map: Dict[str, pd.DataFrame],
    cfg: Dict[str, Any],
    account: float,
    years: int = 3,
    rebalance: str = "W-FRI",
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    etf_cfg = cfg.get("etf", {})
    bt_cfg = etf_cfg.get("backtest", {})
    commission_rate = float(bt_cfg.get("commission_rate", 0.0003))
    slippage_bps = float(bt_cfg.get("slippage_bps", 5.0))
    cost_rate = commission_rate + slippage_bps / 10000.0
    close_map = {}
    for code, hist in hist_map.items():
        h = _hist_until(hist, None)
        if h.empty:
            continue
        close_map[normalize_code(code)] = h.set_index("date")["close"].astype(float)
    closes = pd.DataFrame(close_map).sort_index().dropna(how="all")
    if closes.empty:
        return pd.DataFrame(), pd.DataFrame(), {}
    if years and years > 0:
        start_cut = closes.index.max() - pd.Timedelta(days=int(years * 365.25))
        closes = closes[closes.index >= start_cut]
    dates = list(closes.index)
    rot_cfg = etf_cfg.get("rotation", {})
    rel_cfg = rot_cfg.get("relative_momentum", {}) if str(rot_cfg.get("model", "balanced")) == "relative_momentum" else {}
    cooldown_days = int(rel_cfg.get("cooldown_days", 0) or 0)
    min_history_days = int(rot_cfg.get("min_history_days", etf_cfg.get("min_history_days", 180)))
    if len(dates) <= min_history_days + 2:
        return pd.DataFrame(), pd.DataFrame(), {"error": "可回测日期不足"}

    rebalance_dates = []
    for _, group in closes.groupby(pd.Grouper(freq=rebalance)):
        if not group.empty:
            rebalance_dates.append(group.index[-1])
    rebalance_set = set(rebalance_dates)
    equity = float(account)
    weights: Dict[str, float] = {}
    equity_rows = [{"date": dates[min_history_days], "equity": equity, "cash_weight": 1.0}]
    rebalance_rows: List[Dict[str, Any]] = []
    total_turnover = 0.0
    cooldown_until: Dict[str, pd.Timestamp] = {}

    for i in range(min_history_days, len(dates) - 1):
        date = dates[i]
        if date in rebalance_set:
            cooldown_until = {
                code: until for code, until in cooldown_until.items()
                if pd.Timestamp(until).normalize() >= pd.Timestamp(date).normalize()
            }
            candidates = compute_rotation_candidates(pool, hist_map, cfg, as_of=date)
            regime = market_regime_from_candidates(candidates, cfg)
            positions = select_rotation_positions(
                candidates,
                hist_map,
                cfg,
                account=equity,
                regime=regime,
                as_of=date,
                current_weights=weights,
                cooldown_until=cooldown_until,
            )
            new_weights = {normalize_code(r["code"]): float(r["target_weight"]) for _, r in positions.iterrows()} if not positions.empty else {}
            codes = set(weights) | set(new_weights)
            turnover = sum(abs(new_weights.get(c, 0.0) - weights.get(c, 0.0)) for c in codes)
            if turnover > 0:
                equity *= max(0.0, 1.0 - turnover * cost_rate)
                total_turnover += turnover
            if cooldown_days > 0:
                expiry_idx = min(i + cooldown_days, len(dates) - 1)
                expiry = pd.Timestamp(dates[expiry_idx]).normalize()
                for code in set(weights) - set(new_weights):
                    cooldown_until[code] = expiry
            weights = new_weights
            for _, r in positions.iterrows():
                rebalance_rows.append({
                    "date": date,
                    "code": r.get("code"),
                    "name": r.get("name"),
                    "category": r.get("category"),
                    "regime": regime.get("regime"),
                    "target_weight": r.get("target_weight"),
                    "rotation_score": r.get("rotation_score"),
                    "selection_score": r.get("selection_score"),
                })

        next_date = dates[i + 1]
        daily_ret = 0.0
        for code, weight in weights.items():
            if code not in closes.columns:
                continue
            c0 = safe_float(closes.at[date, code])
            c1 = safe_float(closes.at[next_date, code])
            if np.isfinite(c0) and c0 > 0 and np.isfinite(c1):
                daily_ret += weight * (c1 / c0 - 1.0)
        equity *= (1.0 + daily_ret)
        equity_rows.append({"date": next_date, "equity": equity, "cash_weight": max(0.0, 1.0 - sum(weights.values()))})

    equity_df = pd.DataFrame(equity_rows)
    benchmark_code = str(bt_cfg.get("benchmark_code") or "")
    if not benchmark_code:
        broad = pool[pool.apply(lambda r: classify_asset_class(r.get("name", ""), r.get("category", "")) == "broad", axis=1)]
        benchmark_code = normalize_code(broad.iloc[0]["code"] if not broad.empty else pool.iloc[0]["code"])
    summary_benchmark_code = normalize_code(benchmark_code)
    if benchmark_code in closes.columns and not equity_df.empty:
        bench = closes[benchmark_code].reindex(pd.to_datetime(equity_df["date"]))
        bench = bench.ffill()
        if bench.notna().any() and bench.dropna().iloc[0] > 0:
            equity_df["benchmark_equity"] = (float(account) * bench / bench.dropna().iloc[0]).to_numpy()
    summary = summarize_equity(equity_df, total_turnover)
    summary["benchmark_code"] = summary_benchmark_code
    if "benchmark_equity" in equity_df.columns:
        bench_summary = summarize_equity(
            equity_df[["date", "benchmark_equity"]].rename(columns={"benchmark_equity": "equity"}),
            0.0,
        )
        summary["benchmark_total_return"] = bench_summary.get("total_return", np.nan)
        summary["benchmark_annual_return"] = bench_summary.get("annual_return", np.nan)
        summary["benchmark_max_drawdown"] = bench_summary.get("max_drawdown", np.nan)
        summary["benchmark_annual_volatility"] = bench_summary.get("annual_volatility", np.nan)
        summary["benchmark_sharpe"] = bench_summary.get("sharpe", np.nan)
        summary["excess_total_return"] = safe_float(summary.get("total_return"), np.nan) - safe_float(summary.get("benchmark_total_return"), np.nan)
        summary["excess_annual_return"] = safe_float(summary.get("annual_return"), np.nan) - safe_float(summary.get("benchmark_annual_return"), np.nan)
        summary["excess_sharpe"] = safe_float(summary.get("sharpe"), np.nan) - safe_float(summary.get("benchmark_sharpe"), np.nan)
    summary["rebalance_count"] = len(pd.DataFrame(rebalance_rows)["date"].drop_duplicates()) if rebalance_rows else 0
    summary["avg_turnover_per_rebalance"] = total_turnover / max(1, summary["rebalance_count"])
    return equity_df, pd.DataFrame(rebalance_rows), summary


def format_rotation_message(positions: pd.DataFrame, candidates: pd.DataFrame, regime: Dict[str, Any]) -> str:
    lines = [f"ETF轮动配置 {now_cn().strftime('%Y-%m-%d %H:%M')}"]
    lines.append(str(regime.get("summary", "")))
    lines.append(f"目标总仓位：{float(regime.get('target_exposure', 0.0)):.0%}")
    lines.append("")
    if positions is None or positions.empty:
        lines.append("当前无ETF轮动配置。")
    else:
        total_weight = float(pd.to_numeric(positions.get("target_weight"), errors="coerce").fillna(0.0).sum())
        lines.append(f"本期配置 {len(positions)} 只，合计仓位约 {total_weight:.0%}：")
        for _, r in positions.iterrows():
            lines.append(
                f"- {r.get('code')} {r.get('name')}：仓位{safe_float(r.get('target_weight'), 0):.1%}，"
                f"轮动分{safe_float(r.get('rotation_score'), 0):.1f}，{r.get('rotation_reason', '')}"
            )
        note = str(positions.iloc[0].get("allocation_note", "")).strip()
        if note:
            lines.append(f"约束：{note}")
    lines.append("")
    lines.append("已生成：latest_etf_rotation_positions.csv、latest_etf_rotation_candidates.csv、latest_etf_rotation_report.md。")
    return "\n".join(lines)


def write_rotation_outputs(out_dir: Path, positions: pd.DataFrame, candidates: pd.DataFrame, regime: Dict[str, Any], errors: List[Dict[str, Any]]) -> Path:
    run_date = now_cn().strftime("%Y%m%d_%H%M%S")
    to_rotation_chinese(positions).to_csv(out_dir / f"etf_rotation_positions_{run_date}.csv", index=False, encoding="utf-8-sig")
    to_rotation_chinese(candidates).to_csv(out_dir / f"etf_rotation_candidates_{run_date}.csv", index=False, encoding="utf-8-sig")
    to_rotation_chinese(positions).to_csv(out_dir / "latest_etf_rotation_positions.csv", index=False, encoding="utf-8-sig")
    to_rotation_chinese(candidates).to_csv(out_dir / "latest_etf_rotation_candidates.csv", index=False, encoding="utf-8-sig")
    positions.to_csv(out_dir / "latest_etf_rotation_positions_raw.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(out_dir / "latest_etf_rotation_candidates_raw.csv", index=False, encoding="utf-8-sig")
    write_or_clear_error_csv(out_dir / "latest_etf_rotation_errors.csv", errors)
    msg = format_rotation_message(positions, candidates, regime)
    report = "# ETF轮动配置报告\n\n" + msg + "\n"
    (out_dir / f"etf_rotation_report_{run_date}.md").write_text(report, encoding="utf-8")
    (out_dir / "latest_etf_rotation_report.md").write_text(report, encoding="utf-8")
    msg_path = out_dir / "latest_etf_rotation_message.txt"
    msg_path.write_text(msg, encoding="utf-8")
    return msg_path


def run_rotation(args: argparse.Namespace) -> Tuple[pd.DataFrame, pd.DataFrame, Path]:
    cfg = load_config(args.config)
    etf_cfg = cfg.setdefault("etf", {})
    pool_path = args.pool or etf_cfg.get("pool", "etf_pool.csv")
    out_dir = ensure_dir(args.out or etf_cfg.get("out_dir", "etf_output"))
    pool = read_etf_pool(pool_path)
    hist_map, errors = fetch_rotation_histories(pool, cfg, refresh=bool(args.refresh), limit=int(args.limit or 0))
    if args.limit and args.limit > 0:
        pool = pool.head(int(args.limit)).copy()
    write_or_clear_error_csv(out_dir / "latest_etf_rotation_errors.csv", errors)
    validate_history_coverage(pool, hist_map, cfg, "ETF轮动配置")
    candidates = compute_rotation_candidates(pool, hist_map, cfg)
    regime = market_regime_from_candidates(candidates, cfg)
    state_path = _rotation_state_path(out_dir, cfg)
    state = _read_rotation_state(state_path)
    as_of = _latest_candidate_date(candidates)
    rot_cfg = cfg.get("etf", {}).get("rotation", {})
    rel_cfg = rot_cfg.get("relative_momentum", {}) if str(rot_cfg.get("model", "balanced")) == "relative_momentum" else {}
    cooldown_map = _state_cooldown_until(state) if int(rel_cfg.get("cooldown_days", 0) or 0) > 0 else {}
    positions = select_rotation_positions(
        candidates,
        hist_map,
        cfg,
        account=float(args.account),
        regime=regime,
        as_of=as_of,
        current_weights=_state_current_weights(state),
        cooldown_until=cooldown_map,
    )
    if str(rot_cfg.get("model", "balanced")) == "relative_momentum":
        _write_rotation_state(state_path, state, positions, rel_cfg, as_of)
    msg_path = write_rotation_outputs(out_dir, positions, candidates, regime, errors)
    return positions, candidates, msg_path


def write_backtest_outputs(out_dir: Path, equity: pd.DataFrame, rebalances: pd.DataFrame, summary: Dict[str, Any]) -> Path:
    run_date = now_cn().strftime("%Y%m%d_%H%M%S")
    equity.to_csv(out_dir / f"etf_rotation_backtest_equity_{run_date}.csv", index=False, encoding="utf-8-sig")
    rebalances.to_csv(out_dir / f"etf_rotation_backtest_rebalances_{run_date}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([summary]).to_csv(out_dir / f"etf_rotation_backtest_summary_{run_date}.csv", index=False, encoding="utf-8-sig")
    equity.to_csv(out_dir / "latest_etf_rotation_backtest_equity.csv", index=False, encoding="utf-8-sig")
    rebalances.to_csv(out_dir / "latest_etf_rotation_backtest_rebalances.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([summary]).to_csv(out_dir / "latest_etf_rotation_backtest_summary.csv", index=False, encoding="utf-8-sig")
    lines = [f"# ETF轮动回测报告 {now_cn().strftime('%Y-%m-%d %H:%M')}", ""]
    if summary:
        lines.append(f"- 区间：{summary.get('start_date')} 至 {summary.get('end_date')}")
        lines.append(f"- 总收益：{safe_float(summary.get('total_return'), 0):.2%}")
        lines.append(f"- 年化收益：{safe_float(summary.get('annual_return'), 0):.2%}")
        lines.append(f"- 最大回撤：{safe_float(summary.get('max_drawdown'), 0):.2%}")
        lines.append(f"- 年化波动：{safe_float(summary.get('annual_volatility'), 0):.2%}")
        lines.append(f"- 夏普：{safe_float(summary.get('sharpe'), np.nan):.2f}")
        if "benchmark_total_return" in summary:
            lines.append(f"- 基准代码：{summary.get('benchmark_code', '')}")
            lines.append(f"- 基准总收益：{safe_float(summary.get('benchmark_total_return'), 0):.2%}")
            lines.append(f"- 基准年化收益：{safe_float(summary.get('benchmark_annual_return'), 0):.2%}")
            lines.append(f"- 基准最大回撤：{safe_float(summary.get('benchmark_max_drawdown'), 0):.2%}")
            lines.append(f"- 基准夏普：{safe_float(summary.get('benchmark_sharpe'), np.nan):.2f}")
            lines.append(f"- 超额总收益：{safe_float(summary.get('excess_total_return'), 0):.2%}")
            lines.append(f"- 超额年化收益：{safe_float(summary.get('excess_annual_return'), 0):.2%}")
        lines.append(f"- 再平衡次数：{summary.get('rebalance_count', 0)}")
    else:
        lines.append("回测没有生成有效结果。")
    path = out_dir / "latest_etf_rotation_backtest_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (out_dir / f"etf_rotation_backtest_report_{run_date}.md").write_text("\n".join(lines), encoding="utf-8")
    return path


def run_backtest(args: argparse.Namespace) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any], Path]:
    cfg = load_config(args.config)
    etf_cfg = cfg.setdefault("etf", {})
    pool_path = args.pool or etf_cfg.get("pool", "etf_pool.csv")
    out_dir = ensure_dir(args.out or etf_cfg.get("out_dir", "etf_output"))
    pool = read_etf_pool(pool_path)
    hist_map, errors = fetch_rotation_histories(pool, cfg, refresh=bool(args.refresh), limit=int(args.limit or 0))
    if args.limit and args.limit > 0:
        pool = pool.head(int(args.limit)).copy()
    write_or_clear_error_csv(out_dir / "latest_etf_rotation_backtest_errors.csv", errors)
    validate_history_coverage(pool, hist_map, cfg, "ETF轮动回测")
    equity, rebalances, summary = backtest_rotation(
        pool,
        hist_map,
        cfg,
        account=float(args.account),
        years=int(args.years),
        rebalance=str(args.rebalance),
    )
    report_path = write_backtest_outputs(out_dir, equity, rebalances, summary)
    return equity, rebalances, summary, report_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="A股ETF轮动配置与回测")
    p.add_argument("--mode", choices=["rotate", "backtest"], default="rotate", help="rotate=生成当期轮动配置；backtest=回测轮动策略")
    p.add_argument("--pool", default="", help="ETF池文件，默认读取配置 etf.pool")
    p.add_argument("--config", default="", help="配置文件 YAML/JSON，可选")
    p.add_argument("--out", default="", help="输出目录，默认读取配置 etf.out_dir")
    p.add_argument("--account", type=float, default=100000.0, help="账户权益/回测初始资金")
    p.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新拉取ETF数据")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 只，测试用")
    p.add_argument("--years", type=int, default=3, help="回测最近 N 年，默认 3")
    p.add_argument("--rebalance", default="W-FRI", help="再平衡频率，默认 W-FRI")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.mode == "backtest":
        _, _, summary, report_path = run_backtest(args)
        print(f"[ETF轮动回测] {report_path}")
        if summary:
            print(f"总收益 {safe_float(summary.get('total_return'), 0):.2%}，最大回撤 {safe_float(summary.get('max_drawdown'), 0):.2%}")
    else:
        _, _, msg_path = run_rotation(args)
        print(Path(msg_path).read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
