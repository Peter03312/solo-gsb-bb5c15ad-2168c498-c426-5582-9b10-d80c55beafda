"""并入事件来源溯源 HTTP API 端到端测试：python tests/test_provenance_api.py

覆盖：
- 确认并入后 GET 复盘详情：复制事件带来源摘要（来源复盘/版本/事件身份），
  目标原有事件与非并入事件 source 为 null；
- 版本回看（GET versions/{v}）的不可变快照带来源摘要；
- 再次保存（PUT，客户端不回传 source）不丢失来源；伪造/篡改 source 被忽略
  （来源摘要由服务端按持续身份继承，客户端无法注入或改写）；
- 恢复（restore）后来源摘要与新草稿版本一起保留；
- 版本对照（compare）新增/未变/变化条目均可见来源；
- 来源复盘被删除后仍展示已保存的来源摘要，接口不回读来源历史；
- 推算结果与 digest 不因来源展示而改变。
"""
import json
import os
import pathlib
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DB_PATH"] = _tmp.name
os.environ["PORT"] = "0"

import db  # noqa: E402
import server  # noqa: E402

db.init_db(server.DB_PATH)
HTTPD = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
PORT = HTTPD.server_address[1]
threading.Thread(target=HTTPD.serve_forever, daemon=True).start()


def req(method, path, body=None):
    url = "http://127.0.0.1:{}{}".format(PORT, path)
    data = None if body is None else json.dumps(body).encode("utf-8")
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def ev(key, name, es, ls, dur, uid=None, note=""):
    d = {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
         "duration_minutes": dur, "note": note}
    if uid:
        d["uid"] = uid
    return d


def rel(a, b, min_g=None, max_g=None):
    r = {"predecessor_key": a, "successor_key": b}
    if min_g is not None:
        r["min_gap_minutes"] = min_g
    if max_g is not None:
        r["max_gap_minutes"] = max_g
    return r


def make_source():
    _, b = req("POST", "/api/reviews", {"title": "来源复盘"})
    sid = b["review"]["id"]
    status, b = req("PUT", "/api/reviews/{}".format(sid), {
        "base_version": 1, "title": "来源复盘",
        "events": [ev("a", "来源告警", "2026-10-01T09:00", "2026-10-01T09:00", 15,
                      note="源备注"),
                   ev("b", "来源响应", "2026-10-01T09:00", "2026-10-01T12:00", 30)],
        "relations": [rel("a", "b", min_g=10, max_g=60)]})
    assert status == 200
    return sid, {e["name"]: e["uid"] for e in b["events"]}


def make_target():
    _, b = req("POST", "/api/reviews", {"title": "目标复盘"})
    tid = b["review"]["id"]
    status, b = req("PUT", "/api/reviews/{}".format(tid), {
        "base_version": 1, "title": "目标复盘",
        "events": [ev("x", "目标已有", "2026-10-01T07:00", "2026-10-01T08:00", 20)],
        "relations": []})
    assert status == 200
    return tid, b


def do_merge(tid, sid, sver, uids):
    status, pv = req("POST", "/api/reviews/{}/merge/preview".format(tid),
                     {"source_review_id": sid, "source_version": sver,
                      "event_uids": uids})
    assert status == 200, pv
    _, cur = req("GET", "/api/reviews/{}".format(tid))
    status, b = req("POST", "/api/reviews/{}/merge/confirm".format(tid),
                    {"source_review_id": sid, "source_version": sver,
                     "event_uids": uids, "base_version": cur["review"]["version"],
                     "digest": pv["digest"]})
    assert status == 200, b
    return pv, b


def by_name(bundle):
    return {e["name"]: e for e in bundle["events"]}


def test_detail_version_and_compare_show_source():
    sid, suids = make_source()
    tid, _ = make_target()
    chosen = [suids["来源告警"], suids["来源响应"]]
    _, b = do_merge(tid, sid, 2, chosen)

    # 当前事件详情：复制事件带来源摘要，目标原有事件为 null
    events = by_name(b)
    a = events["来源告警"]
    assert a["source"] == {"review_id": sid, "review_title": "来源复盘",
                           "version": 2, "event_uid": suids["来源告警"],
                           "event_name": "来源告警"}
    assert a["uid"] != suids["来源告警"]          # 新持续身份与来源关联
    assert events["来源响应"]["source"]["event_uid"] == suids["来源响应"]
    assert events["目标已有"]["source"] is None   # 非并入事件不显示来源
    assert all("source_json" not in e for e in b["events"])

    # 版本回看：不可变快照带来源摘要
    status, detail = req("GET", "/api/reviews/{}/versions/3".format(tid))
    assert status == 200
    snap = {e["name"]: e for e in detail["input"]["events"]}
    assert snap["来源告警"]["source"] == a["source"]
    assert snap["来源响应"]["source"]["version"] == 2
    assert snap["目标已有"]["source"] is None

    # 版本对照：v2 → v3 新增条目带来源，未变条目带 source 键
    status, cmp = req("GET", "/api/reviews/{}/compare?from=2&to=3".format(tid))
    assert status == 200
    added = {e["name"]: e for e in cmp["events"]["added"]}
    assert added["来源告警"]["source"]["event_uid"] == suids["来源告警"]
    assert added["来源响应"]["source"]["review_id"] == sid
    unchanged = {e["name"]: e for e in cmp["events"]["unchanged"]}
    assert unchanged["目标已有"]["source"] is None


