# Copyright (c) 2026, Rasiin Technology and contributors
# For license information, please see license.txt

"""
Till Reconciliation — collections in, sweeps out, running closing balance,
per cash/bank account per day.

Path: rasiin_insights/rasiin_insights/management_dashboard/report/till_reconciliation/till_reconciliation.py

WHY THIS IS A LIVE GL QUERY, NOT A FACT/SNAPSHOT READ
    Internal transfers are deliberately never written to Management Fact —
    extract.py's transfer step "writes no facts of its own", because a
    transfer is not income and the fact table only holds real money
    movements. That means the one thing this report needs — how much moved
    OUT of a specific till on a specific day — does not exist in the fact
    table at all. This reads GL Entry directly, using the same is_internal
    rule extract.summarise_transfers() already uses for collections.

WHAT IT CATCHES
    A till's running balance should never go negative. A sweep booked
    against the wrong till, a duplicate sweep, or a same-day counting error
    all show up as a negative running closing balance — this is what would
    have caught till 701235 sitting at -$1,200 for five days.
"""

import frappe
from frappe.utils import flt, getdate, add_days

from rasiin_insights.management_dashboard.utils.extract import cash_bank_accounts


def execute(filters=None):
    filters = frappe._dict(filters or {})
    from_date = getdate(filters.from_date) if filters.from_date else add_days(getdate(), -6)
    to_date = getdate(filters.to_date) if filters.to_date else getdate()
    company = filters.company

    columns = [
        {"label": "Date", "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
        {"label": "Account (Till)", "fieldname": "account", "fieldtype": "Link",
         "options": "Account", "width": 240},
        {"label": "Opening", "fieldname": "opening", "fieldtype": "Currency", "width": 120},
        {"label": "Collections In", "fieldname": "collections_in", "fieldtype": "Currency", "width": 120},
        {"label": "Swept Out", "fieldname": "swept_out", "fieldtype": "Currency", "width": 120},
        {"label": "Other Out", "fieldname": "other_out", "fieldtype": "Currency", "width": 110},
        {"label": "Closing", "fieldname": "closing", "fieldtype": "Currency", "width": 120},
        {"label": "Flag", "fieldname": "flag", "fieldtype": "Data", "width": 100},
    ]

    accounts = cash_bank_accounts(company)
    if not accounts:
        return columns, [], "No Cash/Bank accounts found for this company.", None, []

    base_values = {"accounts": list(accounts)}
    company_cond = ""
    if company:
        company_cond = "AND gle.company = %(company)s"
        base_values["company"] = company

    # Opening balance per account, as of the day before the range starts —
    # so this report can be run for any window without rescanning history.
    opening_rows = frappe.db.sql("""
        SELECT gle.account AS account, SUM(gle.debit - gle.credit) AS balance
        FROM `tabGL Entry` gle
        WHERE gle.account IN %(accounts)s AND gle.is_cancelled = 0
          AND gle.posting_date < %(from_date)s {company_cond}
        GROUP BY gle.account
    """.format(company_cond=company_cond),
        dict(base_values, from_date=from_date), as_dict=True)
    opening = {r.account: flt(r.balance) for r in opening_rows}

    # Every posting in range, flagged internal exactly the way collections
    # everywhere else in the app are.
    movement_rows = frappe.db.sql("""
        SELECT gle.account AS account, gle.posting_date AS posting_date,
               gle.debit AS debit, gle.credit AS credit,
               EXISTS (
                   SELECT 1 FROM `tabGL Entry` g2
                   INNER JOIN `tabAccount` a2 ON a2.name = g2.account
                   WHERE g2.voucher_type = gle.voucher_type
                     AND g2.voucher_no = gle.voucher_no
                     AND g2.is_cancelled = 0 AND g2.credit > 0
                     AND a2.account_type IN ('Cash', 'Bank')
               ) AS is_internal
        FROM `tabGL Entry` gle
        WHERE gle.account IN %(accounts)s AND gle.is_cancelled = 0
          AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s {company_cond}
        ORDER BY gle.account, gle.posting_date
    """.format(company_cond=company_cond),
        dict(base_values, from_date=from_date, to_date=to_date), as_dict=True)

    by_account_day = {}
    for r in movement_rows:
        key = (r.account, str(r.posting_date))
        d = by_account_day.setdefault(key, {"in": 0.0, "swept": 0.0, "other_out": 0.0})
        if flt(r.debit):
            d["in"] += flt(r.debit)
        if flt(r.credit):
            if r.is_internal:
                d["swept"] += flt(r.credit)
            else:
                d["other_out"] += flt(r.credit)

    data = []
    for account in sorted(accounts):
        running = opening.get(account, 0.0)
        cursor = from_date
        while cursor <= to_date:
            key = (account, str(cursor))
            m = by_account_day.get(key)
            if m:
                closing = running + m["in"] - m["swept"] - m["other_out"]
                data.append({
                    "posting_date": cursor, "account": account,
                    "opening": running, "collections_in": m["in"],
                    "swept_out": m["swept"], "other_out": m["other_out"],
                    "closing": closing,
                    "flag": "NEGATIVE" if closing < -0.01 else "",
                })
                running = closing
            cursor = add_days(cursor, 1)
        # Accounts with zero movement anywhere in the range are left out —
        # nothing to reconcile, and it would be one silent row per idle
        # till per day otherwise.

    data.sort(key=lambda d: (d["flag"] != "NEGATIVE", d["account"], d["posting_date"]))

    flagged = [d for d in data if d["flag"] == "NEGATIVE"]
    report_summary = [
        {"label": "Tills with a negative day", "value": len({d["account"] for d in flagged}),
         "datatype": "Int", "indicator": "Red" if flagged else "Green"},
        {"label": "Negative days, total", "value": len(flagged), "datatype": "Int"},
        {"label": "Rows shown", "value": len(data), "datatype": "Int"},
    ]
    message = ("Opening balance is the account's real GL balance the day before "
               "the range starts. A negative closing balance is not a cash-flow "
               "event — it means a sweep is missing its collection, duplicated, "
               "or booked against the wrong till, and should be investigated "
               "the same day it appears, not at month end.")

    return columns, data, message, None, report_summary
