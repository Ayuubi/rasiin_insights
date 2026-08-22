# Copyright (c) 2026, Rasiin Technology and contributors
# For license information, please see license.txt

"""
Operational Reports — Page 3: Expenses & Payables.

Path: rasiin_insights/management_dashboard/utils/operational_api_payables.py

WHO THIS IS FOR
    Finance and accountants closing the day, same audience as Page 1
    (operational_api_receivable.py) and Page 2 (operational_api_cash.py).
    Every function here reads live from GL Entry / Purchase Invoice /
    Payment Entry — never Management Snapshot (monthly-grain only) and
    never Management Fact (the hourly-refreshed CEO-dashboard copy).

SCOPE — confirmed with the user 2026-08-22, against the original catalog
(operational-reports-catalog-v1.md, Page 3):
    C1  Daily expenses, petty cash (accounts 50301/50302) broken out as
        its own line (confirmed 2026-08-20: petty cash is an expense
        category here, not a float — E3 folded into C1, not standalone).
    C2  Goods/assets received but not yet invoiced.
    C3  Payables opening/closing rollforward — the AP mirror of A1.
    C4  Supplier payments in a date range ("today" = from_date == to_date).
    Plus two additions the round-3 handoff proposed and the user
    confirmed: a by-supplier aging breakdown (AP mirror of ar_aging.py)
    and a top-supplier-movers table (AP mirror of A3 / get_ar_top_movers).

VALIDATION — performed 2026-08-22 against the user's real Jan/Jul 2026
recon exports (recon_20260101_20260131 / recon_20260701_20260731), the
same exports the AR side was validated against on 2026-08-20:
    Re-derived AP opening/charged/settled/closing independently in pandas,
    the same identity extract.py's REFERENCE_BALANCES already documents
    (opening = SUM(credit_before - debit_before) on Payable-type accounts
    from 23_balance_before_period.csv; charged = SUM(credit), settled =
    SUM(debit) on GL Entry against those accounts in range; closing =
    opening + charged - settled).

    January — ties to the cent, all four figures, "all companies" scope:
        opening 157,125.40 / charged 467,435.55 / settled 198,085.28 /
        closing 426,475.67.

    July — opening and settled tie to the cent; charged/closing are both
    off by exactly $432.00. Traced to one specific real GL voucher
    (ACC-JV-2026-09569, posted 2026-07-31, created 2026-08-19: credits
    2110 - Creditors - SH for Supplier MAGOOL, debits 50100 A- -
    Sanitation Expense - SH — a normal, balanced, non-cancelled Journal
    Entry) that is present in this export but not reflected in
    REFERENCE_BALANCES' July figure. Most likely explanation: this is a
    live, moving-target dataset (the same kind of drift already noted
    elsewhere in this project for AR / net sales — see
    daily-filtering-and-next-steps.md's 2026-08-19 addendum) and
    REFERENCE_BALANCES' July snapshot predates this voucher. NOT treated
    as a bug in this module's logic: the double-entry identity
    (closing == opening + charged - settled) held exactly, to the cent,
    in both months, for every scope tested — the $432 is a timing
    difference in the reference figure, not a computation error. Flagging
    this plainly rather than presenting it as a clean tie, per this
    project's standing rule about not overstating confidence.

    SDC static balance: Shaafi Diagnostic Center's payable accounts carry
    a static $14,451.50 opening balance with ZERO GL movement in either
    January or July — the exact same shape as the $5,645.92 static SDC
    receivable balance found 2026-08-22 (operational-reports-mismatch-fix-v2.md
    §1). Same fix applies here: this page defaults Company to
    api.get_filters()'s default_company (Shaafi Hospital), not "all
    companies", the same way Pages 1/2 now do. "All companies" stays
    selectable.

WHY THE AGING/TOP-MOVERS BUCKETING WON'T MATCH REFERENCE_BALANCES' OWN
CHARGED/SETTLED SPLIT
    extract.py's charged/settled is a pure debit/credit SIDE split across
    every voucher type touching a Payable account. get_ap_rollforward
    below instead categorises net movement BY VOUCHER TYPE (Purchase
    Invoice -> charged, Payment Entry -> paid, Journal Entry ->
    adjustments) — the same categorisation get_ar_rollforward uses for
    Sales Invoice/Payment Entry/Journal Entry on the receivable side, and
    the more legible breakdown for a page finance is meant to read. Both
    schemes are complete partitions of the SAME raw net movement, so the
    OPENING and CLOSING totals are identical either way and both were
    independently validated above — only the "which day did which change
    happen" per-bucket display differs, and it differs on purpose.

WHY C2 READS THE ACCRUAL ACCOUNTS DIRECTLY, NOT PURCHASE RECEIPT
    'Stock Received But Not Billed' / 'Asset Received But Not Billed'
    (accounts 2210/2211, confirmed present for both SH and SDC in the
    real chart of accounts) are the standard ERPNext accrual accounts a
    Purchase Receipt credits on receipt and a matching Purchase Invoice
    debits when billed. Their live GL balance IS the value of goods/
    assets received but not yet invoiced, by construction — no Purchase
    Receipt join needed, which is useful because Purchase Receipt has
    never been part of any export this app has seen. See
    get_grn_not_invoiced's own docstring for the one real limitation this
    shortcut has.
"""

