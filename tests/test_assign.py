"""POST /assign 与 /health 的端到端测试。"""

from __future__ import annotations

import random

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _verify_constraints(body: dict, response: dict) -> None:
    """校验分配结果满足全部约束：篇均评审数、容量、赛道限额、主题匹配、冲突回避。"""
    assert response["status"] == "feasible"
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in response["assignments"]}
    reviewers = {r["id"]: r for r in body["reviewers"]}
    default_req = body.get("required_reviewers", 1)

    used = {rid: 0 for rid in reviewers}
    track_used: dict[str, dict[str, int]] = {rid: {} for rid in reviewers}
    for m in body["manuscripts"]:
        rid_list = assignments[m["id"]]
        req = m.get("required_reviewers") or default_req
        assert len(rid_list) == req, f"{m['id']} 评审数不足"
        assert len(set(rid_list)) == len(rid_list), "同一稿件重复分配评审人"
        track = m.get("track")
        for rid in rid_list:
            r = reviewers[rid]
            used[rid] += 1
            assert rid not in m["authors"], "自稿冲突"
            assert not set(m["authors"]) & set(r.get("conflicted_authors", [])), "利益冲突"
            assert m["id"] not in r.get("conflicted_manuscripts", [])
            if m["topics"]:
                assert set(m["topics"]) & set(r["topics"]), "主题不匹配"
            if track is not None:
                per = track_used[rid]
                per[track] = per.get(track, 0) + 1
    for rid, n in used.items():
        assert n <= reviewers[rid]["capacity"], f"{rid} 超出容量"
    for rid, per_track in track_used.items():
        caps = reviewers[rid].get("track_capacities") or {}
        for track, n in per_track.items():
            if track in caps:
                assert n <= caps[track], f"{rid} 赛道 {track} 超出限额"


# ---------- 健康检查 ----------

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------- 可行场景 ----------

def test_feasible_basic():
    body = {
        "required_reviewers": 2,
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["ml"]},
            {"id": "m2", "authors": ["bob"], "topics": ["ml", "db"]},
            {"id": "m3", "authors": ["carol"], "topics": ["db"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["ml"], "capacity": 2},
            {"id": "r2", "topics": ["ml", "db"], "capacity": 2},
            {"id": "r3", "topics": ["db"], "capacity": 2},
            {"id": "r4", "topics": ["ml", "db"], "capacity": 1,
             "conflicted_authors": ["alice"]},
        ],
    }
    resp = client.post("/assign", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_required"] == 6
    assert data["total_assigned"] == 6
    assert data["unsatisfied"] is None
    _verify_constraints(body, data)


def test_global_matching_not_greedy():
    """逐稿贪心会失败、全局匹配才能成功的经典情形。

    m1 可由 r1/r2 评审，m2 只能由 r1 评审，容量均为 1。
    贪心先把 r1 给 m1 后 m2 无人可评；全局解必须是 m1->r2, m2->r1。
    """
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t1"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["t2"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t1", "t2"], "capacity": 1},
            {"id": "r2", "topics": ["t1"], "capacity": 1},
        ],
    }
    resp = client.post("/assign", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "feasible"
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]}
    assert assignments == {"m1": ["r2"], "m2": ["r1"]}


def test_per_manuscript_required_overrides_default():
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 2},
            {"id": "m2", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2},
            {"id": "r2", "topics": ["t"], "capacity": 2},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assert data["total_required"] == 3
    _verify_constraints(body, data)


def test_empty_manuscripts_is_feasible():
    body = {"manuscripts": [], "reviewers": [{"id": "r1", "topics": [], "capacity": 1}]}
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"] == []


# ---------- 确定性 / 稳定性 ----------

def _random_instance(seed: int) -> dict:
    rng = random.Random(seed)
    topics = ["ml", "nlp", "cv", "db", "se"]
    authors = [f"author{i}" for i in range(20)]
    manuscripts = [
        {
            "id": f"m{i}",
            "authors": rng.sample(authors, 2),
            "topics": rng.sample(topics, rng.randint(1, 2)),
            "required_reviewers": rng.randint(1, 3),
        }
        for i in range(12)
    ]
    reviewers = [
        {
            "id": f"r{j}",
            "topics": rng.sample(topics, rng.randint(1, 3)),
            "capacity": rng.randint(1, 4),
            "conflicted_authors": rng.sample(authors, rng.randint(0, 3)),
        }
        for j in range(15)
    ]
    return {"manuscripts": manuscripts, "reviewers": reviewers}


