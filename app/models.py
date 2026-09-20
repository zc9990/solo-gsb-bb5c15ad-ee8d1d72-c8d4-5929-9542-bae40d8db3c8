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
