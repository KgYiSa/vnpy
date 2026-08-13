# converter.py 业务逻辑文档

## 1. 模块概述

`converter.py` 是 VeighNa 框架中**期货开平仓转换**的核心模块，负责解决以下问题：

- **交易所差异**：不同交易所对"平仓"的定义不同（如上期所必须区分平今/平昨，其他交易所支持通用平仓）
- **持仓模式差异**：多空分仓 vs 净仓模式，框架需要在本地正确追踪持仓状态
- **策略需求差异**：部分策略需要"锁仓"或"净仓"模式，而非直接平仓

### 模块架构

```
OffsetConverter（调度层 - 对外接口）
    │
    ├── holdings: dict[str, PositionHolding]
    │
    └── PositionHolding（持仓层 - 单合约状态管理）
            ├── 持仓数据：long/short × pos/td/yd
            ├── 冻结数据：long/short × pos/td/yd _frozen
            ├── 活跃订单：active_orders
            └── 转换方法：shfe / lock / net
```

---

## 2. PositionHolding 类

**职责**：管理单个合约的本地持仓状态，并提供平仓请求转换能力。

### 2.1 数据结构

#### 持仓量（6个字段）

| | 总持仓 | 今日持仓 | 昨日持仓 |
|------|--------|---------|---------|
| **多头 (long)** | `long_pos` | `long_td` | `long_yd` |
| **空头 (short)** | `short_pos` | `short_td` | `short_yd` |

> 约束：`pos = td + yd`（总持仓 = 今日 + 昨日）

#### 冻结量（6个字段）

| | 总冻结 | 今日冻结 | 昨日冻结 |
|------|--------|---------|---------|
| **多头 (long)** | `long_pos_frozen` | `long_td_frozen` | `long_yd_frozen` |
| **空头 (short)** | `short_pos_frozen` | `short_td_frozen` | `short_yd_frozen` |

> 约束：`pos_frozen ≤ pos`，`td_frozen ≤ td`，`yd_frozen ≤ yd`

#### 活跃订单

```python
active_orders: dict[str, OrderData] = {}   # vt_orderid → OrderData
```

仅保存**尚未完成**的挂单，已成交/已撤单的会被移除。

---

### 2.2 数据更新方法

#### `update_position(position)` — 同步交易所持仓

```
输入：PositionData（来自交易所/网关的持仓推送）
行为：直接覆盖本地持仓数据
计算：td = pos - yd（今日仓 = 总仓 - 昨日仓）
```

#### `update_order(order)` — 同步订单状态

```
输入：OrderData（来自交易所/网关的订单推送）
行为：
  - 订单活跃 → 加入 active_orders
  - 订单结束 → 从 active_orders 移除
  - 触发 calculate_frozen() 重算冻结量
```

#### `update_order_request(req, vt_orderid)` — 本地发出订单后立即记录

```
输入：OrderRequest + vt_orderid（本地发出下单请求后立即调用）
行为：将 req 转为 OrderData 后调用 update_order
目的：在交易所返回确认前，提前冻结对应仓位
```

#### `update_trade(trade)` — 同步成交信息

```
输入：TradeData（来自交易所/网关的成交推送）
行为：根据方向和开平类型，增减对应的 td/yd 字段
特殊处理：
  - CLOSE 类型在 SHFE/INE：优先减 yd（交易所规则）
  - CLOSE 类型在其他交易所：优先减 td，不够溢出到 yd
  - 执行后重算总持仓并调用 sum_pos_frozen() 校验
```

**update_trade 分支逻辑表：**

| 方向 | Offset | SHFE/INE | 其他交易所 |
|------|--------|----------|-----------|
| LONG | OPEN | `long_td += vol` | `long_td += vol` |
| LONG | CLOSETODAY | `short_td -= vol` | `short_td -= vol` |
| LONG | CLOSEYESTERDAY | `short_yd -= vol` | `short_yd -= vol` |
| LONG | CLOSE | `short_yd -= vol` | 先 `short_td -= vol`，溢出到 `short_yd` |
| SHORT | OPEN | `short_td += vol` | `short_td += vol` |
| SHORT | CLOSETODAY | `long_td -= vol` | `long_td -= vol` |
| SHORT | CLOSEYESTERDAY | `long_yd -= vol` | `long_yd -= vol` |
| SHORT | CLOSE | `long_yd -= vol` | 先 `long_td -= vol`，溢出到 `long_yd` |

