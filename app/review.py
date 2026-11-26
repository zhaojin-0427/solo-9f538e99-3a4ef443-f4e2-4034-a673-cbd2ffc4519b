# -*- coding: utf-8 -*-
"""现场复核批次业务。

闭环规则（与需求一一对应）：

1. 从当前总图选择若干裂缝 :func:`create_batch`，记录开始时的对位版本
   与每条裂缝的总图坐标快照（``review_items.snap_x/snap_y``）。
2. 现场逐条提交 :func:`submit_review`，必须能解析出“批次号 + 裂缝号 +
   现场 x/y 坐标”；现场坐标与该条目的批次快照欧氏距离
   ``<= REVIEW_TOL``（2 像素）才确认。
3. 一个批次中同一条裂缝只有一行（``UNIQUE(batch_id, crack_id)``）。
   首次合法提交写入唯一确认；之后重复提交只返回同一条确认
   （``already_confirmed``），不再产生第二条记录，也不覆盖历史坐标。
4. 对位更新（有解应用）时由 :func:`on_alignment_applied` 在同一事务内
   把所有 ``pending`` 且快照版本落后的条目置为 ``expired``；已确认条目
   完全不动，保留现场坐标/偏差/时间等历史。
5. 向过期条目提交旧坐标会被拒绝（``reason_code='expired'``）；
   偏差超 2px 被拒绝（``too_far``）。拒绝只向 append-only 的
   ``review_submissions`` 追加一条审计原因，**不改变条目的复核状态、
   快照与确认字段**。
6. :func:`reopen_item` 重开过期项：按当前总图重新取快照并把快照版本
   更新为当前版本，条目回到 ``pending``，轮次 +1。

“对位版本”是 ``app_meta.alignment_version`` 中的单调整数：种子为 v1，
每次有解应用对位更新 +1（无解不改变任何几何，不升版本）。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .db import get_meta, set_meta
from .services import ValidationError

# 现场坐标与批次快照的最大允许偏差（总图/样片像素，欧氏距离）。
REVIEW_TOL = 2.0

# 说明：所有“无效/被拒绝”的提交在 HTTP 层统一返回 400（与全站
# ValidationError 约定一致），机器可进一步看 body.status 与
# body.reason_code（too_far / expired）。成功/幂等重复为 200。


# ---------------------------------------------------------------- version

def get_alignment_version(db) -> int:
    raw = get_meta(db, "alignment_version", "1")
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 1


def bump_alignment_version(db) -> int:
    """在对位更新事务内调用：版本 +1 并返回新版本号。"""
    v = get_alignment_version(db) + 1
    set_meta(db, "alignment_version", v)
    return v


def on_alignment_applied(db) -> Dict[str, Any]:
    """对位“有解并应用”的同一事务收尾：升版本并令未提交项过期。

    返回 ``{"version": 新版本, "expired": [ {batch_no, crack_code}, ... ]}``。
    已确认（confirmed）条目不在更新范围内，历史原样保留。
    """
    new_version = bump_alignment_version(db)
    rows = db.execute(
        """
        SELECT ri.id, ri.crack_code, rb.batch_no
          FROM review_items ri
          JOIN review_batches rb ON rb.id = ri.batch_id
         WHERE ri.status = 'pending'
           AND ri.snap_version < ?
        """,
        (new_version,),
    ).fetchall()
    expired = [{"item_id": r["id"], "batch_no": r["batch_no"],
                "crack_code": r["crack_code"]} for r in rows]
    if rows:
        db.executemany(
            """
            UPDATE review_items
               SET status = 'expired',
                   expired_at = datetime('now','localtime'),
                   updated_at = datetime('now','localtime')
             WHERE id = ?
            """,
            [(r["id"],) for r in rows],
        )
    return {"version": new_version, "expired": expired}


def expire_stale_items(db) -> List[Dict[str, Any]]:
    """防御性兜底：把快照版本落后于当前对位版本的 pending 项标过期。

    正常路径下 :func:`on_alignment_applied` 已在对位更新事务里完成标记；
    这里只用于绕过服务层直接改库等异常情况，保证提交/展示时状态一致。
    """
    current = get_alignment_version(db)
    rows = db.execute(
        """
        SELECT id FROM review_items
         WHERE status = 'pending' AND snap_version < ?
        """,
        (current,),
    ).fetchall()
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    db.executemany(
        """UPDATE review_items
              SET status='expired',
                  expired_at=datetime('now','localtime'),
                  updated_at=datetime('now','localtime')
            WHERE id=?""",
        [(i,) for i in ids],
    )
    db.commit()
    return [{"item_id": i} for i in ids]


# ---------------------------------------------------------------- helpers

def _as_float(v: Any, field: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是数字坐标")
    if not math.isfinite(f):
        raise ValidationError(f"{field} 必须是有限数字")
    return f


def _first(data: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in data and data[k] is not None and str(data[k]).strip() != "":
            return data[k]
    return None


def _as_code_list(raw: Any) -> List[str]:
    """把表单 getlist / JSON 数组 / 逗号字符串统一成去重后的编号列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        vals: List[str] = []
        for x in raw:
            vals.extend(_as_code_list(x))
    else:
        s = str(raw).replace("，", ",")
        vals = [p.strip() for p in s.split(",")]
    out: List[str] = []
    seen = set()
    for v in vals:
        if not v:
            continue
        key = v.upper()
        if key in seen:  # 同一次建批勾选重复裂缝只保留一条
            continue
        seen.add(key)
        out.append(v)
    return out


