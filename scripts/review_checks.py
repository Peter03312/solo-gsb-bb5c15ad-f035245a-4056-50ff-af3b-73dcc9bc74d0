"""普通校准证书双人复核测试：录入申请（凭据/幂等/不占用编号与有效期）、
复核定稿（批准签发绑定/驳回/前置校验/批准时校验失败仍待审）、
同操作重试与异操作冲突、并发复核唯一份定稿、既有契约保持。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/review_checks.py [base_url] [submitter_key] [reviewer_key] [management_key]
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
SK = sys.argv[2] if len(sys.argv) > 2 else "caltrace-review-submitter"
RK = sys.argv[3] if len(sys.argv) > 3 else "caltrace-review-reviewer"
MK = sys.argv[4] if len(sys.argv) > 4 else "caltrace-delivery-admin"
results = []


def call(method, path, body=None, key=None, key_header="X-Submitter-Key"):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers[key_header] = key
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" :: {detail}"))


def err_code(body):
    return body.get("error", {}).get("code")


TZ = timezone(timedelta(hours=8))
t0 = datetime(2026, 1, 1, 0, 0, tzinfo=TZ)
_seq = [0]


def nxt(prefix):
    _seq[0] += 1
    return f"{prefix}-{_seq[0]}"


def iso(offset_days=0):
    return (t0 + timedelta(days=offset_days)).isoformat()


def ref(no):
    return call("POST", "/api/v1/reference-certificates", {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": "基准" + no,
        "valid_from": iso(0), "valid_to": iso(1500), "calibrated_at": iso(0)})


def cal(no, parent, device_id=None, vfrom=10, vto=1200, cat=11):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": device_id or ("DEV-" + no), "device_name": "仪表" + no,
        "valid_from": iso(vfrom), "valid_to": iso(vto), "calibrated_at": iso(cat),
        "conclusion": "PASS", "parent_cert_no": parent})


def submit(rno, cert_no, parent, submitter="李录入", device_id=None,
           vfrom=10, vto=1200, cat=11, key=SK):
    return call("POST", "/api/v1/review-requests", {
        "request_no": rno,
        "submitter": submitter,
        "certificate": {
            "cert_no": cert_no, "device_id": device_id or ("DEV-" + cert_no),
            "device_name": "仪表" + cert_no,
            "valid_from": iso(vfrom), "valid_to": iso(vto), "calibrated_at": iso(cat),
            "conclusion": "PASS", "parent_cert_no": parent}}, key=key)


def decide(rno, decision, reviewer="王复核", reason=None, key=RK):
    body = {"decision": decision, "reviewer": reviewer}
    if reason is not None:
        body["reason"] = reason
    return call("POST", f"/api/v1/review-requests/{rno}/review",
                body, key=key, key_header="X-Reviewer-Key")


def rq(rno):
    return call("GET", f"/api/v1/review-requests/{rno}")


def suspend(no, reason="标准器数据异常，待复核"):
    return call("POST", f"/api/v1/certificates/{no}/suspensions",
                {"cert_no": no, "reason": reason}, key=MK, key_header="X-Management-Key")


def lift(no, reason="复核合格，恢复使用"):
    return call("POST", f"/api/v1/certificates/{no}/suspension-lift",
                {"cert_no": no, "reason": reason}, key=MK, key_header="X-Management-Key")


# ---------------------------------------------------------------------------
print("== 0. 前置数据（基准链） ==")
r_main = nxt("R-MAIN")
check("基准 201", ref(r_main)[0] == 201)
c_main = nxt("C-MAIN")
check("链上普通证书 201", cal(c_main, r_main)[0] == 201)

# ---------------------------------------------------------------------------
print("== A. 提交申请：凭据 / 幂等 / 不占用证书编号与设备有效期 ==")
s, e = submit(nxt("RR-AUTH"), "CAL-X", r_main, key="wrong-key")
check("错误录入人凭据 401", s == 401 and err_code(e) == "invalid_submitter_key", f"{s} {err_code(e)}")
s, e = call("POST", "/api/v1/review-requests", {
    "request_no": nxt("RR-AUTH2"), "certificate": {
        "cert_no": "CAL-X", "device_id": "D", "valid_from": iso(10), "valid_to": iso(100),
        "calibrated_at": iso(11), "conclusion": "PASS", "parent_cert_no": r_main}})
check("缺少录入人凭据头 401", s == 401 and err_code(e) == "invalid_submitter_key", f"{s} {err_code(e)}")

rn_a, cert_a = nxt("RR-A"), nxt("CAL-RV-A")
s, b1 = submit(rn_a, cert_a, c_main)
check("提交申请 201", s == 201, f"got {s} {b1}")
check("申请为待审：status=PENDING 且 review=null",
      b1["status"] == "PENDING" and b1["review"] is None
      and b1["request"]["cert_no"] == cert_a and b1["submitter"] == "李录入",
      json.dumps(b1, ensure_ascii=False))
check("申请不产生证书（404）", call("GET", f"/api/v1/certificates/{cert_a}")[0] == 404)

s, b2 = submit(rn_a, cert_a, c_main)
check("同号同内容重试 200 返回原申请（提交时刻不变）",
      s == 200 and b2 == b1, f"{s}")
s, e = submit(rn_a, cert_a, c_main, submitter="张录入")
check("同号异内容 409 review_request_conflict",
      s == 409 and err_code(e) == "review_request_conflict"
      and e["error"]["details"]["request_no"] == rn_a, f"{s} {err_code(e)}")

# 申请不占用证书编号：同编号可直接签发（有效期窗口错开，避免与重叠校验混淆）
rn_b, cert_b = nxt("RR-B"), nxt("CAL-RV-B")
s, _ = submit(rn_b, cert_b, c_main, vfrom=10, vto=600, cat=11)
check("提交申请（编号将被直接签发占用）201", s == 201, f"got {s}")
s, _ = cal(cert_b, c_main, vfrom=600, vto=1200, cat=601)
check("同编号证书仍可直接签发 201（申请不占用证书编号）", s == 201, f"got {s}")

# 申请不占用设备有效期：同设备重叠区间可直接签发
rn_c, cert_c = nxt("RR-C"), nxt("CAL-RV-C")
dev_c = "DEV-RV-SHARED"
s, _ = submit(rn_c, cert_c, c_main, device_id=dev_c, vfrom=10, vto=1000, cat=11)
check("提交申请（设备区间将被占用）201", s == 201, f"got {s}")
s, _ = cal(nxt("CAL-RV-C2"), c_main, device_id=dev_c, vfrom=20, vto=900, cat=21)
check("同设备重叠区间仍可直接签发 201（申请不占用设备有效期）", s == 201, f"got {s}")

# ---------------------------------------------------------------------------
print("== B. 复核前置校验：凭据 / 申请号 / 复核人 / 驳回原因 ==")
s, e = decide(rn_a, "APPROVE", key="wrong-key")
check("错误复核人凭据 401", s == 401 and err_code(e) == "invalid_reviewer_key", f"{s} {err_code(e)}")
s, e = call("POST", f"/api/v1/review-requests/{rn_a}/review",
            {"decision": "APPROVE", "reviewer": "王复核"})
check("缺少复核人凭据头 401", s == 401 and err_code(e) == "invalid_reviewer_key", f"{s} {err_code(e)}")
s, e = decide("RR-NOPE-xyz", "APPROVE")
check("未知申请号 404", s == 404 and err_code(e) == "review_request_not_found", f"{s} {err_code(e)}")
s, e = decide(rn_a, "APPROVE", reviewer="   ")
check("复核人空白 422", s == 422 and err_code(e) == "reviewer_required", f"{s} {err_code(e)}")
s, e = decide(rn_a, "REJECT", reason="  ")
check("驳回缺原因 422", s == 422 and err_code(e) == "review_reason_required", f"{s} {err_code(e)}")
s, q = rq(rn_a)
check("前置校验失败不定稿（仍 PENDING）", s == 200 and q["status"] == "PENDING", f"{s} {q['status']}")

# ---------------------------------------------------------------------------
print("== C. 驳回：附原因定稿；驳回后不能再批准；同操作重试 ==")
rn_d, cert_d = nxt("RR-D"), nxt("CAL-RV-D")
s, _ = submit(rn_d, cert_d, c_main)
check("提交申请 201", s == 201, f"got {s}")
s, bj = decide(rn_d, "REJECT", reviewer="赵复核", reason="签发资料不全，退回补正")
check("驳回 201", s == 201, f"got {s} {bj}")
check("驳回定稿：状态/结论/复核人/原因/时刻",
      bj["status"] == "REJECTED" and bj["review"]["decision"] == "REJECT"
      and bj["review"]["reviewer"] == "赵复核"
      and bj["review"]["reason"] == "签发资料不全，退回补正"
      and bool(bj["review"]["reviewed_at"])
      and bj["review"]["cert_no"] is None and bj["review"]["certificate"] is None,
      json.dumps(bj["review"], ensure_ascii=False))
check("驳回不产生证书（404）", call("GET", f"/api/v1/certificates/{cert_d}")[0] == 404)

s, bj2 = decide(rn_d, "REJECT", reviewer="赵复核", reason="签发资料不全，退回补正")
check("同操作重试 200 返回首次定稿（时刻不变）",
      s == 200 and bj2 == bj, f"{s}")
s, e = decide(rn_d, "APPROVE", reviewer="赵复核")
check("驳回后不能再批准 409 review_conflict",
      s == 409 and err_code(e) == "review_conflict"
      and e["error"]["details"]["finalized_status"] == "REJECTED", f"{s} {err_code(e)}")
s, e = decide(rn_d, "REJECT", reviewer="赵复核", reason="换一个原因")
check("异内容再驳回 409 review_conflict",
      s == 409 and err_code(e) == "review_conflict", f"{s} {err_code(e)}")
s, q = rq(rn_d)
check("查询：驳回状态与结果可查",
      s == 200 and q["status"] == "REJECTED" and q["review"]["reviewer"] == "赵复核", f"{s}")

# ---------------------------------------------------------------------------
print("== D. 批准：同事务真正签发并绑定复核人；定稿后异操作冲突 ==")
rn_e, cert_e = nxt("RR-E"), nxt("CAL-RV-E")
s, _ = submit(rn_e, cert_e, c_main)
check("提交申请 201", s == 201, f"got {s}")
s, ba = decide(rn_e, "APPROVE", reviewer="王复核")
check("批准 201", s == 201, f"got {s} {ba}")
check("批准定稿：状态/结论/复核人/证书绑定",
      ba["status"] == "APPROVED" and ba["review"]["decision"] == "APPROVE"
      and ba["review"]["reviewer"] == "王复核" and ba["review"]["reason"] is None
      and ba["review"]["cert_no"] == cert_e
      and ba["review"]["certificate"]["cert_no"] == cert_e
      and ba["review"]["certificate"]["status"] == "ACTIVE"
      and ba["review"]["chain"]["root_cert_no"] == r_main,
      json.dumps(ba["review"], ensure_ascii=False))
check("定稿时刻与证书创建时刻一致（同一事务）",
      ba["review"]["reviewed_at"] == ba["review"]["certificate"]["created_at"],
      f"{ba['review']['reviewed_at']} != {ba['review']['certificate']['created_at']}")
s, cert = call("GET", f"/api/v1/certificates/{cert_e}")
check("批准签发的证书落库可查（内容与申请一致）",
      s == 200 and cert["cert_no"] == cert_e and cert["parent_cert_no"] == c_main
      and cert["conclusion"] == "PASS" and cert["status"] == "ACTIVE", f"{s}")
s, chain = call("GET", f"/api/v1/certificates/{cert_e}/chain")
check("追溯链完整（基准->中间证书->新证书）",
      s == 200 and [n["certificate"]["cert_no"] for n in chain["nodes"]] == [r_main, c_main, cert_e],
      f"{s}")

s, ba2 = decide(rn_e, "APPROVE", reviewer="王复核")
check("同操作重试 200 返回首次定稿（不重复签发）",
      s == 200 and ba2 == ba, f"{s}")
s, e = decide(rn_e, "APPROVE", reviewer="钱复核")
check("异操作（换复核人）再批准 409 review_conflict",
      s == 409 and err_code(e) == "review_conflict"
      and e["error"]["details"]["finalized_status"] == "APPROVED", f"{s} {err_code(e)}")
s, e = decide(rn_e, "REJECT", reviewer="王复核", reason="事后反悔")
check("批准后再驳回 409 review_conflict",
      s == 409 and err_code(e) == "review_conflict", f"{s} {err_code(e)}")
s, q = rq(rn_e)
check("查询：批准状态与绑定结果可查",
      s == 200 and q["status"] == "APPROVED" and q["review"]["reviewer"] == "王复核"
      and q["review"]["certificate"]["cert_no"] == cert_e, f"{s}")
s, e = rq("RR-NOPE-xyz")
check("查询未知申请号 404", s == 404 and err_code(e) == "review_request_not_found", f"{s} {err_code(e)}")

# ---------------------------------------------------------------------------
print("== E. 批准时校验失败：返回具体原因，申请仍待审且不产生证书 ==")
# E1. 引用链临时停用：批准时 422 reference_suspended；解除后批准成功
r_s = nxt("R-SUSP")
check("基准（停用链）201", ref(r_s)[0] == 201)
rn_s, cert_s = nxt("RR-S"), nxt("CAL-RV-S")
s, _ = submit(rn_s, cert_s, r_s)
check("提交申请（引用将被停用的基准）201", s == 201, f"got {s}")
s, _ = suspend(r_s)
check("停用基准 201", s == 201, f"got {s}")
s, e = decide(rn_s, "APPROVE")
check("批准时引用链停用 -> 422 reference_suspended",
      s == 422 and err_code(e) == "reference_suspended"
      and e["error"]["details"]["suspension_source"] == r_s, f"{s} {err_code(e)}")
s, q = rq(rn_s)
check("校验失败申请仍待审", s == 200 and q["status"] == "PENDING" and q["review"] is None,
      f"{s} {q['status']}")
check("校验失败不产生证书（404）", call("GET", f"/api/v1/certificates/{cert_s}")[0] == 404)
s, _ = lift(r_s)
check("解除停用 200", s == 200, f"got {s}")
s, b = decide(rn_s, "APPROVE")
check("解除后批准成功 201", s == 201 and b["status"] == "APPROVED", f"got {s}")

# E2. 设备有效期被并发占用：批准时 422 validity_overlap，仍待审
s, e = decide(rn_c, "APPROVE")
check("批准时设备区间重叠 -> 422 validity_overlap",
      s == 422 and err_code(e) == "validity_overlap", f"{s} {err_code(e)}")
s, q = rq(rn_c)
check("有效期冲突申请仍待审", s == 200 and q["status"] == "PENDING", f"{s} {q['status']}")

# E3. 证书编号被直接签发占用：批准时 409 cert_duplicate，仍待审
s, e = decide(rn_b, "APPROVE")
check("批准时编号已存在 -> 409 cert_duplicate",
      s == 409 and err_code(e) == "cert_duplicate", f"{s} {err_code(e)}")
s, q = rq(rn_b)
check("编号冲突申请仍待审", s == 200 and q["status"] == "PENDING", f"{s} {q['status']}")

# E4. 引用不存在 / 引用已撤销
rn_f, cert_f = nxt("RR-F"), nxt("CAL-RV-F")
s, _ = submit(rn_f, cert_f, "REF-NOPE-xyz")
check("提交申请（引用不存在，提交时不校验）201", s == 201, f"got {s}")
s, e = decide(rn_f, "APPROVE")
check("批准时引用不存在 -> 422 invalid_reference",
      s == 422 and err_code(e) == "invalid_reference", f"{s} {err_code(e)}")
s, q = rq(rn_f)
check("引用不存在申请仍待审", s == 200 and q["status"] == "PENDING", f"{s} {q['status']}")

r_x = nxt("R-REVOKE")
check("基准（将被撤销）201", ref(r_x)[0] == 201)
rn_g, cert_g = nxt("RR-G"), nxt("CAL-RV-G")
s, _ = submit(rn_g, cert_g, r_x)
check("提交申请（引用将被撤销的基准）201", s == 201, f"got {s}")
s, _ = call("POST", f"/api/v1/certificates/{r_x}/revoke", {"reason": "超差撤销"})
check("撤销基准 200", s == 200, f"got {s}")
s, e = decide(rn_g, "APPROVE")
check("批准时引用已撤销 -> 422 reference_not_active",
      s == 422 and err_code(e) == "reference_not_active", f"{s} {err_code(e)}")
s, q = rq(rn_g)
check("引用失效申请仍待审（可驳回收尾）", s == 200 and q["status"] == "PENDING", f"{s} {q['status']}")
s, b = decide(rn_g, "REJECT", reason="上游基准已撤销，退回")
check("待审申请可驳回定稿 201", s == 201 and b["status"] == "REJECTED", f"got {s}")

# ---------------------------------------------------------------------------
print("== F. 并发复核：只允许一份定稿；同操作重试返回原结果 ==")
# F1. 同内容并发批准：1×201 + 其余 200，证书仅一张
rn_h, cert_h = nxt("RR-H"), nxt("CAL-RV-H")
s, _ = submit(rn_h, cert_h, c_main)
check("提交申请 201", s == 201, f"got {s}")
N = 10
barrier = threading.Barrier(N)
race, rlock = {}, threading.Lock()


def race_approve(idx):
    barrier.wait()
    st, b = decide(rn_h, "APPROVE", reviewer="王复核")
    with rlock:
        race[idx] = (st, b)


threads = [threading.Thread(target=race_approve, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
n201 = sum(1 for st, _ in race.values() if st == 201)
n200 = sum(1 for st, _ in race.values() if st == 200)
bodies = {json.dumps(b, sort_keys=True, ensure_ascii=False) for _, b in race.values()}
check(f"同内容并发批准：1×201 + {N-1}×200 且响应一致",
      n201 == 1 and n200 == N - 1 and len(bodies) == 1,
      f"201={n201} 200={n200} bodies={len(bodies)}")
s, q = rq(rn_h)
check("并发批准后状态唯一（APPROVED，绑定王复核）",
      s == 200 and q["status"] == "APPROVED" and q["review"]["reviewer"] == "王复核", f"{s}")
s, cert = call("GET", f"/api/v1/certificates/{cert_h}")
check("证书仅签发一张且可查", s == 200 and cert["cert_no"] == cert_h, f"{s}")

# F2. 混合并发（批准 vs 驳回）：恰好一份定稿，其余 200/409
rn_i, cert_i = nxt("RR-I"), nxt("CAL-RV-I")
s, _ = submit(rn_i, cert_i, c_main)
check("提交申请 201", s == 201, f"got {s}")
M = 12
barrier2 = threading.Barrier(M)
race2, rlock2 = {}, threading.Lock()


def race_mixed(idx):
    barrier2.wait()
    if idx % 2 == 0:
        st, b = decide(rn_i, "APPROVE", reviewer="王复核")
    else:
        st, b = decide(rn_i, "REJECT", reviewer="赵复核", reason="资料存疑")
    with rlock2:
        race2[idx] = (st, b)


threads = [threading.Thread(target=race_mixed, args=(i,)) for i in range(M)]
for t in threads:
    t.start()
for t in threads:
    t.join()
n201 = sum(1 for st, _ in race2.values() if st == 201)
n200 = sum(1 for st, _ in race2.values() if st == 200)
n409 = sum(1 for st, b in race2.values() if st == 409 and err_code(b) == "review_conflict")
check(f"混合并发复核：恰好一份定稿（1×201，其余 200/409 守恒）",
      n201 == 1 and n200 + n409 == M - 1, f"201={n201} 200={n200} 409={n409}")
s, q = rq(rn_i)
final_status = q["status"]
check("混合并发后状态唯一确定", s == 200 and final_status in ("APPROVED", "REJECTED"),
      f"{s} {final_status}")
if final_status == "APPROVED":
    check("定稿为批准：证书已签发", call("GET", f"/api/v1/certificates/{cert_i}")[0] == 200)
else:
    check("定稿为驳回：证书不存在", call("GET", f"/api/v1/certificates/{cert_i}")[0] == 404)

# F3. 并发提交申请：同号同内容 1×201 其余 200；同号异内容仅 1 次成功
rn_j, cert_j = nxt("RR-J"), nxt("CAL-RV-J")
K = 8
barrier3 = threading.Barrier(K)
race3, rlock3 = {}, threading.Lock()


def race_submit(idx, same):
    barrier3.wait()
    st, b = submit(rn_j, cert_j, c_main, submitter="李录入" if same else f"录入{idx}")
    with rlock3:
        race3[idx] = (st, b)


threads = [threading.Thread(target=race_submit, args=(i, True)) for i in range(K)]
for t in threads:
    t.start()
for t in threads:
    t.join()
n201 = sum(1 for st, _ in race3.values() if st == 201)
n200 = sum(1 for st, _ in race3.values() if st == 200)
check(f"并发同号同内容提交：1×201 + {K-1}×200", n201 == 1 and n200 == K - 1,
      f"201={n201} 200={n200}")

rn_k, cert_k = nxt("RR-K"), nxt("CAL-RV-K")
barrier4 = threading.Barrier(K)
race4, rlock4 = {}, threading.Lock()


def race_submit_diff(idx):
    barrier4.wait()
    st, b = submit(rn_k, cert_k, c_main, submitter=f"录入{idx}")
    with rlock4:
        race4[idx] = (st, b)


threads = [threading.Thread(target=race_submit_diff, args=(i,)) for i in range(K)]
for t in threads:
    t.start()
for t in threads:
    t.join()
n201 = sum(1 for st, _ in race4.values() if st == 201)
n409 = sum(1 for st, b in race4.values() if st == 409 and err_code(b) == "review_request_conflict")
check(f"并发同号异内容提交：仅 1 次 201，其余 409", n201 == 1 and n409 == K - 1,
      f"201={n201} 409={n409}")

# ---------------------------------------------------------------------------
print("== G. 既有接口契约保持 ==")
s, idx = call("GET", "/")
eps = idx["endpoints"]
check("根索引列出复核端点", s == 200
      and "POST /api/v1/review-requests" in eps
      and "POST /api/v1/review-requests/{request_no}/review" in eps
      and "GET  /api/v1/review-requests/{request_no}" in eps, str(eps))
s, c = call("GET", f"/api/v1/certificates/{cert_e}")
check("证书查询契约不变", s == 200 and c["cert_no"] == cert_e and c["status"] == "ACTIVE")
s, b = cal(nxt("CAL-DIRECT"), c_main)
check("直接签发契约不变 201", s == 201 and b["certificate"]["cert_no"], f"got {s}")

# ---------------------------------------------------------------------------
n_pass = sum(1 for _, ok, _ in results if ok)
n_fail = len(results) - n_pass
print(f"\n结果: PASS={n_pass} FAIL={n_fail}")
sys.exit(1 if n_fail else 0)