---

### 2.3 冻结量计算方法

#### `calculate_frozen()` — 根据活跃挂单全量重算

```
流程：
1. 所有冻结量清零
2. 遍历 active_orders 中的每个挂单：
   - OPEN 开仓单 → 跳过（不冻结持仓）
   - 计算 frozen = volume - traded（剩余未成交量）
   - 根据方向和平仓类型累加到对应的冻结字段
   - CLOSE 通用平仓：先冻结今仓，溢出到昨仓
3. 调用 sum_pos_frozen() 做兜底校验
```

#### `sum_pos_frozen()` — 兜底校验

```
流程：
1. 每个冻结字段 = min(冻结量, 实际持仓量)   ← 防止超额冻结
2. 汇总：pos_frozen = td_frozen + yd_frozen
```

**调用时机：**

| 方法 | 调用场景 |
|------|---------|
| `calculate_frozen()` | 订单状态变化时（`update_order` 末尾） |
| `sum_pos_frozen()` | `calculate_frozen` 末尾、`update_trade` 末尾 |

---

### 2.4 平仓请求转换方法

#### `convert_order_request_shfe(req)` — SHFE/INE 拆单

**适用场景**：上期所、能源所不支持通用 `CLOSE`，必须明确平今/平昨。

```
决策流程：
1. OPEN 开仓 → 直接放行 [req]
2. 计算可用量：
   - pos_available = 对手方总仓 - 总冻结
   - td_available  = 对手方今仓 - 今冻结
3. req.volume > pos_available → 拒绝 []（仓位不足）
4. req.volume ≤ td_available  → 全平今 [CLOSETODAY]
5. 其他 → 拆分 [CLOSETODAY(td可用部分), CLOSEYESTERDAY(剩余部分)]
```

**示例：**

| 持仓状态 | 请求 | 结果 |
|---------|------|------|
| short_td=4, short_yd=6, 无冻结 | 买平 3 手 | `[CLOSETODAY 3手]` |
| short_td=4, short_yd=6, 无冻结 | 买平 7 手 | `[CLOSETODAY 4手, CLOSEYESTERDAY 3手]` |
| short_td=4, short_yd=6, 无冻结 | 买平 11 手 | `[]`（拒绝） |

#### `convert_order_request_lock(req)` — 锁仓模式

**适用场景**：策略希望保留原仓位，通过反向开仓对冲风险。

```
决策流程：
1. 对手方有今仓 且 非 SHFE/INE → 全部反向开仓 [OPEN]
2. 其他情况：
   - close_volume = min(请求量, 昨仓可用量)
   - open_volume  = max(0, 请求量 - close_volume)
   - 有昨仓 → [CLOSE/CLOSEYESTERDAY(close_volume)]
   - 有剩余 → [OPEN(open_volume)]
```

> ⚠️ 已知问题：缺少对 `req.offset == OPEN` 的前置检查，传入开仓请求时可能产生逻辑错误。

**示例：**

| 持仓状态 | 请求 | 结果 |
|---------|------|------|
| long_td=3, long_yd=0, 非SHFE | 卖平 3 手 | `[OPEN 卖开3手]`（锁仓） |
| long_td=0, long_yd=6, 非SHFE | 卖平 4 手 | `[CLOSE 4手]` |
| long_td=0, long_yd=2, 非SHFE | 卖平 5 手 | `[CLOSE 2手, OPEN 卖开3手]` |

#### `convert_order_request_net(req)` — 净仓模式

**适用场景**：策略希望优先对冲抵消对手方仓位，不够再反向开仓。

```
SHFE/INE 分支：
  ① 有今仓可用 → [CLOSETODAY(min(td_available, volume))]
  ② 有昨仓可用 → [CLOSEYESTERDAY(min(yd_available, 剩余))]
  ③ 仍有剩余  → [OPEN(剩余量)]

其他交易所分支：
  ① 有总仓可用 → [CLOSE(min(pos_available, volume))]
  ② 仍有剩余  → [OPEN(剩余量)]
```

**示例：**