def crack_master_xy(db, crack_row) -> tuple[float, float]:
    """裂缝当前总图坐标 = 样片局部坐标 + 照片平移。"""
    img = db.execute(
        "SELECT tx, ty FROM images WHERE id=?", (crack_row["image_id"],)
    ).fetchone()
    tx = img["tx"] if img else 0.0
    ty = img["ty"] if img else 0.0
    return crack_row["x"] + tx, crack_row["y"] + ty


def _resolve_batch(db, ref: Any):
    if ref is None or str(ref).strip() == "":
        return None
    s = str(ref).strip()
    if s.isdigit():
        row = db.execute(
            "SELECT * FROM review_batches WHERE id=?", (int(s),)
        ).fetchone()
        if row:
            return row
    return db.execute(
        "SELECT * FROM review_batches WHERE UPPER(batch_no)=UPPER(?)", (s,)
    ).fetchone()


def _resolve_item(db, batch_id: int, crack_ref: Any):
    if crack_ref is None or str(crack_ref).strip() == "":
        return None
    s = str(crack_ref).strip()
    if s.isdigit():
        row = db.execute(
            "SELECT * FROM review_items WHERE id=? AND batch_id=?",
            (int(s), batch_id),
        ).fetchone()
        if row:
            return row
    return db.execute(
        """SELECT * FROM review_items
            WHERE batch_id=? AND (
                UPPER(crack_code)=UPPER(?) OR UPPER(crack_id)=UPPER(?)
            )""",
        (batch_id, s, s),
    ).fetchone()


def _log_rejection(db, batch_row, item_row, fx, fy, deviation,
                   code: str, reason: str) -> None:
    db.execute(
        """INSERT INTO review_submissions
           (batch_id, item_id, crack_code, round_no,
            field_x, field_y, deviation, accepted, reason_code, reason)
           VALUES (?,?,?,?,?,?,?,0,?,?)""",
        (batch_row["id"], item_row["id"], item_row["crack_code"],
         item_row["round_no"], fx, fy, deviation, code, reason),
    )


# ---------------------------------------------------------------- create

