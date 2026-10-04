"""交付签收邀请/定稿测试：一次性密钥、截止、每包一份定稿、撤销拦截与并发次序。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/receipt_checks.py [base_url]

依赖：DELIVERY_MANAGEMENT_KEY 默认为 test-admin-key（与其余 *_checks.py 相同，
直接对按默认/测试配置启动的服务运行即可）。
"""
import json
import sys
import threading
import time
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


def future(seconds=3600):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def ref(no, name=None):
    return call("POST", "/api/v1/reference-certificates", {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": name or ("基准" + no),
        "valid_from": iso(0), "valid_to": iso(1500), "calibrated_at": iso(0)})


def cal(no, parent, conclusion="PASS"):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": "仪表" + no,
        "valid_from": iso(2), "valid_to": iso(1200), "calibrated_at": iso(3),
        "conclusion": conclusion, "parent_cert_no": parent})


def make_chain(tag, depth=2):
    """基准 + depth-1 张普通证书链；返回 (基准号, [普通证书号...])。"""
    r = nxt("RC-" + tag)
    check(f"[{tag}] 基准 201", ref(r)[0] == 201)
    cs = []
    prev = r
    for _ in range(depth - 1):
        c = nxt("CC-" + tag)
        check(f"[{tag}] 普通证书 201", cal(c, prev)[0] == 201)
        cs.append(c)
        prev = c
    return r, cs


def make_archive(tag, cert_nos):
    no = nxt("DA-" + tag)
    s, _ = call("POST", "/api/v1/delivery-archives", {"archive_no": no, "cert_nos": cert_nos})
    check(f"[{tag}] 归档包 {no} 201", s == 201, f"got {s}")
    return no


def make_invite(archive_no, recipient="测试客户", deadline=None, mkey=MK):
    return call("POST", "/api/v1/delivery-invitations", {
        "archive_no": archive_no, "recipient": recipient,
        "deadline": deadline or iso(1000)}, key=mkey)


# ---------------------------------------------------------------------------
print("== 0. 前置数据（多条独立链） ==")
r1, (c1, c2) = make_chain("OK", 3)
r_rej, (c_rej,) = make_chain("REJ", 2)
a1 = make_archive("OK", [c1, c2])
a_rej = make_archive("REJ", [c_rej])

# ---------------------------------------------------------------------------
print("== A. 创建邀请：管理密钥 / 归档存在 / 未来截止 ==")
s, e = make_invite(a1, mkey="wrong-admin-key")
check("错误管理密钥 401", s == 401 and err_code(e) == "invalid_management_key", f"{s} {err_code(e)}")
s, e = call("POST", "/api/v1/delivery-invitations",
            {"archive_no": a1, "recipient": "测试客户", "deadline": iso(1000)})
check("缺少管理密钥头 401", s == 401 and err_code(e) == "invalid_management_key", f"{s} {err_code(e)}")
s, e = make_invite("DA-NOT-EXIST-xyz")
check("归档包不存在 404", s == 404 and err_code(e) == "archive_not_found", f"{s} {err_code(e)}")
s, e = make_invite(a1, deadline=iso(-1))
check("截止时间非未来值 422", s == 422 and err_code(e) == "invitation_deadline_past", f"{s} {err_code(e)}")
s, e = make_invite(a1, recipient="   ")
check("收件人空白 422", s == 422 and err_code(e) == "invitation_recipient_required", f"{s} {err_code(e)}")

s, inv = make_invite(a1, recipient="客户甲", deadline=iso(1000))
check("创建邀请 201", s == 201, f"got {s}")
check("返回邀请编号", s == 201 and inv["invitation_no"].startswith("DINV-"), str(inv.get("invitation_no")))
check("一次性随机密钥仅展示一次（URL-safe、长度足够）",
      s == 201 and isinstance(inv["key"], str) and len(inv["key"]) >= 32, str(inv.get("key")))
check("回显归档号/收件人/截止",
      inv["archive_no"] == a1 and inv["recipient"] == "客户甲" and bool(inv["deadline"])
      and bool(inv["created_at"]))
inv_no, secret = inv["invitation_no"], inv["key"]

