# -*- coding: utf-8 -*-
from __future__ import annotations

from .interaction import *

def handle_chat_command(text: str, args: argparse.Namespace, cfg: Dict[str, Any]) -> Tuple[str, Optional[Path]]:
    action = parse_chat_action(text)
    out_path = ensure_dir(args.out)

    if action.kind == "add":
        reply = add_items_to_stock_pool(args.pool, action.items, action.terms, cfg, refresh=args.refresh)
    elif action.kind == "remove":
        reply = remove_items_from_stock_pool(args.pool, action.items, action.terms, cfg)
    elif action.kind == "list":
        reply = format_pool_list(args.pool)
    elif action.kind in {"scan", "prune"}:
        manual_prune = action.auto_prune or (args.auto_prune is True)
        signals, candidates, market, msg_path, _ = scan(
            args.pool,
            cfg,
            args.out,
            args.account,
            refresh=args.refresh,
            limit=args.limit,
            auto_prune=manual_prune,
        )
        reply = msg_path.read_text(encoding="utf-8")
    elif action.kind == "explain":
        # 默认不重新拉行情：读取最近一次扫描生成的 latest_explanations_raw.csv / latest_report.md。
        # 这样你可以先“生成买入信号”，再按需“解释原因”，不会因为查看解释又触发一轮数据源限流。
        reply = explain_from_latest_output(action, out_path)
    elif action.kind == "help":
        reply = help_message()
    else:
        reply = "我没有识别这个命令。\n\n" + help_message()

    reply_path = out_path / "latest_chat_reply.txt"
    reply_path.write_text(reply, encoding="utf-8")
    return reply, reply_path



