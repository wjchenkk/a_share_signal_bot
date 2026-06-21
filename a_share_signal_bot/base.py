#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股尾盘/收盘 K 线买入信号扫描器 v6.5

功能：
1) 读取股票池 CSV/TXT/XLSX；
2) 用 AkShare 拉取个股日 K 和大盘指数日 K；
3) 大盘过滤 + 板块主线 + 个股板块内强度 + 突破/回踩/天量锚点再异动 + ATR 风控；
4) 输出买入信号、目标仓位、建议股数、止损价、止盈价；
5) 可选通过企业微信/钉钉/飞书 webhook 推送文本消息；
6) 支持“小龙虾/AI Agent”通过对话命令增删查股票池、手动触发扫描；
7) 支持定时扫描后在股票池超过阈值时自动淘汰图形最弱的股票。

注意：本脚本只生成信号，不自动下单。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


DEFAULT_CONFIG: Dict[str, Any] = {
    "data": {
        "start_date": "20220101",
        "end_date": "",
        "adjust": "qfq",  # qfq / hfq / "". 尾盘实时模式会自动改成不复权，避免复权价与实时价混用。
        "market_indices": ["sh000001", "sz399001"],  # 上证指数、深证成指
        "cache_dir": "cache",
        "cache_hours": 24,
        "sleep_seconds": 0.35,
        "use_realtime_tail": False,
        # 个股日 K 默认不再优先用东方财富，避免 stock_zh_a_hist 被限流后整池失败。
        # 免费可用的数据源：tencent=腾讯 stock_zh_a_hist_tx；sina=新浪 stock_zh_a_daily；eastmoney=东方财富 stock_zh_a_hist。
        # 同花顺专业 OpenAPI 通常需要授权和账号，本脚本先预留 provider 名称，不把未授权接口硬编码进去。
        "hist_providers": ["tencent", "sina", "eastmoney"],
        "spot_providers": ["akshare_em", "efinance", "tencent_direct"],
        "spot_cache_minutes": 2,
        "prefer_pool_spot": True,  # 扫描只需要股票池实时行情，优先用按池批量 quote，避免全市场实时源拖慢。
        "two_stage_scan": True,
        "max_scan_per_run": 45,
        "prefilter_pool_when_gt": 60,
        "prefilter_keep_pct_chg_min": -6.0,
        "prefilter_keep_pct_chg_max": 8.8,
        "intraday_buy_filter": True,
        "intraday_snapshot_dir": "cache/intraday_snapshots",
        "index_providers": ["tencent", "eastmoney", "legacy"],
        "request_retries": 3,
        "retry_backoff_seconds": [1.5, 4.0, 8.0],
        "allow_stale_cache_on_error": True,
        "use_stale_cache_on_network_error": True,
        "fail_fast_network_errors": True,
        "max_error_rate_for_valid_run": 0.20,
    },
    "strategy": {
        "min_history_days": 160,
        "min_amount_ma20": 50_000_000,  # 20 日平均成交额，默认 5000 万
        "min_close": 3.0,
        "max_close": 300.0,
        "exclude_st": True,
        "score_threshold": 72.0,
        "max_positions": 8,
        "max_position_pct": 0.22,       # 单票最大仓位；v6.3 按信号等级动态调整
        "risk_per_trade_pct": 0.010,    # 单票止损触发时，组合最大亏损约 0.6%
        "atr_period": 14,
        "atr_mult": 2.5,
        "min_stop_pct": 0.04,
        "max_stop_pct": 0.12,
        # v6：主线-买点分型。sector 可以来自股票池 sector/板块/行业 列，也可以来自 sector_map.csv。
        "sector": {
            "enabled": True,
            "sector_map_path": "sector_map.csv",
            # v6.1：自动补全板块。concept_first=概念优先、行业托底；industry/sw=行业兜底。
            "auto_fill": True,
            "auto_source": "concept_first",   # concept_first / concept / industry / sw / none
            "auto_fallback_sources": ["industry", "sw"],
            "auto_write_back": True,          # 行业模式可写回 stock_pool.csv；概念模式只写缓存，避免题材固化
            "auto_map_path": "cache/auto_sector_map_concept_first.csv",
            "allow_stale_auto_map": True,     # 数据源不可用时允许用旧自动板块映射，避免冷缓存全量反查板块。
            "auto_scan_max_boards": 0,        # 0 表示扫描全部行业/概念板块；调试时可设 20
            "min_sector_size": 3,
            "mainline_score_threshold": 66.0,
            "strong_score_threshold": 55.0,
            # v6.3：板块主线从“加分项”升级为入场门槛。
            "use_board_hist_strength": True,
            "hard_gate_enabled": True,
            "min_score_for_any_buy": 55.0,
            "min_score_for_pullback": 66.0,
            "min_score_for_breakout": 60.0,
            "min_score_for_anchor": 64.0,
            "front_rs20_min": 0.60,
            "front_rs60_min": 0.55,
            "core_rs20_min": 0.75,
            "require_outperform_sector20": True,
            "require_mainline_for_pullback": True,
            "prefer_mainline_for_breakout": True
        },
        "setup": {
            "enabled": True,
            "allowed_types": ["breakout", "pullback", "volume_anchor_reaccumulation"],
            "breakout_score_threshold": 66.0,
            "pullback_score_threshold": 66.0,
            "anchor_score_threshold": 68.0,
            "volume_anchor_lookback": 20,
            "volume_anchor_min_days_ago": 3,
            "volume_anchor_max_days_ago": 20,
            "volume_anchor_amount_ratio": 3.0,
            "volume_anchor_min_pct": 7.0,
            "volume_anchor_hold_low_pct": -0.03,
            "volume_anchor_midline_pct": 0.50,
            "pullback_ma_options_strong": [5, 10],
            "pullback_ma_options_normal": [10, 20],
            "pullback_distance_pct": 0.035,
            "breakout_amount_min": 1.08,
            "breakout_amount_max": 2.60,
            "pullback_amount_max": 1.15,
            "reaccum_amount_min": 1.25,
            "breakout_need_contraction": True,
            "pullback_need_dryup": True,
            "strong_mainline_pullback_ma": [5, 10],
            "normal_mainline_pullback_ma": [10, 20]
        },
        "max_chase_day_pct": 7.5,       # 单日涨幅过大不追
        "min_day_pct": -5.5,            # 单日跌幅过大不接飞刀
        "avoid_upper_shadow_pct": 0.08, # 长上影过大过滤
        "total_exposure_strong": 0.95,
        "total_exposure_neutral": 0.65,
        "total_exposure_weak": 0.00,
        # A 股专用风险闸门：先排雷，再评分。用于过滤连续跌停、高位崩盘、暴涨后踩踏。
        "risk_gate": {
            "enabled": True,
            "limit_down_count_3_block": 1,
            "limit_down_count_6_block": 2,
            "limit_down_count_10_block": 3,
            "ret3_min": -0.12,
            "ret5_min": -0.18,
            "drawdown10_max": -0.22,
            "drawdown20_max": -0.30,
            "climax_ret30": 0.80,
            "climax_ret60": 1.20,
            "climax_drawdown10": -0.12,
            "climax_amount_ratio20": 2.8,
            "min_cooling_days_after_limit_down": 5
        },
    },
    "etf": {
        "pool": "etf_pool.csv",
        "out_dir": "etf_output",
        "cache_dir": "cache/etf",
        "start_date": "20200101",
        "end_date": "",
        "adjust": "",
        "hist_providers": ["eastmoney", "sina"],
        "cache_hours": 24,
        "sleep_seconds": 0.20,
        "request_retries": 2,
        "retry_backoff_seconds": [1.0, 3.0],
        "allow_stale_cache_on_error": True,
        "use_stale_cache_on_network_error": True,
        "fail_fast_network_errors": True,
        "min_history_days": 180,
        "min_amount_ma20": 20_000_000,
        "score_threshold": 70.0,
        "max_positions": 5,
        "max_position_pct": 0.25,
        "total_exposure": 0.90,
        "min_lot": 100,
        "trend": {
            "min_trend_score": 20.0
        },
        "setup": {
            "min_setup_score": 16.0,
            "breakout_amount_min": 1.05,
            "breakout_amount_max": 2.60,
            "pullback_ma20_distance_pct": 0.035,
            "pullback_amount_dryup_max": 1.05,
            "max_chase_day_pct": 5.5,
            "min_day_pct": -4.5
        },
        "risk": {
            "atr_period": 14,
            "atr_mult": 2.2,
            "min_stop_pct": 0.025,
            "max_stop_pct": 0.10,
            "max_atr_pct": 0.085,
            "max_drawdown120": -0.24
        },
        "rotation": {
            "min_history_days": 180,
            "min_amount_ma20": 20_000_000,
            "score_threshold": 55.0,
            "min_ret60": -0.03,
            "require_ma60": True,
            "max_positions": 5,
            "max_per_category": 2,
            "max_position_pct": 0.25,
            "max_correlation": 0.92,
            "correlation_lookback": 120,
            "strong_ret60": 0.04,
            "neutral_ret60": 0.00,
            "strong_above_ma60_rate": 0.60,
            "neutral_above_ma60_rate": 0.45,
            "strong_total_exposure": 0.90,
            "neutral_total_exposure": 0.65,
            "weak_total_exposure": 0.35,
            "defensive_asset_classes": ["defensive", "commodity"],
            "weak_defensive_bonus": 15.0,
            "min_risk_vol": 0.008
        },
        "backtest": {
            "initial_cash": 200_000,
            "years": 3,
            "rebalance": "W-FRI",
            "commission_rate": 0.0003,
            "slippage_bps": 5,
            "benchmark_code": ""
        },
    },
    "position_monitor": {
        "portfolio_path": "portfolio.csv",
        "out_dir": "position_output",
        "minute_period": "5",
        "minute_providers": ["eastmoney", "sina", "snapshot"],
        "minute_allow_snapshot_fallback": True,
        "check_windows": ["10:00-10:30", "14:00-14:30"],
        "window_only": False,
        "max_loss_pct": 0.06,
        "trend_break_reduce_pct": 0.50,
        "hard_stop_sell_pct": 1.00,
        "profit_protect_1": 0.08,
        "profit_protect_2": 0.15,
        "profit_reduce_pct": 0.33,
        "weak_intraday_reduce_pct": 0.33,
        "allow_add": True,
        "add_max_position_pct": 0.18,
        "add_step_pct": 0.25,
        "min_trade_lot": 100
    },

    "notify": {
        "webhook_url": "",
        "webhook_type": "wecom",  # wecom / dingtalk / feishu / generic
        "send": False,
    },
    "pool": {
        "auto_prune": False,          # 定时任务建议通过 --auto-prune 显式开启；手动对话扫描默认不删池子。
        "max_size": 50,               # 股票池超过该数量才触发淘汰
        "prune_count": 10,            # 每次触发时淘汰数量
        "backup_dir": "pool_backups",
    },
    "report": {
        # 默认推送/聊天只输出精简摘要；逐股原因仍会生成到文件，等你说“解释/为什么”再展示。
        "include_explanations_in_message": False,
        "show_no_signal_top": 12,      # 详细模式中展示多少只“未买入/接近达标”的解释
        "max_report_stocks": 120,      # latest_report.md 中最多写入多少只逐股解释；股票池 50 只以内会全部写入
        "show_blocker_counts": True,   # 是否统计今日未买入的主要拦截原因
    },
    "output": {
        "cleanup_on_run": True,
        "retention_days": 14,
        "max_history_files": 300,
        "keep_latest_files": True,
    },
}


