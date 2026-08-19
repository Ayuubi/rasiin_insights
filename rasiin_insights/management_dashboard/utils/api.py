"""
Dashboard data API.

Path: rasiin_insights/management_dashboard/utils/api.py

One endpoint set, serving every visual on the dashboard page. Reads the
snapshot only — a twelve-month view is a few thousand rows, so it returns in
milliseconds rather than scanning two million facts.

PERIOD ROLL-UP
    Snapshots are stored monthly. Quarter, half-year and year are produced by
    combining months here, which is why no extra tables are needed for them.

    Flows are summed. Balances are NOT: a quarter's closing receivable is the
    LAST month's closing, and its opening is the FIRST month's opening. Summing
    three closing balances would produce a number three times too large — an
    easy and very visible mistake.

SELF-EXPLANATION
    Every figure the page shows can be asked to explain itself. get_definitions
    returns the metric registry, so the dashboard can show what a number means,
    where it comes from and its caveat, without anyone having to remember.
"""

import frappe
from frappe.utils import flt, getdate, add_months, add_days

from rasiin_insights.management_dashboard.utils import controls

# Balances are point-in-time. Everything else accumulates over the period.
BALANCE_METRICS = {"ar_opening", "ar_closing", "ap_opening", "ap_closing"}
OPENING_METRICS = {"ar_opening", "ap_opening"}

# Flow metrics only. Balances have no daily equivalent without walking the
# ledger day by day, which is a different, heavier build than this — refuse
# them here rather than return a number that looks like a daily balance and
# isn't one.
DAILY_METRICS = {
    "gross_sales", "discount", "return", "return_discount", "revenue_reclass",
    "collection_current", "collection_prior", "collection_unallocated",
    "payment_discount", "commission", "payroll", "expense", "refund",
    "ar_transfer_in", "ar_transfer_out", "payable_charged", "supplier_payment",
}

GRANULARITIES = {
    "Monthly": 1,
    "Quarterly": 3,
    "Half-yearly": 6,
    "Yearly": 12,
}


def month_list(from_period, to_period):
    """['2026-01', '2026-02', ...] inclusive."""
    out = []
    cursor = getdate(from_period + "-01")
    last = getdate(to_period + "-01")
    while cursor <= last:
        out.append("{0}-{1:02d}".format(cursor.year, cursor.month))
        cursor = add_months(cursor, 1)
    return out


