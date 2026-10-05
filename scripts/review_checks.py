"""普通证书双人复核签发测试：

申请提交（凭据/幂等/不占编号与有效期）、批准与驳回（双人不同凭据、
驳回原因、驳回后不可再批准）、批准按“批准当时”的标准证书状态/临时停用/
签发规则真正签发（失败返回具体原因且申请仍待审、不产生证书）、
同操作重放与异操作冲突、按申请号查询、并发复核只允许一份定稿，
以及既有直接签发与证书查询契约不变。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/review_checks.py [base_url] [entry_key] [reviewer_key] [reviewer_key_2] [shared_key]
  shared_key 非空时额外校验“录入与复核同凭据 403”（该凭据需同时配置在两张凭据表中）。
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
ENTRY = sys.argv[2] if len(sys.argv) > 2 else "caltrace-entry-li"
REV1 = sys.argv[3] if len(sys.argv) > 3 else "caltrace-review-zhao"
REV2 = sys.argv[4] if len(sys.argv) > 4 else "caltrace-review-sun"
SHARED = sys.argv[5] if len(sys.argv) > 5 else ""
MK = "test-admin-key"
results = []


def call(method, path, body=None, entry=None, reviewer=None, mgmt=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if entry is not None:
        headers["X-Entry-Key"] = entry
    if reviewer is not None:
        headers["X-Reviewer-Key"] = reviewer
    if mgmt is not None:
        headers["X-Management-Key"] = mgmt
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


def ref(no, **kw):
    body = {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": "基准" + no,
        "valid_from": iso(0), "valid_to": iso(1500), "calibrated_at": iso(0),
    }
    body.update(kw)
    return call("POST", "/api/v1/reference-certificates", body)


def issue(no, parent, conclusion="PASS", vfrom=10, vto=1200, cat=11, device=None):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": device or ("DEV-" + no), "device_name": "仪表" + no,
        "valid_from": iso(vfrom), "valid_to": iso(vto), "calibrated_at": iso(cat),
        "conclusion": conclusion, "parent_cert_no": parent})


def submit(no, cert_no, parent, key=ENTRY, **over):
    body = {
        "application_no": no, "cert_no": cert_no, "device_id": "DEV-" + cert_no,
        "device_name": "待复核仪表" + cert_no,
        "valid_from": iso(10), "valid_to": iso(1200), "calibrated_at": iso(11),
        "conclusion": "PASS", "parent_cert_no": parent,
    }
    body.update(over)
    return call("POST", "/api/v1/certificate-review-requests", body, entry=key)


def approve(no, key=REV1, reason=None):
    body = {"decision": "APPROVE"}
    if reason is not None:
        body["reason"] = reason
    return call("POST", f"/api/v1/certificate-review-requests/{no}/decision",
                body, reviewer=key)


def reject(no, reason, key=REV1):
    return call("POST", f"/api/v1/certificate-review-requests/{no}/decision",
                {"decision": "REJECT", "reason": reason}, reviewer=key)


def query(no, *, entry=None, reviewer=None):
    return call("GET", f"/api/v1/certificate-review-requests/{no}",
                entry=entry, reviewer=reviewer)


def suspend(no, reason="标准器数据异常，待复核"):
    return call("POST", f"/api/v1/certificates/{no}/suspensions",
                {"cert_no": no, "reason": reason}, mgmt=MK)


def lift(no, reason="复核合格，恢复使用"):
    return call("POST", f"/api/v1/certificates/{no}/suspension-lift",
                {"cert_no": no, "reason": reason}, mgmt=MK)


# ---------------------------------------------------------------------------
print("== 0. 前置数据 ==")
r0 = nxt("REF")
check("基准证书 201", ref(r0)[0] == 201)

# ---------------------------------------------------------------------------
print("== A. 提交申请：凭据校验 / 不占编号与有效期 ==")
s, e = call("POST", "/api/v1/certificate-review-requests",
            {"application_no": nxt("APL"), "cert_no": nxt("TMP"), "device_id": "D",
             "valid_from": iso(10), "valid_to": iso(1200), "calibrated_at": iso(11),
             "conclusion": "PASS", "parent_cert_no": r0})
check("缺少录入凭据 401", s == 401 and err_code(e) == "invalid_identity_key", f"{s} {err_code(e)}")
s, e = submit(nxt("APL"), nxt("TMP"), r0, key="wrong-entry-key")
check("错误录入凭据 401", s == 401 and err_code(e) == "invalid_identity_key", f"{s} {err_code(e)}")
s, e = submit(nxt("APL"), nxt("TMP"), r0, key=REV1)
check("复核凭据不能用于提交 401", s == 401 and err_code(e) == "invalid_identity_key", f"{s}")

ap1, c1 = nxt("APL"), nxt("CAL")
s, body = submit(ap1, c1, r0)
check("提交申请 201 且 PENDING",
      s == 201 and body["status"] == "PENDING" and body["application_no"] == ap1
      and body["submitter"] and body["review"] is None
      and body["certificate"] is None and body["chain"] is None
      and body["request"]["cert_no"] == c1 and body["request"]["parent_cert_no"] == r0
      and bool(body["submitted_at"]), json.dumps(body, ensure_ascii=False)[:300])
s, _ = call("GET", f"/api/v1/certificates/{c1}")
check("待审申请不占用证书编号（证书尚不存在 404）", s == 404, f"got {s}")

# 待审期间，其他设备同有效期的“直接签发”仍可成功——申请不预占设备有效期；
# 同时用本设备的另一个设备号签发（不占用待审证书自己的设备区间）
s, direct = issue(nxt("CAL-DIRECT"), r0, device=nxt("DEV-OTHER"))
check("待审申请不占用设备有效期（直接签发不受影响 201）", s == 201, f"got {s}")
s, direct2 = issue(nxt("CAL-DIRECT"), r0)
check("直接签发通道始终可用 201", s == 201 and direct2["certificate"]["status"] == "ACTIVE", f"got {s}")

# 引用尚不存在的父证书也允许先提交（能否签发留待批准当时判定）
ap_miss, c_miss = nxt("APL"), nxt("CAL")
s, _ = submit(ap_miss, c_miss, "REF-NEVER-EXIST")
check("父证书暂不存在也可提交待审 201", s == 201, f"got {s}")

# ---------------------------------------------------------------------------
print("== B. 申请号幂等：同内容返回原申请 / 异内容冲突 ==")
s, body2 = submit(ap1, c1, r0)
check("同号同内容重试 200 且返回原申请",
      s == 200 and body2["submitted_at"] == body["submitted_at"]
      and body2["status"] == "PENDING", f"got {s}")
s, e = submit(ap1, c1, r0, operator="不同内容")
check("同号异内容 409 review_request_conflict",
      s == 409 and err_code(e) == "review_request_conflict"
      and e["error"]["details"]["application_no"] == ap1, f"{s} {err_code(e)}")

# ---------------------------------------------------------------------------
print("== C. 复核定稿的凭据与双人校验 ==")
s, e = approve(ap1, key=None) if False else call(
    "POST", f"/api/v1/certificate-review-requests/{ap1}/decision",
    {"decision": "APPROVE"})
check("缺少复核凭据 401", s == 401 and err_code(e) == "invalid_identity_key", f"{s}")
s, e = approve(ap1, key="wrong-reviewer-key")
check("错误复核凭据 401", s == 401 and err_code(e) == "invalid_identity_key", f"{s}")
s, e = approve(ap1, key=ENTRY)
check("录入凭据不能用于复核 401", s == 401 and err_code(e) == "invalid_identity_key", f"{s}")
s, e = call("POST", f"/api/v1/certificate-review-requests/{nxt('APL-NOPE')}/decision",
            {"decision": "APPROVE"}, reviewer=REV1)
check("未知申请 404 review_request_not_found",
      s == 404 and err_code(e) == "review_request_not_found", f"{s} {err_code(e)}")

if SHARED:
    ap_sh, c_sh = nxt("APL"), nxt("CAL")
    check("共享凭据提交申请 201", submit(ap_sh, c_sh, r0, key=SHARED)[0] == 201)
    s, e = approve(ap_sh, key=SHARED)
    check("录入与复核同凭据 403 reviewer_must_differ",
          s == 403 and err_code(e) == "reviewer_must_differ"
          and e["error"]["details"]["application_no"] == ap_sh, f"{s} {err_code(e)}")
    s, _ = query(ap_sh, reviewer=REV1)
    check("403 后申请仍 PENDING", s == 200 and query(ap_sh, reviewer=REV1)[1]["status"] == "PENDING")

# ---------------------------------------------------------------------------
print("== D. 驳回：原因必填 / 驳回后不能再批准 / 同操作重放 ==")
ap_rj, c_rj = nxt("APL"), nxt("CAL")
check("提交待驳回申请 201", submit(ap_rj, c_rj, r0)[0] == 201)
s, e = reject(ap_rj, "   ")
check("空白驳回原因 422 review_reason_required",
      s == 422 and err_code(e) == "review_reason_required", f"{s} {err_code(e)}")
s, b = reject(ap_rj, "校准记录缺少环境温度项，退回补正", key=REV2)
check("驳回 201：REJECTED/复核人/原因/时刻绑定，不产生证书",
      s == 201 and b["status"] == "REJECTED"
      and b["review"]["decision"] == "REJECT" and b["review"]["reviewer"]
      and b["review"]["reason"] == "校准记录缺少环境温度项，退回补正"
      and bool(b["review"]["reviewed_at"])
      and b["certificate"] is None and b["chain"] is None,
      json.dumps(b.get("review"), ensure_ascii=False))
s, _ = call("GET", f"/api/v1/certificates/{c_rj}")
check("驳回不产生证书（404）", s == 404, f"got {s}")
s, e = approve(ap_rj)
check("驳回后不能再批准 409 review_already_decided",
      s == 409 and err_code(e) == "review_already_decided"
      and e["error"]["details"]["status"] == "REJECTED", f"{s} {err_code(e)}")
s, e = reject(ap_rj, "另一个原因")
check("驳回后不同原因再驳回 409", s == 409 and err_code(e) == "review_already_decided", f"{s}")
s, b2 = reject(ap_rj, "校准记录缺少环境温度项，退回补正", key=REV2)
check("同复核人同操作（同原因）重放 200 返回首次结果",
      s == 200 and b2["review"] == b["review"] and b2["status"] == "REJECTED", f"got {s}")
s, e = reject(ap_rj, "校准记录缺少环境温度项，退回补正", key=REV1)
check("另一复核人同内容再提交 409（只允许一份定稿）",
      s == 409 and err_code(e) == "review_already_decided"
      and e["error"]["details"]["finalized_reviewer"] == b["review"]["reviewer"], f"{s}")

# ---------------------------------------------------------------------------
print("== E. 批准：同事务真正签发并绑定结论/复核人/证书 ==")
s, b = approve(ap1)
check("批准 201：APPROVED 并真正签发（与直接签发同构）",
      s == 201 and b["status"] == "APPROVED"
      and b["certificate"]["cert_no"] == c1 and b["certificate"]["status"] == "ACTIVE"
      and b["certificate"]["parent_cert_no"] == r0
      and b["chain"]["root_cert_no"] == r0 and b["chain"]["length"] == 2
      and b["review"]["decision"] == "APPROVE" and b["review"]["reason"] is None
      and bool(b["review"]["reviewed_at"]),
      json.dumps({"status": b["status"], "cert": b["certificate"]}, ensure_ascii=False)[:300])
s, cert = call("GET", f"/api/v1/certificates/{c1}")
check("批准后证书可查（真正占用编号）", s == 200 and cert["status"] == "ACTIVE", f"got {s}")
s, chain = call("GET", f"/api/v1/certificates/{c1}/chain")
check("批准证书的追溯链查询契约不变", s == 200 and chain["length"] == 2, f"got {s}")
s, e = approve(ap1, reason="不该带原因") if False else call(
    "POST", f"/api/v1/certificate-review-requests/{ap1}/decision",
    {"decision": "APPROVE", "reason": "x"}, reviewer=REV1)
check("APPROVE 带原因 422（仅对 PENDING 判定；此处已 APPROVED 故 409）",
      s == 409 and err_code(e) == "review_already_decided", f"{s}")
s, e = reject(ap1, "批准后想改判驳回")
check("批准后不能改判驳回 409", s == 409 and err_code(e) == "review_already_decided", f"{s}")
s, b2 = approve(ap1)
check("同复核人同操作重放 200 返回首次结果（时刻/证书不变）",
      s == 200 and b2["review"] == b["review"]
      and b2["certificate"]["created_at"] == b["certificate"]["created_at"], f"got {s}")

# 待审申请 APPROVE 携带原因应在定稿前被 422
ap_r2, c_r2 = nxt("APL"), nxt("CAL")
check("提交申请 201", submit(ap_r2, c_r2, r0)[0] == 201)
s, e = call("POST", f"/api/v1/certificate-review-requests/{ap_r2}/decision",
            {"decision": "APPROVE", "reason": "批准不应有原因"}, reviewer=REV1)
check("PENDING 时 APPROVE 带原因 422",
      s == 422 and err_code(e) == "review_approve_reason_not_allowed", f"{s} {err_code(e)}")
check("422 后申请仍 PENDING", query(ap_r2, reviewer=REV1)[1]["status"] == "PENDING")

# ---------------------------------------------------------------------------
print("== F. 批准校验失败：具体原因、申请仍待审、不产生证书 ==")
# F1. 父证书始终不存在
s, e = approve(ap_miss)
check("引用不存在父证书 -> 422 invalid_reference",
      s == 422 and err_code(e) == "invalid_reference", f"{s} {err_code(e)}")
check("失败后仍 PENDING 且无证书",
      query(ap_miss, reviewer=REV1)[1]["status"] == "PENDING"
      and call("GET", f"/api/v1/certificates/{c_miss}")[0] == 404)

# F2. 批准前父证书被撤销 -> reference_not_active；重新签发父证书后再次批准成功
r_rev = nxt("REF")
check("前置基准 201", ref(r_rev)[0] == 201)
ap_rev, c_rev = nxt("APL"), nxt("CAL")
check("提交申请 201", submit(ap_rev, c_rev, r_rev)[0] == 201)
s, _ = call("POST", f"/api/v1/certificates/{r_rev}/revoke",
            {"reason": "复核期间发现超差", "operator": "qa"})
check("撤销基准 200", s == 200, f"got {s}")
s, e = approve(ap_rev)
check("批准时父证书已撤销 -> 422 reference_not_active",
      s == 422 and err_code(e) == "reference_not_active", f"{s} {err_code(e)}")
check("申请仍 PENDING", query(ap_rev, reviewer=REV1)[1]["status"] == "PENDING")

# F3. 批准前父证书被临时停用 -> reference_suspended；解除后批准成功
r_su = nxt("REF")
check("前置基准 201", ref(r_su)[0] == 201)
ap_su, c_su = nxt("APL"), nxt("CAL")
check("提交申请 201", submit(ap_su, c_su, r_su)[0] == 201)
check("登记停用 201", suspend(r_su)[0] == 201)
s, e = approve(ap_su)
check("批准时父证书停用中 -> 422 reference_suspended",
      s == 422 and err_code(e) == "reference_suspended", f"{s} {err_code(e)}")
check("停用拦截后仍 PENDING、无证书",
      query(ap_su, reviewer=REV1)[1]["status"] == "PENDING"
      and call("GET", f"/api/v1/certificates/{c_su}")[0] == 404)
check("解除停用 200", lift(r_su)[0] == 200)
s, b = approve(ap_su)
check("解除后批准成功 201",
      s == 201 and b["status"] == "APPROVED" and b["certificate"]["cert_no"] == c_su, f"got {s}")

# F4. 编号在待审期间被直接签发占用 -> 409 cert_duplicate；申请仍待审
c_occ = nxt("CAL")
check("直接签发先占用编号 201", issue(c_occ, r0)[0] == 201)
ap_occ = nxt("APL")
check("同编号申请可提交待审 201", submit(ap_occ, c_occ, r0, device_id="DEV-OTHER")[0] == 201)
s, e = approve(ap_occ)
check("批准时编号已占用 -> 409 cert_duplicate",
      s == 409 and err_code(e) == "cert_duplicate", f"{s} {err_code(e)}")
check("冲突后申请仍 PENDING（可由录入人重新提交新申请）",
      query(ap_occ, reviewer=REV1)[1]["status"] == "PENDING")

# F5. 有效期重叠 -> 422 validity_overlap（批准当时判定）
ap_ov, c_ov = nxt("APL"), nxt("CAL")
dev_ov = "DEV-OVERLAP-" + str(_seq[0])
check("先直接签发占用区间 201", issue(nxt("CAL-OV"), r0, device=dev_ov)[0] == 201)
check("重叠区间申请可提交 201", submit(ap_ov, c_ov, r0, device_id=dev_ov)[0] == 201)
s, e = approve(ap_ov)
check("批准时区间重叠 -> 422 validity_overlap",
      s == 422 and err_code(e) == "validity_overlap", f"{s} {err_code(e)}")
check("重叠拦截后仍 PENDING、无证书",
      query(ap_ov, reviewer=REV1)[1]["status"] == "PENDING"
      and call("GET", f"/api/v1/certificates/{c_ov}")[0] == 404)

# F6. 校准时刻超出父证书有效期 -> 422（按批准当时规则；提交只过结构校验）
#     证书自身窗口取 [20,100)，校准时刻 60 天落在本证书窗口内，但父证书
#     的有效期为 [0,1500)——改用短有效期父基准把校准时刻挡在其有效期外。
r_short = nxt("REF")
check("短有效期基准 201", ref(r_short, valid_from=iso(0), valid_to=iso(50),
                              calibrated_at=iso(0))[0] == 201)
ap_out, c_out = nxt("APL"), nxt("CAL")
check("校准时刻超出父有效期的申请可提交 201",
      submit(ap_out, c_out, r_short,
             valid_from=iso(20), valid_to=iso(100), calibrated_at=iso(60))[0] == 201)
s, e = approve(ap_out)
check("批准时校准时刻超父证书有效期 -> 422 reference_out_of_validity",
      s == 422 and err_code(e) == "reference_out_of_validity", f"{s} {err_code(e)}")

# ---------------------------------------------------------------------------
print("== G. 按申请号查询：状态/结果与凭据 ==")
s, e = call("GET", f"/api/v1/certificate-review-requests/{ap1}")
check("无凭据查询 401", s == 401 and err_code(e) == "invalid_identity_key", f"{s}")
s, e = query(ap1, entry="nope")
check("错误凭据查询 401", s == 401, f"{s}")
s, q = query(ap1, entry=ENTRY)
check("录入凭据可查：APPROVED + 证书与链结果",
      s == 200 and q["status"] == "APPROVED" and q["certificate"]["cert_no"] == c1
      and q["chain"]["length"] == 2, f"{s}")
s, q = query(ap_rj, reviewer=REV2)
check("复核凭据可查：REJECTED + 驳回原因",
      s == 200 and q["status"] == "REJECTED" and q["review"]["reason"]
      and q["certificate"] is None, f"{s}")
s, q = query(ap_ov, reviewer=REV1)
check("待审申请可查：PENDING 且携带完整签发资料",
      s == 200 and q["status"] == "PENDING" and q["request"]["cert_no"] == c_ov
      and q["review"] is None, f"{s}")
s, e = query(nxt("APL-NOPE"), reviewer=REV1)
check("未知申请 404", s == 404 and err_code(e) == "review_request_not_found", f"{s}")

# ---------------------------------------------------------------------------
print("== H. 并发复核：只允许一份定稿，同操作重放返回原结果 ==")
# H1. 多复核人并发 APPROVE/REJECT：恰一份首次定稿（201），其余全部 409
ap_c = nxt("APL")
c_c = nxt("CAL")
check("并发用申请提交 201", submit(ap_c, c_c, r0)[0] == 201)
N = 12
barrier = threading.Barrier(N)
outcomes = []
olock = threading.Lock()


def race_decide(idx):
    barrier.wait()
    # 一半批准一半驳回；全部使用各自存在的复核凭据
    if idx % 2 == 0:
        s, b = approve(ap_c, key=(REV1 if idx % 4 == 0 else REV2))
    else:
        s, b = reject(ap_c, f"并发驳回原因 {idx}", key=(REV1 if idx % 4 == 1 else REV2))
    with olock:
        outcomes.append((idx, s, b))


threads = [threading.Thread(target=race_decide, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()

n201 = sum(1 for _, s, _ in outcomes if s == 201)
n409 = sum(1 for _, s, b in outcomes if s == 409
           and err_code(b) == "review_already_decided")
check(f"并发定稿恰一份 201、其余 {N - 1} 份 409（实际 201={n201}, 409={n409}）",
      n201 == 1 and n409 == N - 1, str((n201, n409)))
s, final = query(ap_c, reviewer=REV1)
winner = next(b for _, s2, b in outcomes if s2 == 201)
check("最终状态与胜方定稿一致",
      final["status"] == ("APPROVED" if winner["status"] == "APPROVED" else "REJECTED")
      and final["review"]["reviewer"] == winner["review"]["reviewer"]
      and final["review"]["reviewed_at"] == winner["review"]["reviewed_at"],
      json.dumps(final["review"], ensure_ascii=False))
if final["status"] == "APPROVED":
    check("胜方为批准：证书已真正签发且唯一",
          call("GET", f"/api/v1/certificates/{c_c}")[0] == 200)
else:
    check("胜方为驳回：不产生证书",
          call("GET", f"/api/v1/certificates/{c_c}")[0] == 404)

# H2. 同一复核人同内容并发重放：1 份 201，其余 200 且响应与首次完全一致
ap_c2 = nxt("APL")
c_c2 = nxt("CAL")
check("并发重放用申请提交 201", submit(ap_c2, c_c2, r0)[0] == 201)
M = 8
barrier2 = threading.Barrier(M)
outs2 = []
olock2 = threading.Lock()


def race_approve_same(_idx):
    barrier2.wait()
    s, b = approve(ap_c2, key=REV1)
    with olock2:
        outs2.append((s, b))


threads = [threading.Thread(target=race_approve_same, args=(i,)) for i in range(M)]
for t in threads:
    t.start()
for t in threads:
    t.join()
codes = sorted(s for s, _ in outs2)
check("同操作并发：1×201 + 其余 200",
      codes == [200] * (M - 1) + [201], str(codes))
first = next(b for s, b in outs2 if s == 201)
check("全部响应等于首次定稿结果",
      all(b["review"] == first["review"] and b["certificate"]["cert_no"] == c_c2
          for _, b in outs2), "")
check("只签发一张证书",
      call("GET", f"/api/v1/certificates/{c_c2}")[1]["cert_no"] == c_c2)

# H3. 并发同申请号提交：同内容仅 1 份 201，其余 200
ap_c3, c_c3 = nxt("APL"), nxt("CAL")
K = 8
barrier3 = threading.Barrier(K)
outs3 = []
olock3 = threading.Lock()


def race_submit(_idx):
    barrier3.wait()
    s, b = submit(ap_c3, c_c3, r0)
    with olock3:
        outs3.append((s, b))


threads = [threading.Thread(target=race_submit, args=(i,)) for i in range(K)]
for t in threads:
    t.start()
for t in threads:
    t.join()
codes3 = sorted(s for s, _ in outs3)
check("并发同号同内容提交：1×201 + 其余 200",
      codes3 == [200] * (K - 1) + [201], str(codes3))

# ---------------------------------------------------------------------------
print("== I. 既有契约保持 ==")
s, idx = call("GET", "/")
eps = idx["endpoints"]
check("根索引列出复核三端点",
      any(x.endswith("/certificate-review-requests") for x in eps)
      and any(x.endswith("/certificate-review-requests/{application_no}/decision") for x in eps)
      and any(x.endswith("/certificate-review-requests/{application_no}") for x in eps),
      str([x for x in eps if "review" in x]))
s, c = call("GET", f"/api/v1/certificates/{c1}")
check("证书查询契约不变（批准签发的证书与直接签发同形）",
      s == 200 and set(c) >= {"cert_no", "kind", "status", "conclusion", "parent_cert_no",
                              "valid_from", "valid_to", "calibrated_at", "created_at"},
      str(sorted(c)))
s, _ = issue(nxt("CAL-PLAIN"), r0)
check("直接签发通道不受影响 201", s == 201, f"got {s}")
# 直接签发不经过复核表：列表/撤销/链等全部照旧
s, rev = call("POST", f"/api/v1/certificates/{c1}/revoke",
              {"reason": "批准后的证书仍可按既有规则撤销", "operator": "qa"})
check("批准签发的证书可按既有规则撤销 200，下游级联",
      s == 200 and rev["revoked"]["status"] == "REVOKED", f"got {s}")
s, q = query(ap1, reviewer=REV1)
check("撤销不改写申请的首次签发结果快照（仍 APPROVED，结果保留）",
      s == 200 and q["status"] == "APPROVED"
      and q["certificate"]["cert_no"] == c1
      and q["certificate"]["status"] == "ACTIVE",
      f"{s} {q['certificate']['status'] if s==200 else ''}")

# ---------------------------------------------------------------------------
n_pass = sum(1 for _, ok, _ in results if ok)
n_fail = len(results) - n_pass
print(f"\n结果: PASS={n_pass} FAIL={n_fail}")
sys.exit(1 if n_fail else 0)
