# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd


def to_chinese_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        columns = ["日期", "股票代码", "股票名称", "判定", "结论原因", "收盘价", "综合分", "评分拆解", "是否买入信号", "目标仓位", "建议买入金额", "建议股数", "止损价", "止损幅度", "止盈1", "止盈2", "60日收益", "相对强度排名", "加分项", "阻断/扣分项", "关键指标", "完整解释", "过滤原因"]
        return pd.DataFrame(columns=columns)
    mapping = {
        "date": "日期",
        "code": "股票代码",
        "name": "股票名称",
        "close": "收盘价",
        "pct_chg": "当日涨跌幅%",
        "score": "综合分",
        "trend_score": "趋势分",
        "momentum_score": "动量相对强弱分",
        "breakout_score": "买点分型分",
        "sector_score": "板块主线分",
        "setup_score": "买点原始分",
        "setup_type": "买点类型",
        "setup_ok": "买点是否达标",
        "setup_reason": "买点依据",
        "setup_blockers": "买点阻断",
        "sector": "板块",
        "sector_strength_score": "板块强度分",
        "sector_is_mainline": "是否主线板块",
        "sector_is_strong": "是否强势板块",
        "sector_ret20": "板块20日收益",
        "sector_ret60": "板块60日收益",
        "sector_up_rate20": "板块20日上涨比例",
        "sector_high_pos_rate": "板块强势区比例",
        "stock_sector_rs20": "个股板块内20日排名",
        "stock_sector_rs60": "个股板块内60日排名",
        "outperform_sector20": "20日超额板块收益",
        "outperform_sector60": "60日超额板块收益",
        "anchor_score": "天量锚点分",
        "anchor_date": "天量锚点日期",
        "anchor_days_ago": "距锚点天数",
        "anchor_reason": "锚点有效依据",
        "anchor_blockers": "锚点阻断",
        "risk_score": "风险结构分",
        "data_provider": "K线数据源",
        "data_warning": "数据提醒",
        "data_quality_warning": "数据质量提醒",
        "setup_tags": "形态标签",
        "ma250": "MA250",
        "ma120_slope40": "120日线斜率40",
        "drawdown250": "距250日高点",
        "close_pos60": "60日区间位置",
        "close_pos120": "120日区间位置",
        "range_contraction_20_60": "20/60日波动收敛",
        "amount_dryup20": "短期缩量比",
        "lower_shadow_pct": "下影线幅度",
        "body_pct": "实体幅度",
        "candle_strength": "K线强度",
        "reg_slope20": "20日回归斜率",
        "reg_r2_20": "20日趋势效率R2",
        "reg_slope60": "60日回归斜率",
        "reg_r2_60": "60日趋势效率R2",
        "score_detail": "评分拆解",
        "score_weakness": "评分扣分点",
        "risk_gate_block": "A股风险闸门",
        "risk_gate_reason": "风险闸门原因",
        "risk_tags": "风险标签",
        "limit_down_count_3": "近3日跌停数",
        "limit_down_count_6": "近6日跌停数",
        "limit_down_count_10": "近10日跌停数",
        "days_since_limit_down": "距最近跌停天数",
        "ret3": "3日收益",
        "ret5": "5日收益",
        "ret10": "10日收益",
        "drawdown10": "距10日高点",
        "drawdown20": "距20日高点",
        "drawdown60": "距60日高点",
        "is_signal": "是否买入信号",
        "decision": "判定",
        "decision_reason": "结论原因",
        "positive_factors": "加分项",
        "negative_factors": "阻断/扣分项",
        "metric_snapshot": "关键指标",
        "compact_snapshot": "关键指标摘要",
        "buy_logic": "完整解释",
        "target_weight": "目标仓位",
        "target_cash": "建议买入金额",
        "target_shares": "建议股数",
        "actual_weight_by_lot": "按整手实际仓位",
        "stop_loss": "止损价",
        "risk_pct": "止损幅度",
        "take_profit_1": "止盈1_1.5R",
        "take_profit_2": "止盈2_3R",
        "ret20": "20日收益",
        "ret60": "60日收益",
        "ret120": "120日收益",
        "rs_rank60": "60日相对强度排名",
        "drawdown120": "距120日高点",
        "amount_ma20": "20日均成交额",
        "amount_ratio20": "成交额/20日均额",
        "atr_pct": "ATR波动率",
        "reason": "原始加分原因",
        "filter_reason": "过滤原因",
        "prune_score": "淘汰评分",
        "淘汰原因": "淘汰原因",
    }
    preferred = [
        "date", "code", "name", "sector", "decision", "decision_reason", "close", "pct_chg", "score", "score_detail",
        "trend_score", "momentum_score", "sector_score", "breakout_score", "setup_type", "setup_score", "setup_ok", "signal_grade", "risk_score", "is_signal", "target_weight", "target_cash",
        "target_shares", "actual_weight_by_lot", "stop_loss", "risk_pct", "take_profit_1", "take_profit_2",
        "risk_gate_block", "risk_gate_reason", "risk_tags", "limit_down_count_3", "limit_down_count_6", "limit_down_count_10", "days_since_limit_down",
        "sector_strength_score", "sector_strength_source", "sector_is_mainline", "sector_is_strong", "sector_ret20", "sector_ret60", "sector_up_rate20", "sector_high_pos_rate", "stock_sector_rs20", "stock_sector_rs60", "outperform_sector20", "outperform_sector60",
        "setup_reason", "setup_blockers", "anchor_score", "anchor_date", "anchor_days_ago", "anchor_reason", "anchor_blockers",
        "ret3", "ret5", "ret10", "ret20", "ret60", "ret120", "rs_rank60", "drawdown10", "drawdown20", "drawdown60", "drawdown120", "drawdown250", "close_pos60", "close_pos120",
        "range_contraction_20_60", "amount_ma20", "amount_ratio20", "amount_dryup20", "atr_pct",
        "data_provider", "data_warning", "data_quality_warning", "setup_tags",
        "positive_factors", "negative_factors", "metric_snapshot", "buy_logic", "reason", "score_weakness", "filter_reason", "淘汰原因", "prune_score",
    ]
    cols = [c for c in preferred if c in df.columns] + [c for c in df.columns if c not in preferred]
    out = df[cols].copy().rename(columns=mapping)
    for c in ["目标仓位", "按整手实际仓位", "止损幅度", "3日收益", "5日收益", "10日收益", "20日收益", "60日收益", "120日收益", "60日相对强度排名", "距10日高点", "距20日高点", "距60日高点", "距120日高点", "距250日高点", "ATR波动率", "20/60日波动收敛", "短期缩量比", "下影线幅度", "实体幅度", "20日回归斜率", "60日回归斜率", "板块20日收益", "板块60日收益", "板块20日上涨比例", "板块强势区比例", "个股板块内20日排名", "个股板块内60日排名", "20日超额板块收益", "60日超额板块收益"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.2%}" if pd.notna(x) else "")
    for c in ["收盘价", "止损价", "止盈1_1.5R", "止盈2_3R"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.2f}" if pd.notna(x) else "")
    for c in ["综合分", "趋势分", "动量相对强弱分", "买点分型分", "板块主线分", "买点原始分", "风险结构分", "板块强度分", "天量锚点分"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.1f}" if pd.notna(x) else "")
    for c in ["建议买入金额", "20日均成交额"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:,.0f}" if pd.notna(x) else "")
    return out
