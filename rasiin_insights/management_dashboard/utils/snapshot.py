"""
Snapshot aggregation and the nightly build.

Path: rasiin_insights/management_dashboard/utils/snapshot.py

WHY THIS EXISTS
    Management Fact holds ~250,000 rows a month. Reading twelve months of that
    for every dashboard load would take minutes and get worse every month. The
    snapshot pre-aggregates each period once, so a year of history is a few
    thousand rows instead of three million.

    This is the bank-statement model: closing balance carried forward, only the
    open period replayed.

WHAT A SNAPSHOT ROW IS
    company | period | metric | dimension_type | dimension_value | amount

    One row per combination. A new question from the CEO adds a new
    dimension_type VALUE, never a new column and never a new table.

    Cross-dimension questions — "merchant account by service line" — are not
    pre-aggregated. Those fall through to the fact table live, which is indexed
    and fast enough for the rare case.

WHY CUSTOMER IS NOT A DIMENSION HERE
    8,723 customers times 16 metrics would be 140,000 snapshot rows a month, to
    answer a question nobody asks of a summary. Top-customer lists read the fact
    table directly.

REBUILD RULE
    The current and previous month are rebuilt in full every night. This is not
    caution: 64% of the till-sweep journals are created the day after their
    posting date, and 930 invoices were cancelled in January alone. Older months
    are frozen and skipped.
"""

import time

import frappe
from frappe.utils import flt, add_months, getdate, nowdate

from rasiin_insights.management_dashboard.utils import extract

# dimension_type -> the Management Fact column it groups by
DIMENSIONS = {
    "Item Group": "item_group",
    "Channel": "channel",
    "Entity": "entity",
    "Service Line": "service_line",
    "Sales Type": "sales_type",
    "Payer Type": "payer_type",
    "Practitioner": "practitioner",
    "Cashier": "cashier",
    "Merchant Account": "merchant_account",
    "Mode of Payment": "mode_of_payment",
    "Voucher Type": "voucher_type",
    "Quality Flag": "quality_flag",
}

# Metrics whose sign is negative when reading a net figure.
NET_SIGNS = {
    "gross_sales": 1, "discount": -1, "return": -1, "return_discount": 1,
    "revenue_reclass": -1,
}


def period_of(date_str):
    d = getdate(date_str)
    return "{0}-{1:02d}".format(d.year, d.month)


def recent_periods(count):
    """Current period and the previous count-1 periods, newest first."""
    today = getdate(nowdate())
    return [period_of(add_months(today, -i)) for i in range(count)]


# ------------------------------------------------------------- aggregation

def companies_in_period(period):
    """
    Every company that needs a snapshot — not just those with activity.

    A dormant company still holds balances. Shaafi Diagnostic Center had no
    July movement but carried $5,645.92 receivable and $14,451.50 payable;
    without this, those balances vanish from the dashboard entirely.
    """
    return frappe.get_all("Company", pluck="name", order_by="name")


def aggregate(period, company):
    """
    One pass per dimension, done in SQL. Returns a list of snapshot dicts.

    The Total rows come first because every dimension's rows are checked
    against them — if a dimension does not sum to its own metric total, the
    drill-down would disagree with the headline, which is the one thing this
    project exists to prevent.
    """
    rows = []

    totals = {r.metric: flt(r.amount) for r in frappe.db.sql("""
        SELECT metric, SUM(amount) AS amount
        FROM `tabManagement Fact`
        WHERE company = %(c)s AND period = %(p)s
        GROUP BY metric
    """, {"c": company, "p": period}, as_dict=True)}

    for metric, amount in totals.items():
        rows.append({
            "company": company, "period": period, "metric": metric,
            "dimension_type": "Total", "dimension_value": "All",
            "amount": amount, "control_total": amount, "variance": 0.0,
        })

    for dimension_type, column in DIMENSIONS.items():
        grouped = frappe.db.sql("""
            SELECT metric, COALESCE(NULLIF(`{col}`, ''), 'Not recorded') AS value,
                   SUM(amount) AS amount
            FROM `tabManagement Fact`
            WHERE company = %(c)s AND period = %(p)s
            GROUP BY metric, value
        """.format(col=column), {"c": company, "p": period}, as_dict=True)

        summed = {}
        for r in grouped:
            summed[r.metric] = summed.get(r.metric, 0.0) + flt(r.amount)
            rows.append({
                "company": company, "period": period, "metric": r.metric,
                "dimension_type": dimension_type,
                "dimension_value": r.value,
                "amount": flt(r.amount),
                "control_total": flt(totals.get(r.metric, 0.0)),
                "variance": 0.0,
            })

        # Stamp the variance on every row of a dimension that does not tie.
        for metric, total in summed.items():
            drift = round(total - flt(totals.get(metric, 0.0)), 2)
            if abs(drift) >= 0.01:
                for row in rows:
                    if (row["dimension_type"] == dimension_type
                            and row["metric"] == metric):
                        row["variance"] = drift

    return rows


