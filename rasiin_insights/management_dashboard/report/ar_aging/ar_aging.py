# Copyright (c) 2026, Rasiin Technology and contributors
# For license information, please see license.txt

"""
AR Aging — invoice-level receivables, with age buckets, as of any date.

Path: rasiin_insights/rasiin_insights/management_dashboard/report/ar_aging/ar_aging.py

WHY THIS DOES NOT READ Sales Invoice.outstanding_amount
    That field is CURRENT state only. Age a past month-end with it and you
    report today's number under last quarter's heading (Metric Definitions,
    "Closing Outstanding"). Every invoice's outstanding here is rebuilt from
    GL Entry against the receivable account, up to the as-of date chosen —
    the same rule the Management Snapshot's ar_closing figure already
    follows at company level, just at invoice grain.

WHY THIS IS ITS OWN QUERY, NOT A SNAPSHOT READ
    Management Snapshot deliberately has no invoice dimension — 8,700+
    customers x every metric was the reason "customer" was left out
    (snapshot.py). Invoice-level aging is inherently that granularity, so
    this reads Sales Invoice + GL Entry live, the same "fall through to the
    ledger live" rule the daily trend and Till Reconciliation both use.

PATIENT TYPE (OPD/IPD) COLUMN
    Resolved from Sales Invoice.source_order through the "Patient Type"
    Management Dimension Rule (see patches/add_patient_type_dimension.py).
    Only OPD and IPD are mapped so far — everything else (E.R, PACKAGE,
    Referral, Anaesthesia, unset) reads "Unclassified" until finance
    decides how those should bucket. Treat the OPD/IPD split as reliable;
    treat "Unclassified" as "not decided yet", not as its own category.

DATA-QUALITY GATE — read before trusting the oldest buckets
    97% of January's Payment Entry receipts were never matched to an
    invoice. An unmatched receipt still reduces the customer's GL balance
    but not any single invoice's via against_voucher, so invoice-level aging
    will show a specific invoice as "still owed" when the customer in fact
    paid, just not against that invoice. The report_summary below always
    shows the current unallocated-receipts figure so nobody reads the aging
    table blind for a period that hasn't been reconciled.
"""

import frappe
from frappe.utils import flt, getdate, date_diff, nowdate

from rasiin_insights.management_dashboard.utils.resolve import (
    DimensionResolver,
    build_insurance_flag,
)

# Patient Type resolves through the same configurable Management Dimension
# Rule/Mapping mechanism as Payer Type — see patches/add_patient_type_dimension.py
# for what's actually seeded (OPD, IPD; everything else falls through to
# "Unclassified" until finance decides how it should bucket).

BUCKETS = [(0, 30, "0-30"), (31, 60, "31-60"), (61, 90, "61-90"),
           (91, 120, "91-120"), (121, None, "120+")]


def bucket_of(age):
    for lo, hi, label in BUCKETS:
        if age >= lo and (hi is None or age <= hi):
            return label
    return "120+"


def receivable_accounts(company=None):
    filters = {"account_type": "Receivable"}
    if company:
        filters["company"] = company
    return set(frappe.get_all("Account", filters=filters, pluck="name"))


