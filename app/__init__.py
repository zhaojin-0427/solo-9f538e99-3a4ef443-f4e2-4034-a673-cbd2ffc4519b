# -*- coding: utf-8 -*-
"""Flask 应用工厂与路由。"""

from __future__ import annotations

from pathlib import Path

from flask import (
    Flask, jsonify, redirect, render_template, request, url_for,
)

from .db import get_db, init_app, init_db
from .review import (
    create_batch,
    get_batch_detail,
    load_review_overview,
    reopen_item,
    submit_review,
)
from .seed_data import reset_all
from .services import (
    ValidationError, get_run, load_state, submit_alignment,
)
from .scene import ensure_sample_files


def create_app(database: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "mural-alignment-local"
    if database is None:
        from .db import default_db_path
        database = default_db_path()
    app.config["DATABASE"] = str(database)

    init_app(app)
    init_db(app.config["DATABASE"])
    ensure_sample_files()

    @app.template_filter("num")
    def _num(v):
        """整数显示为整数，浮点保留一位。"""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return v
        if abs(f - round(f)) < 1e-6:
            return str(int(round(f)))
        return f"{f:.1f}"

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    @app.get("/")
    def index():
        db = get_db()
        state = load_state(db)
        run_id = request.args.get("run", type=int)
        current_run = get_run(db, run_id) if run_id else (
            state["runs"][0] if state["runs"] else None
        )
        batch_id = request.args.get("batch", type=int)
        review = load_review_overview(db, batch_id)
        return render_template(
            "index.html",
            state=state,
            run=current_run,
            review=review,
            active_nav="operate",
        )

    def render_index_error(message, form_values=None, status=400):
        """表单校验失败时原样重渲染首页（含复核工作台），返回给定状态码。"""
        db = get_db()
        state = load_state(db)
        review = load_review_overview(db)
        return render_template(
            "index.html",
            state=state,
            run=None,
            review=review,
            form_error=message,
            form_values=form_values,
        ), status

    # ---------- 传统表单提交（无 JS 也可用，PRG） ----------
    @app.post("/submit")
    def submit_form():
        db = get_db()
        try:
            run = submit_alignment(db, request.form)
        except ValidationError as exc:
            return render_index_error(str(exc), request.form)
        return redirect(url_for("index", run=run["id"]))

    @app.post("/reset")
    def reset_form():
        db = get_db()
        reset_all(db)
        return redirect(url_for("index"))

    # ---------- JSON API ----------
    @app.get("/api/state")
    def api_state():
        return jsonify(load_state(get_db()))

    @app.post("/api/submit")
    def api_submit():
        db = get_db()
        data = request.get_json(silent=True)
        if data is None:
            data = request.form.to_dict()
        try:
            run = submit_alignment(db, data)
        except ValidationError as exc:
            return jsonify(status="invalid", error=str(exc)), 400
        return jsonify(_api_run(run))

    @app.post("/api/reset")
    def api_reset():
        db = get_db()
        reset_all(db)
        return jsonify(ok=True, state=load_state(db))

    @app.get("/api/runs/<int:run_id>")
    def api_run_detail(run_id: int):
        run = get_run(get_db(), run_id)
        if not run:
            return jsonify(error="run not found"), 404
        return jsonify(_api_run(run))

    # ============================================================
    # 现场复核批次：页面表单（PRG，无 JS 可用）
    # ============================================================
    @app.post("/review/batches")
    def review_create_form():
        db = get_db()
        data = {
            "crack_ids": request.form.getlist("crack_ids"),
            "note": request.form.get("note", ""),
        }
        try:
            batch = create_batch(db, data)
        except ValidationError as exc:
            return render_template(
                "index.html",
                state=load_state(db),
                run=None,
                review=load_review_overview(db),
                review_form_error=str(exc),
                review_form_values=request.form,
            ), 400
        return redirect(url_for("index", batch=batch["id"]) + "#review")

    @app.post("/review/submit")
    def review_submit_form():
        db = get_db()
        data = {k: request.form.get(k) for k in (
            "batch_no", "batch", "batch_id",
            "crack_code", "crack_id", "crack", "code",
            "x", "y", "fx", "fy", "field_x", "field_y",
        )}
        try:
            result = submit_review(db, data)
        except ValidationError as exc:
            # 连批次/裂缝都解析不了：留在复核区并直接显示错误（不改变任何状态）
            return render_template(
                "index.html",
                state=load_state(db),
                run=None,
                review=load_review_overview(db),
                review_form_error=str(exc),
                review_form_values=request.form,
            ), 400

        if result["status"] == "rejected":
            # 业务拒绝（超差/过期）：拒绝原因已落审计，直接重渲染 400 页面，
            # 顶部横幅与对应行“拒绝原因”同时可见；复核状态未改变。
            return render_template(
                "index.html",
                state=load_state(db),
                run=None,
                review=load_review_overview(db, result["batch"]["id"]),
                review_form_error=result["reason"],
                review_form_values=request.form,
            ), 400

        if result["status"] == "already_confirmed":
            target = url_for(
                "index", batch=result["batch"]["id"], review_dup=1)
        else:
            target = url_for(
                "index", batch=result["batch"]["id"], review_done=1)
        return redirect(target + "#review")

    @app.post("/review/items/<int:item_id>/reopen")
    def review_reopen_form(item_id: int):
        db = get_db()
        try:
            detail = reopen_item(db, item_id=item_id)
        except ValidationError as exc:
            return render_template(
                "index.html",
                state=load_state(db),
                run=None,
                review=load_review_overview(db),
                review_form_error=str(exc),
            ), 400
        return redirect(
            url_for("index", batch=detail["id"], review_reopened=1) + "#review"
        )

    # ============================================================
    # 现场复核批次：JSON API
    # ============================================================
    @app.get("/api/review/batches")
    def api_review_batches():
        db = get_db()
        overview = load_review_overview(db)
        return jsonify({
            "alignment_version": overview["alignment_version"],
            "tolerance": 2.0,
            "batches": [
                get_batch_detail(db, b["id"]) for b in overview["batches"]
            ],
        })

    @app.post("/api/review/batches")
    def api_review_create():
        db = get_db()
        data = request.get_json(silent=True)
        if data is None:
            # 兼容表单 / 混合提交（含多选 crack_ids）
            data = request.form.to_dict(flat=False)
            data = {k: (v if len(v) > 1 else v[0]) for k, v in data.items()}
            if "crack_ids" in request.form:
                data["crack_ids"] = request.form.getlist("crack_ids")
        try:
            batch = create_batch(db, data)
        except ValidationError as exc:
            return jsonify(status="invalid", ok=False, error=str(exc)), 400
        return jsonify({"status": "created", "ok": True, "batch": batch})

    @app.get("/api/review/batches/<path:batch_ref>")
    def api_review_detail(batch_ref: str):
        batch = get_batch_detail(get_db(), batch_ref)
        if not batch:
            return jsonify(status="invalid", ok=False,
                           error=f"复核批次 {batch_ref} 不存在"), 404
        return jsonify(batch)

    @app.post("/api/review/submit")
    @app.post("/api/review")
    @app.post("/api/reviews")
    def api_review_submit():
        db = get_db()
        data = request.get_json(silent=True)
        if data is None:
            data = request.form.to_dict()
        try:
            result = submit_review(db, data)
        except ValidationError as exc:
            return jsonify(status="invalid", ok=False, success=False,
                           accepted=False,
                           error=str(exc), reason=str(exc)), 400
        if result["status"] == "rejected":
            # 与全站“无效提交 -> 400”的约定保持一致；具体原因看 reason_code。
            return jsonify(result), 400
        return jsonify(result)

    @app.post("/api/review/items/<int:item_id>/reopen")
    def api_review_reopen(item_id: int):
        db = get_db()
        try:
            detail = reopen_item(db, item_id=item_id)
        except ValidationError as exc:
            return jsonify(status="invalid", ok=False, error=str(exc)), 400
        return jsonify(status="reopened", ok=True, batch=detail)

    @app.post("/api/review/reopen")
    def api_review_reopen_by_ref():
        db = get_db()
        data = request.get_json(silent=True)
        if data is None:
            data = request.form.to_dict()
        try:
            detail = reopen_item(
                db,
                batch_ref=data.get("batch_no") or data.get("batch"),
                crack_ref=data.get("crack_code") or data.get("crack_id")
                or data.get("crack"),
            )
        except ValidationError as exc:
            return jsonify(status="invalid", ok=False, error=str(exc)), 400
        if detail is None:
            return jsonify(status="invalid", ok=False,
                           error="要重开的复核项不存在"), 404
        return jsonify(status="reopened", ok=True, batch=detail)

    return app


def _api_run(run: dict) -> dict:
    return {
        "id": run["id"],
        "created_at": run["created_at"],
        "status": run["status"],
        "summary": run["summary"],
        "submission": {
            "image_a": run["image_a"], "image_b": run["image_b"],
            "ax": run["ax"], "ay": run["ay"],
            "bx": run["bx"], "by": run["by"],
        },
        **run["result"],
        # 无解诊断放在 reason 字段（若有）
        "reason": run["result"].get("reason", ""),
    }
