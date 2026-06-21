# A股 v6.3 主线优先 + 盘中持仓监控版

本版包含两个独立模块：

1. `main.py`：尾盘/收盘候选股票池买入信号。
2. `position_monitor.py`：盘中持仓交易建议，适合 10:00-10:30、14:00-14:30 每 10 分钟调用。

脚本只给信号和风控建议，不自动下单。

## 当前代码结构

顶层 `main.py`、`backtest.py`、`trade_manager.py` 等文件保留为兼容入口；业务实现已经拆到 `a_share_signal_bot/` 包内。模块职责和回归测试入口见 [docs/architecture.md](docs/architecture.md)。

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


---

# A 股 K 线尾盘信号扫描器 v6：主线-买点分型版

用于每天尾盘或收盘后扫描股票池，输出买入信号、仓位、止损、止盈参考，并支持 OpenClaw/小龙虾通过对话更新股票池、按需查看解释。

> 本项目只生成信号，不自动下单。策略结果不是投资建议，实盘前请先小仓/模拟验证。

## 1. 推荐目录

```bash
mkdir -p ~/project
unzip a_share_signal_bot_v6.zip -d ~/project
cd ~/project/a_share_signal_bot
```

## 2. 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp -n stock_pool_sample.csv stock_pool.csv
```

国内网络可用：

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 3. 股票池格式

v6.1 开始，股票池不需要你手工填写板块，只维护代码和名称即可：

```csv
code,name
600519,贵州茅台
000858,五粮液
300750,宁德时代
```

运行扫描时脚本会自动通过 AkShare 的东方财富行业/概念板块成份股接口反查所属板块，并缓存到：

```text
cache/auto_sector_map.csv
```

如果配置 `auto_write_back: true`，还会把自动识别到的板块写回 `stock_pool.csv`，后续不用重复构建。

优先级为：`stock_pool.csv` 已有板块列 > `sector_map.csv` 手工覆盖 > `cache/auto_sector_map.csv` 自动缓存 > AkShare 自动反查 > 未分组。

字段仍兼容：`sector / industry / 板块 / 行业 / 概念 / 所属板块 / 所属行业`。

## 4. 数据源

个股 K 线默认顺序：

```yaml
hist_providers: ["tencent", "sina", "eastmoney"]
```

东方财富被限制时，会优先使用腾讯/新浪历史 K 线兜底。

## 5. v6 策略逻辑

v6 不再把突破、回踩、量能异动混成一个综合形态分，而是按以下流程：

```text
数据检查
  ↓
A股风险闸门：连续跌停、高位崩盘、暴涨后踩踏直接一票否决
  ↓
大盘量价/趋势结构评分：决定总仓位上限
  ↓
板块主线评分：判断该板块是不是当前强主线
  ↓
个股板块内强度：判断个股是不是板块前排
  ↓
买点分型：breakout / pullback / volume_anchor_reaccumulation
  ↓
ATR 止损距离和仓位配置
```

### 5.1 板块主线评分

板块按股票池中同一 `sector` 的股票聚合，计算：

- 板块 20/60 日平均收益；
- 板块相对其他板块的强度排名；
- 板块内 20 日上涨股票比例；
- 板块内处于 60 日强势区的比例；
- 板块成交额相对 20 日均额是否温和放大。

输出字段包括：

```text
板块强度分
是否主线板块
是否强势板块
个股板块内20日排名
个股板块内60日排名
20日超额板块收益
60日超额板块收益
```

### 5.2 买点分型

v6 有三类买点：

#### A. breakout：平台/阶段突破

要求：

- 接近或突破 20/60/120 日高点；
- 处于 60 日区间强势区；
- 前期波动收敛；
- 成交额温和放大，默认 1.15–2.80 倍；
- 收盘较强，上影线不能过长；
- 板块强势、个股板块内靠前会加分。

#### B. pullback：主线回踩

要求：

- 板块是主线或强板块；
- 个股在板块内相对靠前；
- 强主线优先看 5/10 日线，普通趋势看 10/20 日线；
- 回踩过程缩量；
- 收盘重新站上关键短均线；
- 下影承接、止损距离可控会加分。

#### C. volume_anchor_reaccumulation：天量锚点后缩量再异动

识别：

- 近 3–20 日出现天量涨停/大阳线锚点；
- 锚点日成交额约为 20 日均额 3 倍以上，且属于近 120 日高量级别；
- 锚点后没有有效跌破锚点低点；
- 调整期间逐步缩量；
- 今日重新放量，站回 5/10 日线，并突破锚点后整理区间高点。

这类模型用于跟踪“前期天量资金事件”，但不会在天量当日盲目追入。

## 6. 运行

生成信号：

```bash
cd ~/project/a_share_signal_bot
python main.py --pool stock_pool.csv --config config.example.yml --out output --account 100000 --tail
```

OpenClaw/小龙虾对话入口：

```bash
printf '%s\n' '生成买入信号' | ./stockbot_chat.sh
printf '%s\n' '解释今天为什么没有信号' | ./stockbot_chat.sh
printf '%s\n' '为什么没买 600519' | ./stockbot_chat.sh
```

定时任务脚本：

```bash
ACCOUNT=100000 ./run_tail_prune.sh
```

### 热榜交易池

`stock_pool.csv` 作为核心池，不直接被热榜写回。收盘后缓存热榜，次日生成临时交易池：

```bash
# 收盘后缓存当天热榜
python hot_pool.py --mode cache --pool stock_pool.csv --top 100

