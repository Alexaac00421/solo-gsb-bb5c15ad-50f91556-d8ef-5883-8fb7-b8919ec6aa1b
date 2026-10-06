"""冲突证据裁决（adjudication）端到端测试。

语义要点：
- 演练负责人以冲突 ID 提交裁决：维持现记录（keep）或采纳迟到原文（adopt），
  必须附非空理由与操作者；结果可按演练连同证据查询；
- 采纳须提交所见当前修订号 seen_rev；期间记录被更正（修订号不一致）时返回
  409 revision_mismatch 且不落裁决，不能用证据里的旧内容覆盖新版本；
- 采纳成功把证据所存迟到原文写为下一修订（原文与历史修订保留）；迟到原文已与
  当前内容一致时只记裁决、不产生空修订；维持不改记录；
- 同一证据以相同结论和理由重试幂等返回原结果；改判或改理由拒绝；
- 仅限本演练证据；演练完成后不得首次裁决（既有裁决的幂等重试仍返回原结果）；
- 裁决与修订原子生效：已有封存版本不变，后续封存包含修订结果。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor


def _create_drill(client, terminals):
    r = client.post("/api/drills", json={"name": "裁决测试演练", "terminals": terminals})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _report(client, drill, term, seq, content, ts="2026-10-05T09:00:00Z", etype="evt"):
    r = client.post(
        f"/api/drills/{drill}/terminals/{term}/reports",
        json={"entries": [{"seq": seq, "occurred_at": ts, "event_type": etype,
                           "content": content}]},
    )
    assert r.status_code == 200, r.text
    return r.json()["results"][0]


def _make_conflict(client, drill, term, seq, original, late):
    """上报原文，再以同序号不同内容制造一份冲突证据，返回 conflict_id。"""
    assert _report(client, drill, term, seq, original)["status"] == "accepted"
    result = _report(client, drill, term, seq, late)
    assert result["status"] == "conflict"
    return result["conflict_id"]


def _adjudicate(client, drill, conflict_id, **payload):
    return client.post(
        f"/api/drills/{drill}/conflicts/{conflict_id}/adjudications", json=payload
    )


def test_keep_adjudication_idempotent_and_queryable(client):
    drill = _create_drill(client, ["A1"])
    conflict_id = _make_conflict(client, drill, "A1", 1, "原始内容", "迟到原文")

    # 维持现记录：记录不变，裁决落库
    r = _adjudicate(client, drill, conflict_id,
                    decision="keep", reason="现记录已与现场核实", operator="演练负责人")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["already_adjudicated"] is False
    adj = body["adjudication"]
    assert adj["decision"] == "keep"
    assert adj["applied_rev"] is None and adj["revision_created"] is False
    assert adj["terminal_code"] == "A1" and adj["seq"] == 1
    assert adj["operator"] == "演练负责人" and adj["reason"] == "现记录已与现场核实"

    rec = client.get(f"/api/drills/{drill}/records/A1/1").json()
    assert rec["rev"] == 0 and rec["content"] == "原始内容"
    assert len(rec["revisions"]) == 1

    # 相同结论和理由重试 => 幂等返回原结果（首次裁决的内容原样返回）
    r = _adjudicate(client, drill, conflict_id,
                    decision="keep", reason="现记录已与现场核实", operator="别人重试")
    assert r.status_code == 200, r.text
    again = r.json()
    assert again["already_adjudicated"] is True
    assert again["adjudication"]["id"] == adj["id"]
    assert again["adjudication"]["operator"] == "演练负责人"

    # 改判或改理由 => 拒绝，不覆盖既有裁决
    r = _adjudicate(client, drill, conflict_id,
                    decision="adopt", reason="现记录已与现场核实",
                    operator="演练负责人", seen_rev=0)
    assert r.status_code == 409 and r.json()["error"]["code"] == "adjudication_exists"
    r = _adjudicate(client, drill, conflict_id,
                    decision="keep", reason="换一个理由", operator="演练负责人")
    assert r.status_code == 409 and r.json()["error"]["code"] == "adjudication_exists"

    # 冲突证据列表内联裁决结果；裁决结果可按演练连同证据查询
    conflicts = client.get(f"/api/drills/{drill}/conflicts").json()["conflicts"]
    c0 = next(c for c in conflicts if c["id"] == conflict_id)
    assert c0["adjudication"]["id"] == adj["id"]
    assert c0["adjudication"]["decision"] == "keep"

    listing = client.get(f"/api/drills/{drill}/adjudications").json()
    assert listing["drill_id"] == drill
    assert len(listing["adjudications"]) == 1
    item = listing["adjudications"][0]
    assert item["id"] == adj["id"]
    assert item["conflict"]["id"] == conflict_id
    assert "迟到原文" in item["conflict"]["incoming"]
    assert "原始内容" in item["conflict"]["existing"]


def test_adopt_writes_next_revision_preserving_history(client):
    drill = _create_drill(client, ["A2"])
    conflict_id = _make_conflict(client, drill, "A2", 1, "原始内容", "迟到原文")
    # 先更正一次（rev 0 -> 1），再采纳迟到原文
    r = client.put(f"/api/drills/{drill}/records/A2/1",
                   json={"content": "更正版", "reason": "笔误", "revised_by": "审核员"})
    assert r.status_code == 200 and r.json()["rev"] == 1

    r = _adjudicate(client, drill, conflict_id,
                    decision="adopt", reason="迟到的才是现场原报", operator="演练负责人",
                    seen_rev=1)
    assert r.status_code == 201, r.text
    adj = r.json()["adjudication"]
    assert adj["decision"] == "adopt"
    assert adj["seen_rev"] == 1
    assert adj["applied_rev"] == 2
    assert adj["revision_created"] is True

    # 迟到原文写为下一修订；原文与历次修订全部保留
    rec = client.get(f"/api/drills/{drill}/records/A2/1").json()
    assert rec["rev"] == 2 and rec["content"] == "迟到原文"
    assert [(rv["rev"], rv["content"]) for rv in rec["revisions"]] == [
        (0, "原始内容"), (1, "更正版"), (2, "迟到原文"),
    ]
    assert rec["revisions"][2]["reason"] == "迟到的才是现场原报"
    assert rec["revisions"][2]["revised_by"] == "演练负责人"


def test_adopt_requires_current_seen_rev(client):
    drill = _create_drill(client, ["A3"])
    conflict_id = _make_conflict(client, drill, "A3", 1, "原始内容", "迟到原文")

    # 采纳缺少所见修订号
    r = _adjudicate(client, drill, conflict_id,
                    decision="adopt", reason="采纳", operator="负责人")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_seen_rev"

    # 期间记录被更正：所见修订号过期 => 409 且不落裁决
    client.put(f"/api/drills/{drill}/records/A3/1",
               json={"content": "更正版", "reason": "笔误"})
    r = _adjudicate(client, drill, conflict_id,
                    decision="adopt", reason="采纳", operator="负责人", seen_rev=0)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "revision_mismatch"
    assert err["details"]["current_rev"] == 1 and err["details"]["seen_rev"] == 0

    # 未落裁决：证据仍未裁决，记录未被证据里的旧内容覆盖
    conflicts = client.get(f"/api/drills/{drill}/conflicts").json()["conflicts"]
    assert next(c for c in conflicts if c["id"] == conflict_id)["adjudication"] is None
    assert client.get(f"/api/drills/{drill}/adjudications").json()["adjudications"] == []
    rec = client.get(f"/api/drills/{drill}/records/A3/1").json()
    assert rec["rev"] == 1 and rec["content"] == "更正版"

    # 基于最新修订号重新采纳 => 成功
    r = _adjudicate(client, drill, conflict_id,
                    decision="adopt", reason="采纳", operator="负责人", seen_rev=1)
    assert r.status_code == 201
    rec = client.get(f"/api/drills/{drill}/records/A3/1").json()
    assert rec["rev"] == 2 and rec["content"] == "迟到原文"


def test_adopt_matching_content_records_no_empty_revision(client):
    drill = _create_drill(client, ["A4"])
    conflict_id = _make_conflict(client, drill, "A4", 1, "原始内容", "迟到原文")
    # 把记录更正为与迟到原文完全一致（同时间/类型/内容）
    r = client.put(f"/api/drills/{drill}/records/A4/1",
                   json={"occurred_at": "2026-10-05T09:00:00Z", "event_type": "evt",
                         "content": "迟到原文", "reason": "按迟到内容更正"})
    assert r.status_code == 200 and r.json()["rev"] == 1

    r = _adjudicate(client, drill, conflict_id,
                    decision="adopt", reason="迟到原文已与现记录一致", operator="负责人",
                    seen_rev=1)
    assert r.status_code == 201, r.text
    adj = r.json()["adjudication"]
    assert adj["revision_created"] is False
    assert adj["applied_rev"] is None

    # 只记裁决，不产生空修订
    rec = client.get(f"/api/drills/{drill}/records/A4/1").json()
    assert rec["rev"] == 1 and len(rec["revisions"]) == 2


def test_multiple_conflicts_same_record_adjudicated_independently(client):
    drill = _create_drill(client, ["A5"])
    assert _report(client, drill, "A5", 1, "原始内容")["status"] == "accepted"
    c1 = _report(client, drill, "A5", 1, "迟到甲")["conflict_id"]
    c2 = _report(client, drill, "A5", 1, "迟到乙")["conflict_id"]

    r = _adjudicate(client, drill, c1, decision="adopt", reason="甲为准",
                    operator="负责人", seen_rev=0)
    assert r.status_code == 201 and r.json()["adjudication"]["applied_rev"] == 1

    # 第二份证据须基于新的当前修订号采纳（旧修订号被拒绝）
    r = _adjudicate(client, drill, c2, decision="adopt", reason="乙更准",
                    operator="负责人", seen_rev=0)
    assert r.status_code == 409 and r.json()["error"]["code"] == "revision_mismatch"
    r = _adjudicate(client, drill, c2, decision="adopt", reason="乙更准",
                    operator="负责人", seen_rev=1)
    assert r.status_code == 201 and r.json()["adjudication"]["applied_rev"] == 2

    rec = client.get(f"/api/drills/{drill}/records/A5/1").json()
    assert rec["rev"] == 2 and rec["content"] == "迟到乙"
    assert [rv["content"] for rv in rec["revisions"]] == ["原始内容", "迟到甲", "迟到乙"]


def test_adjudication_validation_and_scoping(client):
    drill = _create_drill(client, ["A6"])
    other = _create_drill(client, ["A6"])
    conflict_id = _make_conflict(client, drill, "A6", 1, "原始内容", "迟到原文")

    # 非法结论 / 空理由 / 空操作者
    r = _adjudicate(client, drill, conflict_id,
                    decision="overwrite", reason="r", operator="o")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_decision"
    r = _adjudicate(client, drill, conflict_id,
                    decision="keep", reason="   ", operator="o")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_reason"
    r = _adjudicate(client, drill, conflict_id,
                    decision="keep", reason="r", operator="  ")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_operator"

    # 仅限本演练证据：跨演练的冲突 ID 不可裁决
    r = _adjudicate(client, other, conflict_id,
                    decision="keep", reason="r", operator="o")
    assert r.status_code == 404 and r.json()["error"]["code"] == "conflict_not_found"
    r = _adjudicate(client, "drill_nope", conflict_id,
                    decision="keep", reason="r", operator="o")
    assert r.status_code == 404 and r.json()["error"]["code"] == "drill_not_found"
    r = _adjudicate(client, drill, "cfl_nope",
                    decision="keep", reason="r", operator="o")
    assert r.status_code == 404 and r.json()["error"]["code"] == "conflict_not_found"

    # 上述失败均未落裁决
    assert client.get(f"/api/drills/{drill}/adjudications").json()["adjudications"] == []


def test_first_adjudication_rejected_after_completion(client):
    drill = _create_drill(client, ["A7"])
    assert _report(client, drill, "A7", 1, "原始内容")["status"] == "accepted"
    c1 = _report(client, drill, "A7", 1, "迟到原文甲")["conflict_id"]
    c2 = _report(client, drill, "A7", 1, "迟到原文乙")["conflict_id"]

    # 完成前裁决 c1
    r = _adjudicate(client, drill, c1, decision="keep", reason="定稿", operator="负责人")
    assert r.status_code == 201

    r = client.post(f"/api/drills/{drill}/complete", json={"completed_by": "负责人"})
    assert r.status_code == 201, r.text

    # 完成后不得首次裁决
    r = _adjudicate(client, drill, c2, decision="keep", reason="定稿", operator="负责人")
    assert r.status_code == 409 and r.json()["error"]["code"] == "drill_completed"

    # 既有裁决的幂等重试仍返回原结果；改理由仍拒绝
    r = _adjudicate(client, drill, c1, decision="keep", reason="定稿", operator="负责人")
    assert r.status_code == 200 and r.json()["already_adjudicated"] is True
    r = _adjudicate(client, drill, c1, decision="keep", reason="改理由", operator="负责人")
    assert r.status_code == 409 and r.json()["error"]["code"] == "adjudication_exists"

    # 裁决结果在完成后的演练上仍可读
    listing = client.get(f"/api/drills/{drill}/adjudications").json()["adjudications"]
    assert len(listing) == 1 and listing[0]["conflict_id"] == c1


def test_adjudication_atomic_with_revision_and_seal_versions(client):
    drill = _create_drill(client, ["A8"])
    conflict_id = _make_conflict(client, drill, "A8", 1, "原始内容", "迟到原文")

    # 封存 v1：采纳前的状态
    r = client.post(f"/api/drills/{drill}/seals", json={"note": "首封"})
    assert r.status_code == 201
    assert r.json()["records"][0]["content"] == "原始内容"

    # 采纳 => 裁决与新修订原子生效
    r = _adjudicate(client, drill, conflict_id,
                    decision="adopt", reason="迟到原文为准", operator="负责人", seen_rev=0)
    assert r.status_code == 201 and r.json()["adjudication"]["applied_rev"] == 1

    # 已有封存版本不变
    v1 = client.get(f"/api/drills/{drill}/seals/1").json()
    rec_v1 = v1["records"][0]
    assert rec_v1["content"] == "原始内容" and rec_v1["rev_at_seal"] == 0
    assert [rv["content"] for rv in rec_v1["revisions"]] == ["原始内容"]

    # 后续封存包含修订结果
    r = client.post(f"/api/drills/{drill}/seals", json={"note": "第二封"})
    assert r.status_code == 201
    rec_v2 = r.json()["records"][0]
    assert rec_v2["content"] == "迟到原文" and rec_v2["rev_at_seal"] == 1
    assert [rv["content"] for rv in rec_v2["revisions"]] == ["原始内容", "迟到原文"]


def test_concurrent_adjudication_single_winner(client):
    drill = _create_drill(client, ["A9"])
    conflict_id = _make_conflict(client, drill, "A9", 1, "原始内容", "迟到原文")

    def adjudicate(_):
        return _adjudicate(client, drill, conflict_id,
                           decision="keep", reason="并发同一理由", operator="负责人")

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(adjudicate, range(12)))

    assert all(r.status_code in (200, 201) for r in responses)
    bodies = [r.json() for r in responses]
    # 恰好一个请求真正落裁决，其余幂等命中同一裁决
    assert sum(1 for b in bodies if not b["already_adjudicated"]) == 1
    assert len({b["adjudication"]["id"] for b in bodies}) == 1
    listing = client.get(f"/api/drills/{drill}/adjudications").json()["adjudications"]
    assert len(listing) == 1


def test_concurrent_conflicting_adjudications_only_first_wins(client):
    drill = _create_drill(client, ["A10"])
    conflict_id = _make_conflict(client, drill, "A10", 1, "原始内容", "迟到原文")

    def adjudicate(i):
        return _adjudicate(client, drill, conflict_id,
                           decision="keep", reason=f"理由-{i}", operator="负责人")

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(adjudicate, range(12)))

    # 不同理由互斥：恰好一个成功落裁决，其余 409 adjudication_exists
    assert sum(1 for r in responses if r.status_code == 201) == 1
    assert sum(
        1 for r in responses
        if r.status_code == 409 and r.json()["error"]["code"] == "adjudication_exists"
    ) == 11
    listing = client.get(f"/api/drills/{drill}/adjudications").json()["adjudications"]
    assert len(listing) == 1
