#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股交易计划生命周期管理 v6.8

把尾盘买入信号落地成“后续怎么办”的可执行计划：
- T 日从 latest_signals_raw.csv 生成 T+1 待买入计划；
- T+1 买入后，用 portfolio.csv 同步为 ACTIVE 持仓计划；
- 后续按与严格回测一致的规则输出止损/止盈/趋势退出/大盘退出；
- 可选给出顺势加仓建议；
- 严格遵守 A 股 T+1：买入当日不提示卖出/止盈/止损。

本脚本只输出交易建议，不自动下单。
"""
from __future__ import annotations

import argparse
import math
import re
import shutil
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from . import scanner as bot
try:
    from . import position_monitor
except Exception:  # pragma: no cover
    position_monitor = None

STATE_COLUMNS = [
    "code", "name", "sector", "status", "signal_date", "entry_date", "entry_price",
    "shares", "initial_shares", "stop_loss", "initial_stop", "take_profit_1", "take_profit_2",
    "tp1_done", "setup_type", "score", "highest_close", "last_price",
    "pending_exit_reason", "pending_exit_signal_date", "source", "notes", "updated_at",
]

ACTION_COLUMNS = [
    "date", "code", "name", "action", "action_cn", "trade_shares", "price_ref", "price_range",
    "order_timing", "reason", "entry_date", "entry_price", "shares", "stop_loss", "take_profit_1",
    "take_profit_2", "tp1_done", "setup_type", "score", "status",
]


def now_cn() -> datetime:
    return bot.now_cn()


def today_str() -> str:
    return now_cn().strftime("%Y-%m-%d")


def now_tag() -> str:
    return now_cn().strftime("%Y%m%d_%H%M%S")


def safe_float(x: Any, default: float = np.nan) -> float:
    return bot.safe_float(x, default)


def normalize_code(x: Any) -> str:
    try:
        return bot.normalize_code(x)
    except Exception:
        s = str(x or "")
        m = re.search(r"(\d{6})", s)
        return m.group(1) if m else ""


def read_csv_flexible(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, dtype=str, encoding="gbk")


def write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def ensure_state(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=STATE_COLUMNS)
    out = df.copy()
    rename = {
        "股票代码": "code", "代码": "code", "证券代码": "code",
        "股票名称": "name", "名称": "name", "证券简称": "name",
        "状态": "status", "信号日期": "signal_date", "买入日期": "entry_date",
        "买入价格": "entry_price", "成本价": "entry_price", "持仓股数": "shares", "股票股数": "shares",
        "初始股数": "initial_shares", "止损价": "stop_loss", "初始止损": "initial_stop",
        "止盈1_1.5R": "take_profit_1", "止盈1": "take_profit_1", "止盈2_3R": "take_profit_2", "止盈2": "take_profit_2",
        "是否已1.5R止盈": "tp1_done", "买点类型": "setup_type", "综合分": "score",
    }
    out = out.rename(columns={c: rename.get(c, c) for c in out.columns})
    for c in STATE_COLUMNS:
        if c not in out.columns:
            out[c] = ""
    out["code"] = out["code"].map(normalize_code)
    out = out[out["code"].astype(str).str.len() == 6].copy()
    # 数值列标准化，不强制格式化，便于人工读写
    for c in ["entry_price", "shares", "initial_shares", "stop_loss", "initial_stop", "take_profit_1", "take_profit_2", "score", "highest_close", "last_price"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["status"] = out["status"].replace("", np.nan).fillna("ACTIVE")
    out["tp1_done"] = out["tp1_done"].astype(str).str.lower().isin(["true", "1", "yes", "y", "已", "是"])
    return out[STATE_COLUMNS].reset_index(drop=True)


def read_state(path: Path) -> pd.DataFrame:
    return ensure_state(read_csv_flexible(path)) if path.exists() else pd.DataFrame(columns=STATE_COLUMNS)


def save_state(path: Path, df: pd.DataFrame, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        bdir = path.parent / "trade_state_backups"
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, bdir / f"{path.stem}_{now_tag()}.csv")
    out = ensure_state(df)
    out["updated_at"] = out["updated_at"].replace("", np.nan).fillna(now_cn().strftime("%Y-%m-%d %H:%M:%S"))
    write_csv(path, out)


def normalize_signals(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    rename = {
        "股票代码": "code", "代码": "code", "股票名称": "name", "名称": "name", "板块": "sector",
        "日期": "date", "收盘价": "close", "综合分": "score", "建议买入金额": "target_cash",
        "建议股数": "target_shares", "目标仓位": "target_weight", "止损价": "stop_loss",
        "止盈1_1.5R": "take_profit_1", "止盈1": "take_profit_1", "止盈2_3R": "take_profit_2", "止盈2": "take_profit_2",
        "买点类型": "setup_type", "是否买入信号": "is_signal",
    }
    out = df.rename(columns={c: rename.get(c, c) for c in df.columns}).copy()
    if "code" not in out.columns:
        return pd.DataFrame()
    out["code"] = out["code"].map(normalize_code)
    for c in ["close", "score", "target_cash", "target_shares", "target_weight", "stop_loss", "take_profit_1", "take_profit_2"]:
        if c in out.columns:
            # 兼容中文格式百分比/逗号
            out[c] = out[c].astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False)
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "is_signal" in out.columns:
        s = out["is_signal"].astype(str).str.lower()
        out = out[s.isin(["true", "1", "yes", "y", "是"])]
    return out[out["code"].astype(str).str.len() == 6].copy()


def read_latest_signals(out_dir: Path) -> pd.DataFrame:
    raw = out_dir / "latest_signals_raw.csv"
    cn = out_dir / "latest_signals.csv"
    if raw.exists():
        return normalize_signals(read_csv_flexible(raw))
    if cn.exists():
        return normalize_signals(read_csv_flexible(cn))
    return pd.DataFrame()


def price_range(price: float, mode: str = "sell") -> str:
    if not np.isfinite(price) or price <= 0:
        return "按盘口成交"
    if mode == "urgent_sell":
        lo, hi = price * 0.985, price * 0.998
    elif mode == "sell_open":
        lo, hi = price * 0.990, price * 1.005
    elif mode == "buy":
        lo, hi = price * 0.998, price * 1.010
    elif mode == "add":
        lo, hi = price * 0.995, price * 1.008
    else:
        lo, hi = price * 0.995, price * 1.005
    return f"{lo:.2f}~{hi:.2f}"


def round_lot(shares: float, lot: int = 100) -> int:
    if not np.isfinite(shares) or shares <= 0:
        return 0
    return int(math.floor(float(shares) / lot) * lot)


def parse_date(s: Any) -> Optional[pd.Timestamp]:
    if s is None or str(s).strip() == "" or str(s).lower() == "nan":
        return None
    try:
        return pd.Timestamp(str(s).strip()).normalize()
    except Exception:
        try:
            return pd.Timestamp(datetime.strptime(str(s).strip().replace("-", ""), "%Y%m%d")).normalize()
        except Exception:
            return None


def is_t1_locked(entry_date: Any, current_date: Optional[pd.Timestamp] = None) -> bool:
    ed = parse_date(entry_date)
    cd = current_date or pd.Timestamp(today_str())
    if ed is None:
        return False
    return bool(ed >= pd.Timestamp(cd).normalize())


def infer_stop_targets(hist: pd.DataFrame, entry_price: float, cfg: Dict[str, Any]) -> Tuple[float, float, float]:
    """没有系统买入信号时，按主策略止损逻辑推断止损和 R 倍止盈。"""
    if hist is None or hist.empty or not np.isfinite(entry_price) or entry_price <= 0:
        stop = entry_price * 0.92 if np.isfinite(entry_price) and entry_price > 0 else np.nan
        return stop, entry_price + 1.5 * (entry_price - stop), entry_price + 3.0 * (entry_price - stop)
    st = cfg.get("strategy", {})
    ind = bot.add_indicators(hist, atr_period=int(st.get("atr_period", 14)))
    last = ind.iloc[-1]
    atr = safe_float(last.get("atr")); ma20 = safe_float(last.get("ma20")); low20 = safe_float(last.get("low20"))
    atr_mult = float(st.get("atr_mult", 2.5))
    min_stop = float(st.get("min_stop_pct", 0.04))
    cands = []
    if np.isfinite(atr) and atr > 0:
        cands.append(entry_price - atr_mult * atr)
    if np.isfinite(ma20) and ma20 > 0:
        cands.append(ma20 * 0.97)
    if np.isfinite(low20) and low20 > 0:
        cands.append(low20 * 0.98)
    cands = [x for x in cands if np.isfinite(x) and 0 < x < entry_price]
    stop = max(cands) if cands else entry_price * (1 - min_stop)
    if (entry_price - stop) / entry_price < min_stop:
        stop = entry_price * (1 - min_stop)
    risk = max(0.01, entry_price - stop)
    return stop, entry_price + 1.5 * risk, entry_price + 3.0 * risk


def create_pending_from_signals(state: pd.DataFrame, signals: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """把 latest_signals 写入交易状态，状态为 PENDING_BUY。"""
    state = ensure_state(state)
    if signals.empty:
        return state, pd.DataFrame(columns=ACTION_COLUMNS)
    active_codes = set(state[state["status"].astype(str).isin(["ACTIVE", "PENDING_BUY"])] ["code"].astype(str)) if not state.empty else set()
    rows = []
    created_actions = []
    for _, r in signals.iterrows():
        code = normalize_code(r.get("code"))
        if not code or code in active_codes:
            continue
        close = safe_float(r.get("close"))
        stop = safe_float(r.get("stop_loss"))
        tp1 = safe_float(r.get("take_profit_1"))
        tp2 = safe_float(r.get("take_profit_2"))
        if not np.isfinite(stop) or stop <= 0 or (np.isfinite(close) and stop >= close):
            stop = close * 0.92 if np.isfinite(close) and close > 0 else np.nan
        if np.isfinite(close) and np.isfinite(stop) and close > stop:
            risk = close - stop
            if not np.isfinite(tp1) or tp1 <= close:
                tp1 = close + 1.5 * risk
            if not np.isfinite(tp2) or tp2 <= close:
                tp2 = close + 3.0 * risk
        shares = round_lot(safe_float(r.get("target_shares"), 0))
        signal_date = str(r.get("date", "") or today_str())[:10]
        rows.append({
            "code": code, "name": str(r.get("name", "")), "sector": str(r.get("sector", "")),
            "status": "PENDING_BUY", "signal_date": signal_date, "entry_date": "", "entry_price": np.nan,
            "shares": shares, "initial_shares": shares, "stop_loss": stop, "initial_stop": stop,
            "take_profit_1": tp1, "take_profit_2": tp2, "tp1_done": False,
            "setup_type": str(r.get("setup_type", "")), "score": safe_float(r.get("score")),
            "highest_close": close, "last_price": close, "pending_exit_reason": "", "pending_exit_signal_date": "",
            "source": "latest_signals", "notes": "T日信号，等待T+1开盘确认买入", "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        })
        created_actions.append({
            "date": today_str(), "code": code, "name": str(r.get("name", "")), "action": "BUY_NEXT_OPEN", "action_cn": "次日开盘买入计划",
            "trade_shares": shares, "price_ref": close, "price_range": price_range(close, "buy"), "order_timing": "T+1开盘执行；若高开超过8%或涨停开盘则放弃",
            "reason": f"尾盘买入信号；分数{safe_float(r.get('score'), 0):.1f}；买点{str(r.get('setup_type',''))}",
            "entry_date": "", "entry_price": np.nan, "shares": shares, "stop_loss": stop, "take_profit_1": tp1,
            "take_profit_2": tp2, "tp1_done": False, "setup_type": str(r.get("setup_type", "")), "score": safe_float(r.get("score")), "status": "PENDING_BUY",
        })
        active_codes.add(code)
    if rows:
        new_rows = pd.DataFrame(rows)
        state = new_rows if state.empty else pd.concat([state, new_rows], ignore_index=True)
    return ensure_state(state), pd.DataFrame(created_actions, columns=ACTION_COLUMNS)


def sync_with_portfolio(
    state: pd.DataFrame,
    portfolio_path: Path,
    cfg: Dict[str, Any],
    fetcher: bot.AkshareFetcher,
    signals: pd.DataFrame,
    account_default: float,
) -> Tuple[pd.DataFrame, List[str]]:
    """用 portfolio.csv 同步交易状态。

    - PENDING_BUY 股票如果出现在 portfolio.csv 中，转成 ACTIVE，并用实际成本重算 TP1/TP2；
    - ACTIVE 股票股数/成本以 portfolio.csv 为准；
    - portfolio 中存在但 state 中没有的股票，按非系统持仓初始化计划；
    - ACTIVE 股票不在 portfolio 中，认为已人工卖出，标记 CLOSED。
    """
    messages: List[str] = []
    state = ensure_state(state)
    if position_monitor is None:
        raise RuntimeError("无法导入 position_monitor，不能读取 portfolio.csv")
    try:
        total_equity, cash, port = position_monitor.read_portfolio(str(portfolio_path), account_default=account_default)
    except FileNotFoundError:
        messages.append(f"未找到持仓文件 {portfolio_path}，只保留买入计划。")
        return state, messages
    except Exception as exc:
        messages.append(f"持仓文件读取失败：{exc}")
        return state, messages
    if port.empty:
        # 所有 ACTIVE 标记为可能已清仓；PENDING 保留
        active_mask = state["status"].astype(str).eq("ACTIVE")
        if active_mask.any():
            state.loc[active_mask, "status"] = "CLOSED"
            state.loc[active_mask, "notes"] = "portfolio.csv 无持仓，自动标记为已关闭"
            messages.append("portfolio.csv 为空，已把 ACTIVE 交易状态标记为 CLOSED。")
        return state, messages
    sig_by_code = {normalize_code(r.get("code")): r for _, r in signals.iterrows()} if signals is not None and not signals.empty else {}
    port_codes = set(port["code"].astype(str).map(normalize_code))
    # 标记已人工清仓
    for idx, row in state[state["status"].astype(str).eq("ACTIVE")].iterrows():
        code = normalize_code(row.get("code"))
        if code and code not in port_codes:
            state.loc[idx, "status"] = "CLOSED"
            state.loc[idx, "notes"] = "portfolio.csv 已无此持仓，视为人工清仓"
            state.loc[idx, "updated_at"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    for _, p in port.iterrows():
        code = normalize_code(p.get("code"))
        if not code:
            continue
        shares = int(float(p.get("shares", 0)))
        cost = safe_float(p.get("cost_price"))
        name = str(p.get("name", ""))
        if shares <= 0 or not np.isfinite(cost) or cost <= 0:
            continue
        m = (state["code"].astype(str).eq(code)) & (state["status"].astype(str).isin(["ACTIVE", "PENDING_BUY"]))
        if m.any():
            idx = state[m].index[-1]
            prev_shares = safe_float(state.loc[idx, "shares"], shares)
            initial = safe_float(state.loc[idx, "initial_shares"], shares)
            # PENDING -> ACTIVE，或者更新已有 ACTIVE
            was_pending = str(state.loc[idx, "status"]) == "PENDING_BUY"
            state.loc[idx, "status"] = "ACTIVE"
            state.loc[idx, "name"] = name or state.loc[idx, "name"]
            if not str(state.loc[idx, "entry_date"]).strip() or str(state.loc[idx, "entry_date"]).lower() == "nan":
                state.loc[idx, "entry_date"] = today_str()
            # 买入成交价以真实持仓成本为准；如果持仓文件改了成本，也更新 entry_price。
            state.loc[idx, "entry_price"] = cost
            state.loc[idx, "shares"] = shares
            if not np.isfinite(initial) or initial <= 0 or was_pending:
                state.loc[idx, "initial_shares"] = shares
                initial = shares
            stop = safe_float(state.loc[idx, "stop_loss"])
            if not np.isfinite(stop) or stop <= 0 or stop >= cost:
                # 优先用最新信号；否则根据日K推断
                sr = sig_by_code.get(code)
                if sr is not None:
                    stop = safe_float(sr.get("stop_loss"))
                if not np.isfinite(stop) or stop <= 0 or stop >= cost:
                    try:
                        hist = fetcher.stock_hist(code, cfg.get("data", {}).get("start_date", "20220101"), cfg.get("data", {}).get("end_date") or bot.today_yyyymmdd(), cfg.get("data", {}).get("adjust", "qfq"))
                        stop, _, _ = infer_stop_targets(hist, cost, cfg)
                    except Exception:
                        stop = cost * 0.92
                state.loc[idx, "stop_loss"] = stop
                state.loc[idx, "initial_stop"] = stop
            # 成交后强制用实际入场价重算 R 倍止盈，和严格回测一致。
            stop = safe_float(state.loc[idx, "stop_loss"])
            risk = max(0.01, cost - stop)
            tp1 = cost + 1.5 * risk
            tp2 = cost + 3.0 * risk
            state.loc[idx, "take_profit_1"] = tp1
            state.loc[idx, "take_profit_2"] = tp2
            # 如果持仓股数已明显减少，推断 TP1 或人工减仓已经执行，把剩余止损提到成本。
            if shares <= max(0, int(float(initial) * 0.65)) and not bool(state.loc[idx, "tp1_done"]):
                state.loc[idx, "tp1_done"] = True
                state.loc[idx, "stop_loss"] = max(safe_float(state.loc[idx, "stop_loss"]), cost)
                messages.append(f"{code} {name}：检测到股数从{int(initial)}降至{shares}，已视为部分止盈/减仓，剩余止损抬到成本附近。")
            if was_pending:
                messages.append(f"{code} {name}：已从 PENDING_BUY 同步为 ACTIVE，成本{cost:.3f}，止损{safe_float(state.loc[idx,'stop_loss']):.3f}。")
            elif prev_shares != shares:
                messages.append(f"{code} {name}：已同步股数 {int(prev_shares)} -> {shares}。")
            state.loc[idx, "updated_at"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
        else:
            # 非系统持仓，初始化交易状态
            try:
                hist = fetcher.stock_hist(code, cfg.get("data", {}).get("start_date", "20220101"), cfg.get("data", {}).get("end_date") or bot.today_yyyymmdd(), cfg.get("data", {}).get("adjust", "qfq"))
                stop, tp1, tp2 = infer_stop_targets(hist, cost, cfg)
                last_close = safe_float(hist.iloc[-1].get("close")) if not hist.empty else cost
            except Exception:
                stop = cost * 0.92; risk = cost - stop; tp1 = cost + 1.5 * risk; tp2 = cost + 3.0 * risk; last_close = cost
            new = {c: "" for c in STATE_COLUMNS}
            new.update({
                "code": code, "name": name, "sector": "", "status": "ACTIVE", "signal_date": "manual",
                "entry_date": today_str(), "entry_price": cost, "shares": shares, "initial_shares": shares,
                "stop_loss": stop, "initial_stop": stop, "take_profit_1": tp1, "take_profit_2": tp2,
                "tp1_done": False, "setup_type": "manual_portfolio", "score": np.nan,
                "highest_close": max(cost, last_close), "last_price": last_close,
                "source": "portfolio", "notes": "非系统信号导入持仓，止损/止盈按成本和日K推断", "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            })
            new_df = pd.DataFrame([new])
            state = new_df if state.empty else pd.concat([state, new_df], ignore_index=True)
            messages.append(f"{code} {name}：已按非系统持仓初始化交易计划，成本{cost:.3f}，止损{stop:.3f}。")
    return ensure_state(state), messages


def current_spot_map(fetcher: bot.AkshareFetcher) -> pd.DataFrame:
    try:
        return fetcher.stock_spot_all()
    except Exception:
        return pd.DataFrame()


def current_price_from_spot_or_hist(code: str, spot: pd.DataFrame, hist: pd.DataFrame) -> Tuple[float, Dict[str, float]]:
    row = {}
    if position_monitor is not None and spot is not None and not spot.empty:
        try:
            row = position_monitor.current_spot_row(spot, code)
        except Exception:
            row = {}
    price = safe_float(row.get("price")) if row else np.nan
    if not np.isfinite(price) or price <= 0:
        price = safe_float(hist.iloc[-1].get("close")) if hist is not None and not hist.empty else np.nan
    return price, row


def should_limit_down_locked(code: str, name: str, current: float, hist: pd.DataFrame) -> bool:
    """实时环境粗略判断是否接近跌停，不承诺真实可成交性。"""
    if hist is None or hist.empty or not np.isfinite(current) or current <= 0:
        return False
    try:
        prev_close = safe_float(hist.iloc[-2].get("close")) if len(hist) >= 2 else safe_float(hist.iloc[-1].get("open"))
        if not np.isfinite(prev_close) or prev_close <= 0:
            return False
        pct = current / prev_close - 1.0
        threshold = float(bot.limit_down_threshold_pct(code, name)) / 100.0
        return pct <= threshold + 0.003
    except Exception:
        return False


def add_action(actions: List[Dict[str, Any]], state_row: pd.Series, action: str, action_cn: str, shares: int,
               price_ref: float, timing: str, reason: str, mode: str = "sell") -> None:
    actions.append({
        "date": now_cn().strftime("%Y-%m-%d %H:%M"),
        "code": normalize_code(state_row.get("code")),
        "name": str(state_row.get("name", "")),
        "action": action,
        "action_cn": action_cn,
        "trade_shares": int(max(0, shares)),
        "price_ref": round(float(price_ref), 4) if np.isfinite(price_ref) else np.nan,
        "price_range": price_range(price_ref, mode) if shares else "",
        "order_timing": timing,
        "reason": reason,
        "entry_date": str(state_row.get("entry_date", "")),
        "entry_price": safe_float(state_row.get("entry_price")),
        "shares": int(safe_float(state_row.get("shares"), 0)),
        "stop_loss": safe_float(state_row.get("stop_loss")),
        "take_profit_1": safe_float(state_row.get("take_profit_1")),
        "take_profit_2": safe_float(state_row.get("take_profit_2")),
        "tp1_done": bool(state_row.get("tp1_done", False)),
        "setup_type": str(state_row.get("setup_type", "")),
        "score": safe_float(state_row.get("score")),
        "status": str(state_row.get("status", "")),
    })


def advise_active_positions(
    state: pd.DataFrame,
    cfg: Dict[str, Any],
    fetcher: bot.AkshareFetcher,
    market: bot.MarketState,
    spot: pd.DataFrame,
    mode: str = "intraday",
    allow_add: bool = True,
    total_equity: float = 200000.0,
    cash: float = 0.0,
    latest_signals: Optional[pd.DataFrame] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    state = ensure_state(state)
    actions: List[Dict[str, Any]] = []
    notes: List[str] = []
    current_date = pd.Timestamp(today_str())
    st = cfg.get("strategy", {})
    max_hold_days = int(cfg.get("trade_lifecycle", {}).get("max_hold_days", 60))
    active = state[state["status"].astype(str).eq("ACTIVE")].copy()
    latest_signals = latest_signals if latest_signals is not None else pd.DataFrame()
    signal_codes = set(latest_signals["code"].astype(str)) if not latest_signals.empty else set()
    for idx, row in active.iterrows():
        code = normalize_code(row.get("code"))
        name = str(row.get("name", ""))
        shares = int(safe_float(row.get("shares"), 0))
        if not code or shares < 100:
            continue
        try:
            hist = fetcher.stock_hist(code, cfg.get("data", {}).get("start_date", "20220101"), cfg.get("data", {}).get("end_date") or bot.today_yyyymmdd(), cfg.get("data", {}).get("adjust", "qfq"))
            ind = bot.add_indicators(hist, atr_period=int(st.get("atr_period", 14)))
            last = ind.iloc[-1]
        except Exception as exc:
            add_action(actions, row, "DATA_ERROR", "数据错误", 0, np.nan, "不交易", f"日K获取失败：{exc}", "hold")
            continue
        current, spot_row = current_price_from_spot_or_hist(code, spot, hist)
        close = safe_float(last.get("close"))
        high = safe_float(last.get("high")); low = safe_float(last.get("low"))
        ma20 = safe_float(last.get("ma20")); ma60 = safe_float(last.get("ma60"))
        state.loc[idx, "last_price"] = current if np.isfinite(current) else close
        state.loc[idx, "highest_close"] = max(safe_float(row.get("highest_close"), close), close if np.isfinite(close) else current)
        entry_date = parse_date(row.get("entry_date"))
        hold_days = (current_date - entry_date).days if entry_date is not None else 999
        if is_t1_locked(row.get("entry_date"), current_date):
            add_action(actions, row, "T1_LOCK", "T+1锁定持有", 0, current, "买入当日不允许卖出", "A股T+1：今日买入，止损/止盈/趋势退出都从下一交易日开始。", "hold")
            continue
        stop = safe_float(row.get("stop_loss"))
        tp1 = safe_float(row.get("take_profit_1"))
        tp2 = safe_float(row.get("take_profit_2"))
        tp1_done = bool(row.get("tp1_done", False))
        # 已经有收盘退出信号，提示次日开盘执行，直到用户卖出并同步 portfolio。
        pending_reason = str(row.get("pending_exit_reason", "") or "").strip()
        if pending_reason:
            add_action(actions, row, "SELL_NEXT_OPEN", "次日开盘卖出", shares, current, "开盘执行；若跌停封死需人工确认是否可成交", pending_reason, "sell_open")
            continue
        # 风险闸门：实盘建议中直接提示风险卖出，优先级高于普通止盈。
        try:
            risk_gate = bot.compute_risk_gate(ind, code, name, cfg)
            if bool(risk_gate.get("risk_gate_block", False)):
                if should_limit_down_locked(code, name, current, hist):
                    add_action(actions, row, "SELL_RISK_BLOCKED", "风险卖出但可能跌停难成交", shares, current, "尽量卖出；若封死则继续挂单/次日处理", "A股风险闸门触发：" + str(risk_gate.get("risk_gate_reason", "")), "urgent_sell")
                else:
                    add_action(actions, row, "SELL_RISK", "风险卖出", shares, current, "盘中/次日开盘均可执行，优先风控", "A股风险闸门触发：" + str(risk_gate.get("risk_gate_reason", "")), "urgent_sell")
                continue
        except Exception:
            pass
        # 盘中价格触发的止损/止盈，和回测一样可用 high/low 逻辑；实时建议用 current 触发。
        if np.isfinite(stop) and np.isfinite(current) and current <= stop:
            if should_limit_down_locked(code, name, current, hist):
                add_action(actions, row, "STOP_BLOCKED", "止损触发但可能跌停难成交", shares, current, "尽量卖出；若封死则继续挂单/次日处理", f"现价{current:.2f}跌破止损{stop:.2f}", "urgent_sell")
            else:
                add_action(actions, row, "STOP_LOSS", "止损卖出", shares, current, "盘中触发即可执行", f"现价{current:.2f}跌破止损{stop:.2f}", "urgent_sell")
            continue
        if np.isfinite(tp2) and np.isfinite(current) and current >= tp2:
            add_action(actions, row, "TAKE_PROFIT_3R", "3R止盈清仓", shares, current, "盘中触发即可执行", f"现价{current:.2f}达到3R止盈{tp2:.2f}", "sell")
            continue
        if (not tp1_done) and np.isfinite(tp1) and np.isfinite(current) and current >= tp1:
            half = round_lot(shares / 2)
            if half >= 100:
                add_action(actions, row, "TAKE_PROFIT_1_5R", "1.5R止盈半仓", half, current, "盘中触发即可执行；成交后同步持仓，剩余止损抬到成本", f"现价{current:.2f}达到1.5R止盈{tp1:.2f}", "sell")
                continue
        # 收盘确认类退出：只在 close/after_close 模式里正式挂明日卖出；盘中只提示预警。
        if mode in {"close", "after_close", "daily"}:
            if np.isfinite(ma60) and np.isfinite(close) and close < ma60:
                state.loc[idx, "pending_exit_reason"] = "跌破MA60趋势退出"
                state.loc[idx, "pending_exit_signal_date"] = today_str()
                add_action(actions, row, "SELL_NEXT_OPEN", "跌破MA60，明日开盘卖出", shares, close, "下一交易日开盘执行", f"收盘{close:.2f}跌破MA60 {ma60:.2f}", "sell_open")
                continue
            if np.isfinite(ma20) and np.isfinite(close) and close < ma20 and hold_days >= 3:
                state.loc[idx, "pending_exit_reason"] = "跌破MA20趋势退出"
                state.loc[idx, "pending_exit_signal_date"] = today_str()
                add_action(actions, row, "SELL_NEXT_OPEN", "跌破MA20，明日开盘卖出", shares, close, "下一交易日开盘执行", f"收盘{close:.2f}跌破MA20 {ma20:.2f}，持仓{hold_days}天", "sell_open")
                continue
            if market.regime == "weak":
                state.loc[idx, "pending_exit_reason"] = "大盘弱势退出"
                state.loc[idx, "pending_exit_signal_date"] = today_str()
                add_action(actions, row, "SELL_NEXT_OPEN", "大盘弱势，明日开盘卖出", shares, close, "下一交易日开盘执行", market.summary, "sell_open")
                continue
            if max_hold_days > 0 and hold_days >= max_hold_days:
                state.loc[idx, "pending_exit_reason"] = f"持仓超过{max_hold_days}天"
                state.loc[idx, "pending_exit_signal_date"] = today_str()
                add_action(actions, row, "SELL_NEXT_OPEN", "超期持仓，明日开盘卖出", shares, close, "下一交易日开盘执行", f"持仓{hold_days}天，超过最大持仓{max_hold_days}天", "sell_open")
                continue
        else:
            warn = []
            if np.isfinite(ma20) and np.isfinite(current) and current < ma20 and hold_days >= 3:
                warn.append(f"盘中跌破MA20 {ma20:.2f}，收盘若不能收回则明日开盘卖出")
            if np.isfinite(ma60) and np.isfinite(current) and current < ma60:
                warn.append(f"盘中跌破MA60 {ma60:.2f}，收盘确认则明日开盘卖出")
            if market.regime == "weak":
                warn.append("大盘弱势，收盘确认后可能触发明日开盘退出")
            if warn:
                add_action(actions, row, "WARN", "趋势/大盘预警", 0, current, "盘中不按MA破位直接卖；收盘确认后再挂次日开盘单", "；".join(warn), "hold")
                continue
        # 加仓建议：不属于严格回测默认卖出，但用于实盘计划，必须保守且依赖当前仍有买入信号/主线强。
        if allow_add and code in signal_codes and market.target_exposure > 0:
            entry = safe_float(row.get("entry_price"))
            max_add_pct = float(cfg.get("trade_lifecycle", {}).get("add_max_position_pct", 0.18))
            add_step_pct = float(cfg.get("trade_lifecycle", {}).get("add_step_pct", 0.25))
            # 只对盈利仓位、短均线多头、未过热给小步加仓建议。
            ma5 = safe_float(last.get("ma5")); ma10 = safe_float(last.get("ma10"))
            pct_chg = safe_float(spot_row.get("pct_chg"), safe_float(last.get("pct_chg")))
            mv = current * shares if np.isfinite(current) else 0
            if np.isfinite(entry) and current > entry * 1.02 and np.isfinite(ma5) and np.isfinite(ma10) and np.isfinite(ma20) and current >= ma5 >= ma10 >= ma20 and (not np.isfinite(pct_chg) or pct_chg <= 5.5):
                target_mv = total_equity * max_add_pct
                add_cash = min(max(0.0, target_mv - mv), max(0.0, cash), mv * add_step_pct)
                add_shares = round_lot(add_cash / current) if np.isfinite(current) and current > 0 else 0
                if add_shares >= 100:
                    add_action(actions, row, "ADD", "顺势小幅加仓", add_shares, current, "盘中分批限价，不追涨停/大幅拉升", "当前仍在买入信号列表，持仓盈利且5/10/20日线多头", "add")
                    continue
        add_action(actions, row, "HOLD", "持有", 0, current, "不交易", f"未触发止损、止盈、趋势退出或加仓条件；止损{stop:.2f}，1.5R {tp1:.2f}，3R {tp2:.2f}", "hold")
    return ensure_state(state), pd.DataFrame(actions, columns=ACTION_COLUMNS), notes


def format_message(actions: pd.DataFrame, state: pd.DataFrame, market: Optional[bot.MarketState], sync_notes: List[str], mode: str) -> str:
    lines = [f"A股交易计划/持仓后续操作 {now_cn().strftime('%Y-%m-%d %H:%M')}（模式：{mode}）"]
    if market is not None:
        lines.append(market.summary)
    if sync_notes:
        lines.append("同步提示：" + "；".join(sync_notes[:6]))
    if actions is None or actions.empty:
        lines.append("当前没有待执行动作，也没有 ACTIVE/PENDING_BUY 交易计划。")
    else:
        trade = actions[pd.to_numeric(actions["trade_shares"], errors="coerce").fillna(0).astype(int) > 0].copy()
        pending_buy = actions[actions["action"].astype(str).eq("BUY_NEXT_OPEN")].copy()
        if not pending_buy.empty:
            lines.append("\n待买入计划：")
            for _, r in pending_buy.iterrows():
                lines.append(f"- {r['code']} {r.get('name','')}：{r['action_cn']} {int(r['trade_shares'])}股，参考{r['price_range']}；止损{safe_float(r.get('stop_loss')):.2f}，1.5R {safe_float(r.get('take_profit_1')):.2f}，3R {safe_float(r.get('take_profit_2')):.2f}。")
        need = trade[~trade["action"].astype(str).eq("BUY_NEXT_OPEN")]
        if not need.empty:
            lines.append("\n需要执行/准备的操作：")
            for _, r in need.iterrows():
                lines.append(
                    f"- {r['code']} {r.get('name','')}：{r['action_cn']} {int(r['trade_shares'])}股，参考区间 {r['price_range']}；"
                    f"时点：{r['order_timing']}；原因：{r['reason']}"
                )
        if need.empty and pending_buy.empty:
            lines.append("当前没有需要买卖的动作。")
        info = actions[pd.to_numeric(actions["trade_shares"], errors="coerce").fillna(0).astype(int) <= 0]
        if not info.empty:
            lines.append("\n持仓状态/预警：")
            for _, r in info.head(12).iterrows():
                lines.append(f"- {r['code']} {r.get('name','')}：{r['action_cn']}；{r['reason']}")
    active_count = int((state["status"].astype(str) == "ACTIVE").sum()) if state is not None and not state.empty else 0
    pending_count = int((state["status"].astype(str) == "PENDING_BUY").sum()) if state is not None and not state.empty else 0
    lines.append(f"\n交易状态：ACTIVE {active_count} 只，PENDING_BUY {pending_count} 只。")
    lines.append("说明：本计划不自动下单；买入当日遵守A股T+1，不提示卖出；止损/止盈按盘中价触发，MA20/MA60/大盘弱势按收盘确认后次日开盘执行。")
    return "\n".join(lines)


def infer_mode_from_message(msg: str) -> str:
    if re.search(r"收盘|盘后|明天|次日|after[_ -]?close|close", msg, re.I):
        return "close"
    if re.search(r"盘中|现在|当前|此刻|10[:：]?|14[:：]?|intraday", msg, re.I):
        return "intraday"
    # 14:45 后更适合接近收盘确认；其余盘中用 intraday。
    n = now_cn()
    if n.hour >= 15:
        return "close"
    return "intraday"


def run(args: argparse.Namespace) -> Tuple[pd.DataFrame, str, Path]:
    cfg = bot.load_config(args.config)
    # 盘中管理需要实时快照；收盘模式不强制，但开启也不影响。
    if args.mode == "intraday":
        cfg.setdefault("data", {})["use_realtime_tail"] = True
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state)
    portfolio_path = Path(args.portfolio)
    signals_dir = Path(args.signals_out)
    state = read_state(state_path)
    signals = read_latest_signals(signals_dir)
    fetcher = bot.AkshareFetcher(cfg, refresh=bool(args.refresh))
    # chat 模式决定动作
    action = args.action
    msg_text = ""
    if args.message_file:
        msg_text = Path(args.message_file).read_text(encoding="utf-8", errors="ignore")
        if action == "auto":
            if re.search(r"生成.*交易计划|从.*信号.*计划|买入计划|明日.*买入", msg_text):
                action = "from_signals"
            elif re.search(r"同步.*持仓|刷新.*持仓|更新.*交易状态|同步交易", msg_text):
                action = "sync"
            else:
                action = "advise"
        if args.mode == "auto":
            args.mode = infer_mode_from_message(msg_text)
    if args.mode == "auto":
        args.mode = "intraday"
    all_actions: List[pd.DataFrame] = []
    sync_notes: List[str] = []
    if action in {"from_signals", "all"}:
        state, buy_actions = create_pending_from_signals(state, signals)
        if not buy_actions.empty:
            all_actions.append(buy_actions)
    if action in {"sync", "advise", "all"} or args.sync:
        state, notes = sync_with_portfolio(state, portfolio_path, cfg, fetcher, signals, args.account)
        sync_notes.extend(notes)
    need_market = action in {"advise", "all", "sync"}
    market = bot.evaluate_market(fetcher, cfg) if need_market else None
    try:
        spot = current_spot_map(fetcher) if need_market else pd.DataFrame()
    except Exception:
        spot = pd.DataFrame()
    # 估算账户现金；优先持仓文件中的现金。
    cash = 0.0
    if position_monitor is not None:
        try:
            total_eq, cash_val, _ = position_monitor.read_portfolio(str(portfolio_path), account_default=args.account)
            if np.isfinite(cash_val):
                cash = float(cash_val)
            if total_eq > 0:
                args.account = float(total_eq)
        except Exception:
            pass
    if action in {"advise", "all", "sync"}:
        state, act, notes = advise_active_positions(state, cfg, fetcher, market, spot, mode=args.mode, allow_add=not args.no_add, total_equity=args.account, cash=cash, latest_signals=signals)
        sync_notes.extend(notes)
        if not act.empty:
            all_actions.append(act)
    actions = pd.concat(all_actions, ignore_index=True) if all_actions else pd.DataFrame(columns=ACTION_COLUMNS)
    # 保存状态和输出
    save_state(state_path, state, backup=True)
    ts = now_tag()
    write_csv(out_dir / f"trade_actions_{ts}.csv", actions)
    write_csv(out_dir / "latest_trade_actions.csv", actions)
    write_csv(out_dir / "latest_trade_state.csv", state)
    msg = format_message(actions, state, market, sync_notes, args.mode)
    msg_path = out_dir / "latest_trade_plan.txt"
    msg_path.write_text(msg, encoding="utf-8")
    return actions, msg, msg_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="A股交易计划生命周期管理：买入后止损/止盈/退出/加仓建议")
    ap.add_argument("--action", choices=["auto", "from_signals", "sync", "advise", "all"], default="advise", help="from_signals=从买入信号生成T+1买入计划；sync=同步持仓；advise=输出后续操作；all=全流程")
    ap.add_argument("--portfolio", default="portfolio.csv", help="持仓文件")
    ap.add_argument("--state", default="trade_state.csv", help="交易计划状态文件")
    ap.add_argument("--signals-out", default="output", help="latest_signals_raw.csv 所在目录")
    ap.add_argument("--config", default="config.example.yml", help="配置文件")
    ap.add_argument("--out", default="trade_output", help="输出目录")
    ap.add_argument("--account", type=float, default=200000.0, help="默认账户权益")
    ap.add_argument("--mode", choices=["auto", "intraday", "close", "after_close", "daily"], default="intraday", help="intraday=盘中止损/止盈建议；close=收盘确认MA/大盘退出并挂次日开盘卖出")
    ap.add_argument("--sync", action="store_true", help="先用 portfolio.csv 同步持仓状态")
    ap.add_argument("--no-add", action="store_true", help="不输出加仓建议")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新拉取")
    ap.add_argument("--message-file", default="", help="小龙虾对话原文文件，action=auto 时用于意图识别")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        _, msg, _ = run(args)
        print(msg)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
