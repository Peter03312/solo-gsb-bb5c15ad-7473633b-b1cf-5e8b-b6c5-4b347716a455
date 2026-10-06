# 博物馆借展运输环境监控系统

面向博物馆借展运输场景的中文 Web 应用：按批次管理运输节点与文物，采集温湿度读数，
按时间轴自动核验并生成风险事件，支持处置闭环与评估版本管理。

**零第三方依赖**：仅使用 Python 3.9+ 标准库（`http.server` + `sqlite3`），
Docker 镜像无需安装任何包即可构建运行。

## 功能特性

- **持久化**：批次、有序运输节点、每件文物的温湿度上下限与约定采样间隔、
  绑定文物与节点的带时间戳读数，全部存入 SQLite。
- **按时间核验**：读数按时间轴排序评估；同一文物同一指标（温度/湿度）的连续越界
  读数**跨节点合并**为一个风险事件；相邻读数间隔超过约定采样间隔时标出**缺测区间**
  并**中断**正在进行的事件。
- **处置闭环**：工作人员必须填写**处置说明**与**证据编号**才能关闭事件。
- **评估版本**：补录或更正读数自动生成新的评估版本；旧版本的读数与关闭结论保持
  可查（只读快照）；**新版事件不自动继承关闭状态**；提供版本间差异展示
  （新增/更正/移除读数，新增/消失/变化事件，关闭状态未继承清单）。
- **输入校验**：重复时间戳、无序上下限（下限 > 上限）、非数值读数一律拒绝保存，
  并返回中文原因。
- **页面**：首页与批次页展示批次风险摘要（开放/已关闭事件、按指标统计、缺测区间数、
  涉险文物等）。

## 快速开始

### 方式一：Docker Compose（一键启动）

```bash
docker compose up --build
```

### 方式二：Docker

```bash
docker build -t museum-monitor .
docker run -d -p 8000:8000 -v museum-data:/data --name museum-monitor museum-monitor
```

### 方式三：本地直接运行（无需安装依赖）

```bash
python3 app.py
```

## 配置

通过环境变量配置（均有默认值）：

| 变量      | 默认值             | 说明                     |
| --------- | ------------------ | ------------------------ |
| `HOST`    | `0.0.0.0`          | 监听地址                 |
| `PORT`    | `8000`             | 监听端口                 |
| `DB_PATH` | `museum.db`（容器内 `/data/museum.db`） | SQLite 数据库文件路径 |

## 地址与端口

- 页面入口：<http://localhost:8000/>
  - `/` 批次列表与风险摘要
  - `/batches/<id>` 批次详情（摘要、节点、文物、读数录入、事件处置、缺测区间、版本）
  - `/batches/<id>/versions/<n>` 历史版本只读快照
  - `/batches/<id>/diff?from=<a>&to=<b>` 版本差异
- API 前缀：`http://localhost:8000/api`
- 健康检查：`GET /api/health`

## 数据模型

| 表          | 说明                                                                 |
| ----------- | -------------------------------------------------------------------- |
| `batches`   | 运输批次                                                             |
| `nodes`     | 运输节点，`seq` 为批次内顺序（唯一）                                  |
| `artifacts` | 文物：温湿度上下限、`sample_interval` 约定采样间隔（秒）              |
| `versions`  | 评估版本：每次提交读数生成一个版本，批次内版本号递增                  |
| `readings`  | 读数：绑定版本、文物、节点、时间戳、温度、湿度；`corrected_from` 标记更正来源 |
| `events`    | 风险事件：指标、起止时间、读数个数、实测范围、限值、涉及节点、最大偏离、状态与关闭结论 |
| `gaps`      | 缺测区间：起止时间、约定间隔、实际间隔                                |

## 业务规则

1. **事件合并**：同一文物同一指标，按时间轴相邻的越界读数合并为一个事件，
   与所在节点无关（可跨节点），事件记录涉及的全部节点（按节点顺序）。
2. **缺测中断**：相邻读数间隔 `> sample_interval` 时记缺测区间，进行中的事件
   在缺测前的最后一条越界读数处结束；缺测之后若仍越界，生成新事件。
3. **关闭条件**：`note`（处置说明）与 `evidence_no`（证据编号）均必填；
   仅可关闭**当前版本**的事件，历史版本事件与结论只读可查。
4. **版本化**：每次提交读数（追加/补录/更正）都生成新版本并重新评估；
   更正指同一文物同一时间戳但数值不同（旧版本中的旧值保持不变）；
   新版本事件一律为开放状态，不继承旧版关闭结论。
5. **拒绝保存**（返回 400 与中文原因）：
   - 重复时间戳：同一提交内重复，或与当前版本已有读数完全相同；
   - 无序上下限：温度/湿度下限大于上限；
   - 非数值读数：温度/湿度无法解析为数值；
   - 其他：时间戳格式无效、文物/节点不属于该批次、采样间隔非正整数等。

## HTTP API