# 再次创建（可生成不同编号/密钥；同一归档允许多份邀请）
s, inv_alt = make_invite(a1, recipient="客户甲-副本", deadline=iso(1000))
check("同一归档可再建邀请", s == 201 and inv_alt["invitation_no"] != inv_no
      and inv_alt["key"] != secret, f"got {s}")

# ---------------------------------------------------------------------------
print("== B. 提交前置校验：邀请/密钥/截止/拒收原因 ==")
s, e = call("POST", "/api/v1/delivery-receipts",
            {"invitation_no": "DINV-0000000000000000", "key": secret, "decision": "ACCEPT"})
check("未知邀请 404", s == 404 and err_code(e) == "invitation_not_found", f"{s} {err_code(e)}")
s, e = call("POST", "/api/v1/delivery-receipts",
            {"invitation_no": inv_no, "key": "bad-secret", "decision": "ACCEPT"})
check("错误一次性密钥 401", s == 401 and err_code(e) == "invalid_receipt_key", f"{s} {err_code(e)}")
s, e = call("POST", "/api/v1/delivery-receipts",
            {"invitation_no": inv_no, "key": secret, "decision": "REJECT"})
check("REJECT 缺少原因 422", s == 422 and err_code(e) == "receipt_reason_required", f"{s} {err_code(e)}")
s, e = call("POST", "/api/v1/delivery-receipts",
            {"invitation_no": inv_no, "key": secret, "decision": "REJECT", "reason": "   "})
check("REJECT 纯空白原因 422", s == 422 and err_code(e) == "receipt_reason_required", f"{s}")

# ---------------------------------------------------------------------------
print("== C. ACCEPT 定稿 + 同邀请同内容重放（返回首次结果） ==")
s, receipt = call("POST", "/api/v1/delivery-receipts",
                  {"invitation_no": inv_no, "key": secret, "decision": "ACCEPT"})
check("ACCEPT 首次定稿 201", s == 201, f"got {s} {receipt}")
check("定稿内容（归档号/收件人/结论/原因/签收时刻）",
      receipt["archive_no"] == a1 and receipt["invitation_no"] == inv_no
      and receipt["recipient"] == "客户甲" and receipt["decision"] == "ACCEPT"
      and receipt["reason"] is None and bool(receipt["signed_at"]),
      json.dumps(receipt, ensure_ascii=False))
first_signed_at = receipt["signed_at"]

s, replay = call("POST", "/api/v1/delivery-receipts",
                 {"invitation_no": inv_no, "key": secret, "decision": "ACCEPT"})
check("同邀请同内容重试 200", s == 200, f"got {s}")
check("重试返回首次结果（逐字段一致、签收时刻不变）", replay == receipt
      and replay["signed_at"] == first_signed_at)

# ---------------------------------------------------------------------------
print("== D. 不同内容 / 其他邀请再提交均冲突 ==")
s, e = call("POST", "/api/v1/delivery-receipts",
            {"invitation_no": inv_no, "key": secret, "decision": "REJECT", "reason": "改主意了"})
check("同一邀请不同内容 409", s == 409 and err_code(e) == "receipt_conflict", f"{s} {err_code(e)}")
check("冲突详情点名首次定稿的邀请与结论",
      e["error"]["details"]["finalized_invitation_no"] == inv_no
      and e["error"]["details"]["finalized_decision"] == "ACCEPT")
s, e = call("POST", "/api/v1/delivery-receipts",
            {"invitation_no": inv_alt["invitation_no"], "key": inv_alt["key"], "decision": "ACCEPT"})
check("其他邀请再提交 409", s == 409 and err_code(e) == "receipt_conflict", f"{s} {err_code(e)}")

# ---------------------------------------------------------------------------
print("== E. 管理密钥查询定稿记录 ==")
s, q = call("GET", f"/api/v1/delivery-archives/{a1}/receipt", key=MK)
check("凭管理密钥查询 200 且与定稿一致", s == 200 and q == receipt, f"{s}")
s, e = call("GET", f"/api/v1/delivery-archives/{a1}/receipt", key="nope")
check("查询密钥错误 401", s == 401 and err_code(e) == "invalid_management_key", f"{s}")
s, e = call("GET", "/api/v1/delivery-archives/DA-NOT-EXIST-xyz/receipt", key=MK)
check("查询未知归档 404", s == 404 and err_code(e) == "archive_not_found", f"{s}")
a_pending = make_archive("PENDING", [c1])
s, e = call("GET", f"/api/v1/delivery-archives/{a_pending}/receipt", key=MK)
check("归档存在但未定稿 404 receipt_not_found",
      s == 404 and err_code(e) == "receipt_not_found", f"{s} {err_code(e)}")