def bucket_label(period, granularity):
    """Which column a month belongs to, and what to call it."""
    year, month = period.split("-")
    month = int(month)
    if granularity == "Monthly":
        return period
    if granularity == "Quarterly":
        return "{0}-Q{1}".format(year, (month - 1) // 3 + 1)
    if granularity == "Half-yearly":
        return "{0}-H{1}".format(year, 1 if month <= 6 else 2)
    return year


def _snapshot_rows(months, company, dimension_type="Total"):
    if not months:
        return []
    conditions = ["period IN %(months)s", "dimension_type = %(dt)s"]
    values = {"months": months, "dt": dimension_type}
    if company:
        conditions.append("company = %(c)s")
        values["c"] = company
    return frappe.db.sql("""
        SELECT period, company, metric, dimension_value, amount, quality_score
        FROM `tabManagement Snapshot`
        WHERE {0}
    """.format(" AND ".join(conditions)), values, as_dict=True)


def _roll_up(rows, months, granularity):
    """
    Collapse monthly rows into buckets.

    Flows add. Closing balances take the last month in the bucket, openings
    take the first — the only correct way to combine a point-in-time figure.
    """
    order = {m: i for i, m in enumerate(months)}
    buckets = {}

    for r in rows:
        label = bucket_label(r.period, granularity)
        b = buckets.setdefault(label, {"metrics": {}, "quality": [],
                                       "first": None, "last": None})
        idx = order.get(r.period, 0)
        if b["first"] is None or idx < b["first"]:
            b["first"] = idx
        if b["last"] is None or idx > b["last"]:
            b["last"] = idx

    for r in rows:
        label = bucket_label(r.period, granularity)
        b = buckets[label]
        idx = order.get(r.period, 0)

        if r.metric in BALANCE_METRICS:
            wanted = b["first"] if r.metric in OPENING_METRICS else b["last"]
            if idx != wanted:
                continue
            b["metrics"][r.metric] = b["metrics"].get(r.metric, 0.0) + flt(r.amount)
        else:
            b["metrics"][r.metric] = b["metrics"].get(r.metric, 0.0) + flt(r.amount)

        if r.quality_score is not None:
            b["quality"].append(flt(r.quality_score))

    ordered = sorted(buckets.items(), key=lambda x: x[1]["first"])
    return [(label, b) for label, b in ordered]


def _derive(m):
    """The derived figures, in one place so every visual agrees."""
    g = lambda k: flt(m.get(k, 0.0))
    net_sales = (g("gross_sales") - g("discount") - g("return")
                 + g("return_discount") - g("revenue_reclass"))
    collections = (g("collection_current") + g("collection_prior")
                   + g("collection_unallocated"))
    total_expense = g("commission") + g("payroll") + g("expense")
    money_out = total_expense + g("refund")
    return {
        "gross_sales": g("gross_sales"),
        "discount": g("discount"),
        "return": g("return"),
        "return_discount": g("return_discount"),
        "revenue_reclass": g("revenue_reclass"),
        "net_sales": net_sales,
        "collection_current": g("collection_current"),
        "collection_prior": g("collection_prior"),
        "collection_unallocated": g("collection_unallocated"),
        "total_collections": collections,
        "payment_discount": g("payment_discount"),
        "commission": g("commission"),
        "payroll": g("payroll"),
        "expense": g("expense"),
        "total_expense": total_expense,
        "refund": g("refund"),
        "money_out": money_out,
        "ar_opening": g("ar_opening"),
        "ar_closing": g("ar_closing"),
        "ap_opening": g("ap_opening"),
        "ap_closing": g("ap_closing"),
        "discount_pct": (g("discount") / g("gross_sales")
                         if g("gross_sales") else 0.0),
        "return_pct": (g("return") / g("gross_sales")
                       if g("gross_sales") else 0.0),
        "collection_efficiency": (collections / net_sales if net_sales else 0.0),
        "net_cash": collections - money_out,
    }


@frappe.whitelist()
def get_summary(from_period, to_period, granularity="Monthly", company=None):
    """
    Headline figures per period bucket. This drives the KPI cards and every
    trend line on the page.
    """
    months = month_list(from_period, to_period)
    rows = _snapshot_rows(months, company)
    buckets = _roll_up(rows, months, granularity)

    periods = []
    for label, b in buckets:
        figures = _derive(b["metrics"])
        figures["period"] = label
        figures["quality_score"] = (round(sum(b["quality"]) / len(b["quality"]), 2)
                                    if b["quality"] else 0.0)
        periods.append(figures)

    # The range total. Flows add across every bucket; balances do not — the
    # range's closing AR is the LAST bucket's closing, and its opening is the
    # FIRST bucket's opening. Summing eight closing balances would give a
    # number eight times too large.
    total_metrics = {}
    if buckets:
        first_bucket = min(buckets, key=lambda x: x[1]["first"])[1]
        last_bucket = max(buckets, key=lambda x: x[1]["last"])[1]

        for _, b in buckets:
            for metric, amount in b["metrics"].items():
                if metric in BALANCE_METRICS:
                    continue
                total_metrics[metric] = total_metrics.get(metric, 0.0) + flt(amount)

        for metric in BALANCE_METRICS:
            source = first_bucket if metric in OPENING_METRICS else last_bucket
            if metric in source["metrics"]:
                total_metrics[metric] = flt(source["metrics"][metric])

    total = _derive(total_metrics)

    return {
        "periods": periods,
        "granularity": granularity,
        "company": company,
        "from_period": from_period,
        "to_period": to_period,
        "total": total,
    }


@frappe.whitelist()
def get_dimension(metric, dimension_type, from_period, to_period,
                  company=None, granularity="Monthly", limit=25):
    """
    One metric, broken down by one dimension. Drives every drill-down chart
    and the drill-down table.
    """
    months = month_list(from_period, to_period)
    if not months:
        return {"rows": [], "total": 0.0}

    # A metric may be several metrics: "Money received" is three collection
    # buckets added together. Without this the drill-down showed 905,347
    # against a card reading 1,420,987 — the same word meaning two things.
    metrics = [m.strip() for m in str(metric).split(",") if m.strip()]
    conditions = ["period IN %(months)s", "dimension_type = %(dt)s",
                  "metric IN %(m)s"]
    values = {"months": months, "dt": dimension_type, "m": metrics}
    if company:
        conditions.append("company = %(c)s")
        values["c"] = company

    rows = frappe.db.sql("""
        SELECT dimension_value AS label, SUM(amount) AS amount
        FROM `tabManagement Snapshot`
        WHERE {0}
        GROUP BY dimension_value
        ORDER BY SUM(amount) DESC
    """.format(" AND ".join(conditions)), values, as_dict=True)

    total = sum(flt(r.amount) for r in rows)
    out = []
    for r in rows[:int(limit)]:
        out.append({
            "label": r.label,
            "amount": flt(r.amount),
            "share": flt(r.amount) / total if total else 0.0,
        })
    return {"rows": out, "total": total, "metric": metric,
            "dimension_type": dimension_type}


@frappe.whitelist()
def get_dimension_trend(metric, dimension_type, from_period, to_period,
                        company=None, granularity="Monthly", top=6):
    """
    One metric, one dimension, across time — a stacked trend rather than a
    single-period snapshot. Answers "is this changing?", which is usually the
    CEO's real question.
    """
    months = month_list(from_period, to_period)
    if not months:
        return {"periods": [], "series": []}

    # A metric may be several metrics: "Money received" is three collection
    # buckets added together. Without this the drill-down showed 905,347
    # against a card reading 1,420,987 — the same word meaning two things.
    metrics = [m.strip() for m in str(metric).split(",") if m.strip()]
    conditions = ["period IN %(months)s", "dimension_type = %(dt)s",
                  "metric IN %(m)s"]
    values = {"months": months, "dt": dimension_type, "m": metrics}
    if company:
        conditions.append("company = %(c)s")
        values["c"] = company

    rows = frappe.db.sql("""
        SELECT period, dimension_value AS label, SUM(amount) AS amount
        FROM `tabManagement Snapshot`
        WHERE {0}
        GROUP BY period, dimension_value
    """.format(" AND ".join(conditions)), values, as_dict=True)

    totals = {}
    for r in rows:
        totals[r.label] = totals.get(r.label, 0.0) + flt(r.amount)
    keep = [k for k, _ in sorted(totals.items(), key=lambda x: -x[1])[:int(top)]]

    labels, seen = [], set()
    for m in months:
        label = bucket_label(m, granularity)
        if label not in seen:
            seen.add(label)
            labels.append(label)

    series = {k: {l: 0.0 for l in labels} for k in keep}
    other = {l: 0.0 for l in labels}
    for r in rows:
        label = bucket_label(r.period, granularity)
        if r.label in series:
            series[r.label][label] += flt(r.amount)
        else:
            other[label] += flt(r.amount)

    out = [{"name": k, "values": [series[k][l] for l in labels]} for k in keep]
    if any(other.values()):
        out.append({"name": "Everything else",
                    "values": [other[l] for l in labels]})

    return {"periods": labels, "series": out, "metric": metric,
            "dimension_type": dimension_type}


@frappe.whitelist()
def get_daily_trend(from_date, to_date, metric, company=None):
    """
    Day-level trend for one or more flow metrics, read live from Management
    Fact — the snapshot has no day grain by design (see snapshot.py), so
    this bypasses it on purpose, the same way cross-dimension one-off
    questions already fall through to the fact table live.

    Every calendar day in range is returned, zero-filled, so a line chart
    never silently skips a day with no activity.
    """
    metrics = [m.strip() for m in str(metric).split(",") if m.strip()]
    bad = [m for m in metrics if m not in DAILY_METRICS]
    if bad:
        frappe.throw("Not a daily metric: {0}".format(", ".join(bad)))

    conditions = ["posting_date BETWEEN %(from)s AND %(to)s", "metric IN %(m)s"]
    values = {"from": from_date, "to": to_date, "m": metrics}
    if company:
        conditions.append("company = %(c)s")
        values["c"] = company

    rows = frappe.db.sql("""
        SELECT posting_date, SUM(amount) AS amount
        FROM `tabManagement Fact`
        WHERE {0}
        GROUP BY posting_date
    """.format(" AND ".join(conditions)), values, as_dict=True)

    by_date = {str(r.posting_date): flt(r.amount) for r in rows}
    cursor, end = getdate(from_date), getdate(to_date)
    dates, values_out = [], []
    while cursor <= end:
        key = str(cursor)
        dates.append(key)
        values_out.append(by_date.get(key, 0.0))
        cursor = add_days(cursor, 1)

    return {"dates": dates, "values": values_out, "metric": metric,
            "total": sum(values_out)}


@frappe.whitelist()
def get_daily_summary(from_date, to_date, company=None):
    """
    The same story as get_summary — gross sales, less discount, less
    returns, net sales, money in, money out, ratios — one row per day
    instead of one row per month bucket. Read live from Management Fact,
    same reasoning as get_daily_trend.

    Receivables/payables are left out on purpose: ar_closing/ap_closing are
    point-in-time balances built from the monthly snapshot's carry-forward
    opening. There is no daily equivalent without walking the ledger day by
    day, so those keys come back 0 here and the caller should not render
    that group for the daily table.
    """
    conditions = ["posting_date BETWEEN %(from)s AND %(to)s", "metric IN %(m)s"]
    values = {"from": from_date, "to": to_date, "m": list(DAILY_METRICS)}
    if company:
        conditions.append("company = %(c)s")
        values["c"] = company
    where = " AND ".join(conditions)

    rows = frappe.db.sql("""
        SELECT posting_date, metric, SUM(amount) AS amount
        FROM `tabManagement Fact`
        WHERE {0}
        GROUP BY posting_date, metric
    """.format(where), values, as_dict=True)

    by_day = {}
    for r in rows:
        by_day.setdefault(str(r.posting_date), {})[r.metric] = flt(r.amount)

    quality_rows = frappe.db.sql("""
        SELECT posting_date,
               COALESCE(SUM(CASE WHEN item_group = 'Unallocated' THEN amount END), 0) AS untraced,
               COALESCE(SUM(amount), 0) AS total
        FROM `tabManagement Fact`
        WHERE {0}
          AND metric IN ('collection_current', 'collection_prior', 'collection_unallocated')
        GROUP BY posting_date
    """.format(where), values, as_dict=True)
    quality_by_day = {str(r.posting_date):
        (round(100.0 * (1 - flt(r.untraced) / flt(r.total)), 2) if flt(r.total) else 0.0)
        for r in quality_rows}

    cursor, end = getdate(from_date), getdate(to_date)
    days = []
    all_metrics = {}
    while cursor <= end:
        key = str(cursor)
        day_metrics = by_day.get(key, {})
        for m, a in day_metrics.items():
            all_metrics[m] = all_metrics.get(m, 0.0) + a
        figures = _derive(day_metrics)
        figures["date"] = key
        figures["quality_score"] = quality_by_day.get(key, 0.0)
        days.append(figures)
        cursor = add_days(cursor, 1)

    total = _derive(all_metrics)
    total_untraced = sum(flt(r.untraced) for r in quality_rows)
    total_all = sum(flt(r.total) for r in quality_rows)
    total["quality_score"] = (round(100.0 * (1 - total_untraced / total_all), 2)
                               if total_all else 0.0)

    return {"days": days, "total": total}


@frappe.whitelist()
def get_definitions():
    """
    The metric registry, for the 'what is this?' popover on every figure.
    Read straight from controls.METRICS so it can never drift from the code
    that produces the numbers.
    """
    return {"metrics": controls.METRICS, "derived": controls.DERIVED}


@frappe.whitelist()
def get_filters():
    """Everything the filter bar needs to populate itself."""
    periods = [r.period for r in frappe.db.sql("""
        SELECT DISTINCT period FROM `tabManagement Snapshot` ORDER BY period
    """, as_dict=True)]
    dimensions = [r.dimension_type for r in frappe.db.sql("""
        SELECT DISTINCT dimension_type FROM `tabManagement Snapshot`
        WHERE dimension_type != 'Total' ORDER BY dimension_type
    """, as_dict=True)]
    metrics = [r.metric for r in frappe.db.sql("""
        SELECT DISTINCT metric FROM `tabManagement Snapshot` ORDER BY metric
    """, as_dict=True)]
    # Order by fact volume, biggest first, so the dashboard opens on the company
    # that matters. Alphabetical order put Shaafi Diagnostic Center — four
    # snapshot rows and no invoices — in front of the actual hospital.
    by_size = frappe.db.sql("""
        SELECT company, COUNT(*) AS n FROM `tabManagement Fact`
        GROUP BY company ORDER BY n DESC
    """, as_dict=True)
    ranked = [r.company for r in by_size if r.company]
    for c in frappe.get_all("Company", pluck="name", order_by="name"):
        if c not in ranked:
            ranked.append(c)

    default_company = frappe.db.get_single_value(
        "Rasiin Insights Settings", "md_default_company") or (
        ranked[0] if ranked else None)

    return {
        "periods": periods,
        "companies": ranked,
        "default_company": default_company,
        "dimensions": dimensions,
        "metrics": metrics,
        "granularities": list(GRANULARITIES.keys()),
    }


@frappe.whitelist()
def get_health(from_period, to_period, company=None):
    """
    Data-quality banner. A dashboard that hides its own weak spots is how the
    CEO stops trusting it — so the page states plainly how much of each period
    could be traced, and whether the facts still agree with the ledger.
    """
    months = month_list(from_period, to_period)
    rows = _snapshot_rows(months, company)

    by_period = {}
    for r in rows:
        if r.quality_score is None:
            continue
        by_period.setdefault(r.period, set()).add(flt(r.quality_score))

    out = []
    for period in months:
        scores = by_period.get(period)
        out.append({
            "period": period,
            "quality_score": round(max(scores), 2) if scores else 0.0,
        })

    worst = min((p["quality_score"] for p in out), default=0.0)
    return {"periods": out, "worst": worst,
            "message": ("Collections are fully traceable in this range."
                        if worst >= 95 else
                        "Some periods have collections that could not be traced "
                        "to a service. They are shown as Unallocated, never "
                        "spread across services.")}


def self_test(from_period="2026-01", to_period="2026-08"):
    """
    Prove the roll-up. Read-only.

        bench --site shaafi execute \
          rasiin_insights.management_dashboard.utils.api.self_test
    """
    company = "Shaafi Hospital"

    monthly = get_summary(from_period, to_period, "Monthly", company)
    print("\nMONTHLY")
    print("{0:<12}{1:>16}{2:>16}{3:>14}{4:>10}".format(
        "period", "net sales", "collections", "closing AR", "disc %"))
    for p in monthly["periods"]:
        print("{0:<12}{1:>16,.2f}{2:>16,.2f}{3:>14,.2f}{4:>9.1f}%".format(
            p["period"], p["net_sales"], p["total_collections"],
            p["ar_closing"], p["discount_pct"] * 100))

    for gran in ("Quarterly", "Half-yearly", "Yearly"):
        rolled = get_summary(from_period, to_period, gran, company)
        print("\n{0}".format(gran.upper()))
        for p in rolled["periods"]:
            print("{0:<12}{1:>16,.2f}{2:>16,.2f}{3:>14,.2f}{4:>9.1f}%".format(
                p["period"], p["net_sales"], p["total_collections"],
                p["ar_closing"], p["discount_pct"] * 100))

    m_net = sum(p["net_sales"] for p in monthly["periods"])
    y_net = sum(p["net_sales"] for p in
                get_summary(from_period, to_period, "Yearly", company)["periods"])
    print("\n{0:<40}{1:>16,.2f}".format("net sales, summed monthly", m_net))
    print("{0:<40}{1:>16,.2f}".format("net sales, rolled to yearly", y_net))
    print("{0}".format("FLOWS ROLL UP CORRECTLY" if abs(m_net - y_net) < 0.01
                       else "MISMATCH — flows are not summing"))

    last_month = monthly["periods"][-1]["ar_closing"]
    yearly_ar = get_summary(from_period, to_period, "Yearly",
                            company)["periods"][-1]["ar_closing"]
    print("{0:<40}{1:>16,.2f}".format("closing AR, last month", last_month))
    print("{0:<40}{1:>16,.2f}".format("closing AR, yearly bucket", yearly_ar))
    print("{0}\n".format("BALANCES TAKE THE LAST MONTH, CORRECT"
                         if abs(last_month - yearly_ar) < 0.01
                         else "MISMATCH — balances are being summed"))

    d = get_dimension("gross_sales", "Item Group", from_period, to_period, company)
    print("TOP SERVICE LINES, {0} to {1}".format(from_period, to_period))
    for r in d["rows"][:8]:
        print("  {0:<26}{1:>16,.2f}{2:>9.1f}%".format(
            r["label"][:25], r["amount"], r["share"] * 100))

    h = get_health(from_period, to_period, company)
    print("\nworst quality score in range: {0}%".format(h["worst"]))
    print(h["message"] + "\n")
    return True