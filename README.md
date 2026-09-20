# 匿名评审分配 API（Anonymous Review Assignment）

基于 Python + FastAPI 的评审分配服务。`POST /assign` 将「稿件—评审人」分配问题整体规约为
**全局最大流**（Edmonds-Karp，非逐稿贪心），一次求出同时满足以下全部约束的完整分配：

- 每篇稿件获得指定数量的评审人；
- 评审人主题能力与稿件主题匹配；
- 回避利益冲突（含自稿冲突）；
- 评审人分配总量不超过其容量；
- 评审人声明了某赛道限额时，其在该赛道上的分配量不超过对应限额
  （赛道限额与总容量在同一流网络中联立求解，而非分赛道独立求解后拼接）。

无可行解时返回未满足稿件及候选/容量缺口诊断，并在求解结束的残量网络上给出
**全局最小割证明**（`bottleneck_cut`：源侧节点 + 按 `demand` / `eligibility` /
`track_capacity` / `reviewer_capacity` 分类的跨割饱和边 + `max_assignable` 与
`shortfall`），**不输出任何部分分配结果**；
相同输入（甚至不同的列表顺序）必然产生完全相同的输出。服务无状态，不持久化任何数据。

请求携带 `previous_assignments` / `locked_assignments` 时进入**增量重排模式**：
锁定关系经稿件需求、评审资格与容量校验后强制保留（任一冲突即整体不可行并返回结构化原因），
其余名额在保证全局可行的前提下以**最小费用最大流**求解，使相对上一轮的
**新增 + 移除关系总数最少**，同成本解按 ID 顺序确定性破除。

## 快速启动（单条命令）

```bash
docker compose up --build
```

依赖安装与初始化全部在镜像构建与容器启动时完成，无需任何额外步骤。

启动后访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/assign | 分配接口（POST） |
| http://localhost:8000/docs | Swagger 交互式文档 |
| http://localhost:8000/redoc | ReDoc 文档 |

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 容器内服务监听端口 |

宿主机端口映射在 `docker-compose.yml` 的 `ports` 中调整（默认 `8000:8000`）。

不使用 Compose 时也可直接构建运行：

```bash
docker build -t review-assign .
docker run -p 8000:8000 review-assign
```

## API 说明

### `GET /health`

```json
{"status": "ok"}
```

### `POST /assign`

请求体：

```json
{
  "required_reviewers": 2,
  "manuscripts": [
    {"id": "m1", "authors": ["alice"], "topics": ["ml"]},
    {"id": "m2", "authors": ["bob"], "topics": ["ml", "db"], "required_reviewers": 1}
  ],
  "reviewers": [
    {"id": "r1", "topics": ["ml"], "capacity": 2},
    {"id": "r2", "topics": ["ml", "db"], "capacity": 1, "conflicted_authors": ["alice"]},
    {"id": "r3", "topics": ["db"], "capacity": 2, "conflicted_manuscripts": ["m1"]},
    {"id": "r4", "topics": ["ml"], "capacity": 1}
  ]
}
```

字段规则：

- `required_reviewers`（请求级，默认 `1`）：每稿默认所需评审数；稿件级
  `required_reviewers` 可对其覆盖。
- 稿件 `topics` 非空时，评审人须与其有交集才具备能力；稿件 `topics` 为空则不做主题限制。
- 冲突回避（满足任一即不可评审）：
  - 评审人 `id` 出现在稿件 `authors` 中（自稿冲突）；
  - 稿件作者与评审人 `conflicted_authors` 有交集；
  - 稿件 `id` 出现在评审人 `conflicted_manuscripts` 中。
- 分赛道容量（均可选，省略时行为与历史版本完全一致）：
  - 稿件 `track`：稿件所属赛道；
  - 评审人 `track_capacities`：`赛道 -> 非负整数限额` 的映射。稿件赛道出现在某
    评审人的映射中时，该评审人在此赛道上的分配数不得超过对应限额，且与总容量
    `capacity` 同时生效；未列入映射的赛道与不携带 `track` 的稿件只受总容量约束。
- 稿件 / 评审人 `id` 必须各自唯一，否则返回 `422`。

可行时响应（`200`）：

```json
{
  "status": "feasible",
  "total_required": 3,
  "total_assigned": 3,
  "assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1", "r4"]},
    {"manuscript_id": "m2", "reviewer_ids": ["r1"]}
  ],
  "unsatisfied": null,
  "shared_pool": null,
  "locked_conflicts": null,
  "bottleneck_cut": null
}
```

