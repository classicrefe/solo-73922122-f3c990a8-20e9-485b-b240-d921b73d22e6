# SQL 改写有界等价核对服务

面向数仓质控场景：维护员替换站点读数入仓质控 SQL 前，提交**原始 SELECT** 与
**改写 SELECT**，服务在固定模式下枚举每表 **0~3 行**的所有合法取值，核对两侧
结果**多重集**是否一致（按列位置比较、忽略行序、保留重复），用于排查改写对
缺测（NULL）与重复采样的影响。

- 差异必须在**隔离 PostgreSQL** 中实际复现才报 `NOT_EQUIVALENT`，并返回
  可装载 SQL 与两侧实际结果；
- 界内无反例报 `EQUIVALENT`（仅三行界内结论，不构成更大表上的证明）；
- 求解或复现失败报 `UNKNOWN`。

另提供**待补测读数影响分析**（`POST /alert-bounds`）：有几条读数尚未补测
（`value` 为 NULL）时，按给定补测区间求每站告警行数在所有合法补测取值下的
最少 / 最多值，标出"最少未达阈值、最多达阈值"的待补测站点，极值附完整补测
值并在隔离 PostgreSQL 中重跑确认。

## 固定模式

```sql
CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER);
CREATE TABLE readings (station_id INTEGER REFERENCES stations(id), value INTEGER);
```

全列 `integer`；非主键列可 `NULL`；`readings.station_id` 为外键（NULL 合法）。

## 支持的 SQL 子集（其余一律 400 拒绝）

| 子句 | 支持内容 |
| --- | --- |
| SELECT | 仅列投影（可带表限定、别名；不支持 `*`、表达式、函数、字面量） |
| FROM | 单表，或两表间至多一次 `INNER` / `LEFT [OUTER]` 等值连接（`ON a.col = b.col`） |
| WHERE | 列与整数字面量比较（`=` `!=` `<` `<=` `>` `>=`）、`IS [NOT] NULL`、`AND` / `OR` / `NOT`、括号 |

不支持：聚合 / `GROUP BY` / `HAVING` / `ORDER BY` / `LIMIT` / `DISTINCT` /
子查询 / `IN` / `BETWEEN` / 字符串与浮点字面量 / 列间比较（WHERE 内）/
自连接 / `RIGHT` / `FULL` / `CROSS` 连接 / 写操作等。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# API: http://localhost:8000  交互文档: http://localhost:8000/docs
```

`docker-compose.yml` 启动两个服务：`db`（postgres:16，仅内部网络，专用于
反例复现）与 `api`（FastAPI）。无需初始化数据——复现用的表结构由服务在
临时 schema 中自建，事务回滚即清理。

## API 用法

### `POST /check`

```json
{
  "query_a": "SELECT id FROM stations WHERE threshold = 5",
  "query_b": "SELECT id FROM stations WHERE threshold = 5 OR threshold IS NULL",
  "solver_timeout_ms": 10000
}
```

响应（`NOT_EQUIVALENT` 示例）：

```json
{
  "verdict": "NOT_EQUIVALENT",
  "bound_rows": 3,
  "message": "两侧结果存在差异，已在隔离 PostgreSQL 中实际复现。",
  "counterexample": {
    "setup_sql": "DROP TABLE IF EXISTS readings;\nDROP TABLE IF EXISTS stations;\nCREATE TABLE stations ...\nINSERT INTO stations ...",
    "query_a_result": [],
    "query_b_result": [[-1]]
  }
}
```

- `verdict=EQUIVALENT`：每表 0~3 行所有合法取值下结果多重集相同；
- `verdict=NOT_EQUIVALENT`：`counterexample.setup_sql` 可直接装载到任意
   scratch 库复现差异，`query_a_result` / `query_b_result` 为实际执行结果；
- `verdict=UNKNOWN`：z3 超时/无法判定，或 PostgreSQL 复现失败（`message`
  说明原因）。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/check \
  -H 'Content-Type: application/json' \
  -d '{"query_a":"SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id",
       "query_b":"SELECT id FROM stations"}'
```

### 其他端点

- `GET /`：服务信息；
- `GET /health`：健康检查（含数据库连通性）；
- `GET /docs`：Swagger 交互文档，内置等价 / 不等价（NULL 语义、LEFT JOIN
  重复行）等命名示例，可直接 "Try it out"。

### `POST /alert-bounds`（待补测读数影响分析）