import frappe
from frappe.utils import flt, getdate, add_days, cint, date_diff


# ============================================================= account helpers

def payable_accounts(company=None):
    filters = {"account_type": "Payable"}
    if company:
        filters["company"] = company
    return set(frappe.get_all("Account", filters=filters, pluck="name"))


def grn_accrual_accounts(company=None):
    filters = {"account_type": ["in", ["Stock Received But Not Billed", "Asset Received But Not Billed"]]}
    if company:
        filters["company"] = company
    return set(frappe.get_all("Account", filters=filters, pluck="name"))


def expense_account_map(company=None):
    """{account_name: account_number} for every root_type=Expense account, so
    the petty-cash split (accounts 50301/50302) can be identified without a
    second query per row."""
    filters = {"root_type": "Expense"}
    if company:
        filters["company"] = company
    return {r.name: r.account_number for r in
            frappe.get_all("Account", filters=filters, fields=["name", "account_number"])}


PETTY_CASH_ACCOUNT_NUMBERS = ("50301", "50302")


def _resolve_party_names(rows):
    """
    Bulk name lookup for GL Entry rows carrying party_type/party — the
    exact batched-by-5000 pattern from operational_api_receivable.py /
    operational_api_cash.py, copied rather than shared (this app has no
    JS/Python module-sharing between the operational files by design).
    Returns {(party_type, party): name}.
    """
    names = {}
    parties = {(r.get("party_type"), r.get("party")) for r in rows
               if r.get("party_type") and r.get("party")}
    for doctype in {p[0] for p in parties}:
        field = {"Customer": "customer_name", "Supplier": "supplier_name",
                 "Employee": "employee_name"}.get(doctype)
        if not field:
            continue
        ids = [p[1] for p in parties if p[0] == doctype]
        for i in range(0, len(ids), 5000):
            for rec in frappe.get_all(doctype, filters={"name": ["in", ids[i:i + 5000]]},
                                       fields=["name", field]):
                names[(doctype, rec["name"])] = rec.get(field)
    return names


# ============================================================= C3 — rollforward

