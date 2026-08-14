# BarGenerator 业务逻辑文档

## 1. 模块概述

`BarGenerator` 是 VeighNa 框架中的 **K线合成器**，负责将低粒度行情数据聚合为高粒度 K 线，供策略层使用。

### 核心能力

```
Tick 数据 ──→ 1分钟K线 ──→ X分钟K线 / X小时K线 / 日K线
```

### 两层合成架构

```
┌─────────────────────────────────────────────────────────┐
│  第一层：update_tick() → 合成 1 分钟 K 线 → on_bar()    │
│                                                         │
│  第二层：update_bar() → 合成窗口 K 线 → on_window_bar() │
│     ├── update_bar_minute_window()  (X 分钟)            │
│     ├── update_bar_hour_window()    (1 小时)            │
│     │     └── on_hour_bar()         (X 小时)            │
│     └── update_bar_daily_window()   (日 K)              │
└─────────────────────────────────────────────────────────┘
```

---

## 2. 构造参数

```python
BarGenerator(
    on_bar: Callable,           # 每根 1 分钟 K 线完成时的回调
    window: int = 0,            # 窗口周期数（如 5 表示 5 分钟 / 5 小时）
    on_window_bar: Callable,    # 窗口 K 线完成时的回调
    interval: Interval,         # 目标周期：MINUTE / HOUR / DAILY
    daily_end: time             # 日K收盘时间（仅 DAILY 模式必填）
)
```

### 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `on_bar` | `Callable` | 必填 | 1分钟K线完成时的回调函数 |
| `window` | `int` | `0` | 窗口周期数（0 表示不合成窗口K线） |
| `on_window_bar` | `Callable \| None` | `None` | 窗口K线完成时的回调函数 |
| `interval` | `Interval` | `MINUTE` | 目标K线周期类型 |
| `daily_end` | `time \| None` | `None` | 日K收盘时刻（合成日K线时必填） |

### 约束规则

- **X分钟K线**：`window` 必须能整除 60，合法值：2, 3, 5, 6, 10, 15, 20, 30
- **X小时K线**：`window` 可以为任意正整数
- **日K线**：必须传入 `daily_end`，否则抛出 `RuntimeError`

---

## 3. 内部状态

| 字段 | 类型 | 用途 |
|------|------|------|
| `bar` | `BarData \| None` | 正在合成的当前 1 分钟 K 线 |
| `window_bar` | `BarData \| None` | 正在合成的窗口 K 线（分钟/小时共用） |
| `hour_bar` | `BarData \| None` | 正在合成的 1 小时 K 线 |
| `daily_bar` | `BarData \| None` | 正在合成的日 K 线 |
| `last_tick` | `TickData \| None` | 上一笔 Tick（用于计算增量） |
| `interval_count` | `int` | 小时K线计数器（用于 X 小时合成） |

---

## 4. 第一层：Tick → 1 分钟 K 线

### `update_tick(tick)`

#### 流程图

```
收到新 Tick
│
├── last_price == 0？ → 过滤丢弃（无效行情）
│
├── self.bar 为空？（首 Tick 或新分钟开始）
│   └── 创建新 BarData
│       open = high = low = close = last_price
│       datetime = tick.datetime
│
├── 分钟切换？（当前 Tick 的小时或分钟 ≠ bar 的时间）
│   ├── 旧 bar 的 second/microsecond 清零
│   ├── 推送旧 bar → on_bar(self.bar)
│   └── 创建新 BarData
│
└── 同一分钟内？
    ├── high = max(bar.high, tick.last_price)
    │   └── 若 tick.high_price > last_tick.high_price → high = max(high, tick.high_price)
    ├── low  = min(bar.low, tick.last_price)
    │   └── 若 tick.low_price < last_tick.low_price  → low  = min(low, tick.low_price)
    ├── close = tick.last_price
    ├── open_interest = tick.open_interest
    ├── volume += max(tick.volume - last_tick.volume, 0)
    └── turnover += max(tick.turnover - last_tick.turnover, 0)
```

#### 关键设计细节

**为什么用 `last_price` 更新 high/low？**

Tick 数据中的 `tick.high_price` / `tick.low_price` 通常是**当日累计最高/最低价**，而非本分钟内的极值。因此：

```python
# 主要逻辑：用逐笔成交价跟踪本分钟内的高低点
self.bar.high_price = max(self.bar.high_price, tick.last_price)
self.bar.low_price  = min(self.bar.low_price, tick.last_price)
```

**为什么额外检查 `tick.high_price > last_tick.high_price`？**

当 Tick 推送间隔较大时（如行情稀疏），可能在两个 Tick 之间出现了更高/更低的价格。如果 `tick.high_price > last_tick.high_price`，说明本 Tick 带来了**新的日内新高**，此时用 `tick.high_price` 补充更新，避免遗漏。

**成交量为什么用增量累加？**

