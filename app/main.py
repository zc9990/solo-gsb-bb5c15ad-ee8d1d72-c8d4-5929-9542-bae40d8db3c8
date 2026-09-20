"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI

from .models import (
    AssignmentIn,
    AssignmentOut,
    AssignRequest,
    AssignResponse,
    LockedConflictOut,
    SharedPoolOut,
    UnsatisfiedOut,
)
from .solver import ManuscriptSpec, ReviewerSpec, solve_assignment

app = FastAPI(
    title="Anonymous Review Assignment API",
    version="1.2.0",
    description=(
        "匿名评审分配服务：基于全局最大流（非逐稿贪心）求满足主题能力、"
        "利益冲突与容量约束的完整分配；不可行时返回缺口诊断且不输出部分结果。"
        "支持分赛道容量：稿件可声明 track，评审人可声明 track_capacities，"
        "赛道限额与总容量在同一流网络中联立求解。"
        "支持增量重排：携带 previous_assignments / locked_assignments 时，"
        "锁定关系经稿件需求、评审资格与容量校验后强制保留，其余名额在全局可行"
        "前提下最少改动地复用上一轮分配。服务无状态，不持久化任何稿件/评审信息。"
    ),
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "anonymous-review-assign",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
    }


def _to_pair_set(
    items: list[AssignmentIn] | None,
) -> frozenset[tuple[str, str]] | None:
    """把 AssignmentIn 列表展开为 (manuscript_id, reviewer_id) 对集合（自动去重）。"""
    if items is None:
        return None
    return frozenset(
        (a.manuscript_id, rid) for a in items for rid in set(a.reviewer_ids)
    )


@app.post("/assign", response_model=AssignResponse, tags=["assign"])
def assign(req: AssignRequest) -> AssignResponse:
    default_required = req.required_reviewers
    manuscripts = [
        ManuscriptSpec(
            id=m.id,
            authors=tuple(m.authors),
            topics=tuple(m.topics),
            required_reviewers=(
                m.required_reviewers
                if m.required_reviewers is not None
                else default_required
            ),
            track=m.track,
        )
        for m in req.manuscripts
    ]
    reviewers = [
        ReviewerSpec(
            id=r.id,
            topics=tuple(r.topics),
            capacity=r.capacity,
            conflicted_authors=tuple(r.conflicted_authors),
            conflicted_manuscripts=tuple(r.conflicted_manuscripts),
            track_capacities=(
                tuple(sorted(r.track_capacities.items()))
                if r.track_capacities
                else ()
            ),
        )
        for r in req.reviewers
    ]

    result = solve_assignment(
        manuscripts,
        reviewers,
        previous=_to_pair_set(req.previous_assignments),
        locked=_to_pair_set(req.locked_assignments),
    )

    # 锁定关系冲突：结构化原因，不输出任何部分分配
    if result.locked_conflicts:
        return AssignResponse(
            status="infeasible",
            total_required=result.total_required,
            total_assigned=result.total_assigned,
            assignments=None,
            unsatisfied=None,
            shared_pool=None,
            locked_conflicts=[
                LockedConflictOut(**vars(c)) for c in result.locked_conflicts
            ],
        )

    if result.feasible:
        return AssignResponse(
            status="feasible",
            total_required=result.total_required,
            total_assigned=result.total_assigned,
            assignments=[
                AssignmentOut(manuscript_id=mid, reviewer_ids=list(rids))
                for mid, rids in sorted(result.assignments.items())
            ],
            unsatisfied=None,
            shared_pool=None,
            locked_conflicts=None,
        )

    return AssignResponse(
        status="infeasible",
        total_required=result.total_required,
        total_assigned=result.total_assigned,
        assignments=None,
        unsatisfied=[UnsatisfiedOut(**vars(u)) for u in result.unsatisfied],
        shared_pool=SharedPoolOut(**result.shared_pool) if result.shared_pool else None,
        locked_conflicts=None,
    )
