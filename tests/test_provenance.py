"""并入事件来源溯源（provenance）持久化测试：python tests/test_provenance.py

覆盖：
- 确认并入后，每个复制事件在目标带上来源摘要（来源复盘 id/标题、来源版本、
  来源事件 uid/名称），并取得全新持续身份；目标原有事件与非并入事件无来源；
- 当前详情（get_bundle）、版本回看（get_version 快照）均可见来源摘要；
- 再次保存（改名/改时间，回传 uid）与恢复均不丢失来源摘要；
  新持续身份（uid）始终与来源摘要关联；
- 来源复盘被删除后仍展示已保存的来源摘要，不回读、不改写来源历史；
- 版本对照中新增/未变/变化条目均带来源摘要，且来源摘要不参与差值判定；
- 来源摘要只是展示元数据：推算结果与 digest 不因来源展示而改变；
- 旧库迁移补 source_json 列，旧行/旧快照无来源（非并入事件语义）。
"""
import json
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import db  # noqa: E402


def ev(key, name, es, ls, dur, tz=None, note=""):
    return {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
            "duration_minutes": dur, "note": note, "timezone": tz}


def rel(a, b, min_g=0, max_g=None):
    return {"predecessor_key": a, "successor_key": b,
            "min_gap_minutes": min_g, "max_gap_minutes": max_g}


def setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.init_db(tmp.name)
    return tmp.name


def build_source(p, title="来源复盘"):
    """来源复盘 v2：A→B（10/60）、A→C 无间隔。返回 (rid, {name: uid})。"""
    rid = db.create_review(p, title)
    events = [ev("a", "来源告警", "2026-10-01T09:00", "2026-10-01T09:00", 15, note="源备注"),
              ev("b", "来源响应", "2026-10-01T09:00", "2026-10-01T12:00", 30),
              ev("c", "来源未选", "2026-10-01T08:00", "2026-10-01T08:30", 10)]
    db.update_review(p, rid, 1, title, events,
                     [rel("a", "b", min_g=10, max_g=60), rel("a", "c")])
    bundle = db.get_bundle(p, rid)
    return rid, {e["name"]: e["uid"] for e in bundle["events"]}


def build_target(p, title="目标复盘"):
    rid = db.create_review(p, title)
    db.update_review(p, rid, 1, title,
                     [ev("x", "目标已有", "2026-10-01T07:00", "2026-10-01T08:00", 20)], [])
    return rid


def do_merge(p, tid, sid, suids, names=("来源告警", "来源响应")):
    chosen = [suids[n] for n in names]
    preview = db.preview_merge(p, tid, sid, 2, chosen)
    base = db.current_version(p, tid)
    db.merge_into_review(p, tid, sid, 2, chosen, base, preview["digest"])
    return preview


def by_name(bundle):
    return {e["name"]: e for e in bundle["events"]}


def test_merge_attaches_source_summary_to_copied_events_only():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    do_merge(p, tid, sid, suids)

    bundle = db.get_bundle(p, tid)
    events = by_name(bundle)
    # 复制事件：来源摘要完整，且与目标新身份关联（不沿用来源 uid）
    a = events["来源告警"]
    assert a["uid"] != suids["来源告警"]
    assert a["source"] == {
        "review_id": sid, "review_title": "来源复盘", "version": 2,
        "event_uid": suids["来源告警"], "event_name": "来源告警"}
    b = events["来源响应"]
    assert b["source"]["event_uid"] == suids["来源响应"]
    assert b["source"]["review_id"] == sid and b["source"]["version"] == 2
    # 目标原有事件：非并入事件，无来源信息
    assert events["目标已有"]["source"] is None
    # 不泄露内部存储列
    assert all("source_json" not in e for e in bundle["events"])

    # 版本回看（不可变快照）同样带来源摘要
    v3 = db.get_version(p, tid, 3)
    snap = {e["name"]: e for e in v3["input"]["events"]}
    assert snap["来源告警"]["source"] == a["source"]
    assert snap["来源响应"]["source"]["event_uid"] == suids["来源响应"]
    assert snap["目标已有"]["source"] is None