有几条读数还没补测（`value` 为 NULL）时，值班员需要知道哪些站点的告警
可能因此达到处置阈值。提交 stations / readings 快照（每表至多 3 行，沿用
固定模式的主键 / 外键 / int4 约束）、按 readings 输入行号（0 起始）为每个
待补测 NULL value 指定的整数闭区间、告警条数阈值，以及一条质控 SELECT
（须以 stations 为 FROM 表、仅投影 stations.id，连接与筛选沿用上述语法
子集）。

```json
{
  "stations": [{"id": 1, "threshold": 5}, {"id": 2, "threshold": 5}],
  "readings": [
    {"station_id": 1, "value": 7},
    {"station_id": 1, "value": null},
    {"station_id": 2, "value": 3}
  ],
  "fill_ranges": [{"row": 1, "min": 0, "max": 10}],
  "alert_threshold": 2,
  "query": "SELECT s.id FROM stations s INNER JOIN readings r ON s.id = r.station_id WHERE r.value >= 5"
}
```

响应（节选，站 1 待补测）：

```json
{
  "status": "OK",
  "alert_threshold": 2,
  "stations": [
    {
      "station_id": 1,
      "min_count": 1,
      "max_count": 2,
      "pending": true,
      "min_extremum": {"count": 1, "fills": [{"row": 1, "value": 0}]},
      "max_extremum": {"count": 2, "fills": [{"row": 1, "value": 10}]}
    }
  ],
  "message": "共 2 站，1 站待补测……"
}
```

- 对每站给出结果行数（重复行照计）在所有合法补测取值下的 `min_count` /
  `max_count`；`min_count < alert_threshold <= max_count` 的站点
  `pending=true`（待补测）；
- 每个极值附一组**完整补测值**（`fills`：readings 行号 -> 取值），并已在
  隔离 PostgreSQL 中按该补测值重跑查询、确认行数与极值一致；
- 其余数据照原样参与查询；`fill_ranges` 必须覆盖且仅覆盖 value 为 NULL
  的行，否则 400；
- 求解（z3 超时 / 无法判定）或复算（数据库不可达 / 行数不符）无法确认时
  返回 `UNKNOWN`。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/alert-bounds \
  -H 'Content-Type: application/json' \
  -d '{"stations":[{"id":1,"threshold":5}],
       "readings":[{"station_id":1,"value":null}],
       "fill_ranges":[{"row":0,"min":0,"max":10}],
       "alert_threshold":1,
       "query":"SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id WHERE r.value >= 5 OR r.value IS NULL"}'
```

## 判定原理

1. **解析与白名单校验**（`app/sql_parser.py`，基于 sqlglot）：越界语法拒绝。
2. **z3 有界判定**（`app/z3check.py`）：两表各 0~3 行符号化，施加主键唯一、
   外键引用、int4 值域约束；WHERE 按 SQL 三值逻辑编码（`UNKNOWN` 被过滤）；
   JOIN/LEFT JOIN 展开为符号行；结果多重集差异编码为"存在某元组值两侧计数
   不同"。`UNSAT` 即界内等价，`SAT` 提取候选反例。
3. **PostgreSQL 复现**（`app/verifier.py`）：候选反例在独立临时 schema 中
   建表、装载、实际执行两条查询并比较多重集；确认差异才报
   `NOT_EQUIVALENT`，否则报 `UNKNOWN`。

`/alert-bounds` 的判定（`app/bounds.py`）：快照为具体数据，仅待补测的 NULL
value 以 z3 变量代替并约束在请求区间内；查询在具体快照上展开（连接匹配涉及
待补测 value 时匹配本身也是符号条件），对每站行数表达式用 z3 Optimize 求
最小 / 最大值并提取达到极值的补测见证；随后每个见证在隔离 PostgreSQL 中
重跑确认行数（`Verifier.run_query`，同样的单事务 + 临时 schema 隔离）。

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/postgres` | 复现用 PostgreSQL DSN |
| `SOLVER_TIMEOUT_MS` | `10000` | z3 求解超时（可被请求字段 `solver_timeout_ms` 覆盖；`/alert-bounds` 中为单次极值求解的超时） |

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/                       # 单元测试（解析器 / z3 / API 打桩）
DATABASE_URL=postgresql://... pytest tests/test_e2e.py   # 端到端（需真实 PG）
```

## 已知边界

- 等价结论仅限每表 ≤3 行（有界模型检验，非全量证明）；
- 仅支持上述 SQL 子集；两查询投影列数不一致返回 400；
- 复现使用单事务 + 临时 schema，不在库中留存对象；数据库不可达时
  不等价判定降级为 `UNKNOWN`（等价判定不依赖数据库）；
- `/alert-bounds` 的查询须以 stations 为 FROM 表且仅投影 stations.id；
  其结论依赖请求给定的补测区间，区间之外的取值不在考虑范围内。