def test_same_input_same_output():
    body = _random_instance(seed=42)
    r1 = client.post("/assign", json=body).json()
    r2 = client.post("/assign", json=body).json()
    assert r1 == r2


def test_input_order_does_not_change_result():
    body = _random_instance(seed=7)
    shuffled = {
        "manuscripts": list(reversed(body["manuscripts"])),
        "reviewers": list(reversed(body["reviewers"])),
    }
    r1 = client.post("/assign", json=body).json()
    r2 = client.post("/assign", json=shuffled).json()
    assert r1 == r2


# ---------- 不可行场景 ----------

def test_infeasible_capacity_gap():
    """总容量不足：2 稿各需 2 人，只有 2 名评审各容量 1。"""
    body = {
        "required_reviewers": 2,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"]},
            {"id": "m2", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    # 不输出任何部分分配
    assert data["assignments"] is None
    assert data["total_required"] == 4
    assert data["total_assigned"] == 2

    unsat = {u["manuscript_id"]: u for u in data["unsatisfied"]}
    assert set(unsat) == {"m2"}  # 确定性：m1 先被满足，m2 缺口 2
    assert unsat["m2"]["missing"] == 2
    assert unsat["m2"]["eligible_reviewers"] == 2
    assert unsat["m2"]["eligible_remaining_capacity"] == 0
    assert unsat["m2"]["candidate_gap"] == 0
    assert unsat["m2"]["capacity_gap"] == 2

    pool = data["shared_pool"]
    assert pool["missing_total"] == 2
    assert pool["pool_remaining_capacity"] == 0
    assert pool["pool_total_capacity"] == 2
    assert sorted(pool["reviewer_ids"]) == ["r1", "r2"]


def test_infeasible_candidate_gap():
    """候选人数不足：m1 需 2 人，但 r2 与作者存在利益冲突。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["ml"],
             "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["ml"], "capacity": 5},
            {"id": "r2", "topics": ["ml"], "capacity": 5,
             "conflicted_authors": ["alice"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    u = data["unsatisfied"][0]
    assert u["manuscript_id"] == "m1"
    assert u["eligible_reviewers"] == 1
    assert u["candidate_gap"] == 1
    assert u["capacity_gap"] == 0  # r1 剩余容量充足，缺的是候选人数


def test_infeasible_no_eligible_reviewer_at_all():
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["quantum"],
             "required_reviewers": 1},
        ],
        "reviewers": [{"id": "r1", "topics": ["ml"], "capacity": 3}],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    u = data["unsatisfied"][0]
    assert u["eligible_reviewers"] == 0
    assert u["candidate_gap"] == 1
    assert data["shared_pool"]["reviewer_ids"] == []


def test_self_conflict_excluded():
    """评审人是稿件作者时自动回避。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["r1"], "topics": ["t"],
             "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"][0]["reviewer_ids"] == ["r2"]


# ---------- 请求校验 ----------

def test_duplicate_manuscript_id_rejected():
    body = {
        "manuscripts": [{"id": "m1"}, {"id": "m1"}],
        "reviewers": [],
    }
    resp = client.post("/assign", json=body)
    assert resp.status_code == 422


def test_duplicate_reviewer_id_rejected():
    body = {
        "manuscripts": [],
        "reviewers": [
            {"id": "r1", "capacity": 1},
            {"id": "r1", "capacity": 2},
        ],
    }
    resp = client.post("/assign", json=body)
    assert resp.status_code == 422


def test_negative_capacity_rejected():
    body = {
        "manuscripts": [],
        "reviewers": [{"id": "r1", "capacity": -1}],
    }
    resp = client.post("/assign", json=body)
    assert resp.status_code == 422


# ---------- 增量重排模式：复用 previous ----------

def test_incremental_reuses_previous_assignments():
    """普通模式按 ID 顺序会选 r1,r2；携带 previous 后必须原样复用 r2,r3。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
            {"id": "r3", "topics": ["t"], "capacity": 1},
        ],
        "previous_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r2", "r3"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"] == [
        {"manuscript_id": "m1", "reviewer_ids": ["r2", "r3"]}
    ]
    assert data["locked_conflicts"] is None
    _verify_constraints(body, data)


def test_incremental_minimizes_changes_on_growth():
    """需求从 1 涨到 2：保留上一轮的 r3，仅新增 1 人，同成本取 ID 最小的 r1。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
            {"id": "r3", "topics": ["t"], "capacity": 1},
        ],
        "previous_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r3"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"] == [
        {"manuscript_id": "m1", "reviewer_ids": ["r1", "r3"]}
    ]


