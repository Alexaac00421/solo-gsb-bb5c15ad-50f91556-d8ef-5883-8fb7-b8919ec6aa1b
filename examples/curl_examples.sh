#!/usr/bin/env bash
# 应急演练记录汇聚服务 —— curl 调用示例
# 前置：服务已在 http://127.0.0.1:8080 启动（docker compose up 或本地 uvicorn）
set -euo pipefail

BASE="${BASE_URL:-http://127.0.0.1:8080}"
ctype='Content-Type: application/json'

echo "## 0. 健康检查"
curl -s "$BASE/health"; echo

echo "## 1. 建演练并随演登记终端 T1，再补登 T2/T3"
DID=$(curl -s -X POST "$BASE/api/drills" -H "$ctype" \
  -d '{"name":"2026-10 防汛应急演练","terminals":["T1"]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "drill_id=$DID"
curl -s -X POST "$BASE/api/drills/$DID/terminals" -H "$ctype" \
  -d '{"terminals":[{"code":"T2","name":"堤岸监测点"},{"code":"T3","name":"指挥车"}]}'; echo

echo "## 2. T1 批量上报（本地序号 1,2,3）"
curl -s -X POST "$BASE/api/drills/$DID/terminals/T1/reports" -H "$ctype" -d '{
  "entries": [
    {"seq": 1, "occurred_at": "2026-10-05T09:00:00Z", "event_type": "login", "content": "终端上线自检通过"},
    {"seq": 2, "occurred_at": "2026-10-05T09:10:00Z", "event_type": "water_level", "content": "水位 21.4m，低于警戒"},
    {"seq": 3, "occurred_at": "2026-10-05T09:20:00Z", "event_type": "patrol", "content": "完成第一轮堤段巡查"}
  ]
}'; echo

echo "## 3. 幂等：同终端同序号、完整内容完全一致 => duplicate（不产生新数据）"
curl -s -X POST "$BASE/api/drills/$DID/terminals/T1/reports" -H "$ctype" -d '{
  "entries": [{"seq": 1, "occurred_at": "2026-10-05T09:00:00Z", "event_type": "login", "content": "终端上线自检通过"}]
}'; echo

echo "## 4. 冲突：同序号但内容不同 => conflict，冲突证据落库可查"
curl -s -X POST "$BASE/api/drills/$DID/terminals/T1/reports" -H "$ctype" -d '{
  "entries": [{"seq": 2, "occurred_at": "2026-10-05T09:10:00Z", "event_type": "water_level", "content": "水位 21.4m，已超警戒（被篡改）"}]
}'; echo
curl -s "$BASE/api/drills/$DID/conflicts"; echo

echo "## 5. 当前时间线（发生时间->终端标识->序号 稳定排序）与显式缺号"
curl -s "$BASE/api/drills/$DID/timeline"; echo
curl -s "$BASE/api/drills/$DID/gaps"; echo

echo "## 6. 带理由更正 T1#3，原文与历次修订均可查询"
curl -s -X PUT "$BASE/api/drills/$DID/records/T1/3" -H "$ctype" -d '{
  "content": "完成第一轮堤段巡查（东段补巡 200m）",
  "reason": "录入时遗漏东段补巡里程",
  "revised_by": "值班长"
}'; echo
curl -s "$BASE/api/drills/$DID/records/T1/3"; echo