@dataclass
class MarketState:
    date: str
    score: float
    regime: str
    target_exposure: float
    details: pd.DataFrame
    summary: str
    market_ret20: float
    market_ret60: float


@dataclass
class PruneReport:
    enabled: bool
    triggered: bool
    pool_size_before: int
    pool_size_after: int
    removed: pd.DataFrame
    message: str
    backup_path: str = ""


@dataclass
class ChatAction:
    kind: str  # add / remove / list / scan / explain / prune / help / unknown
    text: str
    items: List[Tuple[str, str]]
    terms: List[str]
    auto_prune: bool = False


def now_cn() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    return datetime.utcnow() + timedelta(hours=8)


def today_yyyymmdd() -> str:
    return now_cn().strftime("%Y%m%d")


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not path:
        return cfg
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in {".json"}:
        user_cfg = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("读取 YAML 配置需要安装 PyYAML：pip install pyyaml") from exc
        user_cfg = yaml.safe_load(text) or {}
    return deep_merge(cfg, user_cfg)


def normalize_code(value: Any) -> str:
    s = str(value).strip().upper()
    s = s.replace(" ", "")
    # 支持 600519.SH / SH600519 / sh600519 / 600519
    m = re.search(r"(\d{6})", s)
    if not m:
        raise ValueError(f"无法识别股票代码: {value}")
    return m.group(1).zfill(6)