def create_batch(db, data: Dict[str, Any]) -> Dict[str, Any]:
    """从当前总图选择若干裂缝开始复核批次，并固化坐标/版本快照。"""
    refs = _as_code_list(_first(
        data, "crack_ids", "cracks", "crack_codes", "crack_id", "crack"
    ))
    if not refs:
        raise ValidationError("请至少勾选一条裂缝，再开始复核批次")

    crack_rows: List[Any] = []
    missing: List[str] = []
    for ref in refs:
        row = db.execute(
            "SELECT * FROM cracks WHERE id=? OR UPPER(code)=UPPER(?)",
            (ref, ref),
        ).fetchone()
        if row is None:
            missing.append(str(ref))
        elif all(r["id"] != row["id"] for r in crack_rows):
            crack_rows.append(row)
    if missing:
        raise ValidationError("选择的裂缝不存在：" + "、".join(missing))

    note = (data.get("note") or data.get("batch_note") or "")
    note = str(note).strip()[:200] or None
    version = get_alignment_version(db)

    cur = db.execute(
        """INSERT INTO review_batches (batch_no, alignment_version, note)
           VALUES (NULL, ?, ?)""",
        (version, note),
    )
    batch_id = cur.lastrowid
    batch_no = f"PC-{batch_id:04d}"
    db.execute(
        "UPDATE review_batches SET batch_no=? WHERE id=?",
        (batch_no, batch_id),
    )

    for cr in crack_rows:
        mx, my = crack_master_xy(db, cr)
        db.execute(
            """INSERT INTO review_items
               (batch_id, crack_id, crack_code,
                snap_x, snap_y, snap_version, status)
               VALUES (?,?,?,?,?,?,'pending')""",
            (batch_id, cr["id"], cr["code"], mx, my, version),
        )

    db.commit()
    return get_batch_detail(db, batch_id)


# ---------------------------------------------------------------- submit

def submit_review(db, data: Dict[str, Any]) -> Dict[str, Any]:
    """处理一条现场复核提交。

    返回结果字典；``status`` 取值：

    * ``confirmed``         首次合法，已写入唯一确认；
    * ``already_confirmed``  重复提交，返回既有确认（不覆盖历史）；
    * ``rejected``           被拒绝，带 ``reason_code/reason``。

    批次/裂缝无法解析或坐标不是数字时抛 :class:`ValidationError`（400）。
    """
    batch_ref = _first(data, "batch_no", "batch", "batch_id", "batchNo", "pid")
    if batch_ref is None:
        raise ValidationError("提交复核必须携带批次号（batch_no）")
    batch = _resolve_batch(db, batch_ref)
    if batch is None:
        raise ValidationError(f"复核批次 {batch_ref} 不存在")

    crack_ref = _first(data, "crack_code", "crack_id", "crack",
                       "code", "crackNo", "crack_no")
    if crack_ref is None:
        raise ValidationError("提交复核必须携带裂缝号（crack_code）")
    item = _resolve_item(db, batch["id"], crack_ref)
    if item is None:
        raise ValidationError(
            f"批次 {batch['batch_no']} 中没有裂缝 {crack_ref}"
        )

    x_raw = _first(data, "x", "fx", "field_x", "coord_x", "master_x", "cx")
    y_raw = _first(data, "y", "fy", "field_y", "coord_y", "master_y", "cy")
    if x_raw is None or y_raw is None:
        raise ValidationError("提交复核必须携带现场坐标 x 和 y")
    fx = _as_float(x_raw, "现场横坐标 x")
    fy = _as_float(y_raw, "现场纵坐标 y")

    base = {
        "ok": True,
        "success": True,
        "accepted": True,
        "tolerance": REVIEW_TOL,
        "batch": {
            "id": batch["id"],
            "batch_no": batch["batch_no"],
            "alignment_version": batch["alignment_version"],
        },
        "snapshot": [round(item["snap_x"], 2), round(item["snap_y"], 2)],
        "field": [round(fx, 2), round(fy, 2)],
    }

    # 已确认：幂等返回同一条历史确认，重复提交绝不产生第二条/覆盖历史。
    if item["status"] == "confirmed":
        detail = get_batch_detail(db, batch["id"])
        cur_item = next(i for i in detail["items"] if i["id"] == item["id"])
        return {
            **base,
            "status": "already_confirmed",
            "duplicate": True,
            "item": cur_item,
            "deviation": cur_item["deviation"],
            "message": "该裂缝在本批次已确认；重复提交只保留这一次记录。",
        }

    current_version = get_alignment_version(db)

    # 对位更新后未提交项已过期（含防御性懒标记）：拒绝旧坐标。
    stale = (
        item["status"] == "expired"
        or (item["status"] == "pending"
            and item["snap_version"] < current_version)
    )
    if stale:
        if item["status"] == "pending":
            db.execute(
                """UPDATE review_items
                      SET status='expired',
                          expired_at=datetime('now','localtime'),
                          updated_at=datetime('now','localtime')
                    WHERE id=?""",
                (item["id"],),
            )
        dist = math.hypot(fx - item["snap_x"], fy - item["snap_y"])
        reason = (
            f"对位已从 v{item['snap_version']} 更新到 v{current_version}，"
            f"{item['crack_code']} 的未提交复核项已过期，旧总图坐标 "
            f"({item['snap_x']:.0f}, {item['snap_y']:.0f}) 已拒绝；"
            f"请点“重开”，按更新后的总图坐标重新复核。"
        )
        _log_rejection(db, batch, item, fx, fy, dist, "expired", reason)
        db.commit()
        detail = get_batch_detail(db, batch["id"])
        cur_item = next(i for i in detail["items"] if i["id"] == item["id"])
        return {
            **base,
            "ok": False,
            "success": False,
            "accepted": False,
            "status": "rejected",
            "reason_code": "expired",
            "reason": reason,
            "error": reason,
            "deviation": round(dist, 2),
            "item": cur_item,
        }

    deviation = math.hypot(fx - item["snap_x"], fy - item["snap_y"])
    if deviation > REVIEW_TOL + 1e-9:
        reason = (
            f"现场坐标 ({fx:g}, {fy:g}) 与批次快照 "
            f"({item['snap_x']:g}, {item['snap_y']:g}) 偏差 "
            f"{deviation:.2f} 像素，超过 {REVIEW_TOL:g} 像素容差，"
            f"本次提交已拒绝，复核状态不变。"
        )
        _log_rejection(db, batch, item, fx, fy, deviation, "too_far", reason)
        db.commit()
        detail = get_batch_detail(db, batch["id"])
        cur_item = next(i for i in detail["items"] if i["id"] == item["id"])
        return {
            **base,
            "ok": False,
            "success": False,
            "accepted": False,
            "status": "rejected",
            "reason_code": "too_far",
            "reason": reason,
            "error": reason,
            "deviation": round(deviation, 2),
            "item": cur_item,
        }

    # 合法且在容差内：写入唯一一次确认。
    db.execute(
        """UPDATE review_items
              SET status='confirmed',
                  field_x=?, field_y=?, deviation=?,
                  confirmed_at=datetime('now','localtime'),
                  updated_at=datetime('now','localtime')
            WHERE id=?""",
        (fx, fy, deviation, item["id"]),
    )
    db.commit()
    detail = get_batch_detail(db, batch["id"])
    cur_item = next(i for i in detail["items"] if i["id"] == item["id"])
    return {
        **base,
        "status": "confirmed",
        "duplicate": False,
        "item": cur_item,
        "deviation": round(deviation, 2),
        "message": f"{item['crack_code']} 已确认，偏差 {deviation:.2f}px。",
    }