def test_incremental_global_feasibility_overrides_reuse():
    """复用 previous 会导致不可行时，全局可行优先：必须放弃 m1->r1。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t1"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["t2"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t1", "t2"], "capacity": 1},
            {"id": "r2", "topics": ["t1"], "capacity": 1},
        ],
        "previous_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]}
    assert assignments == {"m1": ["r2"], "m2": ["r1"]}


def test_incremental_previous_unknown_ids_ignored():
    """previous 中引用已不存在的稿件/评审人的关系自动忽略，不影响求解。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 1}],
        "previous_assignments": [
            {"manuscript_id": "m9", "reviewer_ids": ["r1"]},
            {"manuscript_id": "m1", "reviewer_ids": ["r9"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"] == [{"manuscript_id": "m1", "reviewer_ids": ["r1"]}]


def test_incremental_deterministic_and_order_independent():
    body = _random_instance(seed=13)  # 该种子生成的实例可行
    # 先求一轮作为 previous，再叠加增量重排
    first = client.post("/assign", json=body).json()
    assert first["status"] == "feasible"
    body["previous_assignments"] = first["assignments"]
    r1 = client.post("/assign", json=body).json()
    r2 = client.post("/assign", json=body).json()
    assert r1 == r2
    # 复用上一轮全部关系 -> 结果应与上一轮完全一致（0 改动）
    assert r1["assignments"] == first["assignments"]
    shuffled = {
        **body,
        "manuscripts": list(reversed(body["manuscripts"])),
        "reviewers": list(reversed(body["reviewers"])),
        "previous_assignments": list(reversed(body["previous_assignments"])),
    }
    r3 = client.post("/assign", json=shuffled).json()
    assert r3 == r1


# ---------- 增量重排模式：锁定关系 ----------

def test_locked_assignments_are_forced():
    """锁定 m1->r2 后，即使普通模式会选 r1，结果也必须保留 r2。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r2"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"] == [{"manuscript_id": "m1", "reviewer_ids": ["r2"]}]
    _verify_constraints(body, data)


def test_locked_and_previous_combined():
    """锁定对固化后，剩余名额仍优先复用 previous。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
            {"id": "r3", "topics": ["t"], "capacity": 1},
        ],
        "previous_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r2", "r3"]},
        ],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r3"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    # r3 锁定保留，剩余 1 个名额复用 previous 的 r2 -> 0 改动
    assert data["assignments"] == [
        {"manuscript_id": "m1", "reviewer_ids": ["r2", "r3"]}
    ]


def test_locked_topic_mismatch_rejected():
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["ml"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["ml"], "capacity": 1},
            {"id": "r2", "topics": ["db"], "capacity": 1},
        ],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r2"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    # 不输出部分分配，也不给缺口诊断，只给锁定冲突
    assert data["assignments"] is None
    assert data["unsatisfied"] is None
    assert data["shared_pool"] is None
    conflicts = data["locked_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "topic_mismatch"
    assert conflicts[0]["manuscript_id"] == "m1"
    assert conflicts[0]["reviewer_id"] == "r2"


def test_locked_author_conflict_rejected():
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["t"],
             "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1,
             "conflicted_authors": ["alice"]},
        ],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["locked_conflicts"][0]["type"] == "author_conflict"


def test_locked_self_conflict_rejected():
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["r1"], "topics": ["t"],
             "required_reviewers": 1},
        ],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 1}],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["locked_conflicts"][0]["type"] == "self_conflict"


def test_locked_capacity_exceeded():
    """r1 容量 1，却被锁定到 2 篇稿件。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 2},
        ],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
            {"manuscript_id": "m2", "reviewer_ids": ["r1"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    conflicts = data["locked_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "reviewer_capacity_exceeded"
    assert conflicts[0]["reviewer_id"] == "r1"
    assert conflicts[0]["locked"] == 2
    assert conflicts[0]["limit"] == 1


def test_locked_requirement_exceeded():
    """m1 只需 1 名评审，却被锁定了 2 名。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1", "r2"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    conflicts = data["locked_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "manuscript_requirement_exceeded"
    assert conflicts[0]["manuscript_id"] == "m1"
    assert conflicts[0]["locked"] == 2
    assert conflicts[0]["limit"] == 1


