"""크라우드 주택관리 프로그램 - 웹 서버."""
import datetime as dt
import functools
import json
import os
import secrets

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session,
                   url_for)
from markupsafe import escape
from werkzeug.security import check_password_hash, generate_password_hash

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import billing  # noqa: E402  (DATABASE_URL 을 읽은 뒤에 불러와야 함)
import importer  # noqa: E402
import work  # noqa: E402
from db import IS_PG, close_db, get_db, get_settings, init_db  # noqa: E402

app = Flask(__name__, instance_relative_config=True)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.teardown_appcontext(close_db)
app.register_blueprint(work.bp)

# Vercel 같은 서버리스 환경은 파일 시스템이 읽기 전용이고 요청이 끝나면 서버가 사라진다.
# 그래서 자료는 반드시 Supabase(DATABASE_URL)에, 로그인 키는 SECRET_KEY 환경변수에 둬야 한다.
ON_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
CONFIG_ERRORS = []

app.secret_key = os.environ.get("SECRET_KEY")
if ON_SERVERLESS:
    if not IS_PG:
        CONFIG_ERRORS.append("DATABASE_URL 환경변수가 없습니다. Supabase 연결 문자열을 넣어야 자료가 저장됩니다.")
    if not app.secret_key:
        CONFIG_ERRORS.append("SECRET_KEY 환경변수가 없습니다. 긴 임의 문자열을 넣어야 로그인이 유지됩니다.")
        app.secret_key = secrets.token_hex(32)
else:
    os.makedirs(app.instance_path, exist_ok=True)
    if not app.secret_key:
        _key_file = os.path.join(app.instance_path, "secret.key")
        if not os.path.exists(_key_file):
            with open(_key_file, "w") as f:
                f.write(secrets.token_hex(32))
        with open(_key_file) as f:
            app.secret_key = f.read().strip()

if not CONFIG_ERRORS:
    try:
        init_db(os.path.join(app.instance_path, "housing.db"))
    except Exception as e:  # 연결 문자열·비밀번호 오류 등을 화면에 보여주기 위해 잡는다
        CONFIG_ERRORS.append(f"데이터베이스에 연결하지 못했습니다: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- 공통

def today():
    return dt.date.today().isoformat()


def this_ym():
    return dt.date.today().strftime("%Y-%m")


@app.template_filter("won")
def won(v):
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return ""


@app.template_filter("ymk")
def ymk(ym):
    return f"{ym[:4]}년 {int(ym[5:7])}월" if ym and len(ym) >= 7 else ""


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf_token": session["csrf"], "METHODS": billing.METHODS, "UTILS": billing.UTILS,
            "user": g.get("user"), "today": today(), "this_ym": this_ym(),
            "db_kind": "Supabase" if IS_PG else "이 PC (SQLite)", "require_login": REQUIRE_LOGIN}


PUBLIC = {"login", "setup", "static"}

# 로그인 사용 여부. 이 PC에서는 로그인 없이 바로 쓰고, 인터넷에 공개되는 서버리스(Vercel)에서는 로그인을 요구한다.
# 환경변수 REQUIRE_LOGIN=1 / 0 으로 직접 정할 수 있다.
_login_env = os.environ.get("REQUIRE_LOGIN", "").strip().lower()
REQUIRE_LOGIN = _login_env in ("1", "true", "yes", "on") if _login_env else ON_SERVERLESS
GUEST = {"id": 0, "username": "", "name": "관리자", "is_admin": 1, "pw_hash": ""}


@app.before_request
def guard():
    if CONFIG_ERRORS:
        items = "".join(f"<li>{escape(e)}</li>" for e in CONFIG_ERRORS)
        return (f"<!doctype html><meta charset='utf-8'><title>설정 필요</title>"
                f"<div style='font-family:sans-serif;max-width:640px;margin:10vh auto;line-height:1.6'>"
                f"<h2>서버 설정이 필요합니다</h2><ul>{items}</ul>"
                f"<p>Vercel 프로젝트 → Settings → Environment Variables 에 값을 넣은 뒤 다시 배포(Redeploy)하세요.</p></div>", 503)
    if request.endpoint == "static":
        return None
    db = get_db()
    g.user = None
    if not REQUIRE_LOGIN:
        g.user = GUEST
        if request.endpoint in ("login", "setup", "password"):
            return redirect(url_for("dashboard"))
    elif request.endpoint in PUBLIC:
        pass
    elif db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        return redirect(url_for("setup"))
    else:
        uid = session.get("uid")
        g.user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() if uid else None
        if g.user is None:
            return redirect(url_for("login", next=request.path))
    if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
        abort(400, "보안 토큰이 맞지 않습니다. 페이지를 새로고침한 뒤 다시 시도하세요.")


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not g.user or not g.user["is_admin"]:
            abort(403)
        return fn(*a, **kw)
    return wrapper