def execute(filters=None):
    filters = frappe._dict(filters or {})
    as_of = getdate(filters.as_of_date or nowdate())
    company = filters.company

    columns = [
        {"label": "Invoice", "fieldname": "invoice", "fieldtype": "Link",
         "options": "Sales Invoice", "width": 150},
        {"label": "Invoice Date", "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
        {"label": "Age (days)", "fieldname": "age", "fieldtype": "Int", "width": 90},
        {"label": "Bucket", "fieldname": "bucket", "fieldtype": "Data", "width": 80},
        {"label": "Customer", "fieldname": "customer_name", "fieldtype": "Data", "width": 180},
        {"label": "Payer Type", "fieldname": "payer_type", "fieldtype": "Data", "width": 110},
        {"label": "Patient Type", "fieldname": "patient_type", "fieldtype": "Data", "width": 100},
        {"label": "Company", "fieldname": "company", "fieldtype": "Link",
         "options": "Company", "width": 130},
        {"label": "Net Amount", "fieldname": "net_amount", "fieldtype": "Currency", "width": 120},
        {"label": "Paid", "fieldname": "paid", "fieldtype": "Currency", "width": 120},
        {"label": "Outstanding", "fieldname": "outstanding", "fieldtype": "Currency", "width": 130},
    ]

    accounts = receivable_accounts(company)
    if not accounts:
        return columns, [], "No Receivable-type accounts found for this company.", None, []

    conditions = [
        "gle.account IN %(accounts)s",
        "gle.is_cancelled = 0",
        "gle.posting_date <= %(as_of)s",
        "(gle.voucher_type = 'Sales Invoice' OR gle.against_voucher_type = 'Sales Invoice')",
    ]
    values = {"accounts": list(accounts), "as_of": as_of}
    if company:
        conditions.append("gle.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT
            CASE WHEN gle.voucher_type = 'Sales Invoice' THEN gle.voucher_no
                 ELSE gle.against_voucher END AS invoice,
            SUM(gle.debit - gle.credit) AS outstanding
        FROM `tabGL Entry` gle
        WHERE {0}
        GROUP BY invoice
        HAVING ABS(SUM(gle.debit - gle.credit)) >= 0.01
    """.format(" AND ".join(conditions)), values, as_dict=True)

    if not rows:
        return columns, [], "Nothing outstanding as of {0}.".format(as_of), None, []

    invoice_names = [r.invoice for r in rows if r.invoice]
    invoices = {i.name: i for i in frappe.get_all(
        "Sales Invoice", filters={"name": ["in", invoice_names]},
        fields=["name", "posting_date", "customer", "customer_name",
                "customer_group", "is_insurance", "insurance", "company",
                "base_net_total", "source_order"])}

    resolver = DimensionResolver()
    data = []
    total_outstanding = 0.0
    weighted_age = 0.0
    by_bucket = {}

    for r in rows:
        inv = invoices.get(r.invoice)
        if not inv:
            continue  # opening-balance or non-invoice reference — out of scope here
        age = date_diff(as_of, inv.posting_date)
        if age < 0:
            continue  # invoice raised after the as-of date

        bucket = bucket_of(age)
        outstanding = flt(r.outstanding)

        insurance_flag = build_insurance_flag(inv.is_insurance, inv.insurance)
        dims = resolver.resolve_all(inv.posting_date, {
            "item_group": None, "sales_type": None, "cost_center": None,
            "income_account": None, "warehouse": None, "company": inv.company,
            "insurance_flag": insurance_flag, "customer_group": inv.customer_group,
        })
        patient_type, _ = resolver.resolve(
            "Patient Type", inv.posting_date, {"source_order": inv.source_order})

        data.append({
            "invoice": inv.name, "posting_date": inv.posting_date, "age": age,
            "bucket": bucket, "customer_name": inv.customer_name,
            "payer_type": dims["payer_type"], "patient_type": patient_type,
            "company": inv.company,
            "net_amount": flt(inv.base_net_total),
            "paid": flt(inv.base_net_total) - outstanding,
            "outstanding": outstanding,
        })

        total_outstanding += outstanding
        weighted_age += outstanding * age
        by_bucket[bucket] = by_bucket.get(bucket, 0.0) + outstanding

    data.sort(key=lambda d: -d["outstanding"])

    dso = (weighted_age / total_outstanding) if total_outstanding else 0.0

    unallocated_filters = {"metric": "collection_unallocated", "dimension_type": "Total"}
    if company:
        unallocated_filters["company"] = company
    unallocated = flt(frappe.db.sql("""
        SELECT SUM(amount) AS a FROM `tabManagement Snapshot`
        WHERE metric = 'collection_unallocated' AND dimension_type = 'Total'
        {0}
    """.format("AND company = %(company)s" if company else ""),
        {"company": company}, as_dict=True)[0].a or 0)

    by_patient_type = {}
    for d in data:
        by_patient_type[d["patient_type"]] = by_patient_type.get(d["patient_type"], 0.0) + d["outstanding"]

    report_summary = [
        {"label": "Total outstanding", "value": total_outstanding, "datatype": "Currency"},
        {"label": "OPD outstanding", "value": by_patient_type.get("OPD", 0.0), "datatype": "Currency"},
        {"label": "IPD outstanding", "value": by_patient_type.get("IPD", 0.0), "datatype": "Currency"},
        {"label": "Unclassified (patient type)",
         "value": by_patient_type.get("Unclassified", 0.0), "datatype": "Currency",
         "indicator": "Orange" if by_patient_type.get("Unclassified", 0.0) else "Green"},
        {"label": "Weighted average age (days)", "value": round(dso, 1), "datatype": "Float"},
        {"label": "120+ days", "value": by_bucket.get("120+", 0.0), "datatype": "Currency",
         "indicator": "Red" if by_bucket.get("120+", 0.0) > 0 else "Green"},
        {"label": "Invoices in this book", "value": len(data), "datatype": "Int"},
        {"label": "Unallocated receipts, all history", "value": unallocated,
         "datatype": "Currency",
         "indicator": "Red" if unallocated > 50000 else "Orange" if unallocated else "Green"},
    ]

    message = (
        "As of {0}. Outstanding is rebuilt from GL Entry against the receivable "
        "account up to this date — never from Sales Invoice.outstanding_amount, "
        "which is current-state only. Receipts that were never matched to an "
        "invoice still reduce the customer's ledger balance but not any single "
        "invoice's, so the oldest buckets can overstate what a customer with "
        "unmatched receipts actually still owes. Cross-check against the "
        "'Not matched to an invoice' line on the dashboard for these invoices' "
        "periods before acting on this."
    ).format(as_of)

    return columns, data, message, None, report_summary