def test_locked_unknown_references_rejected():
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 1}],
        "locked_assignments": [
            {"manuscript_id": "m9", "reviewer_ids": ["r1"]},
            {"manuscript_id": "m1", "reviewer_ids": ["r9"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    types = {c["type"] for c in data["locked_conflicts"]}
    assert types == {"unknown_manuscript", "unknown_reviewer"}


def test_incremental_infeasible_still_diagnoses():
    """增量模式整体不可行（非锁定冲突）时，仍返回缺口诊断且不输出部分分配。"""
    body = {
        "required_reviewers": 2,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"]},
            {"id": "m2", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
        "previous_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    assert data["locked_conflicts"] is None
    assert data["unsatisfied"] is not None
    assert data["shared_pool"] is not None


def test_pair_ineligibility_reasons_matches_eligible():
    """锁定校验的资格判定必须与建图的资格过滤完全一致。"""
    from app.solver import (
        ManuscriptSpec,
        ReviewerSpec,
        _eligible_reviewers,
        _pair_ineligibility_reasons,
    )

    rng = random.Random(3)
    topics = ["ml", "nlp", "cv"]
    authors = [f"a{i}" for i in range(8)]
    for _ in range(50):
        ms = [
            ManuscriptSpec(
                id=f"m{i}",
                authors=tuple(rng.sample(authors, rng.randint(0, 2))),
                topics=tuple(rng.sample(topics, rng.randint(0, 2))),
                required_reviewers=1,
            )
            for i in range(4)
        ]
        rs = [
            ReviewerSpec(
                id=f"r{j}",
                topics=tuple(rng.sample(topics, rng.randint(0, 2))),
                capacity=rng.randint(0, 2),
                conflicted_authors=tuple(rng.sample(authors, rng.randint(0, 2))),
                conflicted_manuscripts=tuple(
                    m.id for m in ms if rng.random() < 0.2
                ),
            )
            for j in range(5)
        ]
        # 制造自稿冲突样本
        rs[0] = ReviewerSpec(
            id=ms[0].authors[0] if ms[0].authors else "r0",
            topics=rs[0].topics,
            capacity=rs[0].capacity,
            conflicted_authors=rs[0].conflicted_authors,
            conflicted_manuscripts=rs[0].conflicted_manuscripts,
        )
        for m in ms:
            eligible_ids = {rs[j].id for j in _eligible_reviewers(m, rs)}
            for r in rs:
                assert (not _pair_ineligibility_reasons(m, r)) == (
                    r.id in eligible_ids
                )


# ---------- 分赛道容量：普通模式 ----------

def test_track_capacity_limits_normal_mode():
    """r1 总容量 3 但 poster 赛道限额 1：两篇 poster 至多一篇分给 r1。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "poster"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "poster"},
            {"id": "m3", "authors": [], "topics": ["t"], "track": "oral"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 3,
             "track_capacities": {"poster": 1}},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    _verify_constraints(body, data)
    # 确定性结果：m1->r2，m2->r1（占用其唯一 poster 名额），m3->r1
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]}
    assert assignments == {"m1": ["r2"], "m2": ["r1"], "m3": ["r1"]}


def test_track_and_total_capacity_solved_jointly():
    """赛道限额与总容量联立求解：各赛道限额都够，但总容量不够即不可行。

    若分赛道独立求解后拼接，会误判可行（A: 2<=2，B: 2<=2）；
    统一流网络中总容量 3 < 总需求 4，必须判定不可行。
    """
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m3", "authors": [], "topics": ["t"], "track": "B"},
            {"id": "m4", "authors": [], "topics": ["t"], "track": "B"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 3,
             "track_capacities": {"A": 2, "B": 2}},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    assert data["total_required"] == 4
    assert data["total_assigned"] == 3


def test_unlisted_track_only_total_capacity():
    """未列入 track_capacities 的赛道只受总容量约束。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "B"},
            {"id": "m3", "authors": [], "topics": ["t"], "track": "B"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 3,
             "track_capacities": {"A": 1}},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    _verify_constraints(body, data)
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]}
    assert assignments == {"m1": ["r1"], "m2": ["r1"], "m3": ["r1"]}


def test_manuscript_without_track_unaffected():
    """不携带 track 的稿件不受任何赛道限额约束。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2,
             "track_capacities": {"A": 0}},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"  # m1 的赛道 A 限额为 0，只能分给 r1 之外的人
    assert data["unsatisfied"][0]["manuscript_id"] == "m1"


def test_track_capacity_zero_diagnosed_as_capacity_gap():
    """赛道限额 0：候选存在但该赛道剩余容量为 0，报容量缺口而非候选缺口。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A",
             "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 5,
             "track_capacities": {"A": 0}},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    u = data["unsatisfied"][0]
    assert u["eligible_reviewers"] == 1
    assert u["eligible_remaining_capacity"] == 0
    assert u["candidate_gap"] == 0
    assert u["capacity_gap"] == 1


def test_track_deterministic_and_order_independent():
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": f"m{i}", "authors": [], "topics": ["t"], "track": track}
            for i, track in enumerate(["A", "B", "A", "B", "C"])
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 3,
             "track_capacities": {"A": 1, "B": 2}},
            {"id": "r2", "topics": ["t"], "capacity": 2,
             "track_capacities": {"B": 1}},
            {"id": "r3", "topics": ["t"], "capacity": 2},
        ],
    }
    r1 = client.post("/assign", json=body).json()
    assert r1["status"] == "feasible"
    _verify_constraints(body, r1)
    r2 = client.post("/assign", json=body).json()
    assert r1 == r2
    shuffled = {
        "manuscripts": list(reversed(body["manuscripts"])),
        "reviewers": list(reversed(body["reviewers"])),
    }
    r3 = client.post("/assign", json=shuffled).json()
    assert r1 == r3