echo "## 7. 处置依赖：建立 T1#1->T1#2、T1#2->T1#3（附非空依据）；幂等/校验/查询"
curl -s -X POST "$BASE/api/drills/$DID/dependencies" -H "$ctype" -d '{
  "predecessor": {"terminal_code": "T1", "seq": 1},
  "successor":   {"terminal_code": "T1", "seq": 2},
  "reason": "终端上线自检通过后，水位数据方可采信",
  "created_by": "现场指挥"
}'; echo
curl -s -X POST "$BASE/api/drills/$DID/dependencies" -H "$ctype" -d '{
  "predecessor": {"terminal_code": "T1", "seq": 2},
  "successor":   {"terminal_code": "T1", "seq": 3},
  "reason": "水位确认后才启动堤段巡查"
}'; echo
echo "-- 重复提交（幂等，already_exists: true）"
curl -s -X POST "$BASE/api/drills/$DID/dependencies" -H "$ctype" -d '{
  "predecessor": {"terminal_code": "T1", "seq": 1},
  "successor":   {"terminal_code": "T1", "seq": 2},
  "reason": "终端上线自检通过后，水位数据方可采信"
}'; echo
echo "-- 成环 T1#3->T1#1 => 409 dependency_cycle"
curl -s -X POST "$BASE/api/drills/$DID/dependencies" -H "$ctype" -d '{
  "predecessor": {"terminal_code": "T1", "seq": 3},
  "successor":   {"terminal_code": "T1", "seq": 1},
  "reason": "试图成环"
}'; echo
echo "-- 缺号端点 T2#1 => 404 record_not_found"
curl -s -X POST "$BASE/api/drills/$DID/dependencies" -H "$ctype" -d '{
  "predecessor": {"terminal_code": "T2", "seq": 1},
  "successor":   {"terminal_code": "T1", "seq": 1},
  "reason": "缺号不能作为端点"
}'; echo
echo "-- T1#3 依赖视图：直接前置/后续 + 沿前置方向完整链"
curl -s "$BASE/api/drills/$DID/records/T1/3/dependencies"; echo
curl -s "$BASE/api/drills/$DID/dependencies"; echo

echo "## 8. T2 只报了序号 2 => 缺号 1 未闭合时封存被拒绝（409）"
curl -s -X POST "$BASE/api/drills/$DID/terminals/T2/reports" -H "$ctype" -d '{
  "entries": [{"seq": 2, "occurred_at": "2026-10-05T09:15:00Z", "event_type": "water_level", "content": "水位 21.6m"}]
}'; echo
curl -s -X POST "$BASE/api/drills/$DID/seals" -H "$ctype" -d '{"note":"尝试封存"}'; echo

echo "## 9. 冲突裁决：维持 / 采纳 / 幂等 / 修订号防护"
CFL_T1=$(curl -s "$BASE/api/drills/$DID/conflicts" | python3 -c '
import sys, json
cs = json.load(sys.stdin)["conflicts"]
print(next(c["id"] for c in cs if c["terminal_code"] == "T1" and c["seq"] == 2))')
echo "-- 维持现记录（keep，201）"
curl -s -X POST "$BASE/api/drills/$DID/conflicts/$CFL_T1/adjudications" -H "$ctype" -d '{
  "decision": "keep", "reason": "现记录已与堤岸监测点电话核实，维持不变", "operator": "演练负责人"
}'; echo
echo "-- 相同结论和理由重试（幂等，200 already_adjudicated: true）"
curl -s -X POST "$BASE/api/drills/$DID/conflicts/$CFL_T1/adjudications" -H "$ctype" -d '{
  "decision": "keep", "reason": "现记录已与堤岸监测点电话核实，维持不变", "operator": "演练负责人"
}'; echo
echo "-- 改判 => 409 adjudication_exists"
curl -s -X POST "$BASE/api/drills/$DID/conflicts/$CFL_T1/adjudications" -H "$ctype" -d '{
  "decision": "adopt", "reason": "试图改判", "operator": "演练负责人", "seen_rev": 0
}'; echo
echo "-- 制造 T2#2 冲突，并把记录更正一版（rev 0 -> 1）"
curl -s -X POST "$BASE/api/drills/$DID/terminals/T2/reports" -H "$ctype" -d '{
  "entries": [{"seq": 2, "occurred_at": "2026-10-05T09:15:00Z", "event_type": "water_level", "content": "水位 21.7m（迟到原文）"}]
}'; echo
CFL_T2=$(curl -s "$BASE/api/drills/$DID/conflicts" | python3 -c '
import sys, json
cs = json.load(sys.stdin)["conflicts"]
print(next(c["id"] for c in cs if c["terminal_code"] == "T2" and c["seq"] == 2))')
curl -s -X PUT "$BASE/api/drills/$DID/records/T2/2" -H "$ctype" -d '{
  "content": "水位 21.65m（复核值）", "reason": "与相邻断面交叉复核", "revised_by": "值班长"
}'; echo
echo "-- 用过期的所见修订号采纳 => 409 revision_mismatch（不落裁决）"
curl -s -X POST "$BASE/api/drills/$DID/conflicts/$CFL_T2/adjudications" -H "$ctype" -d '{
  "decision": "adopt", "reason": "采纳迟到原文", "operator": "演练负责人", "seen_rev": 0
}'; echo
CUR_REV=$(curl -s "$BASE/api/drills/$DID/records/T2/2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["rev"])')
echo "-- 以当前修订号 seen_rev=$CUR_REV 重新采纳 => 迟到原文写为下一修订"
curl -s -X POST "$BASE/api/drills/$DID/conflicts/$CFL_T2/adjudications" -H "$ctype" -d "{
  \"decision\": \"adopt\", \"reason\": \"迟到原文为监测点原始读数，予以采纳\",
  \"operator\": \"演练负责人\", \"seen_rev\": $CUR_REV
}"; echo
curl -s "$BASE/api/drills/$DID/records/T2/2"; echo
echo "-- 留一份未裁决证据 T1#3（完成后演示不得首次裁决）"
CFL_T1_LATE=$(curl -s -X POST "$BASE/api/drills/$DID/terminals/T1/reports" -H "$ctype" -d '{
  "entries": [{"seq": 3, "occurred_at": "2026-10-05T09:20:00Z", "event_type": "patrol", "content": "第一轮巡查（迟到不同版本，暂不裁决）"}]
}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["results"][0]["conflict_id"])')
echo "-- 按演练查询裁决结果（连同证据）"
curl -s "$BASE/api/drills/$DID/adjudications"; echo