def balance_rows(period, company):
    """
    Opening and closing balances as their own snapshot rows.

    Opening is taken from the previous period's snapshot when one exists, so
    the report never rescans history. When it does not exist — the first month
    built — it is read from the ledger directly.
    """
    rows = []
    previous = period_of(add_months(getdate(period + "-01"), -1))

    for ledger, charged, settled in (
            ("ar", "ar_transfer_in", "ar_transfer_out"),
            ("ap", "payable_charged", "supplier_payment")):

        opening = frappe.db.get_value("Management Snapshot", {
            "company": company, "period": previous,
            "metric": ledger + "_closing", "dimension_type": "Total",
        }, "amount")

        if opening is None:
            account_type = "Receivable" if ledger == "ar" else "Payable"
            opening = extract.opening_balance(account_type, period, company)
            source = "ledger"
        else:
            source = "carried forward"

        moved_in = frappe.db.sql("""
            SELECT COALESCE(SUM(amount), 0) AS a FROM `tabManagement Fact`
            WHERE company = %(c)s AND period = %(p)s AND metric = %(m)s
        """, {"c": company, "p": period, "m": charged}, as_dict=True)[0].a
        moved_out = frappe.db.sql("""
            SELECT COALESCE(SUM(amount), 0) AS a FROM `tabManagement Fact`
            WHERE company = %(c)s AND period = %(p)s AND metric = %(m)s
        """, {"c": company, "p": period, "m": settled}, as_dict=True)[0].a

        closing = flt(opening) + flt(moved_in) - flt(moved_out)

        for suffix, amount in (("_opening", flt(opening)),
                               ("_closing", closing)):
            rows.append({
                "company": company, "period": period,
                "metric": ledger + suffix, "dimension_type": "Total",
                "dimension_value": source if suffix == "_opening" else "All",
                "amount": amount, "control_total": amount, "variance": 0.0,
            })

    return rows


def quality_score(period, company):
    """Share of collections that could be traced to a service line."""
    row = frappe.db.sql("""
        SELECT
          COALESCE(SUM(CASE WHEN item_group = 'Unallocated' THEN amount END), 0) AS untraced,
          COALESCE(SUM(amount), 0) AS total
        FROM `tabManagement Fact`
        WHERE company = %(c)s AND period = %(p)s
          AND metric IN ('collection_current', 'collection_prior',
                         'collection_unallocated')
    """, {"c": company, "p": period}, as_dict=True)[0]
    if not flt(row.total):
        return 0.0
    return round(100.0 * (1 - flt(row.untraced) / flt(row.total)), 2)


# ------------------------------------------------------------------- write

def delete_snapshot(period, company):
    frappe.db.sql("""
        DELETE FROM `tabManagement Snapshot`
        WHERE company = %(c)s AND period = %(p)s
    """, {"c": company, "p": period})


def is_frozen(period, company):
    return bool(frappe.db.get_value("Management Snapshot", {
        "company": company, "period": period, "is_frozen": 1}, "name"))


