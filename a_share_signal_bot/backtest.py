#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股 v6.6 严格执行回测器

回测约定：
- 每个交易日收盘后，用当日及以前的 K 线计算信号；
- 信号在下一交易日按开盘价成交，避免未来函数；
- 只做多，不融资，不自动下单；
- 买入后用初始止损、1.5R/3R 分批止盈、MA20/MA60 趋势退出、最大持仓天数管理；
- 默认严格执行：收盘才能确认的趋势/大盘/持仓天数退出，下一交易日开盘卖出；
- 默认按实际成交买入价重算 1.5R/3R，且粗略模拟涨跌停不可成交。
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from . import scanner as bot


@dataclass
class BacktestConfig:
    account: float
    start: str
    end: str
    fetch_start: str
    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.0005
    slippage_bps: float = 5.0
    max_hold_days: int = 60
    trend_exit: bool = True
    market_exit: bool = True
    max_signal_rows: int = 20000
    strict_execution: bool = True
    recalc_targets_from_entry: bool = True
    block_limit_up_buys: bool = True
    block_limit_down_sells: bool = True
    limit_tolerance_pct: float = 0.003


def ymd_to_ts(s: str) -> pd.Timestamp:
    s = str(s).strip().replace("-", "")
    return pd.Timestamp(datetime.strptime(s, "%Y%m%d").date())


def ts_to_ymd(ts: pd.Timestamp) -> str:
    return pd.Timestamp(ts).strftime("%Y%m%d")


def date_back_from_ymd(end_ymd: str, days: int) -> str:
    return (ymd_to_ts(end_ymd) - pd.Timedelta(days=days)).strftime("%Y%m%d")


def normalize_code_set(codes: Iterable[str]) -> set[str]:
    out = set()
    for c in codes:
        try:
            out.add(bot.normalize_code(c))
        except Exception:
            pass
    return out


def cost_buy(amount: float, cfg: BacktestConfig) -> float:
    return max(0.0, float(amount)) * float(cfg.commission_rate)


def cost_sell(amount: float, cfg: BacktestConfig) -> float:
    return max(0.0, float(amount)) * (float(cfg.commission_rate) + float(cfg.stamp_tax_rate))


def apply_slippage(price: float, side: str, cfg: BacktestConfig) -> float:
    if not np.isfinite(price) or price <= 0:
        return price
    bps = float(cfg.slippage_bps) / 10000.0
    if side.lower() == "buy":
        return price * (1.0 + bps)
    return price * (1.0 - bps)


def value_positions(positions: Dict[str, Dict[str, Any]], bars: Dict[str, pd.DataFrame], date: pd.Timestamp) -> float:
    total = 0.0
    for code, pos in positions.items():
        bar = get_bar(bars.get(code), date)
        if bar is not None:
            px = bot.safe_float(bar.get("close"))
        else:
            px = bot.safe_float(pos.get("last_price"), bot.safe_float(pos.get("entry_price")))
        if np.isfinite(px) and px > 0:
            total += int(pos.get("shares", 0)) * px
            pos["last_price"] = px
    return float(total)


def get_bar(df: Optional[pd.DataFrame], date: pd.Timestamp) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    # df 已经按日期排序；date 列为 Timestamp。
    m = df["date"] == pd.Timestamp(date)
    if not bool(m.any()):
        return None
    return df.loc[m].iloc[0]


def get_prev_bar(df: Optional[pd.DataFrame], date: pd.Timestamp) -> Optional[pd.Series]:
    """返回 date 之前最近一个交易日的 K 线，用于估算涨跌停阈值。"""
    if df is None or df.empty or "date" not in df.columns:
        return None
    s = df[df["date"] < pd.Timestamp(date)]
    if s.empty:
        return None
    return s.iloc[-1]


def limit_up_threshold_pct(code: str, name: str = "") -> float:
    """粗略估算 A 股涨停阈值百分数。主板约 10%，创业/科创约 20%，ST 约 5%。"""
    try:
        return abs(float(bot.limit_down_threshold_pct(code, name)))
    except Exception:
        code = bot.normalize_code(code)
        up_name = str(name).upper()
        if "ST" in up_name or "*ST" in up_name:
            return 4.8
        if code.startswith(("300", "301", "688")):
            return 19.0
        if code.startswith(("8", "4")):
            return 29.0
        return 9.5


def pct_from_prev_close(df: Optional[pd.DataFrame], date: pd.Timestamp, price: float) -> float:
    prev = get_prev_bar(df, date)
    if prev is None:
        return np.nan
    prev_close = bot.safe_float(prev.get("close"))
    if not np.isfinite(prev_close) or prev_close <= 0 or not np.isfinite(price):
        return np.nan
    return float(price / prev_close - 1.0)


def is_limit_up_open(df: Optional[pd.DataFrame], date: pd.Timestamp, bar: pd.Series, code: str, name: str, cfg: BacktestConfig) -> bool:
    if not cfg.block_limit_up_buys:
        return False
    open_px = bot.safe_float(bar.get("open"))
    if not np.isfinite(open_px) or open_px <= 0:
        return False
    pct = pct_from_prev_close(df, date, open_px)
    if not np.isfinite(pct):
        return False
    threshold = limit_up_threshold_pct(code, name) / 100.0
    return pct >= threshold - float(cfg.limit_tolerance_pct)


def is_limit_down_locked(df: Optional[pd.DataFrame], date: pd.Timestamp, bar: pd.Series, code: str, name: str, cfg: BacktestConfig) -> bool:
    """粗略判断跌停封死/难以卖出。

    日线无法知道真实排队成交。这里用保守近似：开盘接近跌停且全天几乎没有离开开盘价，
    认为卖出无法成交；如果高点明显高于开盘，认为跌停打开过，可以按规则成交。
    """
    if not cfg.block_limit_down_sells:
        return False
    open_px = bot.safe_float(bar.get("open"))
    high_px = bot.safe_float(bar.get("high"))
    if not np.isfinite(open_px) or open_px <= 0:
        return False
    pct = pct_from_prev_close(df, date, open_px)
    if not np.isfinite(pct):
        return False
    threshold = float(bot.limit_down_threshold_pct(code, name)) / 100.0
    near_limit_down = pct <= threshold + float(cfg.limit_tolerance_pct)
    barely_opened = (not np.isfinite(high_px)) or high_px <= open_px * (1.0 + 0.003)
    return bool(near_limit_down and barely_opened)


def get_last_trading_date_before(dates: List[pd.Timestamp], date: pd.Timestamp) -> Optional[pd.Timestamp]:
    prev = [pd.Timestamp(x) for x in dates if pd.Timestamp(x) < pd.Timestamp(date)]
    return prev[-1] if prev else None


def get_last_ind_row(ind: Optional[pd.DataFrame], date: pd.Timestamp) -> Optional[pd.Series]:
    if ind is None or ind.empty:
        return None
    s = ind[ind["date"] <= pd.Timestamp(date)]
    if s.empty:
        return None
    return s.iloc[-1]


def slice_ind(ind: Optional[pd.DataFrame], date: pd.Timestamp) -> pd.DataFrame:
    if ind is None or ind.empty:
        return pd.DataFrame()
    return ind[ind["date"] <= pd.Timestamp(date)].copy()


