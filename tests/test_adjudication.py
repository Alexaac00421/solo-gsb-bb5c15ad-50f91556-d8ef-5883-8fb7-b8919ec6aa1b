"""冲突证据裁决测试：维持现记录 / 采纳迟到原文、幂等与改判拒绝、
乐观修订号、采纳即下一修订且保留原文历史、封存固化、完成后不得首次裁决。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor


def _create_drill(client, terminals=("T",)):
    r = client.post("/api/drills", json={"name": "裁决演练", "terminals": list(terminals)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _report(client, drill, terminal, seq, content, *, occurred_at=None,
            event_type="evt"):
    return client.post(
        f"/api/drills/{drill}/terminals/{terminal}/reports",
        json={"entries": [{"seq": seq,
                           "occurred_at": occurred_at or f"2026-10-05T09:{seq:02d}:00Z",
                           "event_type": event_type, "content": content}]},
    )


def _make_conflict(client, drill, terminal="T", seq=1,
                   original="原始内容", incoming="迟到原文"):
    assert _report(client, drill, terminal, seq, original).json()["counts"]["accepted"] == 1
    r = _report(client, drill, terminal, seq, incoming)
    assert r.json()["counts"]["conflict"] == 1
    return r.json()["results"][0]["conflict_id"]


def _adjudicate(client, drill, conflict_id, decision, reason, *,
                decided_by="演练负责人", expected_rev=None):
    payload = {"decision": decision, "reason": reason, "decided_by": decided_by}
    if expected_rev is not None:
        payload["expected_rev"] = expected_rev
    return client.post(
        f"/api/drills/{drill}/conflicts/{conflict_id}/adjudications", json=payload
    )


# ---------------------------------------------------------------- 维持现记录

def test_keep_current_does_not_change_record(client):
    drill = _create_drill(client)
    cid = _make_conflict(client, drill)

    r = _adjudicate(client, drill, cid, "keep_current", "经核查现记录为现场确认版本")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["adjudication"]["decision"] == "keep_current"
    assert body["already_adjudicated"] is False
    assert body["record"]["content"] == "原始内容"
    assert body["record"]["rev"] == 0
    assert [rv["rev"] for rv in body["record"]["revisions"]] == [0]

    # 当前时间线保持原样
    tl = client.get(f"/api/drills/{drill}/timeline").json()["records"]
    assert tl[0]["content"] == "原始内容" and tl[0]["rev"] == 0


# ---------------------------------------------------------------- 采纳迟到原文

def test_adopt_incoming_creates_next_revision_and_keeps_history(client):
    drill = _create_drill(client)
    cid = _make_conflict(client, drill, incoming="迟到原文")

    # 未提交 expected_rev => 400
    r = _adjudicate(client, drill, cid, "adopt_incoming", "迟到终端数据可信")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_expected_rev"

    r = _adjudicate(client, drill, cid, "adopt_incoming", "迟到终端数据可信",
                    expected_rev=0)
    assert r.status_code == 201, r.text
    body = r.json()
    adj = body["adjudication"]
    assert adj["decision"] == "adopt_incoming"
    assert adj["current_rev_before"] == 0
    assert adj["applied_rev"] == 1
    assert adj["revision_created"] is True
    assert adj["expected_rev"] == 0

    rec = body["record"]
    assert rec["content"] == "迟到原文"
    assert rec["rev"] == 1
    assert [rv["rev"] for rv in rec["revisions"]] == [0, 1]
    # 原文与历史修订保留
    assert rec["revisions"][0]["content"] == "原始内容"
    assert rec["revisions"][0]["is_original"] is True
    assert rec["revisions"][1]["content"] == "迟到原文"
    assert "迟到终端数据可信" in rec["revisions"][1]["reason"]
    assert rec["revisions"][1]["revised_by"] == "演练负责人"

    # 记录视图一致
    r = client.get(f"/api/drills/{drill}/records/T/1")
    assert r.json()["content"] == "迟到原文"
    assert [rv["rev"] for rv in r.json()["revisions"]] == [0, 1]


def test_adopt_when_incoming_already_equals_current_records_only_no_empty_revision(client):
    drill = _create_drill(client)
    # 先发生冲突（rev=0 内容为 v1，迟到为 v2）
    cid = _make_conflict(client, drill, original="v1", incoming="v2")
    # 期间记录被人工更正为与迟到原文一致
    r = client.put(f"/api/drills/{drill}/records/T/1",
                   json={"content": "v2", "reason": "人工复核后修订", "revised_by": "审核员"})
    assert r.status_code == 200 and r.json()["rev"] == 1

    # expected_rev 必须是所见当前修订号；提交旧的 0 => revision_conflict
    r = _adjudicate(client, drill, cid, "adopt_incoming", "迟到原文与现版本一致",
                    expected_rev=0)
    assert r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict"

    # 提交当前修订号 1：迟到原文与当前内容一致，只记裁决，不产生空修订
    r = _adjudicate(client, drill, cid, "adopt_incoming", "迟到原文与现版本一致",
                    expected_rev=1)
    assert r.status_code == 201, r.text
    adj = r.json()["adjudication"]
    assert adj["revision_created"] is False
    assert adj["current_rev_before"] == 1
    assert adj["applied_rev"] == 1
    rec = r.json()["record"]
    assert rec["rev"] == 1
    assert [rv["rev"] for rv in rec["revisions"]] == [0, 1]
    assert rec["content"] == "v2"


def test_adopt_stale_expected_rev_after_correction_returns_conflict_and_keeps_new_version(client):
    drill = _create_drill(client)
    cid = _make_conflict(client, drill, original="v1", incoming="v2")

    # 期间记录被更正为 v3
    r = client.put(f"/api/drills/{drill}/records/T/1",
                   json={"content": "v3", "reason": "现场二次校核", "revised_by": "值班长"})
    assert r.status_code == 200 and r.json()["rev"] == 1

    # 带着旧修订号采纳 => 409 revision_conflict，不能用证据旧内容覆盖新版本
    r = _adjudicate(client, drill, cid, "adopt_incoming", "按旧裁决采纳", expected_rev=0)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"] == {"expected_rev": 0, "current_rev": 1}

    # 裁决未落下：冲突仍为待裁决，记录保持 v3
    r = client.get(f"/api/drills/{drill}/conflicts/{cid}")
    assert r.json()["adjudication"] is None
    rec = client.get(f"/api/drills/{drill}/records/T/1").json()
    assert rec["content"] == "v3" and rec["rev"] == 1

    # 基于当前 rev=1 重新裁决采纳，v2 成为下一修订（rev=2）而非覆盖回旧版本
    r = _adjudicate(client, drill, cid, "adopt_incoming", "复核后仍采纳迟到原文",
                    expected_rev=1)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["adjudication"]["applied_rev"] == 2
    rec = body["record"]
    assert rec["content"] == "v2" and rec["rev"] == 2
    assert [rv["content"] for rv in rec["revisions"]] == ["v1", "v3", "v2"]


# ---------------------------------------------------------------- 幂等与改判

def test_retry_same_decision_and_reason_returns_original(client):
    drill = _create_drill(client)
    cid = _make_conflict(client, drill)
    r1 = _adjudicate(client, drill, cid, "keep_current", "同一理由")
    assert r1.status_code == 201

    r2 = _adjudicate(client, drill, cid, "keep_current", "同一理由",
                     decided_by="另一个操作者")
    assert r2.status_code == 200, r2.text
    assert r2.json()["already_adjudicated"] is True
    # 返回原裁决（原操作者），不产生新裁决/新版本
    assert r2.json()["adjudication"]["id"] == r1.json()["adjudication"]["id"]
    assert r2.json()["adjudication"]["decided_by"] == "演练负责人"


def test_change_decision_or_reason_is_rejected(client):
    drill = _create_drill(client)
    cid = _make_conflict(client, drill)
    assert _adjudicate(client, drill, cid, "keep_current", "理由A").status_code == 201

    # 改判
    r = _adjudicate(client, drill, cid, "adopt_incoming", "理由A", expected_rev=0)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict_already_adjudicated"
    # 改理由
    r = _adjudicate(client, drill, cid, "keep_current", "理由B")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict_already_adjudicated"

    # 原裁决仍然有效
    r = client.get(f"/api/drills/{drill}/conflicts/{cid}")
    adj = r.json()["adjudication"]
    assert adj["decision"] == "keep_current" and adj["reason"] == "理由A"


def test_invalid_adjudication_payload(client):
    drill = _create_drill(client)
    cid = _make_conflict(client, drill)
    url = f"/api/drills/{drill}/conflicts/{cid}/adjudications"

    r = client.post(url, json={"decision": "whatever", "reason": "x", "decided_by": "y"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_decision"
    r = client.post(url, json={"decision": "keep_current", "reason": "  ",
                               "decided_by": "y"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_reason"
    r = client.post(url, json={"decision": "keep_current", "reason": "x",
                               "decided_by": ""})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_decided_by"
    r = client.post(url, json={"decision": "adopt_incoming", "reason": "x",
                               "decided_by": "y", "expected_rev": -1})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_expected_rev"


# ---------------------------------------------------------------- 查询与跨演练

def test_conflicts_list_carries_adjudication_and_scoped_lookup(client):
    d1 = _create_drill(client)
    c1 = _make_conflict(client, d1)
    _adjudicate(client, d1, c1, "keep_current", "维持")

    r = client.get(f"/api/drills/{d1}/conflicts")
    item = next(c for c in r.json()["conflicts"] if c["id"] == c1)
    assert item["adjudication"]["decision"] == "keep_current"

    # 单条查询
    r = client.get(f"/api/drills/{d1}/conflicts/{c1}")
    assert r.status_code == 200
    assert r.json()["conflict"]["id"] == c1
    assert r.json()["adjudication"]["reason"] == "维持"

    # 不存在
    r = client.get(f"/api/drills/{d1}/conflicts/cfl_nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "conflict_not_found"


def test_adjudication_scoped_to_drill(client):
    d1 = _create_drill(client)
    d2 = _create_drill(client, terminals=("T",))
    c1 = _make_conflict(client, d1)

    # 拿 d1 的证据到 d2 下裁决 => 404 cross_drill_conflict
    r = _adjudicate(client, d2, c1, "keep_current", "跨演练裁决")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "cross_drill_conflict"
    # 查询同样限定在本演练
    r = client.get(f"/api/drills/{d2}/conflicts/{c1}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "conflict_not_found"
    # 原证据未被裁决
    assert client.get(f"/api/drills/{d1}/conflicts/{c1}").json()["adjudication"] is None


# ---------------------------------------------------------------- 封存

def test_existing_seals_unchanged_later_seals_include_adjudication_revision(client):
    drill = _create_drill(client)
    cid = _make_conflict(client, drill, original="v1", incoming="v2")

    # 裁决前封存 v1（无缺号，单终端 1 号）
    r = client.post(f"/api/drills/{drill}/seals", json={"note": "裁决前封存"})
    assert r.status_code == 201
    assert r.json()["records"][0]["content"] == "v1"
    assert r.json()["records"][0]["rev_at_seal"] == 0

    # 采纳迟到原文：rev 0 -> 1
    r = _adjudicate(client, drill, cid, "adopt_incoming", "采纳迟到数据", expected_rev=0)
    assert r.status_code == 201

    # 既有封存版本不变
    v1 = client.get(f"/api/drills/{drill}/seals/1").json()
    assert v1["records"][0]["content"] == "v1"
    assert v1["records"][0]["rev_at_seal"] == 0
    assert [rv["rev"] for rv in v1["records"][0]["revisions"]] == [0]

    # 后续封存包含裁决产生的修订结果与完整历史
    r = client.post(f"/api/drills/{drill}/seals", json={"note": "裁决后封存"})
    assert r.status_code == 201
    v2 = r.json()
    assert v2["version"] == 2
    rec = v2["records"][0]
    assert rec["content"] == "v2" and rec["rev_at_seal"] == 1
    assert [rv["rev"] for rv in rec["revisions"]] == [0, 1]
    assert rec["revisions"][0]["content"] == "v1"


# ---------------------------------------------------------------- 完成语义

def test_no_first_adjudication_after_completion_but_retry_still_works(client):
    drill = _create_drill(client)
    c1 = _make_conflict(client, drill, seq=1, original="v1", incoming="v2")
    c2 = _make_conflict(client, drill, seq=2, original="w1", incoming="w2")

    # c1 先裁决（维持）
    assert _adjudicate(client, drill, c1, "keep_current", "维持").status_code == 201
    # 完成演练（无缺号）
    r = client.post(f"/api/drills/{drill}/complete",
                    json={"note": "收官", "completed_by": "张指挥"})
    assert r.status_code == 201, r.text

    # 完成后对未裁决的 c2 首次裁决被拒
    r = _adjudicate(client, drill, c2, "keep_current", "完成后裁决")
    assert r.status_code == 409 and r.json()["error"]["code"] == "drill_completed"
    r = _adjudicate(client, drill, c2, "adopt_incoming", "完成后采纳", expected_rev=0)
    assert r.status_code == 409 and r.json()["error"]["code"] == "drill_completed"
    assert client.get(f"/api/drills/{drill}/conflicts/{c2}").json()["adjudication"] is None

    # 已裁决的 c1 以相同结论和理由重试，仍返回原结果（幂等）
    r = _adjudicate(client, drill, c1, "keep_current", "维持")
    assert r.status_code == 200
    assert r.json()["already_adjudicated"] is True
    assert r.json()["adjudication"]["decision"] == "keep_current"


# ---------------------------------------------------------------- 并发

def test_concurrent_adjudications_of_same_conflict_exactly_one_first_decision(client):
    drill = _create_drill(client)
    cid = _make_conflict(client, drill, incoming="v2")
    url = f"/api/drills/{drill}/conflicts/{cid}/adjudications"
    payload = {"decision": "adopt_incoming", "reason": "并发采纳",
               "decided_by": "负责人", "expected_rev": 0}

    def post(_):
        return client.post(url, json=payload)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(post, range(12)))

    statuses = sorted(r.status_code for r in results)
    assert statuses[-1] == 201
    assert all(s == 200 for s in statuses[:-1])
    bodies = [r.json() for r in results]
    first = next(b for b in bodies if b["already_adjudicated"] is False)
    assert all(not b["already_adjudicated"] for b in [first])
    assert all(
        b["adjudication"]["id"] == first["adjudication"]["id"]
        and b["already_adjudicated"] is True
        for b in bodies if b is not first
    )
    # 裁决与修订原子生效：恰有一个修订 rev=1
    rec = client.get(f"/api/drills/{drill}/records/T/1").json()
    assert rec["rev"] == 1 and rec["content"] == "v2"
    assert [rv["rev"] for rv in rec["revisions"]] == [0, 1]
