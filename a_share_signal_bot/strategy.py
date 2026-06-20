# -*- coding: utf-8 -*-
from __future__ import annotations

from .sectors import *

def detect_volume_anchor_setup(ind: pd.DataFrame, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """识别“天量锚点后缩量再异动”。这是资金事件模型，不等同普通突破。"""
    setup_cfg = cfg.get("strategy", {}).get("setup", {})
    if ind is None or ind.empty or len(ind) < 80:
        return {"anchor_valid": False, "anchor_reason": "K线不足", "anchor_score": 0.0}
    lookback = int(setup_cfg.get("volume_anchor_lookback", 20))
    min_days = int(setup_cfg.get("volume_anchor_min_days_ago", 3))
    max_days = int(setup_cfg.get("volume_anchor_max_days_ago", 20))
    min_ratio = float(setup_cfg.get("volume_anchor_amount_ratio", 3.0))
    min_pct = float(setup_cfg.get("volume_anchor_min_pct", 7.0))
    n = len(ind)
    candidates: List[Tuple[int, float]] = []
    for i in range(max(0, n - max_days - 1), max(0, n - min_days)):
        row = ind.iloc[i]
        pct = safe_float(row.get("pct_chg"))
        ar = safe_float(row.get("amount_ratio20"))
        rank120 = safe_float(row.get("amount_ratio120_rank"))
        cs = safe_float(row.get("candle_strength"))
        if (np.isfinite(pct) and pct >= min_pct) and (np.isfinite(ar) and ar >= min_ratio or np.isfinite(rank120) and rank120 >= 0.95) and cs > 0.35:
            # 越近、量越大、K线越强，越优先作为锚点
            candidates.append((i, (ar if np.isfinite(ar) else 2.0) + (rank120 if np.isfinite(rank120) else 0.0)))
    if not candidates:
        return {"anchor_valid": False, "anchor_reason": "近20日无天量涨停/大阳锚点", "anchor_score": 0.0}
    anchor_i = sorted(candidates, key=lambda x: x[1], reverse=True)[0][0]
    anchor = ind.iloc[anchor_i]
    last = ind.iloc[-1]
    after = ind.iloc[anchor_i + 1:]
    if after.empty:
        return {"anchor_valid": False, "anchor_reason": "锚点后观察期不足", "anchor_score": 0.0}
    a_low = safe_float(anchor.get("low"))
    a_high = safe_float(anchor.get("high"))
    a_open = safe_float(anchor.get("open"))
    a_close = safe_float(anchor.get("close"))
    a_mid = min(a_open, a_close) + (max(a_open, a_close) - min(a_open, a_close)) * float(setup_cfg.get("volume_anchor_midline_pct", 0.50))
    hold_low_pct = float(setup_cfg.get("volume_anchor_hold_low_pct", -0.03))
    min_after_low = safe_float(after["low"].min())
    close = safe_float(last.get("close"))
    ma5 = safe_float(last.get("ma5"))
    ma10 = safe_float(last.get("ma10"))
    amount_ratio20 = safe_float(last.get("amount_ratio20"))
    amount_dryup20 = safe_float(last.get("amount_dryup20"))
    high_since_anchor = safe_float(after.iloc[:-1]["high"].max()) if len(after) > 1 else np.nan
    reasons: List[str] = []
    blockers: List[str] = []
    score = 0.0
    days_ago = n - 1 - anchor_i
    if np.isfinite(min_after_low) and np.isfinite(a_low) and min_after_low >= a_low * (1.0 + hold_low_pct):
        score += 18; reasons.append("未有效跌破天量锚点低点")
    else:
        blockers.append("跌破天量锚点低点，锚点资金疑似失效")
    if np.isfinite(close) and np.isfinite(a_mid) and close >= a_mid:
        score += 10; reasons.append("收盘守住锚点实体半分位")
    else:
        blockers.append("未守住锚点实体半分位")
    if np.isfinite(amount_dryup20) and amount_dryup20 <= 0.90:
        score += 12; reasons.append("锚点后缩量调整")
    if np.isfinite(amount_ratio20) and amount_ratio20 >= float(setup_cfg.get("reaccum_amount_min", 1.25)):
        score += 15; reasons.append("今日重新放量异动")
    else:
        blockers.append("今日尚未重新放量")
    if np.isfinite(high_since_anchor) and np.isfinite(close) and close >= high_since_anchor * 0.995:
        score += 18; reasons.append("突破锚点后整理区间高点")
    elif np.isfinite(a_high) and np.isfinite(close) and close >= a_high * 0.985:
        score += 10; reasons.append("接近天量锚点高点")
    if np.isfinite(close) and ((np.isfinite(ma5) and close > ma5) or (np.isfinite(ma10) and close > ma10)):
        score += 12; reasons.append("重新站上5/10日线")
    else:
        blockers.append("未重新站上5/10日线")
    if 3 <= days_ago <= 20:
        score += 5
    valid = score >= float(setup_cfg.get("anchor_score_threshold", 72.0)) and len([b for b in blockers if "跌破" in b or "未重新" in b]) == 0
    return {
        "anchor_valid": bool(valid),
        "anchor_score": round(min(score, 100.0), 2),
        "anchor_date": anchor.get("date").strftime("%Y-%m-%d") if hasattr(anchor.get("date"), "strftime") else str(anchor.get("date", "")),
        "anchor_days_ago": days_ago,
        "anchor_reason": "；".join(unique_nonempty(reasons)),
        "anchor_blockers": "；".join(unique_nonempty(blockers)),
        "anchor_low": a_low,
        "anchor_high": a_high,
    }


def compute_sector_context(metrics: pd.DataFrame, cfg: Dict[str, Any], fetcher: Optional[AkshareFetcher] = None) -> pd.DataFrame:
    """v6.3 板块主线：优先用对应板块指数日K计算主线强度；失败时回退到股票池内部均值。"""
    if metrics is None or metrics.empty:
        return metrics
    df = metrics.copy()
    if "sector" not in df.columns:
        df["sector"] = "未分组"
    df["sector"] = df["sector"].apply(normalize_sector_value).replace("", "未分组")
    for c in ["ret5", "ret10", "ret20", "ret60", "ret120", "amount_ratio20", "close_pos60", "pct_chg"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    sector_cfg = cfg.get("strategy", {}).get("sector", {})
    g = df.groupby("sector", dropna=False)
    pool_sector = pd.DataFrame({
        "sector": list(g.groups.keys()),
        "sector_size": g["code"].count().values,
        "pool_sector_ret20": g["ret20"].mean().values,
        "pool_sector_ret60": g["ret60"].mean().values,
        "pool_sector_ret5": g["ret5"].mean().values,
        "sector_up_rate20": g["ret20"].apply(lambda x: float((pd.to_numeric(x, errors="coerce") > 0).mean())).values,
        "sector_high_pos_rate": g["close_pos60"].apply(lambda x: float((pd.to_numeric(x, errors="coerce") >= 0.65).mean())).values,
        "pool_sector_amount_ratio20": g["amount_ratio20"].median().values,
    })

    board_rows: List[Dict[str, Any]] = []
    use_board = bool(sector_cfg.get("use_board_hist_strength", True)) and fetcher is not None
    source_kind = normalize_board_source(sector_cfg.get("auto_source", "industry") or "industry")
    if source_kind == "concept_first":
        fallback_sources = sector_cfg.get("auto_fallback_sources", ["industry", "sw"])
        if isinstance(fallback_sources, str):
            fallback_sources = [x.strip() for x in fallback_sources.split(",") if x.strip()]
        source_candidates = ["concept"] + [normalize_board_source(x) for x in fallback_sources]
    else:
        source_candidates = [source_kind]
    source_candidates = [x for x in source_candidates if x in {"concept", "industry", "em_concept", "sw"}]
    if not source_candidates:
        source_kind = "industry"
        source_candidates = ["industry"]
    if use_board:
        start_date = cfg.get("data", {}).get("start_date") or "20220101"
        end_date = cfg.get("data", {}).get("end_date") or today_yyyymmdd()
        adjust = ""
        for sec in pool_sector["sector"].astype(str).tolist():
            if not sec or sec == "未分组":
                continue
            try:
                hist = pd.DataFrame()
                hist_kind = ""
                last_exc: Optional[Exception] = None
                for candidate_kind in source_candidates:
                    try:
                        hist = fetcher.board_hist(candidate_kind, sec, start_date, end_date, adjust=adjust)
                        hist_kind = candidate_kind
                        break
                    except Exception as exc:
                        last_exc = exc
                        continue
                if hist.empty:
                    if last_exc:
                        raise last_exc
                    raise ValueError("板块日K为空")
                ind = add_indicators(hist, atr_period=int(cfg.get("strategy", {}).get("atr_period", 14)))
                if len(ind) < 80:
                    raise ValueError("板块日K不足")
                last = ind.iloc[-1]
                close = safe_float(last.get("close"))
                ma5 = safe_float(last.get("ma5")); ma10 = safe_float(last.get("ma10")); ma20 = safe_float(last.get("ma20")); ma60 = safe_float(last.get("ma60")); ma120 = safe_float(last.get("ma120"))
                board_rows.append({
                    "sector": sec,
                    "board_ret5": safe_float(last.get("ret5")),
                    "board_ret10": safe_float(last.get("ret10")),
                    "board_ret20": safe_float(last.get("ret20")),
                    "board_ret60": safe_float(last.get("ret60")),
                    "board_ret120": safe_float(last.get("ret120")),
                    "board_close_pos60": safe_float(last.get("close_pos60")),
                    "board_close_pos120": safe_float(last.get("close_pos120")),
                    "board_amount_ratio20": safe_float(last.get("amount_ratio20")),
                    "board_amount_dryup20": safe_float(last.get("amount_dryup20")),
                    "board_reg_slope20": safe_float(last.get("reg_slope20")),
                    "board_reg_r2_20": safe_float(last.get("reg_r2_20")),
                    "board_ma5_gt10_gt20": bool(np.isfinite(ma5) and np.isfinite(ma10) and np.isfinite(ma20) and close > ma5 >= ma10 >= ma20),
                    "board_ma20_gt60": bool(np.isfinite(ma20) and np.isfinite(ma60) and close > ma20 and ma20 > ma60),
                    "board_ma60_gt120": bool(np.isfinite(ma60) and np.isfinite(ma120) and ma60 > ma120),
                    "sector_strength_source": f"{hist_kind}_board_hist",
                })
            except Exception:
                continue

    board = pd.DataFrame(board_rows)
    sector = pool_sector.merge(board, on="sector", how="left") if not board.empty else pool_sector.copy()
    for c in ["board_ret5", "board_ret10", "board_ret20", "board_ret60", "board_ret120", "board_close_pos60", "board_close_pos120", "board_amount_ratio20", "board_reg_slope20", "board_reg_r2_20"]:
        if c not in sector.columns:
            sector[c] = np.nan
        sector[c] = pd.to_numeric(sector[c], errors="coerce")

    sector["sector_ret5"] = sector["board_ret5"].fillna(sector["pool_sector_ret5"])
    sector["sector_ret20"] = sector["board_ret20"].fillna(sector["pool_sector_ret20"])
    sector["sector_ret60"] = sector["board_ret60"].fillna(sector["pool_sector_ret60"])
    sector["sector_amount_ratio20"] = sector["board_amount_ratio20"].fillna(sector["pool_sector_amount_ratio20"])
    if "sector_strength_source" not in sector.columns:
        sector["sector_strength_source"] = "pool_internal"
    sector["sector_strength_source"] = sector["sector_strength_source"].fillna("pool_internal")

    sector["sector_rs_rank5"] = pd.to_numeric(sector["sector_ret5"], errors="coerce").rank(pct=True).fillna(0.0)
    sector["sector_rs_rank20"] = pd.to_numeric(sector["sector_ret20"], errors="coerce").rank(pct=True).fillna(0.0)
    sector["sector_rs_rank60"] = pd.to_numeric(sector["sector_ret60"], errors="coerce").rank(pct=True).fillna(0.0)
    score = 0.0
    score = score + 18 * sector["sector_rs_rank5"].fillna(0)
    score = score + 24 * sector["sector_rs_rank20"].fillna(0)
    score = score + 16 * sector["sector_rs_rank60"].fillna(0)
    score = score + 12 * sector["sector_up_rate20"].fillna(0)
    score = score + 10 * sector["sector_high_pos_rate"].fillna(0)
    board_trend = (
        sector.get("board_ma5_gt10_gt20", pd.Series(False, index=sector.index)).fillna(False).astype(bool).astype(float) * 8
        + sector.get("board_ma20_gt60", pd.Series(False, index=sector.index)).fillna(False).astype(bool).astype(float) * 7
        + sector.get("board_ma60_gt120", pd.Series(False, index=sector.index)).fillna(False).astype(bool).astype(float) * 4
    )
    score = score + board_trend
    reg_bonus = np.where((sector["board_reg_slope20"] > 0) & (sector["board_reg_r2_20"] >= 0.20), 5, 0)
    score = score + reg_bonus
    ar = pd.to_numeric(sector["sector_amount_ratio20"], errors="coerce")
    score = score + np.where((ar >= 1.05) & (ar <= 2.2), 6, np.where((ar > 0.85) & (ar < 1.05), 3, np.where(ar > 2.8, -4, 0)))
    sector["sector_strength_score"] = pd.Series(score).clip(lower=0, upper=100).round(2)

    unclassified = sector["sector"].astype(str).eq("未分组")
    if bool(unclassified.any()):
        sector.loc[unclassified, "sector_strength_score"] = 0.0
        sector.loc[unclassified, "sector_strength_source"] = "unclassified"

    threshold = float(sector_cfg.get("mainline_score_threshold", 72.0))
    strong_threshold = float(sector_cfg.get("strong_score_threshold", 62.0))
    sector["sector_is_mainline"] = sector["sector_strength_score"] >= threshold
    sector["sector_is_strong"] = sector["sector_strength_score"] >= strong_threshold

    df = df.merge(sector, on="sector", how="left")
    df["stock_sector_rs20"] = df.groupby("sector")["ret20"].rank(pct=True).fillna(0.0)
    df["stock_sector_rs60"] = df.groupby("sector")["ret60"].rank(pct=True).fillna(0.0)
    df["outperform_sector20"] = df["ret20"] - df["sector_ret20"]
    df["outperform_sector60"] = df["ret60"] - df["sector_ret60"]
    return df


def classify_setup_row(r: pd.Series, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """v6.3 买点分型：先板块主线/个股前排门槛，再识别突破、回踩、天量锚点再异动。"""
    setup_cfg = cfg.get("strategy", {}).get("setup", {})
    sector_cfg = cfg.get("strategy", {}).get("sector", {})
    close = safe_float(r.get("close")); low = safe_float(r.get("low"))
    ma5 = safe_float(r.get("ma5")); ma10 = safe_float(r.get("ma10")); ma20 = safe_float(r.get("ma20")); ma60 = safe_float(r.get("ma60"))
    ret5 = safe_float(r.get("ret5")); amount_ratio20 = safe_float(r.get("amount_ratio20"))
    high20_prev = safe_float(r.get("high20_prev")); high60_prev = safe_float(r.get("high60_prev")); high120_prev = safe_float(r.get("high120_prev"))
    close_pos60 = safe_float(r.get("close_pos60")); close_pos120 = safe_float(r.get("close_pos120"))
    contraction = safe_float(r.get("range_contraction_20_60")); candle_strength = safe_float(r.get("candle_strength"))
    upper_shadow = safe_float(r.get("upper_shadow_pct"), 0.0); lower_shadow = safe_float(r.get("lower_shadow_pct"), 0.0)
    amount_dryup20 = safe_float(r.get("amount_dryup20")); risk_pct = safe_float(r.get("risk_pct"))
    sector_score = safe_float(r.get("sector_strength_score"), 0.0)
    sector_mainline = bool(r.get("sector_is_mainline", False)); sector_strong = bool(r.get("sector_is_strong", False))
    stock_sector_rs20 = safe_float(r.get("stock_sector_rs20"), 0.0); stock_sector_rs60 = safe_float(r.get("stock_sector_rs60"), 0.0)
    outperform_sector20 = safe_float(r.get("outperform_sector20"), 0.0)

    front_min20 = float(sector_cfg.get("front_rs20_min", 0.60)); front_min60 = float(sector_cfg.get("front_rs60_min", 0.55))
    require_outperform = bool(sector_cfg.get("require_outperform_sector20", True))
    common_blocks: List[str] = []
    if bool(sector_cfg.get("hard_gate_enabled", True)) and sector_score < float(sector_cfg.get("min_score_for_any_buy", 60.0)):
        common_blocks.append(f"板块主线分{sector_score:.1f}低于入场门槛")
    if stock_sector_rs20 < front_min20 and stock_sector_rs60 < front_min60:
        common_blocks.append("个股不是板块前排")
    if require_outperform and np.isfinite(outperform_sector20) and outperform_sector20 < -0.02:
        common_blocks.append("20日明显跑输所属板块")
    candidates: List[Dict[str, Any]] = []

    # A. 平台/阶段突破：强板块 + 前排 + 平台收敛 + 温和放量 + 收盘强。
    b_score = 0.0; b_reasons: List[str] = []; b_blocks: List[str] = list(common_blocks)
    if sector_score >= float(sector_cfg.get("min_score_for_breakout", 64.0)):
        b_score += 15; b_reasons.append("板块强度允许做突破")
    else:
        b_blocks.append("板块强度不足，普通突破容易一日游")
    if stock_sector_rs20 >= front_min20 or stock_sector_rs60 >= 0.65:
        b_score += 14; b_reasons.append("个股在板块内前排")
    if require_outperform and np.isfinite(outperform_sector20) and outperform_sector20 >= 0:
        b_score += 8; b_reasons.append("20日跑赢所属板块")
    breakout_hit = False
    if np.isfinite(high20_prev) and close >= high20_prev * 0.995:
        breakout_hit = True; b_score += 16; b_reasons.append("突破/接近20日平台高点")
    if np.isfinite(high60_prev) and close >= high60_prev * 0.990:
        breakout_hit = True; b_score += 14; b_reasons.append("突破/接近60日平台高点")
    if np.isfinite(high120_prev) and close >= high120_prev * 0.990:
        b_score += 8; b_reasons.append("接近120日阶段高点")
    if not breakout_hit:
        b_blocks.append("没有有效突破20/60日平台高点")
    if np.isfinite(close_pos60) and close_pos60 >= 0.70:
        b_score += 8; b_reasons.append("价格处于60日强势区")
    if np.isfinite(close_pos120) and close_pos120 >= 0.60:
        b_score += 4
    if np.isfinite(contraction) and 0.28 <= contraction <= 0.92:
        b_score += 12; b_reasons.append("突破前波动收敛")
    elif bool(setup_cfg.get("breakout_need_contraction", True)):
        b_blocks.append("突破前没有明显平台收敛")
    if np.isfinite(amount_ratio20) and float(setup_cfg.get("breakout_amount_min", 1.20)) <= amount_ratio20 <= float(setup_cfg.get("breakout_amount_max", 2.60)):
        b_score += 14; b_reasons.append("突破量能温和放大")
    else:
        b_blocks.append("突破量能不在健康区间")
    if candle_strength > 0.25 and upper_shadow <= 0.065:
        b_score += 7; b_reasons.append("收盘强且上影不长")
    else:
        b_blocks.append("突破K线收盘强度不足或上影偏长")
    if np.isfinite(ret5) and ret5 > 0.18:
        b_score -= 10; b_blocks.append("近5日涨幅过大，疑似追涨末端")
    candidates.append({"setup_type": "breakout", "setup_score": round(max(0, min(100, b_score)), 2), "setup_reason": "；".join(unique_nonempty(b_reasons)), "setup_blockers": "；".join(unique_nonempty(b_blocks))})

    # B. 主线回踩：只在强主线/前排里做，强主线优先看 MA5/MA10，普通主线看 MA10/MA20。
    p_score = 0.0; p_reasons: List[str] = []; p_blocks: List[str] = list(common_blocks)
    if sector_score >= float(sector_cfg.get("min_score_for_pullback", 72.0)) and (sector_mainline or sector_strong):
        p_score += 20; p_reasons.append("板块主线强，允许做短均线回踩")
    else:
        p_blocks.append("板块不是强主线，不做回踩低吸")
    if stock_sector_rs20 >= float(sector_cfg.get("core_rs20_min", 0.75)) or (stock_sector_rs20 >= front_min20 and stock_sector_rs60 >= 0.65):
        p_score += 16; p_reasons.append("个股为板块核心/前排")
    else:
        p_blocks.append("个股板块内强度不足，不做回踩")
    if np.isfinite(ma5) and np.isfinite(ma10) and np.isfinite(ma20) and close >= ma5 >= ma10 >= ma20:
        p_score += 14; p_reasons.append("5/10/20日线强多头")
    elif np.isfinite(ma10) and np.isfinite(ma20) and np.isfinite(ma60) and close >= ma10 >= ma20 >= ma60:
        p_score += 9; p_reasons.append("10/20/60日线趋势保持")
    else:
        p_blocks.append("短中期均线结构不够强")
    mas = setup_cfg.get("strong_mainline_pullback_ma", [5, 10]) if sector_score >= 82 else setup_cfg.get("normal_mainline_pullback_ma", [10, 20])
    ma_map = {5: ma5, 10: ma10, 20: ma20}; dist = float(setup_cfg.get("pullback_distance_pct", 0.035))
    touched = False; reclaimed = False; line_name = ""
    for w in mas:
        m = ma_map.get(int(w), np.nan)
        if np.isfinite(m) and np.isfinite(close):
            touched_by_close = abs(close / m - 1.0) <= dist
            touched_by_low = np.isfinite(low) and low <= m * (1.0 + dist) and close >= m * 0.997
            if touched_by_close or touched_by_low:
                touched = True; line_name = f"MA{int(w)}"
            if close >= m:
                reclaimed = True
    if touched:
        p_score += 16; p_reasons.append(f"回踩{line_name}附近")
    else:
        p_blocks.append("没有回踩到关键均线附近")
    if reclaimed:
        p_score += 12; p_reasons.append("收盘收回关键均线")
    else:
        p_blocks.append("未收回关键均线")
    if np.isfinite(amount_dryup20) and amount_dryup20 <= float(setup_cfg.get("pullback_amount_max", 1.15)):
        p_score += 12; p_reasons.append("回踩过程缩量")
    elif bool(setup_cfg.get("pullback_need_dryup", True)):
        p_blocks.append("回踩没有缩量")
    if lower_shadow > upper_shadow and lower_shadow > 0.018:
        p_score += 5; p_reasons.append("下影承接")
    if np.isfinite(ret5) and -0.10 <= ret5 <= 0.08:
        p_score += 5
    if np.isfinite(risk_pct) and risk_pct <= 0.10:
        p_score += 5
    candidates.append({"setup_type": "pullback", "setup_score": round(max(0, min(100, p_score)), 2), "setup_reason": "；".join(unique_nonempty(p_reasons)), "setup_blockers": "；".join(unique_nonempty(p_blocks))})

    # C. 天量锚点再异动：锚点有效 + 主线未退潮 + 再次异动确认。
    a_score = safe_float(r.get("anchor_score"), 0.0)
    a_reasons = split_reason_text(str(r.get("anchor_reason", "")))
    a_blocks = split_reason_text(str(r.get("anchor_blockers", ""))) + list(common_blocks)
    if sector_score >= float(sector_cfg.get("min_score_for_anchor", 68.0)):
        a_score += 8; a_reasons.append("板块强度支持二次异动")
    else:
        a_blocks.append("板块强度不足，天量锚点二次异动不做")
    if stock_sector_rs20 >= front_min20:
        a_score += 8; a_reasons.append("个股板块内仍在前排")
    else:
        a_blocks.append("锚点后个股已掉出板块前排")
    candidates.append({"setup_type": "volume_anchor_reaccumulation", "setup_score": round(max(0, min(100, a_score)), 2), "setup_reason": "；".join(unique_nonempty(a_reasons)), "setup_blockers": "；".join(unique_nonempty(a_blocks))})

    best = sorted(candidates, key=lambda x: x["setup_score"], reverse=True)[0]
    if best["setup_type"] == "breakout":
        th = float(setup_cfg.get("breakout_score_threshold", 72.0))
    elif best["setup_type"] == "pullback":
        th = float(setup_cfg.get("pullback_score_threshold", 72.0))
    else:
        th = float(setup_cfg.get("anchor_score_threshold", 74.0))
    btxt = str(best.get("setup_blockers", ""))
    critical_block = any(key in btxt for key in ["入场门槛", "不是板块前排", "不做回踩", "没有有效突破", "未收回", "跌破天量", "二次异动不做"])
    best["setup_ok"] = bool(best["setup_score"] >= th and not critical_block)
    best["setup_threshold"] = th
    best["setup_all_scores"] = "；".join([f"{x['setup_type']}={x['setup_score']}" for x in candidates])
    return best


def compute_raw_metrics(
    code: str,
    name: str,
    hist: pd.DataFrame,
    cfg: Dict[str, Any],
    market: MarketState,
) -> Dict[str, Any]:
    st = cfg["strategy"]
    min_days = int(st.get("min_history_days", 160))
    atr_period = int(st.get("atr_period", 14))
    result: Dict[str, Any] = {
        "code": code,
        "name": name,
        "ok_base": False,
        "filter_reason": "",
        "data_provider": getattr(hist, "attrs", {}).get("data_provider", "") if hist is not None else "",
        "data_warning": getattr(hist, "attrs", {}).get("data_warning", "") if hist is not None else "",
    }
    if hist is None or hist.empty or len(hist) < min_days:
        result["filter_reason"] = f"历史K线不足{min_days}日"
        return result

    ind = add_indicators(hist, atr_period=atr_period)
    last = ind.iloc[-1]
    close = safe_float(last.get("close"))
    if not np.isfinite(close) or close <= 0:
        result["filter_reason"] = "收盘价无效"
        return result

    risk_gate = compute_risk_gate(ind, code, name, cfg)
    anchor_info = detect_volume_anchor_setup(ind, cfg)

    # 计算止损价：2.5ATR、MA20 下方、20日低点下方三者取更靠近当前价的位置，再套最小止损。
    atr = safe_float(last.get("atr"))
    ma5 = safe_float(last.get("ma5"))
    ma10 = safe_float(last.get("ma10"))
    ma20 = safe_float(last.get("ma20"))
    ma60 = safe_float(last.get("ma60"))
    ma120 = safe_float(last.get("ma120"))
    ma250 = safe_float(last.get("ma250"))
    low20 = safe_float(last.get("low20"))
    atr_mult = float(st.get("atr_mult", 2.5))
    min_stop_pct = float(st.get("min_stop_pct", 0.04))
    max_stop_pct = float(st.get("max_stop_pct", 0.12))
    stop_candidates = []
    if np.isfinite(atr) and atr > 0:
        stop_candidates.append(close - atr_mult * atr)
    if np.isfinite(ma20) and ma20 > 0:
        stop_candidates.append(ma20 * 0.97)
    if np.isfinite(low20) and low20 > 0:
        stop_candidates.append(low20 * 0.98)
    stop_candidates = [x for x in stop_candidates if np.isfinite(x) and 0 < x < close]
    stop_loss = max(stop_candidates) if stop_candidates else close * (1 - min_stop_pct)
    if (close - stop_loss) / close < min_stop_pct:
        stop_loss = close * (1 - min_stop_pct)
    risk_pct = (close - stop_loss) / close

    pct_chg = safe_float(last.get("pct_chg"))
    ret3 = safe_float(last.get("ret3"))
    ret5 = safe_float(last.get("ret5"))
    ret6 = safe_float(last.get("ret6"))
    ret10 = safe_float(last.get("ret10"))
    ret20 = safe_float(last.get("ret20"))
    ret30 = safe_float(last.get("ret30"))
    ret60 = safe_float(last.get("ret60"))
    ret120 = safe_float(last.get("ret120"))
    ma5_slope3 = safe_float(last.get("ma5_slope3"))
    ma10_slope5 = safe_float(last.get("ma10_slope5"))
    ma20_slope10 = safe_float(last.get("ma20_slope10"))
    ma60_slope20 = safe_float(last.get("ma60_slope20"))
    ma120_slope40 = safe_float(last.get("ma120_slope40"))
    high20 = safe_float(last.get("high20"))
    high20_prev = safe_float(last.get("high20_prev"))
    high60 = safe_float(last.get("high60"))
    high120 = safe_float(last.get("high120"))
    high120_prev = safe_float(last.get("high120_prev"))
    high60_prev = safe_float(last.get("high60_prev"))
    amount_ma20 = safe_float(last.get("amount_ma20"))
    amount_ratio5 = safe_float(last.get("amount_ratio5"))
    amount_ratio10 = safe_float(last.get("amount_ratio10"))
    amount_ratio20 = safe_float(last.get("amount_ratio20"))
    amount_ratio120_rank = safe_float(last.get("amount_ratio120_rank"))
    amount_dryup20 = safe_float(last.get("amount_dryup20"))
    atr_pct = safe_float(last.get("atr_pct"))
    drawdown10 = safe_float(last.get("drawdown10"))
    drawdown20 = safe_float(last.get("drawdown20"))
    drawdown60 = safe_float(last.get("drawdown60"))
    drawdown120 = safe_float(last.get("drawdown120"))
    drawdown250 = safe_float(last.get("drawdown250"))
    upper_shadow_pct = safe_float(last.get("upper_shadow_pct"), 0.0)
    lower_shadow_pct = safe_float(last.get("lower_shadow_pct"), 0.0)
    body_pct = safe_float(last.get("body_pct"))
    candle_strength = safe_float(last.get("candle_strength"))
    close_pos20 = safe_float(last.get("close_pos20"))
    close_pos60 = safe_float(last.get("close_pos60"))
    close_pos120 = safe_float(last.get("close_pos120"))
    range_contraction = safe_float(last.get("range_contraction_20_60"))
    reg_slope20 = safe_float(last.get("reg_slope20"))
    reg_r2_20 = safe_float(last.get("reg_r2_20"))
    reg_slope60 = safe_float(last.get("reg_slope60"))
    reg_r2_60 = safe_float(last.get("reg_r2_60"))
    turnover = safe_float(last.get("turnover")) if "turnover" in ind.columns else np.nan

    filters = []
    if st.get("exclude_st", True) and ("ST" in str(name).upper() or "*ST" in str(name).upper()):
        filters.append("ST/*ST")
    if not (float(st.get("min_close", 3.0)) <= close <= float(st.get("max_close", 300.0))):
        filters.append("价格区间不合适")
    if np.isfinite(amount_ma20) and amount_ma20 < float(st.get("min_amount_ma20", 50_000_000)):
        filters.append("成交额不足")
    if np.isfinite(pct_chg) and pct_chg > float(st.get("max_chase_day_pct", 7.5)):
        filters.append("当日涨幅过大不追")
    if np.isfinite(pct_chg) and pct_chg < float(st.get("min_day_pct", -5.5)):
        filters.append("当日跌幅过大不接")
    if upper_shadow_pct > float(st.get("avoid_upper_shadow_pct", 0.08)):
        filters.append("上影线过长")
    if bool(risk_gate.get("risk_gate_block", False)):
        filters.append("A股风险闸门：" + str(risk_gate.get("risk_gate_reason", "高风险结构")))

    # 个股硬过滤从“简单多头”升级成图形结构过滤：中期均线、回归趋势、价格所处区间都要配合。
    trend_ok = close > ma60 and ma20 > ma60 and ma20_slope10 > 0 and (not np.isfinite(reg_slope20) or reg_slope20 > -0.02)
    structure_ok = (not np.isfinite(close_pos60)) or close_pos60 >= 0.45
    if not (trend_ok and structure_ok):
        filters.append("趋势/图形结构未达标")
    if not (ret60 > 0 and ret60 > market.market_ret60):
        filters.append("60日相对强度不足")
    if risk_pct > max_stop_pct:
        filters.append("止损距离过宽")
    if market.target_exposure <= 0:
        filters.append("大盘弱势禁止新开仓")

    setup_tags: List[str] = []
    if close > ma5 > ma10 > ma20:
        setup_tags.append("强主线短均线多头")
    elif close > ma20 > ma60:
        setup_tags.append("短中期均线多头")
    if np.isfinite(reg_slope20) and reg_slope20 > 0 and np.isfinite(reg_r2_20) and reg_r2_20 >= 0.25:
        setup_tags.append("回归趋势向上")
    if np.isfinite(high120_prev) and close >= high120_prev * 0.995:
        setup_tags.append("120日突破")
    elif np.isfinite(high60_prev) and close >= high60_prev * 0.995:
        setup_tags.append("60日突破")
    elif np.isfinite(close_pos60) and close_pos60 >= 0.72:
        setup_tags.append("处于60日区间强势区")
    if np.isfinite(range_contraction) and 0.35 <= range_contraction <= 0.80:
        setup_tags.append("波动收敛")
    if np.isfinite(amount_ratio20) and 1.05 <= amount_ratio20 <= 2.80:
        setup_tags.append("量能温和放大")
    if lower_shadow_pct > upper_shadow_pct and lower_shadow_pct > 0.025:
        setup_tags.append("下影承接")
    if anchor_info.get("anchor_valid"):
        setup_tags.append("天量锚点后缩量再异动")

    result.update({
        "date": last["date"].strftime("%Y-%m-%d"),
        "open": safe_float(last.get("open")),
        "high": safe_float(last.get("high")),
        "low": safe_float(last.get("low")),
        "close": close,
        "pct_chg": pct_chg,
        "ret3": ret3,
        "ret5": ret5,
        "ret6": ret6,
        "ret10": ret10,
        "ret20": ret20,
        "ret30": ret30,
        "ret60": ret60,
        "ret120": ret120,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "ma120": ma120,
        "ma250": ma250,
        "ma5_slope3": ma5_slope3,
        "ma10_slope5": ma10_slope5,
        "ma20_slope10": ma20_slope10,
        "ma60_slope20": ma60_slope20,
        "ma120_slope40": ma120_slope40,
        "high20": high20,
        "high20_prev": high20_prev,
        "high60": high60,
        "high120": high120,
        "high60_prev": high60_prev,
        "high120_prev": high120_prev,
        "low5": safe_float(last.get("low5")),
        "low10": safe_float(last.get("low10")),
        "low20": safe_float(last.get("low20")),
        "low5_prev": safe_float(last.get("low5_prev")),
        "low10_prev": safe_float(last.get("low10_prev")),
        "low20_prev": safe_float(last.get("low20_prev")),
        "drawdown10": drawdown10,
        "drawdown20": drawdown20,
        "drawdown60": drawdown60,
        "drawdown120": drawdown120,
        "drawdown250": drawdown250,
        "close_pos20": close_pos20,
        "close_pos60": close_pos60,
        "close_pos120": close_pos120,
        "range_contraction_20_60": range_contraction,
        "amount_ma20": amount_ma20,
        "amount_ratio5": amount_ratio5,
        "amount_ratio10": amount_ratio10,
        "amount_ratio20": amount_ratio20,
        "amount_ratio120_rank": amount_ratio120_rank,
        "amount_dryup20": amount_dryup20,
        "turnover": turnover,
        "atr": atr,
        "atr_pct": atr_pct,
        "stop_loss": stop_loss,
        "risk_pct": risk_pct,
        "upper_shadow_pct": upper_shadow_pct,
        "lower_shadow_pct": lower_shadow_pct,
        "body_pct": body_pct,
        "candle_strength": candle_strength,
        "reg_slope20": reg_slope20,
        "reg_r2_20": reg_r2_20,
        "reg_slope60": reg_slope60,
        "reg_r2_60": reg_r2_60,
        "market_ret60": market.market_ret60,
        "market_regime": market.regime,
        "market_target_exposure": market.target_exposure,
        "setup_tags": "；".join(unique_nonempty(setup_tags)),
    })
    result.update(anchor_info)
    result.update(risk_gate)
    result["ok_base"] = len(filters) == 0
    result["filter_reason"] = "；".join(filters)
    return result


def score_candidates(metrics: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    if metrics.empty:
        return metrics
    df = metrics.copy()
    numeric_cols = [
        "close", "pct_chg", "ret3", "ret5", "ret6", "ret10", "ret20", "ret30", "ret60", "ret120", "ma20", "ma60", "ma120", "ma250",
        "ma20_slope10", "ma60_slope20", "ma120_slope40", "high20", "high60", "high120",
        "high60_prev", "high120_prev", "drawdown10", "drawdown20", "drawdown60", "drawdown120", "drawdown250", "close_pos20", "close_pos60", "close_pos120",
        "range_contraction_20_60", "amount_ma20", "amount_ratio20", "amount_dryup20", "atr_pct", "risk_pct",
        "turnover", "upper_shadow_pct", "lower_shadow_pct", "body_pct", "candle_strength",
        "reg_slope20", "reg_r2_20", "reg_slope60", "reg_r2_60",
        "limit_down_count_3", "limit_down_count_6", "limit_down_count_10", "days_since_limit_down",
        "ma5", "ma10", "ma5_slope3", "ma10_slope5", "high20_prev", "amount_ratio5", "amount_ratio10", "amount_ratio120_rank",
        "sector_strength_score", "sector_ret20", "sector_ret60", "sector_up_rate20", "sector_high_pos_rate",
        "stock_sector_rs20", "stock_sector_rs60", "outperform_sector20", "outperform_sector60",
        "anchor_score", "anchor_low", "anchor_high", "anchor_days_ago",
    ]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for required_col in ["ret60", "ret120", "score", "ok_base", "filter_reason"]:
        if required_col not in df.columns:
            df[required_col] = np.nan if required_col != "ok_base" else False

    # 缺失收益率不能参与相对强弱排名，避免数据错误股票出现假分数。
    df["rs_rank60"] = pd.to_numeric(df["ret60"], errors="coerce").rank(pct=True).fillna(0.0)
    df["rs_rank120"] = pd.to_numeric(df["ret120"], errors="coerce").rank(pct=True).fillna(0.0)

    scores = []
    reasons_all = []
    trend_scores = []
    momentum_scores = []
    breakout_scores = []
    risk_scores = []
    sector_scores = []
    setup_scores = []
    setup_types = []
    setup_reasons = []
    setup_blockers = []
    setup_ok_list = []
    score_details_all = []
    score_weaknesses_all = []
    for _, r in df.iterrows():
        filter_reason = str(r.get("filter_reason", ""))
        close = safe_float(r.get("close"))
        ret60 = safe_float(r.get("ret60"))
        ret120 = safe_float(r.get("ret120"))
        if ("数据错误" in filter_reason) or (not np.isfinite(close)) or (not np.isfinite(ret60)):
            scores.append(0.0)
            reasons_all.append("")
            trend_scores.append(0.0)
            momentum_scores.append(0.0)
            breakout_scores.append(0.0)
            risk_scores.append(0.0)
            sector_scores.append(0.0)
            setup_scores.append(0.0)
            setup_types.append("none")
            setup_reasons.append("")
            setup_blockers.append("数据无效或K线缺失")
            setup_ok_list.append(False)
            score_details_all.append("数据无效，未评分")
            score_weaknesses_all.append("数据无效或K线缺失")
            continue
        risk_gate_reason = str(r.get("risk_gate_reason", ""))
        if bool(r.get("risk_gate_block", False)) or risk_gate_reason:
            scores.append(0.0)
            reasons_all.append("")
            trend_scores.append(0.0)
            momentum_scores.append(0.0)
            breakout_scores.append(0.0)
            risk_scores.append(0.0)
            sector_scores.append(0.0)
            setup_scores.append(0.0)
            setup_types.append("risk_gate_blocked")
            setup_reasons.append("")
            setup_blockers.append(risk_gate_reason or "A股风险闸门触发")
            setup_ok_list.append(False)
            score_details_all.append("A股风险闸门触发，禁止评分为买点")
            score_weaknesses_all.append(risk_gate_reason or "连续跌停/高位崩盘/暴涨后踩踏风险")
            continue

        ma20 = safe_float(r.get("ma20"))
        ma60 = safe_float(r.get("ma60"))
        ma120 = safe_float(r.get("ma120"))
        ma250 = safe_float(r.get("ma250"))
        ret20 = safe_float(r.get("ret20"))
        rs60 = safe_float(r.get("rs_rank60"), 0)
        rs120 = safe_float(r.get("rs_rank120"), 0)
        high20 = safe_float(r.get("high20"))
        high60 = safe_float(r.get("high60"))
        high120 = safe_float(r.get("high120"))
        high60_prev = safe_float(r.get("high60_prev"))
        high120_prev = safe_float(r.get("high120_prev"))
        amount_ratio = safe_float(r.get("amount_ratio20"))
        amount_dryup = safe_float(r.get("amount_dryup20"))
        atr_pct = safe_float(r.get("atr_pct"))
        drawdown120 = safe_float(r.get("drawdown120"))
        risk_pct = safe_float(r.get("risk_pct"))
        ma20_slope10 = safe_float(r.get("ma20_slope10"))
        ma60_slope20 = safe_float(r.get("ma60_slope20"))
        ma120_slope40 = safe_float(r.get("ma120_slope40"))
        close_pos60 = safe_float(r.get("close_pos60"))
        close_pos120 = safe_float(r.get("close_pos120"))
        contraction = safe_float(r.get("range_contraction_20_60"))
        reg_slope20 = safe_float(r.get("reg_slope20"))
        reg_r2_20 = safe_float(r.get("reg_r2_20"))
        reg_slope60 = safe_float(r.get("reg_slope60"))
        reg_r2_60 = safe_float(r.get("reg_r2_60"))
        candle_strength = safe_float(r.get("candle_strength"))
        upper_shadow = safe_float(r.get("upper_shadow_pct"), 0.0)
        lower_shadow = safe_float(r.get("lower_shadow_pct"), 0.0)
        sector_strength = safe_float(r.get("sector_strength_score"), 0.0)
        sector_ret20 = safe_float(r.get("sector_ret20"))
        sector_ret60 = safe_float(r.get("sector_ret60"))
        sector_up_rate20 = safe_float(r.get("sector_up_rate20"), 0.0)
        sector_high_pos_rate = safe_float(r.get("sector_high_pos_rate"), 0.0)
        stock_sector_rs20 = safe_float(r.get("stock_sector_rs20"), 0.0)
        stock_sector_rs60 = safe_float(r.get("stock_sector_rs60"), 0.0)
        outperform_sector20 = safe_float(r.get("outperform_sector20"))
        outperform_sector60 = safe_float(r.get("outperform_sector60"))

        setup_info = classify_setup_row(r, cfg)
        setup_type = str(setup_info.get("setup_type", "none"))
        setup_score = safe_float(setup_info.get("setup_score"), 0.0)
        setup_ok = bool(setup_info.get("setup_ok", False))
        setup_reason = str(setup_info.get("setup_reason", ""))
        setup_block = str(setup_info.get("setup_blockers", ""))

        trend = 0.0
        trend += 4 if close > ma20 else 0
        trend += 6 if close > ma60 else 0
        trend += 5 if close > ma120 else 0
        trend += 2 if np.isfinite(ma250) and close > ma250 else 0
        trend += 5 if ma20 > ma60 else 0
        trend += 4 if ma60 > ma120 else 0
        trend += 3 if np.isfinite(ma250) and ma120 > ma250 else 0
        trend += 3 if ma20_slope10 > 0 else 0
        trend += 2 if ma60_slope20 > 0 else 0
        trend += 2 if ma120_slope40 > 0 else 0
        trend += 4 if reg_slope20 > 0 and reg_r2_20 >= 0.25 else 0
        trend += 2 if reg_slope60 > 0 and reg_r2_60 >= 0.20 else 0
        trend = min(trend, 35)

        momentum = 0.0
        momentum += 10 * max(0, min(1, rs60))
        momentum += 6 * max(0, min(1, rs120))
        momentum += 3 if ret20 > 0 else 0
        momentum += 3 if ret60 > 0 else 0
        momentum += 2 if ret120 > 0 else 0
        momentum += 1 if (np.isfinite(ret20) and np.isfinite(ret60) and ret20 > ret60 / 3) else 0
        momentum = min(momentum, 25)

        structure = 0.0
        if np.isfinite(high120_prev) and high120_prev > 0:
            if close >= high120_prev * 0.995:
                structure += 7
            elif np.isfinite(high120) and close >= high120 * 0.97:
                structure += 5
        if np.isfinite(high60_prev) and high60_prev > 0:
            if close >= high60_prev * 0.995:
                structure += 5
            elif np.isfinite(high60) and close >= high60 * 0.97:
                structure += 3
        if np.isfinite(high20) and high20 > 0:
            if close >= high20 * 0.995:
                structure += 3
            elif close >= high20 * 0.97:
                structure += 2
        if np.isfinite(close_pos60):
            if close_pos60 >= 0.75:
                structure += 3
            elif close_pos60 >= 0.55:
                structure += 2
        if np.isfinite(close_pos120) and close_pos120 >= 0.55:
            structure += 2
        if np.isfinite(contraction) and 0.35 <= contraction <= 0.80:
            structure += 3
        elif np.isfinite(contraction) and contraction < 0.35:
            structure += 1
        if np.isfinite(amount_ratio):
            if 1.05 <= amount_ratio <= 2.80:
                structure += 4
            elif 0.80 <= amount_ratio < 1.05:
                structure += 2
            elif amount_ratio > 3.50:
                structure -= 2
        if np.isfinite(amount_dryup) and amount_dryup <= 0.85 and np.isfinite(contraction) and contraction <= 0.85:
            structure += 1
        if candle_strength > 0.20:
            structure += 2
        if upper_shadow > 0.08:
            structure -= 3
        if lower_shadow > upper_shadow and lower_shadow > 0.025:
            structure += 1
        structure = max(0.0, min(structure, 25))

        risk = 0.0
        if np.isfinite(atr_pct) and 0.012 <= atr_pct <= 0.075:
            risk += 4
        elif np.isfinite(atr_pct) and 0.075 < atr_pct <= 0.10:
            risk += 1
        if np.isfinite(drawdown120) and -0.20 <= drawdown120 <= -0.00:
            risk += 4
        elif np.isfinite(drawdown120) and drawdown120 > -0.02:
            risk += 3
        if np.isfinite(risk_pct) and 0.04 <= risk_pct <= 0.10:
            risk += 5
        elif np.isfinite(risk_pct) and risk_pct <= 0.12:
            risk += 2
        if np.isfinite(amount_ratio) and amount_ratio <= 3.0:
            risk += 2
        risk = min(risk, 15)

        # v6 不再把“突破/回踩/锚点”揉成一个结构分，而是先分型，再把板块主线和买点分型纳入总分。
        sector_component = 0.0
        sector_component += min(20.0, sector_strength * 0.20)
        sector_component += 5.0 if bool(r.get("sector_is_strong", False)) else 0.0
        sector_component += 5.0 if bool(r.get("sector_is_mainline", False)) else 0.0
        sector_component += 5.0 * max(0.0, min(1.0, stock_sector_rs20))
        sector_component += 5.0 * max(0.0, min(1.0, stock_sector_rs60))
        sector_component = min(30.0, sector_component)
        trend_component = min(20.0, trend / 35.0 * 20.0)
        momentum_component = min(15.0, momentum / 25.0 * 15.0)
        setup_component = min(25.0, setup_score / 100.0 * 25.0)
        risk_component = min(10.0, risk / 15.0 * 10.0)
        total = trend_component + momentum_component + sector_component + setup_component + risk_component
        reasons = split_reason_text(r.get("setup_tags", ""))
        if setup_reason:
            reasons.extend(split_reason_text(setup_reason))
        weaknesses = []
        if sector_strength >= float(cfg.get("strategy", {}).get("sector", {}).get("mainline_score_threshold", 68.0)):
            reasons.append("所属板块为主线/强主线")
        elif sector_strength >= float(cfg.get("strategy", {}).get("sector", {}).get("strong_score_threshold", 58.0)):
            reasons.append("所属板块偏强")
        else:
            weaknesses.append("所属板块主线强度不足")
        if stock_sector_rs20 >= 0.70 or stock_sector_rs60 >= 0.70:
            reasons.append("个股在板块内前排")
        else:
            weaknesses.append("个股板块内强度不靠前")
        if setup_ok:
            reasons.append(f"买点分型={setup_type}")
        else:
            weaknesses.append(f"买点分型未达标：{setup_type} {setup_score:.1f}/{safe_float(setup_info.get('setup_threshold'), 0):.1f}")
            if setup_block:
                weaknesses.extend(split_reason_text(setup_block))

        if trend >= 27:
            reasons.append("趋势模板优秀")
        else:
            if not (close > ma20): weaknesses.append("未站上20日线")
            if not (close > ma60): weaknesses.append("未站上60日线")
            if not (close > ma120): weaknesses.append("未站上120日线")
            if not (ma20 > ma60): weaknesses.append("20日线未高于60日线")
            if not (ma60 > ma120): weaknesses.append("60日线未高于120日线")
            if not (ma20_slope10 > 0): weaknesses.append("20日线斜率未转正")
            if not (reg_slope20 > 0 and reg_r2_20 >= 0.25): weaknesses.append("20日趋势推进效率不足")
        if rs60 >= 0.70:
            reasons.append("股票池内60日相对强")
        else:
            weaknesses.append("股票池内60日强度排名不靠前")
        if structure >= 17:
            reasons.append("突破/平台形态较好")
        else:
            if np.isfinite(high120) and high120 > 0 and close < high120 * 0.94:
                weaknesses.append("距离120日高点较远")
            if np.isfinite(close_pos60) and close_pos60 < 0.55:
                weaknesses.append("价格不在60日区间强势区")
            if (not np.isfinite(amount_ratio)) or amount_ratio < 0.80:
                weaknesses.append("量能未放大")
            elif amount_ratio > 3.50:
                weaknesses.append("量能过热，追高风险变大")
            if np.isfinite(contraction) and contraction > 1.05:
                weaknesses.append("波动未收敛")
        if np.isfinite(risk_pct):
            reasons.append(f"止损{risk_pct:.1%}")
        if np.isfinite(risk_pct) and risk_pct > float(cfg["strategy"].get("max_stop_pct", 0.12)):
            weaknesses.append("止损距离过宽")

        scores.append(round(total, 2))
        reasons_all.append("；".join(unique_nonempty(reasons)))
        trend_scores.append(round(trend_component, 2))
        momentum_scores.append(round(momentum_component, 2))
        breakout_scores.append(round(setup_component, 2))
        risk_scores.append(round(risk_component, 2))
        sector_scores.append(round(sector_component, 2))
        setup_scores.append(round(setup_score, 2))
        setup_types.append(setup_type)
        setup_reasons.append(setup_reason)
        setup_blockers.append(setup_block)
        setup_ok_list.append(setup_ok)
        score_details_all.append(
            f"趋势{trend_component:.1f}/20；动量/市场RS{momentum_component:.1f}/15；板块主线{sector_component:.1f}/30；买点分型{setup_component:.1f}/25；风控{risk_component:.1f}/10；"
            f"原始买点={setup_type} {setup_score:.1f}/{safe_float(setup_info.get('setup_threshold'), 0):.1f}"
        )
        score_weaknesses_all.append("；".join(unique_nonempty(weaknesses)))

    df["score"] = scores
    df["reason"] = reasons_all
    df["trend_score"] = trend_scores
    df["momentum_score"] = momentum_scores
    df["breakout_score"] = breakout_scores
    df["risk_score"] = risk_scores
    df["sector_score"] = sector_scores
    df["setup_score"] = setup_scores
    df["setup_type"] = setup_types
    df["setup_reason"] = setup_reasons
    df["setup_blockers"] = setup_blockers
    df["setup_ok"] = setup_ok_list
    df["score_detail"] = score_details_all
    df["score_weakness"] = score_weaknesses_all
    threshold = float(cfg["strategy"].get("score_threshold", 72))
    def _signal_grade(row: pd.Series) -> str:
        score = safe_float(row.get("score"), 0.0)
        sector = safe_float(row.get("sector_strength_score"), 0.0)
        setup = safe_float(row.get("setup_score"), 0.0)
        rs20 = safe_float(row.get("stock_sector_rs20"), 0.0)
        if score >= 84 and sector >= 82 and setup >= 82 and rs20 >= 0.75:
            return "S"
        if score >= 78 and sector >= 72 and setup >= 76 and rs20 >= 0.65:
            return "A"
        if score >= threshold:
            return "B"
        return "C"
    df["signal_grade"] = df.apply(_signal_grade, axis=1)
    df["is_signal"] = df["ok_base"].fillna(False) & df["setup_ok"].fillna(False) & (df["score"] >= threshold)
    return df


def allocate_positions(signals: pd.DataFrame, cfg: Dict[str, Any], market: MarketState, account: float) -> pd.DataFrame:
    if signals.empty:
        return signals
    st = cfg["strategy"]
    max_positions = int(st.get("max_positions", 5))
    max_position_pct = float(st.get("max_position_pct", 0.15))
    risk_per_trade_pct = float(st.get("risk_per_trade_pct", 0.005))
    total_exposure = float(market.target_exposure)

    out = signals.sort_values(["score", "rs_rank60"], ascending=[False, False]).head(max_positions).copy()
    if out.empty or total_exposure <= 0:
        out["target_weight"] = 0.0
        out["target_cash"] = 0.0
        out["target_shares"] = 0
        return out

    score_sum = out["score"].sum()
    if not np.isfinite(score_sum) or score_sum <= 0:
        raw_weights = np.full(len(out), total_exposure / len(out))
    else:
        raw_weights = total_exposure * out["score"].values / score_sum

    weights = []
    for raw_w, (_, r) in zip(raw_weights, out.iterrows()):
        risk_pct = safe_float(r.get("risk_pct"))
        if np.isfinite(risk_pct) and risk_pct > 0:
            risk_cap_weight = risk_per_trade_pct / risk_pct
        else:
            risk_cap_weight = max_position_pct
        grade = str(r.get("signal_grade", "B")).upper()
        grade_cap = {"S": max_position_pct, "A": max_position_pct * 0.95, "B": max_position_pct * 0.75}.get(grade, max_position_pct * 0.45)
        weights.append(max(0.0, min(float(raw_w), max_position_pct, grade_cap, risk_cap_weight)))

    out["target_weight"] = weights
    if account and account > 0:
        out["target_cash"] = out["target_weight"] * account
        shares = []
        for _, r in out.iterrows():
            close = safe_float(r.get("close"))
            cash = safe_float(r.get("target_cash"), 0.0)
            if close > 0:
                # A 股一手 100 股，不足一手记为 0
                shares.append(int(math.floor(cash / close / 100) * 100))
            else:
                shares.append(0)
        out["target_shares"] = shares
        out["actual_weight_by_lot"] = out["target_shares"] * out["close"] / account
    else:
        out["target_cash"] = np.nan
        out["target_shares"] = np.nan
        out["actual_weight_by_lot"] = np.nan

    # 以 R 倍数给止盈参考；实际执行用移动止盈更合理。
    out["take_profit_1"] = out["close"] + 1.5 * (out["close"] - out["stop_loss"])
    out["take_profit_2"] = out["close"] + 3.0 * (out["close"] - out["stop_loss"])
    return out