不可行时响应（`200`，`assignments` 为 `null`，不输出部分结果）：

```json
{
  "status": "infeasible",
  "total_required": 4,
  "total_assigned": 2,
  "assignments": null,
  "unsatisfied": [
    {
      "manuscript_id": "m2",
      "required": 2,
      "assigned": 0,
      "missing": 2,
      "eligible_reviewers": 2,
      "eligible_remaining_capacity": 0,
      "candidate_gap": 0,
      "capacity_gap": 2
    }
  ],
  "shared_pool": {
    "manuscript_ids": ["m2"],
    "reviewer_ids": ["r1", "r2"],
    "missing_total": 2,
    "pool_remaining_capacity": 0,
    "pool_total_capacity": 2
  },
  "locked_conflicts": null,
  "bottleneck_cut": {
    "source_manuscripts": ["m1", "m2"],
    "source_reviewer_tracks": [],
    "source_reviewers": ["r1", "r2"],
    "demand": [],
    "eligibility": [],
    "track_capacity": [],
    "reviewer_capacity": [
      {"type": "reviewer_capacity", "capacity": 1, "manuscript_id": null, "reviewer_id": "r1", "track": null},
      {"type": "reviewer_capacity", "capacity": 1, "manuscript_id": null, "reviewer_id": "r2", "track": null}
    ],
    "max_assignable": 2,
    "shortfall": 2
  }
}
```

诊断字段含义：

- `eligible_reviewers`：通过主题与冲突过滤的合格候选人数；
- `eligible_remaining_capacity`：最大流结束后这些候选对该稿赛道的剩余可接量之和
  （候选受赛道限额约束时，取总剩余容量与赛道剩余限额的较小者）；
- `candidate_gap`：候选人数缺口（`required - eligible_reviewers`，候选本身不足）；
- `capacity_gap`：容量缺口（`missing - eligible_remaining_capacity`，候选够但容量不足）；
- `shared_pool`：所有未满足稿件共享的候选池及其总/剩余容量，用于定位整体瓶颈。

#### `bottleneck_cut`：不可行的全局最小割证明

最大流结束后，从源点沿**残量容量大于 0** 的边可达的节点构成源侧集合 `S`；由
最大流最小割定理，所有 `S → 非 S` 的正向边恰好全部饱和，其容量之和（割容量）
等于最大流——即在主题、冲突、赛道限额与总容量全部约束下**至多可满足的名额数**
`max_assignable`，`shortfall = total_required - max_assignable` 为严格缺口下界。

- `source_manuscripts` / `source_reviewers`：源侧的稿件 / 评审人业务 ID（按 ID 排序）；
- `source_reviewer_tracks`：源侧的「(评审人, 赛道)」限额节点，二元组
  `[reviewer_id, track]`，按业务 ID 排序；
- 跨割饱和边按业务含义分四组列出（组内按业务 ID 确定排序，列表乱序输出不变）：

| 分组 | 边 | 携带字段 |
| --- | --- | --- |
| `demand` | 源 → 稿件（该稿需求整体跨割） | `manuscript_id`, `capacity` |
| `eligibility` | 稿件 → 评审人 / (评审人,赛道) 节点（合格关系构成的 Hall 型瓶颈） | `manuscript_id`, `reviewer_id`, `track`（走赛道节点时）, `capacity` |
| `track_capacity` | (评审人,赛道) → 评审人（赛道限额饱和，含限额为 0 的边） | `reviewer_id`, `track`, `capacity` |
| `reviewer_capacity` | 评审人 → 汇（评审总容量饱和） | `reviewer_id`, `capacity` |

示例：

- 总容量不足时，割边全在 `reviewer_capacity`（如上方示例，割容量 2 = 至多 2 个名额）；
- 合格候选不足时，割边出现在 `eligibility`：下例割容量为 1（唯一合格关系 `m1→r1`），
  证明 2 个名额至多满足 1 个；

```json
{
  "bottleneck_cut": {
    "source_manuscripts": ["m1"],
    "source_reviewer_tracks": [],
    "source_reviewers": [],
    "demand": [],
    "eligibility": [
      {"type": "eligibility", "capacity": 1, "manuscript_id": "m1", "reviewer_id": "r1", "track": null}
    ],
    "track_capacity": [],
    "reviewer_capacity": [],
    "max_assignable": 1,
    "shortfall": 1
  }
}
```