def test_resave_and_rename_keep_source_with_same_identity():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    do_merge(p, tid, sid, suids)
    live = by_name(db.get_bundle(p, tid))
    merged_uid = live["来源告警"]["uid"]

    # 再次保存：改名 + 改时间，回传 uid（客户端不回传 source）
    events = [
        ev("k1", "来源告警（改名）", "2026-10-01T09:05", "2026-10-01T09:05", 15),
        ev("k2", "来源响应", "2026-10-01T09:00", "2026-10-01T12:00", 30),
        ev("k3", "目标已有", "2026-10-01T07:00", "2026-10-01T08:00", 20),
        ev("k4", "全新事件", "2026-10-01T10:00", "2026-10-01T11:00", 5),
    ]
    for e, uid in zip(events, [merged_uid, live["来源响应"]["uid"],
                               live["目标已有"]["uid"], ""]):
        e["uid"] = uid
    db.update_review(p, tid, 3, "目标复盘", events, [])  # v4

    after = by_name(db.get_bundle(p, tid))
    renamed = after["来源告警（改名）"]
    assert renamed["uid"] == merged_uid            # 身份延续
    assert renamed["source"]["event_uid"] == suids["来源告警"]  # 来源不丢失
    assert renamed["source"]["review_id"] == sid
    assert after["来源响应"]["source"]["event_uid"] == suids["来源响应"]
    assert after["目标已有"]["source"] is None
    assert after["全新事件"]["source"] is None     # 新身份事件无来源
    # 新版本的快照同样保留来源
    v4 = {e["name"]: e for e in db.get_version(p, tid, 4)["input"]["events"]}
    assert v4["来源告警（改名）"]["source"]["event_uid"] == suids["来源告警"]
    assert v4["全新事件"]["source"] is None


def test_restore_preserves_source_and_identity():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    do_merge(p, tid, sid, suids)                       # v3 = merge
    merged = by_name(db.get_bundle(p, tid))
    uid_a = merged["来源告警"]["uid"]

    # 中间再保存一版（删掉并入事件），然后恢复 merge 版本
    db.update_review(p, tid, 3, "目标复盘",
                     [ev("x", "目标已有", "2026-10-01T07:00", "2026-10-01T08:00", 20)],
                     [])                                # v4 无并入事件
    assert all(e["source"] is None for e in db.get_bundle(p, tid)["events"])

    db.restore_review(p, tid, 4, 3)                    # v5 恢复 v3
    bundle = db.get_bundle(p, tid)
    restored = by_name(bundle)
    assert bundle["review"]["version"] == 5
    # 恢复沿用快照身份，且来源摘要随身份回来
    assert restored["来源告警"]["uid"] == uid_a
    assert restored["来源告警"]["source"] == {
        "review_id": sid, "review_title": "来源复盘", "version": 2,
        "event_uid": suids["来源告警"], "event_name": "来源告警"}
    assert restored["来源响应"]["source"]["event_uid"] == suids["来源响应"]
    assert restored["目标已有"]["source"] is None
    # 恢复产生的新版本快照也可回看来源
    v5 = {e["name"]: e for e in db.get_version(p, tid, 5)["input"]["events"]}
    assert v5["来源告警"]["source"]["event_uid"] == suids["来源告警"]


def test_source_summary_survives_source_review_deletion():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    do_merge(p, tid, sid, suids)
    before = by_name(db.get_bundle(p, tid))["来源告警"]["source"]

    # 来源复盘先改名（证明展示不回读来源当前数据），再整体删除
    db.update_review(p, sid, 2, "来源复盘-已改名",
                     [ev("a", "来源告警", "2026-10-01T09:00", "2026-10-01T09:00", 15)],
                     [])
    db.delete_review(p, sid)

    # 当前详情、版本回看、版本对照仍展示已保存的来源摘要（并入时的标题）
    after = by_name(db.get_bundle(p, tid))
    assert after["来源告警"]["source"] == before
    assert after["来源告警"]["source"]["review_title"] == "来源复盘"
    v3 = {e["name"]: e for e in db.get_version(p, tid, 3)["input"]["events"]}
    assert v3["来源告警"]["source"] == before
    cmp = db.compare_versions(p, tid, 2, 3)
    added = {e["name"]: e for e in cmp["events"]["added"]}
    assert added["来源告警"]["source"] == before
    # 来源历史不被改写：删除后目标侧摘要保持原样（上面已断言相等）


