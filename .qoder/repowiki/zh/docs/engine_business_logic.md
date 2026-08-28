# engine.py 代码逻辑与业务逻辑详解

## 一、文件概览

`engine.py` 是 VeighNa 交易平台的**引擎层核心文件**，定义了平台运行所需的所有引擎类。它采用**事件驱动架构**，通过 `MainEngine` 统一协调各功能引擎，实现日志记录、订单管理、邮件通知、微信推送等核心能力。

### 类继承关系

```
BaseEngine (ABC)
├── LogEngine      — 日志引擎
├── OmsEngine      — 订单管理系统引擎
├── EmailEngine    — 邮件发送引擎
└── WechatEngine   — 微信推送引擎

MainEngine         — 主引擎（非 BaseEngine 子类，是平台核心调度器）
```

---

## 二、BaseEngine — 引擎抽象基类

**位置**: 第 59–78 行

### 职责
作为所有功能引擎的**抽象基类**，规定引擎的统一接口契约。

### 核心设计

| 成员 | 说明 |
|------|------|
| `main_engine` | 持有主引擎引用，用于跨引擎调度 |
| `event_engine` | 持有事件引擎引用，用于事件注册与分发 |
| `engine_name` | 引擎唯一标识名，用于在主引擎中注册和查找 |
| `close()` | 引擎关闭钩子，子类可覆盖以释放资源 |

### 业务意义
所有功能引擎必须持有 `MainEngine` 和 `EventEngine` 的引用，这保证了：
- 引擎间可以通过 `MainEngine` 互相访问
- 所有引擎共享同一个事件总线（EventEngine）

---

## 三、MainEngine — 主引擎（平台核心）

**位置**: 第 81–323 行

### 职责
作为交易平台的**核心调度中心**，负责统一管理网关（Gateway）、功能引擎（Engine）和应用（App），并对外提供统一的交易操作接口。

### 3.1 初始化流程

```
__init__
  ├── 创建/接收 EventEngine 并启动
  ├── 初始化容器: gateways, engines, apps, exchanges
  ├── 切换工作目录到 TRADER_DIR
  └── 调用 init_engines() 初始化内置引擎
```

**关键设计**: 构造时自动调用 `init_engines()`，这意味着 `MainEngine` 一经创建，日志引擎、OMS引擎、邮件引擎、微信引擎即自动就绪。

### 3.2 组件注册

| 方法 | 功能 | 返回值 |
|------|------|--------|
| `add_engine(engine_class)` | 实例化并注册功能引擎 | 引擎实例 |
| `add_gateway(gateway_class, gateway_name)` | 实例化并注册交易网关，自动收集其支持的交易所 | 网关实例 |
| `add_app(app_class)` | 注册应用，同时注册其关联的引擎 | 引擎实例 |

**`add_gateway` 的业务细节**:
- 若未指定 `gateway_name`，自动使用 `gateway_class.default_name`
- 网关支持的交易所会自动合并到 `self.exchanges` 列表（去重）

**`add_app` 的业务细节**:
- App 是引擎的"包装"，一个 App 对应一个 Engine
- 通过 `app.engine_class` 获取实际引擎类并注册

### 3.3 内置引擎初始化 (`init_engines`)

按顺序注册以下引擎，并将 OMS 引擎的查询方法**代理到 MainEngine 自身**：

```
1. LogEngine   → 日志记录
2. OmsEngine   → 订单管理（核心）
   └── 将 get_tick, get_order, get_trade, get_position, get_account,
       get_contract, get_quote 及其 get_all_* 系列方法代理到 MainEngine
   └── 将 update_order_request, convert_order_request, get_converter 代理
3. EmailEngine → 邮件发送
4. WechatEngine → 微信推送
```

**设计意图**: 外部调用者无需知道 OMS 引擎的存在，直接通过 `main_engine.get_tick()` 即可查询数据，实现了**外观模式（Facade）**。

### 3.4 交易操作接口

