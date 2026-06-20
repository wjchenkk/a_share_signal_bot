#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话式持仓文件管理。

用于 OpenClaw/小龙虾对话更新 portfolio.csv：查看、添加/修改、删除、清空、导入。
本脚本只改本地 CSV，不会下单。
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

COLUMNS = ["总资金", "可用现金", "股票代码", "股票名称", "股票股数", "买入价格"]
CODE_COL = "股票代码"
NAME_COL = "股票名称"
SHARES_COL = "股票股数"
COST_COL = "买入价格"
TOTAL_COL = "总资金"
CASH_COL = "可用现金"


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize_code(x: object) -> str:
    s = str(x or "").strip()
    m = re.search(r"(\d{6})", s)
    if not m:
        return ""
    return m.group(1)


def parse_number(text: object, default: float = 0.0) -> float:
    s = str(text or "").strip().replace(",", "")
    if not s:
        return default
    # 支持 20w / 20万 / 1.5w / 1.5万
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return default
    v = float(m.group(0))
    if pd.isna(v):
        return default
    tail = s[m.end():]
    if "万" in tail.lower() or "w" in tail.lower():
        v *= 10000
    return v


def read_csv_flexible(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, dtype=str, encoding="gbk")
    if df.empty and len(df.columns) == 0:
        return pd.DataFrame(columns=COLUMNS)
    return normalize_df(df)


def find_col(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    lower = {str(c).strip().lower(): c for c in df.columns}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS)
    code_col = find_col(df, ["股票代码", "证券代码", "代码", "股票", "code", "symbol"])
    name_col = find_col(df, ["股票名称", "证券简称", "名称", "简称", "name"])
    shares_col = find_col(df, ["股票股数", "持仓股数", "股数", "数量", "shares", "qty", "quantity"])
    cost_col = find_col(df, ["买入价格", "成本价", "买入价", "持仓成本", "成本", "cost_price", "buy_price", "cost"])
    total_col = find_col(df, ["总资金", "账户总资金", "账户权益", "total_equity", "total_funds", "account", "equity"])
    cash_col = find_col(df, ["可用现金", "可用资金", "现金", "cash", "available_cash"])
    rows = []
    default_total = ""
    default_cash = ""
    if total_col is not None and not df[total_col].dropna().empty:
        default_total = str(df[total_col].dropna().iloc[0]).strip()
    if cash_col is not None and not df[cash_col].dropna().empty:
        default_cash = str(df[cash_col].dropna().iloc[0]).strip()
    if code_col is None:
        return pd.DataFrame(columns=COLUMNS)
    for _, r in df.iterrows():
        code = normalize_code(r.get(code_col, ""))
        if not code:
            continue
        name = str(r.get(name_col, "")).strip() if name_col else ""
        shares = int(parse_number(r.get(shares_col, 0), 0)) if shares_col else 0
        cost = parse_number(r.get(cost_col, 0), 0) if cost_col else 0
        if shares <= 0 or cost <= 0:
            continue
        total = str(r.get(total_col, default_total)).strip() if total_col else default_total
        cash = str(r.get(cash_col, default_cash)).strip() if cash_col else default_cash
        rows.append({TOTAL_COL: total, CASH_COL: cash, CODE_COL: code, NAME_COL: name, SHARES_COL: shares, COST_COL: cost})
    out = pd.DataFrame(rows, columns=COLUMNS)
    return out


def backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    bdir = path.parent / "portfolio_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dst = bdir / f"{path.stem}_{now_tag()}.csv"
    shutil.copy2(path, dst)
    return dst


