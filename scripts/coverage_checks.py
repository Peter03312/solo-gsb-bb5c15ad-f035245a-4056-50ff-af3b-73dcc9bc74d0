"""设备校准覆盖审计测试（GET /api/v1/devices/{device_id}/coverage-audit）。

覆盖：计入窗口起点=max(签发创建时刻, 有效期起点)、终点=min(有效期终点,
撤销/级联失效时刻)；撤销截断与撤销后重签（有效期重叠也按实际事件还原）；
级联失效时刻截断下游；上游事后到期不取消下游覆盖；未来生效/签发时已过期/
FAIL/基准证书均不提供覆盖；未知设备整段空窗；片段完整无重叠且相邻不重复；
起止无效、缺时区、终点晚于查询时刻报 422 invalid_audit_window；
既有证书/设备历史/撤销响应契约不变。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/coverage_checks.py [base_url]
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
results = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
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


UTC = timezone.utc
TZ8 = timezone(timedelta(hours=8))


def iso(dt):
    return dt.isoformat()


def parse(s):
    return datetime.fromisoformat(s)


def now_utc():
    return datetime.now(UTC)


def ref(no, dev, vf, vt, cal=None):
    return call("POST", "/api/v1/reference-certificates", {
        "cert_no": no, "device_id": dev,
        "valid_from": iso(vf), "valid_to": iso(vt),
        "calibrated_at": iso(cal or vf)})


def cal(no, dev, parent, vf, vt, cal_at, conclusion="PASS"):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": dev, "parent_cert_no": parent,
        "valid_from": iso(vf), "valid_to": iso(vt),
        "calibrated_at": iso(cal_at), "conclusion": conclusion})


def audit(device_id, qfrom, qto):
    qs = urllib.parse.urlencode({"from": iso(qfrom), "to": iso(qto)})
    return call("GET", f"/api/v1/devices/{device_id}/coverage-audit?{qs}")


def audit_raw(device_id, query):
    return call("GET", f"/api/v1/devices/{device_id}/coverage-audit?{query}")


def segs(body):
    return [(parse(s["from"]), parse(s["to"]), s["status"], s["cert_nos"])
            for s in body["segments"]]


def check_integrity(name, body, qfrom, qto):
    """片段序列通用不变式：完整覆盖 [from, to)、无重叠、相邻不重复、
    空窗无编号、编号升序、响应时间与查询为同一时刻。"""
    ok = (
        parse(body["from"]) == qfrom.astimezone(UTC)
        and parse(body["to"]) == qto.astimezone(UTC)
        and body["device_id"] is not None
    )
    ss = body["segments"]
    if not ss:
        ok = False
    else:
        ok = ok and parse(ss[0]["from"]) == qfrom.astimezone(UTC)
        ok = ok and parse(ss[-1]["to"]) == qto.astimezone(UTC)
        for prev, cur in zip(ss, ss[1:]):
            ok = ok and parse(prev["to"]) == parse(cur["from"])
            ok = ok and not (
                prev["status"] == cur["status"]
                and prev["cert_nos"] == cur["cert_nos"]
            )
        for s in ss:
            ok = ok and parse(s["from"]) < parse(s["to"])
            ok = ok and s["cert_nos"] == sorted(s["cert_nos"])
            ok = ok and (s["status"] == "COMPLIANT") == bool(s["cert_nos"])
    check(name, ok, json.dumps(body, ensure_ascii=False)[:300])


# ---------------------------------------------------------------------------
print("== 0. 前置数据：基准证书与各类设备证书 ==")
t_ref_from = datetime(2026, 1, 1, 0, 0, tzinfo=TZ8)
t_ref_to = datetime(2027, 6, 1, 0, 0, tzinfo=TZ8)
s, _ = ref("COV-REF-0", "DEV-COV-MASTER", t_ref_from, t_ref_to)
check("主基准证书 201", s == 201, f"got {s}")
# 已过期基准（有效期终点早于现在）：用于“上游事后到期不取消下游覆盖”
s, _ = ref("COV-REF-EXP", "DEV-COV-MASTER2",
           t_ref_from, datetime(2026, 10, 2, 0, 0, tzinfo=TZ8))
check("将过期基准证书 201", s == 201, f"got {s}")
# 级联链专用基准
s, _ = ref("COV-REF-B", "DEV-COV-BMASTER", t_ref_from, t_ref_to)
check("级联链基准证书 201", s == 201, f"got {s}")

q_from = datetime(2026, 8, 1, 0, 0, tzinfo=TZ8)  # 审计统一起点

# ---------------------------------------------------------------------------
print("== A. 生命周期：签发计入 → 撤销截断 → 重叠重签按事件还原 ==")
s, b = cal("COV-A1", "DEV-COV-A", "COV-REF-0",
           datetime(2026, 9, 1, tzinfo=TZ8), datetime(2027, 9, 1, tzinfo=TZ8),
           datetime(2026, 9, 1, 9, tzinfo=TZ8))
check("A1 签发 201", s == 201, f"got {s}")
a1_created = parse(b["certificate"]["created_at"])

q_to = now_utc()
s, body = audit("DEV-COV-A", q_from, q_to)
check("A1 签发后审计 200", s == 200, f"got {s}")
check_integrity("A1 片段完整无重叠", body, q_from, q_to)
expect = [(q_from.astimezone(UTC), a1_created, "GAP", []),
          (a1_created, q_to.astimezone(UTC), "COMPLIANT", ["COV-A1"])]
got = segs(body)
check("A1 覆盖自签发创建时刻起算（有效期起点更早）",
      len(got) == 2 and got[0][0] == expect[0][0] and got[0][2] == "GAP"
      and got[1][0] == a1_created and got[1][2] == "COMPLIANT"
      and got[1][3] == ["COV-A1"],
      json.dumps(got, default=str)[:300])

s, rb = call("POST", "/api/v1/certificates/COV-A1/revoke",
             {"reason": "期间核查超差", "operator": "qa"})
check("撤销 A1 200", s == 200, f"got {s}")
a1_revoked = parse(rb["revoked_at"])

# 撤销后区间释放：以与原证书重叠的有效期重签
s, b = cal("COV-A2", "DEV-COV-A", "COV-REF-0",
           datetime(2026, 9, 15, tzinfo=TZ8), datetime(2027, 9, 15, tzinfo=TZ8),
           datetime(2026, 9, 15, 9, tzinfo=TZ8))
check("A2 重叠有效期重签 201", s == 201, f"got {s}")
a2_created = parse(b["certificate"]["created_at"])

q_to = now_utc()
s, body = audit("DEV-COV-A", q_from, q_to)
check("重签后审计 200", s == 200, f"got {s}")
check_integrity("重签后片段完整无重叠", body, q_from, q_to)
got = segs(body)
check("历史按事件还原：GAP → A1 → GAP → A2（不以当前状态替代历史）",
      len(got) == 4
      and got[0][2] == "GAP"
      and got[1][0] == a1_created and got[1][1] == a1_revoked
      and got[1][2] == "COMPLIANT" and got[1][3] == ["COV-A1"]
      and got[2][0] == a1_revoked and got[2][1] == a2_created and got[2][2] == "GAP"
      and got[3][0] == a2_created and got[3][1] == q_to.astimezone(UTC)
      and got[3][2] == "COMPLIANT" and got[3][3] == ["COV-A2"],
      json.dumps(got, default=str)[:400])

# ---------------------------------------------------------------------------
print("== B. 级联失效：下游覆盖按级联失效时刻截断 ==")
s, b = cal("COV-B1", "DEV-COV-B1", "COV-REF-B",
           datetime(2026, 9, 1, tzinfo=TZ8), datetime(2027, 9, 1, tzinfo=TZ8),
           datetime(2026, 9, 1, 9, tzinfo=TZ8))
check("B1 签发 201", s == 201, f"got {s}")
s, b = cal("COV-B2", "DEV-COV-B2", "COV-B1",
           datetime(2026, 9, 2, tzinfo=TZ8), datetime(2027, 9, 2, tzinfo=TZ8),
           datetime(2026, 9, 2, 9, tzinfo=TZ8))
check("B2 签发 201", s == 201, f"got {s}")
b2_created = parse(b["certificate"]["created_at"])

s, rb = call("POST", "/api/v1/certificates/COV-REF-B/revoke",
             {"reason": "基准超差", "operator": "qa"})
check("撤销链根 200", s == 200, f"got {s}")
check("B1/B2 级联失效", rb["affected_count"] == 2, json.dumps(rb)[:200])
cascade_at = parse(rb["revoked_at"])

q_to = now_utc()
s, body = audit("DEV-COV-B2", q_from, q_to)
check("下游设备审计 200", s == 200, f"got {s}")
check_integrity("下游片段完整无重叠", body, q_from, q_to)
got = segs(body)
check("下游覆盖在级联失效时刻截断",
      len(got) == 3
      and got[0][2] == "GAP"
      and got[1][0] == b2_created and got[1][1] == cascade_at
      and got[1][2] == "COMPLIANT" and got[1][3] == ["COV-B2"]
      and got[2][0] == cascade_at and got[2][2] == "GAP",
      json.dumps(got, default=str)[:300])

# ---------------------------------------------------------------------------
print("== C. 有效期终点退出（不撤销，自然到期） ==")
vt_c = now_utc() + timedelta(seconds=2)
s, b = cal("COV-C1", "DEV-COV-C", "COV-REF-0",
           datetime(2026, 9, 1, tzinfo=TZ8), vt_c,
           datetime(2026, 9, 1, 9, tzinfo=TZ8))
check("C1 签发 201", s == 201, f"got {s}")
c1_created = parse(b["certificate"]["created_at"])
time.sleep(3)  # 等待有效期终点成为过去

q_to = now_utc()
s, body = audit("DEV-COV-C", q_from, q_to)
check("到期后审计 200", s == 200, f"got {s}")
check_integrity("到期片段完整无重叠", body, q_from, q_to)
got = segs(body)
vt_c_utc = vt_c.astimezone(UTC)
check("覆盖在有效期终点退出",
      len(got) == 3
      and got[0][2] == "GAP"
      and got[1][0] == c1_created and got[1][1] == vt_c_utc
      and got[1][2] == "COMPLIANT" and got[1][3] == ["COV-C1"]
      and got[2][0] == vt_c_utc and got[2][2] == "GAP",
      json.dumps(got, default=str)[:300])

# ---------------------------------------------------------------------------
print("== D. 未来生效证书：计入起点为有效期起点（尚未开始） ==")
vf_d = now_utc() + timedelta(hours=2)
s, _ = cal("COV-D1", "DEV-COV-D", "COV-REF-0",
           vf_d, vf_d + timedelta(hours=2), vf_d + timedelta(minutes=30))
check("D1 未来生效签发 201", s == 201, f"got {s}")
q_to = now_utc()
s, body = audit("DEV-COV-D", q_from, q_to)
check("未来生效审计 200", s == 200, f"got {s}")
got = segs(body)
check("未来生效证书尚不提供覆盖（整段空窗）",
      len(got) == 1 and got[0][2] == "GAP"
      and got[0][0] == q_from.astimezone(UTC) and got[0][1] == q_to.astimezone(UTC),
      json.dumps(got, default=str)[:300])

# ---------------------------------------------------------------------------
print("== E. 签发时有效期已过：计入窗口为空 ==")
s, _ = cal("COV-E1", "DEV-COV-E", "COV-REF-0",
           datetime(2026, 1, 1, tzinfo=TZ8), datetime(2026, 2, 1, tzinfo=TZ8),
           datetime(2026, 1, 15, 9, tzinfo=TZ8))
check("E1 已过期证书签发 201", s == 201, f"got {s}")
q_to = now_utc()
s, body = audit("DEV-COV-E", q_from, q_to)
got = segs(body)
check("签发时已过期 → 整段空窗",
      s == 200 and len(got) == 1 and got[0][2] == "GAP",
      json.dumps(got, default=str)[:300])

# ---------------------------------------------------------------------------
print("== F. 不合格证书不提供覆盖 ==")
s, _ = cal("COV-F1", "DEV-COV-F", "COV-REF-0",
           datetime(2026, 9, 1, tzinfo=TZ8), datetime(2027, 9, 1, tzinfo=TZ8),
           datetime(2026, 9, 1, 9, tzinfo=TZ8), conclusion="FAIL")
check("F1 不合格证书签发 201", s == 201, f"got {s}")
q_to = now_utc()
s, body = audit("DEV-COV-F", q_from, q_to)
got = segs(body)
check("FAIL 证书 → 整段空窗",
      s == 200 and len(got) == 1 and got[0][2] == "GAP",
      json.dumps(got, default=str)[:300])

# ---------------------------------------------------------------------------
print("== G. 仅基准证书的设备：普通证书才计入 ==")
q_to = now_utc()
s, body = audit("DEV-COV-MASTER", q_from, q_to)
got = segs(body)
check("基准证书不计入 → 整段空窗",
      s == 200 and len(got) == 1 and got[0][2] == "GAP",
      json.dumps(got, default=str)[:300])

# ---------------------------------------------------------------------------
print("== H. 未知设备：整段空窗（非 404） ==")
q_to = now_utc()
s, body = audit("DEV-COV-NONE", q_from, q_to)
got = segs(body) if s == 200 else []
check("未知设备 200 且整段空窗",
      s == 200 and len(got) == 1 and got[0][2] == "GAP"
      and got[0][0] == q_from.astimezone(UTC) and got[0][1] == q_to.astimezone(UTC)
      and got[0][3] == [],
      f"got {s} " + json.dumps(body, ensure_ascii=False)[:200])

# ---------------------------------------------------------------------------
print("== I. 上游事后到期不取消下游覆盖 ==")
# COV-REF-EXP 有效期至 2026-10-02（早于现在）；下游校准时刻落在其有效期内
s, b = cal("COV-UP1", "DEV-COV-UP", "COV-REF-EXP",
           datetime(2026, 9, 1, tzinfo=TZ8), datetime(2027, 9, 1, tzinfo=TZ8),
           datetime(2026, 9, 15, 9, tzinfo=TZ8))
check("引用已过期基准签发 201（校准时刻在其有效期内）", s == 201, f"got {s}")
up_created = parse(b["certificate"]["created_at"])
q_to = now_utc()
s, body = audit("DEV-COV-UP", datetime(2026, 10, 3, 0, 0, tzinfo=TZ8), q_to)
got = segs(body)
check("上游到期后下游仍合规（覆盖自签发创建时刻起）",
      s == 200 and len(got) == 2
      and got[0][2] == "GAP"
      and got[1][0] == up_created and got[1][2] == "COMPLIANT"
      and got[1][3] == ["COV-UP1"],
      json.dumps(got, default=str)[:300])

# ---------------------------------------------------------------------------
print("== J. 子区间查询：片段端点等于查询起止 ==")
# 完全落在 A2 合规窗口内的子区间（A2 签发已过去数秒）
in_from = a2_created + timedelta(seconds=1)
in_to = now_utc()
s, body = audit("DEV-COV-A", in_from, in_to)
got = segs(body)
check("子区间完全落在合规窗口内 → 单个合规片段",
      s == 200 and len(got) == 1
      and got[0][0] == in_from.astimezone(UTC) and got[0][1] == in_to.astimezone(UTC)
      and got[0][2] == "COMPLIANT" and got[0][3] == ["COV-A2"],
      json.dumps(got, default=str)[:300])
# 跨边界的子区间：C1 有效期终点已在数秒前成为过去，两端各留余量
s, body = audit("DEV-COV-C", vt_c - timedelta(hours=1), vt_c + timedelta(seconds=1))
got = segs(body)
check("子区间跨多个边界 → 以查询端点裁剪（GAP/COMPLIANT/GAP）",
      s == 200 and len(got) == 3
      and got[0][0] == (vt_c - timedelta(hours=1)).astimezone(UTC)
      and got[0][1] == c1_created and got[0][2] == "GAP"
      and got[1][0] == c1_created and got[1][1] == vt_c_utc
      and got[1][2] == "COMPLIANT" and got[1][3] == ["COV-C1"]
      and got[2][0] == vt_c_utc
      and got[2][1] == (vt_c + timedelta(seconds=1)).astimezone(UTC)
      and got[2][2] == "GAP",
      json.dumps(got, default=str)[:300])

# ---------------------------------------------------------------------------
print("== K. 参数错误：起止无效 / 缺时区 / 终点晚于查询时刻 ==")
q_to = now_utc()
s, body = audit("DEV-COV-A", q_to, q_from)
check("起点晚于终点 → 422 invalid_audit_window",
      s == 422 and err_code(body) == "invalid_audit_window", f"got {s} {err_code(body)}")
s, body = audit("DEV-COV-A", q_from, q_from)
check("起点等于终点 → 422 invalid_audit_window",
      s == 422 and err_code(body) == "invalid_audit_window", f"got {s} {err_code(body)}")
s, body = audit("DEV-COV-A", q_from, now_utc() + timedelta(hours=1))
check("终点晚于查询时刻 → 422 invalid_audit_window",
      s == 422 and err_code(body) == "invalid_audit_window", f"got {s} {err_code(body)}")
s, body = audit_raw("DEV-COV-A", "from=2026-08-01T00:00:00&to=2026-09-01T00:00:00%2B08:00")
check("起点缺时区 → 422 invalid_audit_window",
      s == 422 and err_code(body) == "invalid_audit_window", f"got {s} {err_code(body)}")
s, body = audit_raw("DEV-COV-A", "from=2026-08-01T00:00:00%2B08:00&to=2026-09-01T00:00:00")
check("终点缺时区 → 422 invalid_audit_window",
      s == 422 and err_code(body) == "invalid_audit_window", f"got {s} {err_code(body)}")
s, body = audit_raw("DEV-COV-A", "from=not-a-time&to=2026-09-01T00:00:00%2B08:00")
check("起点无法解析 → 422", s == 422, f"got {s}")
s, body = audit_raw("DEV-COV-A", "from=2026-08-01T00:00:00%2B08:00")
check("缺少终点参数 → 422", s == 422, f"got {s}")

# ---------------------------------------------------------------------------
print("== L. 不同时区表示同一时刻：结果一致 ==")
q_to = now_utc()
s1, b1 = audit("DEV-COV-A", q_from, q_to)
q_from_z = q_from.astimezone(UTC)
q_to_z = q_to.astimezone(UTC)
s2, b2 = audit("DEV-COV-A", q_from_z, q_to_z)
check("+08:00 与 UTC 表示同一区间 → 片段一致",
      s1 == 200 and s2 == 200 and b1["segments"] == b2["segments"],
      json.dumps(b2, ensure_ascii=False)[:200])

# ---------------------------------------------------------------------------
print("== M. 既有响应契约不变 ==")
s, b = call("GET", "/api/v1/certificates/COV-A1")
check("证书详情可查且字段完整",
      s == 200 and b["cert_no"] == "COV-A1" and b["status"] == "REVOKED"
      and "created_at" in b and "revoked_at" in b, f"got {s}")
s, b = call("GET", "/api/v1/devices/DEV-COV-A/certificates")
check("设备历史含全部证书（含已撤销）",
      s == 200 and sorted(c["cert_no"] for c in b) == ["COV-A1", "COV-A2"],
      f"got {s}")
s, b = call("GET", "/api/v1/certificates/COV-B2")
check("级联失效证书状态 INVALID",
      s == 200 and b["status"] == "INVALID", f"got {s} {b.get('status')}")
s, b = call("GET", "/api/v1/devices/DEV-COV-A/coverage-audit?from=2026-08-01T00:00:00%2B08:00"
              "&to=2026-09-01T00:00:00%2B08:00")
check("审计不干扰既有路由（/certificates 仍可查）", s == 200, f"got {s}")

# ---------------------------------------------------------------------------
fails = [r for r in results if not r[1]]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)