def compute_metrics_from_indicators(
    code: str,
    name: str,
    ind: pd.DataFrame,
    cfg: Dict[str, Any],
    market: bot.MarketState,
    provider: str = "preloaded",
    warning: str = "",
) -> Dict[str, Any]:
    """与 main.compute_raw_metrics 等价，但输入是已预计算指标的切片，避免回测每天重复滚动计算。"""
    st = cfg["strategy"]
    min_days = int(st.get("min_history_days", 160))
    result: Dict[str, Any] = {
        "code": code,
        "name": name,
        "ok_base": False,
        "filter_reason": "",
        "data_provider": provider,
        "data_warning": warning,
    }
    if ind is None or ind.empty or len(ind) < min_days:
        result["filter_reason"] = f"历史K线不足{min_days}日"
        return result

    last = ind.iloc[-1]
    close = bot.safe_float(last.get("close"))
    if not np.isfinite(close) or close <= 0:
        result["filter_reason"] = "收盘价无效"
        return result

    risk_gate = bot.compute_risk_gate(ind, code, name, cfg)
    anchor_info = bot.detect_volume_anchor_setup(ind, cfg)

    atr = bot.safe_float(last.get("atr"))
    ma5 = bot.safe_float(last.get("ma5"))
    ma10 = bot.safe_float(last.get("ma10"))
    ma20 = bot.safe_float(last.get("ma20"))
    ma60 = bot.safe_float(last.get("ma60"))
    ma120 = bot.safe_float(last.get("ma120"))
    ma250 = bot.safe_float(last.get("ma250"))
    low20 = bot.safe_float(last.get("low20"))
    atr_mult = float(st.get("atr_mult", 2.5))
    min_stop_pct = float(st.get("min_stop_pct", 0.04))
    max_stop_pct = float(st.get("max_stop_pct", 0.12))
    stop_candidates: List[float] = []
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

    pct_chg = bot.safe_float(last.get("pct_chg"))
    ret3 = bot.safe_float(last.get("ret3"))
    ret5 = bot.safe_float(last.get("ret5"))
    ret6 = bot.safe_float(last.get("ret6"))
    ret10 = bot.safe_float(last.get("ret10"))
    ret20 = bot.safe_float(last.get("ret20"))
    ret30 = bot.safe_float(last.get("ret30"))
    ret60 = bot.safe_float(last.get("ret60"))
    ret120 = bot.safe_float(last.get("ret120"))
    ma5_slope3 = bot.safe_float(last.get("ma5_slope3"))
    ma10_slope5 = bot.safe_float(last.get("ma10_slope5"))
    ma20_slope10 = bot.safe_float(last.get("ma20_slope10"))
    ma60_slope20 = bot.safe_float(last.get("ma60_slope20"))
    ma120_slope40 = bot.safe_float(last.get("ma120_slope40"))
    high20 = bot.safe_float(last.get("high20"))
    high20_prev = bot.safe_float(last.get("high20_prev"))
    high60 = bot.safe_float(last.get("high60"))
    high120 = bot.safe_float(last.get("high120"))
    high120_prev = bot.safe_float(last.get("high120_prev"))
    high60_prev = bot.safe_float(last.get("high60_prev"))
    amount_ma20 = bot.safe_float(last.get("amount_ma20"))
    amount_ratio5 = bot.safe_float(last.get("amount_ratio5"))
    amount_ratio10 = bot.safe_float(last.get("amount_ratio10"))
    amount_ratio20 = bot.safe_float(last.get("amount_ratio20"))
    amount_ratio120_rank = bot.safe_float(last.get("amount_ratio120_rank"))
    amount_dryup20 = bot.safe_float(last.get("amount_dryup20"))
    atr_pct = bot.safe_float(last.get("atr_pct"))
    drawdown10 = bot.safe_float(last.get("drawdown10"))
    drawdown20 = bot.safe_float(last.get("drawdown20"))
    drawdown60 = bot.safe_float(last.get("drawdown60"))
    drawdown120 = bot.safe_float(last.get("drawdown120"))
    drawdown250 = bot.safe_float(last.get("drawdown250"))
    upper_shadow_pct = bot.safe_float(last.get("upper_shadow_pct"), 0.0)
    lower_shadow_pct = bot.safe_float(last.get("lower_shadow_pct"), 0.0)
    body_pct = bot.safe_float(last.get("body_pct"))
    candle_strength = bot.safe_float(last.get("candle_strength"))
    close_pos20 = bot.safe_float(last.get("close_pos20"))
    close_pos60 = bot.safe_float(last.get("close_pos60"))
    close_pos120 = bot.safe_float(last.get("close_pos120"))
    range_contraction = bot.safe_float(last.get("range_contraction_20_60"))
    reg_slope20 = bot.safe_float(last.get("reg_slope20"))
    reg_r2_20 = bot.safe_float(last.get("reg_r2_20"))
    reg_slope60 = bot.safe_float(last.get("reg_slope60"))
    reg_r2_60 = bot.safe_float(last.get("reg_r2_60"))
    turnover = bot.safe_float(last.get("turnover")) if "turnover" in ind.columns else np.nan

    filters: List[str] = []
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
        "open": bot.safe_float(last.get("open")),
        "high": bot.safe_float(last.get("high")),
        "low": bot.safe_float(last.get("low")),
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
        "low5": bot.safe_float(last.get("low5")),
        "low10": bot.safe_float(last.get("low10")),
        "low20": bot.safe_float(last.get("low20")),
        "low5_prev": bot.safe_float(last.get("low5_prev")),
        "low10_prev": bot.safe_float(last.get("low10_prev")),
        "low20_prev": bot.safe_float(last.get("low20_prev")),
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
        "setup_tags": "；".join(bot.unique_nonempty(setup_tags)),
    })
    result.update(anchor_info)
    result.update(risk_gate)
    result["ok_base"] = len(filters) == 0
    result["filter_reason"] = "；".join(filters)
    return result


