#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p output backtest_output position_output

ACCOUNT="${ACCOUNT:-200000}"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

cat > "$tmp"

source .venv/bin/activate
msg="$(cat "$tmp")"

if printf '%s' "$msg" | grep -Eq '回测|模拟交易|模拟账户|近两年|backtest|Backtest'; then
  if ! python backtest.py \
    --pool stock_pool.csv \
    --config config.example.yml \
    --out backtest_output \
    --account "$ACCOUNT" \
    --years 2 \
    > backtest_output/last_backtest_run.log 2>&1; then
    echo "回测执行失败："
    tail -120 backtest_output/last_backtest_run.log || true
    exit 1
  fi
  cat backtest_output/latest_backtest_message.txt
  exit 0
fi


# 持仓管理：查看/添加/修改/删除/清空/导入/设置资金，优先于盘中监控。
if printf '%s' "$msg" | grep -Eq '查看持仓|持仓列表|当前持仓|我的持仓|添加持仓|加入持仓|新增持仓|买入持仓|修改持仓|更新持仓|设置持仓|删除持仓|移除持仓|删掉持仓|清空持仓|重置持仓|清除持仓|导入持仓|覆盖持仓|追加持仓|设置.*(总资金|可用现金|可用资金|现金)|修改.*(总资金|可用现金|可用资金|现金)'; then
  PORTFOLIO="${PORTFOLIO:-portfolio.csv}"
  if ! python portfolio_manager.py --portfolio "$PORTFOLIO" --message-file "$tmp" > position_output/last_portfolio_manage_run.log 2>&1; then
    echo "持仓管理执行失败："
    tail -120 position_output/last_portfolio_manage_run.log || true
    exit 1
  fi
  cat position_output/last_portfolio_manage_run.log
  exit 0
fi


# 交易计划生命周期：从买入信号生成待买、同步持仓、输出后续止损/止盈/趋势退出/加仓建议。
if printf '%s' "$msg" | grep -Eq '交易计划|操作计划|后续怎么办|后续怎么|买入后|止损止盈|止损/止盈|卖出计划|明天.*(买|卖)|次日.*(买|卖)|同步交易|更新交易状态|从信号生成|持仓后续|后续操作'; then
  PORTFOLIO="${PORTFOLIO:-portfolio.csv}"
  if ! python trade_manager.py \
    --action auto \
    --message-file "$tmp" \
    --portfolio "$PORTFOLIO" \
    --state trade_state.csv \
    --signals-out output \
    --config config.example.yml \
    --out trade_output \
    --account "$ACCOUNT" \
    --sync \
    > trade_output/last_trade_manager_chat_run.log 2>&1; then
    echo "交易计划执行失败："
    tail -120 trade_output/last_trade_manager_chat_run.log || true
    exit 1
  fi
  cat trade_output/latest_trade_plan.txt
  exit 0
fi

# 持仓监控/交易建议：只读持仓，输出是否需要交易，不自动下单。
if printf '%s' "$msg" | grep -Eq '检查.*持仓|持仓.*检查|持仓.*交易建议|盘中|交易建议|止损|止盈|减仓|加仓|position|Position'; then
  PORTFOLIO="${PORTFOLIO:-portfolio.csv}"
  if ! python position_monitor.py     --portfolio "$PORTFOLIO"     --config config.example.yml     --out position_output     --account "$ACCOUNT"     > position_output/last_position_chat_run.log 2>&1; then
    echo "持仓监控执行失败："
    tail -120 position_output/last_position_chat_run.log || true
    exit 1
  fi
  cat position_output/latest_position_message.txt
  exit 0
fi

if ! python main.py \
  --pool stock_pool.csv \
  --config config.example.yml \
  --out output \
  --account "$ACCOUNT" \
  --tail \
  --chat-file "$tmp" \
  > output/last_chat_run.log 2>&1; then
  echo "股票池/信号命令执行失败："
  tail -80 output/last_chat_run.log || true
  exit 1
fi

cat output/latest_chat_reply.txt