def prefilter_pool_for_stability(pool: pd.DataFrame, stock_spot: pd.DataFrame, cfg: Dict[str, Any]) -> Tuple[pd.DataFrame, str]:
    """股票池过大时先用实时行情做轻量预筛，避免 100+ 全量历史K线扫描被系统 kill。

    这是稳定性策略：它不删除股票池，只决定本次优先扫描哪些股票。
    """
    data_cfg = cfg.get("data", {})
    if not bool(data_cfg.get("two_stage_scan", True)) or pool.empty:
        return pool, ""
    trigger = int(data_cfg.get("prefilter_pool_when_gt", 60) or 0)
    max_scan = int(data_cfg.get("max_scan_per_run", 45) or 0)
    if trigger <= 0 or max_scan <= 0 or len(pool) <= trigger:
        return pool, ""
    out = pool.copy()
    stale_spot_note = ""
    if is_stale_data_frame(stock_spot):
        out = pool.copy()
        stale_spot_note = "实时行情为旧缓存，改用股票池原始顺序。"
    elif stock_spot is not None and not stock_spot.empty and "代码" in stock_spot.columns:
        spot = stock_spot.copy()
        spot["code"] = spot["代码"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
        cols = [c for c in ["code", "最新价", "涨跌幅", "成交额", "换手率", "名称"] if c in spot.columns]
        out = out.merge(spot[cols], on="code", how="left")
        pct = pd.to_numeric(out.get("涨跌幅"), errors="coerce")
        amount = pd.to_numeric(out.get("成交额"), errors="coerce")
        turnover = pd.to_numeric(out.get("换手率"), errors="coerce")
        min_pct = float(data_cfg.get("prefilter_keep_pct_chg_min", -6.0))
        max_pct = float(data_cfg.get("prefilter_keep_pct_chg_max", 8.8))
        tradable = pct.between(min_pct, max_pct) | pct.isna()
        # 轻量 rank：优先今天活跃但不过热、成交额高、换手适中的股票。
        pct_score = (100 - (pct.fillna(0).clip(-8, 9) - 3.0).abs() * 8).clip(lower=0)
        amt_score = amount.rank(pct=True).fillna(0.2) * 100
        turn_score = turnover.rank(pct=True).fillna(0.2) * 20
        out["_prefilter_score"] = pct_score + 0.8 * amt_score + turn_score
        out = out[tradable].sort_values("_prefilter_score", ascending=False)
        if out.empty:
            out = pool.copy()
    else:
        # 没有实时行情时，不让程序崩，取前 max_scan 只；用户可用 --limit 或缩池进一步控制。
        out = pool.copy()
    selected = out.head(max_scan)[[c for c in pool.columns if c in out.columns]].copy()
    msg = f"股票池{len(pool)}只，启用两阶段稳定扫描：本次优先扫描{len(selected)}只；未扫描股票不会被删除。"
    if stale_spot_note:
        msg += stale_spot_note
    return selected, msg


def _snapshot_dir(cfg: Dict[str, Any]) -> Path:
    p = Path(cfg.get("data", {}).get("intraday_snapshot_dir", "cache/intraday_snapshots"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def update_intraday_snapshot_from_spot(code: str, spot_all: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    """用实时行情快照构造稳定的伪分时序列。

    当东方财富/新浪分钟K失效时，10分钟定时调用会不断积累快照，足够判断VWAP近似、日内高低位、弱于快照均线等。
    """
    code = normalize_code(code)
    if spot_all is None or spot_all.empty or "代码" not in spot_all.columns or is_stale_data_frame(spot_all):
        return pd.DataFrame()
    row = spot_all[spot_all["代码"].astype(str).str.zfill(6) == code]
    if row.empty:
        return pd.DataFrame()
    r = row.iloc[0]
    now = now_cn().replace(second=0, microsecond=0)
    def f(col: str, default: float = np.nan) -> float:
        return safe_float(r.get(col), default)
    price = f("最新价")
    if not np.isfinite(price) or price <= 0:
        return pd.DataFrame()
    rec = {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "open": f("今开", price),
        "high": f("最高", price),
        "low": f("最低", price),
        "close": price,
        "volume": f("成交量"),
        "amount": f("成交额"),
        "pct_chg": f("涨跌幅"),
    }
    path = _snapshot_dir(cfg) / f"{code}.csv"
    try:
        old = pd.read_csv(path)
    except Exception:
        old = pd.DataFrame()
    df = pd.concat([old, pd.DataFrame([rec])], ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    # 只保留近5天快照，避免文件无限增长。
    cutoff = pd.Timestamp(now.date() - timedelta(days=5))
    df = df.dropna(subset=["datetime", "close"]).drop_duplicates(subset=["datetime"], keep="last")
    df = df[df["datetime"] >= cutoff].sort_values("datetime").reset_index(drop=True)
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
    except Exception:
        pass
    return df


def intraday_buy_confirmation(code: str, spot_all: pd.DataFrame, cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """尾盘/盘中买入信号的日内确认。

    免费分钟K源经常失败，所以这里优先使用实时快照构造的伪分时；如果快照不够，也至少用实时价在日内区间的位置做保护。
    """
    if not bool(cfg.get("data", {}).get("intraday_buy_filter", True)):
        return True, "未开启日内确认"
    snap = update_intraday_snapshot_from_spot(code, spot_all, cfg)
    if spot_all is None or spot_all.empty or "代码" not in spot_all.columns:
        return True, "实时行情缺失，跳过日内确认"
    if is_stale_data_frame(spot_all):
        return True, "实时行情为旧缓存，跳过日内确认"
    row = spot_all[spot_all["代码"].astype(str).str.zfill(6) == normalize_code(code)]
    if row.empty:
        return True, "实时行情无该股，跳过日内确认"
    r = row.iloc[0]
    price = safe_float(r.get("最新价")); high = safe_float(r.get("最高")); low = safe_float(r.get("最低")); pct = safe_float(r.get("涨跌幅")); amount = safe_float(r.get("成交额"))
    reasons = []
    ok = True
    if np.isfinite(pct) and pct < -1.8:
        ok = False; reasons.append(f"日内跌幅{pct:.2f}%偏弱")
    if np.isfinite(pct) and pct > 8.8:
        ok = False; reasons.append(f"日内涨幅{pct:.2f}%过热，避免追高")
    if np.isfinite(price) and np.isfinite(high) and high > 0 and price / high - 1 < -0.035:
        ok = False; reasons.append("从日内高点回落超过3.5%")
    if np.isfinite(price) and np.isfinite(high) and np.isfinite(low) and high > low:
        pos = (price - low) / (high - low)
        if pos < 0.45:
            ok = False; reasons.append(f"收盘/当前价处于日内区间偏低位置{pos:.0%}")
        else:
            reasons.append(f"日内区间位置{pos:.0%}")
    if not snap.empty and len(snap) >= 3:
        today = now_cn().date()
        td = snap[pd.to_datetime(snap["datetime"]).dt.date == today].copy()
        if len(td) >= 3:
            ma = pd.to_numeric(td["close"], errors="coerce").rolling(3, min_periods=2).mean().iloc[-1]
            if np.isfinite(price) and np.isfinite(ma) and price < ma * 0.995:
                ok = False; reasons.append("弱于盘中快照均线")
            else:
                reasons.append("强于盘中快照均线")
    else:
        reasons.append("分钟源不可用，使用实时快照/日内高低位确认")
    return ok, "；".join(unique_nonempty(reasons))

def scan(
    pool_path: str,
    cfg: Dict[str, Any],
    out_dir: str,
    account: float,
    refresh: bool = False,
    limit: int = 0,
    auto_prune: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame, MarketState, Path, Optional[PruneReport]]:
    out_path = ensure_dir(out_dir)
    removed_old = cleanup_output_dir(out_path, cfg)
    if removed_old:
        print(f"[清理] output 已清理历史文件 {len(removed_old)} 个")
    cfg["_active_pool_path"] = pool_path
    cfg["_pool_spot_only"] = bool(cfg.get("data", {}).get("prefer_pool_spot", True))
    fetcher = AkshareFetcher(cfg, refresh=refresh)
    full_pool = enrich_pool_sectors(read_stock_pool(pool_path), pool_path, cfg, fetcher=fetcher)
    pool = full_pool.copy()
    limit_skip_prune_reason = ""
    if limit and limit > 0:
        pool = pool.head(limit).copy()
        limit_skip_prune_reason = "本次使用了 --limit 测试扫描，避免误删未扫描股票"

    if cfg["data"].get("use_realtime_tail") and cfg["data"].get("adjust", "qfq") != "":
        # 实时行情是不复权价，和 qfq/hfq 混用会造成指标跳变。
        cfg["data"]["adjust"] = ""
        print("[提示] 尾盘实时模式已自动改用不复权日K，避免复权历史价与实时价混用。")

    pool_missing_name = pool["name"].astype(str).str.strip().eq("") if "name" in pool.columns else pd.Series(True, index=pool.index)
    full_missing_name = full_pool["name"].astype(str).str.strip().eq("") if "name" in full_pool.columns else pd.Series(True, index=full_pool.index)
    if bool(pool_missing_name.any() or full_missing_name.any()):
        name_map = fetch_name_map(fetcher)
        if name_map:
            pool.loc[pool_missing_name, "name"] = pool.loc[pool_missing_name, "code"].map(name_map).fillna("")
            full_pool.loc[full_missing_name, "name"] = full_pool.loc[full_missing_name, "code"].map(name_map).fillna("")

    market = evaluate_market(fetcher, cfg)
    print(f"[市场] {market.summary}")

    stock_spot = pd.DataFrame()
    # v6.5: 无论是否尾盘实时模式，都尽量先取一次实时行情；用于稳定预筛和日内买点确认。
    try:
        stock_spot = fetcher.stock_spot_all()
    except Exception as exc:
        print(f"[实时行情] 获取失败，继续使用日K扫描：{exc}")
        stock_spot = pd.DataFrame()
    realtime_spot_ok = not is_stale_data_frame(stock_spot)
    if not stock_spot.empty and not realtime_spot_ok:
        print("[实时行情] 当前只拿到旧缓存，本次不用于尾盘K线、日内确认或预筛。")

    prefilter_msg = ""
    if not limit:
        pool, prefilter_msg = prefilter_pool_for_stability(pool, stock_spot, cfg)
        if prefilter_msg:
            print(f"[预筛] {prefilter_msg}")
            limit_skip_prune_reason = prefilter_msg

    end_date = cfg["data"].get("end_date") or today_yyyymmdd()
    start_date = cfg["data"].get("start_date") or "20220101"
    adjust = cfg["data"].get("adjust", "qfq")

    raw_rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    total = len(pool)
    for n, (_, r) in enumerate(pool.iterrows(), start=1):
        code = r["code"]
        name = r.get("name", "")
        try:
            hist = fetcher.stock_hist(code, start_date, end_date, adjust)
            if cfg["data"].get("use_realtime_tail") and not stock_spot.empty and realtime_spot_ok:
                hist = merge_stock_tail_realtime(hist, stock_spot, code)
            metrics = compute_raw_metrics(code, name, hist, cfg, market)
            metrics["sector"] = str(r.get("sector", "未分组") or "未分组")
            raw_rows.append(metrics)
        except Exception as exc:
            errors.append({"code": code, "name": name, "error": str(exc)})
            raw_rows.append({"code": code, "name": name, "sector": str(r.get("sector", "未分组") or "未分组"), "ok_base": False, "filter_reason": f"数据错误：{exc}"})
        if n % 10 == 0 or n == total:
            print(f"[进度] 已扫描 {n}/{total}")

    metrics_df = pd.DataFrame(raw_rows)
    metrics_df = compute_sector_context(metrics_df, cfg, fetcher=fetcher)
    candidates = score_candidates(metrics_df, cfg)
    # 数据源错误率过高时，本次信号只做记录，不应触发买入或自动淘汰。
    data_error_count = int(candidates["filter_reason"].astype(str).str.contains("数据错误", na=False).sum()) if not candidates.empty and "filter_reason" in candidates.columns else len(errors)
    data_error_rate = (data_error_count / total) if total else 0.0
    max_error_rate = float(cfg.get("data", {}).get("max_error_rate_for_valid_run", 0.20))
    data_quality_skip_reason = ""
    if data_error_rate > max_error_rate:
        data_quality_skip_reason = f"数据源失败率 {data_error_rate:.0%} 超过阈值 {max_error_rate:.0%}，本次结果仅供检查，不执行买入/淘汰"
        print(f"[数据] {data_quality_skip_reason}")
        if not candidates.empty:
            candidates["is_signal"] = False
            candidates["data_quality_warning"] = data_quality_skip_reason
    # v6.5: 对已经达标或接近达标的股票做日内确认，避免日K信号在盘中明显走弱时仍然推送。
    if not candidates.empty and bool(cfg.get("data", {}).get("intraday_buy_filter", True)) and not stock_spot.empty and realtime_spot_ok:
        intraday_notes = []
        intraday_oks = []
        threshold = float(cfg["strategy"].get("score_threshold", 68))
        for _, rr in candidates.iterrows():
            code = str(rr.get("code", "")).zfill(6)
            needs_check = bool(rr.get("is_signal", False)) or safe_float(rr.get("score"), 0) >= threshold - 4
            if needs_check:
                ok, note = intraday_buy_confirmation(code, stock_spot, cfg)
            else:
                ok, note = True, "未进入买点候选，不做日内确认"
            intraday_oks.append(ok)
            intraday_notes.append(note)
        candidates["intraday_ok"] = intraday_oks
        candidates["intraday_reason"] = intraday_notes
        weak_mask = candidates["is_signal"].fillna(False) & (~candidates["intraday_ok"].fillna(True))
        if weak_mask.any():
            candidates.loc[weak_mask, "is_signal"] = False
            candidates.loc[weak_mask, "filter_reason"] = candidates.loc[weak_mask, "filter_reason"].astype(str).replace("", np.nan).fillna("日内买点确认未通过") + "；日内买点确认未通过"
    signals = candidates[candidates.get("is_signal", False) == True].copy() if not candidates.empty else pd.DataFrame()
    allocated = allocate_positions(signals, cfg, market, account)
    candidates = add_decision_explanations(candidates, allocated, market, cfg, account=account)
    allocated = attach_explanations_to_allocated(allocated, candidates)

    run_date = now_cn().strftime("%Y%m%d_%H%M%S")
    candidates_path = out_path / f"candidates_{run_date}.csv"
    signals_path = out_path / f"signals_{run_date}.csv"
    explanations_path = out_path / f"explanations_{run_date}.csv"
    report_path = out_path / f"report_{run_date}.md"
    market_path = out_path / f"market_{run_date}.csv"
    errors_path = out_path / f"errors_{run_date}.csv"

    prune_report: Optional[PruneReport] = None
    if auto_prune:
        prune_report = prune_stock_pool_by_candidates(
            pool_path,
            full_pool,
            candidates,
            allocated,
            cfg,
            out_path,
            run_date,
            skipped_reason=limit_skip_prune_reason or data_quality_skip_reason,
        )

    candidates_out = to_chinese_columns(candidates)
    signals_out = to_chinese_columns(allocated)
    explanations_out = to_chinese_columns(candidates)
    candidates_out.to_csv(candidates_path, index=False, encoding="utf-8-sig")
    signals_out.to_csv(signals_path, index=False, encoding="utf-8-sig")
    explanations_out.to_csv(explanations_path, index=False, encoding="utf-8-sig")
    # raw 文件保留英文字段，方便“解释/为什么”对话不重新扫描，直接读取最近一次结果。
    candidates.to_csv(out_path / f"explanations_raw_{run_date}.csv", index=False, encoding="utf-8-sig")
    allocated.to_csv(out_path / f"signals_raw_{run_date}.csv", index=False, encoding="utf-8-sig")
    market.details.to_csv(market_path, index=False, encoding="utf-8-sig")
    if errors:
        pd.DataFrame(errors).to_csv(errors_path, index=False, encoding="utf-8-sig")

    # 额外写 latest，方便小龙虾/定时任务固定读取
    candidates_out.to_csv(out_path / "latest_candidates.csv", index=False, encoding="utf-8-sig")
    signals_out.to_csv(out_path / "latest_signals.csv", index=False, encoding="utf-8-sig")
    explanations_out.to_csv(out_path / "latest_explanations.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(out_path / "latest_explanations_raw.csv", index=False, encoding="utf-8-sig")
    allocated.to_csv(out_path / "latest_signals_raw.csv", index=False, encoding="utf-8-sig")
    market.details.to_csv(out_path / "latest_market.csv", index=False, encoding="utf-8-sig")
    if prune_report is None:
        # 即使没有启用淘汰，也把最新股票池镜像写到 output，方便外部 Agent 查看。
        write_stock_pool(full_pool, out_path / "latest_pool.csv")

    report = format_detailed_report(candidates, allocated, market, cfg, account, prune_report=prune_report)
    report_path.write_text(report, encoding="utf-8")
    (out_path / "latest_report.md").write_text(report, encoding="utf-8")

    msg = format_message(allocated, market, account, prune_report=prune_report, candidates=candidates, cfg=cfg)
    msg_path = out_path / f"message_{run_date}.txt"
    msg_path.write_text(msg, encoding="utf-8")
    (out_path / "latest_message.txt").write_text(msg, encoding="utf-8")

    print(f"[输出] {signals_path}")
    print(f"[输出] {candidates_path}")
    print(f"[输出] {explanations_path}")
    print(f"[输出] {report_path}")
    if prune_report is not None:
        print(f"[股票池] {prune_report.message}")
    return allocated, candidates, market, msg_path, prune_report

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


def format_compact_message(
    signals: pd.DataFrame,
    market: MarketState,
    account: float,
    prune_report: Optional[PruneReport] = None,
    candidates: Optional[pd.DataFrame] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> str:
    cfg = cfg or DEFAULT_CONFIG
    lines: List[str] = []
    lines.append(f"A股K线策略信号 {now_cn().strftime('%Y-%m-%d %H:%M')}")
    lines.append(market.summary)
    lines.append("模型：大盘模式 + 板块主线硬门槛 + 个股板块前排 + 突破/回踩/天量锚点分型 + ATR风控仓位")
    if candidates is not None and not candidates.empty:
        n_total = len(candidates)
        n_signal_all = int(candidates["is_signal"].fillna(False).sum()) if "is_signal" in candidates.columns else 0
        n_error = int(candidates["filter_reason"].astype(str).str.contains("数据错误", na=False).sum()) if "filter_reason" in candidates.columns else 0
        error_note = f"；数据失败 {n_error} 只" if n_error else ""
        lines.append(f"股票池扫描：{n_total}只；买入候选：{n_signal_all}只；最终配置：{0 if signals is None or signals.empty else len(signals)}只{error_note}。")
        quality_note = format_data_quality_summary(candidates)
        if quality_note:
            lines.append(quality_note)
    lines.append("")
    if signals is None or signals.empty:
        lines.append("今日无最终买入配置。逐股原因已生成，但默认不在推送里展开。")
        lines.append("需要看原因时，对小龙虾说：解释今天为什么没有信号；或：为什么没买 600519。")
    else:
        total_weight = float(signals["target_weight"].sum()) if "target_weight" in signals.columns else 0.0
        lines.append(f"买入信号 {len(signals)} 只，建议合计仓位约 {total_weight:.0%}：")
        for _, r in signals.iterrows():
            code = str(r.get("code", ""))
            name = str(r.get("name", ""))
            close = safe_float(r.get("close"))
            score = safe_float(r.get("score"))
            w = safe_float(r.get("target_weight"), 0)
            stop = safe_float(r.get("stop_loss"))
            shares = r.get("target_shares", "")
            cash = safe_float(r.get("target_cash"), np.nan)
            if account and account > 0 and np.isfinite(cash):
                pos_text = f"仓位{w:.1%}，约{cash:,.0f}元，{shares}股"
            else:
                pos_text = f"仓位{w:.1%}"
            lines.append(f"- {code} {name}：收盘{close:.2f}，分数{score:.1f}，{pos_text}，止损{stop:.2f}")
        lines.append("需要看买入逻辑时，对小龙虾说：解释今天买入逻辑；或：解释 600519。")

    prune_text = format_prune_report(prune_report)
    if prune_text:
        lines.append("")
        lines.append("股票池维护：" + prune_text)
    lines.append("")
    lines.append("已生成：latest_signals.csv、latest_candidates.csv、latest_explanations.csv、latest_report.md。")
    lines.append("提示：本脚本只给信号和风控参考，不自动下单。")
    return "\n".join(lines)


def format_message(
    signals: pd.DataFrame,
    market: MarketState,
    account: float,
    prune_report: Optional[PruneReport] = None,
    candidates: Optional[pd.DataFrame] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> str:
    cfg = cfg or DEFAULT_CONFIG
    if not cfg.get("report", {}).get("include_explanations_in_message", False):
        return format_compact_message(signals, market, account, prune_report=prune_report, candidates=candidates, cfg=cfg)
    lines: List[str] = []
    report_cfg = cfg.get("report", {})
    show_no_signal_top = int(report_cfg.get("show_no_signal_top", 12))
    threshold = float(cfg["strategy"].get("score_threshold", 70))

    lines.append(f"A股K线策略信号 {now_cn().strftime('%Y-%m-%d %H:%M')}")
    lines.append(market.summary)
    lines.append("策略：大盘过滤 + 个股均线多头 + 相对强弱 + 阶段新高/量能 + ATR止损仓位")

    if candidates is not None and not candidates.empty:
        n_total = len(candidates)
        n_signal_all = int(candidates["is_signal"].fillna(False).sum()) if "is_signal" in candidates.columns else 0
        blocker_text = format_blocker_counts(candidates, cfg, top_n=6) if report_cfg.get("show_blocker_counts", True) else ""
        lines.append(f"股票池扫描：{n_total}只；达到买入候选：{n_signal_all}只；最终配置：{0 if signals is None or signals.empty else len(signals)}只。")
        quality_note = format_data_quality_summary(candidates)
        if quality_note:
            lines.append(quality_note)
        if blocker_text:
            lines.append(f"未买入主要原因Top：{blocker_text}")

    lines.append("")
    if signals is None or signals.empty:
        lines.append("今日没有新的买入信号。不是简单地“没信号”，而是下面这些规则没有同时满足。")
        if market.target_exposure <= 0:
            lines.append(f"- 大盘过滤：{market.summary}，策略禁止新开仓。")
        if candidates is not None and not candidates.empty:
            near = rank_no_signal_rows(candidates).head(show_no_signal_top)
            if not near.empty:
                lines.append(f"- 最接近但仍未买入的 {len(near)} 只：")
                for _, r in near.iterrows():
                    lines.append("  - " + format_one_stock_explanation(r, detail=False))
            else:
                lines.append("- 股票池里没有接近达标的候选。")
    else:
        total_weight = float(signals["target_weight"].sum()) if "target_weight" in signals.columns else 0.0
        lines.append(f"买入信号 {len(signals)} 只，建议合计仓位约 {total_weight:.0%}")
        for _, r in signals.iterrows():
            code = str(r.get("code", ""))
            name = str(r.get("name", ""))
            close = safe_float(r.get("close"))
            score = safe_float(r.get("score"))
            w = safe_float(r.get("target_weight"), 0)
            stop = safe_float(r.get("stop_loss"))
            tp1 = safe_float(r.get("take_profit_1"))
            shares = r.get("target_shares", "")
            cash = safe_float(r.get("target_cash"), np.nan)
            decision_reason = str(r.get("decision_reason", "")).strip()
            score_detail = str(r.get("score_detail", "")).strip()
            compact = str(r.get("compact_snapshot", "")).strip()
            if account and account > 0 and np.isfinite(cash):
                pos_text = f"仓位{w:.1%}，约{cash:,.0f}元，{shares}股"
            else:
                pos_text = f"仓位{w:.1%}"
            lines.append(f"- {code} {name}：收盘{close:.2f}，分数{score:.1f}，{pos_text}，止损{stop:.2f}，止盈1 {tp1:.2f}")
            if decision_reason:
                lines.append(f"  买入逻辑：{decision_reason}")
            if score_detail:
                lines.append(f"  评分拆解：{score_detail}")
            if compact:
                lines.append(f"  关键指标：{compact}")

        if candidates is not None and not candidates.empty:
            near = rank_no_signal_rows(candidates).head(show_no_signal_top)
            if not near.empty:
                lines.append("")
                lines.append(f"其他未买入股票的主要原因，展示最接近的 {len(near)} 只：")
                for _, r in near.iterrows():
                    lines.append("  - " + format_one_stock_explanation(r, detail=False))

    prune_text = format_prune_report(prune_report)
    if prune_text:
        lines.append("")
        lines.append("股票池维护：" + prune_text)

    lines.append("")
    lines.append("完整逐股解释已写入输出目录：latest_explanations.csv 和 latest_report.md。")
    lines.append(f"判断阈值：综合分 ≥ {threshold:.1f} 且必须通过硬过滤；未通过任一硬过滤，即使分数高也不买。")
    lines.append("执行纪律：不追高于计划仓位；跌破止损先退出；盈利后用10/20日线或2ATR移动止盈。")
    return "\n".join(lines)

def send_webhook(msg: str, url: str, webhook_type: str = "wecom") -> None:
    if not url:
        print("[推送] webhook_url 为空，跳过推送")
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
        raise RuntimeError(f"推送失败: HTTP {resp.status_code} {resp.text[:200]}")
    print("[推送] 已发送 webhook")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A股个股/大盘K线尾盘买入信号扫描器")
    parser.add_argument("--pool", required=True, help="股票池文件：CSV/TXT/XLSX，至少包含 code/代码 列，或第一列为股票代码")
    parser.add_argument("--config", default="", help="配置文件 YAML/JSON，可选")
    parser.add_argument("--out", default="output", help="输出目录")
    parser.add_argument("--account", type=float, default=100000.0, help="账户权益，用于换算买入金额/股数，默认 100000")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新拉取数据")
    parser.add_argument("--tail", action="store_true", help="尾盘实时模式：用实时行情近似合成今日K线；会自动使用不复权日K")
    parser.add_argument("--send", action="store_true", help="扫描/对话处理后发送 webhook")
    parser.add_argument("--webhook-url", default="", help="企业微信/钉钉/飞书 webhook URL；也可用环境变量 SIGNAL_WEBHOOK_URL")
    parser.add_argument("--webhook-type", default="", choices=["", "wecom", "dingtalk", "feishu", "generic"], help="webhook 类型")
    parser.add_argument("--market-index", default="", help="大盘指数，逗号分隔，例如 sh000001,sz399001,sh000300")
    parser.add_argument("--limit", type=int, default=0, help="只扫描前 N 只，测试用；使用该参数时不会自动淘汰股票池")
    parser.add_argument("--chat", default="", help="小龙虾/AI Agent 传入的自然语言命令，例如：加入 600519 贵州茅台 到股票池")
    parser.add_argument("--chat-file", default="", help="从文本文件读取自然语言命令")
    parser.add_argument("--auto-prune", dest="auto_prune", action="store_true", help="扫描完成后，如果股票池超过阈值，自动淘汰图形最弱的股票")
    parser.add_argument("--detail", action="store_true", help="把逐股原因也写入 latest_message.txt/推送；默认只输出精简摘要，原因按需查询")
    parser.add_argument("--no-auto-prune", dest="auto_prune", action="store_false", help="禁用自动淘汰；优先级高于配置文件")
    parser.set_defaults(auto_prune=None)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    if args.tail:
        cfg["data"]["use_realtime_tail"] = True
    if args.market_index:
        cfg["data"]["market_indices"] = [x.strip() for x in args.market_index.split(",") if x.strip()]
    if args.webhook_url:
        cfg["notify"]["webhook_url"] = args.webhook_url
    elif os.environ.get("SIGNAL_WEBHOOK_URL"):
        cfg["notify"]["webhook_url"] = os.environ["SIGNAL_WEBHOOK_URL"]
    if args.webhook_type:
        cfg["notify"]["webhook_type"] = args.webhook_type
    if args.send:
        cfg["notify"]["send"] = True
    if getattr(args, "detail", False):
        cfg.setdefault("report", {})["include_explanations_in_message"] = True

    chat_text = args.chat or ""
    if args.chat_file:
        chat_text = Path(args.chat_file).read_text(encoding="utf-8")

    try:
        if chat_text.strip():
            reply, reply_path = handle_chat_command(chat_text, args, cfg)
            print("\n" + reply + "\n")
            if cfg["notify"].get("send"):
                send_webhook(reply, cfg["notify"].get("webhook_url", ""), cfg["notify"].get("webhook_type", "wecom"))
            return 0

        auto_prune = bool(cfg.get("pool", {}).get("auto_prune", False)) if args.auto_prune is None else bool(args.auto_prune)
        signals, candidates, market, msg_path, prune_report = scan(
            args.pool,
            cfg,
            args.out,
            args.account,
            refresh=args.refresh,
            limit=args.limit,
            auto_prune=auto_prune,
        )
        msg = msg_path.read_text(encoding="utf-8")
        print("\n" + msg + "\n")
        if cfg["notify"].get("send"):
            send_webhook(msg, cfg["notify"].get("webhook_url", ""), cfg["notify"].get("webhook_type", "wecom"))
        return 0
    except KeyboardInterrupt:
        print("用户中断")
        return 130
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