# ---------------------------------------------------------------------------
print("== F. ACCEPT 时目标或冻结链已撤销/失效 -> 拒绝并点名；REJECT 仍可提交 ==")
s, inv_rej = make_invite(a_rej, recipient="拒收客户")
check("拒收链邀请 201", s == 201)
# 撤销基准：目标 c_rej 级联失效，且其冻结链含被撤销基准
s, _ = call("POST", f"/api/v1/certificates/{r_rej}/revoke", {"reason": "基准超差", "operator": "qa"})
check("撤销基准 200", s == 200, f"got {s}")
s, cur = call("GET", f"/api/v1/certificates/{c_rej}")
check("目标已级联失效", cur["status"] == "INVALID")
s, e = call("POST", "/api/v1/delivery-receipts",
            {"invitation_no": inv_rej["invitation_no"], "key": inv_rej["key"], "decision": "ACCEPT"})
check("链上撤销后 ACCEPT 422", s == 422 and err_code(e) == "receipt_targets_revoked",
      f"{s} {err_code(e)}")
check("列出受影响目标", e["error"]["details"]["affected_targets"] == [c_rej],
      str(e["error"]["details"].get("affected_targets")))
s, rr = call("POST", "/api/v1/delivery-receipts", {
    "invitation_no": inv_rej["invitation_no"], "key": inv_rej["key"],
    "decision": "REJECT", "reason": "证书已被撤销，客户拒收"})
check("撤销后 REJECT 仍可提交 201", s == 201 and rr["decision"] == "REJECT"
      and rr["reason"] == "证书已被撤销，客户拒收", f"{s}")
# 归档快照不被后续撤销改写
s, snap = call("GET", f"/api/v1/delivery-archives/{a_rej}")
frozen = [n["certificate"]["status"] for n in snap["items"][0]["chain"]["nodes"]]
check("归档快照链状态仍冻结为 ACTIVE", frozen == ["ACTIVE"] * len(frozen), str(frozen))
s, q = call("GET", f"/api/v1/delivery-archives/{a_rej}/receipt", key=MK)
check("定稿记录不被后续撤销改写", s == 200 and q["decision"] == "REJECT" and q == rr)

# 目标自身被撤销（非链上）也点名
r_self, (c_self,) = make_chain("SELF", 2)
a_self = make_archive("SELF", [c_self])
s, inv_self = make_invite(a_self)
call("POST", f"/api/v1/certificates/{c_self}/revoke", {"reason": "目标自身撤销"})
s, e = call("POST", "/api/v1/delivery-receipts",
            {"invitation_no": inv_self["invitation_no"], "key": inv_self["key"], "decision": "ACCEPT"})
check("目标自身撤销后 ACCEPT 422 点名目标",
      s == 422 and e["error"]["details"]["affected_targets"] == [c_self], f"{s}")

# 多目标包：仅部分链受影响 -> 只点名受影响目标
r_mix, (cm_a,) = make_chain("MIX-A", 2)
r_mix_b, (cm_b,) = make_chain("MIX-B", 2)
a_mix = make_archive("MIX", [cm_a, cm_b])
s, inv_mix = make_invite(a_mix)
call("POST", f"/api/v1/certificates/{r_mix_b}/revoke", {"reason": "仅B链撤销"})
s, e = call("POST", "/api/v1/delivery-receipts",
            {"invitation_no": inv_mix["invitation_no"], "key": inv_mix["key"], "decision": "ACCEPT"})
check("多目标包仅点名受影响目标",
      s == 422 and e["error"]["details"]["affected_targets"] == [cm_b],
      str(e.get("error", {}).get("details", {}).get("affected_targets")))

# ---------------------------------------------------------------------------
print("== G. 截止语义：未定稿过期不能签收；定稿后截止后仍可重放 ==")
r_g, (c_g,) = make_chain("EXP", 2)
a_g = make_archive("EXP", [c_g])
s, inv_exp = make_invite(a_g, deadline=future(2))
check("短截止邀请 201", s == 201)
time.sleep(2.3)
s, e = call("POST", "/api/v1/delivery-receipts",
            {"invitation_no": inv_exp["invitation_no"], "key": inv_exp["key"], "decision": "ACCEPT"})
