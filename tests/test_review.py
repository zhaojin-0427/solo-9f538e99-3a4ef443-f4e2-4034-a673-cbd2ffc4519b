# -*- coding: utf-8 -*-
"""现场复核批次自测。

重点覆盖题目要求的三种情形：
1. 正常：建批快照 -> ≤2px 提交确认 -> 重复提交只留一次；
2. 坐标偏差：>2px 被拒绝，且复核状态/快照/确认字段完全不变；
3. 对位更新后过期：未提交项过期并拒绝旧坐标，已确认项保留历史，
   过期项可重开并按新总图坐标重新复核。

运行：
    python -m unittest tests.test_review -v
"""

import tempfile
import unittest
from pathlib import Path

from app import create_app
from app.db import connect
from app.seed_data import DEMO_SOLVABLE


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_review.db"
        self.app = create_app(database=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------ helpers
    def create_batch(self, cracks=("LF-03", "LF-04"), via="json"):
        if via == "json":
            r = self.client.post("/api/review/batches",
                                 json={"crack_ids": list(cracks)})
        else:
            r = self.client.post(
                "/review/batches",
                data={"crack_ids": list(cracks)},
            )
        self.assertEqual(r.status_code, 200 if via == "json" else 302,
                         r.get_data(as_text=True)[:400])
        if via == "json":
            return r.get_json()["batch"]
        loc = r.headers["Location"]
        bid = int(loc.split("batch=")[1].split("&")[0].split("#")[0])
        return self.client.get(f"/api/review/batches/{bid}").get_json()

    def submit(self, batch_no, crack, x, y, **extra):
        payload = {"batch_no": batch_no, "crack_code": crack, "x": x, "y": y}
        payload.update(extra)
        return self.client.post("/api/review/submit", json=payload)

    def apply_solvable_alignment(self):
        d = DEMO_SOLVABLE
        return self.client.post("/api/submit", json={
            "image_a": d["image_a"], "image_b": d["image_b"],
            "ax": d["ax"], "ay": d["ay"], "bx": d["bx"], "by": d["by"],
        })

    def db(self):
        return connect(self.db_path)

    # ------------------------------------------------------------ 建批快照
    def test_state_starts_at_alignment_version_1(self):
        st = self.client.get("/api/state").get_json()
        self.assertEqual(st["alignment_version"], 1)
        ov = self.client.get("/api/review/batches").get_json()
        self.assertEqual(ov["alignment_version"], 1)
        self.assertEqual(ov["tolerance"], 2.0)

    def test_create_batch_records_version_and_master_snapshot(self):
        batch = self.create_batch(["LF-03", "LF-01", "LF-03"])  # 重复去重
        self.assertEqual(batch["batch_no"], "PC-0001")
        self.assertEqual(batch["alignment_version"], 1)
        self.assertEqual(batch["n_total"], 2)  # 同裂缝不重复进批

        by_code = {it["crack_code"]: it for it in batch["items"]}
        # LF-03 局部 (160,200) + img2 平移 (830,20) = (990,220)
        self.assertAlmostEqual(by_code["LF-03"]["snap_x"], 990)
        self.assertAlmostEqual(by_code["LF-03"]["snap_y"], 220)
        # LF-01 在基准图 img0 上：(140,110)
        self.assertAlmostEqual(by_code["LF-01"]["snap_x"], 140)
        self.assertAlmostEqual(by_code["LF-01"]["snap_y"], 110)
        self.assertEqual(by_code["LF-03"]["snap_version"], 1)
        self.assertEqual(by_code["LF-03"]["status"], "pending")

        with self.db() as db:
            n = db.execute("SELECT COUNT(*) n FROM review_batches").fetchone()["n"]
            self.assertEqual(n, 1)
            ni = db.execute("SELECT COUNT(*) n FROM review_items").fetchone()["n"]
            self.assertEqual(ni, 2)

    def test_create_batch_requires_cracks(self):
        r = self.client.post("/api/review/batches", json={"crack_ids": []})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["status"], "invalid")
        with self.db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) n FROM review_batches").fetchone()["n"], 0)

    def test_create_batch_unknown_crack_rejected(self):
        r = self.client.post("/api/review/batches",
                             json={"crack_ids": ["LF-999"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("不存在", r.get_json()["error"])

    # ------------------------------------------------------------ 情形一：正常
    def test_submit_within_tolerance_confirms(self):
        batch = self.create_batch(["LF-01", "LF-02"])
        no = batch["batch_no"]

        r = self.submit(no, "LF-01", 140, 110)  # 完全一致
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["status"], "confirmed")
        self.assertEqual(body["accepted"], True)
        self.assertLessEqual(body["deviation"], 2.0)

        # 恰好 2 像素（含边界）也应确认
        r = self.submit(no, "LF-02", 652, 140)  # 快照 (650,140)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["status"], "confirmed")

        # 对角 sqrt(1^2+1^2) < 2
        batch2 = self.create_batch(["LF-03"])
        r = self.submit(batch2["batch_no"], "LF-03", 991, 221)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "confirmed")

        with self.db() as db:
            rows = db.execute(
                "SELECT status, field_x, field_y, deviation, confirmed_at "
                "FROM review_items ORDER BY id"
            ).fetchall()
            self.assertTrue(all(x["status"] == "confirmed" for x in rows))
            self.assertIsNotNone(rows[0]["confirmed_at"])

    def test_repeat_submit_keeps_single_confirmation_and_first_history(self):
        batch = self.create_batch(["LF-01"])
        no = batch["batch_no"]

        r1 = self.submit(no, "LF-01", 141, 110)   # 偏差 1
        self.assertEqual(r1.get_json()["status"], "confirmed")
        r2 = self.submit(no, "LF-01", 142, 110)   # 再提交一个仍在容差内的点
        self.assertEqual(r2.status_code, 200)
        body2 = r2.get_json()
        self.assertEqual(body2["status"], "already_confirmed")
        self.assertTrue(body2["duplicate"])

        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM review_items WHERE batch_id=? AND crack_id='LF-01'",
                (batch["id"],),
            ).fetchall()
            self.assertEqual(len(rows), 1)                 # 只保留一次
            self.assertAlmostEqual(rows[0]["field_x"], 141)  # 首次现场坐标不被覆盖
            self.assertEqual(rows[0]["status"], "confirmed")
            n_conf = db.execute(
                "SELECT COUNT(*) n FROM review_items WHERE status='confirmed'"
            ).fetchone()["n"]
            self.assertEqual(n_conf, 1)

    # ------------------------------------------------------------ 情形二：偏差
    def test_submit_coordinate_deviation_rejected_state_unchanged(self):
        batch = self.create_batch(["LF-01"])
        item_id = batch["items"][0]["id"]

        r = self.submit(batch["batch_no"], "LF-01", 143, 110)  # 偏差 3px
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(body["reason_code"], "too_far")
        self.assertFalse(body["accepted"])
        self.assertIn("2 像素", body["reason"])
        self.assertGreater(body["deviation"], 2.0)

        # 无效提交不得改变复核状态/快照/确认字段
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM review_items WHERE id=?", (item_id,)
            ).fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertIsNone(row["field_x"])
            self.assertIsNone(row["field_y"])
            self.assertIsNone(row["confirmed_at"])
            self.assertAlmostEqual(row["snap_x"], 140)
            self.assertAlmostEqual(row["snap_y"], 110)
            # 但拒绝原因作为审计被记录，供页面列出
            rej = db.execute(
                "SELECT * FROM review_submissions WHERE item_id=?", (item_id,)
            ).fetchall()
            self.assertEqual(len(rej), 1)
            self.assertEqual(rej[0]["accepted"], 0)
            self.assertEqual(rej[0]["reason_code"], "too_far")

        # 随后在容差内的合法提交仍可正常确认
        r = self.submit(batch["batch_no"], "LF-01", 141, 110)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "confirmed")

    def test_malformed_submissions_are_400_and_change_nothing(self):
        batch = self.create_batch(["LF-01"])
        no = batch["batch_no"]
        bad_requests = [
            {},
            {"crack_code": "LF-01", "x": 1, "y": 1},                 # 缺批次号
            {"batch_no": "PC-9999", "crack_code": "LF-01",
             "x": 1, "y": 1},                                        # 批次不存在
            {"batch_no": no, "x": 1, "y": 1},                        # 缺裂缝号
            {"batch_no": no, "crack_code": "LF-02", "x": 1, "y": 1},  # 不在批次内
            {"batch_no": no, "crack_code": "LF-01",
             "x": "abc", "y": 1},                                    # 坐标非数字
            {"batch_no": no, "crack_code": "LF-01", "x": 1},         # 缺 y
        ]
        for payload in bad_requests:
            r = self.client.post("/api/review/submit", json=payload)
            self.assertEqual(r.status_code, 400, payload)
            self.assertEqual(r.get_json()["status"], "invalid")

        with self.db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) n FROM review_items "
                           "WHERE status!='pending'").fetchone()["n"], 0)
            # 400 级别的畸形提交不写拒绝流水
            self.assertEqual(
                db.execute("SELECT COUNT(*) n FROM review_submissions").fetchone()["n"], 0)

    # ------------------------------------------------------------ 情形三：对位更新过期
    def test_alignment_update_expires_pending_and_rejects_old_coords(self):
        batch = self.create_batch(["LF-03", "LF-04"])
        no = batch["batch_no"]

        ar = self.apply_solvable_alignment()
        self.assertEqual(ar.status_code, 200)
        abody = ar.get_json()
        self.assertEqual(abody["status"], "applied")
        self.assertEqual(abody["alignment_version"], 2)
        expired_codes = {e["crack_code"] for e in abody["review_expired"]}
        self.assertEqual(expired_codes, {"LF-03", "LF-04"})

        detail = self.client.get(f"/api/review/batches/{no}").get_json()
        self.assertEqual(detail["alignment_current_version"], 2)
        self.assertTrue(detail["version_stale"])
        self.assertEqual(detail["n_expired"], 2)
        self.assertEqual(detail["n_pending"], 0)

        # 用批次旧快照坐标提交 -> 拒绝（过期），状态保持 expired
        r = self.submit(no, "LF-03", 990, 220)
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(body["reason_code"], "expired")
        self.assertIn("过期", body["reason"])

        with self.db() as db:
            row = db.execute(
                "SELECT status FROM review_items WHERE crack_code='LF-03'"
            ).fetchone()
            self.assertEqual(row["status"], "expired")

    def test_no_solution_alignment_does_not_expire_or_bump_version(self):
        batch = self.create_batch(["LF-03"])
        r = self.client.post("/api/submit", json={
            "image_a": "img0", "image_b": "img1",
            "ax": 470, "ay": 150, "bx": 20, "by": 150,
        })
        self.assertEqual(r.get_json()["status"], "no_solution")

        st = self.client.get("/api/state").get_json()
        self.assertEqual(st["alignment_version"], 1)
        detail = self.client.get(
            f"/api/review/batches/{batch['batch_no']}").get_json()
        self.assertEqual(detail["items"][0]["status"], "pending")
        # 旧快照坐标仍可正常提交
        r = self.submit(batch["batch_no"], "LF-03", 990, 220)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "confirmed")

    def test_confirmed_item_keeps_history_through_alignment_update(self):
        batch = self.create_batch(["LF-03", "LF-04"])
        no = batch["batch_no"]
        # 更新对位前先确认 LF-03
        r = self.submit(no, "LF-03", 990, 220)
        self.assertEqual(r.get_json()["status"], "confirmed")

        self.apply_solvable_alignment()

        detail = self.client.get(f"/api/review/batches/{no}").get_json()
        by_code = {it["crack_code"]: it for it in detail["items"]}
        c3 = by_code["LF-03"]
        c4 = by_code["LF-04"]

        # 已确认项保留：状态、首次现场坐标、旧快照、确认时间都在
        self.assertEqual(c3["status"], "confirmed")
        self.assertAlmostEqual(c3["field_x"], 990)
        self.assertAlmostEqual(c3["field_y"], 220)
        self.assertAlmostEqual(c3["snap_x"], 990)
        self.assertIsNotNone(c3["confirmed_at"])
        # 但当前总图坐标确实已经移动到 (960,200)
        self.assertAlmostEqual(c3["current_x"], 960)
        self.assertAlmostEqual(c3["current_y"], 200)
        # 未提交项过期
        self.assertEqual(c4["status"], "expired")

        with self.db() as db:
            row = db.execute(
                "SELECT * FROM review_items WHERE crack_code='LF-03'"
            ).fetchone()
            self.assertEqual(row["status"], "confirmed")
            self.assertAlmostEqual(row["field_x"], 990)
            self.assertIsNotNone(row["confirmed_at"])

    # ------------------------------------------------------------ 重开过期项
    def test_reopen_expired_refreshes_snapshot_and_review_again(self):
        batch = self.create_batch(["LF-04"])
        no = batch["batch_no"]
        item_id = batch["items"][0]["id"]

        self.apply_solvable_alignment()
        # 旧坐标先被拒绝一次
        self.assertEqual(self.submit(no, "LF-04", 1150, 110).status_code, 400)

        r = self.client.post(f"/api/review/items/{item_id}/reopen")
        self.assertEqual(r.status_code, 200)
        reopened = r.get_json()["batch"]
        it = reopened["items"][0]
        self.assertEqual(it["status"], "pending")
        # 快照刷新为移位后的新总图坐标 (1120,90)，版本 v2，轮次 +1
        self.assertAlmostEqual(it["snap_x"], 1120)
        self.assertAlmostEqual(it["snap_y"], 90)
        self.assertEqual(it["snap_version"], 2)
        self.assertEqual(it["round_no"], 2)

        # 拿更旧的坐标按新快照提交 -> 偏差过大，仍拒绝且保持待复核
        r = self.submit(no, "LF-04", 1150, 110)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason_code"], "too_far")

        # 按新快照 ≤2px 提交 -> 确认
        r = self.submit(no, "LF-04", 1120, 92)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "confirmed")

        # 已确认项不允许重开（历史保护）
        r = self.client.post(f"/api/review/items/{item_id}/reopen")
        self.assertEqual(r.status_code, 400)
        self.assertIn("历史", r.get_json()["error"])

    def test_reopen_by_batch_and_code(self):
        batch = self.create_batch(["LF-04"])
        self.apply_solvable_alignment()
        r = self.client.post("/api/review/reopen",
                             json={"batch_no": batch["batch_no"],
                                   "crack_code": "LF-04"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.get_json()["batch"]["items"][0]["status"], "pending")

    # ------------------------------------------------------------ 页面 / 表单
    def test_page_lists_all_groups_and_reject_reason(self):
        batch = self.create_batch(["LF-03", "LF-04"])
        # 制造一条偏差拒绝
        self.submit(batch["batch_no"], "LF-03", 1000, 220)

        html = self.client.get(f"/?batch={batch['id']}").get_data(as_text=True)
        for token in [
            "现场裂缝复核批次", "开始复核批次", batch["batch_no"],
            "待复核", "已确认", "已过期", "拒绝原因",
            "重开", "超过 2 像素", "PC-0001", "LF-03", "LF-04",
            'name="batch_no"', 'name="crack_code"', 'name="x"', 'name="y"',
        ]:
            self.assertIn(token, html)

    def test_page_expired_row_has_working_reopen_form(self):
        batch = self.create_batch(["LF-04"], via="form")
        item_id = batch["items"][0]["id"]
        self.apply_solvable_alignment()

        html = self.client.get(f"/?batch={batch['id']}").get_data(as_text=True)
        self.assertIn("已过期（1）", html)
        self.assertIn(f"/review/items/{item_id}/reopen", html)

        r = self.client.post(f"/review/items/{item_id}/reopen")
        self.assertEqual(r.status_code, 302)
        self.assertIn("review_reopened=1", r.headers["Location"])
        html = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("待复核（1）", html)
        self.assertIn("已重开过期项", html)

    def test_form_create_and_submit_prg(self):
        # 表单多选建批
        r = self.client.post("/review/batches",
                             data={"crack_ids": ["LF-01", "LF-02"],
                                   "note": "上午西壁"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/?batch=1", r.headers["Location"])

        # 表单提交复核
        r = self.client.post("/review/submit", data={
            "batch_no": "PC-0001", "crack_code": "LF-01", "x": "140", "y": "110",
        })
        self.assertEqual(r.status_code, 302)
        self.assertIn("review_done=1", r.headers["Location"])
        follow = self.client.get(r.headers["Location"])
        self.assertIn("已确认（1）", follow.get_data(as_text=True))

    def test_form_create_without_crack_renders_400(self):
        r = self.client.post("/review/batches", data={"note": "空批次"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("至少勾选一条裂缝", r.get_data(as_text=True))

    def test_form_rejected_coordinate_renders_400_with_reason_and_keeps_state(self):
        batch = self.create_batch(["LF-01"])
        r = self.client.post("/review/submit", data={
            "batch_no": batch["batch_no"], "crack_code": "LF-01",
            "x": "200", "y": "110",  # 偏差 60px
        })
        self.assertEqual(r.status_code, 400)
        html = r.get_data(as_text=True)
        self.assertIn("超过 2 像素", html)
        self.assertIn("拒绝原因", html)
        with self.db() as db:
            row = db.execute(
                "SELECT status FROM review_items WHERE crack_code='LF-01'"
            ).fetchone()
            self.assertEqual(row["status"], "pending")

    def test_form_expired_submission_renders_400(self):
        batch = self.create_batch(["LF-04"])
        self.apply_solvable_alignment()
        r = self.client.post("/review/submit", data={
            "batch_no": batch["batch_no"], "crack_code": "LF-04",
            "x": "1150", "y": "110",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("已过期", r.get_data(as_text=True))

    # ------------------------------------------------------------ 重置
    def test_reset_clears_review_and_version(self):
        batch = self.create_batch(["LF-03"])
        self.submit(batch["batch_no"], "LF-03", 990, 220)
        self.apply_solvable_alignment()
        self.assertEqual(
            self.client.get("/api/state").get_json()["alignment_version"], 2)

        r = self.client.post("/api/reset")
        self.assertEqual(r.status_code, 200)
        ov = self.client.get("/api/review/batches").get_json()
        self.assertEqual(ov["batches"], [])
        self.assertEqual(ov["alignment_version"], 1)

        with self.db() as db:
            for t in ("review_batches", "review_items", "review_submissions"):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"],
                    0, t)
            self.assertEqual(
                db.execute("SELECT value FROM app_meta "
                           "WHERE key='alignment_version'").fetchone()["value"],
                "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
