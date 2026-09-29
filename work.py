"""작업 메모 - 건물 일·고장신고를 적고, 원가/받은 금액으로 차익을 계산하고, 통합검색한다."""
import datetime as dt

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from db import get_db

bp = Blueprint("work", __name__, url_prefix="/work")

FIELDS = ("work_date", "building", "tenant", "phone", "issue", "memo")


def _money(name):
    v = (request.form.get(name) or "").replace(",", "").strip()
    try:
        return int(float(v)) if v else 0
    except ValueError:
        return 0


def _back(v):
    v = v or ""
    return v if v.startswith("/") and not v.startswith("//") else url_for("work.index")


def search(db, q, status):
    where, params = [], []
    for word in q.split():
        like = f"%{word.lower()}%"
        digits = word.replace("-", "")
        where.append("""(LOWER(COALESCE(building,'')) LIKE ? OR LOWER(COALESCE(tenant,'')) LIKE ?
                        OR REPLACE(COALESCE(phone,''), '-', '') LIKE ? OR LOWER(COALESCE(issue,'')) LIKE ?
                        OR LOWER(COALESCE(memo,'')) LIKE ?)""")
        params += [like, like, f"%{digits}%" if digits else like, like, like]
    if status == "todo":
        where.append("done = 0")
    elif status == "done":
        where.append("done = 1")
    sql = "SELECT * FROM work_orders"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return db.execute(sql + " ORDER BY done, work_date DESC, id DESC", params).fetchall()


@bp.route("/")
def index():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    status = request.args.get("s") or ""
    rows = search(db, q, status)
    total = {"cost": sum(r["cost"] for r in rows), "received": sum(r["received"] for r in rows)}
    total["profit"] = total["received"] - total["cost"]
    todo = db.execute("SELECT COUNT(*) FROM work_orders WHERE done = 0").fetchone()[0]
    return render_template("work.html", rows=rows, q=q, status=status, total=total, todo=todo,
                           blist=_building_names(db), today_s=dt.date.today().isoformat())


def _building_names(db):
    names = {r[0] for r in db.execute("SELECT name FROM buildings WHERE active = 1")}
    names |= {r[0] for r in db.execute("SELECT DISTINCT building FROM work_orders WHERE building <> ''") if r[0]}
    return sorted(names)


def _save(db, wid=None):
    f = {k: (request.form.get(k) or "").strip() for k in FIELDS}
    f["work_date"] = f["work_date"] or dt.date.today().isoformat()
    if not any(f[k] for k in ("building", "tenant", "phone", "issue", "memo")):
        flash("내용을 한 가지 이상 입력하세요.", "error")
        return False
    vals = [f[k] for k in FIELDS] + [_money("cost"), _money("received"), 1 if request.form.get("done") else 0]
    if wid:
        db.execute("""UPDATE work_orders SET work_date=?, building=?, tenant=?, phone=?, issue=?, memo=?,
                      cost=?, received=?, done=? WHERE id=?""", vals + [wid])
    else:
        db.execute("""INSERT INTO work_orders(work_date, building, tenant, phone, issue, memo, cost, received, done)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", vals)
    db.commit()
    return True


@bp.route("/new", methods=["POST"])
def new():
    if _save(get_db()):
        flash("저장했습니다.", "ok")
    return redirect(_back(request.form.get("back")))


@bp.route("/<int:wid>", methods=["GET", "POST"])
def edit(wid):
    db = get_db()
    w = db.execute("SELECT * FROM work_orders WHERE id = ?", (wid,)).fetchone()
    if w is None:
        abort(404)
    if request.method == "POST" and _save(db, wid):
        flash("수정했습니다.", "ok")
        return redirect(_back(request.args.get("back")))
    return render_template("work_edit.html", w=w, blist=_building_names(db), back=_back(request.args.get("back")))


@bp.route("/<int:wid>/toggle", methods=["POST"])
def toggle(wid):
    db = get_db()
    db.execute("UPDATE work_orders SET done = 1 - done WHERE id = ?", (wid,))
    db.commit()
    return redirect(_back(request.form.get("back")))


@bp.route("/<int:wid>/delete", methods=["POST"])
def delete(wid):
    db = get_db()
    db.execute("DELETE FROM work_orders WHERE id = ?", (wid,))
    db.commit()
    flash("삭제했습니다.", "ok")
    return redirect(url_for("work.index"))
