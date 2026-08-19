# Copyright (c) 2026, Rasiin Technology and contributors
# For license information, please see license.txt

"""
Control Panel — the data-quality dashboard for the build itself.

Path: rasiin_insights/rasiin_insights/management_dashboard/report/control_panel/control_panel.py

"Unclassified revenue... is your data-quality KPI. If it grows, every
item-group drill-down on the dashboard is quietly becoming less true."
(Metric Definitions.) This report is where that KPI, and its siblings,
actually get watched, in one place, for one period: unclassified revenue,
unallocated receipts, drafts and cancellations excluded from sales, revenue
booked by journal instead of invoice, and whether the fact table still ties
to the ledger — controls.check_period, surfaced here instead of only being
reachable from `bench execute`.
"""

import frappe
from frappe.utils import flt

from rasiin_insights.management_dashboard.utils import controls
from rasiin_insights.management_dashboard.utils.extract import period_bounds, get_settings

QUALITY_NOTES = {
    "unclassified": "No item group — usually a Journal Entry. Watch this grow.",
    "unallocated": "Receipt never matched to an invoice. Real money, no service line.",
    "pending_allocation": "Awaiting pro-rata allocation to a service line.",
    "opening": "In sales but not in GL income — opening-balance entries.",
    "insurer_no_flag": "Names an insurer but the insurance flag isn't set.",
    "reclass": "Revenue moved or reversed between companies/accounts.",
}


def execute(filters=None):
    filters = frappe._dict(filters or {})
    period = filters.period or frappe.utils.nowdate()[:7]
    company = filters.company

    columns = [
        {"label": "Category", "fieldname": "category", "fieldtype": "Data", "width": 170},
        {"label": "Item", "fieldname": "item", "fieldtype": "Data", "width": 240},
        {"label": "Count", "fieldname": "count", "fieldtype": "Int", "width": 80},
        {"label": "Amount", "fieldname": "amount", "fieldtype": "Currency", "width": 130},
        {"label": "Note", "fieldname": "note", "fieldtype": "Data", "width": 300},
    ]

    data = []
    data += _quality_flags(period, company)
    data += _drafts_and_cancelled(period, company)
    data += _voucher_source(period, company)
    data += _drift(period, company)

    drift_failing = sum(1 for d in data
                         if d["category"] == "Fact vs ledger" and d["note"] == "DRIFT")
    report_summary = [
        {"label": "Period", "value": period, "datatype": "Data"},
        {"label": "Drift checks failing", "value": drift_failing, "datatype": "Int",
         "indicator": "Red" if drift_failing else "Green"},
    ]

    message = ("Period {0}. 'Fact vs ledger' rows come straight from "
               "controls.check_period — the same control that gates the "
               "nightly build.").format(period)

    return columns, data, message, None, report_summary


def _quality_flags(period, company):
    conditions = ["period = %(period)s", "quality_flag != ''", "quality_flag IS NOT NULL"]
    values = {"period": period}
    if company:
        conditions.append("company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT quality_flag, COUNT(*) AS n, SUM(amount) AS amount
        FROM `tabManagement Fact`
        WHERE {0}
        GROUP BY quality_flag
        ORDER BY SUM(amount) DESC
    """.format(" AND ".join(conditions)), values, as_dict=True)

    # A row's quality_flag can carry several comma-joined flags at once
    # (e.g. "unclassified,opening") — split and re-aggregate per flag.
    totals = {}
    for r in rows:
        for flag in str(r.quality_flag).split(","):
            flag = flag.strip()
            if not flag:
                continue
            t = totals.setdefault(flag, {"n": 0, "amount": 0.0})
            t["n"] += r.n
            t["amount"] += flt(r.amount)

    return [{
        "category": "Data quality flag", "item": flag,
        "count": t["n"], "amount": t["amount"],
        "note": QUALITY_NOTES.get(flag, ""),
    } for flag, t in sorted(totals.items(), key=lambda x: -x[1]["amount"])]


def _drafts_and_cancelled(period, company):
    start, end = period_bounds(period)
    out = []
    for label, docstatus in (("Draft invoices excluded", 0), ("Cancelled invoices excluded", 2)):
        sql_filters = {"posting_date": ["between", [start, end]], "docstatus": docstatus}
        if company:
            sql_filters["company"] = company
        rows = frappe.get_all("Sales Invoice", filters=sql_filters,
                               fields=["name", "base_grand_total"])
        out.append({
            "category": "Excluded from sales", "item": label,
            "count": len(rows),
            "amount": sum(flt(r.base_grand_total) for r in rows),
            "note": "Not counted as sales — md_include_drafts is off by default.",
        })
    return out


def _voucher_source(period, company):
    conditions = ["period = %(period)s", "metric = 'gross_sales'"]
    values = {"period": period}
    if company:
        conditions.append("company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT voucher_type, COUNT(*) AS n, SUM(amount) AS amount
        FROM `tabManagement Fact`
        WHERE {0}
        GROUP BY voucher_type
        ORDER BY SUM(amount) DESC
    """.format(" AND ".join(conditions)), values, as_dict=True)

    return [{
        "category": "Revenue by voucher type", "item": r.voucher_type,
        "count": r.n, "amount": flt(r.amount),
        "note": "" if r.voucher_type == "Sales Invoice" else "Booked by journal, not an invoice.",
    } for r in rows]


def _drift(period, company):
    results = controls.check_period(period, company, verbose=False)
    threshold = flt(get_settings().md_drift_threshold or 1.0)
    out = []
    for comp, r in results.items():
        for c in r["checks"]:
            drifted = abs(c["unexplained"]) > threshold
            out.append({
                "category": "Fact vs ledger",
                "item": "{0} — {1}".format(comp, c["check"]),
                "count": None, "amount": c["unexplained"],
                "note": "DRIFT" if drifted else "ties",
            })
    return out