check("未定稿过期后 ACCEPT 409", s == 409 and err_code(e) == "invitation_expired", f"{s} {err_code(e)}")
s, e = call("POST", "/api/v1/delivery-receipts", {
    "invitation_no": inv_exp["invitation_no"], "key": inv_exp["key"],
    "decision": "REJECT", "reason": "过期拒收"})
check("未定稿过期后 REJECT 同样 409", s == 409 and err_code(e) == "invitation_expired", f"{s}")
s, q = call("GET", f"/api/v1/delivery-archives/{a_g}/receipt", key=MK)
check("过期未定稿仍查不到记录", s == 404 and err_code(q) == "receipt_not_found", f"{s}")

r_g2, (c_g2,) = make_chain("EXP2", 2)
a_g2 = make_archive("EXP2", [c_g2])
s, inv_ok = make_invite(a_g2, deadline=future(2))
s, before = call("POST", "/api/v1/delivery-receipts",
                 {"invitation_no": inv_ok["invitation_no"], "key": inv_ok["key"], "decision": "ACCEPT"})
check("截止前定稿 201", s == 201, f"got {s}")
time.sleep(2.3)
s, after = call("POST", "/api/v1/delivery-receipts",
                {"invitation_no": inv_ok["invitation_no"], "key": inv_ok["key"], "decision": "ACCEPT"})
check("定稿后截止后同内容仍返回首次结果 200", s == 200 and after == before, f"{s}")

# ---------------------------------------------------------------------------
print("== H. 并发：签收与撤销按提交次序；同包定稿唯一 ==")

# H1: N 条独立链，ACCEPT 与撤销基准同时发车
N = 16
chains = []
for i in range(N):
    rr, (cc,) = make_chain(f"RACE{i}", 2)
    aa = make_archive(f"RACE{i}", [cc])
    s, ii = make_invite(aa)
    assert s == 201
    chains.append((rr, cc, aa, ii))
barrier = threading.Barrier(N * 2)
res = {}
rlock = threading.Lock()


def race_accept(idx, cc, ii):
    barrier.wait()
    s, b = call("POST", "/api/v1/delivery-receipts",
                {"invitation_no": ii["invitation_no"], "key": ii["key"], "decision": "ACCEPT"})
    with rlock:
        res[idx] = ("accept", s, b.get("error", {}).get("code"))


def race_revoke(idx, rr):
    barrier.wait()
    s, b = call("POST", f"/api/v1/certificates/{rr}/revoke", {"reason": "并发撤销"})
    with rlock:
        res[(idx, "rv")] = (s, b.get("error", {}).get("code"))


threads = []
for idx, (rr, cc, aa, ii) in enumerate(chains):
    threads.append(threading.Thread(target=race_accept, args=(idx, cc, ii)))
    threads.append(threading.Thread(target=race_revoke, args=(idx, rr)))
for t in threads:
    t.start()
for t in threads:
    t.join()

n_ok = n_block = 0
for idx, (rr, cc, aa, ii) in enumerate(chains):
    rv_status, _ = res[(idx, "rv")]
    check(f"[{idx}] 撤销恒成功（ACCEPT 不改证书状态）", rv_status == 200, str(res[(idx, "rv")]))
    _, a_status, a_code = res[idx]
    if a_status == 201:
        n_ok += 1
        s, q = call("GET", f"/api/v1/delivery-archives/{aa}/receipt", key=MK)
        check(f"[{idx}] 签收先提交：定稿 ACCEPT 且不被随后撤销改写",
              s == 200 and q["decision"] == "ACCEPT")
    else:
        n_block += 1
        check(f"[{idx}] 撤销先提交：ACCEPT 被 422 点名",
              a_status == 422 and a_code == "receipt_targets_revoked", f"{a_status} {a_code}")
        s, e = call("GET", f"/api/v1/delivery-archives/{aa}/receipt", key=MK)
        check(f"[{idx}] 被拒 ACCEPT 不留定稿",
              s == 404 and err_code(e) == "receipt_not_found", f"{s}")
