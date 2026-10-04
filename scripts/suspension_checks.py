"""标准证书临时停用测试：登记/解除/查询、受阻链拦截（单张+批次+批内引用）、
与签发/撤销/冻结快照的关系、提交次序并发。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/suspension_checks.py [base_url] [management_key]
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
MK = sys.argv[2] if len(sys.argv) > 2 else "test-admin-key"
results = []


def call(method, path, body=None, key=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-Management-Key"] = key
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


def ref(no, name=None):
    return call("POST", "/api/v1/reference-certificates", {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": name or ("基准" + no),
        "valid_from": iso(0), "valid_to": iso(1500), "calibrated_at": iso(0)})


def cal(no, parent, conclusion="PASS", vfrom=10, vto=1200, cat=11):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": "仪表" + no,
        "valid_from": iso(vfrom), "valid_to": iso(vto), "calibrated_at": iso(cat),
        "conclusion": conclusion, "parent_cert_no": parent})


def suspend(no, reason="标准器数据异常，待复核", key=MK):
    return call("POST", f"/api/v1/certificates/{no}/suspensions",
                {"cert_no": no, "reason": reason}, key=key)


def lift(no, reason="复核合格，恢复使用", key=MK):
    return call("POST", f"/api/v1/certificates/{no}/suspension-lift",
                {"cert_no": no, "reason": reason}, key=key)


def suspq(no, key=MK):
    return call("GET", f"/api/v1/certificates/{no}/suspensions", key=key)


def make_chain(tag, depth=3):
    """基准 + depth-1 张普通证书链；返回 (基准, [普通证书...])。"""
    r = nxt("R-" + tag)
    check(f"[{tag}] 基准 201", ref(r)[0] == 201)
    cs, prev = [], r
    for _ in range(depth - 1):
        c = nxt("C-" + tag)
        check(f"[{tag}] 普通证书 201", cal(c, prev)[0] == 201)
        cs.append(c)
        prev = c
    return r, cs


# ---------------------------------------------------------------------------
print("== 0. 前置数据（多条独立链） ==")
r_a, (c_a1, c_a2) = make_chain("A", 3)
r_b, (c_b1,) = make_chain("B", 2)
r_ok, (c_ok1,) = make_chain("OK", 2)

# ---------------------------------------------------------------------------
print("== A. 登记停用：管理密钥 / 生效证书 / 非空原因 / 重复停用 ==")
s, e = suspend(r_a, key="wrong-key")
check("错误管理密钥 401", s == 401 and err_code(e) == "invalid_management_key", f"{s} {err_code(e)}")
s, e = call("POST", f"/api/v1/certificates/{r_a}/suspensions",
           {"cert_no": r_a, "reason": "无密钥头"})
check("缺少管理密钥头 401", s == 401 and err_code(e) == "invalid_management_key", f"{s} {err_code(e)}")
s, e = suspend("CERT-NOPE-xyz")
check("证书不存在 404", s == 404 and err_code(e) == "cert_not_found", f"{s} {err_code(e)}")
s, e = suspend(r_a, reason="   ")
check("纯空白原因 422", s == 422 and err_code(e) == "suspension_reason_required", f"{s} {err_code(e)}")

# 先撤销一张证书，再对它停用 -> 只接受生效证书
s, _ = call("POST", f"/api/v1/certificates/{r_b}/revoke", {"reason": "基准超差撤销"})
check("撤销基准 200", s == 200, f"got {s}")
s, e = suspend(r_b)
check("对已撤销证书停用 409 cert_not_active",
      s == 409 and err_code(e) == "cert_not_active", f"{s} {err_code(e)}")

s, body = suspend(r_a, reason="年度比对存疑，暂停作为溯源依据")
check("停用生效证书 201", s == 201, f"got {s} {body}")
ev = body["suspension"]
check("响应含停用事件（编号/原因/时刻）",
      ev["action"] == "SUSPEND" and ev["reason"] == "年度比对存疑，暂停作为溯源依据"
      and isinstance(ev["suspension_id"], int) and bool(ev["at"])
      and ev["lifted_at"] is None and ev["lift_reason"] is None, json.dumps(ev, ensure_ascii=False))
check("视图：当前停用中", body["currently_suspended"] is True and body["active"] == ev)
check("视图：历史含一次 SUSPEND",
      [h["action"] for h in body["history"]] == ["SUSPEND"]
      and body["history"][0]["at"] == ev["at"])
check("证书自身状态不改写（仍 ACTIVE）",
      body["certificate"]["cert_no"] == r_a and body["certificate"]["status"] == "ACTIVE")

s, e = suspend(r_a)
check("重复停用 409 suspension_conflict",
      s == 409 and err_code(e) == "suspension_conflict"
      and e["error"]["details"]["cert_no"] == r_a, f"{s} {err_code(e)}")

# 停用一张链中的普通证书（下游仍 ACTIVE）
r_m, (c_m1, c_m2) = make_chain("MID", 3)
s, _ = suspend(c_m1, reason="中间标准证书待复核")
check("停用链中普通证书 201", s == 201, f"got {s}")

# ---------------------------------------------------------------------------
print("== B. 停用期间新签发受阻（单张 + 命中点指出受阻证书与停用源） ==")
new1 = nxt("NEW-B1")
s, e = cal(new1, r_a)
check("直接引用被停用基准 -> 422 reference_suspended",
      s == 422 and err_code(e) == "reference_suspended", f"{s} {err_code(e)}")
check("受阻证书=直接引用，停用源=被停用基准",
      e["error"]["details"]["blocked_cert_no"] == r_a
      and e["error"]["details"]["suspension_source"] == r_a
      and e["error"]["details"]["cert_no"] == new1,
      json.dumps(e["error"]["details"], ensure_ascii=False))

new2 = nxt("NEW-B2")
s, e = cal(new2, c_a1)
check("引用被停用基准的下游证书 -> 422",
      s == 422 and err_code(e) == "reference_suspended", f"{s} {err_code(e)}")
check("受阻证书=直接父证书，停用源沿链上溯到被停用基准",
      e["error"]["details"]["blocked_cert_no"] == c_a1
      and e["error"]["details"]["suspension_source"] == r_a,
      json.dumps(e["error"]["details"], ensure_ascii=False))

new3 = nxt("NEW-B3")
s, e = cal(new3, c_m2)
check("引用被停用普通证书的下游 -> 422（停用源为链中普通证书）",
      s == 422 and e["error"]["details"]["blocked_cert_no"] == c_m2
      and e["error"]["details"]["suspension_source"] == c_m1,
      f"{s} {json.dumps(e.get('error', {}).get('details'), ensure_ascii=False)}")

s, body = cal(nxt("NEW-OK"), c_ok1)
check("不受影响的链照常签发 201", s == 201, f"got {s}")

# 受阻证书确实未落库
s, _ = call("GET", f"/api/v1/certificates/{new1}")
check("被拒签发证书不落库（404）", s == 404, f"got {s}")

# ---------------------------------------------------------------------------
print("== C. 交接批次同样校验：外部受阻 / 批内引用 / 整批不落库 ==")
bk1 = nxt("BK-EXT")
s, e = call("POST", "/api/v1/certificate-batches", {"batch_key": bk1, "certificates": [
    {"kind": "CALIBRATION", "cert_no": nxt("BC-1"), "device_id": "D1",
     "valid_from": iso(20), "valid_to": iso(1000), "calibrated_at": iso(21),
     "conclusion": "PASS", "parent_cert_no": c_a2},
]})
check("批次外部引用受阻链 -> 422 且定位出错证书",
      s == 422 and err_code(e) == "reference_suspended"
      and e["error"]["details"]["cert_no"], f"{s} {err_code(e)}")
check("批次错误指出受阻证书与停用源",
      e["error"]["details"]["blocked_cert_no"] == c_a2
      and e["error"]["details"]["suspension_source"] == r_a,
      json.dumps(e["error"]["details"], ensure_ascii=False))

# 批内引用：本批 A 引用库内被停用基准，本批 B 引用 A（列表乱序）
bk2 = nxt("BK-IN")
in_a, in_b = nxt("BIA"), nxt("BIB")
s, e = call("POST", "/api/v1/certificate-batches", {"batch_key": bk2, "certificates": [
    {"kind": "CALIBRATION", "cert_no": in_b, "device_id": "D-IN-B",
     "valid_from": iso(20), "valid_to": iso(1000), "calibrated_at": iso(22),
     "conclusion": "PASS", "parent_cert_no": in_a},
    {"kind": "CALIBRATION", "cert_no": in_a, "device_id": "D-IN-A",
     "valid_from": iso(20), "valid_to": iso(1000), "calibrated_at": iso(21),
     "conclusion": "PASS", "parent_cert_no": r_a},
]})
check("批内引用追溯到库内停用源 -> 422（拓扑校验后逐张拦截）",
      s == 422 and err_code(e) == "reference_suspended"
      and e["error"]["details"]["cert_no"] == in_a
      and e["error"]["details"]["suspension_source"] == r_a,
      f"{s} {json.dumps(e.get('error', {}).get('details'), ensure_ascii=False)}")
s, _ = call("GET", f"/api/v1/certificate-batches/{bk2}")
check("受阻批次不生成批次快照（404）", s == 404, f"got {s}")
s, _ = call("GET", f"/api/v1/certificates/{in_a}")
check("受阻批次整批不落库", s == 404, f"got {s}")

# 同键在停用期间重试：仍然被拒（没有首次快照可重放）
s, e = call("POST", "/api/v1/certificate-batches", {"batch_key": bk2, "certificates": [
    {"kind": "CALIBRATION", "cert_no": in_b, "device_id": "D-IN-B",
     "valid_from": iso(20), "valid_to": iso(1000), "calibrated_at": iso(22),
     "conclusion": "PASS", "parent_cert_no": in_a},
    {"kind": "CALIBRATION", "cert_no": in_a, "device_id": "D-IN-A",
     "valid_from": iso(20), "valid_to": iso(1000), "calibrated_at": iso(21),
     "conclusion": "PASS", "parent_cert_no": r_a},
]})
check("停用期间同键重试仍被拒", s == 422 and err_code(e) == "reference_suspended", f"{s}")

# ---------------------------------------------------------------------------
print("== D. 解除停用：密钥/原因/生效中约束；解除后恢复签发 ==")
s, e = lift(r_a, key="bad")
check("解除密钥错误 401", s == 401 and err_code(e) == "invalid_management_key", f"{s}")
s, e = lift(r_a, reason="  ")
check("解除原因为空 422", s == 422 and err_code(e) == "suspension_reason_required", f"{s}")
s, e = lift("CERT-NOPE-xyz")
check("解除未知证书 404", s == 404 and err_code(e) == "cert_not_found", f"{s}")

s, body = lift(r_a, reason="复核通过：比对结果合格")
check("解除停用 200", s == 200, f"got {s}")
lev = body["lift"]
check("解除事件 LIFT 与原因/时刻",
      lev["action"] == "LIFT" and lev["reason"] == "复核通过：比对结果合格"
      and lev["at"] == lev["lifted_at"] and lev["at"] >= body["history"][0]["at"],
      json.dumps(lev, ensure_ascii=False))
check("解除后当前不在停用中", body["currently_suspended"] is False and body["active"] is None)
check("历史完整：SUSPEND 后 LIFT（时间与原因齐全）",
      [(h["action"], bool(h["reason"]) and bool(h["at"])) for h in body["history"]]
      == [("SUSPEND", True), ("LIFT", True)], json.dumps(body["history"], ensure_ascii=False))

s, e = lift(r_a)
check("无生效停用时再解除 409 suspension_not_active",
      s == 409 and err_code(e) == "suspension_not_active"
      and e["error"]["details"]["cert_no"] == r_a, f"{s} {err_code(e)}")

# 解除后此前受阻的签发恢复
s, body = cal(new1, r_a)
check("解除后单张签发恢复 201", s == 201, f"got {s}")
# 此前受阻批次同键同内容现在首次签发成功，再重试返回首次快照
s, body1 = call("POST", "/api/v1/certificate-batches", {"batch_key": bk2, "certificates": [
    {"kind": "CALIBRATION", "cert_no": in_b, "device_id": "D-IN-B",
     "valid_from": iso(20), "valid_to": iso(1000), "calibrated_at": iso(22),
     "conclusion": "PASS", "parent_cert_no": in_a},
    {"kind": "CALIBRATION", "cert_no": in_a, "device_id": "D-IN-A",
     "valid_from": iso(20), "valid_to": iso(1000), "calibrated_at": iso(21),
     "conclusion": "PASS", "parent_cert_no": r_a},
]})
check("解除后受阻批次首次签发 201", s == 201 and body1["count"] == 2, f"got {s}")
snapshot1 = json.dumps(body1, sort_keys=True, ensure_ascii=False)
s, body2 = call("POST", "/api/v1/certificate-batches", {"batch_key": bk2, "certificates": [
    {"kind": "CALIBRATION", "cert_no": in_b, "device_id": "D-IN-B",
     "valid_from": iso(20), "valid_to": iso(1000), "calibrated_at": iso(22),
     "conclusion": "PASS", "parent_cert_no": in_a},
    {"kind": "CALIBRATION", "cert_no": in_a, "device_id": "D-IN-A",
     "valid_from": iso(20), "valid_to": iso(1000), "calibrated_at": iso(21),
     "conclusion": "PASS", "parent_cert_no": r_a},
]})
check("同键同内容重试 200 且返回首次签发快照", s == 200
      and json.dumps(body2, sort_keys=True, ensure_ascii=False) == snapshot1, f"got {s}")

# 再次停用 -> 可再次登记，历史追加第二轮
s, body = suspend(r_a, reason="下一轮复核暂停")
check("解除后可再次停用 201", s == 201 and body["currently_suspended"] is True, f"got {s}")
check("第二轮停用追加在历史末尾",
      [h["action"] for h in body["history"]] == ["SUSPEND", "LIFT", "SUSPEND"],
      str([h["action"] for h in body["history"]]))
s, _ = lift(r_a)

# ---------------------------------------------------------------------------
print("== E. 查询接口：密钥/未知证书/无记录证书/撤销后仍可查 ==")
s, e = suspq(r_a, key="nope")
check("查询密钥错误 401", s == 401 and err_code(e) == "invalid_management_key", f"{s}")
s, e = suspq("CERT-NOPE-xyz")
check("查询未知证书 404", s == 404 and err_code(e) == "cert_not_found", f"{s}")
s, q = suspq(c_ok1)
check("从无停用记录：currently_suspended=false/active=null/history=[]",
      s == 200 and q["currently_suspended"] is False and q["active"] is None
      and q["history"] == [] and q["certificate"]["cert_no"] == c_ok1, f"{s}")
s, q = suspq(r_b)
check("已撤销证书的停用视图仍可查（证书状态如实为 REVOKED，停用记录为空）",
      s == 200 and q["certificate"]["status"] == "REVOKED"
      and q["currently_suspended"] is False, f"{s}")

# ---------------------------------------------------------------------------
print("== F. 停用不改写既有内容/撤销状态/冻结快照；与交付、更正互不干扰 ==")
# 先归档、再停用：冻结快照不变；停用不阻止新归档与 ACCEPT（证书仍 ACTIVE）
r_f, (c_f1, c_f2) = make_chain("FROZEN", 3)
arch = nxt("ARCH-F")
s, _ = call("POST", "/api/v1/delivery-archives",
             {"archive_no": arch, "cert_nos": [c_f1, c_f2]})
check("停用前创建归档包 201", s == 201, f"got {s}")
snap_before = call("GET", f"/api/v1/delivery-archives/{arch}")[1]
s, _ = suspend(r_f, reason="冻结快照回归测试停用")
check("停用基准 201", s == 201)
snap_after = call("GET", f"/api/v1/delivery-archives/{arch}")[1]
check("已冻结交付快照不因停用改写", snap_after == snap_before)
frozen_status = [n["certificate"]["status"] for n in snap_after["items"][1]["chain"]["nodes"]]
check("快照链状态仍冻结 ACTIVE", frozen_status == ["ACTIVE"] * len(frozen_status), str(frozen_status))

# 停用期间仍可创建交付归档（停用不是撤销，证书仍 ACTIVE/PASS）
arch2 = nxt("ARCH-F2")
s, _ = call("POST", "/api/v1/delivery-archives",
            {"archive_no": arch2, "cert_nos": [c_f2]})
check("停用期间新归档包仍可创建（不与撤销规则耦合）", s == 201, f"got {s}")

# 停用期间 ACCEPT 仍成立（无撤销/失效）
s, inv = call("POST", "/api/v1/delivery-invitations",
             {"archive_no": arch2, "recipient": "冻结测试客户", "deadline": iso(1000)}, key=MK)
check("停用期间创建签收邀请 201", s == 201, f"got {s}")
s, rcpt = call("POST", "/api/v1/delivery-receipts",
               {"invitation_no": inv["invitation_no"], "key": inv["key"], "decision": "ACCEPT"})
check("停用期间 ACCEPT 仍成立（仅撤销/失效拦截）", s == 201 and rcpt["decision"] == "ACCEPT", f"got {s}")

# 更正声明在停用期间照常登记
s, corr = call("POST", "/api/v1/correction-declarations", {
    "declaration_no": nxt("CORR-F"), "cert_no": c_f1,
    "corrections": {"remark": "停用期间登记的更正"}, "reason": "展示字段纠错"})
check("停用期间更正声明可登记 201", s == 201, f"got {s}")

# 撤销与停用并存：撤销改状态，停用记录独立保留
s, rev = call("POST", f"/api/v1/certificates/{r_f}/revoke",
              {"reason": "停用期间又发现超差，正式撤销", "operator": "qa"})
check("停用期间撤销 200，下游级联失效", s == 200 and rev["affected_count"] == 2, f"got {s}")
s, cert = call("GET", f"/api/v1/certificates/{r_f}")
check("被停用证书撤销后状态为 REVOKED", cert["status"] == "REVOKED")
s, cert = call("GET", f"/api/v1/certificates/{c_f2}")
check("下游仍级联 INVALID", cert["status"] == "INVALID")
s, q = suspq(r_f)
check("撤销不改写停用状态：仍显示生效停用 + 证书状态 REVOKED",
      s == 200 and q["currently_suspended"] is True and q["certificate"]["status"] == "REVOKED",
      f"{s} {q['certificate']['status']}")
s, e = suspend(r_f)
check("撤销后不能重复停用（非 ACTIVE）", s == 409 and err_code(e) == "cert_not_active", f"{s}")
# 已撤销证书上的停用仍可解除（复核流程独立于撤销）
s, q = lift(r_f, reason="复核结论记录：与撤销原因区分留档")
check("撤销后仍可解除停用（停用生命周期闭环）",
      s == 200 and q["currently_suspended"] is False
      and q["certificate"]["status"] == "REVOKED", f"got {s}")
snap_final = call("GET", f"/api/v1/delivery-archives/{arch}")[1]
check("撤销后归档快照仍不被改写", snap_final == snap_before)
s, rcpt2 = call("GET", f"/api/v1/delivery-archives/{arch2}/receipt", key=MK)
check("定稿记录不被停用/撤销改写", s == 200 and rcpt2 == rcpt, f"{s}")

# 既有批次首次签发快照不被停用/撤销改写
s, qb = call("GET", f"/api/v1/certificate-batches/{bk2}")
check("批次首次签发快照保持不变", s == 200
      and json.dumps(qb, sort_keys=True, ensure_ascii=False) == snapshot1, f"{s}")

# ---------------------------------------------------------------------------
print("== G. 并发：停用/解除与签发按提交次序生效 ==")
N = 16
chains = []
for i in range(N):
    rr, (cc,) = make_chain(f"RACE{i}", 2)
    chains.append((rr, cc, nxt(f"NW{i}")))
barrier = threading.Barrier(N * 2)
race = {}
rlock = threading.Lock()


def race_issue(idx, rr, new_no):
    barrier.wait()
    s, b = cal(new_no, rr)
    with rlock:
        race[idx] = (s, b.get("error", {}).get("code"))


def race_suspend(idx, rr):
    barrier.wait()
    s, b = suspend(rr, reason=f"并发停用 {idx}")
    with rlock:
        race[(idx, "su")] = (s, b.get("error", {}).get("code"))


threads = []
for idx, (rr, cc, new_no) in enumerate(chains):
    threads.append(threading.Thread(target=race_issue, args=(idx, rr, new_no)))
    threads.append(threading.Thread(target=race_suspend, args=(idx, rr)))
for t in threads:
    t.start()
for t in threads:
    t.join()

n_issued = n_blocked = 0
for idx, (rr, cc, new_no) in enumerate(chains):
    su_s, su_code = race[(idx, "su")]
    check(f"[{idx}] 停用恒成功", su_s == 201, str(race[(idx, "su")]))
    st, code = race[idx]
    if st == 201:
        n_issued += 1
        # 先签发成功者：子证书在停用前已存在（停用不溯及既有证书）
        s, c = call("GET", f"/api/v1/certificates/{new_no}")
        check(f"[{idx}] 签发先提交：证书落库且仍可查", s == 200 and c["status"] == "ACTIVE")
    else:
        n_blocked += 1
        check(f"[{idx}] 停用先提交：引用受阻链 422",
              st == 422 and code == "reference_suspended", str((st, code)))
check(f"停用×签发并发两类结果均出现且守恒（{n_issued} 先签发 / {n_blocked} 先停用）",
      n_issued + n_blocked == N and n_issued > 0 and n_blocked > 0,
      f"{n_issued}+{n_blocked}!={N}")

# 解除 × 受阻签发并发：先解除则 201，先停用语义仍在则 422
N2 = 16
chains2 = []
for i in range(N2):
    rr, (cc,) = make_chain(f"LIFT{i}", 2)
    s, _ = suspend(rr, reason=f"解除并发前置停用 {i}")
    assert s == 201
    chains2.append((rr, nxt(f"LW{i}")))
barrier2 = threading.Barrier(N2 * 2)
race2 = {}
lk2 = threading.Lock()


def race_issue2(idx, rr, new_no):
    barrier2.wait()
    s, b = cal(new_no, rr)
    with lk2:
        race2[idx] = (s, b.get("error", {}).get("code"))


def race_lift(idx, rr):
    barrier2.wait()
    s, b = lift(rr, reason=f"并发解除 {idx}")
    with lk2:
        race2[(idx, "lf")] = (s, b.get("error", {}).get("code"))


threads = []
for idx, (rr, new_no) in enumerate(chains2):
    threads.append(threading.Thread(target=race_issue2, args=(idx, rr, new_no)))
    threads.append(threading.Thread(target=race_lift, args=(idx, rr)))
for t in threads:
    t.start()
for t in threads:
    t.join()

n201 = n422 = 0
for idx, (rr, new_no) in enumerate(chains2):
    lf_s, lf_code = race2[(idx, "lf")]
    check(f"[{idx}] 解除恒成功", lf_s == 200, str(race2[(idx, "lf")]))
    st, code = race2[idx]
    if st == 201:
        n201 += 1
    else:
        n422 += 1
        check(f"[{idx}] 签发先于解除提交：仍按停用中拒绝",
              code == "reference_suspended", str((st, code)))
check(f"解除×签发并发两类结果均出现且守恒（{n201} 先解除 / {n422} 签发先到）",
      n201 + n422 == N2 and n201 > 0 and n422 > 0, f"{n201}+{n422}!={N2}")

# ---------------------------------------------------------------------------
print("== H. 既有接口契约保持 ==")
s, idx = call("GET", "/")
eps = idx["endpoints"]
check("根索引列出停用/解除/查询端点", s == 200
      and any(x.endswith("/certificates/{cert_no}/suspensions") for x in eps)
      and any(x.endswith("/certificates/{cert_no}/suspension-lift") for x in eps),
      str(eps))
s, c = call("GET", f"/api/v1/certificates/{c_ok1}")
check("普通证书查询契约不变", s == 200 and c["cert_no"] == c_ok1 and c["status"] == "ACTIVE")

# ---------------------------------------------------------------------------
n_pass = sum(1 for _, ok, _ in results if ok)
n_fail = len(results) - n_pass
print(f"\n结果: PASS={n_pass} FAIL={n_fail}")
sys.exit(1 if n_fail else 0)
