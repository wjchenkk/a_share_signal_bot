# -*- coding: utf-8 -*-
from __future__ import annotations

from .strategy import *
from .formatting import to_chinese_columns

def fetch_name_map(fetcher: AkshareFetcher) -> Dict[str, str]:
    try:
        spot = fetcher.stock_spot_all()
        if {"代码", "名称"}.issubset(spot.columns):
            return {str(c).zfill(6): str(n) for c, n in zip(spot["代码"], spot["名称"])}
    except Exception:
        pass
    try:
        import akshare as ak
        info = ak.stock_info_a_code_name()
        if {"code", "name"}.issubset(info.columns):
            return {str(c).zfill(6): str(n) for c, n in zip(info["code"], info["name"])}
    except Exception:
        pass
    return {}


def resolve_stock_by_name(term: str, fetcher: Optional[AkshareFetcher] = None, name_map: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    """把“贵州茅台”或“600519”解析成 (code, name)。名称模糊匹配到多只时会报错。"""
    term = str(term).strip()
    m = re.search(r"(\d{6})", term)
    if m:
        code = normalize_code(m.group(1))
        name = ""
        if name_map:
            name = name_map.get(code, "")
        return code, name

    if not term:
        raise ValueError("股票名称为空")
    if name_map is None:
        if fetcher is None:
            raise ValueError("没有可用的股票名称表，无法按名称匹配")
        name_map = fetch_name_map(fetcher)
    if not name_map:
        raise ValueError("未能获取 A 股代码名称表，建议直接发送 6 位股票代码")

    exact = [(c, n) for c, n in name_map.items() if str(n).strip() == term]
    if len(exact) == 1:
        return exact[0]
    fuzzy = [(c, n) for c, n in name_map.items() if term in str(n)]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(exact) > 1 or len(fuzzy) > 1:
        sample = (exact or fuzzy)[:8]
        choices = "、".join([f"{c} {n}" for c, n in sample])
        raise ValueError(f"名称“{term}”匹配到多只股票：{choices}。请改发 6 位代码。")
    raise ValueError(f"没有找到名称为“{term}”的 A 股股票，建议直接发送 6 位代码。")


def clean_stock_name(raw: str) -> str:
    s = str(raw)
    s = re.sub(r"\d{6}", " ", s)
    s = re.sub(r"(加入|添加|新增|加到|放入|放进|移入|删除|移除|剔除|去掉|踢出|查看|列表|解释|为什么|为啥|原因|逻辑|详细|复盘|有没有|没有|没买|不买|买入|信号|生成|扫描|筛选|代码|股票代码|证券代码|股票名称|证券简称|名称|股票池|池子|里面|里|中|到|今天|今日|请|帮我|把|这个|这只|一只)", " ", s)
    s = re.sub(r"[，,。；;：:/\\|\[\]（）(){}<>《》【】\"'`]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:30]


def extract_stock_items(text: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """从对话中提取股票代码/名称。返回 [(code, name)] 和需要按名称解析的 term。"""
    items: List[Tuple[str, str]] = []
    terms: List[str] = []
    seen_codes = set()

    # 优先按行解析，支持：加入 600519 贵州茅台 / 600519,贵州茅台
    lines = [x.strip() for x in re.split(r"[\n\r]+", text) if x.strip()]
    for line in lines:
        codes = list(re.finditer(r"\d{6}", line))
        if not codes:
            continue
        if len(codes) == 1:
            code = normalize_code(codes[0].group(0))
            name = clean_stock_name(line)
            if code not in seen_codes:
                items.append((code, name))
                seen_codes.add(code)
        else:
            # 多个代码在同一行时，先保证代码全部加入；名称可后续用名称表补齐。
            for m in codes:
                code = normalize_code(m.group(0))
                if code not in seen_codes:
                    items.append((code, ""))
                    seen_codes.add(code)

    if not items:
        term = clean_stock_name(text)
        # 去掉常见动词后剩下的内容，如果没有 6 位代码，就当作股票名尝试解析。
        if term and not re.search(r"(生成|扫描|筛选|信号|查看|列表|删除|移除|剔除|清理|整理|帮助|help)", term, re.I):
            terms.append(term)
    return items, terms


def parse_chat_action(text: str) -> ChatAction:
    raw = (text or "").strip()
    t = re.sub(r"\s+", " ", raw.lower())
    add_words = ["加入", "添加", "新增", "加到", "放入", "放进", "add"]
    remove_words = ["删除", "移除", "剔除", "去掉", "踢出", "del", "remove"]
    scan_words = ["生成买入信号", "买入信号", "生成信号", "扫描", "筛选", "跑一下", "跑一遍", "看信号", "主动生成", "signal"]
    list_words = ["查看股票池", "股票池列表", "股票池", "池子里", "多少只", "list"]
    prune_words = ["清理股票池", "整理股票池", "淘汰弱势", "删除图形差", "删除图形较差", "剔除弱势", "prune"]
    explain_words = ["解释", "为什么", "为啥", "原因", "逻辑", "详细", "复盘", "没信号", "没有信号", "为什么没买", "explain"]
    help_words = ["帮助", "怎么用", "help"]

    if any(w in raw for w in add_words) or any(w in t for w in ["add"]):
        items, terms = extract_stock_items(raw)
        return ChatAction("add", raw, items, terms)
    if any(w in raw for w in prune_words) or any(w in t for w in ["prune"]):
        return ChatAction("prune", raw, [], [], auto_prune=True)
    if any(w in raw for w in remove_words) or any(w in t for w in ["del", "remove"]):
        items, terms = extract_stock_items(raw)
        return ChatAction("remove", raw, items, terms)
    if any(w in raw for w in explain_words) or any(w in t for w in ["explain"]):
        items, terms = extract_stock_items(raw)
        auto_prune = any(w in raw for w in prune_words) or ("清理" in raw) or ("淘汰" in raw)
        return ChatAction("explain", raw, items, terms, auto_prune=auto_prune)
    if any(w in raw for w in scan_words) or any(w in t for w in ["signal"]):
        auto_prune = any(w in raw for w in prune_words) or ("清理" in raw) or ("淘汰" in raw)
        return ChatAction("scan", raw, [], [], auto_prune=auto_prune)
    if any(w in raw for w in list_words) or any(w in t for w in ["list"]):
        return ChatAction("list", raw, [], [])
    if any(w in raw for w in help_words) or any(w in t for w in ["help"]):
        return ChatAction("help", raw, [], [])
    return ChatAction("unknown", raw, [], [])


def add_items_to_stock_pool(pool_path: str, raw_items: List[Tuple[str, str]], terms: List[str], cfg: Dict[str, Any], refresh: bool = False) -> str:
    pool = read_stock_pool_or_empty(pool_path)
    fetcher = AkshareFetcher(cfg, refresh=refresh)
    name_map: Dict[str, str] = {}

    resolved: List[Tuple[str, str]] = []
    errors: List[str] = []
    for code, name in raw_items:
        try:
            code = normalize_code(code)
            if not name:
                if not name_map:
                    name_map = fetch_name_map(fetcher)
                name = name_map.get(code, "")
            resolved.append((code, name))
        except Exception as exc:
            errors.append(str(exc))
    for term in terms:
        try:
            if not name_map:
                name_map = fetch_name_map(fetcher)
            resolved.append(resolve_stock_by_name(term, fetcher=fetcher, name_map=name_map))
        except Exception as exc:
            errors.append(str(exc))

    if not resolved:
        return "没有识别到要加入的股票。示例：加入 600519 贵州茅台 到股票池。" + ("\n" + "\n".join(errors) if errors else "")

    before = len(pool)
    existing = set(pool["code"].astype(str)) if not pool.empty else set()
    added, updated, duplicated = [], [], []
    rows = pool.to_dict("records") if not pool.empty else []
    row_by_code = {str(r.get("code", "")): r for r in rows}

    for code, name in resolved:
        if code in existing:
            if name and not str(row_by_code.get(code, {}).get("name", "")).strip():
                row_by_code[code]["name"] = name
                updated.append((code, name))
            else:
                duplicated.append((code, name or row_by_code.get(code, {}).get("name", "")))
            continue
        rows.append({"code": code, "name": name})
        existing.add(code)
        added.append((code, name))

    if added or updated:
        backup_dir = cfg.get("pool", {}).get("backup_dir", "pool_backups")
        backup_stock_pool(pool_path, backup_dir)
        write_stock_pool(pd.DataFrame(rows), pool_path)

    after = len(read_stock_pool_or_empty(pool_path))
    parts = [f"股票池更新完成：原来 {before} 只，现在 {after} 只。"]
    if added:
        parts.append("已加入：" + "、".join([f"{c} {n}".strip() for c, n in added]))
    if updated:
        parts.append("已补全名称：" + "、".join([f"{c} {n}".strip() for c, n in updated]))
    if duplicated:
        parts.append("已存在，未重复加入：" + "、".join([f"{c} {n}".strip() for c, n in duplicated]))
    if errors:
        parts.append("未处理成功：" + "；".join(errors))
    return "\n".join(parts)


def remove_items_from_stock_pool(pool_path: str, raw_items: List[Tuple[str, str]], terms: List[str], cfg: Dict[str, Any]) -> str:
    pool = read_stock_pool_or_empty(pool_path)
    if pool.empty:
        return "股票池为空，不需要删除。"

    codes = {normalize_code(c) for c, _ in raw_items if re.search(r"\d{6}", str(c))}
    name_terms = [n for _, n in raw_items if n] + terms
    if not codes and not name_terms:
        # 没提取到代码时，退一步从原文清洗出来一个名称。
        fallback = clean_stock_name(" ".join([str(x) for pair in raw_items for x in pair]))
        if fallback:
            name_terms.append(fallback)

    mask = pool["code"].astype(str).isin(codes) if codes else pd.Series(False, index=pool.index)
    for term in name_terms:
        term = str(term).strip()
        if not term:
            continue
        mask = mask | pool["name"].astype(str).str.contains(re.escape(term), na=False)

    removed = pool[mask].copy()
    if removed.empty:
        return "股票池里没有找到要删除的股票。建议发送：删除 600519。"

    backup_dir = cfg.get("pool", {}).get("backup_dir", "pool_backups")
    backup_stock_pool(pool_path, backup_dir)
    remain = pool[~mask].copy().reset_index(drop=True)
    write_stock_pool(remain, pool_path)
    removed_text = "、".join([f"{r.code} {r.name}".strip() for r in removed.itertuples(index=False)])
    return f"已从股票池删除 {len(removed)} 只：{removed_text}\n当前股票池剩余 {len(remain)} 只。"


def format_pool_list(pool_path: str, max_show: int = 80) -> str:
    pool = read_stock_pool_or_empty(pool_path)
    if pool.empty:
        return "股票池为空。你可以发送：加入 600519 贵州茅台 到股票池。"
    lines = [f"股票池当前共 {len(pool)} 只。"]
    show = pool.head(max_show)
    for _, r in show.iterrows():
        lines.append(f"- {r['code']} {r.get('name', '')}".rstrip())
    if len(pool) > max_show:
        lines.append(f"……还有 {len(pool) - max_show} 只未显示。")
    return "\n".join(lines)


def help_message() -> str:
    return """小龙虾可用命令示例：
1) 加入股票：加入 600519 贵州茅台 到股票池
2) 按名称加入：加入 贵州茅台 到股票池（名称重名时会提示改用代码）
3) 删除股票：删除 600519
4) 查看股票池：查看股票池
5) 手动生成信号：生成买入信号
6) 手动扫描并清理弱势股：生成买入信号并清理股票池
7) 查看详细解释：解释今天为什么没有信号 / 为什么没买 600519

定时任务建议使用：python main.py --pool stock_pool.csv --config config.example.yml --out output --account 100000 --tail --auto-prune --send ...
默认只输出精简摘要；要看逐股原因，先生成信号后再问：解释今天为什么没有信号 / 为什么没买 600519。
""".strip()


def fmt_decimal_pct(x: Any, digits: int = 1, empty: str = "") -> str:
    """把 0.123 这种收益率/仓位格式化为 12.3%。"""
    v = safe_float(x)
    if not np.isfinite(v):
        return empty
    return f"{v:.{digits}%}"


def fmt_point_pct(x: Any, digits: int = 1, empty: str = "") -> str:
    """把 AkShare 的 2.3 这种涨跌幅点数格式化为 2.3%。"""
    v = safe_float(x)
    if not np.isfinite(v):
        return empty
    return f"{v:.{digits}f}%"


def fmt_price(x: Any, digits: int = 2, empty: str = "") -> str:
    v = safe_float(x)
    if not np.isfinite(v):
        return empty
    return f"{v:.{digits}f}"


def fmt_ratio(x: Any, digits: int = 2, empty: str = "") -> str:
    v = safe_float(x)
    if not np.isfinite(v):
        return empty
    return f"{v:.{digits}f}倍"


def unique_nonempty(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        s = str(item or "").strip().strip("；;，, ")
        if not s or s.lower() == "nan":
            continue
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out


def split_reason_text(text: Any) -> List[str]:
    if text is None or (isinstance(text, float) and np.isnan(text)):
        return []
    return unique_nonempty(re.split(r"[；;]", str(text)))


def build_metric_snapshot(r: pd.Series, compact: bool = False) -> str:
    close = fmt_price(r.get("close"))
    pct_chg = fmt_point_pct(r.get("pct_chg"))
    ret3 = fmt_decimal_pct(r.get("ret3"))
    ret5 = fmt_decimal_pct(r.get("ret5"))
    ret20 = fmt_decimal_pct(r.get("ret20"))
    ret60 = fmt_decimal_pct(r.get("ret60"))
    ret120 = fmt_decimal_pct(r.get("ret120"))
    rs60 = fmt_decimal_pct(r.get("rs_rank60"), 0)
    dd10 = fmt_decimal_pct(r.get("drawdown10"))
    dd20 = fmt_decimal_pct(r.get("drawdown20"))
    dd120 = fmt_decimal_pct(r.get("drawdown120"))
    risk_gate_reason = str(r.get("risk_gate_reason", "")).strip()
    ldc6 = safe_float(r.get("limit_down_count_6"), 0.0)
    amount_ratio = fmt_ratio(r.get("amount_ratio20"))
    risk_pct = fmt_decimal_pct(r.get("risk_pct"))
    stop_loss = fmt_price(r.get("stop_loss"))
    ma20 = fmt_price(r.get("ma20"))
    ma60 = fmt_price(r.get("ma60"))
    close_pos60 = fmt_decimal_pct(r.get("close_pos60"), 0)
    contraction = fmt_ratio(r.get("range_contraction_20_60"))
    setup_tags = str(r.get("setup_tags", "")).strip()
    provider = str(r.get("data_provider", "")).strip()

    if compact:
        parts = [
            f"收盘{close}" if close else "",
            f"涨跌{pct_chg}" if pct_chg else "",
            f"5日{ret5}" if ret5 else "",
            f"60日{ret60}" if ret60 else "",
            f"RS{rs60}" if rs60 else "",
            f"近6日跌停{int(ldc6)}次" if ldc6 else "",
            f"位置{close_pos60}" if close_pos60 else "",
            f"量比{amount_ratio}" if amount_ratio else "",
            f"止损{risk_pct}" if risk_pct else "",
        ]
        return "，".join(unique_nonempty(parts))

    parts = [
        f"收盘{close}" if close else "",
        f"当日涨跌{pct_chg}" if pct_chg else "",
        f"3/5/20/60/120日收益={ret3}/{ret5}/{ret20}/{ret60}/{ret120}" if (ret3 or ret5 or ret20 or ret60 or ret120) else "",
        f"股票池内60日强度排名={rs60}" if rs60 else "",
        f"近6日跌停/近跌停={int(ldc6)}次" if ldc6 else "",
        f"风险闸门={risk_gate_reason}" if risk_gate_reason else "",
        f"MA20={ma20}，MA60={ma60}" if (ma20 or ma60) else "",
        f"距10/20/120日高点={dd10}/{dd20}/{dd120}" if (dd10 or dd20 or dd120) else "",
        f"60日区间位置={close_pos60}" if close_pos60 else "",
        f"20/60日波动收敛={contraction}" if contraction else "",
        f"成交额/20日均额={amount_ratio}" if amount_ratio else "",
        f"形态标签={setup_tags}" if setup_tags else "",
        f"K线数据源={provider}" if provider else "",
        f"计划止损={stop_loss}，止损幅度={risk_pct}" if (stop_loss or risk_pct) else "",
    ]
    return "；".join(unique_nonempty(parts))


def add_decision_explanations(
    candidates: pd.DataFrame,
    allocated: pd.DataFrame,
    market: MarketState,
    cfg: Dict[str, Any],
    account: float = 0.0,
) -> pd.DataFrame:
    """给每只股票加上“买/不买”的可解释字段。"""
    if candidates is None or candidates.empty:
        return candidates

    df = candidates.copy()
    threshold = float(cfg["strategy"].get("score_threshold", 70))
    max_positions = int(cfg["strategy"].get("max_positions", 5))

    allocated_codes = set()
    alloc_by_code: Dict[str, Dict[str, Any]] = {}
    if allocated is not None and not allocated.empty and "code" in allocated.columns:
        for _, ar in allocated.iterrows():
            code = str(ar.get("code", "")).zfill(6)
            allocated_codes.add(code)
            alloc_by_code[code] = ar.to_dict()

    decisions: List[str] = []
    decision_reasons: List[str] = []
    positive_factors_all: List[str] = []
    negative_factors_all: List[str] = []
    metric_snapshots: List[str] = []
    compact_snapshots: List[str] = []
    buy_logic_all: List[str] = []

    for _, r in df.iterrows():
        code = str(r.get("code", "")).zfill(6)
        score = safe_float(r.get("score"), 0.0)
        ok_base = bool(r.get("ok_base", False))
        is_signal = bool(r.get("is_signal", False))
        filter_reasons = split_reason_text(r.get("filter_reason", ""))
        score_weaknesses = split_reason_text(r.get("score_weakness", ""))
        positives = split_reason_text(r.get("reason", ""))
        block_reasons: List[str] = []

        if filter_reasons:
            block_reasons.extend(filter_reasons)
        if score < threshold:
            block_reasons.append(f"综合分{score:.1f}低于阈值{threshold:.1f}")
        if score_weaknesses:
            block_reasons.extend(score_weaknesses)
        block_reasons = unique_nonempty(block_reasons)

        if code in allocated_codes:
            alloc = alloc_by_code.get(code, {})
            target_weight = safe_float(alloc.get("target_weight"), 0.0)
            target_shares = safe_float(alloc.get("target_shares"), 0.0)
            if target_weight > 0 and target_shares > 0:
                decision = "买入"
            elif target_weight > 0:
                decision = "买入信号但不足一手"
                block_reasons.append("按账户金额和一手100股规则计算，不足一手")
            else:
                decision = "买入信号但仓位为0"
                block_reasons.append("大盘/风险预算导致仓位为0")
            reason = f"通过硬过滤，综合分{score:.1f}≥{threshold:.1f}；" + ("；".join(positives) if positives else "形态评分达标")
        elif is_signal:
            decision = "达标未配置"
            reason = f"达到买入信号，但本策略最多配置前{max_positions}只；本票没有进入最终仓位列表"
            block_reasons.append(reason)
        else:
            decision = "不买"
            if not ok_base:
                reason = "未通过硬过滤：" + ("；".join(filter_reasons) if filter_reasons else "基础条件不足")
            elif score < threshold:
                reason = f"硬过滤通过，但综合分{score:.1f}低于阈值{threshold:.1f}"
            else:
                reason = "未形成最终买入信号"

        snapshot = build_metric_snapshot(r, compact=False)
        compact_snapshot = build_metric_snapshot(r, compact=True)
        score_detail = str(r.get("score_detail", "")).strip()
        positive_text = "；".join(unique_nonempty(positives)) or "暂无明显加分项"
        negative_text = "；".join(unique_nonempty(block_reasons)) or "无明显阻断项"

        if decision.startswith("买入"):
            logic = f"{decision}：{reason}。评分拆解：{score_detail}。关键指标：{snapshot}。"
        elif decision == "达标未配置":
            logic = f"暂不配置：{reason}。评分拆解：{score_detail}。关键指标：{snapshot}。"
        else:
            logic = f"不买：{reason}。主要阻断：{negative_text}。评分拆解：{score_detail}。关键指标：{snapshot}。"

        decisions.append(decision)
        decision_reasons.append(reason)
        positive_factors_all.append(positive_text)
        negative_factors_all.append(negative_text)
        metric_snapshots.append(snapshot)
        compact_snapshots.append(compact_snapshot)
        buy_logic_all.append(logic)

    df["decision"] = decisions
    df["decision_reason"] = decision_reasons
    df["positive_factors"] = positive_factors_all
    df["negative_factors"] = negative_factors_all
    df["metric_snapshot"] = metric_snapshots
    df["compact_snapshot"] = compact_snapshots
    df["buy_logic"] = buy_logic_all
    return df


def attach_explanations_to_allocated(allocated: pd.DataFrame, candidates: pd.DataFrame) -> pd.DataFrame:
    if allocated is None or allocated.empty or candidates is None or candidates.empty:
        return allocated
    out = allocated.copy()
    explain_cols = [
        "decision", "decision_reason", "positive_factors", "negative_factors", "metric_snapshot",
        "compact_snapshot", "buy_logic", "score_detail", "score_weakness",
        "trend_score", "momentum_score", "breakout_score", "risk_score",
    ]
    lookup = candidates[["code"] + [c for c in explain_cols if c in candidates.columns]].copy()
    lookup["code"] = lookup["code"].astype(str).str.zfill(6)
    out["code"] = out["code"].astype(str).str.zfill(6)
    for col in explain_cols:
        if col in out.columns:
            out = out.drop(columns=[col])
    return out.merge(lookup, on="code", how="left")


def blocker_counts(candidates: pd.DataFrame, cfg: Dict[str, Any], top_n: int = 8) -> List[Tuple[str, int]]:
    if candidates is None or candidates.empty:
        return []
    threshold = float(cfg["strategy"].get("score_threshold", 70))
    counts: Dict[str, int] = {}
    for _, r in candidates.iterrows():
        if bool(r.get("is_signal", False)):
            continue
        reasons = []
        reasons.extend(split_reason_text(r.get("filter_reason", "")))
        if safe_float(r.get("score"), 0.0) < threshold:
            reasons.append("综合分未达阈值")
        reasons.extend(split_reason_text(r.get("score_weakness", ""))[:3])
        for reason in unique_nonempty(reasons):
            counts[reason] = counts.get(reason, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:top_n]


def format_blocker_counts(candidates: pd.DataFrame, cfg: Dict[str, Any], top_n: int = 8) -> str:
    rows = blocker_counts(candidates, cfg, top_n=top_n)
    if not rows:
        return ""
    return "；".join([f"{reason}×{count}" for reason, count in rows])


def rank_no_signal_rows(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates is None or candidates.empty:
        return pd.DataFrame()
    df = candidates.copy()
    if "decision" not in df.columns:
        df["decision"] = np.where(df.get("is_signal", False), "买入", "不买")
    df = df[~df["decision"].astype(str).str.startswith("买入")].copy()
    if df.empty:
        return df
    for c in ["score", "rs_rank60", "ret60", "amount_ratio20"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            df[c] = np.nan
    return df.sort_values(["score", "rs_rank60", "ret60", "amount_ratio20"], ascending=[False, False, False, False])


def format_one_stock_explanation(r: pd.Series, detail: bool = True) -> str:
    code = str(r.get("code", "")).zfill(6)
    name = str(r.get("name", "")).strip()
    decision = str(r.get("decision", "")).strip() or ("买入" if bool(r.get("is_signal", False)) else "不买")
    score = safe_float(r.get("score"), 0.0)
    line1 = f"{code} {name}：{decision}，综合分{score:.1f}。"
    reason = str(r.get("decision_reason", "")).strip()
    compact = str(r.get("compact_snapshot", "")).strip()
    if not detail:
        tail = "；".join(unique_nonempty([reason, compact]))
        return line1 + (tail if tail else "")
    positives = str(r.get("positive_factors", "")).strip()
    negatives = str(r.get("negative_factors", "")).strip()
    score_detail = str(r.get("score_detail", "")).strip()
    snapshot = str(r.get("metric_snapshot", "")).strip()
    parts = [
        line1,
        f"结论原因：{reason}" if reason else "",
        f"加分项：{positives}" if positives else "",
        f"阻断/扣分项：{negatives}" if negatives else "",
        f"评分拆解：{score_detail}" if score_detail else "",
        f"关键指标：{snapshot}" if snapshot else "",
    ]
    return "\n".join(unique_nonempty(parts))


def filter_explanations_for_action(candidates: pd.DataFrame, action: ChatAction) -> pd.DataFrame:
    if candidates is None or candidates.empty:
        return pd.DataFrame()
    df = candidates.copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    codes = {normalize_code(c) for c, _ in action.items if re.search(r"\d{6}", str(c))}
    terms = [str(n).strip() for _, n in action.items if str(n).strip()] + [str(t).strip() for t in action.terms if str(t).strip()]
    mask = pd.Series(False, index=df.index)
    if codes:
        mask = mask | df["code"].isin(codes)
    for term in terms:
        mask = mask | df["name"].astype(str).str.contains(re.escape(term), na=False)
    return df[mask].copy() if mask.any() else pd.DataFrame()


def format_detailed_report(
    candidates: pd.DataFrame,
    signals: pd.DataFrame,
    market: MarketState,
    cfg: Dict[str, Any],
    account: float,
    prune_report: Optional[PruneReport] = None,
) -> str:
    lines: List[str] = []
    threshold = float(cfg["strategy"].get("score_threshold", 70))
    max_positions = int(cfg["strategy"].get("max_positions", 5))
    report_cfg = cfg.get("report", {})
    max_report_stocks = int(report_cfg.get("max_report_stocks", 120))

    lines.append(f"# A股K线策略逐股解释报告（{now_cn().strftime('%Y-%m-%d %H:%M')}）")
    lines.append("")
    lines.append("## 1. 今日大盘过滤")
    lines.append(f"- {market.summary}")
    if market.details is not None and not market.details.empty:
        for _, mr in market.details.iterrows():
            lines.append(
                f"- {mr.get('symbol', '')}：分数{safe_float(mr.get('score'), 0):.1f}，"
                f"20日收益{fmt_decimal_pct(mr.get('ret20'))}，60日收益{fmt_decimal_pct(mr.get('ret60'))}，"
                f"{'收盘站上20日线' if bool(mr.get('close_gt_ma20', False)) else '未站上20日线'}，"
                f"{'收盘站上60日线' if bool(mr.get('close_gt_ma60', False)) else '未站上60日线'}"
            )
    lines.append("")
    lines.append("## 2. 策略判定规则")
    lines.append(f"- 先看大盘：不是只看强弱，而是综合指数均线结构、回归趋势效率、位置、波动收敛、K线实体/上影线等盘面图形。大盘弱势时不新开仓；中性/强势时才允许配置，总仓位由盘面状态控制。")
    lines.append(
        "- 再看硬过滤：非ST、价格区间合适、20日均成交额达标、当日不追过大涨幅、不接过大跌幅、上影线不过长、趋势/图形结构达标、60日收益强于大盘、止损距离不过宽。"
    )
    lines.append(f"- 最后看评分：趋势模板35分 + 动量/相对强弱25分 + 形态/量能25分 + 风险结构15分；综合分 ≥ {threshold:.1f} 才算买入候选。")
    lines.append(f"- 仓位配置：候选达标后最多取前{max_positions}只，再用 ATR/均线止损距离反推目标仓位，单票亏损风险由配置里的 risk_per_trade_pct 控制。")
    lines.append("")

    n_total = len(candidates) if candidates is not None else 0
    n_buy = 0 if signals is None or signals.empty else len(signals)
    n_signal_all = int(candidates["is_signal"].fillna(False).sum()) if candidates is not None and not candidates.empty and "is_signal" in candidates.columns else n_buy
    lines.append("## 3. 今日总体结果")
    lines.append(f"- 股票池扫描 {n_total} 只；达到买入候选 {n_signal_all} 只；最终配置 {n_buy} 只。")
    blocker_text = format_blocker_counts(candidates, cfg, top_n=10)
    if blocker_text:
        lines.append(f"- 未买入主要原因：{blocker_text}")
    prune_text = format_prune_report(prune_report)
    if prune_text:
        lines.append(f"- 股票池维护：{prune_text}")
    lines.append("")

    lines.append("## 4. 买入/配置标的解释")
    if signals is None or signals.empty:
        lines.append("- 今日没有最终买入配置。原因通常是大盘过滤、硬过滤未通过、综合分不达阈值，或仓位/名额限制。")
    else:
        for _, r in signals.iterrows():
            lines.append(format_one_stock_explanation(r, detail=True))
            lines.append("")

    lines.append("## 5. 未买入标的逐股解释")
    no_sig = rank_no_signal_rows(candidates)
    if no_sig.empty:
        lines.append("- 没有未买入标的。")
    else:
        show = no_sig.head(max_report_stocks)
        for _, r in show.iterrows():
            lines.append(format_one_stock_explanation(r, detail=True))
            lines.append("")
        if len(no_sig) > len(show):
            lines.append(f"- 还有 {len(no_sig) - len(show)} 只未写入文本报告，可在 latest_explanations.csv 查看。")

    lines.append("\n> 本脚本只生成信号和风控参考，不自动下单；执行前仍需你确认流动性、公告、财报和个人风险承受能力。")
    return "\n".join(lines).strip() + "\n"


def prune_stock_pool_by_candidates(
    pool_path: str,
    full_pool: pd.DataFrame,
    candidates: pd.DataFrame,
    signals: pd.DataFrame,
    cfg: Dict[str, Any],
    out_path: Path,
    run_date: str,
    skipped_reason: str = "",
) -> PruneReport:
    p_cfg = cfg.get("pool", {})
    max_size = int(p_cfg.get("max_size", 50))
    prune_count = int(p_cfg.get("prune_count", 10))
    backup_dir = p_cfg.get("backup_dir", "pool_backups")
    size_before = len(full_pool) if full_pool is not None else 0

    if skipped_reason:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), f"股票池自动淘汰已跳过：{skipped_reason}")
    if size_before <= max_size:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), f"股票池 {size_before} 只，未超过 {max_size}，不淘汰。")
    if candidates is None or candidates.empty:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), "股票池超过阈值，但候选评分为空，本次不淘汰。")

    cand = candidates.copy()
    if "code" not in cand.columns:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), "候选结果缺少股票代码，本次不淘汰。")
    cand["code"] = cand["code"].astype(str).str.zfill(6)
    pool_codes = set(full_pool["code"].astype(str).str.zfill(6))
    signal_codes = set()
    if signals is not None and not signals.empty and "code" in signals.columns:
        signal_codes = set(signals["code"].astype(str).str.zfill(6))

    cand = cand[cand["code"].isin(pool_codes) & (~cand["code"].isin(signal_codes))].copy()
    if "filter_reason" in cand.columns:
        cand = cand[~cand["filter_reason"].astype(str).str.contains("数据错误", na=False)].copy()
    if cand.empty:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), "股票池超过阈值，但除买入信号/数据错误股票外没有可淘汰标的。")

    for c in ["score", "ret20", "ret60", "ret120", "drawdown120", "ma20_slope10", "ma60_slope20"]:
        if c in cand.columns:
            cand[c] = pd.to_numeric(cand[c], errors="coerce")
        else:
            cand[c] = np.nan
    if "filter_reason" not in cand.columns:
        cand["filter_reason"] = ""
    if "reason" not in cand.columns:
        cand["reason"] = ""
    if "name" not in cand.columns:
        cand["name"] = ""

    reason_text = cand["filter_reason"].astype(str)
    # 大盘弱势会让所有股票都被过滤，不应单独作为淘汰依据；个股趋势/相对强度才是“图形差”的核心。
    cand["bad_trend_penalty"] = reason_text.str.contains("趋势未达标", na=False).astype(int) * 12
    cand["bad_rs_penalty"] = reason_text.str.contains("相对强度不足", na=False).astype(int) * 8
    cand["bad_liq_penalty"] = reason_text.str.contains("成交额不足", na=False).astype(int) * 3
    cand["invalid_penalty"] = reason_text.str.contains("历史K线不足|收盘价无效", na=False).astype(int) * 30
    cand["prune_score"] = cand["score"].fillna(0) - cand["bad_trend_penalty"] - cand["bad_rs_penalty"] - cand["bad_liq_penalty"] - cand["invalid_penalty"]
    cand["ret60_prune"] = cand["ret60"].fillna(-9)
    cand["ret120_prune"] = cand["ret120"].fillna(-9)
    cand["drawdown_prune"] = cand["drawdown120"].fillna(-9)

    n_remove = min(prune_count, len(cand))
    removed = cand.sort_values(
        ["prune_score", "ret60_prune", "ret120_prune", "drawdown_prune"],
        ascending=[True, True, True, True],
    ).head(n_remove).copy()
    remove_codes = set(removed["code"].astype(str).str.zfill(6))
    if not remove_codes:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), "没有可淘汰标的。")

    backup_path = backup_stock_pool(pool_path, backup_dir)
    new_pool = full_pool[~full_pool["code"].astype(str).str.zfill(6).isin(remove_codes)].copy().reset_index(drop=True)
    write_stock_pool(new_pool, pool_path)
    size_after = len(new_pool)

    removed = removed.copy()
    removed["淘汰原因"] = removed.apply(
        lambda r: str(r.get("filter_reason", "")).replace("大盘弱势禁止新开仓", "").strip("；") or str(r.get("reason", "")) or "综合评分靠后",
        axis=1,
    )
    removed_path = out_path / f"pruned_{run_date}.csv"
    latest_removed_path = out_path / "latest_pruned.csv"
    to_chinese_columns(removed).to_csv(removed_path, index=False, encoding="utf-8-sig")
    to_chinese_columns(removed).to_csv(latest_removed_path, index=False, encoding="utf-8-sig")
    write_stock_pool(new_pool, out_path / "latest_pool.csv")

    show = "、".join([f"{r.code} {r.name}".strip() for r in removed[["code", "name"]].itertuples(index=False)])
    msg = f"股票池超过 {max_size} 只，已自动淘汰图形较差的 {len(removed)} 只；{size_before} → {size_after}。淘汰：{show}"
    return PruneReport(True, True, size_before, size_after, removed, msg, backup_path=backup_path)


def format_prune_report(report: Optional[PruneReport]) -> str:
    if report is None or not report.enabled:
        return ""
    return report.message or ""


def read_latest_candidates_raw(out_path: Path) -> pd.DataFrame:
    raw_path = out_path / "latest_explanations_raw.csv"
    if raw_path.exists():
        try:
            df = pd.read_csv(raw_path, dtype={"code": str})
            if "code" in df.columns:
                df["code"] = df["code"].astype(str).str.zfill(6)
            return df
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def explain_from_latest_output(action: ChatAction, out_path: Path) -> str:
    candidates = read_latest_candidates_raw(out_path)
    if not candidates.empty:
        selected = filter_explanations_for_action(candidates, action)
        if not selected.empty:
            return "\n\n".join([format_one_stock_explanation(r, detail=True) for _, r in selected.iterrows()])
    report_path = out_path / "latest_report.md"
    if report_path.exists():
        return report_path.read_text(encoding="utf-8")
    msg_path = out_path / "latest_message.txt"
    if msg_path.exists():
        return msg_path.read_text(encoding="utf-8")
    return "还没有找到最近一次扫描结果。请先发送：生成买入信号。"