def f_int(name, default=0):
    v = (request.form.get(name) or "").replace(",", "").strip()
    try:
        return int(float(v)) if v else default
    except ValueError:
        return default


def f_float(name):
    v = (request.form.get(name) or "").replace(",", "").strip()
    try:
        return float(v) if v != "" else None
    except ValueError:
        return None


def f_str(name):
    return (request.form.get(name) or "").strip()


def buildings_list(db):
    return db.execute("SELECT * FROM buildings WHERE active = 1 ORDER BY name").fetchall()


# ---------------------------------------------------------------- 로그인

@app.route("/setup", methods=["GET", "POST"])
def setup():
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        username, pw = f_str("username"), request.form.get("password") or ""
        if not username or len(pw) < 6:
            flash("아이디와 6자 이상의 비밀번호를 입력하세요.", "error")
        elif pw != request.form.get("password2"):
            flash("비밀번호 확인이 일치하지 않습니다.", "error")
        else:
            db.execute("INSERT INTO users(username, pw_hash, name, is_admin) VALUES (?, ?, ?, 1)",
                       (username, generate_password_hash(pw), f_str("name") or username))
            db.commit()
            flash("관리자 계정을 만들었습니다. 로그인하세요.", "ok")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = get_db().execute("SELECT * FROM users WHERE username = ?", (f_str("username"),)).fetchone()
        if u and check_password_hash(u["pw_hash"], request.form.get("password") or ""):
            session.clear()
            session["uid"] = u["id"]
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        flash("아이디 또는 비밀번호가 맞지 않습니다.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- 대시보드

@app.route("/")
def dashboard():
    db = get_db()
    ym = request.args.get("ym") or (db.execute("SELECT MAX(ym) FROM bills WHERE kind = 'normal'").fetchone()[0] or this_ym())
    month = billing.bill_statuses(db, "b.ym = ? AND b.kind = 'normal'", (ym,))
    all_owed = [b for b in billing.bill_statuses(db) if b["owed"] > 0]
    stats = {
        "buildings": db.execute("SELECT COUNT(*) FROM buildings WHERE active = 1").fetchone()[0],
        "units": db.execute("SELECT COUNT(*) FROM units u JOIN buildings b ON b.id = u.building_id WHERE b.active = 1").fetchone()[0],
        "vacant": db.execute("SELECT COUNT(*) FROM units u JOIN buildings b ON b.id = u.building_id WHERE b.active = 1 AND u.status = '공실'").fetchone()[0],
        "billed": sum(b["amount"] for b in month),
        "paid": sum(b["paid"] for b in month),
        "month_owed": sum(max(b["owed"], 0) for b in month),
        "total_owed": sum(b["owed"] for b in all_owed),
        "owed_units": len({b["unit_id"] for b in all_owed}),
    }
    recent = db.execute(
        """SELECT p.*, u.ho, b.name AS building_name FROM payments p JOIN units u ON u.id = p.unit_id
           JOIN buildings b ON b.id = u.building_id ORDER BY p.pay_date DESC, p.id DESC LIMIT 10""").fetchall()
    return render_template("dashboard.html", ym=ym, stats=stats, recent=recent)


# ---------------------------------------------------------------- 빌라

@app.route("/buildings")
def buildings():
    db = get_db()
    q = request.args.get("q", "").strip()
    rows = db.execute(
        """SELECT b.*, COUNT(u.id) AS units, CAST(COALESCE(SUM(CASE WHEN u.status = '공실' THEN 1 ELSE 0 END), 0) AS BIGINT) AS vacant
           FROM buildings b LEFT JOIN units u ON u.building_id = b.id
           WHERE b.active = 1 AND (b.name LIKE ? OR COALESCE(b.address,'') LIKE ? OR COALESCE(b.owner_name,'') LIKE ?)
           GROUP BY b.id ORDER BY b.name""", (f"%{q}%",) * 3).fetchall()
    return render_template("buildings.html", rows=rows, q=q)


BUILDING_FIELDS = ["code", "name", "kind", "address", "zip", "owner_name", "owner_phone", "manager",
                   "elec_no", "water_no", "gas_no", "elec_method", "water_method", "gas_method", "common_method", "memo"]


@app.route("/buildings/new", methods=["GET", "POST"])
@app.route("/buildings/<int:bid>", methods=["GET", "POST"])
def building_edit(bid=None):
    db = get_db()
    b = db.execute("SELECT * FROM buildings WHERE id = ?", (bid,)).fetchone() if bid else None
    if bid and b is None:
        abort(404)
    if request.method == "POST":
        vals = {k: f_str(k) for k in BUILDING_FIELDS}
        vals["bill_rent"] = 1 if request.form.get("bill_rent") else 0
        if not vals["name"]:
            flash("빌라명은 반드시 입력해야 합니다.", "error")
        else:
            if b:
                db.execute(f"UPDATE buildings SET {', '.join(k + ' = ?' for k in vals)} WHERE id = ?",
                           list(vals.values()) + [bid])
            else:
                bid = db.execute(f"INSERT INTO buildings({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))}) RETURNING id",
                                 list(vals.values())).fetchone()[0]
            db.commit()
            flash("저장했습니다.", "ok")
            return redirect(url_for("building_edit", bid=bid))
    units, fees = [], []
    if b:
        units = db.execute("SELECT * FROM units WHERE building_id = ? ORDER BY LENGTH(ho), ho", (bid,)).fetchall()
        fees = db.execute(
            """SELECT fi.id, fi.name, bf.amount, bf.method FROM fee_items fi
               LEFT JOIN building_fees bf ON bf.item_id = fi.id AND bf.building_id = ?
               WHERE fi.active = 1 ORDER BY fi.sort, fi.id""", (bid,)).fetchall()
    return render_template("building_edit.html", b=b, units=units, fees=fees)


@app.route("/buildings/<int:bid>/fees", methods=["POST"])
def building_fees(bid):
    db = get_db()
    for item in db.execute("SELECT id FROM fee_items WHERE active = 1"):
        amount = f_int(f"amount_{item['id']}")
        method = request.form.get(f"method_{item['id']}") or "unit"
        if amount or db.execute("SELECT 1 FROM unit_fees uf JOIN units u ON u.id = uf.unit_id WHERE u.building_id = ? AND uf.item_id = ?",
                                (bid, item["id"])).fetchone():
            db.execute("INSERT INTO building_fees(building_id, item_id, amount, method) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT (building_id, item_id) DO UPDATE SET amount = excluded.amount, method = excluded.method",
                       (bid, item["id"], amount, method))
        else:
            db.execute("DELETE FROM building_fees WHERE building_id = ? AND item_id = ?", (bid, item["id"]))
    db.commit()
    flash("관리비 항목을 저장했습니다.", "ok")
    return redirect(url_for("building_edit", bid=bid) + "#fees")


@app.route("/buildings/<int:bid>/delete", methods=["POST"])
@admin_required
def building_delete(bid):
    db = get_db()
    if request.form.get("confirm_name") != db.execute("SELECT name FROM buildings WHERE id = ?", (bid,)).fetchone()["name"]:
        flash("빌라명을 정확히 입력해야 삭제됩니다.", "error")
        return redirect(url_for("building_edit", bid=bid))
    db.execute("DELETE FROM buildings WHERE id = ?", (bid,))
    db.commit()
    flash("빌라와 소속 세대·부과·수납 자료를 삭제했습니다.", "ok")
    return redirect(url_for("buildings"))


# ---------------------------------------------------------------- 세대

UNIT_FIELDS = ["ho", "tenant", "phone", "mobile", "move_in", "move_out", "status", "memo"]


@app.route("/units/new", methods=["GET", "POST"])
@app.route("/units/<int:uid>", methods=["GET", "POST"])
def unit_edit(uid=None):
    db = get_db()
    u = db.execute("SELECT * FROM units WHERE id = ?", (uid,)).fetchone() if uid else None
    if uid and u is None:
        abort(404)
    bid = u["building_id"] if u else request.args.get("building_id", type=int)
    b = db.execute("SELECT * FROM buildings WHERE id = ?", (bid,)).fetchone()
    if b is None:
        abort(404)
    if request.method == "POST":
        vals = {k: f_str(k) for k in UNIT_FIELDS}
        vals.update(deposit=f_int("deposit"), rent=f_int("rent"), auto_transfer=1 if request.form.get("auto_transfer") else 0)
        if not vals["ho"]:
            flash("호수는 반드시 입력해야 합니다.", "error")
        elif db.execute("SELECT 1 FROM units WHERE building_id = ? AND ho = ? AND id != ?", (bid, vals["ho"], uid or 0)).fetchone():
            flash("이미 등록된 호수입니다.", "error")
        else:
            if u:
                db.execute(f"UPDATE units SET {', '.join(k + ' = ?' for k in vals)} WHERE id = ?", list(vals.values()) + [uid])
            else:
                vals["building_id"] = bid
                uid = db.execute(f"INSERT INTO units({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))}) RETURNING id",
                                 list(vals.values())).fetchone()[0]
            for item in db.execute("SELECT item_id FROM building_fees WHERE building_id = ?", (bid,)).fetchall():
                raw = f_str(f"fee_{item['item_id']}")
                if raw == "":
                    db.execute("DELETE FROM unit_fees WHERE unit_id = ? AND item_id = ?", (uid, item["item_id"]))
                else:
                    db.execute("INSERT INTO unit_fees(unit_id, item_id, amount) VALUES (?, ?, ?) "
                               "ON CONFLICT (unit_id, item_id) DO UPDATE SET amount = excluded.amount",
                               (uid, item["item_id"], f_int(f"fee_{item['item_id']}")))
            db.commit()
            flash("저장했습니다.", "ok")
            if request.form.get("next") == "new":
                return redirect(url_for("unit_edit", building_id=bid))
            return redirect(url_for("building_edit", bid=bid))
    fees = db.execute(
        """SELECT fi.id, fi.name, bf.amount AS base, bf.method, uf.amount AS own FROM building_fees bf
           JOIN fee_items fi ON fi.id = bf.item_id
           LEFT JOIN unit_fees uf ON uf.item_id = bf.item_id AND uf.unit_id = ?
           WHERE bf.building_id = ? AND bf.method = 'unit' ORDER BY fi.sort""", (uid or 0, bid)).fetchall()
    return render_template("unit_edit.html", u=u, b=b, fees=fees)


@app.route("/units/<int:uid>/delete", methods=["POST"])
def unit_delete(uid):
    db = get_db()
    u = db.execute("SELECT * FROM units WHERE id = ?", (uid,)).fetchone()
    if db.execute("SELECT 1 FROM payments WHERE unit_id = ?", (uid,)).fetchone():
        flash("수납 기록이 있는 세대는 삭제할 수 없습니다. 이사 처리(퇴거일 입력) 또는 공실로 바꾸세요.", "error")
        return redirect(url_for("unit_edit", uid=uid))
    db.execute("DELETE FROM units WHERE id = ?", (uid,))
    db.commit()
    flash(f"{u['ho']}호를 삭제했습니다.", "ok")
    return redirect(url_for("building_edit", bid=u["building_id"]))


@app.route("/units/<int:uid>/ledger")
def unit_ledger(uid):
    db = get_db()
    u = db.execute("SELECT u.*, b.name AS building_name FROM units u JOIN buildings b ON b.id = u.building_id WHERE u.id = ?",
                   (uid,)).fetchone()
    if u is None:
        abort(404)
    bills = billing.bill_statuses(db, "b.unit_id = ?", (uid,))
    payments = db.execute("SELECT * FROM payments WHERE unit_id = ? ORDER BY pay_date DESC, id DESC", (uid,)).fetchall()
    return render_template("unit_ledger.html", u=u, bills=bills, payments=payments,
                           owed=sum(b["owed"] for b in bills), credit=billing.unit_credit(db, uid))


# ---------------------------------------------------------------- 검침

@app.route("/readings", methods=["GET", "POST"])
def readings():
    db = get_db()
    blist = buildings_list(db)
    bid = request.values.get("building_id", type=int) or (blist[0]["id"] if blist else None)
    ym = request.values.get("ym") or this_ym()
    b = db.execute("SELECT * FROM buildings WHERE id = ?", (bid,)).fetchone() if bid else None
    if b is None:
        return render_template("readings.html", blist=blist, b=None, ym=ym)
    utils = [k for k in billing.UTILS if b[f"{k}_method"] in ("price", "ratio")]
    units = db.execute("SELECT * FROM units WHERE building_id = ? ORDER BY LENGTH(ho), ho", (bid,)).fetchall()

    if request.method == "POST":
        for u in units:
            for util in utils:
                prev, curr = f_float(f"prev_{u['id']}_{util}"), f_float(f"curr_{u['id']}_{util}")
                if prev is None and curr is None:
                    db.execute("DELETE FROM readings WHERE unit_id = ? AND ym = ? AND util = ?", (u["id"], ym, util))
                else:
                    db.execute("INSERT INTO readings(unit_id, ym, util, prev, curr) VALUES (?, ?, ?, ?, ?) "
                               "ON CONFLICT (unit_id, ym, util) DO UPDATE SET prev = excluded.prev, curr = excluded.curr",
                               (u["id"], ym, util, prev, curr))
        for util in list(billing.UTILS) + ["common"]:
            if f_str(f"bb_amount_{util}") or f_str(f"bb_price_{util}"):
                db.execute("INSERT INTO building_bills(building_id, ym, util, amount, unit_price) VALUES (?, ?, ?, ?, ?) "
                           "ON CONFLICT (building_id, ym, util) DO UPDATE SET amount = excluded.amount, unit_price = excluded.unit_price",
                           (bid, ym, util, f_int(f"bb_amount_{util}"), f_float(f"bb_price_{util}") or 0))
            else:
                db.execute("DELETE FROM building_bills WHERE building_id = ? AND ym = ? AND util = ?", (bid, ym, util))
        db.commit()
        flash(f"{b['name']} {ymk(ym)} 검침을 저장했습니다.", "ok")
        return redirect(url_for("readings", building_id=bid, ym=ym))

    grid = []
    for u in units:
        row = {"u": u, "vals": {}}
        for util in utils:
            r = db.execute("SELECT prev, curr FROM readings WHERE unit_id = ? AND ym = ? AND util = ?", (u["id"], ym, util)).fetchone()
            prev = r["prev"] if r and r["prev"] is not None else billing.previous_reading(db, u["id"], util, ym)
            row["vals"][util] = (prev, r["curr"] if r else None)
        grid.append(row)
    bbills = {r["util"]: r for r in db.execute("SELECT * FROM building_bills WHERE building_id = ? AND ym = ?", (bid, ym))}
    return render_template("readings.html", blist=blist, b=b, ym=ym, utils=utils, grid=grid, bbills=bbills,
                           UTIL_UNITS=billing.UTIL_UNITS, fmt=billing.fmt_num)


# ---------------------------------------------------------------- 관리비 계산

@app.route("/calc", methods=["GET", "POST"])
def calc():
    db = get_db()
    settings = get_settings(db)
    blist = buildings_list(db)
    ym = request.values.get("ym") or this_ym()
    results = None
    if request.method == "POST":
        ids = [int(x) for x in request.form.getlist("building_ids")]
        due = f_str("due_date") or billing.default_due_date(settings, ym)
        issued = f_str("issued") or today()
        if not ids:
            flash("계산할 빌라를 선택하세요.", "error")
        else:
            results = [billing.calculate_building(db, i, ym, due, issued) for i in ids]
            db.commit()
            flash(f"{ymk(ym)} 관리비 계산을 마쳤습니다. 총 {sum(r['created'] for r in results)}세대, "
                  f"{sum(r['total'] for r in results):,}원", "ok")
    done = {r[0]: (r[1], r[2]) for r in db.execute(
        """SELECT u.building_id, COUNT(*), CAST(SUM(b.amount) AS BIGINT) FROM bills b JOIN units u ON u.id = b.unit_id
           WHERE b.ym = ? AND b.kind = 'normal' GROUP BY u.building_id""", (ym,))}
    return render_template("calc.html", blist=blist, ym=ym, done=done, results=results,
                           due=billing.default_due_date(settings, ym))


@app.route("/bills")
def bills():
    db = get_db()
    blist = buildings_list(db)
    ym = request.args.get("ym") or this_ym()
    bid = request.args.get("building_id", type=int)
    where, params = "b.ym = ? AND b.kind = 'normal'", [ym]
    if bid:
        where += " AND u.building_id = ?"
        params.append(bid)
    rows = billing.bill_statuses(db, where, params)
    return render_template("bills.html", blist=blist, ym=ym, bid=bid, rows=rows)


@app.route("/bills/<int:bill_id>/delete", methods=["POST"])
def bill_delete(bill_id):
    db = get_db()
    bl = db.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    db.execute("DELETE FROM bills WHERE id = ?", (bill_id,))
    billing.reallocate_unit(db, bl["unit_id"])
    db.commit()
    flash("부과 내역을 삭제했습니다. 해당 세대의 수납액은 다른 고지 건에 다시 배정됩니다.", "ok")
    return redirect(request.referrer or url_for("bills"))


def notice_data(db, where, params):
    """고지서 출력용 자료: 세대별 당월 고지 + 이전 미납."""
    settings = get_settings(db)
    rows = billing.bill_statuses(db, where, params)
    out = []
    for r in rows:
        others = billing.bill_statuses(db, "b.unit_id = ? AND b.id != ? AND (b.ym < ? OR b.kind = 'carry')",
                                       (r["unit_id"], r["id"], r["ym"]))
        prev_owed = sum(o["owed"] for o in others if o["owed"] > 0)
        lines = db.execute("SELECT * FROM bill_lines WHERE bill_id = ? ORDER BY sort", (r["id"],)).fetchall()
        meters = db.execute("SELECT * FROM readings WHERE unit_id = ? AND ym = ? ORDER BY util", (r["unit_id"], r["ym"])).fetchall()
        history = db.execute(
            "SELECT ym, amount FROM bills WHERE unit_id = ? AND kind = 'normal' AND ym <= ? ORDER BY ym DESC LIMIT 6",
            (r["unit_id"], r["ym"])).fetchall()[::-1]
        out.append({"bill": r, "lines": lines, "meters": meters, "prev_owed": prev_owed, "history": history,
                    "before_due": r["amount"] + prev_owed, "after_due": r["amount"] + r["late_fee"] + prev_owed})
    return out, settings


@app.route("/bills/print")
def bills_print():
    db = get_db()
    ym = request.args.get("ym") or this_ym()
    where, params = "b.ym = ? AND b.kind = 'normal'", [ym]
    if request.args.get("building_id"):
        where += " AND u.building_id = ?"
        params.append(request.args.get("building_id", type=int))
    if request.args.get("unit_id"):
        where += " AND u.id = ?"
        params.append(request.args.get("unit_id", type=int))
    items, settings = notice_data(db, where, params)
    return render_template("print_bills.html", items=items, s=settings, ym=ym, util_names=billing.UTILS,
                           util_units=billing.UTIL_UNITS, fmt=billing.fmt_num)


# ---------------------------------------------------------------- 수납

@app.route("/payments", methods=["GET", "POST"])
def payments():
    db = get_db()
    if request.method == "POST":
        uid = request.form.get("unit_id", type=int)
        amount = f_int("amount")
        if not uid or amount <= 0:
            flash("세대와 금액을 입력하세요.", "error")
        else:
            db.execute("INSERT INTO payments(unit_id, pay_date, amount, method, payer, memo) VALUES (?, ?, ?, ?, ?, ?)",
                       (uid, f_str("pay_date") or today(), amount, f_str("method"), f_str("payer"), f_str("memo")))
            billing.reallocate_unit(db, uid)
            db.commit()
            flash(f"{amount:,}원 수납 처리했습니다.", "ok")
        return redirect(url_for("payments", d1=request.args.get("d1"), d2=request.args.get("d2"),
                                building_id=request.form.get("building_id")))
    d1 = request.args.get("d1") or today()[:8] + "01"
    d2 = request.args.get("d2") or today()
    rows = db.execute(
        """SELECT p.*, u.ho, u.tenant, b.name AS building_name FROM payments p JOIN units u ON u.id = p.unit_id
           JOIN buildings b ON b.id = u.building_id WHERE p.pay_date BETWEEN ? AND ?
           ORDER BY p.pay_date DESC, p.id DESC""", (d1, d2)).fetchall()
    by_method = {}
    for r in rows:
        by_method[r["method"] or "기타"] = by_method.get(r["method"] or "기타", 0) + r["amount"]
    return render_template("payments.html", blist=buildings_list(db), rows=rows, d1=d1, d2=d2,
                           total=sum(r["amount"] for r in rows), by_method=by_method,
                           sel_building=request.args.get("building_id", type=int))


@app.route("/payments/<int:pid>/delete", methods=["POST"])
def payment_delete(pid):
    db = get_db()
    p = db.execute("SELECT * FROM payments WHERE id = ?", (pid,)).fetchone()
    db.execute("DELETE FROM payments WHERE id = ?", (pid,))
    billing.reallocate_unit(db, p["unit_id"])
    db.commit()
    flash("수납 기록을 삭제했습니다.", "ok")
    return redirect(request.referrer or url_for("payments"))


@app.route("/payments/bulk", methods=["GET", "POST"])
def payments_bulk():
    db = get_db()
    blist = buildings_list(db)
    ym = request.values.get("ym") or this_ym()
    bid = request.values.get("building_id", type=int)
    if request.method == "POST":
        pay_date, method = f_str("pay_date") or today(), f_str("method")
        count = 0
        for bill_id in request.form.getlist("bill_ids"):
            st = billing.bill_statuses(db, "b.id = ?", (int(bill_id),), asof=pay_date)
            if st and st[0]["owed"] > 0:
                db.execute("INSERT INTO payments(unit_id, pay_date, amount, method, memo) VALUES (?, ?, ?, ?, ?)",
                           (st[0]["unit_id"], pay_date, st[0]["owed"], method, f"{ymk(st[0]['ym'])}분 일괄수납"))
                billing.reallocate_unit(db, st[0]["unit_id"])
                count += 1
        db.commit()
        flash(f"{count}건 일괄 수납 처리했습니다.", "ok")
        return redirect(url_for("payments_bulk", ym=ym, building_id=bid or ""))
    where, params = "b.ym = ?", [ym]
    if bid:
        where += " AND u.building_id = ?"
        params.append(bid)
    rows = [r for r in billing.bill_statuses(db, where, params) if r["owed"] > 0]
    return render_template("payments_bulk.html", blist=blist, ym=ym, bid=bid, rows=rows)


# ---------------------------------------------------------------- 미납

def unpaid_summary(db, bid=None, min_months=1):
    where, params = "1=1", []
    if bid:
        where += " AND u.building_id = ?"
        params.append(bid)
    per_unit = {}
    for r in billing.bill_statuses(db, where, params):
        if r["owed"] <= 0:
            continue
        e = per_unit.setdefault(r["unit_id"], {"unit_id": r["unit_id"], "building_name": r["building_name"], "ho": r["ho"],
                                               "tenant": r["tenant"], "mobile": r["mobile"] or r["phone"], "bills": [], "owed": 0})
        e["bills"].append(r)
        e["owed"] += r["owed"]
    return [e for e in per_unit.values() if len(e["bills"]) >= min_months]


@app.route("/unpaid")
def unpaid():
    db = get_db()
    bid = request.args.get("building_id", type=int)
    months = request.args.get("months", type=int) or 1
    rows = unpaid_summary(db, bid, months)
    return render_template("unpaid.html", blist=buildings_list(db), rows=rows, bid=bid, months=months,
                           total=sum(r["owed"] for r in rows))


@app.route("/unpaid/print")
def unpaid_print():
    db = get_db()
    ids = set(request.args.getlist("unit_id", type=int))
    rows = [r for r in unpaid_summary(db) if r["unit_id"] in ids]
    return render_template("print_dunning.html", rows=rows, s=get_settings(db),
                           deadline=request.args.get("deadline") or "")


# ---------------------------------------------------------------- 설정

SETTING_KEYS = ["company_name", "company_phone", "company_address", "bank_info", "late_rate", "round_unit",
                "round_mode", "due_offset", "due_day", "notice", "dunning_text"]


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    db = get_db()
    if request.method == "POST":
        if not g.user["is_admin"]:
            abort(403)
        for k in SETTING_KEYS:
            db.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (k, request.form.get(k, "").strip()))
        db.commit()
        flash("설정을 저장했습니다.", "ok")
        return redirect(url_for("settings_page"))
    items = db.execute("SELECT * FROM fee_items ORDER BY sort, id").fetchall()
    users = db.execute("SELECT id, username, name, is_admin, created FROM users ORDER BY id").fetchall()
    return render_template("settings.html", s=get_settings(db), items=items, users=users)