- 赛道限额为 0 时，赛道节点在源侧、评审人在汇侧，`track_capacity` 中列出容量 0 的饱和边。

**增量重排模式**下，合法锁定关系先固化（扣减稿件剩余需求、评审剩余总容量与剩余
赛道限额），瓶颈割建在该「扣除锁定占用后的残量网络」上：各边 `capacity` 为扣除
锁定后的**剩余**容量（因此可能为 0），割容量等于剩余网络最大流；`max_assignable`
为**合法锁定名额 + 割容量**（即含锁定关系在内全局至多可满足的名额数）。锁定关系
校验失败时不进行求解，`bottleneck_cut` 为 `null`（只返回 `locked_conflicts`）。

### 增量重排模式（可选）

请求携带 `previous_assignments` 和/或 `locked_assignments` 即进入增量重排模式
（两者都不携带时行为与上述普通模式完全一致）：

```json
{
  "required_reviewers": 1,
  "manuscripts": [
    {"id": "m1", "authors": ["alice"], "topics": ["ml"], "required_reviewers": 2},
    {"id": "m2", "authors": ["bob"], "topics": ["db"]}
  ],
  "reviewers": [
    {"id": "r1", "topics": ["ml"], "capacity": 2},
    {"id": "r2", "topics": ["ml", "db"], "capacity": 1},
    {"id": "r3", "topics": ["db"], "capacity": 1}
  ],
  "previous_assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1", "r2"]},
    {"manuscript_id": "m2", "reviewer_ids": ["r2"]}
  ],
  "locked_assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1"]}
  ]
}
```

字段规则：

- `previous_assignments`：上一轮分配结果。求解在保证全局可行的前提下尽量复用这些
  关系，使**新增 + 移除的关系总数最少**；引用已不存在的稿件/评审人、或当前已不满足
  资格/约束的关系自动视为无法复用（计为移除），不影响求解。历史关系仅在赛道限额与
  总容量两级容量均允许时才会被复用。
- `locked_assignments`：必须原样保留的锁定关系。每条锁定关系必须同时满足：
  - 稿件需求：同一稿件的锁定人数不超过其 `required_reviewers`；
  - 评审资格：与普通分配完全相同的主题匹配与冲突回避校验；
  - 容量：同一评审人的锁定篇数不超过其 `capacity`，且同一赛道上的锁定篇数
    不超过其 `track_capacities` 中对应赛道的限额。

  任一锁定关系违反上述约束，即整体判定不可行（`200` + `status: "infeasible"`），
  返回结构化的 `locked_conflicts` 且**不输出任何部分分配**。
- 两个字段均为「稿件 -> 评审人列表」的条目数组；同一稿件可拆多条，重复关系自动去重。

上述例子的可行响应（`m1->r1` 被锁定保留，`m1->r2` 复用上一轮；`r2` 容量被 `m1` 占满，
`m2` 被迫由 `r2` 调整为 `r3`，共 1 新增 1 移除，已是最少改动）：

```json
{
  "status": "feasible",
  "total_required": 3,
  "total_assigned": 3,
  "assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1", "r2"]},
    {"manuscript_id": "m2", "reviewer_ids": ["r3"]}
  ],
  "unsatisfied": null,
  "shared_pool": null,
  "locked_conflicts": null,
  "bottleneck_cut": null
}
```

锁定冲突时的响应（`200`，`assignments` 为 `null`，不输出部分结果）：

```json
{
  "status": "infeasible",
  "total_required": 3,
  "total_assigned": 0,
  "assignments": null,
  "unsatisfied": null,
  "shared_pool": null,
  "locked_conflicts": [
    {
      "type": "reviewer_capacity_exceeded",
      "manuscript_id": null,
      "reviewer_id": "r1",
      "locked": 3,
      "limit": 2,
      "track": null,
      "detail": "评审人 r1 被锁定 3 篇稿件，超出其容量 2"
    }
  ],
  "bottleneck_cut": null
}
```

`locked_conflicts[].type` 取值：