def read_stock_pool(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"股票池文件不存在: {path}")

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
        raise ValueError("股票池为空")

    cols_lower = {str(c).strip().lower(): c for c in df.columns}
    code_col = None
    for candidate in ["code", "symbol", "ticker", "股票代码", "证券代码", "代码"]:
        if candidate.lower() in cols_lower:
            code_col = cols_lower[candidate.lower()]
            break
    if code_col is None:
        code_col = df.columns[0]

    name_col = None
    for candidate in ["name", "股票名称", "证券简称", "名称", "简称"]:
        if candidate.lower() in cols_lower:
            name_col = cols_lower[candidate.lower()]
            break

    sector_col = None
    for candidate in ["sector", "industry", "theme", "board", "板块", "行业", "概念", "所属板块", "所属行业"]:
        if candidate.lower() in cols_lower:
            sector_col = cols_lower[candidate.lower()]
            break

    rows = []
    for _, row in df.iterrows():
        raw = row.get(code_col)
        if pd.isna(raw):
            continue
        try:
            code = normalize_code(raw)
        except ValueError:
            continue
        name = ""
        if name_col is not None and not pd.isna(row.get(name_col)):
            name = str(row.get(name_col)).strip()
        sector = ""
        if sector_col is not None and not pd.isna(row.get(sector_col)):
            sector = str(row.get(sector_col)).strip()
        rows.append({"code": code, "name": name, "sector": sector})

    out = pd.DataFrame(rows).drop_duplicates(subset=["code"]).reset_index(drop=True)
    if out.empty:
        raise ValueError("没有从股票池中识别出有效的 6 位 A 股代码")
    return out