def test_negative_track_capacity_rejected():
    body = {
        "manuscripts": [],
        "reviewers": [
            {"id": "r1", "capacity": 1, "track_capacities": {"A": -1}},
        ],
    }
    resp = client.post("/assign", json=body)
    assert resp.status_code == 422


# ---------- 分赛道容量：增量重排模式 ----------

def test_incremental_locked_consumes_track_capacity():
    """锁定占用赛道限额后，同赛道剩余名额必须改走其他评审人。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "A"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2,
             "track_capacities": {"A": 1}},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    _verify_constraints(body, data)
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]}
    assert assignments == {"m1": ["r1"], "m2": ["r2"]}


def test_locked_exceeds_track_capacity_rejected():
    """锁定数超出赛道限额（总容量未超）：整体不可行，返回新冲突类型。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "A"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 5,
             "track_capacities": {"A": 1}},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
            {"manuscript_id": "m2", "reviewer_ids": ["r1"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    # 不输出部分结果，也不给缺口诊断，只给锁定冲突
    assert data["assignments"] is None
    assert data["unsatisfied"] is None
    assert data["shared_pool"] is None
    conflicts = data["locked_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "reviewer_track_capacity_exceeded"
    assert conflicts[0]["reviewer_id"] == "r1"
    assert conflicts[0]["track"] == "A"
    assert conflicts[0]["locked"] == 2
    assert conflicts[0]["limit"] == 1


def test_incremental_previous_reuse_respects_track_capacity():
    """previous 中两条 A 赛道关系，r1 的 A 赛道限额 1：最多复用一条。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "A"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 5,
             "track_capacities": {"A": 1}},
            {"id": "r2", "topics": ["t"], "capacity": 5},
        ],
        "previous_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
            {"manuscript_id": "m2", "reviewer_ids": ["r1"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    _verify_constraints(body, data)
    # 确定性：复用 m1->r1（费用 0），m2 改分 r2（1 新增 1 移除，已是最少改动）
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]}
    assert assignments == {"m1": ["r1"], "m2": ["r2"]}


def test_incremental_track_infeasible_still_diagnoses():
    """增量模式下赛道限额导致整体不可行时，仍返回缺口诊断且不输出部分分配。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "A"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 5,
             "track_capacities": {"A": 1}},
        ],
        "previous_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    assert data["locked_conflicts"] is None
    unsat = {u["manuscript_id"]: u for u in data["unsatisfied"]}
    assert set(unsat) == {"m2"}
    assert unsat["m2"]["eligible_remaining_capacity"] == 0
    assert unsat["m2"]["capacity_gap"] == 1


# ---------- 不可行性证明：bottleneck_cut 全局最小割 ----------

_CUT_GROUPS = ("demand", "eligibility", "track_capacity", "reviewer_capacity")


def _assert_cut_invariants(data: dict, locked_total: int = 0) -> dict:
    """校验 bottleneck_cut 的通用不变量并返回该结构。

    locked_total 为已固化的合法锁定名额数（普通模式 0）：增量模式的瓶颈割建在
    扣除锁定占用后的残量网络上，跨割边容量之和 = 剩余网络最大流；
    max_assignable = locked_total + 割容量。
    """
    cut = data["bottleneck_cut"]
    assert cut is not None
    # 绝不随证明泄露部分分配
    assert data["assignments"] is None

    # 源侧节点列表均按业务 ID 排序
    assert cut["source_manuscripts"] == sorted(cut["source_manuscripts"])
    assert cut["source_reviewers"] == sorted(cut["source_reviewers"])
    assert cut["source_reviewer_tracks"] == [
        list(pair) for pair in sorted(
            (pair[0], pair[1]) for pair in cut["source_reviewer_tracks"]
        )
    ]
    for pair in cut["source_reviewer_tracks"]:
        assert len(pair) == 2

    # 四类跨割边的组内排序键
    sort_keys = {
        "demand": lambda e: e["manuscript_id"] or "",
        "eligibility": lambda e: (
            e["manuscript_id"] or "", e["reviewer_id"] or "", e["track"] or ""
        ),
        "track_capacity": lambda e: (e["reviewer_id"] or "", e["track"] or ""),
        "reviewer_capacity": lambda e: e["reviewer_id"] or "",
    }
    for group in _CUT_GROUPS:
        edges = cut[group]
        assert edges == sorted(edges, key=sort_keys[group])
        for e in edges:
            assert e["type"] == group
            assert e["capacity"] >= 0

    cut_capacity = sum(
        e["capacity"] for group in _CUT_GROUPS for e in cut[group]
    )
    # 最大流最小割：跨割饱和边容量之和 = 本次（剩余）网络的最大流
    assert cut["max_assignable"] == locked_total + cut_capacity
    assert cut["max_assignable"] + cut["shortfall"] == data["total_required"]
    assert cut["max_assignable"] == data["total_assigned"]
    assert cut["shortfall"] > 0
    return cut


def test_bottleneck_cut_total_capacity_gap():
    """总容量不足：瓶颈割由两条评审人->汇饱和边构成。"""
    body = {
        "required_reviewers": 2,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"]},
            {"id": "m2", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
    }
    data = client.post("/assign", json=body).json()
    cut = _assert_cut_invariants(data)
    assert cut["max_assignable"] == 2
    assert cut["shortfall"] == 2
    assert cut["source_manuscripts"] == ["m1", "m2"]
    assert cut["source_reviewers"] == ["r1", "r2"]
    assert cut["demand"] == []
    assert cut["eligibility"] == []
    assert cut["track_capacity"] == []
    assert [(e["reviewer_id"], e["capacity"]) for e in cut["reviewer_capacity"]] == [
        ("r1", 1), ("r2", 1),
    ]


def test_bottleneck_cut_eligibility_gap():
    """候选不足：瓶颈割由饱和的资格边构成（Hall 型割）。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["ml"],
             "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["ml"], "capacity": 5},
            {"id": "r2", "topics": ["ml"], "capacity": 5,
             "conflicted_authors": ["alice"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    cut = _assert_cut_invariants(data)
    assert cut["max_assignable"] == 1
    assert cut["shortfall"] == 1
    assert cut["source_manuscripts"] == ["m1"]
    assert cut["source_reviewers"] == []
    # 只有合格边 m1->r1 跨割；不合格的 r2 根本没有建边
    assert [(e["manuscript_id"], e["reviewer_id"], e["capacity"])
            for e in cut["eligibility"]] == [("m1", "r1", 1)]
    assert cut["reviewer_capacity"] == []
    assert cut["demand"] == []
    assert cut["track_capacity"] == []


def test_bottleneck_cut_no_eligible_reviewer():
    """无任何合格评审：割容量为 0，四类跨割边均可为空。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["quantum"],
             "required_reviewers": 1},
        ],
        "reviewers": [{"id": "r1", "topics": ["ml"], "capacity": 3}],
    }
    data = client.post("/assign", json=body).json()
    cut = _assert_cut_invariants(data)
    assert cut["max_assignable"] == 0
    assert cut["shortfall"] == 1
    assert cut["source_manuscripts"] == ["m1"]
    for group in _CUT_GROUPS:
        assert cut[group] == []