| type | 含义 |
| --- | --- |
| `unknown_manuscript` / `unknown_reviewer` | 锁定关系引用了不存在的稿件 / 评审人 |
| `self_conflict` | 评审人是稿件作者（自稿冲突） |
| `author_conflict` | 评审人与稿件作者存在利益冲突 |
| `manuscript_conflict` | 评审人已声明回避该稿件 |
| `topic_mismatch` | 评审人主题能力与稿件主题不匹配 |
| `reviewer_capacity_exceeded` | 评审人锁定篇数超出总容量（`locked` / `limit` 给出数值） |
| `reviewer_track_capacity_exceeded` | 评审人在某赛道的锁定篇数超出赛道限额（`track` / `locked` / `limit` 给出赛道与数值） |
| `manuscript_requirement_exceeded` | 稿件锁定人数超出所需评审数（`locked` / `limit` 给出数值） |

增量模式下若锁定关系全部合法但整体仍不可行（容量/候选不足），响应与普通模式
不可行情形相同：`unsatisfied` + `shared_pool` 缺口诊断，`locked_conflicts` 为
`null`，并同样给出 `bottleneck_cut` 最小割证明（割边容量为扣除合法锁定占用后的
剩余容量，`max_assignable` 含已固化的锁定名额）。

### 分赛道容量（可选）

稿件可声明所属赛道 `track`，评审人可通过 `track_capacities` 声明分赛道限额。
映射中出现的赛道按对应非负整数限额，未列出的赛道只受总容量约束；赛道限额与
总容量在**同一个流网络中联立求解**（普通最大流与增量最小费用流一致），而非
分赛道独立求解后拼接：

```json
{
  "required_reviewers": 1,
  "manuscripts": [
    {"id": "m1", "authors": ["alice"], "topics": ["ml"], "track": "poster"},
    {"id": "m2", "authors": ["bob"], "topics": ["ml"], "track": "poster"},
    {"id": "m3", "authors": ["carol"], "topics": ["ml"], "track": "oral"}
  ],
  "reviewers": [
    {"id": "r1", "topics": ["ml"], "capacity": 3, "track_capacities": {"poster": 1}},
    {"id": "r2", "topics": ["ml"], "capacity": 1}
  ]
}
```

`r1` 总容量 3 但 poster 赛道限额 1：两篇 poster 至多一篇分给 `r1`，另一篇只能给
`r2`；oral 赛道未列入 `r1` 的映射，只受其总容量约束。可行响应（`200`）：

```json
{
  "status": "feasible",
  "total_required": 3,
  "total_assigned": 3,
  "assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r2"]},
    {"manuscript_id": "m2", "reviewer_ids": ["r1"]},
    {"manuscript_id": "m3", "reviewer_ids": ["r1"]}
  ],
  "unsatisfied": null,
  "shared_pool": null,
  "locked_conflicts": null,
  "bottleneck_cut": null
}
```

增量重排模式下，锁定关系同样占用赛道限额：若 `locked_assignments` 使某评审人
在某赛道的锁定篇数超出对应限额，整体返回 `infeasible` 与结构化
`locked_conflicts`（类型 `reviewer_track_capacity_exceeded`），不输出部分结果。
对上述请求追加锁定后：

```json
{
  "locked_assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
    {"manuscript_id": "m2", "reviewer_ids": ["r1"]}
  ]
}
```

响应（`200`，`assignments` 为 `null`）：

```json
{
  "status": "infeasible",
  "total_required": 3,
  "total_assigned": 0,
  "assignments": null,
  "unsatisfied": null,
  "shared_pool": null,
  "locked_conflicts": [
    {
      "type": "reviewer_track_capacity_exceeded",
      "manuscript_id": null,
      "reviewer_id": "r1",
      "locked": 2,
      "limit": 1,
      "track": "poster",
      "detail": "评审人 r1 在赛道 poster 被锁定 2 篇稿件，超出其赛道限额 1"
    }
  ],
  "bottleneck_cut": null
}
```

### 示例调用