def test_resave_without_source_keeps_provenance_and_forgery_ignored():
    sid, suids = make_source()
    tid, _ = make_target()
    chosen = [suids["来源告警"]]
    _, b = do_merge(tid, sid, 2, chosen)
    live = by_name(b)
    merged_uid = live["来源告警"]["uid"]
    original_source = live["来源告警"]["source"]

    # 再次保存：客户端不回传 source 字段 ⇒ 服务端按 uid 继承，不丢失
    status, b = req("PUT", "/api/reviews/{}".format(tid), {
        "base_version": 3, "title": "目标复盘",
        "events": [ev("k1", "来源告警（改）", "2026-10-01T09:05", "2026-10-01T09:05",
                      15, uid=merged_uid, note="源备注"),
                   ev("k2", "目标已有", "2026-10-01T07:00", "2026-10-01T08:00", 20,
                      uid=live["目标已有"]["uid"])],
        "relations": []})
    assert status == 200, b
    after = by_name(b)
    assert after["来源告警（改）"]["uid"] == merged_uid
    assert after["来源告警（改）"]["source"] == original_source
    assert after["目标已有"]["source"] is None

    # 客户端伪造/篡改 source：一律被忽略（服务端权威）
    forged = dict(original_source, review_title="伪造标题", version=99)
    e1 = ev("k1", "来源告警（改）", "2026-10-01T09:05", "2026-10-01T09:05", 15,
            uid=merged_uid, note="源备注")
    e1["source"] = forged                       # 篡改已有来源 ⇒ 忽略
    e2 = ev("k2", "目标已有", "2026-10-01T07:00", "2026-10-01T08:00", 20,
            uid=live["目标已有"]["uid"])
    e2["source"] = forged                       # 给非并入事件注入来源 ⇒ 忽略
    status, b = req("PUT", "/api/reviews/{}".format(tid), {
        "base_version": 4, "title": "目标复盘", "events": [e1, e2], "relations": []})
    assert status == 200, b
    after = by_name(b)
    assert after["来源告警（改）"]["source"] == original_source
    assert after["目标已有"]["source"] is None


def test_restore_over_http_keeps_provenance():
    sid, suids = make_source()
    tid, _ = make_target()
    _, b = do_merge(tid, sid, 2, [suids["来源告警"]])
    merged_uid = by_name(b)["来源告警"]["uid"]

    # v4：普通保存（无并入事件）；再恢复 v3 ⇒ 来源随身份一起回来
    status, b = req("PUT", "/api/reviews/{}".format(tid), {
        "base_version": 3, "title": "目标复盘",
        "events": [ev("x", "目标已有", "2026-10-01T07:00", "2026-10-01T08:00", 20)],
        "relations": []})
    assert status == 200
    status, b = req("POST", "/api/reviews/{}/versions/3/restore".format(tid),
                    {"base_version": 4})
    assert status == 200, b
    assert b["review"]["version"] == 5
    restored = by_name(b)["来源告警"]
    assert restored["uid"] == merged_uid
    assert restored["source"]["event_uid"] == suids["来源告警"]
    assert restored["source"]["review_id"] == sid
    # 恢复产生的新版本回看同样可见
    status, detail = req("GET", "/api/reviews/{}/versions/5".format(tid))
    snap = {e["name"]: e for e in detail["input"]["events"]}
    assert snap["来源告警"]["source"]["event_uid"] == suids["来源告警"]


def test_source_summary_survives_source_deletion_over_http():
    sid, suids = make_source()
    tid, _ = make_target()
    _, b = do_merge(tid, sid, 2, [suids["来源告警"]])
    before = by_name(b)["来源告警"]["source"]

    # 删除来源复盘后：详情/回看/对照仍展示已保存摘要，不回读来源
    status, _ = req("DELETE", "/api/reviews/{}".format(sid))
    assert status == 200
    _, b = req("GET", "/api/reviews/{}".format(tid))
    assert by_name(b)["来源告警"]["source"] == before
    status, detail = req("GET", "/api/reviews/{}/versions/3".format(tid))
    assert status == 200
    snap = {e["name"]: e for e in detail["input"]["events"]}
    assert snap["来源告警"]["source"] == before
    status, cmp = req("GET", "/api/reviews/{}/compare?from=2&to=3".format(tid))
    assert status == 200
    added = {e["name"]: e for e in cmp["events"]["added"]}
    assert added["来源告警"]["source"] == before


def test_computation_and_digest_unaffected_by_provenance():
    sid, suids = make_source()
    tid, _ = make_target()
    chosen = [suids["来源告警"], suids["来源响应"]]
    # 相同输入两次预览 digest 一致（来源摘要不参与核对值）
    status, pv1 = req("POST", "/api/reviews/{}/merge/preview".format(tid),
                      {"source_review_id": sid, "source_version": 2,
                       "event_uids": chosen})
    status, pv2 = req("POST", "/api/reviews/{}/merge/preview".format(tid),
                      {"source_review_id": sid, "source_version": 2,
                       "event_uids": chosen})
    assert pv1["digest"] == pv2["digest"]
    _, b = do_merge(tid, sid, 2, chosen)
    # 推算窗口与来源展示无关：来源响应最早 = 9:00 + 15 + 10
    events = by_name(b)
    win = b["computation"]["result"]["windows"][str(events["来源响应"]["id"])]
    assert win["earliest_start"] == "2026-10-01T09:25"
    assert b["computation"]["result"]["status"] == "ok"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", t.__name__, "->", e)
    HTTPD.shutdown()
    sys.exit(1 if failed else 0)