```python
volume_change = tick.volume - self.last_tick.volume
self.bar.volume += max(volume_change, 0)
```

交易所推送的 `tick.volume` 是**当日累计成交量**，而非本 Tick 的成交量。因此需要计算两次 Tick 的差值作为增量。`max(..., 0)` 防止因交易所数据异常导致负增量。

---

## 5. 第二层：1 分钟 K 线 → 窗口 K 线

### `update_bar(bar)` — 路由分发

```python
def update_bar(self, bar: BarData) -> None:
    if self.interval == Interval.MINUTE:
        self.update_bar_minute_window(bar)    # X 分钟 K 线
    elif self.interval == Interval.HOUR:
        self.update_bar_hour_window(bar)      # X 小时 K 线（经 hour_bar 中转）
    else:
        self.update_bar_daily_window(bar)     # 日 K 线
```

---

### 5.1 `update_bar_minute_window(bar)` — X 分钟 K 线

#### 流程图

```
收到 1 分钟 bar
│
├── window_bar 为空？ → 创建新 window_bar（second/microsecond 清零）
│
├── 累加更新 window_bar：
│   ├── high = max(window_bar.high, bar.high)
│   ├── low  = min(window_bar.low, bar.low)
│   ├── close = bar.close
│   ├── volume += bar.volume
│   ├── turnover += bar.turnover
│   └── open_interest = bar.open_interest（直接覆盖，取最新值）
│
└── 判断是否完成：(bar.minute + 1) % window == 0？
    ├── YES → 推送 on_window_bar(window_bar)
    │         清空 window_bar = None
    └── NO  → 继续累积
```

#### 完成判断原理

分钟从 0 开始计数，`minute + 1` 代表"第几分钟"：

```
window = 5 时：
  09:00 → (0+1)%5 = 1 → 继续
  09:01 → (1+1)%5 = 2 → 继续
  09:02 → (2+1)%5 = 3 → 继续
  09:03 → (3+1)%5 = 4 → 继续
  09:04 → (4+1)%5 = 0 → ✅ 完成，推送 5 分钟 K 线
  09:05 → (5+1)%5 = 1 → 继续（新的 5 分钟周期）
  ...
```

---

### 5.2 `update_bar_hour_window(bar)` — 1 小时 K 线

#### 流程图

```
收到 1 分钟 bar
│
├── hour_bar 为空？ → 创建新 hour_bar（minute/second/microsecond 清零）
│                     return（不推送，等待后续 bar）
│
├── bar.minute == 59？（当前小时的最后一分钟）
│   ├── 累加到 hour_bar（high/low/close/volume/turnover/open_interest）
│   ├── finished_bar = hour_bar
│   └── hour_bar = None
│
├── bar.hour ≠ hour_bar.hour？（跨小时，上一小时未正常结束）
│   ├── finished_bar = hour_bar（先保存旧的小时 bar）
│   └── 创建新 hour_bar（用当前 bar 数据初始化）
│
└── 同小时内？
    └── 累加更新 hour_bar
│
└── 有 finished_bar？ → on_hour_bar(finished_bar)
```

#### 两种结束触发方式

| 触发条件 | 场景 |
|---------|------|
| `minute == 59` | 正常结束：收到当前小时最后一根 1 分钟 bar |
| `hour != hour_bar.hour` | 异常结束：行情跳过了 59 分钟（如午休后、集合竞价） |

---

### 5.3 `on_hour_bar(bar)` — X 小时 K 线

#### 流程图

```
收到 1 小时 bar
│
├── window == 1？ → 直接推送 on_window_bar(bar)
│
└── window > 1？
    ├── window_bar 为空？ → 创建新 window_bar
    ├── 累加更新 window_bar（high/low/close/volume/turnover/open_interest）
    ├── interval_count += 1
    └── interval_count % window == 0？
        ├── YES → 推送 on_window_bar(window_bar)
        │         interval_count = 0
        │         window_bar = None
        └── NO  → 继续累积
```

#### 示例：window = 2（2 小时 K 线）

```
第 1 小时 bar → 创建 window_bar，count = 1，1%2 ≠ 0 → 继续
第 2 小时 bar → 累加 window_bar，count = 2，2%2 = 0 → ✅ 推送 2 小时 K 线
第 3 小时 bar → 创建新 window_bar，count = 1 → 继续
第 4 小时 bar → 累加 window_bar，count = 2 → ✅ 推送 2 小时 K 线
```

---

### 5.4 `update_bar_daily_window(bar)` — 日 K 线

#### 流程图

```
收到 1 分钟 bar
│
├── daily_bar 为空？ → 创建新 daily_bar
│
├── 累加更新 daily_bar：
│   ├── high = max(daily_bar.high, bar.high)
│   ├── low  = min(daily_bar.low, bar.low)
│   ├── close = bar.close
│   ├── volume += bar.volume
│   ├── turnover += bar.turnover
│   └── open_interest = bar.open_interest
│
└── bar.datetime.time() == daily_end？（到达收盘时间）
    ├── YES → daily_bar.datetime 清零到当天 00:00:00
    │         推送 on_window_bar(daily_bar)
    │         daily_bar = None
    └── NO  → 继续累积
```