```bash
curl -s http://localhost:8000/health

curl -s -X POST http://localhost:8000/assign \
  -H 'Content-Type: application/json' \
  -d '{
        "required_reviewers": 2,
        "manuscripts": [
          {"id": "m1", "authors": ["alice"], "topics": ["ml"]},
          {"id": "m2", "authors": ["bob"],   "topics": ["db"]}
        ],
        "reviewers": [
          {"id": "r1", "topics": ["ml"], "capacity": 2},
          {"id": "r2", "topics": ["ml", "db"], "capacity": 2},
          {"id": "r3", "topics": ["db"], "capacity": 1}
        ]
      }'

# 增量重排：锁定 m1->r1，并在全局可行前提下最少改动地复用上一轮分配
curl -s -X POST http://localhost:8000/assign \
  -H 'Content-Type: application/json' \
  -d '{
        "required_reviewers": 1,
        "manuscripts": [
          {"id": "m1", "authors": ["alice"], "topics": ["ml"], "required_reviewers": 2},
          {"id": "m2", "authors": ["bob"],   "topics": ["db"]}
        ],
        "reviewers": [
          {"id": "r1", "topics": ["ml"], "capacity": 2},
          {"id": "r2", "topics": ["ml", "db"], "capacity": 1},
          {"id": "r3", "topics": ["db"], "capacity": 1}
        ],
        "previous_assignments": [
          {"manuscript_id": "m1", "reviewer_ids": ["r1", "r2"]},
          {"manuscript_id": "m2", "reviewer_ids": ["r2"]}
        ],
        "locked_assignments": [
          {"manuscript_id": "m1", "reviewer_ids": ["r1"]}
        ]
      }'

# 分赛道容量：r1 的 poster 赛道限额 1，与总容量 3 同时生效
curl -s -X POST http://localhost:8000/assign \
  -H 'Content-Type: application/json' \
  -d '{
        "required_reviewers": 1,
        "manuscripts": [
          {"id": "m1", "authors": ["alice"], "topics": ["ml"], "track": "poster"},
          {"id": "m2", "authors": ["bob"],   "topics": ["ml"], "track": "poster"},
          {"id": "m3", "authors": ["carol"], "topics": ["ml"], "track": "oral"}
        ],
        "reviewers": [
          {"id": "r1", "topics": ["ml"], "capacity": 3, "track_capacities": {"poster": 1}},
          {"id": "r2", "topics": ["ml"], "capacity": 1}
        ]
      }'
```

## 算法与确定性

- 建图：`源 -> 稿件（容量=所需评审数）-> 评审人（容量=1，仅合格）-> 汇（容量=评审容量）`；
  最大流等于总需求即存在满足全部约束的分配，取其流即为解。
- 分赛道容量：稿件赛道出现在评审人 `track_capacities` 中时，稿件 -> 评审人 的边改经
  「(评审人, 赛道)」限额节点（`稿件 -> (评审人,赛道) -> 评审人`，中间边容量=赛道限额），
  赛道限额与总容量在同一流网络中联立求解；未列入映射的赛道仍走直边，只受总容量约束。
- 全局求解保证整体可行（逐稿贪心无法保证，例如 m1 可选 r1/r2、m2 仅可选 r1 且容量均为 1 时，
  贪心会失败而全局解为 m1→r2、m2→r1）。
- 增量重排：锁定关系先逐一校验稿件需求、评审资格、总容量与分赛道限额（任一冲突即整体
  不可行，返回结构化 `locked_conflicts`），合法锁定对固化后扣减稿件剩余需求、评审剩余
  总容量与剩余赛道限额；其余名额用最小费用最大流（SSP + 势函数）求解——复用
  `previous_assignments` 的边费用为 0、新增边费用为 1，流量固定时费用最小即复用最多，
  亦即新增 + 移除关系总数最少；复用边同样流经赛道限额节点，历史关系仅在两级容量
  均允许时复用。
- 不可行证明：求解结束后在残量网络上从源点 BFS 得到源侧集合，跨割的正向边必然全部
  饱和；由最大流最小割定理，按 `demand` / `eligibility` / `track_capacity` /
  `reviewer_capacity` 分类列出的跨割边容量之和即割容量 = `max_assignable`，
  `shortfall` 为总需求与之差。增量模式下该割建在扣除合法锁定占用后的残量网络上，
  `max_assignable` 再加上已固化的锁定名额；锁定校验失败时不给该证明。
- 所有节点按 ID 排序后建图，BFS / Dijkstra 均按固定邻接顺序与 (费用, 节点下标) 字典序
  执行，算法无随机性：相同输入（含不同的列表顺序）产生逐字节相同的输出，
  同成本解由 ID 顺序确定性地破除（瓶颈割的节点与边同样按业务 ID 确定排序）。

## 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                 # 运行测试
uvicorn app.main:app --reload --port 8000   # 本地起服务
```

## 项目结构

```
app/
  main.py      # FastAPI 入口：/health、/assign
  models.py    # 请求/响应模型与校验
  solver.py    # 分配求解器：普通模式全局最大流 + 增量模式最小费用最大流
tests/
  test_assign.py
Dockerfile
docker-compose.yml
requirements.txt
requirements-dev.txt
```