def test_compare_exposes_source_without_affecting_diffs():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    do_merge(p, tid, sid, suids)                       # v3 = merge

    # v2 → v3：并入事件出现在 added，带来源；目标事件 unchanged 也带 source 键
    cmp = db.compare_versions(p, tid, 2, 3)
    added = {e["name"]: e for e in cmp["events"]["added"]}
    assert added["来源告警"]["source"]["event_uid"] == suids["来源告警"]
    assert added["来源响应"]["source"]["review_id"] == sid
    unchanged = {e["name"]: e for e in cmp["events"]["unchanged"]}
    assert unchanged["目标已有"]["source"] is None

    # 再保存：只改目标原有事件的时间（回传全部 uid）→ v4。
    # v3 → v4 对照：未变化的并入事件在 unchanged 中带来源；来源本身不产生差值。
    live = by_name(db.get_bundle(p, tid))
    events = [ev("k1", "来源告警", "2026-10-01T09:00", "2026-10-01T09:00", 15, note="源备注"),
              ev("k2", "来源响应", "2026-10-01T09:00", "2026-10-01T12:00", 30),
              ev("k3", "目标已有", "2026-10-01T06:00", "2026-10-01T06:30", 20)]
    for e, uid in zip(events, [live["来源告警"]["uid"], live["来源响应"]["uid"],
                               live["目标已有"]["uid"]]):
        e["uid"] = uid
    db.update_review(p, tid, 3, "目标复盘", events,
                     [rel("k1", "k2", min_g=10, max_g=60)])  # v4
    cmp2 = db.compare_versions(p, tid, 3, 4)
    un = {e["name"]: e for e in cmp2["events"]["unchanged"]}
    assert un["来源告警"]["source"]["event_uid"] == suids["来源告警"]
    assert un["来源响应"]["source"]["event_uid"] == suids["来源响应"]
    changed2 = {c["uid"]: c for c in cmp2["events"]["changed"]}
    assert changed2[live["目标已有"]["uid"]]["source"] is None
    assert set(changed2[live["目标已有"]["uid"]]["input_changes"]) == {
        "earliest_start", "latest_start"}

    # 改名并入事件 → v5；v4 → v5 对照：changed 条目带来源，差值不含来源本身
    events[0] = ev("k1", "来源告警（改名）", "2026-10-01T09:00", "2026-10-01T09:00",
                   15, note="源备注")
    events[0]["uid"] = live["来源告警"]["uid"]
    db.update_review(p, tid, 4, "目标复盘", events,
                     [rel("k1", "k2", min_g=10, max_g=60)])  # v5
    cmp3 = db.compare_versions(p, tid, 4, 5)
    changed3 = {c["uid"]: c for c in cmp3["events"]["changed"]}
    entry = changed3[live["来源告警"]["uid"]]
    assert entry["source"]["event_uid"] == suids["来源告警"]
    assert entry["input_changes"] == {}      # 仅改名；来源摘要不计入差值


def test_provenance_does_not_change_computation_or_digest():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    chosen = [suids["来源告警"], suids["来源响应"]]
    preview = db.preview_merge(p, tid, sid, 2, chosen)
    # digest 对相同输入稳定（来源摘要不参与 digest）
    preview2 = db.preview_merge(p, tid, sid, 2, chosen)
    assert preview["digest"] == preview2["digest"]
    db.merge_into_review(p, tid, sid, 2, chosen, 2, preview["digest"])

    bundle = db.get_bundle(p, tid)
    res = bundle["computation"]["result"]
    events = by_name(bundle)
    # 推算窗口与来源展示无关：来源响应最早 = 9:00 + 15 + 10
    win_b = res["windows"][str(events["来源响应"]["id"])]
    assert win_b["earliest_start"] == "2026-10-01T09:25"
    assert win_b["latest_start"] == "2026-10-01T10:15"
    assert res["status"] == "ok"
    # 预览与确认结论一致
    assert preview["result"]["windows"]["s_" + suids["来源响应"]][
        "earliest_start"] == win_b["earliest_start"]

    # 定稿沿用 live 行：定稿版本快照同样保留来源摘要
    db.finalize_review(p, tid, 3)
    v4 = {e["name"]: e for e in db.get_version(p, tid, 4)["input"]["events"]}
    assert v4["来源告警"]["source"]["event_uid"] == suids["来源告警"]
    assert db.get_bundle(p, tid)["review"]["status"] == "finalized"


