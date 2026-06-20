# A股 v6.3 主线优先 + 盘中持仓监控版

本版包含两个独立模块：

1. `main.py`：尾盘/收盘候选股票池买入信号。
2. `position_monitor.py`：盘中持仓交易建议，适合 10:00-10:30、14:00-14:30 每 10 分钟调用。

脚本只给信号和风控建议，不自动下单。

## 安装

```bash
mkdir -p ~/project
unzip -o a_share_signal_bot_v6_3.zip -d ~/project
cd ~/project/a_share_signal_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp -n stock_pool_sample.csv stock_pool.csv
cp -n portfolio_sample.csv portfolio.csv
```

## v6.3 买入信号改动

v6.3 不再把板块当成普通加分项，而是把“强主线 + 板块前排”作为入场门槛。

流程：

```text
A股风险闸门
  ↓
大盘模式
  ↓
板块主线硬门槛：优先用东方财富行业/概念板块日K计算
  ↓
个股板块内前排：20/60日强度、是否跑赢板块
  ↓
买点分型：突破 / 主线回踩 / 天量锚点再异动
  ↓
信号等级：S / A / B / C
  ↓
ATR风险仓位
```

### 三类买点

`breakout`：板块不弱，个股前排，突破20/60日平台高点，突破前波动收敛，成交额为20日均额约1.2-2.6倍，收盘强、上影不长。

`pullback`：只在强主线里做。强主线优先看5/10日线，普通主线看10/20日线；必须回踩关键均线附近、收盘收回、回踩缩量，且个股是板块前排。

`volume_anchor_reaccumulation`：前期天量涨停/大阳锚点后，不跌破锚点低点，后续缩量调整，再次放量站回5/10日线或突破整理区间。

## 生成买入信号

```bash
cd ~/project/a_share_signal_bot
printf '%s\n' '生成买入信号' | ./stockbot_chat.sh
```

查看原因：

```bash
printf '%s\n' '为什么没买 600519' | ./stockbot_chat.sh
```

定时尾盘扫描：

```bash
ACCOUNT=200000 ./run_tail_prune.sh
```

## 持仓文件格式

`portfolio.csv` 示例：

```csv
总资金,可用现金,股票代码,股票名称,股票股数,买入价格
200000,50000,600519,贵州茅台,100,1500.00
200000,50000,300750,宁德时代,200,210.00
```

必须有：股票代码、股数、买入价格。可选：总资金、可用现金、股票名称。

## 盘中持仓监控

手动运行：

```bash
cd ~/project/a_share_signal_bot
ACCOUNT=200000 PORTFOLIO=portfolio.csv ./run_position_monitor.sh
```

对话触发：

```text
查看持仓交易建议
```

输出：

```text
position_output/latest_position_message.txt
position_output/latest_position_actions.csv
```

盘中模块会根据日K趋势、实时价、5分钟K/VWAP、止损线、趋势防守线和浮盈保护给出：

```text
持有观察 / 止损卖出 / 趋势破位减仓 / 部分止盈 / 盈利保护止盈 / 趋势加仓
```

给出的是参考价格区间和整手股数，不会下单。

## OpenClaw 定时建议

10:00-10:30 每10分钟：

```bash
openclaw cron create "0,10,20,30 10 * * 1-5" "请运行：cd ~/project/a_share_signal_bot && ACCOUNT=200000 PORTFOLIO=portfolio.csv ./run_position_monitor.sh。把输出原样发给我，不要自动下单。" --name "a-share-position-am" --tz "Asia/Shanghai" --session isolated --announce --channel telegram --to "你的聊天ID"
```

14:00-14:30 每10分钟：

```bash
openclaw cron create "0,10,20,30 14 * * 1-5" "请运行：cd ~/project/a_share_signal_bot && ACCOUNT=200000 PORTFOLIO=portfolio.csv ./run_position_monitor.sh。把输出原样发给我，不要自动下单。" --name "a-share-position-pm" --tz "Asia/Shanghai" --session isolated --announce --channel telegram --to "你的聊天ID"
```

尾盘候选扫描仍可保留在 14:50 或 15:05。