def test_bottleneck_cut_track_capacity_zero():
    """赛道限额 0：赛道节点在源侧、评审人在汇侧，0 容量赛道边跨割。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A",
             "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 5,
             "track_capacities": {"A": 0}},
        ],
    }
    data = client.post("/assign", json=body).json()
    cut = _assert_cut_invariants(data)
    assert cut["max_assignable"] == 0
    assert cut["shortfall"] == 1
    assert cut["source_manuscripts"] == ["m1"]
    assert cut["source_reviewer_tracks"] == [["r1", "A"]]
    assert cut["source_reviewers"] == []
    assert [(e["reviewer_id"], e["track"], e["capacity"])
            for e in cut["track_capacity"]] == [("r1", "A", 0)]


def test_bottleneck_cut_joint_track_and_total():
    """赛道与总容量联立不可行：割由总容量边构成（割容量 3 < 需求 4）。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m3", "authors": [], "topics": ["t"], "track": "B"},
            {"id": "m4", "authors": [], "topics": ["t"], "track": "B"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 3,
             "track_capacities": {"A": 2, "B": 2}},
        ],
    }
    data = client.post("/assign", json=body).json()
    cut = _assert_cut_invariants(data)
    assert cut["max_assignable"] == 3
    assert cut["shortfall"] == 1
    assert cut["source_reviewer_tracks"] == [["r1", "A"], ["r1", "B"]]
    assert [(e["reviewer_id"], e["capacity"])
            for e in cut["reviewer_capacity"]] == [("r1", 3)]