def empty_stock_pool() -> pd.DataFrame:
    return pd.DataFrame(columns=["code", "name", "sector"])


MISSING_SECTOR_VALUES = {"", "nan", "none", "null", "unknown", "未分组", "未分类", "未归类", "无"}


def normalize_sector_value(value: Any) -> str:
    """Treat persisted placeholders as missing so auto sector fill can retry."""
    if value is None or pd.isna(value):
        return ""
    sector = str(value).strip()
    if sector.lower() in MISSING_SECTOR_VALUES:
        return ""
    return sector


def write_stock_pool(df: pd.DataFrame, path: str | Path) -> pd.DataFrame:
    """规范化并写回股票池。默认写 code/name 两列，保证代码前导 0 不丢失。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if df is None or df.empty:
        out = empty_stock_pool()
    else:
        rows = []
        for _, r in df.iterrows():
            try:
                code = normalize_code(r.get("code", r.iloc[0] if len(r) else ""))
            except Exception:
                continue
            name = str(r.get("name", "")).strip() if "name" in df.columns else ""
            sector = normalize_sector_value(r.get("sector", "")) if "sector" in df.columns else ""
            rows.append({"code": code, "name": name, "sector": sector})
        out = pd.DataFrame(rows, columns=["code", "name", "sector"]) if rows else empty_stock_pool()
        out = out.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)

    if p.suffix.lower() in {".xlsx", ".xls"}:
        out.to_excel(p, index=False)
    else:
        out.to_csv(p, index=False, encoding="utf-8-sig")
    return out


def read_stock_pool_or_empty(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return empty_stock_pool()
    try:
        return read_stock_pool(str(p))
    except ValueError as exc:
        if "为空" in str(exc) or "没有从股票池" in str(exc):
            return empty_stock_pool()
        raise


def backup_stock_pool(path: str | Path, backup_dir: str | Path = "pool_backups") -> str:
    p = Path(path)
    if not p.exists():
        return ""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_absolute():
        backup_dir = p.parent / backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = now_cn().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{p.stem}_{ts}{p.suffix or '.csv'}"
    shutil.copy2(p, backup_path)
    return str(backup_path)


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_cache_fresh(path: Path, hours: float) -> bool:
    if not path.exists() or hours <= 0:
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age.total_seconds() < hours * 3600


def market_code_prefix(code: str) -> str:
    """把 6 位 A 股代码转成带市场前缀的代码：sh600519 / sz000001 / bj8xxxxx。"""
    code = normalize_code(code)
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("8", "4")):
        return "bj" + code
    return "sz" + code


def provider_list(cfg: Dict[str, Any], key: str, default: List[str]) -> List[str]:
    providers = cfg.get("data", {}).get(key, default)
    if isinstance(providers, str):
        providers = [x.strip() for x in providers.split(",") if x.strip()]
    out = []
    for item in providers or []:
        item = str(item).strip().lower()
        if item and item not in out:
            out.append(item)
    return out or default


def normalize_board_source(kind: str) -> str:
    source = str(kind or "sw").lower().strip()
    aliases = {
        "em": "industry",
        "eastmoney": "industry",
        "em_industry": "industry",
        "eastmoney_industry": "industry",
        "theme": "concept",
        "topic": "concept",
        "ths_concept": "concept",
        "sina_concept": "concept",
        "concept_priority": "concept_first",
        "concept_first": "concept_first",
        "concept_fallback": "concept_first",
        "em_concept": "em_concept",
        "eastmoney_concept": "em_concept",
        "sw_l1": "sw",
        "sw_level1": "sw",
        "sw_industry": "sw",
        "shenwan": "sw",
        "申万": "sw",
    }
    return aliases.get(source, source)


def non_latest_output_file(path: Path) -> bool:
    name = path.name
    if name.startswith(("latest_", "last_")):
        return False
    if name in {"stock_pool.csv"}:
        return False
    return path.is_file()


def cleanup_output_dir(out_dir: str | Path, cfg: Dict[str, Any]) -> List[str]:
    """清理 output 历史文件：保留 latest_* 和 last_*，删除过期历史文件并控制总数。"""
    o_cfg = cfg.get("output", {})
    if not o_cfg.get("cleanup_on_run", True):
        return []
    p = ensure_dir(out_dir)
    retention_days = float(o_cfg.get("retention_days", 14))
    max_history_files = int(o_cfg.get("max_history_files", 300))
    now_ts = time.time()
    removed: List[str] = []
    files = [x for x in p.iterdir() if non_latest_output_file(x)]
    for f in files:
        try:
            if retention_days > 0 and now_ts - f.stat().st_mtime > retention_days * 86400:
                f.unlink()
                removed.append(f.name)
        except Exception:
            pass
    files = sorted([x for x in p.iterdir() if non_latest_output_file(x)], key=lambda x: x.stat().st_mtime, reverse=True)
    if max_history_files > 0 and len(files) > max_history_files:
        for f in files[max_history_files:]:
            try:
                f.unlink()
                removed.append(f.name)
            except Exception:
                pass
    return removed


def unique_nonempty(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        s = str(item).strip()
        if not s or s.lower() == "nan":
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def split_reason_text(text: Any) -> List[str]:
    if text is None or (isinstance(text, float) and np.isnan(text)):
        return []
    return unique_nonempty(re.split(r"[；;]", str(text)))