# 次日盘前生成交易池；核心池优先，热榜补位，默认最多150只且只保留沪深主板
python hot_pool.py --mode build --pool stock_pool.csv --max-size 150

# 网络不稳定或只想扫描本地可用数据时，只补入已有历史K缓存的热榜股
python hot_pool.py --mode build --pool stock_pool.csv --max-size 150 --require-local-history

# 收盘后一条命令完成：缓存今天热榜，并生成下一工作日交易池
python hot_pool.py --mode all --pool stock_pool.csv --max-size 150
```

输出：

```text
cache/hot_pool/hot_rank_YYYYMMDD.csv  当天热榜缓存
output/latest_trading_pool.csv        次日扫描用交易池
output/latest_hot_pool_top100.csv     最新热榜Top100
```

交易池构建默认会优先选择已有本地历史 K 线缓存的热榜股；加 `--require-local-history`
会严格排除无缓存热榜股，适合网络不可用时避免扫描阶段大量数据失败。扫描阶段遇到
DNS/超时类网络错误时，会快速回退本地旧缓存；自动板块映射也会使用旧缓存兜底，避免
冷缓存时先全市场反查板块。

### ETF 独立策略

ETF 策略和个股策略是两条独立链路：使用单独的 `etf_pool.csv`、`cache/etf` 和
`etf_output`，不会读取或写入 `stock_pool.csv`、`output/latest_signals.csv` 等个股文件。

```bash
# 先从样例复制一个ETF池，再按需要增删
cp etf_pool_sample.csv etf_pool.csv

# 生成ETF交易信号
python etf_strategy.py --pool etf_pool.csv --config config.example.yml --out etf_output --account 100000
```

ETF 策略模型为：趋势动量 + 平台突破/缩量回踩 + ATR 止损 + 风险平价仓位。ETF 日线默认
按 `eastmoney -> sina` 顺序获取，单个数据源失败时会自动尝试下一个数据源。输出：

```text
etf_output/latest_etf_message.txt         精简摘要
etf_output/latest_etf_signals.csv         ETF买入配置
etf_output/latest_etf_candidates.csv      ETF候选评分
etf_output/latest_etf_report.md           ETF策略报告
cache/etf/etf_CODE_adjust_START_END.csv   ETF历史K缓存
```

ETF 轮动配置和回测使用独立入口：

```bash
# 生成当期ETF轮动组合，带类别上限、相关性过滤和市场强弱仓位
python etf_rotation.py --mode rotate --pool etf_pool.csv --config config.example.yml --out etf_output --account 100000

# 回测最近3年，每周五再平衡
python etf_rotation.py --mode backtest --pool etf_pool.csv --config config.example.yml --out etf_output --account 200000 --years 3 --rebalance W-FRI
```

轮动策略会按全ETF池横截面排序，而不是只判断单只ETF是否出现买点。主要因子包括：
20/60/120日动量、均线趋势、区间位置、波动率、回撤、流动性。组合层面会限制同类ETF数量，
并跳过与已选ETF高度相关的重复暴露；市场弱时降低总权益仓位，并给债券/黄金等防守资产额外权重。

## 7. 输出文件

```text
output/latest_message.txt           精简摘要
output/latest_signals.csv           买入信号
output/latest_candidates.csv        候选评分
output/latest_explanations.csv      中文字段逐股解释
output/latest_explanations_raw.csv  英文字段，供小龙虾按需解释读取
output/latest_report.md             完整报告
```

默认不会在聊天里刷出全部逐股原因；你需要时再问“解释/为什么没买”。

## 8. 关键参数

```yaml
strategy:
  score_threshold: 70
  sector:
    mainline_score_threshold: 68
    strong_score_threshold: 58
  setup:
    breakout_score_threshold: 70
    pullback_score_threshold: 68
    anchor_score_threshold: 72
    pullback_ma_options_strong: [5, 10]
    pullback_ma_options_normal: [10, 20]