echo "## 10. 逐号确认 T2 的缺号 1 丢失"
curl -s -X POST "$BASE/api/drills/$DID/terminals/T2/gaps/lost" -H "$ctype" \
  -d '{"seq":1,"reason":"离线缓存损坏无法恢复","confirmed_by":"保障组"}'; echo

echo "## 11. 缺号全部闭合 => 封存版本 1（不可变，dependencies 含当时依赖及依据、采纳修订）"
curl -s -X POST "$BASE/api/drills/$DID/seals" -H "$ctype" -d '{"note":"演练结束封存"}'; echo

echo "## 12. 封存后：迟到上报 + 依赖增删只影响当前视图（撤销 T1#2->T1#3，新建 T1#1->T1#3）"
curl -s -X POST "$BASE/api/drills/$DID/terminals/T3/reports" -H "$ctype" -d '{
  "entries": [{"seq": 1, "occurred_at": "2026-10-05T09:40:00Z", "event_type": "command", "content": "指挥车回撤指令回执（迟到补报）"}]
}'; echo
curl -s -X DELETE "$BASE/api/drills/$DID/dependencies" -H "$ctype" -d '{
  "predecessor": {"terminal_code": "T1", "seq": 2},
  "successor":   {"terminal_code": "T1", "seq": 3}
}'; echo
echo "-- 重复撤销（幂等，already_revoked: true）"
curl -s -X DELETE "$BASE/api/drills/$DID/dependencies" -H "$ctype" -d '{
  "predecessor": {"terminal_code": "T1", "seq": 2},
  "successor":   {"terminal_code": "T1", "seq": 3}
}'; echo
curl -s -X POST "$BASE/api/drills/$DID/dependencies" -H "$ctype" -d '{
  "predecessor": {"terminal_code": "T1", "seq": 1},
  "successor":   {"terminal_code": "T1", "seq": 3},
  "reason": "上线自检通过后可直接进入巡查阶段"
}'; echo
curl -s "$BASE/api/drills/$DID/seals/1"; echo
curl -s "$BASE/api/drills/$DID/dependencies"; echo
curl -s "$BASE/api/drills/$DID/timeline"; echo

echo "## 13. 错误场景"
curl -s -X POST "$BASE/api/drills/$DID/terminals/GHOST/reports" -H "$ctype" \
  -d '{"entries":[{"seq":1,"occurred_at":"2026-10-05T09:00:00Z","event_type":"x","content":"y"}]}'; echo
curl -s -X POST "$BASE/api/drills/$DID/terminals/T1/reports" -H "$ctype" \
  -d '{"entries":[{"seq":-1,"occurred_at":"2026-10-05T09:00:00Z","event_type":"x","content":"y"}]}'; echo