| 方法 | 业务含义 | 路由目标 |
|------|----------|----------|
| `connect(setting, gateway_name)` | 连接交易网关（登录） | Gateway.connect |
| `subscribe(req, gateway_name)` | 订阅行情数据 | Gateway.subscribe |
| `send_order(req, gateway_name)` | 发送委托订单 | Gateway.send_order |
| `cancel_order(req, gateway_name)` | 撤销委托订单 | Gateway.cancel_order |
| `send_quote(req, gateway_name)` | 发送做市报价 | Gateway.send_quote |
| `cancel_quote(req, gateway_name)` | 撤销做市报价 | Gateway.cancel_quote |
| `query_history(req, gateway_name)` | 查询历史K线 | Gateway.query_history |

**统一模式**: 每个操作都先通过 `gateway_name` 查找网关，找到后写日志再执行，找不到则安全返回。

### 3.5 通知推送

`send_notification(content, subject)` 方法同时通过**邮件和微信两个渠道**推送通知：

```
send_notification
  ├── EmailEngine.send_email(subject, content)
  └── WechatEngine.send_wechat(f"{subject}\n{content}")
```

若未指定 `subject`，默认使用当前时间戳。

### 3.6 关闭流程

```
close()
  ├── 1. 停止 EventEngine（阻止新的定时器事件）
  ├── 2. 关闭所有 Engine
  └── 3. 关闭所有 Gateway
```

**顺序很重要**: 必须先停事件引擎，防止关闭过程中产生新事件。

---

## 四、LogEngine — 日志引擎

**位置**: 第 325–357 行

### 职责
监听 `EVENT_LOG` 事件，将日志数据通过 `loguru` 的 `logger` 输出。

### 处理流程

```
EVENT_LOG 事件触发
  └── process_log_event
       ├── 检查 active 开关
       ├── 从 LogData 提取 level 和 msg
       ├── 将 level 数字映射为字符串（DEBUG/INFO/WARNING/ERROR/CRITICAL）
       └── 通过 logger.bind(gateway_name=...).log(level, msg) 输出
```

### 业务特点
- **可通过配置开关**: `SETTINGS["log.active"]` 控制是否记录日志
- **支持来源标记**: 通过 `gateway_name` 绑定日志来源，便于过滤
- 只在初始化时注册 `EVENT_LOG` 一种事件

---

## 五、OmsEngine — 订单管理系统引擎

**位置**: 第 360–587 行

### 职责
作为平台的**核心数据中枢**，实时维护所有交易数据（行情、委托、成交、持仓、账户、合约、报价），并提供查询接口。同时集成 `OffsetConverter` 处理开平转换逻辑。

### 5.1 数据容器

| 容器 | 键 | 值类型 | 说明 |
|------|----|--------|------|
| `ticks` | `vt_symbol` | `TickData` | 最新快照行情（按合约） |
| `orders` | `vt_orderid` | `OrderData` | 全部委托记录 |
| `trades` | `vt_tradeid` | `TradeData` | 全部成交记录 |
| `positions` | `vt_positionid` | `PositionData` | 持仓数据 |
| `accounts` | `vt_accountid` | `AccountData` | 账户资金 |
| `contracts` | `vt_symbol` | `ContractData` | 合约信息 |
| `quotes` | `vt_quoteid` | `QuoteData` | 做市报价 |
| `active_orders` | `vt_orderid` | `OrderData` | 活跃委托（未成交/未撤销） |
| `active_quotes` | `vt_quoteid` | `QuoteData` | 活跃报价 |
| `offset_converters` | `gateway_name` | `OffsetConverter` | 开平转换器（按网关） |

### 5.2 事件处理逻辑

#### 行情事件 (`process_tick_event`)
直接用最新 tick 覆盖旧数据，保证 `ticks` 字典中始终是**最新快照**。

#### 委托事件 (`process_order_event`)
```
收到 OrderData
  ├── 更新 orders 字典
  ├── 判断委托是否活跃
  │    ├── 活跃 → 加入 active_orders
  │    └── 非活跃 → 从 active_orders 移除
  └── 通知 OffsetConverter 更新委托状态
```