```

更保守可把 `score_threshold` 提高到 75，或者把 `risk_gate.limit_down_count_6_block` 从 2 改成 1。


## v6.1 自动板块识别

默认配置：

```yaml
strategy:
  sector:
    auto_fill: true
    auto_source: "industry"
    auto_write_back: true
    auto_map_path: "cache/auto_sector_map.csv"
```

- `auto_source: "industry"`：默认，使用东方财富行业板块，首次构建较快，适合稳定分组。
- `auto_source: "concept"`：使用东方财富概念板块，更贴近题材主线，但概念数量多，首次构建更慢；同一股票属于多个概念时，脚本会选择当前板块强度分最高的概念作为主板块。
- `auto_source: "none"`：关闭自动抓取，只用股票池或 `sector_map.csv`。

如果你想让策略更偏题材主线，可以把 `config.example.yml` 里的：

```yaml
auto_source: "industry"
```

改成：

```yaml
auto_source: "concept"
```

第一次运行会慢一些，因为需要缓存板块成份股；后续会读缓存。

## v6.2 近两年模拟回测

新增 `backtest.py`，用于把当前 v6 主线-买点分型逻辑做历史模拟。默认假设：

- 每个交易日收盘后计算信号；
- 下一交易日开盘买入，避免未来函数；
- 初始资金默认 200000；
- 只做多、不融资、不自动下单；
- 买入后使用初始止损、1.5R/3R 分批止盈、MA20/MA60 趋势退出、大盘弱势退出和最长持仓天数控制。

运行近两年回测：

```bash
cd ~/project/a_share_signal_bot
source .venv/bin/activate
python backtest.py \
  --pool stock_pool.csv \
  --config config.example.yml \
  --out backtest_output \
  --account 200000 \
  --years 2
```

指定区间：

```bash
python backtest.py \
  --pool stock_pool.csv \
  --config config.example.yml \
  --out backtest_output \
  --account 200000 \
  --start 20240601 \
  --end 20260609
```

输出文件：

```text
backtest_output/latest_backtest_report.md          回测报告
backtest_output/latest_backtest_message.txt        精简摘要，适合小龙虾回复
backtest_output/latest_backtest_equity.csv         每日权益曲线
backtest_output/latest_backtest_trades.csv         交易明细
backtest_output/latest_backtest_signals.csv        历史信号记录
backtest_output/latest_backtest_open_positions.csv 期末持仓
backtest_output/latest_backtest_summary.csv        核心指标
```

小龙虾对话入口已支持：

```bash
printf '%s\n' '用20万模拟账户回测近两年' | ./stockbot_chat.sh
```

注意：这是日线级别模拟，无法完整还原涨跌停排队成交、停复牌、实际盘口冲击、不同券商费率和真实滑点。结果用于策略验证，不等于实盘收益承诺。

## v6.4：对话式持仓管理

v6.4 新增 `portfolio_manager.py`，小龙虾可以通过对话维护 `portfolio.csv`，包括查看、添加/修改、删除、清空、导入、设置总资金和可用现金。所有修改都会先备份到 `portfolio_backups/`。

### 支持命令示例

```text
查看持仓
添加持仓 600519 贵州茅台 100股 成本1500 总资金20万 可用现金5万
修改持仓 600519 200股 成本1450
删除持仓 600519
清空持仓
设置持仓总资金20万 可用现金3万
```

批量导入并覆盖：

```text
导入持仓 覆盖
股票代码,股票名称,股票股数,买入价格
600519,贵州茅台,100,1500
300750,宁德时代,200,210
```

批量追加/合并：

```text
导入持仓 追加
股票代码,股票名称,股票股数,买入价格
000858,五粮液,100,130
```

持仓交易建议仍然用：

```text
检查我的持仓是否需要交易
```

它只读取持仓并输出建议，不会修改 `portfolio.csv`，也不会自动下单。


## v6.5 重要更新

- 实时行情加入多源兜底：AkShare 东方财富、efinance、腾讯 quote 直连。
- 分钟K失败时不会再让持仓监控崩溃；会用每次定时调用保存的实时快照构造伪分时序列。
- 股票池超过 `prefilter_pool_when_gt` 时启用两阶段扫描，默认从 100+ 股票中优先扫描 45 只活跃候选，避免小龙虾进程被 kill；未扫描股票不会被自动删除。
- 买入信号新增日内确认：日K/板块通过后，还会看实时价在日内区间的位置、日内涨跌幅、从高点回落幅度和本地快照均线。
- 默认仓位更积极：强市场目标 95%，震荡市场目标 65%，最多 8 只，单票上限 22%。这不是收益承诺，建议继续回测验证。

如果仍希望全量扫描 100+ 股票，可在 `config.example.yml` 中设置：

```yaml
data:
  two_stage_scan: false
  max_scan_per_run: 0
```

但全量扫描更容易触发数据源限流或被系统杀进程。