@frappe.whitelist()
def get_ap_rollforward(from_date, to_date, company=None):
    """
    Daily opening -> charged -> paid -> adjustments -> closing, summed
    across every Payable-type account, one row per calendar day — the AP
    mirror of get_ar_rollforward.

    Categorisation (by voucher_type, on the payable-account leg only):
        Charged      Purchase Invoice postings (billed to us)
        Paid         Payment Entry postings, shown positive when they
                     reduce the balance (the normal case)
        Adjustments  Journal Entry postings — write-offs, supplier
                     balance transfers, opening entries
        Other        anything else touching a payable account

    closing = opening + charged_net + paid_net + adjustment_net + other_net,
    using the RAW signed net movement (credit - debit — the payable
    account's normal balance side) for each bucket — the identity holds
    by construction, not by the labels chosen for display. See this
    module's docstring for the independent validation against the real
    Jan/Jul 2026 data.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    accounts = payable_accounts(company)
    if not accounts:
        return {"days": [], "message": "No Payable-type accounts found for this company."}

    values = {"accounts": list(accounts), "from_date": from_date}
    company_cond = ""
    if company:
        company_cond = "AND gle.company = %(company)s"
        values["company"] = company

    opening_total = flt(frappe.db.sql("""
        SELECT SUM(gle.credit - gle.debit) AS bal
        FROM `tabGL Entry` gle
        WHERE gle.account IN %(accounts)s AND gle.is_cancelled = 0
          AND gle.posting_date < %(from_date)s {company_cond}
    """.format(company_cond=company_cond), values, as_dict=True)[0].bal or 0)

    values2 = dict(values, to_date=to_date)
    movement_rows = frappe.db.sql("""
        SELECT gle.posting_date AS posting_date, gle.voucher_type AS voucher_type,
               SUM(gle.credit - gle.debit) AS net
        FROM `tabGL Entry` gle
        WHERE gle.account IN %(accounts)s AND gle.is_cancelled = 0
          AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s {company_cond}
        GROUP BY gle.posting_date, gle.voucher_type
    """.format(company_cond=company_cond), values2, as_dict=True)

    BUCKET_OF = {"Purchase Invoice": "charged", "Payment Entry": "paid", "Journal Entry": "adjustments"}
    by_day = {}
    for r in movement_rows:
        d = by_day.setdefault(str(r.posting_date),
                               {"charged": 0.0, "paid": 0.0, "adjustments": 0.0, "other": 0.0})
        d[BUCKET_OF.get(r.voucher_type, "other")] += flt(r.net)

    days = []
    running = opening_total
    cursor = from_date
    while cursor <= to_date:
        key = str(cursor)
        m = by_day.get(key, {"charged": 0.0, "paid": 0.0, "adjustments": 0.0, "other": 0.0})
        net = m["charged"] + m["paid"] + m["adjustments"] + m["other"]
        closing = running + net
        days.append({
            "date": key,
            "opening": running,
            "charged": m["charged"],
            "paid": -m["paid"],   # display positive = money that reduced AP
            "adjustments": m["adjustments"] + m["other"],
            "closing": closing,
        })
        running = closing
        cursor = add_days(cursor, 1)

    return {
        "days": days,
        "opening_total": opening_total,
        "closing_total": running,
        "message": ("Opening is the real GL balance the day before the range starts. "
                    "Closing = Opening + Charged - Paid + Adjustments, exactly, for every "
                    "day — it is not possible for this to fail to add up, since Closing is "
                    "computed as that sum, not checked against it."),
    }


@frappe.whitelist()
def get_ap_rollforward_drilldown(date, company=None):
    """The vouchers behind one day's movement, for the 'charged/paid/adjustments' click-through."""
    date = getdate(date)
    accounts = payable_accounts(company)
    if not accounts:
        return []

    conditions = ["gle.account IN %(accounts)s", "gle.is_cancelled = 0", "gle.posting_date = %(date)s"]
    values = {"accounts": list(accounts), "date": date}
    if company:
        conditions.append("gle.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT gle.voucher_type AS voucher_type, gle.voucher_no AS voucher_no,
               gle.party_type AS party_type, gle.party AS party, gle.account AS account,
               (gle.credit - gle.debit) AS amount, gle.remarks AS remarks
        FROM `tabGL Entry` gle
        WHERE {0}
        ORDER BY gle.voucher_type, gle.voucher_no
    """.format(" AND ".join(conditions)), values, as_dict=True)

    names = _resolve_party_names(rows)
    for r in rows:
        r["party"] = names.get((r.get("party_type"), r.get("party"))) or r.get("party")
    return rows


# ============================================================= top movers

@frappe.whitelist()
def get_ap_top_movers(from_date, to_date, company=None, limit=15):
    """
    Which suppliers moved the outstanding balance the most in this range,
    charged vs paid, ranked by the size of the swing either direction —
    the AP mirror of get_ar_top_movers, with the customer-name-resolution
    lesson (2026-08-22, A3) applied to suppliers from day one.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    accounts = payable_accounts(company)
    if not accounts:
        return []

    conditions = ["gle.account IN %(accounts)s", "gle.is_cancelled = 0",
                  "gle.posting_date BETWEEN %(from_date)s AND %(to_date)s",
                  "gle.party_type = 'Supplier'", "gle.party IS NOT NULL", "gle.party != ''"]
    values = {"accounts": list(accounts), "from_date": from_date, "to_date": to_date}
    if company:
        conditions.append("gle.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT gle.party AS supplier,
               SUM(CASE WHEN gle.voucher_type = 'Purchase Invoice' THEN gle.credit - gle.debit ELSE 0 END) AS charged,
               SUM(CASE WHEN gle.voucher_type = 'Payment Entry' THEN gle.debit - gle.credit ELSE 0 END) AS paid,
               SUM(gle.credit - gle.debit) AS net_movement,
               COUNT(*) AS n
        FROM `tabGL Entry` gle
        WHERE {0}
        GROUP BY gle.party
        ORDER BY ABS(SUM(gle.credit - gle.debit)) DESC
        LIMIT %(limit)s
    """.format(" AND ".join(conditions)), dict(values, limit=cint(limit)), as_dict=True)

    names = {}
    supplier_ids = [r.supplier for r in rows]
    if supplier_ids:
        for rec in frappe.get_all("Supplier", filters={"name": ["in", supplier_ids]},
                                   fields=["name", "supplier_name"]):
            names[rec.name] = rec.supplier_name

    return [{"supplier": r.supplier, "supplier_name": names.get(r.supplier) or r.supplier,
              "charged": flt(r.charged), "paid": flt(r.paid),
              "net_movement": flt(r.net_movement), "transactions": r.n} for r in rows]


# ============================================================= aging

BUCKETS = [(0, 30, "0-30"), (31, 60, "31-60"), (61, 90, "61-90"),
           (91, 120, "91-120"), (121, None, "120+")]


def _bucket_of(age):
    for lo, hi, label in BUCKETS:
        if age >= lo and (hi is None or age <= hi):
            return label
    return "120+"


@frappe.whitelist()
def get_ap_aging(as_of_date, company=None):
    """
    Invoice-level payables, with age buckets, as of any date — the AP
    mirror of ar_aging.py's execute(). Outstanding is rebuilt from GL
    Entry against the payable account up to the as-of date, never from
    Purchase Invoice.outstanding_amount (current-state only), the same
    rule ar_aging.py documents for its own reason.

    UNLIKE ar_aging.py, this has NOT been checked against a known
    "unmatched supplier payments" figure — no equivalent data-quality
    metric for unallocated Payment Entries against suppliers has been
    validated for this app yet. Treat the oldest buckets with the same
    caution ar_aging.py flags for AR until that check is done.
    """
    as_of = getdate(as_of_date)
    accounts = payable_accounts(company)
    if not accounts:
        return {"rows": [], "summary": [], "message": "No Payable-type accounts found for this company."}

    conditions = [
        "gle.account IN %(accounts)s",
        "gle.is_cancelled = 0",
        "gle.posting_date <= %(as_of)s",
        "(gle.voucher_type = 'Purchase Invoice' OR gle.against_voucher_type = 'Purchase Invoice')",
    ]
    values = {"accounts": list(accounts), "as_of": as_of}
    if company:
        conditions.append("gle.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT
            CASE WHEN gle.voucher_type = 'Purchase Invoice' THEN gle.voucher_no
                 ELSE gle.against_voucher END AS invoice,
            SUM(gle.credit - gle.debit) AS outstanding
        FROM `tabGL Entry` gle
        WHERE {0}
        GROUP BY invoice
        HAVING ABS(SUM(gle.credit - gle.debit)) >= 0.01
    """.format(" AND ".join(conditions)), values, as_dict=True)

    if not rows:
        return {"rows": [], "summary": [], "message": "Nothing outstanding as of {0}.".format(as_of)}

    invoice_names = [r.invoice for r in rows if r.invoice]
    invoices = {i.name: i for i in frappe.get_all(
        "Purchase Invoice", filters={"name": ["in", invoice_names]},
        fields=["name", "posting_date", "supplier", "supplier_name", "company", "base_net_total"])}

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

        bucket = _bucket_of(age)
        outstanding = flt(r.outstanding)

        data.append({
            "invoice": inv.name, "posting_date": inv.posting_date, "age": age,
            "bucket": bucket, "supplier_name": inv.supplier_name, "company": inv.company,
            "net_amount": flt(inv.base_net_total),
            "paid": flt(inv.base_net_total) - outstanding,
            "outstanding": outstanding,
        })

        total_outstanding += outstanding
        weighted_age += outstanding * age
        by_bucket[bucket] = by_bucket.get(bucket, 0.0) + outstanding

    data.sort(key=lambda d: -d["outstanding"])
    dpo = (weighted_age / total_outstanding) if total_outstanding else 0.0

    summary = [
        {"label": "Total outstanding", "value": total_outstanding, "datatype": "Currency"},
        {"label": "Weighted average age (days)", "value": round(dpo, 1), "datatype": "Float"},
        {"label": "120+ days", "value": by_bucket.get("120+", 0.0), "datatype": "Currency",
         "indicator": "Red" if by_bucket.get("120+", 0.0) > 0 else "Green"},
        {"label": "Invoices in this book", "value": len(data), "datatype": "Int"},
    ]

    message = (
        "As of {0}. Outstanding is rebuilt from GL Entry against the payable account up "
        "to this date — never from Purchase Invoice.outstanding_amount, which is "
        "current-state only. Unlike the AR Aging report, this has not yet been "
        "cross-checked against an unmatched-supplier-payments figure — treat the "
        "oldest buckets with that same caution until it has."
    ).format(as_of)

    return {"rows": data, "summary": summary, "by_bucket": by_bucket, "message": message}


# ============================================================= C1 — daily expenses

@frappe.whitelist()
def get_daily_expenses(from_date, to_date, company=None):
    """
    Daily spend on every root_type=Expense account, petty cash (accounts
    50301/50302) broken out as its own line. This is a spend total for
    the range, not a rollforward — a P&L account has no "opening/closing
    balance" the way a Balance Sheet account does; it zeroes each fiscal
    year, so there is no opening figure to show here (unlike C3).
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    acc_map = expense_account_map(company)
    if not acc_map:
        return {"days": [], "message": "No Expense-type accounts found for this company."}

    petty_cash_accounts = {name for name, num in acc_map.items()
                            if str(num or "").strip() in PETTY_CASH_ACCOUNT_NUMBERS}

    values = {"accounts": list(acc_map.keys()), "from_date": from_date, "to_date": to_date}
    company_cond = ""
    if company:
        company_cond = "AND gle.company = %(company)s"
        values["company"] = company

    movement_rows = frappe.db.sql("""
        SELECT gle.posting_date AS posting_date, gle.account AS account,
               SUM(gle.debit - gle.credit) AS net
        FROM `tabGL Entry` gle
        WHERE gle.account IN %(accounts)s AND gle.is_cancelled = 0
          AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s {company_cond}
        GROUP BY gle.posting_date, gle.account
    """.format(company_cond=company_cond), values, as_dict=True)

    by_day = {}
    for r in movement_rows:
        d = by_day.setdefault(str(r.posting_date), {"petty_cash": 0.0, "other": 0.0})
        bucket = "petty_cash" if r.account in petty_cash_accounts else "other"
        d[bucket] += flt(r.net)

    days = []
    total = 0.0
    total_petty = 0.0
    cursor = from_date
    while cursor <= to_date:
        key = str(cursor)
        m = by_day.get(key, {"petty_cash": 0.0, "other": 0.0})
        day_total = m["petty_cash"] + m["other"]
        days.append({"date": key, "petty_cash": m["petty_cash"], "other": m["other"], "total": day_total})
        total += day_total
        total_petty += m["petty_cash"]
        cursor = add_days(cursor, 1)

    return {
        "days": days, "total": total, "total_petty_cash": total_petty,
        "message": ("Every root_type = Expense account, for the exact date range shown. "
                    "Petty cash = accounts 50301/50302 specifically (confirmed 2026-08-20: "
                    "petty cash is booked as an expense category here, not a cash float)."),
    }


@frappe.whitelist()
def get_expense_day_drilldown(date, company=None):
    """The vouchers behind one day's expense total, for the C1 click-through."""
    date = getdate(date)
    acc_map = expense_account_map(company)
    if not acc_map:
        return []

    conditions = ["gle.account IN %(accounts)s", "gle.is_cancelled = 0", "gle.posting_date = %(date)s"]
    values = {"accounts": list(acc_map.keys()), "date": date}
    if company:
        conditions.append("gle.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT gle.voucher_type AS voucher_type, gle.voucher_no AS voucher_no,
               gle.party_type AS party_type, gle.party AS party, gle.account AS account,
               (gle.debit - gle.credit) AS amount, gle.remarks AS remarks
        FROM `tabGL Entry` gle
        WHERE {0}
        ORDER BY gle.voucher_type, gle.voucher_no
    """.format(" AND ".join(conditions)), values, as_dict=True)

    names = _resolve_party_names(rows)
    for r in rows:
        r["party"] = names.get((r.get("party_type"), r.get("party"))) or r.get("party")
    return rows


# ============================================================= C2 — GRN not invoiced

@frappe.whitelist()
def get_grn_not_invoiced(as_of_date, company=None):
    """
    Value of goods/assets received but not yet invoiced, as of a date —
    read directly off the live GL balance of the 'Stock Received But Not
    Billed' / 'Asset Received But Not Billed' accrual accounts. See this
    module's own docstring for why this reads the accrual account rather
    than joining Purchase Receipt directly.

    LIMITATION: this returns the aggregate balance plus the postings that
    make it up (most recent 200 first), not a per-receipt "still
    outstanding" list — attributing the balance to individual still-open
    receipts would need Purchase Receipt data, which has never been part
    of any export this app has seen. If per-receipt tracking turns out to
    matter, this needs revisiting with that doctype's data.
    """
    as_of = getdate(as_of_date)
    accounts = grn_accrual_accounts(company)
    if not accounts:
        return {"balance": 0.0, "rows": [], "message": "No GRN-accrual accounts found for this company."}

    values = {"accounts": list(accounts), "as_of": as_of}
    company_cond = ""
    if company:
        company_cond = "AND gle.company = %(company)s"
        values["company"] = company

    balance = flt(frappe.db.sql("""
        SELECT SUM(gle.credit - gle.debit) AS bal
        FROM `tabGL Entry` gle
        WHERE gle.account IN %(accounts)s AND gle.is_cancelled = 0
          AND gle.posting_date <= %(as_of)s {company_cond}
    """.format(company_cond=company_cond), values, as_dict=True)[0].bal or 0)

    rows = frappe.db.sql("""
        SELECT gle.posting_date AS posting_date, gle.voucher_type AS voucher_type,
               gle.voucher_no AS voucher_no, gle.account AS account,
               gle.debit AS debit, gle.credit AS credit, gle.remarks AS remarks
        FROM `tabGL Entry` gle
        WHERE gle.account IN %(accounts)s AND gle.is_cancelled = 0
          AND gle.posting_date <= %(as_of)s {company_cond}
        ORDER BY gle.posting_date DESC, gle.voucher_no DESC
        LIMIT 200
    """.format(company_cond=company_cond), values, as_dict=True)

    return {
        "balance": balance, "rows": rows,
        "message": ("As of {0}. Live balance of the Stock/Asset Received But Not Billed "
                    "accrual accounts — positive means goods or assets have been received "
                    "and posted but not yet invoiced by the supplier. Showing the most "
                    "recent 200 postings behind this balance, most recent first.").format(as_of),
    }


# ============================================================= C4 — supplier payments

@frappe.whitelist()
def get_supplier_payments(from_date, to_date, company=None):
    """
    Every submitted Payment Entry of type 'Pay' against a Supplier party
    in the range, grouped by supplier, plus the flat voucher list.
    "Supplier payments today" is just this called with
    from_date == to_date == today.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    conditions = [
        "pe.docstatus = 1", "pe.party_type = 'Supplier'", "pe.payment_type = 'Pay'",
        "pe.posting_date BETWEEN %(from_date)s AND %(to_date)s",
    ]
    values = {"from_date": from_date, "to_date": to_date}
    if company:
        conditions.append("pe.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT pe.name AS payment_entry, pe.posting_date AS posting_date,
               pe.party AS supplier, pe.paid_amount AS paid_amount,
               pe.mode_of_payment AS mode_of_payment, pe.reference_no AS reference_no,
               pe.company AS company
        FROM `tabPayment Entry` pe
        WHERE {0}
        ORDER BY pe.posting_date DESC, pe.name
    """.format(" AND ".join(conditions)), values, as_dict=True)

    names = {}
    supplier_ids = [r.supplier for r in rows if r.supplier]
    if supplier_ids:
        for rec in frappe.get_all("Supplier", filters={"name": ["in", supplier_ids]},
                                   fields=["name", "supplier_name"]):
            names[rec.name] = rec.supplier_name

    total = 0.0
    by_supplier = {}
    for r in rows:
        r["supplier_name"] = names.get(r.supplier) or r.supplier
        total += flt(r.paid_amount)
        by_supplier[r["supplier_name"]] = by_supplier.get(r["supplier_name"], 0.0) + flt(r.paid_amount)

    top_suppliers = sorted(
        [{"supplier_name": k, "paid": v} for k, v in by_supplier.items()],
        key=lambda x: -x["paid"])

    return {
        "rows": rows, "total": total, "by_supplier": top_suppliers,
        "message": ("Payment Entries of type 'Pay' against Supplier parties, posting date "
                    "in range, submitted only (drafts excluded)."),
    }