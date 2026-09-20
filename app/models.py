"""请求 / 响应模型（Pydantic v2）。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TrackName = Annotated[str, Field(min_length=1, max_length=128)]
_TrackCapacity = Annotated[int, Field(ge=0, le=1000)]


class ManuscriptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, description="稿件 ID（唯一）")
    authors: list[str] = Field(default_factory=list, max_length=512, description="作者名单")
    topics: list[str] = Field(default_factory=list, max_length=64, description="稿件主题")
    required_reviewers: int | None = Field(
        default=None, ge=0, le=100,
        description="该稿所需评审数；缺省时使用请求级 required_reviewers",
    )
    track: str | None = Field(
        default=None, min_length=1, max_length=128,
        description="稿件所属赛道；缺省表示不参与分赛道容量控制",
    )


class ReviewerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, description="评审人 ID（唯一）")
    topics: list[str] = Field(default_factory=list, max_length=64, description="主题能力")
    capacity: int = Field(ge=0, le=1000, description="最多可评审稿件数")
    conflicted_authors: list[str] = Field(
        default_factory=list, max_length=2048, description="存在利益冲突的作者名单"
    )
    conflicted_manuscripts: list[str] = Field(
        default_factory=list, max_length=2048, description="需回避的稿件 ID 列表"
    )
    track_capacities: dict[_TrackName, _TrackCapacity] | None = Field(
        default=None, max_length=64,
        description=(
            "分赛道容量：赛道 -> 该赛道最多可评审的稿件数（非负整数）。"
            "映射中出现的赛道同时受赛道限额与总容量约束；"
            "未列出的赛道仅受总容量约束"
        ),
    )


class AssignmentIn(BaseModel):
    """一组「稿件 -> 评审人」关系（previous / locked 通用）。"""

    model_config = ConfigDict(extra="forbid")

    manuscript_id: str = Field(min_length=1, max_length=128, description="稿件 ID")
    reviewer_ids: list[str] = Field(
        min_length=1, max_length=10000,
        description="分配给该稿件的评审人 ID 列表（重复项自动去重）",
    )


class AssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manuscripts: list[ManuscriptIn] = Field(max_length=10000)
    reviewers: list[ReviewerIn] = Field(max_length=10000)
    required_reviewers: int = Field(
        default=1, ge=0, le=100,
        description="每稿默认所需评审数（稿件级 required_reviewers 可覆盖）",
    )
    previous_assignments: list[AssignmentIn] | None = Field(
        default=None, max_length=10000,
        description=(
            "上一轮分配结果（增量重排模式）。提供后，求解在保证全局可行的前提下"
            "尽量复用这些关系，使新增 + 移除的关系总数最少；引用已不存在的"
            "稿件/评审人的关系自动忽略"
        ),
    )
    locked_assignments: list[AssignmentIn] | None = Field(
        default=None, max_length=10000,
        description=(
            "必须保留的锁定分配（增量重排模式）。任一锁定关系不满足稿件需求、"
            "评审资格或容量约束时，整体判定不可行并返回结构化冲突原因"
        ),
    )

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "AssignRequest":
        m_ids = [m.id for m in self.manuscripts]
        if len(m_ids) != len(set(m_ids)):
            raise ValueError("manuscripts 中存在重复的 id")
        r_ids = [r.id for r in self.reviewers]
        if len(r_ids) != len(set(r_ids)):
            raise ValueError("reviewers 中存在重复的 id")
        return self


class AssignmentOut(BaseModel):
    manuscript_id: str
    reviewer_ids: list[str]


class UnsatisfiedOut(BaseModel):
    manuscript_id: str
    required: int
    assigned: int
    missing: int
    eligible_reviewers: int
    eligible_remaining_capacity: int
    candidate_gap: int
    capacity_gap: int


class SharedPoolOut(BaseModel):
    manuscript_ids: list[str]
    reviewer_ids: list[str]
    missing_total: int
    pool_remaining_capacity: int
    pool_total_capacity: int


class LockedConflictOut(BaseModel):
    """一条锁定关系冲突的结构化原因。"""

    type: Literal[
        "unknown_manuscript",
        "unknown_reviewer",
        "self_conflict",
        "author_conflict",
        "manuscript_conflict",
        "topic_mismatch",
        "reviewer_capacity_exceeded",
        "reviewer_track_capacity_exceeded",
        "manuscript_requirement_exceeded",
    ]
    manuscript_id: str | None = None
    reviewer_id: str | None = None
    # 仅容量/需求类冲突携带：实际锁定数与上限
    locked: int | None = None
    limit: int | None = None
    # 仅分赛道容量冲突携带：被超出限额的赛道
    track: str | None = None
    detail: str


class _CutEdgeBase(BaseModel):
    # 跨割饱和边的原始容量与实际流量（最小割跨割边必有 flow == capacity）
    capacity: int
    flow: int


class DemandCutEdgeOut(_CutEdgeBase):
    """跨割饱和的 源点 -> 稿件 边：稿件整体需求无法被源侧满足。"""

    manuscript_id: str


class EligibilityCutEdgeOut(_CutEdgeBase):
    """跨割饱和的 稿件 -> 评审人/(评审人,赛道) 边：唯一/全部合格候选在源侧之外。

    边指向 (评审人, 赛道) 限额节点时 track 给出赛道；直接指向评审人时为 null。
    """

    manuscript_id: str
    reviewer_id: str
    track: str | None = None


class TrackCapacityCutEdgeOut(_CutEdgeBase):
    """跨割饱和的 (评审人, 赛道) -> 评审人 边：该评审人的该赛道限额被占满。"""

    reviewer_id: str
    track: str


class ReviewerCapacityCutEdgeOut(_CutEdgeBase):
    """跨割饱和的 评审人 -> 汇点 边：评审人总容量被占满。"""

    reviewer_id: str


class BottleneckCutOut(BaseModel):
    """不可行性的全局最小割证明（最大流最小割定理）。

    source_side 为最大流结束后残量网络中源点可达的业务节点；
    saturated_edges 按业务含义分类列出从源侧跨向割外的饱和边，
    其容量之和恰为 max_assignable，shortfall = total_required - max_assignable。
    增量模式下容量/流量口径为锁定固化扣减后的残量网络；max_assignable 已计入
    必然保留的锁定关系。
    """

    source_side: "BottleneckCutSourceSide"
    saturated_edges: "BottleneckCutEdges"
    max_assignable: int
    shortfall: int


class BottleneckCutSourceSide(BaseModel):
    manuscript_ids: list[str]
    reviewer_ids: list[str]
    reviewer_track_nodes: list["ReviewerTrackNodeOut"]


class ReviewerTrackNodeOut(BaseModel):
    reviewer_id: str
    track: str


class BottleneckCutEdges(BaseModel):
    demand: list[DemandCutEdgeOut]
    eligibility: list[EligibilityCutEdgeOut]
    track_capacity: list[TrackCapacityCutEdgeOut]
    reviewer_capacity: list[ReviewerCapacityCutEdgeOut]


BottleneckCutOut.model_rebuild()


class AssignResponse(BaseModel):
    status: Literal["feasible", "infeasible"]
    total_required: int
    total_assigned: int
    # 可行时给出完整分配；不可行时为 null（不输出部分结果）
    assignments: list[AssignmentOut] | None
    # 不可行时给出缺口诊断；可行时为 null
    unsatisfied: list[UnsatisfiedOut] | None
    shared_pool: SharedPoolOut | None
    # 锁定关系冲突（仅增量重排模式且锁定不可行时非空）；否则为 null
    locked_conflicts: list[LockedConflictOut] | None = None
    # 锁定关系合法但剩余需求无法满足时，基于残量网络的全局最小割证明；
    # 可行或锁定关系校验失败时为 null
    bottleneck_cut: BottleneckCutOut | None = None