echo "## 14. 主动完成：T3 缺号 2 未闭合 => 409 unresolved_gaps，不生成封存版本"
curl -s -X POST "$BASE/api/drills/$DID/terminals/T3/reports" -H "$ctype" -d '{
  "entries": [{"seq": 3, "occurred_at": "2026-10-05T09:50:00Z", "event_type": "command", "content": "指挥车就位回执"}]
}'; echo
curl -s -X POST "$BASE/api/drills/$DID/complete" -H "$ctype" -d '{"completed_by":"演练负责人"}'; echo

echo "## 15. 逐号确认 T3#2 丢失后完成 => 原子生成最终封存版本并转为已完成（201，含当前依赖）"
curl -s -X POST "$BASE/api/drills/$DID/terminals/T3/gaps/lost" -H "$ctype" \
  -d '{"seq":2,"reason":"终端掉电未缓存","confirmed_by":"保障组"}'; echo
curl -s -X POST "$BASE/api/drills/$DID/complete" -H "$ctype" \
  -d '{"note":"演练正式收官","completed_by":"张指挥"}'; echo
curl -s "$BASE/api/drills/$DID"; echo

echo "## 16. 完成后：登记/非重复上报/更正/缺号确认/再封存/依赖变更/首次裁决一律拒绝；重试仍 duplicate；冲突不新增证据"
curl -s -X POST "$BASE/api/drills/$DID/terminals" -H "$ctype" -d '{"terminals":[{"code":"T9"}]}'; echo
curl -s -X POST "$BASE/api/drills/$DID/terminals/T1/reports" -H "$ctype" -d '{
  "entries": [
    {"seq": 1, "occurred_at": "2026-10-05T09:00:00Z", "event_type": "login", "content": "终端上线自检通过"},
    {"seq": 4, "occurred_at": "2026-10-05T10:00:00Z", "event_type": "x", "content": "迟到的新序号"},
    {"seq": 2, "occurred_at": "2026-10-05T09:10:00Z", "event_type": "water_level", "content": "被篡改"}
  ]
}'; echo
curl -s "$BASE/api/drills/$DID/conflicts"; echo
curl -s -X PUT "$BASE/api/drills/$DID/records/T1/1" -H "$ctype" \
  -d '{"content":"试图更正","reason":"完成后更正"}'; echo
curl -s -X POST "$BASE/api/drills/$DID/terminals/T1/gaps/lost" -H "$ctype" -d '{"seq":1}'; echo
curl -s -X POST "$BASE/api/drills/$DID/seals" -H "$ctype" -d '{"note":"x"}'; echo
curl -s -X POST "$BASE/api/drills/$DID/dependencies" -H "$ctype" -d '{
  "predecessor": {"terminal_code": "T1", "seq": 3},
  "successor":   {"terminal_code": "T1", "seq": 1},
  "reason": "完成后试图变更依赖"
}'; echo
curl -s -X DELETE "$BASE/api/drills/$DID/dependencies" -H "$ctype" -d '{
  "predecessor": {"terminal_code": "T1", "seq": 1},
  "successor":   {"terminal_code": "T1", "seq": 2}
}'; echo
echo "-- 未裁决证据首次裁决被拒（409 drill_completed）"
curl -s -X POST "$BASE/api/drills/$DID/conflicts/$CFL_T1_LATE/adjudications" -H "$ctype" -d '{
  "decision": "keep", "reason": "完成后才想起裁决", "operator": "演练负责人"
}'; echo
echo "-- 既有裁决的幂等重试仍返回原结果（200 already_adjudicated: true）"
curl -s -X POST "$BASE/api/drills/$DID/conflicts/$CFL_T1/adjudications" -H "$ctype" -d '{
  "decision": "keep", "reason": "现记录已与堤岸监测点电话核实，维持不变", "operator": "演练负责人"
}'; echo

echo "## 17. 重复完成 => 同一最终版本（200）；历史读取（含依赖）仍可用"
curl -s -X POST "$BASE/api/drills/$DID/complete" -H "$ctype" \
  -d '{"note":"再次完成","completed_by":"别人"}'; echo
curl -s "$BASE/api/drills/$DID/seals"; echo
curl -s "$BASE/api/drills/$DID/seals/2"; echo
curl -s "$BASE/api/drills/$DID/timeline"; echo
curl -s "$BASE/api/drills/$DID/records/T1/3/dependencies"; echo

echo "DONE drill_id=$DID"