@app.route("/settings/items", methods=["POST"])
@admin_required
def settings_items():
    db = get_db()
    for item in db.execute("SELECT id FROM fee_items").fetchall():
        name = f_str(f"name_{item['id']}")
        if name:
            db.execute("UPDATE fee_items SET name = ?, sort = ?, active = ? WHERE id = ?",
                       (name, f_int(f"sort_{item['id']}"), 1 if request.form.get(f"active_{item['id']}") else 0, item["id"]))
    new = f_str("new_name")
    if new:
        if db.execute("SELECT 1 FROM fee_items WHERE name = ?", (new,)).fetchone():
            flash("같은 이름의 항목이 이미 있습니다.", "error")
        else:
            db.execute("INSERT INTO fee_items(name, sort) VALUES (?, ?)", (new, f_int("new_sort", 99)))
    db.commit()
    flash("관리비 항목을 저장했습니다.", "ok")
    return redirect(url_for("settings_page") + "#items")


@app.route("/settings/users", methods=["POST"])
@admin_required
def settings_users():
    db = get_db()
    username, pw = f_str("username"), request.form.get("password") or ""
    if not username or len(pw) < 6:
        flash("아이디와 6자 이상의 비밀번호를 입력하세요.", "error")
    elif db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        flash("이미 있는 아이디입니다.", "error")
    else:
        db.execute("INSERT INTO users(username, pw_hash, name, is_admin) VALUES (?, ?, ?, ?)",
                   (username, generate_password_hash(pw), f_str("name") or username, 1 if request.form.get("is_admin") else 0))
        db.commit()
        flash("사용자를 추가했습니다.", "ok")
    return redirect(url_for("settings_page") + "#users")