def save(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = normalize_df(df) if not set(COLUMNS).issubset(set(df.columns)) else df.copy()
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = ""
    df = df[COLUMNS]
    df.to_csv(path, index=False, encoding="utf-8-sig")


def infer_name(msg: str, code: str) -> str:
    # 去掉命令词、代码和数值附近词后，尽量保留中文名称；用户不写也允许为空
    s = msg
    s = re.sub(r"(添加|加入|新增|买入|修改|更新|设置|持仓|到持仓|股票|代码|股数|数量|成本价|成本|买入价|买入价格|价格|可用现金|现金|总资金|账户)", " ", s)
    s = s.replace(code, " ")
    s = re.sub(r"\d+(?:\.\d+)?\s*(?:股|手|元|块|万|w)?", " ", s, flags=re.I)
    # 提取连续中文/英文简称，避免把标点和说明吃进去
    cands = re.findall(r"[\u4e00-\u9fffA-Za-z]{2,12}", s)
    bad = {"加入", "添加", "修改", "更新", "删除", "清空", "重置", "持仓", "股票", "成本", "价格", "股数", "数量", "总资金", "可用现金"}
    cands = [x for x in cands if x not in bad]
    return cands[0] if cands else ""


def extract_account(msg: str) -> Tuple[Optional[float], Optional[float]]:
    total = None; cash = None
    m = re.search(r"(?:总资金|账户总资金|账户权益|资金)\s*[:：=为]?\s*([0-9,.]+\s*(?:万|w)?)", msg, re.I)
    if m:
        total = parse_number(m.group(1), 0)
    m = re.search(r"(?:可用现金|可用资金|现金)\s*[:：=为]?\s*([0-9,.]+\s*(?:万|w)?)", msg, re.I)
    if m:
        cash = parse_number(m.group(1), 0)
    return total, cash


def extract_position(msg: str) -> Tuple[str, str, Optional[int], Optional[float], Optional[float], Optional[float]]:
    code = normalize_code(msg)
    shares = None
    cost = None
    # 股数支持 100股、1手、股数100、数量100
    m = re.search(r"(?:股数|数量|持仓股数)\s*[:：=为]?\s*([0-9,.]+)\s*(手|股)?", msg)
    if not m:
        m = re.search(r"([0-9,.]+)\s*(手|股)", msg)
    if m:
        val = parse_number(m.group(1), 0)
        unit = m.group(2) if len(m.groups()) >= 2 else "股"
        shares = int(val * 100 if unit == "手" else val)
    # 成本/买入价
    m = re.search(r"(?:买入价格|买入价|成本价|持仓成本|成本|价格)\s*[:：=为]?\s*([0-9,.]+)", msg)
    if m:
        cost = parse_number(m.group(1), 0)
    total, cash = extract_account(msg)
    name = infer_name(msg, code) if code else ""
    return code, name, shares, cost, total, cash


def set_account_fields(df: pd.DataFrame, total: Optional[float], cash: Optional[float]) -> pd.DataFrame:
    out = df.copy()
    for c in COLUMNS:
        if c not in out.columns:
            out[c] = ""
    if total is not None and total > 0:
        out[TOTAL_COL] = f"{total:.2f}"
    if cash is not None and cash >= 0:
        out[CASH_COL] = f"{cash:.2f}"
    return out[COLUMNS]


def format_holdings(df: pd.DataFrame, path: Path) -> str:
    df = read_csv_flexible(path) if df is None else df
    if df.empty:
        return f"当前持仓为空。文件：{path}"
    def first_valid(col: str) -> str:
        if col not in df.columns:
            return "未设置"
        vals = [str(v).strip() for v in df[col].dropna().tolist() if str(v).strip() and str(v).strip().lower() != "nan"]
        return vals[0] if vals else "未设置"
    total = first_valid(TOTAL_COL)
    cash = first_valid(CASH_COL)
    lines = [f"当前持仓 {len(df)} 只；总资金 {total}；可用现金 {cash}。"]
    for _, r in df.iterrows():
        lines.append(f"- {r[CODE_COL]} {r[NAME_COL]}：{int(float(r[SHARES_COL]))}股，成本/买入价 {float(r[COST_COL]):.3f}")
    return "\n".join(lines)


def parse_import_block(msg: str) -> pd.DataFrame:
    # 支持用户直接粘 CSV，或在“导入持仓 覆盖”后换行粘 CSV。
    text = msg.strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # 找到第一行看起来像表头的位置
    start = 0
    for i, ln in enumerate(lines):
        if ("," in ln or "\t" in ln) and re.search(r"股票代码|code|证券代码|代码", ln, re.I):
            start = i
            break
    else:
        # 容错：把每行“代码 名称 股数 成本”解析成表
        rows = []
        for ln in lines:
            code, name, shares, cost, total, cash = extract_position(ln)
            if code and shares and cost:
                rows.append({TOTAL_COL: total or "", CASH_COL: cash or "", CODE_COL: code, NAME_COL: name, SHARES_COL: shares, COST_COL: cost})
        return pd.DataFrame(rows, columns=COLUMNS)
    csv_text = "\n".join(lines[start:])
    sep = "\t" if "\t" in lines[start] else ","
    df = pd.read_csv(io.StringIO(csv_text), dtype=str, sep=sep)
    return normalize_df(df)


def handle_message(msg: str, portfolio_path: Path) -> str:
    msg = msg.strip()
    df = read_csv_flexible(portfolio_path)
    # 查看
    if re.search(r"查看持仓|持仓列表|当前持仓|我的持仓", msg):
        return format_holdings(df, portfolio_path)
    # 清空/重置
    if re.search(r"清空持仓|重置持仓|情况持仓|清除持仓|全部删除持仓", msg):
        b = backup(portfolio_path)
        save(portfolio_path, pd.DataFrame(columns=COLUMNS))
        return f"已清空持仓。备份：{b if b else '无旧文件'}。"
    # 导入
    if re.search(r"导入持仓|批量导入持仓|覆盖持仓|追加持仓", msg):
        new_df = parse_import_block(msg)
        if new_df.empty:
            return "没有识别到可导入的持仓。请粘贴CSV，例如：\n股票代码,股票名称,股票股数,买入价格\n600519,贵州茅台,100,1500"
        total, cash = extract_account(msg)
        new_df = set_account_fields(new_df, total, cash)
        b = backup(portfolio_path)
        if re.search(r"追加|添加到", msg) and not df.empty:
            merged = pd.concat([df, new_df], ignore_index=True)
            # 同代码后者覆盖前者
            merged[CODE_COL] = merged[CODE_COL].astype(str).str.zfill(6)
            merged = merged.drop_duplicates(subset=[CODE_COL], keep="last")
            save(portfolio_path, merged)
            return f"已追加/合并导入 {len(new_df)} 条持仓。备份：{b if b else '无旧文件'}。\n" + format_holdings(read_csv_flexible(portfolio_path), portfolio_path)
        save(portfolio_path, new_df)
        return f"已覆盖导入 {len(new_df)} 条持仓。备份：{b if b else '无旧文件'}。\n" + format_holdings(read_csv_flexible(portfolio_path), portfolio_path)
    # 删除单只
    if re.search(r"删除持仓|移除持仓|删掉持仓|从持仓.*删除|卖完|清仓记录", msg):
        code = normalize_code(msg)
        if not code:
            return "请提供要删除的6位股票代码，例如：删除持仓 600519。"
        if df.empty or code not in set(df[CODE_COL].astype(str).str.zfill(6)):
            return f"持仓中没有找到 {code}。"
        b = backup(portfolio_path)
        out = df[df[CODE_COL].astype(str).str.zfill(6) != code].copy()
        save(portfolio_path, out)
        return f"已删除持仓 {code}。备份：{b if b else '无旧文件'}。\n" + format_holdings(read_csv_flexible(portfolio_path), portfolio_path)
    # 设置总资金/现金
    if re.search(r"设置.*(总资金|可用现金|可用资金|现金)|修改.*(总资金|可用现金|可用资金|现金)", msg):
        total, cash = extract_account(msg)
        if total is None and cash is None:
            return "没有识别到总资金或可用现金，例如：设置持仓总资金20万 可用现金5万。"
        b = backup(portfolio_path)
        df = set_account_fields(df, total, cash)
        save(portfolio_path, df)
        return f"已更新账户字段。备份：{b if b else '无旧文件'}。\n" + format_holdings(read_csv_flexible(portfolio_path), portfolio_path)
    # 添加/修改单只持仓
    if re.search(r"添加持仓|加入持仓|新增持仓|买入持仓|修改持仓|更新持仓|设置持仓", msg):
        code, name, shares, cost, total, cash = extract_position(msg)
        if not code:
            return "没有识别到股票代码。示例：添加持仓 600519 贵州茅台 100股 成本1500。"
        existing = df[df[CODE_COL].astype(str).str.zfill(6) == code] if not df.empty else pd.DataFrame()
        if shares is None and not existing.empty:
            shares = int(float(existing.iloc[0][SHARES_COL]))
        if cost is None and not existing.empty:
            cost = float(existing.iloc[0][COST_COL])
        if shares is None or shares <= 0:
            return "没有识别到有效股数。示例：添加持仓 600519 贵州茅台 100股 成本1500。"
        if cost is None or cost <= 0:
            return "没有识别到有效买入价格/成本。示例：添加持仓 600519 贵州茅台 100股 成本1500。"
        if not existing.empty and not name:
            name = str(existing.iloc[0][NAME_COL])
        # 先从原表保留账户字段，再删除/覆盖单只持仓
        if total is None and not df.empty and TOTAL_COL in df.columns:
            vals = [v for v in df[TOTAL_COL].dropna().tolist() if str(v).strip() and str(v).strip().lower() != "nan"]
            total = parse_number(vals[0], 0) if vals else None
            if total == 0:
                total = None
        if cash is None and not df.empty and CASH_COL in df.columns:
            vals = [v for v in df[CASH_COL].dropna().tolist() if str(v).strip() and str(v).strip().lower() != "nan"]
            cash = parse_number(vals[0], 0) if vals else None
        b = backup(portfolio_path)
        df = df[df[CODE_COL].astype(str).str.zfill(6) != code].copy() if not df.empty else pd.DataFrame(columns=COLUMNS)
        new_row = {TOTAL_COL: f"{total:.2f}" if total and total > 0 else "", CASH_COL: f"{cash:.2f}" if cash is not None and cash >= 0 else "", CODE_COL: code, NAME_COL: name, SHARES_COL: int(shares), COST_COL: float(cost)}
        if df.empty:
            out = pd.DataFrame([new_row], columns=COLUMNS)
        else:
            out = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        save(portfolio_path, out)
        verb = "更新" if not existing.empty else "添加"
        return f"已{verb}持仓 {code} {name}：{int(shares)}股，成本/买入价 {float(cost):.3f}。备份：{b if b else '无旧文件'}。\n" + format_holdings(read_csv_flexible(portfolio_path), portfolio_path)
    return "没有识别到持仓管理命令。支持：查看持仓、添加/修改持仓、删除持仓、清空持仓、导入持仓、设置总资金/可用现金。"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="对话式持仓管理")
    ap.add_argument("--portfolio", default="portfolio.csv")
    ap.add_argument("--message-file", default="-")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.message_file == "-":
        msg = sys.stdin.read()
    else:
        msg = Path(args.message_file).read_text(encoding="utf-8")
    try:
        print(handle_message(msg, Path(args.portfolio)))
        return 0
    except Exception as exc:
        print(f"持仓管理失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