def test_bottleneck_cut_null_when_feasible_or_locked_conflict():
    """可行时与锁定校验失败时，bottleneck_cut 必须为 null。"""
    feasible = client.post("/assign", json={
        "manuscripts": [{"id": "m1", "authors": [], "topics": ["t"],
                         "required_reviewers": 1}],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 1}],
    }).json()
    assert feasible["status"] == "feasible"
    assert feasible["bottleneck_cut"] is None

    conflict = client.post("/assign", json={
        "manuscripts": [{"id": "m1", "authors": [], "topics": ["ml"],
                         "required_reviewers": 1}],
        "reviewers": [{"id": "r2", "topics": ["db"], "capacity": 1}],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r2"]},
        ],
    }).json()
    assert conflict["status"] == "infeasible"
    assert conflict["locked_conflicts"] is not None
    assert conflict["bottleneck_cut"] is None
    assert conflict["unsatisfied"] is None


def test_bottleneck_cut_incremental_with_valid_lock():
    """合法锁定固化后仍不可行：割建在扣减锁定的残量网络上，容量含锁定名额。"""
    body = {
        "required_reviewers": 2,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"]},
            {"id": "m2", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    cut = _assert_cut_invariants(data, locked_total=1)
    # 1 个锁定名额 + 1 个剩余网络最大流 = 2；总需求 4，缺口 2
    assert cut["max_assignable"] == 2
    assert cut["shortfall"] == 2
    # r1 的剩余总容量被锁定占满：跨割边容量为 0 仍显式列出
    caps = {e["reviewer_id"]: e["capacity"] for e in cut["reviewer_capacity"]}
    assert caps == {"r1": 0, "r2": 1}
    assert data["unsatisfied"] is not None
    assert data["shared_pool"] is not None


def test_bottleneck_cut_incremental_track_edge():
    """增量模式赛道剩余限额构成瓶颈：跨割边按剩余（扣锁定后）容量列出。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "A"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 5,
             "track_capacities": {"A": 1}},
        ],
        "previous_assignments": [
            {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    cut = _assert_cut_invariants(data)
    assert cut["max_assignable"] == 1
    assert cut["shortfall"] == 1
    assert cut["source_reviewer_tracks"] == [["r1", "A"]]
    assert [(e["reviewer_id"], e["track"], e["capacity"])
            for e in cut["track_capacity"]] == [("r1", "A", 1)]


def test_bottleneck_cut_order_independent():
    """列表乱序不改变 bottleneck_cut 的输出。"""
    body = {
        "required_reviewers": 2,
        "manuscripts": [
            {"id": "m3", "authors": [], "topics": ["t"]},
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "A"},
        ],
        "reviewers": [
            {"id": "r2", "topics": ["t"], "capacity": 1},
            {"id": "r1", "topics": ["t"], "capacity": 1,
             "track_capacities": {"A": 1}},
        ],
    }
    shuffled = {
        "manuscripts": list(reversed(body["manuscripts"])),
        "reviewers": list(reversed(body["reviewers"])),
        "required_reviewers": 2,
    }
    r1 = client.post("/assign", json=body).json()
    r2 = client.post("/assign", json=shuffled).json()
    assert r1["status"] == r2["status"] == "infeasible"
    assert r1["bottleneck_cut"] == r2["bottleneck_cut"]


def test_bottleneck_cut_random_instances_invariants():
    """随机不可行实例（普通 + 增量模式，含合法锁定）：割容量恒等不变量必须始终成立。"""
    rng = random.Random(2026)
    topics = ["ml", "nlp", "cv"]
    authors = [f"a{i}" for i in range(6)]
    checked = {"normal": 0, "incremental": 0}
    for seed in range(200):
        sub = random.Random(seed)
        body = {
            "required_reviewers": rng.choice([1, 2]),
            "manuscripts": [
                {
                    "id": f"m{i}",
                    "authors": sub.sample(authors, sub.randint(0, 2)),
                    "topics": sub.sample(topics, sub.randint(1, 2)),
                    "required_reviewers": sub.randint(1, 3),
                    **(
                        {"track": sub.choice(["A", "B"])}
                        if sub.random() < 0.5 else {}
                    ),
                }
                for i in range(sub.randint(1, 5))
            ],
            "reviewers": [
                {
                    "id": f"r{j}",
                    "topics": sub.sample(topics, sub.randint(1, 2)),
                    # 小容量制造大量不可行实例
                    "capacity": sub.randint(0, 2),
                    "conflicted_authors": sub.sample(authors, sub.randint(0, 2)),
                    **(
                        {"track_capacities": {
                            t: sub.randint(0, 2) for t in sub.sample(["A", "B"], 1)
                        }}
                        if sub.random() < 0.5 else {}
                    ),
                }
                for j in range(sub.randint(1, 4))
            ],
        }

        # 普通模式
        data = client.post("/assign", json=body).json()
        if data["status"] == "infeasible" and data["locked_conflicts"] is None:
            _assert_cut_invariants(data)
            checked["normal"] += 1

        # 增量模式：从普通解中随机挑合法关系锁定（仅当普通解可行时可安全选取），
        # 再抬高未锁定稿件的需求以进入「锁定合法但整体不可行」分支（锁定仍合法）
        if data["status"] == "feasible" and data["assignments"]:
            locks = []
            locked_mids: set[str] = set()
            for entry in data["assignments"]:
                if sub.random() < 0.4 and entry["reviewer_ids"]:
                    rid = sub.choice(entry["reviewer_ids"])
                    locks.append({
                        "manuscript_id": entry["manuscript_id"],
                        "reviewer_ids": [rid],
                    })
                    locked_mids.add(entry["manuscript_id"])
            if locks:
                # 对所有未锁定稿件抬高需求：锁定仍合法，但总容量必然更紧，
                # 稳定地进入「锁定合法但整体不可行」分支
                grown = []
                for m in body["manuscripts"]:
                    m2 = dict(m)
                    if m2["id"] not in locked_mids:
                        m2["required_reviewers"] = m2.get(
                            "required_reviewers", body["required_reviewers"]
                        ) + sub.randint(2, 4)
                    grown.append(m2)
                req_body = {
                    **body,
                    "manuscripts": grown,
                    "locked_assignments": locks,
                    "previous_assignments": data["assignments"],
                }
                locked_n = len({
                    (x["manuscript_id"], rid)
                    for x in locks for rid in x["reviewer_ids"]
                })
                inc = client.post("/assign", json=req_body).json()
                if (
                    inc["status"] == "infeasible"
                    and inc["locked_conflicts"] is None
                ):
                    _assert_cut_invariants(inc, locked_total=locked_n)
                    checked["incremental"] += 1
    assert checked["normal"] > 0
    assert checked["incremental"] > 0