@app.route("/settings/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def settings_user_delete(user_id):
    if user_id == g.user["id"]:
        flash("자기 자신은 삭제할 수 없습니다.", "error")
    else:
        db = get_db()
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()
        flash("사용자를 삭제했습니다.", "ok")
    return redirect(url_for("settings_page") + "#users")


@app.route("/password", methods=["GET", "POST"])
def password():
    if request.method == "POST":
        db = get_db()
        pw = request.form.get("password") or ""
        if not check_password_hash(g.user["pw_hash"], request.form.get("current") or ""):
            flash("현재 비밀번호가 맞지 않습니다.", "error")
        elif len(pw) < 6 or pw != request.form.get("password2"):
            flash("새 비밀번호는 6자 이상이며 확인과 같아야 합니다.", "error")
        else:
            db.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (generate_password_hash(pw), g.user["id"]))
            db.commit()
            flash("비밀번호를 바꿨습니다.", "ok")
            return redirect(url_for("dashboard"))
    return render_template("password.html")


# ---------------------------------------------------------------- 가져오기

@app.route("/import", methods=["GET", "POST"])
@admin_required
def import_page():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("파일을 선택하세요.", "error")
            return redirect(url_for("import_page"))
        try:
            data = importer.parse(f.filename, f.read())
        except Exception as e:  # 엑셀 형식 오류 등은 사용자에게 그대로 보여준다
            flash(f"파일을 읽지 못했습니다: {e}", "error")
            return redirect(url_for("import_page"))
        token = secrets.token_hex(8)
        db = get_db()
        # 미리보기 자료는 DB에 잠시 보관한다(서버리스 환경에서는 파일을 쓸 수 없음)
        db.execute("DELETE FROM import_jobs WHERE created < ?", ((dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),))
        db.execute("INSERT INTO import_jobs(token, data) VALUES (?, ?)", (token, json.dumps(data["rows"], ensure_ascii=False)))
        db.commit()
        prev_ym = billing.add_months(this_ym(), -1)
        return render_template("import_preview.html", data=data, token=token, carry_ym=prev_ym, carry_date=today())
    return render_template("import.html")