def build_snapshot(period, company=None, dry_run=True):
    started = time.time()
    companies = [company] if company else companies_in_period(period)
    if not companies:
        print("\nNo facts for {0}. Build the facts first.\n".format(period))
        return {}

    results = {}
    for comp in companies:
        if is_frozen(period, comp) and not dry_run:
            print("{0} / {1} is frozen — skipped".format(period, comp))
            continue

        rows = aggregate(period, comp) + balance_rows(period, comp)
        score = quality_score(period, comp)
        for r in rows:
            r["quality_score"] = score
            r["is_frozen"] = 0

        drifted = [r for r in rows if abs(flt(r["variance"])) >= 0.01]

        if not dry_run:
            delete_snapshot(period, comp)
            extract.insert_facts_into(rows, "Management Snapshot")

        results[comp] = {"rows": len(rows), "drift": len(drifted),
                         "quality_score": score}

        print("\n" + "=" * 68)
        print("SNAPSHOT — {0} / {1}   ({2})".format(
            period, comp,
            "DRY RUN, nothing written" if dry_run
            else "{0:,} rows written".format(len(rows))))
        print("=" * 68)

        totals = {r["metric"]: r["amount"] for r in rows
                  if r["dimension_type"] == "Total"}
        net = sum(NET_SIGNS.get(m, 0) * a for m, a in totals.items())
        collections = sum(a for m, a in totals.items()
                          if m.startswith("collection_"))

        print("{0:<26}{1:>18,.2f}".format("net sales", net))
        print("{0:<26}{1:>18,.2f}".format("collections", collections))
        print("{0:<26}{1:>18,.2f}".format(
            "closing receivable", totals.get("ar_closing", 0.0)))
        print("{0:<26}{1:>18,.2f}".format(
            "closing payable", totals.get("ap_closing", 0.0)))
        print("{0:<26}{1:>18,.2f}".format(
            "total expense",
            sum(totals.get(m, 0.0) for m in ("commission", "payroll", "expense"))))
        print("-" * 68)
        print("{0:<26}{1:>17.2f}%".format("collections traced", score))
        print("{0:<26}{1:>18,}".format("snapshot rows", len(rows)))
        print("{0:<26}{1:>18,}".format("dimensions", len(DIMENSIONS) + 1))

        if drifted:
            print("\nDIMENSIONS THAT DO NOT TIE TO THEIR TOTAL")
            seen = set()
            for r in drifted:
                key = (r["dimension_type"], r["metric"])
                if key in seen:
                    continue
                seen.add(key)
                print("  {0:<20}{1:<24}{2:>14,.2f}".format(
                    r["dimension_type"], r["metric"], r["variance"]))
        else:
            print("\nevery dimension ties to its metric total")
        print("seconds                {0:>10.1f}\n".format(time.time() - started))

    return results


# ------------------------------------------------------------ nightly job

def build_period(period, company=None, dry_run=False):
    """Run every extraction step for one period, then snapshot it."""
    extract.build_sales(period, company, dry_run=dry_run)
    extract.build_noninvoice_revenue(period, company, dry_run=dry_run)
    extract.build_collections_allocated(period, company, dry_run=dry_run)
    extract.build_balances(period, company, dry_run=dry_run)
    extract.build_money_out(period, company, dry_run=dry_run)
    build_snapshot(period, company, dry_run=dry_run)


def build_management_facts():
    """
    Scheduler entry point. Registered in hooks.py.

    Rebuilds the recent periods in full. Idempotent by construction — every
    build function deletes its own metrics for the period before inserting,
    so a re-run produces identical output rather than duplicates.
    """
    settings = extract.get_settings()
    if not settings.md_enabled:
        return

    if settings.md_run_hour is not None:
        if frappe.utils.now_datetime().hour != int(settings.md_run_hour):
            return

    log = frappe.new_doc("Management Build Log")
    log.run_started = frappe.utils.now()
    started = time.time()
    periods = recent_periods(int(settings.md_rebuild_months or 2))
    written = 0
    errors = []

    for period in periods:
        try:
            build_period(period, dry_run=False)
            written += frappe.db.count("Management Fact", {"period": period})
            frappe.db.commit()
        except Exception:
            errors.append("{0}\n{1}".format(period, frappe.get_traceback()))
            frappe.db.rollback()

    log.run_finished = frappe.utils.now()
    log.periods_rebuilt = ", ".join(periods)
    log.fact_rows_written = written
    log.duration_seconds = int(time.time() - started)
    log.status = "Failed" if errors else "Success"
    log.error_log = "\n\n".join(errors)[:100000]
    log.insert(ignore_permissions=True)
    frappe.db.commit()


def verify_snapshot(period="2026-07", company=None):
    """Step 2.9 acceptance test. Read-only."""
    return build_snapshot(period, company, dry_run=True)


def backfill(start_period, end_period, dry_run=True):
    """
    Build every month in a range, oldest first, checking each against the
    ledger before moving on.

    Oldest first matters: each month's closing balance becomes the next
    month's opening, so building out of order gives every later month a
    wrong opening figure.
    """
    from rasiin_insights.management_dashboard.utils import controls

    periods = []
    cursor = getdate(start_period + "-01")
    last = getdate(end_period + "-01")
    while cursor <= last:
        periods.append(period_of(cursor))
        cursor = add_months(cursor, 1)

    summary = []
    for period in periods:
        started = time.time()
        build_period(period, dry_run=dry_run)
        results = controls.check_period(period, verbose=False)
        drift = sum(r["drift"] for r in results.values())
        summary.append((period, drift, round(time.time() - started)))
        print("\n>>> {0}: {1}, {2}s\n".format(
            period, "clean" if not drift else
            "{0} check(s) drifted".format(drift), summary[-1][2]))

    print("\n" + "=" * 60)
    print("BACKFILL SUMMARY")
    print("=" * 60)
    for period, drift, secs in summary:
        print("{0:<12}{1:<20}{2:>8}s".format(
            period, "clean" if not drift else
            "{0} drifted".format(drift), secs))
    print("")
    return summary