#### 成交事件 (`process_trade_event`)
```
收到 TradeData
  ├── 记录到 trades 字典
  └── 通知 OffsetConverter 更新成交数据
```

#### 持仓事件 (`process_position_event`)
```
收到 PositionData
  ├── 更新 positions 字典
  └── 通知 OffsetConverter 更新持仓
```

#### 合约事件 (`process_contract_event`)
```
收到 ContractData
  ├── 记录到 contracts 字典
  └── 若该网关尚未创建 OffsetConverter，则创建一个
```

**关键**: `OffsetConverter` 是**延迟初始化**的，在收到第一个合约数据时才创建，确保合约信息已就绪。

#### 报价事件 (`process_quote_event`)
逻辑与委托事件类似，维护活跃报价字典。

### 5.3 OffsetConverter 集成

`OffsetConverter` 负责在"净仓模式"和"锁仓模式"之间转换委托请求：

| 方法 | 说明 |
|------|------|
| `update_order_request` | 记录委托请求与 vt_orderid 的映射 |
| `convert_order_request(req, gateway_name, lock, net)` | 根据锁仓/净仓模式，将原始委托拆分为多个子委托 |
| `get_converter` | 获取指定网关的转换器实例 |

**业务场景**: 某些交易所不支持"平今/平昨"区分，或用户需要锁仓对冲，`OffsetConverter` 自动将一笔平仓请求拆分为多笔符合交易所规则的子委托。

### 5.4 查询接口

提供**单条查询**和**批量查询**两类接口：

- **单条**: `get_tick(vt_symbol)`, `get_order(vt_orderid)` 等 — 字典 get 操作
- **批量**: `get_all_ticks()`, `get_all_orders()` 等 — 返回 values 列表
- **过滤**: `get_all_active_orders()`, `get_all_active_quotes()` — 仅返回活跃状态的记录

---

## 六、EmailEngine — 邮件发送引擎

**位置**: 第 590–654 行

### 职责
提供**异步邮件发送**能力，通过独立后台线程和消息队列避免阻塞主流程。

### 架构设计

```
send_email() ──构建EmailMessage──▶ Queue ──▶ 后台线程 run()
                                              ├── 从队列取消息
                                              ├── SMTP_SSL 连接发送
                                              └── 异常时写日志
```

### 核心流程

1. **延迟启动**: 第一次调用 `send_email` 时才启动后台线程（`start()`）
2. **配置来源**: 从全局 `SETTINGS` 中读取 SMTP 服务器、端口、账号密码、默认收件人
3. **线程模型**: 单个守护线程 + 阻塞队列（`Queue`），超时轮询 1 秒
4. **异常处理**: 发送失败不中断线程，仅记录错误日志

### 生命周期

| 方法 | 行为 |
|------|------|
| `send_email` | 构建 `EmailMessage` 并入队；首次调用时自动 `start()` |
| `start()` | 设置 `active=True`，启动后台线程 |
| `run()` | 循环：取消息 → SMTP发送 → 异常记录 |
| `close()` | 设置 `active=False`，等待线程结束 |

---

## 七、WechatEngine — 微信推送引擎

**位置**: 第 657–839 行

### 职责
通过微信企业号/iLink 机器人向指定用户**推送文本通知**，支持消息批量合并、发送频率控制、会话过期处理。

### 7.1 配置管理

| 方法 | 说明 |
|------|------|
| `load_setting()` | 从 `wechat_setting.json` 加载凭证和配置 |
| `save_setting()` | 持久化当前配置到 JSON 文件 |
| `bind(creds, user_id)` | 绑定新凭证（扫码后调用），自动保存并激活 |
| `unbind()` | 清除凭证，停止推送，持久化 |

**配置字段**:
- `bot_id` / `token` / `base_url` — 微信机器人凭证
- `user_id` (或 `chat_id`) — 推送目标用户
- `send_interval` — 发送间隔（秒），默认 60 秒

### 7.2 消息发送流程