@app.route("/import/commit", methods=["POST"])
@admin_required
def import_commit():
    token = f_str("token")
    if not token.isalnum():
        abort(400)
    db = get_db()
    job = db.execute("SELECT data FROM import_jobs WHERE token = ?", (token,)).fetchone()
    if job is None:
        flash("미리보기 자료가 만료되었습니다. 파일을 다시 올려주세요.", "error")
        return redirect(url_for("import_page"))
    rows = json.loads(job["data"])
    stats = importer.commit(db, rows, f_str("carry_ym") or this_ym(), f_str("carry_date") or today())
    db.execute("DELETE FROM import_jobs WHERE token = ?", (token,))
    db.commit()
    flash(f"가져오기 완료: 빌라 {stats['buildings']}곳 추가, 세대 {stats['units_new']}곳 추가 / {stats['units_updated']}곳 갱신, "
          f"이월 미납 {stats['carry']}건", "ok")
    return redirect(url_for("buildings"))


@app.route("/import/template")
def import_template():
    return send_file(importer.template_xlsx(), as_attachment=True, download_name="세대목록_양식.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- API

@app.route("/api/units")
def api_units():
    db = get_db()
    rows = db.execute("SELECT id, ho, tenant, status FROM units WHERE building_id = ? ORDER BY LENGTH(ho), ho",
                      (request.args.get("building_id", type=int),)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/units/<int:uid>/balance")
def api_balance(uid):
    db = get_db()
    asof = request.args.get("asof") or today()
    bills = [b for b in billing.bill_statuses(db, "b.unit_id = ?", (uid,), asof=asof) if b["owed"] > 0]
    return jsonify({"owed": sum(b["owed"] for b in bills),
                    "bills": [{"ym": b["ym"], "kind": b["kind"], "owed": b["owed"]} for b in bills],
                    "credit": billing.unit_credit(db, uid)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