# ---------------------------------------------------------------- reopen

def reopen_item(db, item_id: Optional[int] = None, *,
                batch_ref: Any = None, crack_ref: Any = None) -> Dict[str, Any]:
    """重开一个过期项：按当前总图重取快照，回到待复核，轮次 +1。"""
    item = None
    batch = None
    if item_id is not None:
        item = db.execute(
            "SELECT * FROM review_items WHERE id=?", (item_id,)
        ).fetchone()
        if item is not None:
            batch = db.execute(
                "SELECT * FROM review_batches WHERE id=?", (item["batch_id"],)
            ).fetchone()
    else:
        batch = _resolve_batch(db, batch_ref)
        if batch is None:
            raise ValidationError(f"复核批次 {batch_ref} 不存在")
        item = _resolve_item(db, batch["id"], crack_ref)

    if item is None:
        raise ValidationError("要重开的复核项不存在")
    if batch is None:
        batch = db.execute(
            "SELECT * FROM review_batches WHERE id=?", (item["batch_id"],)
        ).fetchone()

    if item["status"] == "confirmed":
        raise ValidationError(
            f"{item['crack_code']} 已确认，确认记录作为历史保留，不能重开"
        )

    # pending 且未过期：幂等 no-op（可能版本恰好仍是当前）。
    current_version = get_alignment_version(db)
    if item["status"] == "pending" and item["snap_version"] == current_version:
        return get_batch_detail(db, batch["id"])

    crack = db.execute(
        "SELECT * FROM cracks WHERE id=?", (item["crack_id"],)
    ).fetchone()
    if crack is None:  # 理论上不会发生（重置会级联清表）
        raise ValidationError(f"裂缝 {item['crack_code']} 已不存在")
    mx, my = crack_master_xy(db, crack)

    db.execute(
        """UPDATE review_items
              SET status='pending',
                  snap_x=?, snap_y=?, snap_version=?,
                  round_no=round_no+1,
                  expired_at=NULL,
                  updated_at=datetime('now','localtime')
            WHERE id=?""",
        (mx, my, current_version, item["id"]),
    )
    db.commit()
    return get_batch_detail(db, batch["id"])