```
send_wechat(msg)
  └── 入队 Queue

run() 后台线程循环:
  ├── 从队列阻塞取一条消息 → 加入 pending_msgs
  ├── 非阻塞排空队列中所有剩余消息 → 合并到 pending_msgs
  ├── 检查发送间隔（send_interval）
  │    └── 未到间隔 → continue 等待
  ├── 合并所有 pending_msgs 为一条文本
  ├── 调用 send_text() 发送
  │    ├── 成功 → 清空 pending_msgs，更新 last_ts
  │    ├── SessionExpired → 消息放回队列头部，停止推送，记录过期日志
  │    └── WeixinError → 消息放回队列头部，记录错误日志
  └── 继续循环
```

### 7.3 关键业务特性

- **消息合并**: 短时间内的多条消息合并为一条发送，减少 API 调用频率
- **频率控制**: 通过 `send_interval` 限制最小发送间隔（默认 60 秒）
- **会话过期处理**: `SessionExpired` 异常时自动停止推送，提示用户重新扫码
- **失败重试**: 非过期错误时，消息放回队列头部，下次循环自动重试
- **线程安全**: 使用 `Queue` + `active` 标志 + `current_thread()` 检查，防止死锁

### 7.4 生命周期

```
__init__ → load_setting → (有凭证?) → activate()
                                         └── start() → 启动后台线程

bind() → deactivate → 更新凭证 → save_setting → activate
unbind() → deactivate → 清除凭证 → save_setting
close() → deactivate()
```

---

## 八、整体数据流

```
┌──────────────┐     事件      ┌─────────────────────────────┐
│  Gateway     │ ─────────────▶│  EventEngine (事件总线)       │
│  (交易网关)   │               │                              │
└──────────────┘               │  EVENT_TICK / EVENT_ORDER /  │
                               │  EVENT_TRADE / EVENT_POSITION │
┌──────────────┐               │  EVENT_ACCOUNT / EVENT_LOG    │
│  策略/应用     │◀────订阅─────│  EVENT_CONTRACT / EVENT_QUOTE │
└──────────────┘               └──────────┬───────────────────┘
                                          │ 事件分发
                    ┌──────────────────────┼──────────────────┐
                    ▼                      ▼                  ▼
             ┌────────────┐      ┌──────────────┐    ┌────────────┐
             │ LogEngine  │      │  OmsEngine   │    │ 策略/应用    │
             │ (日志输出)  │      │ (数据管理)    │    │ (业务逻辑)   │
             └────────────┘      │ + Converter  │    └────────────┘
                                 └──────────────┘
                                          │
                    ┌─────────────────────┤
                    ▼                     ▼
             ┌────────────┐      ┌──────────────┐
             │EmailEngine │      │ WechatEngine │
             │ (邮件通知)  │      │ (微信推送)    │
             └────────────┘      └──────────────┘
```

---

## 九、设计模式总结

| 模式 | 应用位置 |
|------|---------|
| **外观模式 (Facade)** | `MainEngine` 将 OMS 查询方法代理到自身，简化外部调用 |
| **策略模式 (Strategy)** | `BaseEngine` 定义统一接口，各引擎提供不同实现 |
| **生产者-消费者** | `EmailEngine` 和 `WechatEngine` 使用 Queue + 后台线程异步处理 |
| **中介者模式 (Mediator)** | `MainEngine` 作为中心协调者，管理 Gateway、Engine、App 之间的交互 |
| **观察者模式 (Observer)** | `EventEngine` 的事件注册/分发机制 |
| **延迟初始化** | `EmailEngine` 首次发送时启动；`OffsetConverter` 收到合约时创建 |

---

## 十、关键依赖关系

| 依赖 | 用途 |
|------|------|
| `EventEngine` / `Event` | 事件驱动核心 |
| `BaseGateway` | 交易网关抽象 |
| `BaseApp` | 应用抽象 |
| `OffsetConverter` | 开平转换逻辑 |
| `object.*` (TickData, OrderData 等) | 交易数据对象 |
| `SETTINGS` | 全局配置（日志开关、SMTP 配置等） |
| `load_json` / `save_json` | 配置文件持久化 |
| `logger` (loguru) | 日志输出后端 |
| `wechat.send_text` | 微信推送 API |
