"""관리 스케줄 - 청소·검침·점검·계약 만료 같은 빌라 관리 일정을 휴대폰에서 보기 좋은 화면으로 관리한다."""
import calendar
import datetime as dt

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from db import get_db

bp = Blueprint("schedule", __name__, url_prefix="/schedule")

# 분류: (이모지, 배경색, 글자색)
CATEGORIES = {
    "청소": ("🧹", "#FFE8A3", "#8A5A00"),
    "검침": ("📟", "#D9ECFF", "#1F5FA8"),
    "점검": ("🔧", "#FFD9CC", "#B8431F"),
    "정화조": ("🚽", "#E3F4D7", "#3F7A1E"),
    "계약·입퇴실": ("🏠", "#F1E2FF", "#6B3FA0"),
    "수납·고지": ("💰", "#FFF3C4", "#9A6B00"),
    "기타": ("📌", "#ECECEC", "#555555"),
}
REPEATS = {"none": "반복 안 함", "weekly": "매주", "monthly": "매월", "yearly": "매년"}
WEEKDAYS = "월화수목금토일"


def _date(s, default=None):
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return default


def _add_months(d, n, day):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return dt.date(y, m, min(day, calendar.monthrange(y, m)[1]))


def occurrences(s, d1, d2):
    """일정 s 가 d1~d2 사이에 돌아오는 날짜들."""
    start = _date(s["start_date"])
    if start is None:
        return []
    until = min(d2, _date(s["repeat_until"], d2))
    rep = s["repeat"]
    if rep == "none":
        return [start] if d1 <= start <= until else []
    out = []
    if rep == "weekly":
        d = start if start >= d1 else start + dt.timedelta(days=-((start - d1).days // 7) * 7)
        while d <= until:
            if d >= d1:
                out.append(d)
            d += dt.timedelta(days=7)
        return out
    step = 1 if rep == "monthly" else 12
    n = 0
    if rep == "monthly" and start < d1:
        n = max(0, (d1.year - start.year) * 12 + d1.month - start.month - 1)
    elif rep == "yearly" and start < d1:
        n = max(0, (d1.year - start.year - 1) * 12)
    while True:
        d = _add_months(start, n, start.day)
        if d > until:
            return out
        if d >= d1:
            out.append(d)
        n += step


def events(db, d1, d2, category=None):
    """d1~d2 사이의 모든 일정(반복 전개 + 세대 퇴실 예정일)을 날짜순으로."""
    rows = db.execute(
        """SELECT s.*, b.name AS building_name FROM schedules s LEFT JOIN buildings b ON b.id = s.building_id
           WHERE s.start_date <= ? AND (s.repeat_until IS NULL OR s.repeat_until = '' OR s.repeat_until >= ?)""",
        (d2.isoformat(), d1.isoformat())).fetchall()
    done = {(r["schedule_id"], r["date"]) for r in db.execute(
        "SELECT schedule_id, date FROM schedule_done WHERE date BETWEEN ? AND ?", (d1.isoformat(), d2.isoformat()))}
    out = []
    for s in rows:
        for d in occurrences(s, d1, d2):
            out.append({"id": s["id"], "title": s["title"], "category": s["category"], "date": d,
                        "time": s["time"] or "", "building_name": s["building_name"] or "", "memo": s["memo"] or "",
                        "repeat": s["repeat"], "done": (s["id"], d.isoformat()) in done, "auto": False})
    # 세대 퇴실(계약 만료) 예정일은 따로 입력하지 않아도 자동으로 보여준다
    for u in db.execute(
            """SELECT u.id, u.ho, u.tenant, u.move_out, b.name AS building_name FROM units u
               JOIN buildings b ON b.id = u.building_id
               WHERE b.active = 1 AND u.move_out BETWEEN ? AND ?""", (d1.isoformat(), d2.isoformat())):
        d = _date(u["move_out"])
        if d:
            out.append({"id": u["id"], "title": f"{u['ho']}호 {u['tenant'] or ''} 퇴실 예정".replace("  ", " "),
                        "category": "계약·입퇴실", "date": d, "time": "", "building_name": u["building_name"],
                        "memo": "", "repeat": "none", "done": d < dt.date.today(), "auto": True})
    if category:
        out = [e for e in out if e["category"] == category]
    out.sort(key=lambda e: (e["date"], e["done"], e["time"] or "99", e["title"]))
    return out


@bp.app_template_filter("md")
def md(d):
    return f"{d.month}.{d.day}({WEEKDAYS[d.weekday()]})"


@bp.app_context_processor
def inject():
    return {"CATS": CATEGORIES, "REPEATS": REPEATS, "WEEKDAYS": WEEKDAYS, "safe_back": _safe_back}


def _safe_back(back):
    back = back or ""
    return back if back.startswith("/") and not back.startswith("//") else url_for("schedule.home")


def _month_arg():
    t = dt.date.today()
    try:
        y, m = map(int, (request.args.get("ym") or "").split("-"))
        return dt.date(y, m, 1)
    except ValueError:
        return t.replace(day=1)


@bp.route("/")
def home():
    db = get_db()
    t = dt.date.today()
    week1 = t - dt.timedelta(days=t.weekday())
    week = events(db, week1, week1 + dt.timedelta(days=6))
    upcoming = events(db, t, t + dt.timedelta(days=30))
    today_list = [e for e in upcoming if e["date"] == t]
    soon = [e for e in upcoming if e["date"] > t][:12]
    overdue = [e for e in events(db, t - dt.timedelta(days=30), t - dt.timedelta(days=1)) if not e["done"]]
    m1 = _month_arg()
    m2 = _add_months(m1, 1, 1) - dt.timedelta(days=1)
    month = events(db, m1, m2)
    counts = {c: sum(1 for e in month if e["category"] == c) for c in CATEGORIES}
    return render_template("m_home.html", t=t, today_list=today_list, soon=soon, overdue=overdue,
                           week_done=sum(e["done"] for e in week), week_total=len(week),
                           m1=m1, prev_ym=_add_months(m1, -1, 1).strftime("%Y-%m"),
                           next_ym=_add_months(m1, 1, 1).strftime("%Y-%m"), month=month, counts=counts)


@bp.route("/calendar")
def cal():
    db = get_db()
    m1 = _month_arg()
    m2 = _add_months(m1, 1, 1) - dt.timedelta(days=1)
    evs = events(db, m1, m2)
    by_day = {}
    for e in evs:
        by_day.setdefault(e["date"], []).append(e)
    sel = _date(request.args.get("d"), dt.date.today() if m1.month == dt.date.today().month
                and m1.year == dt.date.today().year else m1)
    weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(m1.year, m1.month)
    return render_template("m_calendar.html", m1=m1, weeks=weeks, by_day=by_day, sel=sel,
                           sel_list=by_day.get(sel, []), t=dt.date.today(),
                           prev_ym=_add_months(m1, -1, 1).strftime("%Y-%m"),
                           next_ym=_add_months(m1, 1, 1).strftime("%Y-%m"))


@bp.route("/list")
def all_list():
    db = get_db()
    cat = request.args.get("cat") or ""
    rows = db.execute(
        """SELECT s.*, b.name AS building_name FROM schedules s LEFT JOIN buildings b ON b.id = s.building_id
           ORDER BY s.start_date DESC, s.id DESC""").fetchall()
    if cat:
        rows = [r for r in rows if r["category"] == cat]
    return render_template("m_list.html", rows=rows, cat=cat)


@bp.route("/new", methods=["GET", "POST"])
@bp.route("/<int:sid>", methods=["GET", "POST"])
def edit(sid=None):
    db = get_db()
    s = None
    if sid:
        s = db.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
        if s is None:
            abort(404)
    if request.method == "POST":
        f = {k: (request.form.get(k) or "").strip() for k in
             ("title", "category", "building_id", "start_date", "time", "repeat", "repeat_until", "memo")}
        if not f["title"] or not _date(f["start_date"]):
            flash("제목과 날짜를 입력하세요.", "error")
        else:
            vals = (f["title"], f["category"] if f["category"] in CATEGORIES else "기타",
                    int(f["building_id"]) if f["building_id"].isdigit() else None, f["start_date"], f["time"] or None,
                    f["repeat"] if f["repeat"] in REPEATS else "none",
                    f["repeat_until"] if _date(f["repeat_until"]) else None, f["memo"])
            if s:
                db.execute("""UPDATE schedules SET title=?, category=?, building_id=?, start_date=?, time=?, repeat=?,
                              repeat_until=?, memo=? WHERE id=?""", vals + (sid,))
            else:
                db.execute("""INSERT INTO schedules(title, category, building_id, start_date, time, repeat,
                              repeat_until, memo) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", vals)
            db.commit()
            flash("일정을 저장했어요.", "ok")
            return redirect(_safe_back(request.args.get("back")))
    blist = db.execute("SELECT id, name FROM buildings WHERE active = 1 ORDER BY name").fetchall()
    return render_template("m_edit.html", s=s, blist=blist,
                           d=request.args.get("d") or dt.date.today().isoformat(),
                           cat=request.args.get("cat") or "")


@bp.route("/<int:sid>/delete", methods=["POST"])
def delete(sid):
    db = get_db()
    db.execute("DELETE FROM schedule_done WHERE schedule_id = ?", (sid,))
    db.execute("DELETE FROM schedules WHERE id = ?", (sid,))
    db.commit()
    flash("일정을 삭제했어요.", "ok")
    return redirect(url_for("schedule.home"))


@bp.route("/<int:sid>/toggle", methods=["POST"])
def toggle(sid):
    db = get_db()
    d = _date(request.form.get("date"))
    if d is None:
        abort(400)
    key = (sid, d.isoformat())
    if db.execute("SELECT 1 FROM schedule_done WHERE schedule_id = ? AND date = ?", key).fetchone():
        db.execute("DELETE FROM schedule_done WHERE schedule_id = ? AND date = ?", key)
    else:
        db.execute("INSERT INTO schedule_done(schedule_id, date) VALUES (?, ?)", key)
    db.commit()
    return redirect(_safe_back(request.form.get("back")))