所有 API 均返回 JSON；错误响应为 `{"error": "原因", "details": [...]}`。

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET  | `/api/health` | 健康检查 |
| POST | `/api/batches` | 创建批次 |
| GET  | `/api/batches` | 批次列表（含各批次风险摘要） |
| GET  | `/api/batches/<id>` | 批次详情（节点、文物、摘要） |
| POST | `/api/batches/<id>/nodes` | 添加运输节点 |
| POST | `/api/batches/<id>/artifacts` | 登记文物（限值 + 采样间隔） |
| POST | `/api/batches/<id>/readings` | 提交读数，生成新评估版本 |
| GET  | `/api/batches/<id>/summary` | 批次风险摘要（当前版本） |
| GET  | `/api/batches/<id>/events?status=open&version=<n>` | 事件列表（默认当前版本） |
| GET  | `/api/batches/<id>/versions` | 版本列表 |
| GET  | `/api/batches/<id>/versions/<n>` | 版本快照（读数、事件、缺测区间） |
| GET  | `/api/batches/<id>/diff?from=<a>&to=<b>` | 版本差异 |
| POST | `/api/events/<id>/close` | 关闭事件（须处置说明 + 证据编号） |

### 调用示例

```bash
BASE=http://localhost:8000

# 1. 创建批次
curl -s -X POST $BASE/api/batches -H 'Content-Type: application/json' \
  -d '{"name":"2026 春季书画巡展","description":"本馆 → 巡展场馆"}'

# 2. 添加有序节点
curl -s -X POST $BASE/api/batches/1/nodes -H 'Content-Type: application/json' \
  -d '{"seq":1,"name":"库房","location":"本馆"}'
curl -s -X POST $BASE/api/batches/1/nodes -H 'Content-Type: application/json' \
  -d '{"seq":2,"name":"运输车"}'

# 3. 登记文物（温度 18~24℃，湿度 45~60%RH，采样间隔 600 秒）
curl -s -X POST $BASE/api/batches/1/artifacts -H 'Content-Type: application/json' \
  -d '{"name":"青花瓷瓶","temp_min":18,"temp_max":24,"hum_min":45,"hum_max":60,"sample_interval":600}'

# 4. 提交读数（生成评估版本 v1；温度跨节点连续越界将合并为一个事件）
curl -s -X POST $BASE/api/batches/1/readings -H 'Content-Type: application/json' -d '{
  "note": "首批数据",
  "readings": [
    {"artifact_id":1,"node_id":1,"ts":"2026-10-01T08:00:00Z","temperature":20,"humidity":50},
    {"artifact_id":1,"node_id":1,"ts":"2026-10-01T08:10:00Z","temperature":26,"humidity":50},
    {"artifact_id":1,"node_id":2,"ts":"2026-10-01T08:20:00Z","temperature":27,"humidity":50},
    {"artifact_id":1,"node_id":2,"ts":"2026-10-01T08:30:00Z","temperature":21,"humidity":50}
  ]}'

# 5. 关闭事件（缺任一项将被拒绝）
curl -s -X POST $BASE/api/events/1/close -H 'Content-Type: application/json' \
  -d '{"note":"已调整车厢温控并复查","evidence_no":"EV-2026-0001"}'

# 6. 更正读数（同一时间戳、不同数值）→ 生成 v2，新版事件不继承关闭状态
curl -s -X POST $BASE/api/batches/1/readings -H 'Content-Type: application/json' \
  -d '{"note":"更正运输车读数","readings":[{"artifact_id":1,"node_id":2,"ts":"2026-10-01T08:20:00Z","temperature":22,"humidity":50}]}'

# 7. 查看版本差异与批次风险摘要
curl -s "$BASE/api/batches/1/diff?from=1&to=2"
curl -s "$BASE/api/batches/1/summary"

# 8. 校验拒绝示例（均返回 400 与原因）
curl -s -X POST $BASE/api/batches/1/readings -H 'Content-Type: application/json' \
  -d '{"readings":[{"artifact_id":1,"node_id":1,"ts":"2026-10-01T08:00:00Z","temperature":20,"humidity":50}]}'   # 重复时间戳
curl -s -X POST $BASE/api/batches/1/readings -H 'Content-Type: application/json' \
  -d '{"readings":[{"artifact_id":1,"node_id":1,"ts":"2026-10-01T10:00:00Z","temperature":"偏高","humidity":50}]}' # 非数值
curl -s -X POST $BASE/api/batches/1/artifacts -H 'Content-Type: application/json' \
  -d '{"name":"错误示例","temp_min":30,"temp_max":10,"hum_min":45,"hum_max":60,"sample_interval":600}'              # 无序上下限
```

也可以直接运行端到端演示脚本（服务启动后）：

```bash
./scripts/demo.sh
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 目录结构

```
├── app.py               # 入口：HTTP 服务与路由（页面 + API）
├── museum/
│   ├── db.py            # SQLite 连接与表结构
│   ├── evaluate.py      # 风险事件评估（跨节点合并、缺测中断）
│   ├── diff.py          # 版本差异计算
│   ├── service.py       # 业务逻辑与校验
│   ├── web.py           # 极简路由/响应封装
│   └── pages.py         # 中文页面渲染
├── tests/test_app.py    # 评估逻辑单元测试 + API 端到端测试
├── scripts/demo.sh      # curl 端到端演示
├── Dockerfile
├── docker-compose.yml
└── README.md
```