def test_merge_of_merged_event_records_new_source():
    """并入产生的事件再次被并入第三份复盘：来源摘要指向最近一次来源。"""
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    do_merge(p, tid, sid, suids, names=("来源告警",))   # tid v3
    merged_uid = by_name(db.get_bundle(p, tid))["来源告警"]["uid"]

    # 第三份复盘从 tid 的 v3 并入该事件
    rid3 = db.create_review(p, "第三复盘")
    pv = db.preview_merge(p, rid3, tid, 3, [merged_uid])
    db.merge_into_review(p, rid3, tid, 3, [merged_uid], 1, pv["digest"])
    e = db.get_bundle(p, rid3)["events"][0]
    assert e["source"]["review_id"] == tid
    assert e["source"]["version"] == 3
    assert e["source"]["event_uid"] == merged_uid
    assert e["source"]["event_name"] == "来源告警"
    assert e["uid"] != merged_uid                      # 又一次全新身份


def test_sequential_merges_keep_each_events_own_source():
    """同一目标先后并入两份来源：各复制事件保留各自来源，再次保存不串不丢。"""
    p = setup_db()
    s1, u1 = build_source(p, "来源一")
    s2 = db.create_review(p, "来源二")
    db.update_review(p, s2, 1, "来源二",
                     [ev("b", "告警二", "2026-10-01T10:00", "2026-10-01T10:00", 20)], [])
    u2 = db.get_bundle(p, s2)["events"][0]["uid"]
    tid = build_target(p)

    pv = db.preview_merge(p, tid, s1, 2, [u1["来源告警"]])
    db.merge_into_review(p, tid, s1, 2, [u1["来源告警"]], 2, pv["digest"])   # v3
    pv = db.preview_merge(p, tid, s2, 2, [u2])
    db.merge_into_review(p, tid, s2, 2, [u2], 3, pv["digest"])               # v4

    events = by_name(db.get_bundle(p, tid))
    assert events["来源告警"]["source"]["review_id"] == s1
    assert events["来源告警"]["source"]["event_uid"] == u1["来源告警"]
    assert events["告警二"]["source"]["review_id"] == s2
    assert events["告警二"]["source"]["event_uid"] == u2
    assert events["目标已有"]["source"] is None

    # 再次保存（回传 uid）：两份来源摘要各自继承，互不串扰
    live = db.get_bundle(p, tid)["events"]
    resave = []
    for e in live:
        resave.append(ev("id_{}".format(e["id"]), e["name"], e["earliest_start"],
                         e["latest_start"], e["duration_minutes"]))
        resave[-1]["uid"] = e["uid"]
    db.update_review(p, tid, 4, "目标复盘", resave, [])                      # v5
    events = by_name(db.get_bundle(p, tid))
    assert events["来源告警"]["source"]["review_id"] == s1
    assert events["告警二"]["source"]["review_id"] == s2
    assert events["目标已有"]["source"] is None


def test_legacy_db_migration_adds_source_column():
    """旧库（events 无 source_json 列）迁移后：旧行无来源，新流程正常。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript(
        "CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,"
        " created_at TEXT, updated_at TEXT);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " name TEXT, earliest_start TEXT, latest_start TEXT, duration_minutes INTEGER,"
        " note TEXT DEFAULT '');"
        "CREATE TABLE relations (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " predecessor_id INTEGER, successor_id INTEGER);"
        "CREATE TABLE computations (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " review_version INTEGER, status TEXT, result_json TEXT, computed_at TEXT);"
        "INSERT INTO reviews VALUES (1,'旧','draft',1,'t','t');"
        "INSERT INTO events VALUES (1,1,'A','2026-10-01T10:00','2026-10-01T11:00',10,'');")
    conn.commit()
    conn.close()

    db.init_db(tmp.name)  # 迁移：补 uid / timezone / source_json 列
    bundle = db.get_bundle(tmp.name, 1)
    assert bundle["events"][0]["source"] is None       # 旧行 = 非并入事件
    # 旧库上正常走并入流程
    sid, suids = build_source(tmp.name)
    pv = db.preview_merge(tmp.name, 1, sid, 2, [suids["来源告警"]])
    db.merge_into_review(tmp.name, 1, sid, 2, [suids["来源告警"]], 1, pv["digest"])
    events = by_name(db.get_bundle(tmp.name, 1))
    assert events["来源告警"]["source"]["event_uid"] == suids["来源告警"]
    assert events["A"]["source"] is None


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
    sys.exit(1 if failed else 0)
