# -*- coding: utf-8 -*-
from __future__ import annotations

from .base import *
from .market_data import safe_float


ETF_ACTION_COLUMNS = [
    "date", "rebalance_rule", "rebalance_due", "code", "name", "action", "action_cn",
    "current_shares", "target_shares", "trade_shares", "price_ref", "current_weight",
    "target_weight", "weight_diff", "trade_cash", "order_timing", "reason",
]


def _col(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    lower = {str(c).strip().lower(): c for c in df.columns}
    for name in names:
        if str(name).lower() in lower:
            return lower[str(name).lower()]
    return None


def parse_number(value: Any, default: float = 0.0) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    text = str(value).strip().replace(",", "")
    if not text or text.lower() == "nan":
        return default
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return default
    out = float(match.group(0))
    tail = text[match.end():].lower()
    if "%" in tail:
        out /= 100.0
    if "万" in tail or "w" in tail:
        out *= 10000.0
    return out


def parse_weight(value: Any, default: float = 0.0) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    text = str(value).strip()
    if "%" in text:
        return parse_number(text, default)  # parse_number already divides percentages.
    try:
        out = float(str(value).replace(",", ""))
    except Exception:
        return default
    if not np.isfinite(out):
        return default
    return out / 100.0 if out > 1.5 else out


def _read_csv_flexible(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, dtype=str, encoding="gbk")


def read_etf_portfolio(path: str | Path, account_default: float = 0.0) -> Tuple[float, float, pd.DataFrame, str]:
    p = Path(path)
    if not p.exists():
        account = float(account_default or 0.0)
        return account, account, pd.DataFrame(columns=["code", "name", "shares", "cost_price"]), f"未找到ETF持仓文件 {p}，按空仓处理"
    df = _read_csv_flexible(p)
    if df.empty:
        account = float(account_default or 0.0)
        return account, account, pd.DataFrame(columns=["code", "name", "shares", "cost_price"]), "ETF持仓文件为空，按空仓处理"

    code_col = _col(df, ["ETF代码", "基金代码", "证券代码", "股票代码", "代码", "code", "symbol", "ticker"])
    name_col = _col(df, ["ETF名称", "基金名称", "证券简称", "股票名称", "名称", "简称", "name"])
    shares_col = _col(df, ["ETF份额", "持仓份额", "基金份额", "份额", "数量", "持仓数量", "股票股数", "股数", "shares", "qty", "quantity"])
    cost_col = _col(df, ["买入价格", "成本价", "持仓成本", "成本", "买入价", "cost_price", "cost", "buy_price"])
    total_col = _col(df, ["总资金", "账户总资金", "账户权益", "总资产", "total_equity", "total_funds", "account", "equity"])
    cash_col = _col(df, ["可用现金", "可用资金", "现金", "cash", "available_cash"])

    total_equity = float(account_default or 0.0)
    if total_col is not None:
        vals = [parse_number(x, np.nan) for x in df[total_col].dropna().tolist()]
        vals = [x for x in vals if np.isfinite(x) and x > 0]
        if vals:
            total_equity = float(vals[0])
    cash = total_equity
    if cash_col is not None:
        vals = [parse_number(x, np.nan) for x in df[cash_col].dropna().tolist()]
        vals = [x for x in vals if np.isfinite(x)]
        if vals:
            cash = float(vals[0])

    if code_col is None or shares_col is None:
        return total_equity, cash, pd.DataFrame(columns=["code", "name", "shares", "cost_price"]), "ETF持仓文件缺少代码或份额列，按空仓处理"

    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            code = normalize_code(row.get(code_col, ""))
        except Exception:
            continue
        shares = int(parse_number(row.get(shares_col), 0.0))
        if shares <= 0:
            continue
        cost = parse_number(row.get(cost_col), np.nan) if cost_col is not None else np.nan
        name = str(row.get(name_col, "")).strip() if name_col is not None else ""
        rows.append({"code": code, "name": name, "shares": shares, "cost_price": cost})
    out = pd.DataFrame(rows, columns=["code", "name", "shares", "cost_price"])
    if out.empty:
        return total_equity, cash, out, "ETF持仓文件没有有效持仓，按空仓处理"
    return total_equity, cash, out.drop_duplicates("code", keep="last").reset_index(drop=True), ""


def read_rotation_targets(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=["code", "name", "target_weight", "price_ref"])
    df = _read_csv_flexible(p)
    if df.empty:
        return pd.DataFrame(columns=["code", "name", "target_weight", "price_ref"])
    code_col = _col(df, ["code", "ETF代码", "基金代码", "证券代码", "代码"])
    name_col = _col(df, ["name", "ETF名称", "基金名称", "名称", "简称"])
    weight_col = _col(df, ["target_weight", "目标仓位", "仓位"])
    price_col = _col(df, ["close", "收盘价", "price", "价格"])
    score_col = _col(df, ["selection_score", "选择分", "rotation_score", "轮动分"])
    reason_col = _col(df, ["rotation_reason", "轮动依据", "reason", "原因"])
    if code_col is None or weight_col is None:
        return pd.DataFrame(columns=["code", "name", "target_weight", "price_ref"])
    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            code = normalize_code(row.get(code_col, ""))
        except Exception:
            continue
        target_weight = parse_weight(row.get(weight_col), 0.0)
        if target_weight <= 0:
            continue
        rows.append({
            "code": code,
            "name": str(row.get(name_col, "")).strip() if name_col is not None else "",
            "target_weight": target_weight,
            "price_ref": parse_number(row.get(price_col), np.nan) if price_col is not None else np.nan,
            "selection_score": parse_number(row.get(score_col), np.nan) if score_col is not None else np.nan,
            "reason": str(row.get(reason_col, "")).strip() if reason_col is not None else "",
        })
    return pd.DataFrame(rows).drop_duplicates("code", keep="first").reset_index(drop=True) if rows else pd.DataFrame(columns=["code", "name", "target_weight", "price_ref"])


def read_candidate_prices(path: str | Path) -> Dict[str, Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return {}
    df = _read_csv_flexible(p)
    if df.empty:
        return {}
    code_col = _col(df, ["code", "ETF代码", "基金代码", "证券代码", "代码"])
    name_col = _col(df, ["name", "ETF名称", "基金名称", "名称", "简称"])
    price_col = _col(df, ["close", "收盘价", "price", "价格"])
    if code_col is None or price_col is None:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for _, row in df.iterrows():
        try:
            code = normalize_code(row.get(code_col, ""))
        except Exception:
            continue
        price = parse_number(row.get(price_col), np.nan)
        if not np.isfinite(price) or price <= 0:
            continue
        out[code] = {"price": price, "name": str(row.get(name_col, "")).strip() if name_col is not None else ""}
    return out


def resolve_rebalance_rule(cfg: Dict[str, Any], override: str = "") -> str:
    etf_cfg = cfg.get("etf", {})
    trade_cfg = etf_cfg.get("trade", {})
    rule = str(override or trade_cfg.get("rebalance") or etf_cfg.get("backtest", {}).get("rebalance") or "W-FRI").strip()
    return rule or "W-FRI"


def is_rebalance_due(as_of: pd.Timestamp, rule: str) -> bool:
    date = pd.Timestamp(as_of).normalize()
    freq = str(rule or "W-FRI").strip().upper()
    if freq in {"D", "B", "1D", "DAILY", "DAY"}:
        return date.weekday() < 5
    if freq.startswith("W-"):
        weekday = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}.get(freq.split("-", 1)[1][:3])
        if weekday is not None:
            return date.weekday() == weekday
    try:
        dates = pd.date_range(date - pd.Timedelta(days=90), date, freq=rule)
        return bool(len(dates) and pd.Timestamp(dates[-1]).normalize() == date)
    except Exception:
        return date.weekday() == 4


def next_rebalance_date(as_of: pd.Timestamp, rule: str) -> pd.Timestamp:
    cur = pd.Timestamp(as_of).normalize()
    for offset in range(0, 91):
        day = cur + pd.Timedelta(days=offset)
        if is_rebalance_due(day, rule):
            return day
    return cur


def _round_lot(shares: float, lot: int) -> int:
    if shares <= 0 or lot <= 0:
        return 0
    return int(math.floor(shares / lot) * lot)


def build_rebalance_actions(
    portfolio: pd.DataFrame,
    targets: pd.DataFrame,
    candidate_prices: Dict[str, Dict[str, Any]],
    cfg: Dict[str, Any],
    account: float,
    cash: float,
    as_of: pd.Timestamp,
    rebalance_rule: str,
    force_rebalance: bool = False,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    etf_cfg = cfg.get("etf", {})
    trade_cfg = etf_cfg.get("trade", {})
    min_lot = int(etf_cfg.get("min_lot", 100) or 100)
    threshold = float(trade_cfg.get("rebalance_threshold_pct", 0.03) or 0.0)
    min_trade_cash = float(trade_cfg.get("min_trade_cash", 1000.0) or 0.0)
    total_equity = float(account or 0.0)
    due = bool(force_rebalance or is_rebalance_due(as_of, rebalance_rule))

    port = portfolio.copy() if portfolio is not None else pd.DataFrame()
    if port.empty:
        port = pd.DataFrame(columns=["code", "name", "shares", "cost_price"])
    target = targets.copy() if targets is not None else pd.DataFrame()
    if target.empty:
        target = pd.DataFrame(columns=["code", "name", "target_weight", "price_ref"])

    port_by_code = {normalize_code(r["code"]): r for _, r in port.iterrows()} if not port.empty else {}
    target_by_code = {normalize_code(r["code"]): r for _, r in target.iterrows()} if not target.empty else {}
    codes = sorted(set(port_by_code) | set(target_by_code))
    rows: List[Dict[str, Any]] = []
    date_text = pd.Timestamp(as_of).strftime("%Y-%m-%d")

    for code in codes:
        p_row = port_by_code.get(code)
        t_row = target_by_code.get(code)
        current_shares = int(parse_number(p_row.get("shares"), 0.0)) if p_row is not None else 0
        current_name = str(p_row.get("name", "")).strip() if p_row is not None else ""
        target_name = str(t_row.get("name", "")).strip() if t_row is not None else ""
        name = target_name or current_name or candidate_prices.get(code, {}).get("name", "")
        target_weight = parse_weight(t_row.get("target_weight"), 0.0) if t_row is not None else 0.0
        price_ref = parse_number(t_row.get("price_ref"), np.nan) if t_row is not None else np.nan
        if not np.isfinite(price_ref) or price_ref <= 0:
            price_ref = parse_number(candidate_prices.get(code, {}).get("price"), np.nan)
        if not np.isfinite(price_ref) or price_ref <= 0:
            price_ref = parse_number(p_row.get("cost_price"), np.nan) if p_row is not None else np.nan
        current_value = current_shares * price_ref if np.isfinite(price_ref) and price_ref > 0 else np.nan
        current_weight = current_value / total_equity if total_equity > 0 and np.isfinite(current_value) else 0.0
        target_cash = target_weight * total_equity
        target_shares = _round_lot(target_cash / price_ref, min_lot) if np.isfinite(price_ref) and price_ref > 0 else 0
        weight_diff = target_weight - current_weight
        delta_shares = target_shares - current_shares
        trade_cash = abs(delta_shares) * price_ref if np.isfinite(price_ref) else np.nan

        action = "HOLD"
        action_cn = "持有"
        trade_shares = 0
        timing = "不交易"
        reason = "目标仓位偏离未达阈值"

        if not due:
            action = "WAIT_REBALANCE"
            action_cn = "等待再平衡"
            reason = f"今天不是回测规则 {rebalance_rule} 的再平衡日"
        elif t_row is None and current_shares > 0:
            action = "SELL"
            action_cn = "卖出"
            trade_shares = current_shares
            timing = "再平衡日按计划卖出"
            reason = "不在本期ETF目标组合"
        elif delta_shares > 0:
            if abs(weight_diff) >= threshold and (not np.isfinite(trade_cash) or trade_cash >= min_trade_cash):
                action = "BUY"
                action_cn = "买入"
                trade_shares = delta_shares
                timing = "再平衡日分批限价买入，不追高"
                reason = f"目标仓位高于当前仓位 {weight_diff:.1%}"
            else:
                reason = f"买入差额低于阈值：仓位差{weight_diff:.1%}，金额{trade_cash:,.0f}"
        elif delta_shares < 0:
            if abs(weight_diff) >= threshold and (not np.isfinite(trade_cash) or trade_cash >= min_trade_cash):
                action = "SELL"
                action_cn = "卖出"
                trade_shares = abs(delta_shares)
                timing = "再平衡日按计划卖出"
                reason = f"目标仓位低于当前仓位 {weight_diff:.1%}"
            else:
                reason = f"卖出差额低于阈值：仓位差{weight_diff:.1%}，金额{trade_cash:,.0f}"
        else:
            reason = "当前份额已接近目标份额"

        if not np.isfinite(price_ref) or price_ref <= 0:
            action = "DATA_ERROR"
            action_cn = "价格缺失"
            trade_shares = 0
            timing = "不交易"
            reason = "缺少目标或候选价格，无法计算调仓份额"

        rows.append({
            "date": date_text,
            "rebalance_rule": rebalance_rule,
            "rebalance_due": due,
            "code": code,
            "name": name,
            "action": action,
            "action_cn": action_cn,
            "current_shares": current_shares,
            "target_shares": target_shares,
            "trade_shares": int(trade_shares),
            "price_ref": price_ref,
            "current_weight": current_weight,
            "target_weight": target_weight,
            "weight_diff": weight_diff,
            "trade_cash": trade_cash if np.isfinite(trade_cash) else np.nan,
            "order_timing": timing,
            "reason": reason,
        })

    actions = pd.DataFrame(rows, columns=ETF_ACTION_COLUMNS)
    summary = {
        "rebalance_due": due,
        "rebalance_rule": rebalance_rule,
        "next_rebalance_date": next_rebalance_date(as_of + pd.Timedelta(days=1), rebalance_rule).strftime("%Y-%m-%d") if not due else pd.Timestamp(as_of).strftime("%Y-%m-%d"),
        "total_equity": total_equity,
        "cash": float(cash if np.isfinite(cash) else 0.0),
        "target_count": int(len(target)),
        "holding_count": int(len(port)),
    }
    return actions, summary


def format_trade_message(actions: pd.DataFrame, summary: Dict[str, Any], portfolio_note: str = "") -> str:
    now_text = now_cn().strftime("%Y-%m-%d %H:%M")
    lines = [f"ETF买入与持仓调仓计划 {now_text}"]
    rule = str(summary.get("rebalance_rule", ""))
    due = bool(summary.get("rebalance_due", False))
    if due:
        lines.append(f"今日是 ETF 再平衡日（规则：{rule}），调仓动作与回测再平衡频率一致。")
    else:
        lines.append(f"今日不是 ETF 再平衡日（规则：{rule}），不输出买卖指令；下一再平衡日：{summary.get('next_rebalance_date', '')}。")
    if portfolio_note:
        lines.append(f"持仓提示：{portfolio_note}")
    lines.append(f"账户权益：{float(summary.get('total_equity', 0.0)):,.0f}；可用现金：{float(summary.get('cash', 0.0)):,.0f}。")
    lines.append(f"当前ETF持仓 {int(summary.get('holding_count', 0))} 只；目标组合 {int(summary.get('target_count', 0))} 只。")

    if actions is None or actions.empty:
        lines.append("当前没有 ETF 目标或持仓记录。")
        return "\n".join(lines)

    trade = actions[pd.to_numeric(actions["trade_shares"], errors="coerce").fillna(0).astype(int) > 0].copy()
    if trade.empty:
        lines.append("")
        lines.append("今日没有需要执行的 ETF 买卖动作。")
    else:
        lines.append("")
        lines.append("需要执行/准备的 ETF 操作：")
        order = {"SELL": 0, "BUY": 1}
        trade["_order"] = trade["action"].map(order).fillna(9)
        for _, row in trade.sort_values(["_order", "code"]).iterrows():
            lines.append(
                f"- {row['code']} {row.get('name','')}：{row['action_cn']} {int(row['trade_shares'])} 份，"
                f"参考价{safe_float(row.get('price_ref'), np.nan):.3f}，约{safe_float(row.get('trade_cash'), 0.0):,.0f}元；"
                f"目标仓位{safe_float(row.get('target_weight'), 0.0):.1%}，当前{safe_float(row.get('current_weight'), 0.0):.1%}；"
                f"{row.get('reason', '')}"
            )

    target_rows = actions[pd.to_numeric(actions["target_weight"], errors="coerce").fillna(0.0) > 0].copy()
    if not target_rows.empty:
        lines.append("")
        lines.append("本期目标组合：")
        for _, row in target_rows.sort_values(["target_weight", "code"], ascending=[False, True]).iterrows():
            lines.append(
                f"- {row['code']} {row.get('name','')}：目标{safe_float(row.get('target_weight'), 0.0):.1%}，"
                f"目标{int(row.get('target_shares', 0))}份，当前{int(row.get('current_shares', 0))}份"
            )

    info = actions[pd.to_numeric(actions["trade_shares"], errors="coerce").fillna(0).astype(int) <= 0].copy()
    if not info.empty:
        show = info[~info["action"].astype(str).eq("WAIT_REBALANCE")].head(8)
        if not show.empty:
            lines.append("")
            lines.append("持有/观察：")
            for _, row in show.iterrows():
                lines.append(f"- {row['code']} {row.get('name','')}：{row['action_cn']}；{row.get('reason', '')}")

    lines.append("")
    lines.append("说明：ETF实盘调仓只按目标组合再平衡，不自动下单；再平衡频率默认沿用 ETF 回测配置。")
    return "\n".join(lines)


def write_outputs(out_dir: Path, actions: pd.DataFrame, message: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = now_cn().strftime("%Y%m%d_%H%M%S")
    actions.to_csv(out_dir / f"etf_trade_actions_{ts}.csv", index=False, encoding="utf-8-sig")
    actions.to_csv(out_dir / "latest_etf_trade_actions.csv", index=False, encoding="utf-8-sig")
    report = "# ETF买入与持仓调仓计划\n\n" + message + "\n"
    (out_dir / f"etf_trade_report_{ts}.md").write_text(report, encoding="utf-8")
    (out_dir / "latest_etf_trade_report.md").write_text(report, encoding="utf-8")
    msg_path = out_dir / "latest_etf_trade_plan.txt"
    msg_path.write_text(message, encoding="utf-8")
    return msg_path


def run(args: argparse.Namespace) -> Tuple[pd.DataFrame, str, Path]:
    cfg = load_config(args.config)
    etf_cfg = cfg.setdefault("etf", {})
    trade_cfg = etf_cfg.setdefault("trade", {})
    out_dir = ensure_dir(args.out or trade_cfg.get("out_dir") or etf_cfg.get("out_dir", "etf_output"))
    portfolio_path = args.portfolio or trade_cfg.get("portfolio", "etf_portfolio.csv")
    targets_path = args.targets or trade_cfg.get("target_positions", str(Path(out_dir) / "latest_etf_rotation_positions_raw.csv"))
    candidates_path = args.candidates or trade_cfg.get("target_candidates", str(Path(out_dir) / "latest_etf_rotation_candidates_raw.csv"))
    as_of = pd.Timestamp(args.date).normalize() if args.date else pd.Timestamp(now_cn().date())
    rebalance_rule = resolve_rebalance_rule(cfg, args.rebalance)

    account, cash, portfolio, note = read_etf_portfolio(portfolio_path, args.account)
    if not np.isfinite(account) or account <= 0:
        account = float(args.account or etf_cfg.get("backtest", {}).get("initial_cash", 100000.0))
    targets = read_rotation_targets(targets_path)
    candidate_prices = read_candidate_prices(candidates_path)
    actions, summary = build_rebalance_actions(
        portfolio,
        targets,
        candidate_prices,
        cfg,
        account=account,
        cash=cash,
        as_of=as_of,
        rebalance_rule=rebalance_rule,
        force_rebalance=bool(args.force_rebalance),
    )
    msg = format_trade_message(actions, summary, note)
    msg_path = write_outputs(Path(out_dir), actions, msg)
    return actions, msg, msg_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ETF实盘持仓调仓管理：按回测频率对比目标组合并生成买卖差额")
    p.add_argument("--portfolio", default="", help="ETF持仓文件，默认读取 etf.trade.portfolio")
    p.add_argument("--targets", default="", help="ETF目标组合 raw CSV，默认读取 latest_etf_rotation_positions_raw.csv")
    p.add_argument("--candidates", default="", help="ETF候选 raw CSV，用于补全非目标持仓参考价")
    p.add_argument("--config", default="", help="配置文件 YAML/JSON，可选")
    p.add_argument("--out", default="", help="输出目录，默认读取 etf.trade.out_dir 或 etf.out_dir")
    p.add_argument("--account", type=float, default=100000.0, help="账户权益，持仓文件未设置总资金时使用")
    p.add_argument("--rebalance", default="", help="覆盖再平衡规则；默认沿用 etf.backtest.rebalance")
    p.add_argument("--date", default="", help="按指定日期判断是否再平衡 YYYY-MM-DD，测试用")
    p.add_argument("--force-rebalance", action="store_true", help="忽略日期，强制输出调仓动作")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    _, msg, _ = run(args)
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