# ---------------------------------------------------------------- queries

def _latest_rejections(db, batch_id: int) -> Dict[int, Dict[str, Any]]:
    """每个条目“当前轮次”最近一次拒绝原因（页面展示用）。"""
    rows = db.execute(
        """
        SELECT s.* FROM review_submissions s
        JOIN (
            SELECT item_id, MAX(id) AS max_id
              FROM review_submissions
             WHERE accepted = 0 AND batch_id = ?
             GROUP BY item_id
        ) last ON last.max_id = s.id
        """,
        (batch_id,),
    ).fetchall()
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        out[r["item_id"]] = d
    return out


def get_batch_detail(db, batch_id_or_no) -> Optional[Dict[str, Any]]:
    batch = _resolve_batch(db, batch_id_or_no)
    if batch is None:
        return None
    bid = batch["id"]
    rows = db.execute(
        "SELECT * FROM review_items WHERE batch_id=? ORDER BY id", (bid,)
    ).fetchall()
    rejects = _latest_rejections(db, bid)
    current_version = get_alignment_version(db)

    items: List[Dict[str, Any]] = []
    for r in rows:
        it = dict(r)
        crack = db.execute(
            "SELECT * FROM cracks WHERE id=?", (r["crack_id"],)
        ).fetchone()
        if crack is not None:
            cx, cy = crack_master_xy(db, crack)
        else:
            cx, cy = r["snap_x"], r["snap_y"]
        it["current_x"] = round(cx, 2)
        it["current_y"] = round(cy, 2)
        it["map_shift"] = round(
            math.hypot(cx - r["snap_x"], cy - r["snap_y"]), 2
        )
        rej = rejects.get(r["id"])
        # 只展示当前轮次的拒绝原因；重开后旧轮次原因不再挂在条目上。
        if rej is not None and rej["round_no"] == r["round_no"]:
            it["last_reject"] = rej
        else:
            it["last_reject"] = None
        items.append(it)

    counts = {"pending": 0, "confirmed": 0, "expired": 0}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1

    return {
        **dict(batch),
        "alignment_current_version": current_version,
        "version_stale": batch["alignment_version"] < current_version,
        "items": items,
        "counts": counts,
        "n_pending": counts["pending"],
        "n_confirmed": counts["confirmed"],
        "n_expired": counts["expired"],
        "n_total": len(items),
    }


def list_batches(db) -> List[Dict[str, Any]]:
    rows = db.execute(
        "SELECT * FROM review_batches ORDER BY id DESC"
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        counts = {
            row["status"]: row["n"]
            for row in db.execute(
                "SELECT status, COUNT(*) AS n FROM review_items "
                "WHERE batch_id=? GROUP BY status",
                (r["id"],),
            ).fetchall()
        }
        d = dict(r)
        d["n_pending"] = counts.get("pending", 0)
        d["n_confirmed"] = counts.get("confirmed", 0)
        d["n_expired"] = counts.get("expired", 0)
        d["n_total"] = sum(counts.values())
        out.append(d)
    return out


def load_review_overview(db, selected_batch_id: Optional[int] = None
                         ) -> Dict[str, Any]:
    batches = list_batches(db)
    current = None
    if selected_batch_id is not None:
        current = get_batch_detail(db, selected_batch_id)
    if current is None and batches:
        current = get_batch_detail(db, batches[0]["id"])
    return {
        "batches": batches,
        "current": current,
        "tolerance": REVIEW_TOL,
        "alignment_version": get_alignment_version(db),
    }
