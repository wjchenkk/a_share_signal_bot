# v6.8 买入后交易计划生命周期

这版新增 `trade_manager.py`，用于解决“只有买入信号，没有后续止损/止盈/加仓/卖出计划”的问题。

## 核心规则

与严格回测保持一致：

1. T 日尾盘/收盘生成买入信号；
2. T+1 开盘按计划买入；
3. A 股 T+1：T+1 买入当天不提示卖出、止损、止盈；
4. T+2 起管理持仓；
5. 盘中价格触发：止损、1.5R 半仓止盈、3R 清仓；
6. 收盘确认触发：跌破 MA20、跌破 MA60、大盘弱势、超过最大持仓天数，统一提示下一交易日开盘卖出；
7. 可选加仓：只有当前仍在买入信号列表、持仓盈利、5/10/20 日线多头、不过热时才提示小步加仓。

本脚本只输出建议，不自动下单。

## 文件

- `trade_manager.py`：交易生命周期主脚本；
- `trade_state.csv`：交易计划状态文件，记录 entry_date、entry_price、stop_loss、TP1、TP2、tp1_done；
- `trade_output/latest_trade_plan.txt`：小龙虾回复用文字计划；
- `trade_output/latest_trade_actions.csv`：结构化操作清单。

## 常用命令

### 1. 尾盘扫描后生成 T+1 买入计划

```bash
cd ~/project/a_share_signal_bot
source .venv/bin/activate
python trade_manager.py --action from_signals --signals-out output --state trade_state.csv --out trade_output
```

### 2. 买入后同步 portfolio.csv

先用已有的持仓管理功能把实际成交写入 `portfolio.csv`，例如：

```text
添加持仓 600519 贵州茅台 100股 成本1500 总资金20万 可用现金5万
```

然后同步交易状态：

```bash
python trade_manager.py --action sync --portfolio portfolio.csv --state trade_state.csv --out trade_output --account 200000
```

同步后，PENDING_BUY 会变成 ACTIVE，并按真实买入成本重算 1.5R/3R。

### 3. 盘中看是否需要止损/止盈/加仓

```bash
python trade_manager.py --action advise --mode intraday --sync --portfolio portfolio.csv --state trade_state.csv --out trade_output --account 200000
```

### 4. 收盘后生成明日开盘卖出计划

```bash
python trade_manager.py --action advise --mode close --sync --portfolio portfolio.csv --state trade_state.csv --out trade_output --account 200000
```

如果收盘跌破 MA20/MA60 或大盘弱势，会写入 `pending_exit_reason`，并提示下一交易日开盘卖出。

### 5. 一键全流程

尾盘定时脚本 `run_tail_prune.sh` 已经集成：

```bash
ACCOUNT=200000 ./run_tail_prune.sh
```

它会先生成买入信号，再更新 `trade_state.csv`，并输出持仓后续操作计划。

### 6. 小龙虾对话触发

可直接说：

```text
生成交易计划
同步持仓交易状态
我的持仓后续怎么办
今天止损止盈怎么操作
收盘后给我明天操作计划
```

## 与 `position_monitor.py` 的区别

`position_monitor.py` 更偏盘中风控，会用 VWAP/分钟线判断盘中转弱；`trade_manager.py` 是和严格回测一致的主策略生命周期管理。

建议优先使用 `trade_manager.py` 作为正式交易计划，`position_monitor.py` 作为辅助盘中预警。
