"""관리비 계산, 연체료, 수납 배정 로직."""
import calendar
import datetime as dt

from db import get_settings

UTILS = {"elec": "전기료", "water": "수도료", "gas": "가스료"}
UTIL_UNITS = {"elec": "kWh", "water": "톤", "gas": "㎥"}
METHODS = {
    "none": "부과안함",
    "price": "사용량 × 단가",
    "ratio": "고지금액을 사용량 비율로 배분",
    "equal": "고지금액을 세대 균등 분할",
}


def month_range(ym):
    y, m = map(int, ym.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{ym}-01", f"{ym}-{last:02d}"


def add_months(ym, n):
    y, m = map(int, ym.split("-"))
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def default_due_date(settings, ym):
    target = add_months(ym, int(settings.get("due_offset") or 0))
    y, m = map(int, target.split("-"))
    day = min(int(settings.get("due_day") or 25), calendar.monthrange(y, m)[1])
    return f"{target}-{day:02d}"


def round_amount(value, settings):
    unit = int(settings.get("round_unit") or 1)
    mode = settings.get("round_mode") or "floor"
    q = value / unit
    if mode == "round":
        q = int(q + 0.5) if q >= 0 else -int(-q + 0.5)
    elif mode == "ceil":
        q = -int(-q // 1)
    else:
        q = int(q // 1)
    return int(q * unit)


def occupied_units(conn, building_id, ym):
    start, end = month_range(ym)
    return conn.execute(
        """SELECT * FROM units
           WHERE building_id = ? AND status = '입주'
             AND (move_in IS NULL OR move_in = '' OR move_in <= ?)
             AND (move_out IS NULL OR move_out = '' OR move_out >= ?)
           ORDER BY ho""",
        (building_id, end, start),
    ).fetchall()


def previous_reading(conn, unit_id, util, ym):
    """ym 이전 가장 최근 검침의 당월 지침."""
    row = conn.execute(
        "SELECT curr FROM readings WHERE unit_id = ? AND util = ? AND ym < ? AND curr IS NOT NULL ORDER BY ym DESC LIMIT 1",
        (unit_id, util, ym),
    ).fetchone()
    return row["curr"] if row else None


def fmt_num(v):
    if v is None:
        return ""
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def calculate_building(conn, building_id, ym, due_date, issued):
    """빌라 한 곳의 ym월 관리비를 계산해 고지 내역을 새로 만든다. 결과 요약을 돌려준다."""
    settings = get_settings(conn)
    b = conn.execute("SELECT * FROM buildings WHERE id = ?", (building_id,)).fetchone()
    units = occupied_units(conn, building_id, ym)
    n = len(units)
    result = {"building": b["name"], "units": n, "created": 0, "total": 0, "warnings": [], "had_payments": []}
    if n == 0:
        result["warnings"].append("입주 세대가 없습니다.")
        return result

    fees = conn.execute(
        """SELECT bf.*, fi.name FROM building_fees bf JOIN fee_items fi ON fi.id = bf.item_id
           WHERE bf.building_id = ? AND fi.active = 1 ORDER BY fi.sort, fi.id""",
        (building_id,),
    ).fetchall()
    overrides = {}
    for r in conn.execute(
        "SELECT uf.* FROM unit_fees uf JOIN units u ON u.id = uf.unit_id WHERE u.building_id = ?", (building_id,)
    ):
        overrides[(r["unit_id"], r["item_id"])] = r["amount"]

    bbills = {r["util"]: r for r in conn.execute(
        "SELECT * FROM building_bills WHERE building_id = ? AND ym = ?", (building_id, ym))}

    # 세대별 사용량
    usage = {}
    for util in UTILS:
        if b[f"{util}_method"] in ("price", "ratio"):
            for u in units:
                r = conn.execute(
                    "SELECT prev, curr FROM readings WHERE unit_id = ? AND ym = ? AND util = ?", (u["id"], ym, util)
                ).fetchone()
                prev = r["prev"] if r and r["prev"] is not None else previous_reading(conn, u["id"], util, ym)
                curr = r["curr"] if r else None
                if curr is None or prev is None:
                    result["warnings"].append(f"{u['ho']}호 {UTILS[util]} 검침값이 없어 사용량 0으로 계산")
                    usage[(u["id"], util)] = (prev, curr, 0)
                else:
                    used = curr - prev
                    if used < 0:
                        result["warnings"].append(f"{u['ho']}호 {UTILS[util]} 당월지침이 전월보다 작아 사용량 0으로 계산")
                        used = 0
                    usage[(u["id"], util)] = (prev, curr, used)

    for u in units:
        lines = []
        for f in fees:
            if f["method"] == "split":
                amt = f["amount"] / n
                detail = f"총 {f['amount']:,}원 ÷ {n}세대"
            else:
                amt = overrides.get((u["id"], f["item_id"]), f["amount"])
                detail = ""
            if amt:
                lines.append((f["name"], amt, detail))

        for util, label in UTILS.items():
            method = b[f"{util}_method"]
            bb = bbills.get(util)
            if method == "none":
                continue
            if method == "equal":
                if bb and bb["amount"]:
                    lines.append((label, bb["amount"] / n, f"총 {bb['amount']:,}원 ÷ {n}세대"))
                continue
            prev, curr, used = usage[(u["id"], util)]
            meter = f"전월 {fmt_num(prev)} → 당월 {fmt_num(curr)} / 사용 {fmt_num(used)}{UTIL_UNITS[util]}"
            if method == "price":
                price = bb["unit_price"] if bb else 0
                if not price:
                    result["warnings"].append(f"{label} 단가가 입력되지 않았습니다")
                lines.append((label, used * price, f"{meter} × {fmt_num(price)}원"))
            elif method == "ratio":
                total_amt = bb["amount"] if bb else 0
                total_used = sum(usage[(x["id"], util)][2] for x in units)
                if not total_amt:
                    result["warnings"].append(f"{label} 빌라 고지금액이 입력되지 않았습니다")
                share = used / total_used if total_used else 1 / n
                lines.append((label, total_amt * share, meter))

        if b["common_method"] == "equal":
            bb = bbills.get("common")
            if bb and bb["amount"]:
                lines.append(("공용전기", bb["amount"] / n, f"총 {bb['amount']:,}원 ÷ {n}세대"))

        if b["bill_rent"] and u["rent"]:
            lines.append(("임대료", u["rent"], ""))

        lines = [(name, round_amount(amt, settings), detail) for name, amt, detail in lines]
        total = sum(a for _, a, _ in lines)
        late_fee = round_amount(total * float(settings.get("late_rate") or 0) / 100, settings)

        old = conn.execute(
            "SELECT id FROM bills WHERE unit_id = ? AND ym = ? AND kind = 'normal'", (u["id"], ym)
        ).fetchone()
        if old:
            if conn.execute("SELECT 1 FROM allocations WHERE bill_id = ?", (old["id"],)).fetchone():
                result["had_payments"].append(u["ho"])
            conn.execute("DELETE FROM bills WHERE id = ?", (old["id"],))
        bill_id = conn.execute(
            "INSERT INTO bills(unit_id, ym, kind, amount, late_fee, due_date, issued) VALUES (?, ?, 'normal', ?, ?, ?, ?) RETURNING id",
            (u["id"], ym, total, late_fee, due_date, issued),
        ).fetchone()[0]
        for i, (name, amt, detail) in enumerate(lines):
            conn.execute(
                "INSERT INTO bill_lines(bill_id, name, amount, detail, sort) VALUES (?, ?, ?, ?, ?)",
                (bill_id, name, amt, detail, i),
            )
        reallocate_unit(conn, u["id"])
        result["created"] += 1
        result["total"] += total
    # 같은 경고가 세대마다 반복되지 않도록 정리
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result


def reallocate_unit(conn, unit_id):
    """세대의 모든 수납액을 오래된 고지부터 다시 배정한다(납부기한 후 수납 시 연체료 포함)."""
    conn.execute(
        "DELETE FROM allocations WHERE payment_id IN (SELECT id FROM payments WHERE unit_id = ?)", (unit_id,)
    )
    bills = [dict(r) for r in conn.execute(
        "SELECT id, ym, amount, late_fee, due_date FROM bills WHERE unit_id = ? ORDER BY ym, CASE kind WHEN 'carry' THEN 0 ELSE 1 END, id", (unit_id,))]
    for bl in bills:
        bl["paid"] = 0
        bl["paid_by_due"] = 0
    payments = conn.execute(
        "SELECT id, pay_date, amount FROM payments WHERE unit_id = ? ORDER BY pay_date, id", (unit_id,)
    ).fetchall()
    for p in payments:
        left = p["amount"]
        for bl in bills:
            if left <= 0:
                break
            owed = _owed(bl, p["pay_date"])
            if owed <= 0:
                continue
            take = min(owed, left)
            conn.execute("INSERT INTO allocations(payment_id, bill_id, amount) VALUES (?, ?, ?)", (p["id"], bl["id"], take))
            bl["paid"] += take
            if p["pay_date"] <= bl["due_date"]:
                bl["paid_by_due"] += take
            left -= take


def _owed(bl, asof):
    late = bl["late_fee"] if (asof > bl["due_date"] and bl["paid_by_due"] < bl["amount"]) else 0
    return bl["amount"] + late - bl["paid"]


BILL_STATUS_SQL = """
SELECT b.*, u.ho, u.tenant, u.mobile, u.phone, u.building_id, bd.name AS building_name,
       CAST(COALESCE(SUM(a.amount), 0) AS BIGINT) AS paid,
       CAST(COALESCE(SUM(CASE WHEN p.pay_date <= b.due_date THEN a.amount END), 0) AS BIGINT) AS paid_by_due
FROM bills b
JOIN units u ON u.id = b.unit_id
JOIN buildings bd ON bd.id = u.building_id
LEFT JOIN allocations a ON a.bill_id = b.id
LEFT JOIN payments p ON p.id = a.payment_id
"""


def bill_statuses(conn, where="1=1", params=(), asof=None):
    """고지 건별 수납/미납 현황. asof(기준일) 이후 납부기한이 지난 건은 연체료를 더한다."""
    asof = asof or dt.date.today().isoformat()
    rows = conn.execute(
        BILL_STATUS_SQL + f" WHERE {where} GROUP BY b.id, u.id, bd.id ORDER BY bd.name, u.ho, b.ym", params
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["late_applied"] = d["late_fee"] if (asof > d["due_date"] and d["paid_by_due"] < d["amount"]) else 0
        d["owed"] = d["amount"] + d["late_applied"] - d["paid"]
        out.append(d)
    return out


def unit_credit(conn, unit_id):
    """배정되지 않은 수납액(선납금)."""
    paid = conn.execute("SELECT CAST(COALESCE(SUM(amount), 0) AS BIGINT) FROM payments WHERE unit_id = ?", (unit_id,)).fetchone()[0]
    alloc = conn.execute(
        "SELECT CAST(COALESCE(SUM(a.amount), 0) AS BIGINT) FROM allocations a JOIN payments p ON p.id = a.payment_id WHERE p.unit_id = ?",
        (unit_id,),
    ).fetchone()[0]
    return paid - alloc