check(f"并发次序两类结果均出现且总数守恒（{n_ok} 先签收 / {n_block} 先撤销）",
      n_ok + n_block == N and n_ok > 0 and n_block > 0, f"{n_ok}+{n_block}!={N}")

# H2: 同一邀请同一内容并发 10 份：恰好 1 个 201，其余 200，响应体一致
rr, (cc,) = make_chain("SAME", 2)
aa = make_archive("SAME", [cc])
s, ii = make_invite(aa)
M = 10
bar = threading.Barrier(M)
outs = []
olk = threading.Lock()


def same_submit():
    bar.wait()
    s, b = call("POST", "/api/v1/delivery-receipts",
                {"invitation_no": ii["invitation_no"], "key": ii["key"], "decision": "ACCEPT"})
    with olk:
        outs.append((s, b))


ths = [threading.Thread(target=same_submit) for _ in range(M)]
for t in ths:
    t.start()
for t in ths:
    t.join()
codes = sorted(s for s, _ in outs)
check("同内容并发：1×201 其余 200", codes == [200] * (M - 1) + [201], str(codes))
check("同内容并发：响应体全部等于首次结果",
      len({json.dumps(b, sort_keys=True, ensure_ascii=False) for _, b in outs}) == 1)

# H3: 同一邀请互异内容并发 10 份：恰好 1 个 201，其余 409
rr, (cc,) = make_chain("DIFF", 2)
aa = make_archive("DIFF", [cc])
s, ii = make_invite(aa)
bar = threading.Barrier(M)
douts = []
dlk = threading.Lock()


def diff_submit(k):
    bar.wait()
    s, b = call("POST", "/api/v1/delivery-receipts", {
        "invitation_no": ii["invitation_no"], "key": ii["key"],
        "decision": "REJECT", "reason": f"互异原因-{k}"})
    with dlk:
        douts.append(s)


ths = [threading.Thread(target=diff_submit, args=(k,)) for k in range(M)]
for t in ths:
    t.start()
for t in ths:
    t.join()
dcodes = sorted(douts)
check("异内容并发：1×201 其余 409", dcodes == [201] + [409] * (M - 1), str(dcodes))

# H4: 两份不同邀请并发 ACCEPT 同一归档：恰好 1 个 201，另 1 个 409
rr, (cc,) = make_chain("TWO", 2)
aa = make_archive("TWO", [cc])
s, i_a = make_invite(aa, recipient="邀请甲")
s, i_b = make_invite(aa, recipient="邀请乙")
bar = threading.Barrier(2)
wouts = []
wlk = threading.Lock()


def invite_submit(inv):
    bar.wait()
    s, b = call("POST", "/api/v1/delivery-receipts",
                {"invitation_no": inv["invitation_no"], "key": inv["key"], "decision": "ACCEPT"})
    with wlk:
        wouts.append((s, b))


ths = [threading.Thread(target=invite_submit, args=(x,)) for x in (i_a, i_b)]
for t in ths:
    t.start()
for t in ths:
    t.join()
wcodes = sorted(s for s, _ in wouts)
check("两份邀请并发：1×201 + 1×409", wcodes == [201, 409], str(wcodes))
s, q = call("GET", f"/api/v1/delivery-archives/{aa}/receipt", key=MK)
check("仅胜方邀请留档", s == 200 and q["invitation_no"] in (i_a["invitation_no"], i_b["invitation_no"]))

# ---------------------------------------------------------------------------
print("== I. 既有接口契约保持 ==")
s, cert = call("GET", f"/api/v1/certificates/{c1}")
check("证书详情仍返回当前状态（撤销链上为当前态）", s == 200 and cert["cert_no"] == c1)
s, idx = call("GET", "/")
eps = idx["endpoints"]
check("根索引列出新端点", s == 200
      and any(x.endswith("/api/v1/delivery-invitations") for x in eps)
      and any(x.endswith("/api/v1/delivery-receipts") for x in eps)
      and "/api/v1/delivery-archives/{archive_no}/receipt" in " ".join(eps))

# ---------------------------------------------------------------------------
n_pass = sum(1 for _, ok, _ in results if ok)
n_fail = len(results) - n_pass
print(f"\n结果: PASS={n_pass} FAIL={n_fail}")
sys.exit(1 if n_fail else 0)