def evaluate_market_asof(index_inds: Dict[str, pd.DataFrame], cfg: Dict[str, Any], asof: pd.Timestamp) -> bot.MarketState:
    rows: List[Dict[str, Any]] = []
    latest_dates: List[pd.Timestamp] = []
    for symbol, ind_all in index_inds.items():
        try:
            ind = slice_ind(ind_all, asof)
            if len(ind) < 160:
                rows.append({"symbol": symbol, "date": "", "close": np.nan, "score": 0, "error": "指数历史K线不足160日", "provider": "preloaded"})
                continue
            last = ind.iloc[-1]
            close = bot.safe_float(last["close"])
            ma20 = bot.safe_float(last.get("ma20"))
            ma60 = bot.safe_float(last.get("ma60"))
            ma120 = bot.safe_float(last.get("ma120"))
            ma250 = bot.safe_float(last.get("ma250"))
            ret20 = bot.safe_float(last.get("ret20"))
            ret60 = bot.safe_float(last.get("ret60"))
            ret120 = bot.safe_float(last.get("ret120"))
            dd120 = bot.safe_float(last.get("drawdown120"))
            pos60 = bot.safe_float(last.get("close_pos60"))
            pos120 = bot.safe_float(last.get("close_pos120"))
            reg_slope20 = bot.safe_float(last.get("reg_slope20"))
            reg_r2_20 = bot.safe_float(last.get("reg_r2_20"))
            reg_slope60 = bot.safe_float(last.get("reg_slope60"))
            reg_r2_60 = bot.safe_float(last.get("reg_r2_60"))
            contraction = bot.safe_float(last.get("range_contraction_20_60"))
            amount_ratio20 = bot.safe_float(last.get("amount_ratio20"))
            amount_dryup20 = bot.safe_float(last.get("amount_dryup20"))
            candle_strength = bot.safe_float(last.get("candle_strength"))
            upper_shadow = bot.safe_float(last.get("upper_shadow_pct"), 0.0)
            atr_pct = bot.safe_float(last.get("atr_pct"))

            trend = 0.0
            trend += 8 if close > ma20 else 0
            trend += 10 if close > ma60 else 0
            trend += 7 if close > ma120 else 0
            trend += 5 if close > ma250 else 0
            trend += 8 if ma20 > ma60 > ma120 else 0
            trend += 6 if bot.safe_float(last.get("ma20_slope10")) > 0 else 0
            trend += 4 if bot.safe_float(last.get("ma60_slope20")) > 0 else 0
            trend += 5 if reg_slope20 > 0 and reg_r2_20 >= 0.25 else 0
            trend = min(trend, 53)

            momentum = 0.0
            momentum += 6 if ret20 > 0 else 0
            momentum += 7 if ret60 > 0 else 0
            momentum += 5 if ret120 > 0 else 0
            momentum += 5 if reg_slope60 > 0 and reg_r2_60 >= 0.20 else 0
            momentum += 4 if pos60 >= 0.65 else 0
            momentum = min(momentum, 27)

            structure = 0.0
            structure += 5 if np.isfinite(dd120) and dd120 > -0.08 else 0
            structure += 4 if np.isfinite(pos120) and pos120 >= 0.55 else 0
            structure += 4 if np.isfinite(contraction) and 0.35 <= contraction <= 0.85 else 0
            if np.isfinite(amount_ratio20):
                if 0.75 <= amount_ratio20 <= 1.35:
                    structure += 4
                elif 1.35 < amount_ratio20 <= 2.10 and candle_strength > 0:
                    structure += 3
                elif amount_ratio20 > 2.30 and candle_strength < 0:
                    structure -= 5
            if np.isfinite(amount_dryup20) and amount_dryup20 <= 0.85 and np.isfinite(contraction) and contraction <= 0.90:
                structure += 2
            structure += 3 if np.isfinite(atr_pct) and 0.006 <= atr_pct <= 0.035 else 0
            structure += 3 if candle_strength > 0.15 else 0
            structure += 0 if upper_shadow <= 0.025 else -4
            structure = max(0.0, min(structure, 24))
            score = trend + momentum + structure

            notes: List[str] = []
            if close > ma20 > ma60 and ma60 > ma120:
                notes.append("均线多头")
            elif close > ma60:
                notes.append("中期趋势尚可")
            else:
                notes.append("指数未站稳中期均线")
            if reg_slope20 > 0 and reg_r2_20 >= 0.25:
                notes.append("20日回归趋势向上")
            if np.isfinite(contraction) and contraction <= 0.85:
                notes.append("波动收敛")
            if np.isfinite(amount_ratio20):
                if 0.75 <= amount_ratio20 <= 1.35:
                    notes.append("量能平稳")
                elif 1.35 < amount_ratio20 <= 2.10 and candle_strength > 0:
                    notes.append("温和放量上攻")
                elif amount_ratio20 > 2.30 and candle_strength < 0:
                    notes.append("放量下跌风险")
            if np.isfinite(amount_dryup20) and amount_dryup20 <= 0.85 and np.isfinite(contraction) and contraction <= 0.90:
                notes.append("缩量整理")
            if np.isfinite(dd120) and dd120 <= -0.12:
                notes.append("距阶段高点较远")
            if upper_shadow > 0.025:
                notes.append("上影线偏长")

            rows.append({
                "symbol": symbol,
                "date": last["date"].strftime("%Y-%m-%d"),
                "close": close,
                "score": round(score, 2),
                "trend_score": round(trend, 2),
                "momentum_score": round(momentum, 2),
                "structure_score": round(structure, 2),
                "ret20": ret20,
                "ret60": ret60,
                "ret120": ret120,
                "drawdown120": dd120,
                "close_pos60": pos60,
                "close_pos120": pos120,
                "range_contraction_20_60": contraction,
                "amount_ratio20": amount_ratio20,
                "amount_dryup20": amount_dryup20,
                "reg_slope20": reg_slope20,
                "reg_r2_20": reg_r2_20,
                "reg_slope60": reg_slope60,
                "reg_r2_60": reg_r2_60,
                "atr_pct": atr_pct,
                "upper_shadow_pct": upper_shadow,
                "candle_strength": candle_strength,
                "chart_notes": "；".join(bot.unique_nonempty(notes)),
                "provider": "preloaded",
            })
            latest_dates.append(pd.Timestamp(last["date"]))
        except Exception as exc:
            rows.append({"symbol": symbol, "date": "", "close": np.nan, "score": 0, "error": str(exc)})

    details = pd.DataFrame(rows)
    if details.empty or details["score"].replace([np.inf, -np.inf], np.nan).dropna().empty:
        return bot.MarketState(ts_to_ymd(asof), 0, "weak", 0, details, "大盘数据不足，禁止新开仓", 0, 0)

    details, date_warning, force_defensive = bot.enforce_market_date_consistency(details, cfg)
    valid = details[pd.to_numeric(details.get("close"), errors="coerce").notna()].copy()
    if valid.empty:
        valid = details.copy()
    avg_score = float(pd.to_numeric(valid["score"], errors="coerce").fillna(0).mean())
    min_score = float(pd.to_numeric(valid["score"], errors="coerce").fillna(0).min())
    if force_defensive:
        avg_score = min(avg_score, 44.0)
        min_score = min(min_score, 0.0)
    market_ret20 = float(pd.to_numeric(valid.get("ret20"), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().mean()) if "ret20" in valid else 0.0
    market_ret60 = float(pd.to_numeric(valid.get("ret60"), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().mean()) if "ret60" in valid else 0.0

    st_cfg = cfg["strategy"]
    if avg_score >= 76 and min_score >= 52:
        regime = "strong"
        exposure = float(st_cfg.get("total_exposure_strong", 0.8))
        structure_text = "强势多头"
    elif avg_score >= 58 and min_score >= 38:
        regime = "neutral"
        exposure = float(st_cfg.get("total_exposure_neutral", 0.45))
        structure_text = "震荡偏强/结构修复"
    elif avg_score >= 45:
        regime = "cautious"
        exposure = min(float(st_cfg.get("total_exposure_neutral", 0.45)), 0.25)
        structure_text = "弱修复/谨慎试错"
    else:
        regime = "weak"
        exposure = float(st_cfg.get("total_exposure_weak", 0.0))
        structure_text = "弱势防守"

    date = max(latest_dates).strftime("%Y-%m-%d") if latest_dates else asof.strftime("%Y-%m-%d")
    note_counts: Dict[str, int] = {}
    for notes in valid.get("chart_notes", pd.Series(dtype=str)).astype(str):
        for item in bot.split_reason_text(notes):
            note_counts[item] = note_counts.get(item, 0) + 1
    note_text = "；".join([k for k, _ in sorted(note_counts.items(), key=lambda x: (-x[1], x[0]))[:4]])
    summary = f"大盘状态={regime}，盘面结构={structure_text}，图形分={avg_score:.1f}，建议总权益仓位={exposure:.0%}"
    if note_text:
        summary += f"，盘面特征：{note_text}"
    if date_warning:
        summary += f"，数据提示：{date_warning}"
    return bot.MarketState(date, avg_score, regime, exposure, details, summary, market_ret20, market_ret60)


def build_daily_candidates(
    date: pd.Timestamp,
    pool: pd.DataFrame,
    ind_map: Dict[str, pd.DataFrame],
    cfg: Dict[str, Any],
    market: bot.MarketState,
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for _, r in pool.iterrows():
        code = str(r.get("code", "")).zfill(6)
        name = str(r.get("name", ""))
        sector = str(r.get("sector", "未分组") or "未分组")
        try:
            ind = slice_ind(ind_map.get(code), date)
            provider = getattr(ind_map.get(code), "attrs", {}).get("data_provider", "preloaded") if ind_map.get(code) is not None else ""
            warning = getattr(ind_map.get(code), "attrs", {}).get("data_warning", "") if ind_map.get(code) is not None else ""
            m = compute_metrics_from_indicators(code, name, ind, cfg, market, provider=provider, warning=warning)
            m["sector"] = sector
            rows.append(m)
        except Exception as exc:
            rows.append({"code": code, "name": name, "sector": sector, "ok_base": False, "filter_reason": f"数据错误：{exc}"})
    metrics = pd.DataFrame(rows)
    metrics = bot.compute_sector_context(metrics, cfg)
    candidates = bot.score_candidates(metrics, cfg)
    return candidates


def first_index_dates(index_inds: Dict[str, pd.DataFrame], start: str, end: str) -> List[pd.Timestamp]:
    if not index_inds:
        return []
    first = next(iter(index_inds.values()))
    if first is None or first.empty:
        return []
    start_ts = ymd_to_ts(start)
    end_ts = ymd_to_ts(end)
    dates = first.loc[(first["date"] >= start_ts) & (first["date"] <= end_ts), "date"].dropna().tolist()
    return [pd.Timestamp(x) for x in dates]


def execute_sell(
    positions: Dict[str, Dict[str, Any]],
    code: str,
    shares: int,
    price: float,
    date: pd.Timestamp,
    reason: str,
    cash: float,
    trades: List[Dict[str, Any]],
    cfg: BacktestConfig,
) -> float:
    if code not in positions or shares <= 0:
        return cash
    pos = positions[code]
    shares = min(int(shares), int(pos.get("shares", 0)))
    if shares <= 0:
        return cash
    px = apply_slippage(float(price), "sell", cfg)
    gross = px * shares
    fee = cost_sell(gross, cfg)
    cash += gross - fee
    entry_px = bot.safe_float(pos.get("entry_price"))
    entry_fee_alloc = bot.safe_float(pos.get("entry_fee_per_share"), 0.0) * shares
    pnl = gross - fee - entry_px * shares - entry_fee_alloc
    ret = pnl / max(1e-9, entry_px * shares + entry_fee_alloc)
    trades.append({
        "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
        "code": code,
        "name": pos.get("name", ""),
        "side": "SELL",
        "shares": shares,
        "price": round(px, 4),
        "gross": round(gross, 2),
        "fee_tax": round(fee, 2),
        "cash_after": round(cash, 2),
        "reason": reason,
        "entry_date": pos.get("entry_date", ""),
        "entry_price": round(entry_px, 4) if np.isfinite(entry_px) else np.nan,
        "pnl": round(pnl, 2),
        "return_pct": round(ret * 100, 2),
        "setup_type": pos.get("setup_type", ""),
        "signal_date": pos.get("signal_date", ""),
    })
    pos["shares"] = int(pos.get("shares", 0)) - shares
    pos["realized_pnl"] = bot.safe_float(pos.get("realized_pnl"), 0.0) + pnl
    if int(pos.get("shares", 0)) <= 0:
        positions.pop(code, None)
    return cash


def generate_pending_orders(
    date: pd.Timestamp,
    allocated: pd.DataFrame,
    positions: Dict[str, Dict[str, Any]],
    equity: float,
    max_positions: int,
) -> List[Dict[str, Any]]:
    pending: List[Dict[str, Any]] = []
    if allocated is None or allocated.empty:
        return pending
    held = set(positions.keys())
    free_slots = max(0, max_positions - len(held))
    if free_slots <= 0:
        return pending
    for _, r in allocated.iterrows():
        code = str(r.get("code", "")).zfill(6)
        if code in held:
            continue
        target_cash = bot.safe_float(r.get("target_cash"), 0.0)
        if target_cash <= 0:
            target_cash = equity * bot.safe_float(r.get("target_weight"), 0.0)
        if target_cash <= 0:
            continue
        pending.append({
            "signal_date": pd.Timestamp(date).strftime("%Y-%m-%d"),
            "code": code,
            "name": str(r.get("name", "")),
            "sector": str(r.get("sector", "")),
            "target_cash": target_cash,
            "signal_close": bot.safe_float(r.get("close")),
            "stop_loss": bot.safe_float(r.get("stop_loss")),
            "take_profit_1": bot.safe_float(r.get("take_profit_1")),
            "take_profit_2": bot.safe_float(r.get("take_profit_2")),
            "score": bot.safe_float(r.get("score")),
            "setup_type": str(r.get("setup_type", "")),
            "reason": str(r.get("reason", "")),
            "score_detail": str(r.get("score_detail", "")),
        })
        if len(pending) >= free_slots:
            break
    return pending


def execute_pending_buys(
    date: pd.Timestamp,
    pending: List[Dict[str, Any]],
    positions: Dict[str, Dict[str, Any]],
    bars: Dict[str, pd.DataFrame],
    cash: float,
    trades: List[Dict[str, Any]],
    cfg: BacktestConfig,
    max_positions: int,
) -> Tuple[float, List[Dict[str, Any]]]:
    if not pending:
        return cash, []
    remaining: List[Dict[str, Any]] = []
    held = set(positions.keys())
    for order in pending:
        code = str(order.get("code", "")).zfill(6)
        name = str(order.get("name", ""))
        if code in held or len(positions) >= max_positions:
            continue
        bar = get_bar(bars.get(code), date)
        if bar is None:
            remaining.append(order)
            continue
        open_px = bot.safe_float(bar.get("open"))
        if not np.isfinite(open_px) or open_px <= 0:
            continue
        if is_limit_up_open(bars.get(code), date, bar, code, name, cfg):
            trades.append({
                "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                "code": code,
                "name": name,
                "side": "SKIP_BUY",
                "shares": 0,
                "price": round(open_px, 4),
                "gross": 0,
                "fee_tax": 0,
                "cash_after": round(cash, 2),
                "reason": "开盘接近涨停，按不可稳定买入处理",
                "signal_date": order.get("signal_date", ""),
                "setup_type": order.get("setup_type", ""),
            })
            continue
        buy_px = apply_slippage(open_px, "buy", cfg)
        # 如果次日开盘极端高开超过信号收盘 8%，不追；避免回测里买到隔夜一字高开。
        signal_close = bot.safe_float(order.get("signal_close"))
        if np.isfinite(signal_close) and signal_close > 0 and buy_px / signal_close - 1.0 > 0.08:
            trades.append({
                "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                "code": code,
                "name": name,
                "side": "SKIP_BUY",
                "shares": 0,
                "price": round(buy_px, 4),
                "gross": 0,
                "fee_tax": 0,
                "cash_after": round(cash, 2),
                "reason": "次日高开超过8%，跳过追高",
                "signal_date": order.get("signal_date", ""),
                "setup_type": order.get("setup_type", ""),
            })
            continue
        affordable_cash = min(float(order.get("target_cash", 0.0)), cash * 0.98)
        shares = int(math.floor(affordable_cash / max(buy_px, 1e-9) / 100) * 100)
        if shares < 100:
            continue
        gross = shares * buy_px
        fee = cost_buy(gross, cfg)
        if gross + fee > cash:
            shares = int(math.floor((cash / (1.0 + cfg.commission_rate)) / max(buy_px, 1e-9) / 100) * 100)
            gross = shares * buy_px
            fee = cost_buy(gross, cfg)
        if shares < 100 or gross + fee > cash:
            continue
        cash -= gross + fee
        stop = bot.safe_float(order.get("stop_loss"))
        if not np.isfinite(stop) or stop <= 0 or stop >= buy_px:
            stop = buy_px * 0.92
        risk = max(0.01, buy_px - stop)
        if cfg.recalc_targets_from_entry:
            # 严格版：必须用真实成交买入价重算 R，不再沿用信号日收盘价推导的止盈位。
            tp1 = buy_px + 1.5 * risk
            tp2 = buy_px + 3.0 * risk
        else:
            tp1 = bot.safe_float(order.get("take_profit_1"))
            tp2 = bot.safe_float(order.get("take_profit_2"))
            if not np.isfinite(tp1) or tp1 <= buy_px:
                tp1 = buy_px + 1.5 * risk
            if not np.isfinite(tp2) or tp2 <= buy_px:
                tp2 = buy_px + 3.0 * risk
        positions[code] = {
            "code": code,
            "name": name,
            "sector": order.get("sector", ""),
            "shares": shares,
            "entry_date": pd.Timestamp(date).strftime("%Y-%m-%d"),
            "signal_date": order.get("signal_date", ""),
            "entry_price": buy_px,
            "entry_fee_per_share": fee / shares if shares else 0.0,
            "stop_loss": stop,
            "initial_stop": stop,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
            "tp1_done": False,
            "highest_close": buy_px,
            "last_price": buy_px,
            "setup_type": order.get("setup_type", ""),
            "score": order.get("score", np.nan),
            "reason": order.get("reason", ""),
        }
        held.add(code)
        trades.append({
            "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
            "code": code,
            "name": name,
            "side": "BUY",
            "shares": shares,
            "price": round(buy_px, 4),
            "gross": round(gross, 2),
            "fee_tax": round(fee, 2),
            "cash_after": round(cash, 2),
            "reason": order.get("reason", ""),
            "signal_date": order.get("signal_date", ""),
            "setup_type": order.get("setup_type", ""),
            "score": round(bot.safe_float(order.get("score")), 2),
            "stop_loss": round(stop, 4),
            "take_profit_1": round(tp1, 4),
            "take_profit_2": round(tp2, 4),
        })
    return cash, []


def queue_exit_order(
    pending_sells: List[Dict[str, Any]],
    date: pd.Timestamp,
    code: str,
    pos: Dict[str, Any],
    reason: str,
) -> None:
    if any(str(o.get("code", "")).zfill(6) == str(code).zfill(6) for o in pending_sells):
        return
    pending_sells.append({
        "signal_date": pd.Timestamp(date).strftime("%Y-%m-%d"),
        "code": str(code).zfill(6),
        "name": pos.get("name", ""),
        "shares": int(pos.get("shares", 0)),
        "reason": reason,
    })
    pos["pending_exit_reason"] = reason
    pos["pending_exit_signal_date"] = pd.Timestamp(date).strftime("%Y-%m-%d")


def execute_pending_sells(
    date: pd.Timestamp,
    pending: List[Dict[str, Any]],
    positions: Dict[str, Dict[str, Any]],
    bars: Dict[str, pd.DataFrame],
    cash: float,
    trades: List[Dict[str, Any]],
    cfg: BacktestConfig,
) -> Tuple[float, List[Dict[str, Any]]]:
    if not pending:
        return cash, []
    remaining: List[Dict[str, Any]] = []
    for order in pending:
        code = str(order.get("code", "")).zfill(6)
        pos = positions.get(code)
        if not pos:
            continue
        bar = get_bar(bars.get(code), date)
        if bar is None:
            remaining.append(order)
            continue
        open_px = bot.safe_float(bar.get("open"))
        if not np.isfinite(open_px) or open_px <= 0:
            remaining.append(order)
            continue
        if is_limit_down_locked(bars.get(code), date, bar, code, str(pos.get("name", "")), cfg):
            # 趋势/大盘等收盘信号转成次日开盘卖，如果次日一字跌停或近似封死，按卖不出处理并继续挂起。
            last_block = str(pos.get("_last_blocked_exit_date", ""))
            if last_block != pd.Timestamp(date).strftime("%Y-%m-%d"):
                trades.append({
                    "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                    "code": code,
                    "name": pos.get("name", ""),
                    "side": "BLOCKED_SELL",
                    "shares": int(pos.get("shares", 0)),
                    "price": round(open_px, 4),
                    "gross": 0,
                    "fee_tax": 0,
                    "cash_after": round(cash, 2),
                    "reason": str(order.get("reason", "")) + "；次日开盘接近跌停，按无法成交处理",
                    "signal_date": order.get("signal_date", ""),
                    "setup_type": pos.get("setup_type", ""),
                })
                pos["_last_blocked_exit_date"] = pd.Timestamp(date).strftime("%Y-%m-%d")
            remaining.append(order)
            continue
        reason = str(order.get("reason", "")) + "（次日开盘执行）"
        cash = execute_sell(positions, code, int(pos.get("shares", 0)), open_px, date, reason, cash, trades, cfg)
        if code in positions:
            positions[code].pop("pending_exit_reason", None)
            positions[code].pop("pending_exit_signal_date", None)
    return cash, remaining


def manage_positions(
    date: pd.Timestamp,
    positions: Dict[str, Dict[str, Any]],
    bars: Dict[str, pd.DataFrame],
    ind_map: Dict[str, pd.DataFrame],
    market: bot.MarketState,
    cash: float,
    trades: List[Dict[str, Any]],
    cfg: BacktestConfig,
    pending_sells: Optional[List[Dict[str, Any]]] = None,
) -> float:
    if pending_sells is None:
        pending_sells = []
    for code in list(positions.keys()):
        pos = positions.get(code)
        if not pos:
            continue
        bar = get_bar(bars.get(code), date)
        if bar is None:
            continue
        open_px = bot.safe_float(bar.get("open"))
        high_px = bot.safe_float(bar.get("high"))
        low_px = bot.safe_float(bar.get("low"))
        close_px = bot.safe_float(bar.get("close"))
        if not np.isfinite(close_px) or close_px <= 0:
            continue
        pos["last_price"] = close_px
        pos["highest_close"] = max(bot.safe_float(pos.get("highest_close"), close_px), close_px)
        stop = bot.safe_float(pos.get("stop_loss"))

        # 盘中止损：如果当日是近似一字跌停/封死，按无法成交处理，持仓继续保留。
        if np.isfinite(low_px) and np.isfinite(stop) and low_px <= stop:
            if is_limit_down_locked(bars.get(code), date, bar, code, str(pos.get("name", "")), cfg):
                last_block = str(pos.get("_last_blocked_stop_date", ""))
                if last_block != pd.Timestamp(date).strftime("%Y-%m-%d"):
                    trades.append({
                        "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                        "code": code,
                        "name": pos.get("name", ""),
                        "side": "BLOCKED_SELL",
                        "shares": int(pos.get("shares", 0)),
                        "price": round(open_px, 4) if np.isfinite(open_px) else np.nan,
                        "gross": 0,
                        "fee_tax": 0,
                        "cash_after": round(cash, 2),
                        "reason": "止损触发但开盘接近跌停，按无法成交处理",
                        "entry_date": pos.get("entry_date", ""),
                        "entry_price": round(bot.safe_float(pos.get("entry_price")), 4),
                        "setup_type": pos.get("setup_type", ""),
                        "signal_date": pos.get("signal_date", ""),
                    })
                    pos["_last_blocked_stop_date"] = pd.Timestamp(date).strftime("%Y-%m-%d")
                # 当天真实情况下很难卖出，直接进入下一持仓，不继续假设止盈或趋势退出成交。
                continue
            exit_px = open_px if np.isfinite(open_px) and open_px < stop else stop
            cash = execute_sell(positions, code, int(pos.get("shares", 0)), exit_px, date, "止损", cash, trades, cfg)
            continue

        # 分批止盈：1.5R 卖一半，3R 全出。日线无法知道先后顺序，上面先判止损是偏保守处理。
        tp1 = bot.safe_float(pos.get("take_profit_1"))
        tp2 = bot.safe_float(pos.get("take_profit_2"))
        if np.isfinite(high_px) and np.isfinite(tp2) and high_px >= tp2:
            cash = execute_sell(positions, code, int(pos.get("shares", 0)), tp2, date, "3R止盈", cash, trades, cfg)
            continue
        pos = positions.get(code)
        if not pos:
            continue
        if (not bool(pos.get("tp1_done", False))) and np.isfinite(high_px) and np.isfinite(tp1) and high_px >= tp1:
            shares_half = int(math.floor(int(pos.get("shares", 0)) / 2 / 100) * 100)
            if shares_half >= 100:
                cash = execute_sell(positions, code, shares_half, tp1, date, "1.5R部分止盈", cash, trades, cfg)
                if code in positions:
                    positions[code]["tp1_done"] = True
                    positions[code]["stop_loss"] = max(bot.safe_float(positions[code].get("stop_loss")), bot.safe_float(positions[code].get("entry_price")))

        pos = positions.get(code)
        if not pos:
            continue
        if pos.get("pending_exit_reason"):
            # 已经有收盘退出信号挂起，等待次日开盘，避免重复挂单。
            continue

        # 趋势退出：严格执行时，收盘只产生卖出信号，下一交易日开盘执行。
        if cfg.trend_exit:
            last_ind = get_last_ind_row(ind_map.get(code), date)
            if last_ind is not None:
                ma20 = bot.safe_float(last_ind.get("ma20"))
                ma60 = bot.safe_float(last_ind.get("ma60"))
                entry_ts = pd.Timestamp(pos.get("entry_date"))
                hold_days = max(0, (pd.Timestamp(date) - entry_ts).days)
                if np.isfinite(ma60) and close_px < ma60:
                    if cfg.strict_execution:
                        queue_exit_order(pending_sells, date, code, pos, "跌破MA60趋势退出")
                    else:
                        cash = execute_sell(positions, code, int(pos.get("shares", 0)), close_px, date, "跌破MA60趋势退出", cash, trades, cfg)
                    continue
                if np.isfinite(ma20) and close_px < ma20 and hold_days >= 3:
                    if cfg.strict_execution:
                        queue_exit_order(pending_sells, date, code, pos, "跌破MA20趋势退出")
                    else:
                        cash = execute_sell(positions, code, int(pos.get("shares", 0)), close_px, date, "跌破MA20趋势退出", cash, trades, cfg)
                    continue

        pos = positions.get(code)
        if not pos:
            continue
        if cfg.market_exit and market.regime == "weak":
            if cfg.strict_execution:
                queue_exit_order(pending_sells, date, code, pos, "大盘弱势退出")
            else:
                cash = execute_sell(positions, code, int(pos.get("shares", 0)), close_px, date, "大盘弱势退出", cash, trades, cfg)
            continue

        pos = positions.get(code)
        if not pos:
            continue
        if cfg.max_hold_days > 0:
            entry_ts = pd.Timestamp(pos.get("entry_date"))
            hold_days = max(0, (pd.Timestamp(date) - entry_ts).days)
            if hold_days >= cfg.max_hold_days:
                reason = f"持仓超过{cfg.max_hold_days}天"
                if cfg.strict_execution:
                    queue_exit_order(pending_sells, date, code, pos, reason)
                else:
                    cash = execute_sell(positions, code, int(pos.get("shares", 0)), close_px, date, reason, cash, trades, cfg)
                continue
    return cash


def exposure_series(equity_df: pd.DataFrame) -> pd.Series:
    return pd.to_numeric(equity_df.get("exposure", pd.Series(dtype=float)), errors="coerce").replace([np.inf, -np.inf], np.nan)


def invested_day_mask(equity_df: pd.DataFrame, exposure: Optional[pd.Series] = None) -> pd.Series:
    """交易日是否有持仓。优先用持仓数量判断，兼容旧输出时退回仓位比例。"""
    if exposure is None:
        exposure = exposure_series(equity_df)
    if "positions" in equity_df:
        positions = pd.to_numeric(equity_df["positions"], errors="coerce").fillna(0)
        return positions > 0
    return exposure.fillna(0) > 1e-9


def compute_yearly_exposure(equity_df: pd.DataFrame) -> pd.DataFrame:
    if equity_df.empty or "date" not in equity_df:
        return pd.DataFrame()
    df = equity_df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].notna()].copy()
    if df.empty:
        return pd.DataFrame()
    df["year"] = df["date"].dt.year.astype(int)
    rows: List[Dict[str, Any]] = []
    for year, g in df.groupby("year", sort=True):
        exposure = exposure_series(g)
        invested = invested_day_mask(g, exposure)
        total_days = int(len(g))
        invested_days = int(invested.sum())
        empty_days = total_days - invested_days
        rows.append({
            "year": int(year),
            "trading_days": total_days,
            "invested_days": invested_days,
            "empty_days": empty_days,
            "invested_day_ratio": invested_days / total_days if total_days else np.nan,
            "avg_exposure": float(exposure[invested].mean()) if invested_days > 0 else np.nan,
            "avg_exposure_all_days": float(exposure.mean()) if total_days > 0 else np.nan,
        })
    return pd.DataFrame(rows)


def compute_summary(equity_df: pd.DataFrame, trades_df: pd.DataFrame, initial_account: float, benchmark_return: float = np.nan) -> Dict[str, Any]:
    if equity_df.empty:
        return {}
    start_equity = float(initial_account)
    end_equity = float(equity_df["total_equity"].iloc[-1])
    total_return = end_equity / start_equity - 1.0
    days = max(1, (pd.Timestamp(equity_df["date"].iloc[-1]) - pd.Timestamp(equity_df["date"].iloc[0])).days)
    cagr = (end_equity / start_equity) ** (365.0 / days) - 1.0 if end_equity > 0 and start_equity > 0 else np.nan
    daily_ret = pd.to_numeric(equity_df["total_equity"], errors="coerce").pct_change().dropna()
    sharpe = np.nan
    if not daily_ret.empty and daily_ret.std() > 0:
        sharpe = daily_ret.mean() / daily_ret.std() * math.sqrt(252)
    max_dd = float(pd.to_numeric(equity_df["drawdown"], errors="coerce").min()) if "drawdown" in equity_df else np.nan
    sells = trades_df[trades_df.get("side", pd.Series(dtype=str)).astype(str).eq("SELL")].copy() if not trades_df.empty else pd.DataFrame()
    wins = int((pd.to_numeric(sells.get("pnl", pd.Series(dtype=float)), errors="coerce") > 0).sum()) if not sells.empty else 0
    losses = int((pd.to_numeric(sells.get("pnl", pd.Series(dtype=float)), errors="coerce") <= 0).sum()) if not sells.empty else 0
    win_rate = wins / max(1, wins + losses)
    gross_profit = float(pd.to_numeric(sells.loc[pd.to_numeric(sells.get("pnl", pd.Series(dtype=float)), errors="coerce") > 0, "pnl"], errors="coerce").sum()) if not sells.empty else 0.0
    gross_loss = float(-pd.to_numeric(sells.loc[pd.to_numeric(sells.get("pnl", pd.Series(dtype=float)), errors="coerce") < 0, "pnl"], errors="coerce").sum()) if not sells.empty else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.nan
    exposure = exposure_series(equity_df)
    invested = invested_day_mask(equity_df, exposure)
    invested_days = int(invested.sum())
    total_days = int(len(equity_df))
    avg_exposure = float(exposure[invested].mean()) if invested_days > 0 else np.nan
    avg_exposure_all_days = float(exposure.mean()) if total_days > 0 else np.nan
    return {
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "trade_count": int(len(sells)),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_exposure": avg_exposure,
        "avg_exposure_all_days": avg_exposure_all_days,
        "invested_days": invested_days,
        "empty_days": total_days - invested_days,
        "trading_days": total_days,
        "benchmark_return": benchmark_return,
        "excess_return": total_return - benchmark_return if np.isfinite(benchmark_return) else np.nan,
    }


def fmt_pct(x: Any) -> str:
    v = bot.safe_float(x)
    return "--" if not np.isfinite(v) else f"{v:.2%}"


def fmt_num(x: Any, digits: int = 2) -> str:
    v = bot.safe_float(x)
    return "--" if not np.isfinite(v) else f"{v:,.{digits}f}"


def fmt_code(x: Any) -> str:
    s = str(x).strip()
    if not s or s.lower() == "nan":
        return ""
    digits = re.sub(r"\D", "", s)
    if digits and len(digits) <= 6:
        return digits.zfill(6)
    return s


def write_summary_report(
    out_dir: Path,
    summary: Dict[str, Any],
    equity_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    signal_rows: pd.DataFrame,
    positions: Dict[str, Dict[str, Any]],
    args: argparse.Namespace,
) -> Path:
    lines: List[str] = []
    lines.append("# A股 v6.6 严格执行回测报告\n")
    lines.append(f"- 初始资金：{float(args.account):,.2f}")
    lines.append(f"- 回测区间：{args.start} ~ {args.end}")
    lines.append("- 成交假设：收盘后出买入信号，下一交易日开盘买入；止损/止盈用日线高低价近似；MA20/MA60/大盘弱势等收盘确认退出，默认下一交易日开盘卖出。")
    if getattr(args, "legacy_same_day_close_exit", False):
        lines.append("- 执行模式：legacy，同日收盘趋势/大盘退出，偏乐观，仅用于和旧版对比。")
    else:
        lines.append("- 执行模式：strict，趋势/大盘/持仓天数退出均延迟到次日开盘；粗略模拟涨停买不进、跌停卖不出。")
    lines.append(f"- 手续费参数：买卖佣金 {float(args.commission_rate):.4%}；卖出印花税 {float(args.stamp_tax_rate):.4%}；单边滑点 {float(args.slippage_bps):.1f} bps。")
    if not getattr(args, "no_recalc_targets_from_entry", False):
        lines.append("- 止盈计算：按实际成交买入价重新计算 1.5R/3R，不再沿用信号日收盘价。")
    lines.append("")
    lines.append("## 结果概览")
    lines.append(f"- 期末权益：{fmt_num(summary.get('end_equity'))}")
    lines.append(f"- 总收益率：{fmt_pct(summary.get('total_return'))}")
    lines.append(f"- 年化收益率：{fmt_pct(summary.get('cagr'))}")
    lines.append(f"- 最大回撤：{fmt_pct(summary.get('max_drawdown'))}")
    lines.append(f"- Sharpe：{fmt_num(summary.get('sharpe'))}")
    lines.append(f"- 卖出成交记录数：{int(summary.get('trade_count', 0))}")
    lines.append(f"- 胜率：{fmt_pct(summary.get('win_rate'))}")
    lines.append(f"- 盈亏比 Profit Factor：{fmt_num(summary.get('profit_factor'))}")
    lines.append(f"- 平均仓位（非空仓日）：{fmt_pct(summary.get('avg_exposure'))}")
    lines.append(f"- 平均仓位（全样本，含空仓日，仅参考）：{fmt_pct(summary.get('avg_exposure_all_days'))}")
    lines.append(f"- 非空仓交易日：{int(summary.get('invested_days', 0))}/{int(summary.get('trading_days', 0))}，空仓日 {int(summary.get('empty_days', 0))} 天")
    if np.isfinite(bot.safe_float(summary.get("benchmark_return"))):
        lines.append(f"- 基准收益：{fmt_pct(summary.get('benchmark_return'))}")
        lines.append(f"- 超额收益：{fmt_pct(summary.get('excess_return'))}")
    lines.append("")

    yearly_exposure = compute_yearly_exposure(equity_df)
    if not yearly_exposure.empty:
        lines.append("## 年度仓位统计")
        lines.append("")
        lines.append("| 年份 | 交易日 | 非空仓日 | 空仓日 | 非空仓日占比 | 非空仓日平均仓位 | 全样本平均仓位 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for _, r in yearly_exposure.iterrows():
            lines.append(
                f"| {int(r.get('year'))} | "
                f"{int(r.get('trading_days', 0))} | "
                f"{int(r.get('invested_days', 0))} | "
                f"{int(r.get('empty_days', 0))} | "
                f"{fmt_pct(r.get('invested_day_ratio'))} | "
                f"{fmt_pct(r.get('avg_exposure'))} | "
                f"{fmt_pct(r.get('avg_exposure_all_days'))} |"
            )
        lines.append("")

    if not trades_df.empty:
        lines.append("## 退出原因统计")
        sells = trades_df[trades_df["side"].astype(str).eq("SELL")].copy()
        if not sells.empty:
            counts = sells["reason"].astype(str).value_counts().head(10)
            for reason, cnt in counts.items():
                lines.append(f"- {reason}：{cnt} 次")
        lines.append("")
        lines.append("## 最近 20 笔成交")
        tail = trades_df.tail(20)
        for _, r in tail.iterrows():
            side = str(r.get("side", ""))
            pnl = r.get("pnl", "")
            pnl_text = "" if pd.isna(pnl) or pnl == "" else f"，PnL={fmt_num(pnl)}"
            lines.append(f"- {r.get('date','')} {side} {fmt_code(r.get('code',''))} {r.get('name','')} {r.get('shares','')}股 @ {r.get('price','')}{pnl_text}，{r.get('reason','')}")
        lines.append("")

    lines.append("## 期末持仓")
    if not positions:
        lines.append("- 无")
    else:
        for code, pos in positions.items():
            lines.append(f"- {fmt_code(code)} {pos.get('name','')}：{pos.get('shares',0)}股，成本 {fmt_num(pos.get('entry_price'),4)}，止损 {fmt_num(pos.get('stop_loss'),4)}，买点 {pos.get('setup_type','')}")
    lines.append("")

    lines.append("## 输出文件")
    lines.append("- latest_backtest_equity.csv：每日权益曲线")
    lines.append("- latest_backtest_trades.csv：成交明细")
    lines.append("- latest_backtest_signals.csv：历史信号/候选记录")
    lines.append("- latest_backtest_open_positions.csv：期末持仓")
    lines.append("- latest_backtest_summary.csv：概要指标，avg_exposure 为非空仓日平均仓位")
    lines.append("- latest_backtest_yearly_exposure.csv：年度仓位统计")
    lines.append("")
    lines.append("注意：这是日线级别模拟，虽然已粗略处理涨停买不进、跌停卖不出，但仍无法完整模拟排队成交、封单量、停复牌、盘口冲击和真实税费差异。实盘前应先用小仓位或模拟盘连续验证。")

    path = out_dir / "latest_backtest_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "latest_backtest_message.txt").write_text("\n".join(lines[:35]), encoding="utf-8")
    return path


def run_backtest(args: argparse.Namespace) -> int:
    cfg = bot.load_config(args.config)
    cfg["data"]["use_realtime_tail"] = False
    if args.market_index:
        cfg["data"]["market_indices"] = [x.strip() for x in args.market_index.split(",") if x.strip()]
    end = args.end or bot.today_yyyymmdd()
    start = args.start or date_back_from_ymd(end, int(float(args.years) * 365))
    fetch_start = args.fetch_start or date_back_from_ymd(start, int(args.lookback_days))
    args.start = start
    args.end = end
    cfg["data"]["start_date"] = fetch_start
    cfg["data"]["end_date"] = end
    if args.no_sector_autofill:
        cfg.setdefault("strategy", {}).setdefault("sector", {})["auto_fill"] = False

    out_dir = bot.ensure_dir(args.out)
    bt_cfg = BacktestConfig(
        account=float(args.account),
        start=start,
        end=end,
        fetch_start=fetch_start,
        commission_rate=float(args.commission_rate),
        stamp_tax_rate=float(args.stamp_tax_rate),
        slippage_bps=float(args.slippage_bps),
        max_hold_days=int(args.max_hold_days),
        trend_exit=not bool(args.no_trend_exit),
        market_exit=not bool(args.no_market_exit),
        max_signal_rows=int(args.max_signal_rows),
        strict_execution=not bool(args.legacy_same_day_close_exit),
        recalc_targets_from_entry=not bool(args.no_recalc_targets_from_entry),
        block_limit_up_buys=not bool(args.allow_limit_up_buy),
        block_limit_down_sells=not bool(args.allow_limit_down_sell),
        limit_tolerance_pct=float(args.limit_tolerance_pct),
    )

    print(f"[回测] 区间 {start} ~ {end}，初始资金 {args.account:,.2f}，预取历史从 {fetch_start}")
    fetcher = bot.AkshareFetcher(cfg, refresh=bool(args.refresh))
    pool = bot.enrich_pool_sectors(bot.read_stock_pool(args.pool), args.pool, cfg, fetcher=fetcher)
    if args.limit and args.limit > 0:
        pool = pool.head(int(args.limit)).copy()
    name_map = {}
    if pool["name"].fillna("").astype(str).str.strip().eq("").any():
        try:
            name_map = bot.fetch_name_map(fetcher)
        except Exception:
            name_map = {}
        pool["name"] = pool.apply(lambda r: r["name"] if str(r["name"]).strip() else name_map.get(r["code"], ""), axis=1)
    print(f"[回测] 股票池 {len(pool)} 只；板块列已补全：{pool['sector'].fillna('').astype(str).str.strip().ne('').sum()} 只")

    # 预取大盘指数
    index_inds: Dict[str, pd.DataFrame] = {}
    index_symbols = cfg["data"].get("market_indices") or ["sh000001"]
    if isinstance(index_symbols, str):
        index_symbols = [x.strip() for x in index_symbols.split(",") if x.strip()]
    for sym in index_symbols:
        try:
            raw = fetcher.index_hist(sym)
            ind = bot.add_indicators(raw, atr_period=int(cfg["strategy"].get("atr_period", 14)))
            index_inds[sym] = ind
            print(f"[回测] 指数 {sym} K线 {len(ind)} 行")
        except Exception as exc:
            print(f"[警告] 指数 {sym} 获取失败：{exc}")
    if not index_inds:
        raise RuntimeError("大盘指数数据获取失败，无法回测")

    # 预取个股 K 线并预计算指标。
    raw_bars: Dict[str, pd.DataFrame] = {}
    ind_map: Dict[str, pd.DataFrame] = {}
    errors: List[Dict[str, str]] = []
    for n, (_, r) in enumerate(pool.iterrows(), start=1):
        code = str(r.get("code", "")).zfill(6)
        try:
            raw = fetcher.stock_hist(code, fetch_start, end, cfg["data"].get("adjust", "qfq"))
            ind = bot.add_indicators(raw, atr_period=int(cfg["strategy"].get("atr_period", 14)))
            raw_bars[code] = raw
            ind_map[code] = ind
        except Exception as exc:
            errors.append({"code": code, "name": str(r.get("name", "")), "error": str(exc)})
        if n % 20 == 0 or n == len(pool):
            print(f"[回测] 已预取个股 {n}/{len(pool)}")
    if errors:
        pd.DataFrame(errors).to_csv(out_dir / "latest_backtest_data_errors.csv", index=False, encoding="utf-8-sig")
        print(f"[警告] 个股数据失败 {len(errors)} 只，已写 latest_backtest_data_errors.csv")
    if not ind_map:
        raise RuntimeError("个股 K 线全部获取失败，无法回测")

    dates = first_index_dates(index_inds, start, end)
    if len(dates) < 30:
        raise RuntimeError("回测交易日不足，检查 start/end 或指数数据")
    max_positions = int(cfg["strategy"].get("max_positions", 5))
    cash = float(args.account)
    positions: Dict[str, Dict[str, Any]] = {}
    pending_orders: List[Dict[str, Any]] = []
    pending_sells: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    equity_rows: List[Dict[str, Any]] = []
    signal_rows_all: List[pd.DataFrame] = []
    peak_equity = cash
    last_market = None

    for idx, date in enumerate(dates):
        market = evaluate_market_asof(index_inds, cfg, date)
        last_market = market
        # 次日开盘先执行昨日收盘生成的卖出信号，再执行昨日买入信号。
        cash, pending_sells = execute_pending_sells(date, pending_sells, positions, raw_bars, cash, trades, bt_cfg)
        cash, pending_orders = execute_pending_buys(date, pending_orders, positions, raw_bars, cash, trades, bt_cfg, max_positions)
        # 当日盘中管理止损/止盈；收盘确认的趋势/大盘退出会在 strict 模式下挂到次日开盘。
        cash = manage_positions(date, positions, raw_bars, ind_map, market, cash, trades, bt_cfg, pending_sells)
        mv = value_positions(positions, raw_bars, date)
        equity = cash + mv
        peak_equity = max(peak_equity, equity)
        drawdown = equity / peak_equity - 1.0 if peak_equity > 0 else 0.0
        exposure = mv / equity if equity > 0 else 0.0

        # 收盘后生成今日信号，下一交易日执行。
        candidates = build_daily_candidates(date, pool, ind_map, cfg, market)
        data_error_count = int(candidates["filter_reason"].astype(str).str.contains("数据错误", na=False).sum()) if not candidates.empty and "filter_reason" in candidates.columns else 0
        data_error_rate = data_error_count / max(1, len(pool))
        if data_error_rate > float(cfg.get("data", {}).get("max_error_rate_for_valid_run", 0.20)):
            if not candidates.empty:
                candidates["is_signal"] = False
        signals = candidates[candidates.get("is_signal", False) == True].copy() if not candidates.empty else pd.DataFrame()
        allocated = bot.allocate_positions(signals, cfg, market, equity)
        pending_orders = generate_pending_orders(date, allocated, positions, equity, max_positions)

        if not candidates.empty:
            sig_keep = candidates[candidates.get("is_signal", False) == True].copy()
            if not sig_keep.empty:
                sig_keep["signal_date"] = pd.Timestamp(date).strftime("%Y-%m-%d")
                signal_rows_all.append(sig_keep)
            elif args.keep_all_candidates and len(signal_rows_all) < bt_cfg.max_signal_rows:
                top = candidates.sort_values("score", ascending=False).head(10).copy()
                top["signal_date"] = pd.Timestamp(date).strftime("%Y-%m-%d")
                signal_rows_all.append(top)

        equity_rows.append({
            "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
            "cash": round(cash, 2),
            "market_value": round(mv, 2),
            "total_equity": round(equity, 2),
            "drawdown": drawdown,
            "exposure": exposure,
            "positions": len(positions),
            "market_regime": market.regime,
            "market_score": market.score,
            "target_exposure": market.target_exposure,
            "signals": int(len(signals)),
            "pending_sells": int(len(pending_sells)),
            "pending_buys": int(len(pending_orders)),
        })
        if idx % 50 == 0 or idx == len(dates) - 1:
            print(f"[回测] {pd.Timestamp(date).strftime('%Y-%m-%d')} {idx+1}/{len(dates)} 权益={equity:,.2f} 持仓={len(positions)} 信号={len(signals)}")

    # 期末按最后收盘价清算未平仓，用于完整计算；同时保留期末持仓文件。
    final_date = dates[-1]
    open_positions_snapshot = pd.DataFrame(list(positions.values())) if positions else pd.DataFrame()
    if args.close_at_end:
        for code in list(positions.keys()):
            bar = get_bar(raw_bars.get(code), final_date)
            if bar is not None:
                cash = execute_sell(positions, code, int(positions[code].get("shares", 0)), bot.safe_float(bar.get("close")), final_date, "期末清算", cash, trades, bt_cfg)
        mv = value_positions(positions, raw_bars, final_date)
        equity = cash + mv
        if equity_rows:
            equity_rows[-1]["cash"] = round(cash, 2)
            equity_rows[-1]["market_value"] = round(mv, 2)
            equity_rows[-1]["total_equity"] = round(equity, 2)
            equity_rows[-1]["positions"] = len(positions)
            peak = max([float(x["total_equity"]) for x in equity_rows])
            equity_rows[-1]["drawdown"] = equity / peak - 1.0 if peak > 0 else 0.0

    equity_df = pd.DataFrame(equity_rows)
    if not equity_df.empty:
        peak = pd.to_numeric(equity_df["total_equity"], errors="coerce").cummax()
        equity_df["drawdown"] = pd.to_numeric(equity_df["total_equity"], errors="coerce") / peak - 1.0
    trades_df = pd.DataFrame(trades)
    signals_df = pd.concat(signal_rows_all, ignore_index=True) if signal_rows_all else pd.DataFrame()

    # 基准收益：默认第一个指数。
    benchmark_return = np.nan
    bench_sym = args.benchmark or (next(iter(index_inds.keys())) if index_inds else "")
    if bench_sym in index_inds:
        bench = index_inds[bench_sym]
        b = bench[(bench["date"] >= ymd_to_ts(start)) & (bench["date"] <= ymd_to_ts(end))].copy()
        if len(b) >= 2:
            benchmark_return = bot.safe_float(b["close"].iloc[-1] / b["close"].iloc[0] - 1.0)

    summary = compute_summary(equity_df, trades_df, float(args.account), benchmark_return=benchmark_return)
    yearly_exposure_df = compute_yearly_exposure(equity_df)
    equity_df.to_csv(out_dir / "latest_backtest_equity.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(out_dir / "latest_backtest_trades.csv", index=False, encoding="utf-8-sig")
    signals_df.to_csv(out_dir / "latest_backtest_signals.csv", index=False, encoding="utf-8-sig")
    open_positions_snapshot.to_csv(out_dir / "latest_backtest_open_positions.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([summary]).to_csv(out_dir / "latest_backtest_summary.csv", index=False, encoding="utf-8-sig")
    yearly_exposure_df.to_csv(out_dir / "latest_backtest_yearly_exposure.csv", index=False, encoding="utf-8-sig")
    open_pos_dict = {str(r.get("code", "")): r for r in open_positions_snapshot.to_dict("records")} if not open_positions_snapshot.empty else {}
    report_path = write_summary_report(out_dir, summary, equity_df, trades_df, signals_df, open_pos_dict, args)

    print("\n[回测完成]")
    print(f"期末权益：{fmt_num(summary.get('end_equity'))}")
    print(f"总收益率：{fmt_pct(summary.get('total_return'))}，最大回撤：{fmt_pct(summary.get('max_drawdown'))}，交易数：{int(summary.get('trade_count', 0))}")
    print(f"报告：{report_path}")
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A股 v6.6 严格执行回测器")
    parser.add_argument("--pool", required=True, help="股票池文件，至少包含 code/name；板块可自动补全")
    parser.add_argument("--config", default="config.example.yml", help="配置文件 YAML/JSON")
    parser.add_argument("--out", default="backtest_output", help="回测输出目录")
    parser.add_argument("--account", type=float, default=200000.0, help="初始资金，默认 200000")
    parser.add_argument("--years", type=float, default=2.0, help="回测最近几年，默认2年；若提供 --start 则忽略")
    parser.add_argument("--start", default="", help="回测开始日期 YYYYMMDD")
    parser.add_argument("--end", default="", help="回测结束日期 YYYYMMDD，默认今天")
    parser.add_argument("--fetch-start", default="", help="指标预热数据开始日期，默认 start 往前 lookback-days")
    parser.add_argument("--lookback-days", type=int, default=900, help="指标预热天数，默认900自然日")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，重新拉取行情")
    parser.add_argument("--limit", type=int, default=0, help="只回测股票池前 N 只，用于调试")
    parser.add_argument("--market-index", default="", help="大盘指数，逗号分隔，例如 sh000001,sz399001,sh000300")
    parser.add_argument("--benchmark", default="sh000001", help="基准指数，默认 sh000001")
    parser.add_argument("--commission-rate", type=float, default=0.0003, help="单边佣金率，默认0.03%%")
    parser.add_argument("--stamp-tax-rate", type=float, default=0.0005, help="卖出印花税率，默认0.05%%")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="单边滑点 bps，默认5")
    parser.add_argument("--max-hold-days", type=int, default=60, help="最长持仓自然日，默认60；0表示不启用")
    parser.add_argument("--no-trend-exit", action="store_true", help="关闭 MA20/MA60 趋势退出")
    parser.add_argument("--no-market-exit", action="store_true", help="关闭大盘弱势退出")
    parser.add_argument("--close-at-end", action="store_true", help="回测结束日按收盘价清算所有持仓")
    parser.add_argument("--keep-all-candidates", action="store_true", help="保存每天最高分候选，不只保存信号；文件会更大")
    parser.add_argument("--max-signal-rows", type=int, default=20000, help="保存候选最大行数提示参数")
    parser.add_argument("--no-sector-autofill", action="store_true", help="关闭 AkShare 自动补全板块")
    parser.add_argument("--legacy-same-day-close-exit", action="store_true", help="恢复旧版同日收盘趋势/大盘退出成交；默认关闭，严格模式为次日开盘卖")
    parser.add_argument("--no-recalc-targets-from-entry", action="store_true", help="不按实际买入成交价重算 1.5R/3R；默认会重算，更严格")
    parser.add_argument("--allow-limit-up-buy", action="store_true", help="允许开盘接近涨停时买入；默认按不可稳定买入跳过")
    parser.add_argument("--allow-limit-down-sell", action="store_true", help="允许开盘接近跌停且封死时卖出；默认按不可成交继续持有")
    parser.add_argument("--limit-tolerance-pct", type=float, default=0.003, help="涨跌停近似容差，默认0.003即0.3个百分点")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return run_backtest(args)
    except KeyboardInterrupt:
        print("用户中断")
        return 130
    except Exception as exc:
        print(f"[回测失败] {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