#### daily_end 的作用

不同市场的收盘时间不同，需要明确指定：

| 市场 | daily_end 示例 |
|------|---------------|
| A 股 | `time(15, 0)` |
| 期货日盘 | `time(15, 0)` |
| 期货夜盘 | `time(23, 0)` 或 `time(1, 0)` |

> ⚠️ 构造时若 `interval=DAILY` 且未传 `daily_end`，会直接抛出 `RuntimeError`。

---

## 6. 辅助方法

### `generate()` — 强制推送当前 1 分钟 K 线

```python
def generate(self) -> BarData | None:
    bar = self.bar
    if bar:
        bar.datetime = bar.datetime.replace(second=0, microsecond=0)
        self.on_bar(bar)
    self.bar = None
    return bar
```

**使用场景**：在分钟未结束时强制推送当前正在合成的 K 线，例如：

- 收盘前最后一根 K 线（不会等到下一分钟才开始推送）
- 暂停行情/断开连接时需要保存已有数据
- 回测结束时推送最后一根未完成的 K 线

---

## 7. 完整数据流示例

### 场景：window=5, interval=MINUTE（合成 5 分钟 K 线）

```
Tick 09:00:00.100 → 创建 bar（1分钟）
Tick 09:00:30.200 → 更新 bar 的 high/low/close/volume
Tick 09:01:00.050 → 分钟切换！
  ├── 推送 bar(09:00) → on_bar()
  ├── update_bar_minute_window(bar) → 创建 window_bar，累积
  └── 创建新 bar
Tick 09:01:30.200 → 更新 bar
Tick 09:02:00.050 → 推送 bar(09:01) → on_bar() → 累积到 window_bar
Tick 09:03:00.050 → 推送 bar(09:02) → on_bar() → 累积到 window_bar
Tick 09:04:00.050 → 推送 bar(09:03) → on_bar() → 累积到 window_bar
Tick 09:05:00.050 → 推送 bar(09:04) → on_bar() → 累积到 window_bar
  └── (4+1)%5 == 0 → ✅ on_window_bar(5分钟K线: 09:00~09:04)
```

### 场景：window=2, interval=HOUR（合成 2 小时 K 线）

```
1分钟 bar 09:00 → hour_bar 为空 → 创建 hour_bar(09:00)，return
1分钟 bar 09:01 → 累加到 hour_bar
...
1分钟 bar 09:59 → minute==59 → 累加 → finished_bar → on_hour_bar()
  └── window_bar 为空 → 创建 window_bar，count=1 → 继续
1分钟 bar 10:00 → hour_bar 为空 → 创建 hour_bar(10:00)，return
...
1分钟 bar 10:59 → finished_bar → on_hour_bar()
  └── 累加 window_bar，count=2，2%2==0 → ✅ on_window_bar(2小时K线)
```

---

## 8. 方法汇总表

| 方法 | 输入 | 输出 | 触发条件 |
|------|------|------|---------|
| `update_tick` | TickData | 1 分钟 BarData → `on_bar` | 分钟切换时 |
| `update_bar` | 1 分钟 BarData | 路由分发 | 由外部调用 |
| `update_bar_minute_window` | 1 分钟 bar | X 分钟 BarData → `on_window_bar` | `(minute+1) % window == 0` |
| `update_bar_hour_window` | 1 分钟 bar | 1 小时 BarData → `on_hour_bar` | `minute==59` 或跨小时 |
| `on_hour_bar` | 1 小时 bar | X 小时 BarData → `on_window_bar` | `count % window == 0` |
| `update_bar_daily_window` | 1 分钟 bar | 日 BarData → `on_window_bar` | `time == daily_end` |
| `generate` | 无 | 当前 1 分钟 bar → `on_bar` | 手动调用（强制推送） |

---

## 9. 关键设计决策

| 设计 | 原因 |
|------|------|
| **用 `last_price` 跟踪 high/low** | Tick 的 high/low 是当日累计值，不适合用于分钟K线 |
| **成交量用增量累加** | 交易所推送的是当日累计成交量，需计算差值 |
| **`max(volume_change, 0)` 保护** | 防止交易所数据异常（如换合约）导致负增量 |
| **`minute + 1` 判断分钟K线完成** | 分钟从 0 开始计数，`+1` 后取模更直观 |
| **hour_bar 独立中转** | 小时K线需要先合成完整的 1 小时 bar，再累加到 X 小时窗口 |
| **daily_end 可配置** | 不同市场收盘时间不同，夜盘/日盘/外盘各有差异 |
| **`generate` 强制推送** | 解决收盘时最后一根K线无法自然完成的问题 |