| 持仓状态 | 请求 | 交易所 | 结果 |
|---------|------|-------|------|
| short_td=3, short_yd=4 | 买平 10 手 | SHFE | `[CLOSETODAY 3手, CLOSEYESTERDAY 4手, OPEN 3手]` |
| long_pos=5, 无冻结 | 卖 8 手 | 其他 | `[CLOSE 5手, OPEN 卖开3手]` |

#### 三种转换模式对比

| 对比项 | SHFE 拆单 | 锁仓 (lock) | 净仓 (net) |
|--------|----------|------------|-----------|
| 触发条件 | SHFE/INE 自动触发 | 策略启用 `lock=True` | 策略启用 `net=True` |
| 核心思路 | CLOSE → 拆分为平今+平昨 | 优先反向开仓保留原仓 | 优先平仓抵消再开新仓 |
| 开仓单处理 | 直接放行 | 无前置检查（缺陷） | 无前置检查（缺陷） |
| 结果 | 持仓减少 | 多空并存（净仓不变） | 持仓净减少 |
| 保证金 | 正常 | 较低（对冲优惠） | 正常 |

---

## 3. OffsetConverter 类

**职责**：作为上层门面，管理所有合约的 `PositionHolding`，并根据合约属性和策略参数选择合适的转换策略。

### 3.1 持仓管理

#### `get_position_holding(vt_symbol)` — 懒加载

```
流程：
1. 从 holdings 字典查找
2. 找不到 → 从 OMS 查 ContractData
3. 合约存在 → 创建新的 PositionHolding 并缓存
4. 合约不存在 → 返回 None
```

> 合约数量庞大（数千），按需创建节省内存。

#### `is_convert_required(vt_symbol)` — 转换必要性判断

```
需要转换：合约存在 且 net_position = False（多空分仓模式）
不需要转换：合约不存在 或 net_position = True（净仓模式由交易所处理）
```

### 3.2 数据更新方法

四个 `update_xxx` 方法结构一致：

```
1. is_convert_required → 不需要则跳过
2. get_position_holding → 获取对应的 PositionHolding
3. 委托给 PositionHolding 执行具体更新
```

| 方法 | 数据来源 | 更新内容 |
|------|---------|---------|
| `update_position` | 交易所持仓推送 | 覆盖本地持仓 |
| `update_trade` | 交易所成交推送 | 增减 td/yd |
| `update_order` | 交易所订单状态推送 | 更新活跃订单 + 重算冻结 |
| `update_order_request` | 本地发出下单请求 | 提前记录 + 冻结 |

### 3.3 核心转换方法

#### `convert_order_request(req, lock, net)` — 策略选择

```
决策流程：
1. is_convert_required = False → 直接放行 [req]
2. holding 不存在 → 直接放行 [req]
3. lock = True → convert_order_request_lock（锁仓模式）
4. net  = True → convert_order_request_net（净仓模式）
5. SHFE / INE  → convert_order_request_shfe（拆单模式）
6. 其他         → 直接放行 [req]
```

---

## 4. 数据流全景图

```
                    交易所 / 网关
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
    PositionData   TradeData     OrderData
          │             │             │
          ▼             ▼             ▼
    OffsetConverter.update_position   │
    OffsetConverter.update_trade      │
    OffsetConverter.update_order ◄────┘
          │
          ▼
    PositionHolding（本地持仓状态）
    ├── long_td, long_yd, short_td, short_yd
    ├── xxx_frozen（冻结量）
    └── active_orders（活跃挂单）
          │
          ▼
    convert_order_request(req, lock, net)
          │
          ├── SHFE拆单 / 锁仓 / 净仓 / 直接放行
          │
          ▼
    list[OrderRequest] → Gateway → 交易所
```

---

## 5. 关键设计决策

| 设计 | 原因 |
|------|------|
| **懒加载 PositionHolding** | 合约数量庞大（数千），按需创建节省内存 |
| **全量重算冻结量** | `calculate_frozen` 每次清零后重新遍历，避免增量计算的累积误差 |
| **`sum_pos_frozen` 兜底** | 防止挂单超额导致可用量计算为负 |
| **SHFE/INE 特殊处理** | 这两个交易所协议层不支持通用 CLOSE，必须在框架层拆分 |
| **`update_trade` 溢出逻辑** | 其他交易所 CLOSE 先平今再平昨，当今日不够时自动溢出到昨日 |
