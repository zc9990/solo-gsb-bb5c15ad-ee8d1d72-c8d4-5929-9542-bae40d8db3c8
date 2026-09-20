"""全局流量匹配求解器。

将评审分配问题整体规约为二部 b-匹配，用最大流（Edmonds-Karp）一次求解，
而不是逐稿贪心：

    源点 --(所需评审数)--> 稿件 --(1, 仅当可评审)--> 评审人 --(容量)--> 汇点

- 稿件 -> 评审人 的边仅当：主题能力匹配、无利益冲突、非自稿。
- 最大流 == 总需求 时存在满足全部约束的分配；否则判定不可行并给出缺口诊断。
- 不可行时在求解结束的残量网络上沿源点可达集构造全局最小割（bottleneck_cut）：
  跨割饱和边按 demand（稿件需求）、eligibility（评审资格）、track_capacity
  （赛道限额）、reviewer_capacity（评审总容量）四类列出，并给出割容量
  max_assignable 与缺口 shortfall，作为「至多可满足这么多」的严格证明。
- 所有节点按 ID 排序后建图，BFS 按固定邻接顺序遍历，算法完全确定：
  相同输入（甚至不同的列表顺序）必然产生完全相同的分配结果。

分赛道容量（稿件携带 track、评审人声明 track_capacities 时）在稿件与评审人
之间插入一层「(评审人, 赛道)」限额节点：

    稿件 --(1)--> (评审人, 赛道) --(赛道限额)--> 评审人 --(总容量)--> 汇点

赛道受限稿件的流量同时经过赛道限额边与总容量边，两级约束在同一个流网络中
联立求解（而非分赛道独立求解后拼接）；赛道未列入该评审人映射、或稿件不携带
track 时仍走 稿件->评审人 直边，只受总容量约束。

增量重排模式（请求携带 previous_assignments / locked_assignments 时）：

- 锁定关系先逐一校验稿件需求、评审资格、总容量与分赛道限额，任一冲突即整体
  不可行，返回结构化冲突原因，不输出任何部分分配（此情形 bottleneck_cut 为 None）；
- 合法锁定关系固化为必留边（扣减稿件剩余需求、评审剩余总容量与剩余赛道限额），
  其余名额用最小费用最大流求解：复用 previous_assignments 的边费用为 0、
  新增边费用为 1。流量固定时费用最小 <=> 复用上一轮的边最多 <=> 新增+移除的
  关系总数最少；复用边同样流经赛道限额节点，历史关系仅在两级容量均允许时复用；
- 锁全部合法但剩余需求仍不可满足时，瓶颈割建在「扣除锁定占用后的残量网络」上：
  跨割边容量为扣除锁定后的剩余容量，割容量等于剩余网络最大流；max_assignable
  为合法锁定名额与割容量之和，证明含锁定关系在内全局至多可满足的名额数；
- 增广路按 (费用, 节点下标) 字典序选取，节点下标来自 ID 排序：
  同成本解由 ID 顺序确定性地破除，输出完全确定。
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ManuscriptSpec:
    id: str
    authors: tuple[str, ...]
    topics: tuple[str, ...]
    required_reviewers: int
    track: str | None = None


@dataclass(frozen=True)
class ReviewerSpec:
    id: str
    topics: tuple[str, ...]
    capacity: int
    conflicted_authors: tuple[str, ...]
    conflicted_manuscripts: tuple[str, ...]
    # (赛道, 限额) 对，按赛道名排序；缺省为空表示不做分赛道限制
    track_capacities: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class UnsatisfiedManuscript:
    manuscript_id: str
    required: int
    assigned: int
    missing: int
    eligible_reviewers: int
    eligible_remaining_capacity: int
    candidate_gap: int
    capacity_gap: int


# 锁定关系冲突类型：
#   unknown_manuscript / unknown_reviewer  —— 引用了不存在的稿件 / 评审人
#   self_conflict / author_conflict / manuscript_conflict / topic_mismatch —— 评审资格
#   reviewer_capacity_exceeded             —— 锁定数超出评审总容量
#   reviewer_track_capacity_exceeded       —— 锁定数超出评审某赛道限额（track 给出赛道）
#   manuscript_requirement_exceeded        —— 锁定数超出稿件所需评审数
@dataclass(frozen=True)
class LockedConflict:
    type: str
    manuscript_id: str | None
    reviewer_id: str | None
    locked: int | None
    limit: int | None
    detail: str
    track: str | None = None


@dataclass(frozen=True)
class BottleneckCutEdge:
    """一条跨越瓶颈割 (源侧 -> 汇侧) 的饱和边。

    manuscript_id / reviewer_id / track 按边类型携带：
      demand                  -> manuscript_id
      eligibility             -> manuscript_id + reviewer_id（+ track，走赛道节点时）
      track_capacity          -> reviewer_id + track
      reviewer_capacity       -> reviewer_id
    capacity 为建图容量（增量模式下为扣除合法锁定占用后的剩余容量）。
    """

    type: str
    capacity: int
    manuscript_id: str | None = None
    reviewer_id: str | None = None
    track: str | None = None


@dataclass(frozen=True)
class BottleneckCut:
    """最大流结束后残量网络上的全局最小割证明。

    - source_manuscripts / source_reviewer_tracks / source_reviewers：
      残量网络中从源点可达（源侧）的业务节点，按业务 ID 排序；
    - demand / eligibility / track_capacity / reviewer_capacity：
      跨割（源侧 -> 汇侧）饱和边，按业务 ID 确定排序；
    - max_assignable：割容量，即在全部约束下至多可满足的名额数
      （增量模式含已固化的合法锁定名额）；
    - shortfall：总需求 - max_assignable。
    """

    source_manuscripts: tuple[str, ...]
    source_reviewer_tracks: tuple[tuple[str, str], ...]
    source_reviewers: tuple[str, ...]
    demand: tuple[BottleneckCutEdge, ...]
    eligibility: tuple[BottleneckCutEdge, ...]
    track_capacity: tuple[BottleneckCutEdge, ...]
    reviewer_capacity: tuple[BottleneckCutEdge, ...]
    max_assignable: int
    shortfall: int


@dataclass(frozen=True)
class SolveResult:
    feasible: bool
    # manuscript_id -> 已排序的 reviewer id 元组；不可行时为空（不输出部分结果）
    assignments: dict[str, tuple[str, ...]]
    total_required: int
    total_assigned: int
    unsatisfied: tuple[UnsatisfiedManuscript, ...]
    shared_pool: dict | None
    # 锁定关系冲突（增量重排模式）；无冲突或普通模式时为空
    locked_conflicts: tuple[LockedConflict, ...] = ()
    # 合法但需求无法全部满足时的全局最小割证明；可行或锁定校验失败时为 None
    bottleneck_cut: BottleneckCut | None = None


class _MaxFlow:
    """Edmonds-Karp 最大流。邻接表按插入顺序遍历，建图顺序确定则结果确定。"""

    def __init__(self, n: int) -> None:
        self.n = n
        self.cap = [[0] * n for _ in range(n)]
        self.adj: list[list[int]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, c: int) -> None:
        if c <= 0:
            return
        self.adj[u].append(v)
        self.adj[v].append(u)
        self.cap[u][v] += c

    def max_flow(self, s: int, t: int) -> int:
        flow = 0
        while True:
            parent = [-1] * self.n
            parent[s] = s
            q = deque([s])
            while q and parent[t] == -1:
                u = q.popleft()
                for v in self.adj[u]:
                    if parent[v] == -1 and self.cap[u][v] > 0:
                        parent[v] = u
                        q.append(v)
            if parent[t] == -1:
                return flow
            bottleneck = None
            v = t
            while v != s:
                u = parent[v]
                c = self.cap[u][v]
                bottleneck = c if bottleneck is None else min(bottleneck, c)
                v = u
            v = t
            while v != s:
                u = parent[v]
                self.cap[u][v] -= bottleneck
                self.cap[v][u] += bottleneck
                v = u
            flow += bottleneck


class _MinCostMaxFlow:
    """SSP 最小费用最大流（初始费用非负，Dijkstra + 势函数）。

    堆元素为 (费用, 顶点下标)，松弛严格小于才更新：同费用路径取下标最小者，
    建图顺序确定则每次增广路确定，整体结果完全确定。
    """

    def __init__(self, n: int) -> None:
        self.n = n
        # 边: [to, 残余容量, 单位费用, 反向边在 g[to] 中的下标]
        self.g: list[list[list[int]]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> int:
        """返回正向边在 g[u] 中的下标（容量为 0 的边同样建边，便于诊断读取）。"""
        fwd = [v, cap, cost, len(self.g[v])]
        rev = [u, 0, -cost, len(self.g[u])]
        self.g[u].append(fwd)
        self.g[v].append(rev)
        return len(self.g[u]) - 1

    def residual(self, u: int, ei: int) -> int:
        """正向边 (u, ei) 的残余容量。"""
        return self.g[u][ei][1]

    def outgoing(self, u: int) -> list[int]:
        """节点 u 的全部邻接点（含正/反向边，按邻接表顺序，供残量 BFS 使用）。"""
        return [e[0] for e in self.g[u]]

    def residual_to(self, u: int, v: int) -> int:
        """u -> v 的残余容量之和（平行边累加；无边时为 0）。"""
        return sum(e[1] for e in self.g[u] if e[0] == v)

    def flow_on(self, u: int, ei: int) -> int:
        """正向边 (u, ei) 上的流量（即反向边的残余容量）。"""
        e = self.g[u][ei]
        return self.g[e[0]][e[3]][1]

    def min_cost_max_flow(self, s: int, t: int) -> tuple[int, int]:
        n = self.n
        g = self.g
        potential = [0] * n
        flow = 0
        cost = 0
        INF = 1 << 60
        while True:
            dist = [INF] * n
            prev_v = [-1] * n
            prev_e = [-1] * n
            dist[s] = 0
            pq = [(0, s)]
            while pq:
                d, u = heapq.heappop(pq)
                if d > dist[u]:
                    continue
                for i, e in enumerate(g[u]):
                    if e[1] <= 0:
                        continue
                    v = e[0]
                    nd = d + e[2] + potential[u] - potential[v]
                    if nd < dist[v]:
                        dist[v] = nd
                        prev_v[v] = u
                        prev_e[v] = i
                        heapq.heappush(pq, (nd, v))
            if dist[t] == INF:
                return flow, cost
            for v in range(n):
                if dist[v] < INF:
                    potential[v] += dist[v]
            d = INF
            v = t
            while v != s:
                e = g[prev_v[v]][prev_e[v]]
                if e[1] < d:
                    d = e[1]
                v = prev_v[v]
            v = t
            while v != s:
                e = g[prev_v[v]][prev_e[v]]
                e[1] -= d
                g[v][e[3]][1] += d
                v = prev_v[v]
            flow += d
            cost += d * potential[t]


def _build_bottleneck_cut(
    ms: list[ManuscriptSpec],
    rs: list[ReviewerSpec],
    *,
    m_count: int,
    r_count: int,
    track_nodes: list[dict[str, int]],
    src: int,
    snk: int,
    residual_capacity,
    outgoing,
    demand_edges: list[tuple[int, int]],
    eligibility_edges: list[tuple[int, int, int, str | None]],
    track_edges: list[tuple[int, str, int, int]],
    reviewer_edges: list[tuple[int, int]],
    reduced_flow: int,
    locked_total: int,
    total_required: int,
) -> BottleneckCut:
    """在求解结束的残量网络上构造全局最小割（瓶颈割）证明。

    网络节点布局：SRC | 稿件 | 评审人 | (评审人, 赛道) 限额节点 | SNK。
    从源点沿残量容量 > 0 的边 BFS 得到源侧集合 S：由最大流最小割定理，
    全部 S -> 非 S 的正向边（必为饱和边）容量之和即割容量 = 最大流。

    边清单（业务 ID 均按建图时的 ID 排序下标给出）：
      demand_edges       : (稿件下标, 源->稿件边容量)
      eligibility_edges  : (稿件下标, 评审下标, 边容量, 经过的赛道名或 None)
      track_edges        : (评审下标, 赛道, 限额节点, 限额边容量)
      reviewer_edges     : (评审下标, 评审->汇边容量)
    reduced_flow 为本次网络求得的流（增量模式下为扣除锁定后的剩余网络流）；
    locked_total 为已固化的合法锁定名额（普通模式为 0）。
    """

    def m_node(i: int) -> int:
        return 1 + i

    def r_node(j: int) -> int:
        return 1 + m_count + j

    # ---- 残量网络上的可达集（源侧） ----
    reachable = [False] * (snk + 1)
    reachable[src] = True
    q = deque([src])
    while q:
        u = q.popleft()
        for v in outgoing(u):
            if not reachable[v] and residual_capacity(u, v) > 0:
                reachable[v] = True
                q.append(v)

    def crossed(u: int, v: int) -> bool:
        return reachable[u] and not reachable[v]

    demand: list[BottleneckCutEdge] = []
    for i, cap in demand_edges:
        if crossed(src, m_node(i)):
            demand.append(BottleneckCutEdge(
                type="demand", capacity=cap, manuscript_id=ms[i].id,
            ))

    eligibility: list[BottleneckCutEdge] = []
    for i, j, cap, track in eligibility_edges:
        target = track_nodes[j][track] if track is not None else r_node(j)
        if crossed(m_node(i), target):
            eligibility.append(BottleneckCutEdge(
                type="eligibility", capacity=cap,
                manuscript_id=ms[i].id, reviewer_id=rs[j].id, track=track,
            ))

    track_cut: list[BottleneckCutEdge] = []
    for j, track, node, cap in track_edges:
        if crossed(node, r_node(j)):
            track_cut.append(BottleneckCutEdge(
                type="track_capacity", capacity=cap,
                reviewer_id=rs[j].id, track=track,
            ))

    reviewer_cut: list[BottleneckCutEdge] = []
    for j, cap in reviewer_edges:
        if crossed(r_node(j), snk):
            reviewer_cut.append(BottleneckCutEdge(
                type="reviewer_capacity", capacity=cap, reviewer_id=rs[j].id,
            ))

    # 业务 ID 确定排序（赛道节点为 (reviewer_id, track)）
    demand.sort(key=lambda e: e.manuscript_id or "")
    eligibility.sort(key=lambda e: (
        e.manuscript_id or "", e.reviewer_id or "", e.track or ""
    ))
    track_cut.sort(key=lambda e: (e.reviewer_id or "", e.track or ""))
    reviewer_cut.sort(key=lambda e: e.reviewer_id or "")

    source_manuscripts = tuple(
        ms[i].id for i in range(m_count) if reachable[m_node(i)]
    )
    source_reviewers = tuple(
        rs[j].id for j in range(r_count) if reachable[r_node(j)]
    )
    source_reviewer_tracks = tuple(sorted(
        (rs[j].id, track)
        for j in range(r_count)
        for track, node in track_nodes[j].items()
        if reachable[node]
    ))

    cut_capacity = sum(
        e.capacity
        for edges in (demand, eligibility, track_cut, reviewer_cut)
        for e in edges
    )
    # 最大流最小割：跨割正向边容量之和必等于本次网络的最大流
    assert cut_capacity == reduced_flow

    max_assignable = locked_total + reduced_flow
    return BottleneckCut(
        source_manuscripts=source_manuscripts,
        source_reviewer_tracks=source_reviewer_tracks,
        source_reviewers=source_reviewers,
        demand=tuple(demand),
        eligibility=tuple(eligibility),
        track_capacity=tuple(track_cut),
        reviewer_capacity=tuple(reviewer_cut),
        max_assignable=max_assignable,
        shortfall=total_required - max_assignable,
    )


def _eligible_reviewers(m: ManuscriptSpec, reviewers: list[ReviewerSpec]) -> list[int]:
    """返回可评审该稿件的评审人下标列表（按评审人排序后的顺序）。"""
    m_topics = set(m.topics)
    authors = set(m.authors)
    out: list[int] = []
    for j, r in enumerate(reviewers):
        if r.id in authors:
            continue  # 自稿冲突：评审人是稿件作者
        if authors.intersection(r.conflicted_authors):
            continue  # 利益冲突：与稿件作者冲突
        if m.id in r.conflicted_manuscripts:
            continue  # 利益冲突：直接声明回避该稿件
        if m_topics and not m_topics.intersection(r.topics):
            continue  # 主题能力不匹配
        out.append(j)
    return out


_REASON_DETAIL = {
    "self_conflict": "评审人是稿件作者（自稿冲突）",
    "author_conflict": "评审人与稿件作者存在利益冲突",
    "manuscript_conflict": "评审人已声明回避该稿件",
    "topic_mismatch": "评审人主题能力与稿件主题不匹配",
}


def _pair_ineligibility_reasons(m: ManuscriptSpec, r: ReviewerSpec) -> tuple[str, ...]:
    """返回 (m, r) 不满足评审资格的全部原因；空元组表示合格。

    判定条件必须与 _eligible_reviewers 保持一致。
    """
    authors = set(m.authors)
    reasons: list[str] = []
    if r.id in authors:
        reasons.append("self_conflict")
    if authors.intersection(r.conflicted_authors):
        reasons.append("author_conflict")
    if m.id in r.conflicted_manuscripts:
        reasons.append("manuscript_conflict")
    if m.topics and not set(m.topics).intersection(r.topics):
        reasons.append("topic_mismatch")
    return tuple(reasons)


def solve_assignment(
    manuscripts: list[ManuscriptSpec],
    reviewers: list[ReviewerSpec],
    previous: frozenset[tuple[str, str]] | None = None,
    locked: frozenset[tuple[str, str]] | None = None,
) -> SolveResult:
    """求解分配。

    previous / locked 为 (manuscript_id, reviewer_id) 对集合；两者都缺省时
    走普通模式（行为与历史版本完全一致），任一提供即进入增量重排模式。
    """
    # 按 ID 排序，保证建图顺序与输入列表顺序无关 -> 输出稳定
    ms = sorted(manuscripts, key=lambda m: m.id)
    rs = sorted(reviewers, key=lambda r: r.id)
    if previous is None and locked is None:
        return _solve_normal(ms, rs)
    return _solve_incremental(
        ms,
        rs,
        previous if previous is not None else frozenset(),
        locked if locked is not None else frozenset(),
    )


def _track_node_layout(
    m_count: int, rs: list[ReviewerSpec]
) -> tuple[list[dict[str, int]], int]:
    """为每个评审人声明的赛道限额分配图节点（紧跟评审人节点之后）。

    返回 (评审人下标 -> {赛道: 节点下标}, 下一可用节点下标)。评审人已按 ID
    排序、赛道按名称排序：节点编号与输入列表顺序无关，保证结果确定。
    """
    nxt = 1 + m_count + len(rs)
    layout: list[dict[str, int]] = []
    for r in rs:
        nodes: dict[str, int] = {}
        for track, _cap in sorted(r.track_capacities):
            nodes[track] = nxt
            nxt += 1
        layout.append(nodes)
    return layout, nxt


def _solve_normal(ms: list[ManuscriptSpec], rs: list[ReviewerSpec]) -> SolveResult:
    m_count, r_count = len(ms), len(rs)

    SRC = 0
    # 节点布局：SRC | 稿件 | 评审人 | (评审人, 赛道) 限额节点 | SNK
    track_nodes, SNK = _track_node_layout(m_count, rs)
    track_caps = [dict(r.track_capacities) for r in rs]

    def m_node(i: int) -> int:
        return 1 + i

    def r_node(j: int) -> int:
        return 1 + m_count + j

    def mr_target(i: int, j: int) -> int:
        """稿件 i -> 评审人 j 的边指向：赛道受该评审人限额约束时走限额节点。"""
        track = ms[i].track
        if track is not None:
            node = track_nodes[j].get(track)
            if node is not None:
                return node
        return r_node(j)

    eligible = [_eligible_reviewers(m, rs) for m in ms]

    mf = _MaxFlow(SNK + 1)
    demand_edges: list[tuple[int, int]] = []  # (稿件下标, 源边容量)
    for i, m in enumerate(ms):
        mf.add_edge(SRC, m_node(i), m.required_reviewers)
        demand_edges.append((i, m.required_reviewers))
    # (稿件下标, 评审下标, 容量, 经过的赛道名或 None)
    eligibility_edges: list[tuple[int, int, int, str | None]] = []
    for i in range(m_count):
        for j in eligible[i]:
            target = mr_target(i, j)
            mf.add_edge(m_node(i), target, 1)
            track = None
            if target != r_node(j):
                # target 即该 (评审人, 赛道) 限额节点，反查赛道名
                track = next(t for t, node in track_nodes[j].items() if node == target)
            eligibility_edges.append((i, j, 1, track))
    # (评审下标, 赛道, 限额节点, 容量)
    track_edges: list[tuple[int, str, int, int]] = []
    for j, r in enumerate(rs):
        for track, node in track_nodes[j].items():
            cap = track_caps[j][track]
            mf.add_edge(node, r_node(j), cap)
            track_edges.append((j, track, node, cap))
    reviewer_edges: list[tuple[int, int]] = []  # (评审下标, 容量)
    for j, r in enumerate(rs):
        mf.add_edge(r_node(j), SNK, r.capacity)
        reviewer_edges.append((j, r.capacity))

    total_required = sum(m.required_reviewers for m in ms)
    total_assigned = mf.max_flow(SRC, SNK)

    # 提取分配：m->r（或 m->(r,赛道)）原始容量为 1，反向残余容量即该边流量
    flow_assignment: dict[str, tuple[str, ...]] = {}
    for i, m in enumerate(ms):
        picked = tuple(
            sorted(
                rs[j].id
                for j in eligible[i]
                if mf.cap[mr_target(i, j)][m_node(i)] > 0
            )
        )
        flow_assignment[m.id] = picked

    if total_assigned == total_required:
        return SolveResult(
            feasible=True,
            assignments=flow_assignment,
            total_required=total_required,
            total_assigned=total_assigned,
            unsatisfied=(),
            shared_pool=None,
        )

    # ---- 不可行：诊断缺口，不输出任何部分分配 ----
    def remaining_capacity(j: int, track: str | None) -> int:
        """评审人 j 对该赛道稿件的剩余可接量：总剩余与赛道剩余取小。"""
        remaining = mf.cap[r_node(j)][SNK]
        if track is not None:
            node = track_nodes[j].get(track)
            if node is not None:
                remaining = min(remaining, mf.cap[node][r_node(j)])
        return remaining

    unsatisfied: list[UnsatisfiedManuscript] = []
    unsat_idx: list[int] = []
    for i, m in enumerate(ms):
        assigned = len(flow_assignment[m.id])
        if assigned >= m.required_reviewers:
            continue
        missing = m.required_reviewers - assigned
        elig = eligible[i]
        remaining = sum(remaining_capacity(j, m.track) for j in elig)
        unsatisfied.append(
            UnsatisfiedManuscript(
                manuscript_id=m.id,
                required=m.required_reviewers,
                assigned=assigned,
                missing=missing,
                eligible_reviewers=len(elig),
                eligible_remaining_capacity=remaining,
                # 候选缺口：合格候选人数本身不足（容量再大也不够）
                candidate_gap=max(0, m.required_reviewers - len(elig)),
                # 容量缺口：候选存在但剩余容量不足
                capacity_gap=max(0, missing - remaining),
            )
        )
        unsat_idx.append(i)

    # 所有未满足稿件共享的候选池：解释“缺口到底缺在哪”
    pool = sorted({j for i in unsat_idx for j in eligible[i]})
    shared_pool = {
        "manuscript_ids": [ms[i].id for i in unsat_idx],
        "reviewer_ids": [rs[j].id for j in pool],
        "missing_total": sum(u.missing for u in unsatisfied),
        "pool_remaining_capacity": sum(mf.cap[r_node(j)][SNK] for j in pool),
        "pool_total_capacity": sum(rs[j].capacity for j in pool),
    }

    bottleneck_cut = _build_bottleneck_cut(
        ms,
        rs,
        m_count=m_count,
        r_count=r_count,
        track_nodes=track_nodes,
        src=SRC,
        snk=SNK,
        residual_capacity=lambda u, v: mf.cap[u][v],
        outgoing=lambda u: mf.adj[u],
        demand_edges=demand_edges,
        eligibility_edges=eligibility_edges,
        track_edges=track_edges,
        reviewer_edges=reviewer_edges,
        reduced_flow=total_assigned,
        locked_total=0,
        total_required=total_required,
    )

    return SolveResult(
        feasible=False,
        assignments={},
        total_required=total_required,
        total_assigned=total_assigned,
        unsatisfied=tuple(unsatisfied),
        shared_pool=shared_pool,
        bottleneck_cut=bottleneck_cut,
    )


def _check_locked(
    ms: list[ManuscriptSpec],
    rs: list[ReviewerSpec],
    locked: frozenset[tuple[str, str]],
) -> tuple[dict[str, tuple[str, ...]], tuple[LockedConflict, ...]]:
    """校验锁定关系：评审资格（逐对）、评审总容量与分赛道限额、稿件需求。

    返回 (每个稿件合法锁定的评审人元组, 冲突列表)；冲突非空即整体不可行。
    """
    m_by_id = {m.id: m for m in ms}
    r_by_id = {r.id: r for r in rs}

    conflicts: list[LockedConflict] = []
    locked_by_m: dict[str, list[str]] = {}
    locked_by_r: dict[str, list[str]] = {}
    for mid, rid in sorted(locked):
        m = m_by_id.get(mid)
        if m is None:
            conflicts.append(LockedConflict(
                type="unknown_manuscript", manuscript_id=mid, reviewer_id=rid,
                locked=None, limit=None,
                detail=f"锁定关系引用了不存在的稿件 {mid}",
            ))
            continue
        r = r_by_id.get(rid)
        if r is None:
            conflicts.append(LockedConflict(
                type="unknown_reviewer", manuscript_id=mid, reviewer_id=rid,
                locked=None, limit=None,
                detail=f"锁定关系引用了不存在的评审人 {rid}",
            ))
            continue
        reasons = _pair_ineligibility_reasons(m, r)
        for reason in reasons:
            conflicts.append(LockedConflict(
                type=reason, manuscript_id=mid, reviewer_id=rid,
                locked=None, limit=None,
                detail=f"锁定关系 {mid} -> {rid} 不满足评审资格：{_REASON_DETAIL[reason]}",
            ))
        if not reasons:
            locked_by_m.setdefault(mid, []).append(rid)
            locked_by_r.setdefault(rid, []).append(mid)

    # 容量与需求校验只针对资格合法的锁定对
    for rid in sorted(locked_by_r):
        r = r_by_id[rid]
        n_locked = len(locked_by_r[rid])
        if n_locked > r.capacity:
            conflicts.append(LockedConflict(
                type="reviewer_capacity_exceeded", manuscript_id=None, reviewer_id=rid,
                locked=n_locked, limit=r.capacity,
                detail=f"评审人 {rid} 被锁定 {n_locked} 篇稿件，超出其容量 {r.capacity}",
            ))
    # 分赛道限额校验：锁定关系同样占用赛道容量
    for rid in sorted(locked_by_r):
        caps = dict(r_by_id[rid].track_capacities)
        if not caps:
            continue
        per_track: dict[str, int] = {}
        for mid in locked_by_r[rid]:
            track = m_by_id[mid].track
            if track is not None and track in caps:
                per_track[track] = per_track.get(track, 0) + 1
        for track in sorted(per_track):
            n_locked = per_track[track]
            limit = caps[track]
            if n_locked > limit:
                conflicts.append(LockedConflict(
                    type="reviewer_track_capacity_exceeded",
                    manuscript_id=None, reviewer_id=rid,
                    locked=n_locked, limit=limit,
                    detail=f"评审人 {rid} 在赛道 {track} 被锁定 {n_locked} 篇稿件，"
                           f"超出其赛道限额 {limit}",
                    track=track,
                ))
    for mid in sorted(locked_by_m):
        m = m_by_id[mid]
        n_locked = len(locked_by_m[mid])
        if n_locked > m.required_reviewers:
            conflicts.append(LockedConflict(
                type="manuscript_requirement_exceeded", manuscript_id=mid, reviewer_id=None,
                locked=n_locked, limit=m.required_reviewers,
                detail=f"稿件 {mid} 锁定 {n_locked} 名评审，超出其所需评审数 "
                       f"{m.required_reviewers}",
            ))

    conflicts.sort(
        key=lambda c: (c.type, c.manuscript_id or "", c.reviewer_id or "", c.track or "")
    )
    locked_map = {mid: tuple(sorted(rids)) for mid, rids in locked_by_m.items()}
    return locked_map, tuple(conflicts)


def _solve_incremental(
    ms: list[ManuscriptSpec],
    rs: list[ReviewerSpec],
    previous: frozenset[tuple[str, str]],
    locked: frozenset[tuple[str, str]],
) -> SolveResult:
    """增量重排：锁定关系必须全部成立；其余名额在全局可行前提下最少改动。"""
    total_required = sum(m.required_reviewers for m in ms)

    # ---- 1. 校验锁定关系：任一冲突即整体不可行，不输出部分分配 ----
    locked_map, conflicts = _check_locked(ms, rs, locked)
    if conflicts:
        return SolveResult(
            feasible=False,
            assignments={},
            total_required=total_required,
            total_assigned=0,
            unsatisfied=(),
            shared_pool=None,
            locked_conflicts=conflicts,
        )

    locked_pairs = {(mid, rid) for mid, rids in locked_map.items() for rid in rids}
    m_by_id = {m.id: m for m in ms}
    locked_count_r: dict[str, int] = {}
    locked_track_count_r: dict[str, dict[str, int]] = {}
    for mid, rid in locked_pairs:
        locked_count_r[rid] = locked_count_r.get(rid, 0) + 1
        track = m_by_id[mid].track
        if track is not None:
            counts = locked_track_count_r.setdefault(rid, {})
            counts[track] = counts.get(track, 0) + 1

    m_count, r_count = len(ms), len(rs)

    SRC = 0
    # 节点布局与普通模式一致：SRC | 稿件 | 评审人 | (评审人, 赛道) 限额节点 | SNK
    track_nodes, SNK = _track_node_layout(m_count, rs)
    track_caps = [dict(r.track_capacities) for r in rs]

    def m_node(i: int) -> int:
        return 1 + i

    def r_node(j: int) -> int:
        return 1 + m_count + j

    def mr_target(i: int, j: int) -> int:
        track = ms[i].track
        if track is not None:
            node = track_nodes[j].get(track)
            if node is not None:
                return node
        return r_node(j)

    eligible = [_eligible_reviewers(m, rs) for m in ms]

    # ---- 2. 锁定对固化后，对剩余需求做最小费用最大流 ----
    # 复用 previous 的边费用 0、新增边费用 1：流量固定时费用最小
    # <=> 复用上一轮的分配最多 <=> 新增 + 移除的关系总数最少。
    # 复用边同样流经 (评审人, 赛道) 限额节点与总容量边：
    # 历史关系仅在两级容量均允许时才会被复用。
    mf = _MinCostMaxFlow(SNK + 1)
    demand_edges: list[tuple[int, int]] = []  # (稿件下标, 扣除锁定后的剩余需求)
    for i, m in enumerate(ms):
        remaining_req = m.required_reviewers - len(locked_map.get(m.id, ()))
        mf.add_edge(SRC, m_node(i), remaining_req, 0)
        demand_edges.append((i, remaining_req))
    mr_edges: list[tuple[int, int, int]] = []  # (稿件下标, 边下标, 评审下标)
    # (稿件下标, 评审下标, 容量, 经过的赛道名或 None)
    eligibility_edges: list[tuple[int, int, int, str | None]] = []
    for i, m in enumerate(ms):
        for j in eligible[i]:
            if (m.id, rs[j].id) in locked_pairs:
                continue  # 锁定对已固化，不再作为候选边
            cost = 0 if (m.id, rs[j].id) in previous else 1
            target = mr_target(i, j)
            ei = mf.add_edge(m_node(i), target, 1, cost)
            mr_edges.append((i, ei, j))
            track = None
            if target != r_node(j):
                track = next(t for t, node in track_nodes[j].items() if node == target)
            eligibility_edges.append((i, j, 1, track))
    track_edge_idx: list[dict[str, int]] = []
    # (评审下标, 赛道, 限额节点, 扣除锁定后的剩余限额)
    track_edges: list[tuple[int, str, int, int]] = []
    for j, r in enumerate(rs):
        locked_tracks = locked_track_count_r.get(r.id, {})
        edges: dict[str, int] = {}
        for track, node in track_nodes[j].items():
            remaining_track = track_caps[j][track] - locked_tracks.get(track, 0)
            edges[track] = mf.add_edge(node, r_node(j), remaining_track, 0)
            track_edges.append((j, track, node, remaining_track))
        track_edge_idx.append(edges)
    r_edge_idx: list[int] = []
    reviewer_edges: list[tuple[int, int]] = []  # (评审下标, 扣除锁定后的剩余总容量)
    for j, r in enumerate(rs):
        remaining_cap = r.capacity - locked_count_r.get(r.id, 0)
        r_edge_idx.append(mf.add_edge(r_node(j), SNK, remaining_cap, 0))
        reviewer_edges.append((j, remaining_cap))

    total_remaining = sum(
        m.required_reviewers - len(locked_map.get(m.id, ())) for m in ms
    )
    flow, _cost = mf.min_cost_max_flow(SRC, SNK)

    # 提取分配：锁定对 + 流量对
    flow_by_m: dict[str, list[str]] = {m.id: [] for m in ms}
    for i, ei, j in mr_edges:
        if mf.flow_on(m_node(i), ei) > 0:
            flow_by_m[ms[i].id].append(rs[j].id)
    assignment: dict[str, tuple[str, ...]] = {}
    for m in ms:
        picked = tuple(sorted(list(locked_map.get(m.id, ())) + flow_by_m[m.id]))
        assignment[m.id] = picked
    total_assigned = sum(len(v) for v in assignment.values())

    if flow == total_remaining:
        return SolveResult(
            feasible=True,
            assignments=assignment,
            total_required=total_required,
            total_assigned=total_assigned,
            unsatisfied=(),
            shared_pool=None,
        )

    # ---- 3. 不可行：诊断缺口（残余容量已扣除锁定占用），不输出部分分配 ----
    def remaining_capacity(j: int, track: str | None) -> int:
        """评审人 j 对该赛道稿件的剩余可接量：总剩余与赛道剩余取小。"""
        remaining = mf.residual(r_node(j), r_edge_idx[j])
        if track is not None:
            ei = track_edge_idx[j].get(track)
            if ei is not None:
                remaining = min(remaining, mf.residual(track_nodes[j][track], ei))
        return remaining

    unsatisfied: list[UnsatisfiedManuscript] = []
    unsat_idx: list[int] = []
    for i, m in enumerate(ms):
        assigned = len(assignment[m.id])
        if assigned >= m.required_reviewers:
            continue
        missing = m.required_reviewers - assigned
        elig = eligible[i]
        remaining = sum(remaining_capacity(j, m.track) for j in elig)
        unsatisfied.append(
            UnsatisfiedManuscript(
                manuscript_id=m.id,
                required=m.required_reviewers,
                assigned=assigned,
                missing=missing,
                eligible_reviewers=len(elig),
                eligible_remaining_capacity=remaining,
                candidate_gap=max(0, m.required_reviewers - len(elig)),
                capacity_gap=max(0, missing - remaining),
            )
        )
        unsat_idx.append(i)

    pool = sorted({j for i in unsat_idx for j in eligible[i]})
    shared_pool = {
        "manuscript_ids": [ms[i].id for i in unsat_idx],
        "reviewer_ids": [rs[j].id for j in pool],
        "missing_total": sum(u.missing for u in unsatisfied),
        "pool_remaining_capacity": sum(
            mf.residual(r_node(j), r_edge_idx[j]) for j in pool
        ),
        "pool_total_capacity": sum(rs[j].capacity for j in pool),
    }

    # 合法锁定名额已固化，瓶颈割建在扣除锁定占用后的残量网络上：
    # 割容量 = 剩余网络最大流，加上锁定名额即全局 max_assignable。
    locked_total = total_required - total_remaining
    bottleneck_cut = _build_bottleneck_cut(
        ms,
        rs,
        m_count=m_count,
        r_count=r_count,
        track_nodes=track_nodes,
        src=SRC,
        snk=SNK,
        residual_capacity=mf.residual_to,
        outgoing=mf.outgoing,
        demand_edges=demand_edges,
        eligibility_edges=eligibility_edges,
        track_edges=track_edges,
        reviewer_edges=reviewer_edges,
        reduced_flow=flow,
        locked_total=locked_total,
        total_required=total_required,
    )

    return SolveResult(
        feasible=False,
        assignments={},
        total_required=total_required,
        total_assigned=total_assigned,
        unsatisfied=tuple(unsatisfied),
        shared_pool=shared_pool,
        bottleneck_cut=bottleneck_cut,
    )
