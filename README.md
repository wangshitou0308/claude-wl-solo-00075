# 单手腊叶标本翻拍接力 API

面向**上肢缺失或仅一只手可主动操作**的志愿者，为地方档案馆逐页翻拍脆弱腊叶标本的纯后端服务。
操作者无法同时扶稳标本册、翻隔页并触发相机，因此服务把每个页面流程编排成**接力式**动作序列：
支撑职责在「可用手 ↔ 夹具 / 压条 / 翻页垫」之间传递，任何时刻只安排能力档案可完成的动作。

- 纯后端：Python 3.13 + FastAPI + Pydantic + SQLite（单文件本机库，无任何外部服务）
- 资料仅存本机，可本机原生运行

## 运行

```bash
# Python 3.13
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # 运行依赖
.venv/bin/pip install -r requirements-dev.txt      # 测试依赖（可选）

.venv/bin/uvicorn app.main:app --reload            # 默认库文件 ./herbarium.db
# 或指定库位置：HERBARIUM_DB=/path/to/herbarium.db .venv/bin/uvicorn app.main:app
```

交互式文档：`http://127.0.0.1:8000/docs`

```bash
.venv/bin/python -m pytest                          # 17 个端到端测试
```

## 领域模型

| 概念 | 说明 |
| --- | --- |
| 能力档案 `capability_profiles` | 逐项登记可用手（左/右/无）、脚踏开关、单键开关、语音确认——不假定能力相同 |
| 标本册 `volumes` / 册页 `pages` | 装订方向（左/右/上装订）+ 册页次序（页码、正/反面、是否有隔页） |
| 易碎区 `fragile_zones` | 页面归一化坐标矩形，**压条不得落入** |
| 器材 `equipment` | 夹具、压条（含宽度）、翻页垫、相机遥控（含触发方式 hand/foot_pedal/single_key/voice） |
| 翻拍计划 `plans` / 步骤 `steps` | 编排结果：每步含指令、占用通道、支撑位状态、是否安全落点、输入指纹 |
| 执行会话 `sessions` / 事件 `events` | 统一确认/撤回接口驱动；全部事件入档 |
| 完成版 | 会话完成即冻结**实际路径**（含撤回/异常/重排痕迹）与**输入哈希**，不可再写 |

## 编排约束（`app/planner.py`）

每页流程：**固定 → 揭隔页 → 展平 → 拍摄前核对 → 拍摄 → 拍摄后复核 → 复位**（无隔页的页跳过揭隔页）。

1. **能力匹配**：徒手工步需要可用手；拍摄需要与档案匹配的遥控触发通道；任一约束不满足则整个计划返回 422 并列出全部原因——不会编排能力档案完不成的动作。
2. **压条避障**：在候选落位中扫描，矩形相交检测保证压条不落入易碎区；全部被覆盖则判定不可行。
3. **支撑不变量**：每步记录支撑位占用（`support_state`），拍摄/核对时必须已有夹具或压条支撑；未获得稳定支撑不得松手或翻页（由 `validate_support` 校验）。
4. **拍摄前后核对**：核对步骤的确认事件必须携带 `checks={page_number, side, scale}` 三项全真，否则 409 并记录 `check_failed`。
5. **安全落点**：固定、展平、复核、复位完成处为检查点，异常后从最近落点重排。

## 主要接口

```
POST   /capability-profiles            登记能力档案          PATCH /capability-profiles/{id}
POST   /volumes                        登记标本册            PUT   /volumes/{id}/pages（册页次序）
POST   /volumes/{id}/fragile-zones     登记易碎区            PATCH /volumes/{id}（装订方向等）
POST   /equipment                      登记器材              PATCH /equipment/{id}
POST   /plans                          编排计划（含全部步骤） GET   /plans/{id}
POST   /sessions                       开启执行会话          GET   /sessions/{id}
POST   /sessions/{id}/events           统一确认/撤回（单键/脚踏/语音/徒手同一接口）
POST   /sessions/{id}/exceptions       异常上报 → 最近安全落点重排
POST   /sessions/{id}/replan           资料变更后重排过期步骤
GET    /sessions/{id}/events           事件流（审计轨迹）
```

### 统一确认/撤回

```json
POST /sessions/{id}/events
{"type": "confirm",  "source": "foot_pedal", "checks": {"page_number": true, "side": true, "scale": true}}
{"type": "withdraw", "source": "single_key"}
```

- `source` 为 `single_key`（单键顺序扫描）/ `foot_pedal` / `voice` / `hand`，走同一接口、同一状态机；档案未登记的来源会被拒绝（409）。
- `client_event_id` 可选，用于开关抖动/重试的**幂等去重**：重复提交返回首次结果并标记 `duplicate: true`。

### 异常后从最近安全落点重排

```
POST /sessions/{id}/exceptions {"kind": "page_slipped", "detail": "压条滑脱"}
```

回退到最近的已达成检查点，落点之后的步骤（含已完成的）用最新资料重新编排；事件流保留异常与重排记录。

### 资料改变只使受影响步骤过期

- 修改某页易碎区 → 仅该页待执行步骤标记 `stale`；改册页内容 → 仅该页；改装订方向/能力档案 → 影响各自依赖步骤；已完成步骤与已完成计划不受影响。
- 确认到过期步骤返回 409，调用 `POST /sessions/{id}/replan` 从当前位置用最新资料重排（压条自动避开新易碎区）；会话开始前资料已变的，开启会话时自动整体重排。

### 完成版冻结

最后一步确认后会话自动完成：`frozen_path` 冻结实际执行路径（含撤回/异常/重排痕迹），`input_hash` 冻结全部步骤输入指纹与事件序列的 SHA-256；之后任何事件、异常、重排请求均返回 409。

## 目录结构

```
app/
  main.py        应用工厂与入口（create_app）
  db.py          SQLite 模式与连接
  planner.py     编排器：能力匹配、压条避障、支撑不变量、安全落点、重排起点
  runtime.py     步骤存取与计划上下文加载
  routers/       profiles / volumes / equipment / plans / sessions
  schemas.py     Pydantic 入参模型
  hashing.py     规范化 JSON + SHA-256
tests/test_api.py  17 个端到端测试
```
