# Copyright (c) 2026, Rasiin Technology and contributors
# For license information, please see license.txt

"""
Operational Reports — Page 2: Cash & Collections.

Path: rasiin_insights/management_dashboard/utils/operational_api_cash.py

WHY THIS FILE IS SEPARATE FROM operational_api_receivable.py
    Not an accident of naming, and not "the receivable one" despite the
    generic old filename it used to have (operational_api.py, renamed
    2026-08-22 for exactly this reason — see that file's own docstring).
    Page 1 (Receivables & Revenue) answers "what do we bill and what's
    still owed", reading GL Entry against Receivable accounts and Sales
    Invoice Item. Page 2 (this file) answers "what actually came in the
    door as cash", reading GL Entry against Cash/Bank accounts, Sales
    Invoice Payment, and Payment Entry. Different accounts, different
    doctypes, different questions finance asks at different points in the
    day — billing and collecting are not the same ledger movement (a bill
    can sit unpaid for months; cash can arrive against a bill from a
    different day entirely, which is the entire subject of D1 below).
    Splitting them the way ar_aging.py and till_reconciliation.py are
    already split (two Script Reports, not one) keeps each file's queries
    about one thing. Page 3 (Expenses & Payables, not yet built) will get
    its own operational_api_payables.py on the same pattern rather than
    being bolted onto either of these.

WHERE COLLECTIONS ACTUALLY LIVE (found by inspecting real data, not assumed)
    Payment Entry.mode_of_payment is blank on ~98% of rows. Day-to-day
    patient collections are recorded on the Sales Invoice itself, in its
    Sales Invoice Payment child table (POS payment lines) — that is where
    the real mode-of-payment values live, and they are not generic
    categories, they are specific accounts, almost always one dedicated
    account per cashier (e.g. "Merchant Acc 701228"). Payment Entry is
    used for the smaller volume of Receive-type receipts that get
    explicitly allocated against older invoices, plus Pay-type (supplier/
    refund) transactions.

D1 — REWRITTEN 2026-08-22 TO MATCH THE CEO DASHBOARD'S OWN LOGIC EXACTLY
    The previous version of this function invented its own "same calendar
    day = sales-cash" rule from Sales Invoice Payment + Payment Entry
    Reference alone — it never looked at Journal Entry cash inflows at
    all, and never scaled down a Payment Entry that was allocated for
    more than the cash it actually received. That made this page's
    "Total collected" run about $29,600 short of the CEO dashboard's
    "Money received" for the same January range ($1,391,380.68 here vs
    $1,420,987 there) — a real gap from using different rules on the same
    ledger, not a rounding difference, and not something two people
    should ever have to "pick which one to believe" about.

    This now replicates extract.py's build_collection_facts() rule for
    rule — the exact logic that already feeds the CEO dashboard's own
    Money-In breakdown — generalised from "the calendar-month period" to
    "the from/to date range the user picked on this page":
      - every debit to a Cash/Bank account that is NOT also matched by a
        credit to a Cash/Bank account on the same voucher (that credit
        leg means the voucher is an internal transfer — cash moving
        between our own accounts, not new money coming in the door —
        fetch_cash_movements' is_internal EXISTS subquery, same rule,
        just bound to a date range instead of a period)
      - Sales Invoice cash -> always "sales-cash" for this range, no
        date comparison needed: a POS payment line is posted in the same
        voucher as the sale, so it cannot be from a different day
      - Payment Entry cash -> split allocation by allocation: sales-cash
        if the referenced invoice's posting_date falls inside the
        selected range, debt-cash if it falls before the range started,
        with the same overallocation scaling factor
        (cash / allocated, applied when allocated > cash) the CEO
        dashboard's own fact-builder uses, so a payment allocated for
        more than what actually cleared never overstates either bucket
      - Journal Entry cash -> sales-cash if linked to a Sales Invoice via
        the sales_invoice field, unallocated otherwise
    Validated to the cent against the CEO dashboard's own printed January
    bridge (This period's invoices 905,347 / Older debt 66 / Not matched
    515,574 / Money received 1,420,987) — see validate_d1_v2.py and the
    project's mismatch-fix-v2 doc for the full run and numbers.

D3 — THE CASHIER -> MAIN MERCHANT -> BANK CHAIN
    Traced from real GL Entry data, not from any field that says so
    explicitly: each cashier's dedicated merchant account is swept into
    "Main Merchant - 612855558 - SH" (~$1M/month across ~300+ Journal
    Entries), which is in turn swept into the hospital's main bank
    account. till_reconciliation.py already computes the per-account
    daily rollforward this needs (including the same is_internal sweep
    detection) — this module calls it directly and adds the cashier-name
    labelling via the POS Profile chain, rather than re-deriving the
    rollforward a second time.
"""

import frappe
from frappe.utils import flt, getdate, add_days, cint

from rasiin_insights.management_dashboard.utils.extract import cash_bank_accounts
from rasiin_insights.management_dashboard.report.till_reconciliation.till_reconciliation import (
    execute as till_execute,
)


# ================================================================ D1 — split

def _resolve_party_names(rows):
    """
    Bulk name lookup for a set of GL Entry rows carrying party_type/party
    — the exact batched-by-5000 pattern extract.py's build_balance_facts()
    already uses for this, not a per-row frappe.db.get_value() and not
    GL Entry's own party_name-style field (unreliably populated in this
    data). Returns {(party_type, party): name}. `rows` may be frappe._dict
    rows (from frappe.db.sql(..., as_dict=True)) or plain dicts — both
    support .get().
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


def _fetch_cash_movements_range(from_date, to_date, company=None):
    """
    Same rule as extract.py's fetch_cash_movements() — every posting to a
    Cash/Bank account, flagged for internal transfers via the identical
    EXISTS subquery — bound to an arbitrary [from_date, to_date] range
    instead of a fixed YYYY-MM period, since this page lets finance pick
    any range, not just a calendar month.
    """
    conditions = [
        "gle.posting_date BETWEEN %(from_date)s AND %(to_date)s",
        "gle.is_cancelled = 0",
        "acc.account_type IN ('Cash', 'Bank')",
    ]
    values = {"from_date": from_date, "to_date": to_date, "company": company}
    if company:
        conditions.append("gle.company = %(company)s")

    return frappe.db.sql("""
        SELECT
            gle.name          AS gl_name,
            gle.company       AS company,
            gle.posting_date  AS posting_date,
            gle.voucher_type  AS voucher_type,
            gle.voucher_no    AS voucher_no,
            gle.account       AS account,
            gle.debit         AS debit,
            gle.credit        AS credit,
            gle.party_type    AS party_type,
            gle.party         AS party,
            EXISTS (
                SELECT 1
                FROM `tabGL Entry` g2
                INNER JOIN `tabAccount` a2 ON a2.name = g2.account
                WHERE g2.voucher_type = gle.voucher_type
                  AND g2.voucher_no   = gle.voucher_no
                  AND g2.is_cancelled = 0
                  AND g2.credit > 0
                  AND a2.account_type IN ('Cash', 'Bank')
            ) AS is_internal
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE {conditions}
        ORDER BY gle.posting_date, gle.voucher_no
    """.format(conditions=" AND ".join(conditions)), values, as_dict=True)


def _cash_collection_rows(from_date, to_date, company=None):
    """
    One row per allocation of real cash-in, bucketed sales_cash (booked
    against an invoice dated inside [from_date, to_date]) / debt_cash
    (against an invoice from before the range) / unallocated (not tied to
    any invoice) — mirrors extract.py's build_collection_facts() branching
    exactly: Sales Invoice cash is always this-range; Payment Entry
    allocated amounts split by whether the referenced invoice's date
    falls in range, with the same overallocation scaling factor; Journal
    Entry cash counts as this-range if linked to a Sales Invoice via its
    sales_invoice field, unallocated otherwise. "Current" is redefined
    from "this calendar-month period" to "this selected date range" so
    the same rule generalises to any range finance picks, not just a
    full month — validated to the cent against a full month (see the
    module docstring's D1 note).
    """
    rows = _fetch_cash_movements_range(from_date, to_date, company)
    money_in = [r for r in rows if flt(r.debit) > 0 and not r.is_internal]

    pe_names = [r.voucher_no for r in money_in if r.voucher_type == "Payment Entry"]
    refs_by_pe = {}
    if pe_names:
        for ref in frappe.get_all(
            "Payment Entry Reference",
            filters={"parent": ["in", pe_names], "reference_doctype": "Sales Invoice"},
            fields=["parent", "reference_name", "allocated_amount"],
        ):
            refs_by_pe.setdefault(ref.parent, []).append(ref)

    inv_names = {r.voucher_no for r in money_in if r.voucher_type == "Sales Invoice"}
    ref_inv_names = {ref.reference_name for refs in refs_by_pe.values() for ref in refs}
    all_inv_names = list(inv_names | ref_inv_names)
    inv_by_name = {}
    if all_inv_names:
        for inv in frappe.get_all(
            "Sales Invoice", filters={"name": ["in", all_inv_names]},
            fields=["name", "posting_date", "customer_name"],
        ):
            inv_by_name[inv.name] = inv

    # FIXED 2026-08-22: base["party"] used to be gle.party as-is — a raw ID
    # like CUST-2026-212009 for most Customer rows (some Supplier/Insurance
    # records happen to be named human-readably, most Customer records
    # aren't). Resolved in bulk now, same pattern as A3's Top Receivable
    # Movers and A1's rollforward drilldown fix.
    party_names = _resolve_party_names(money_in)

    out = []
    for r in money_in:
        cash = flt(r.debit)
        resolved_party = party_names.get((r.party_type, r.party)) or r.party
        base = {"date": str(r.posting_date), "voucher_type": r.voucher_type,
                "voucher": r.voucher_no, "account": r.account,
                "party": resolved_party, "mode_of_payment": None}

        # ---------------------------------------------------- POS payments
        if r.voucher_type == "Sales Invoice":
            inv = inv_by_name.get(r.voucher_no)
            out.append(dict(base, bucket="sales_cash", amount=cash,
                             against_invoice=r.voucher_no,
                             party=base["party"] or getattr(inv, "customer_name", None)))
            continue

        # ------------------------------------------------- Payment Entries
        if r.voucher_type == "Payment Entry":
            refs = refs_by_pe.get(r.voucher_no, [])
            allocated = sum(flt(x.allocated_amount) for x in refs)
            factor = (cash / allocated) if (allocated > cash and allocated > 0) else 1.0

            used = 0.0
            for x in refs:
                amt = flt(x.allocated_amount) * factor
                if not amt:
                    continue
                inv = inv_by_name.get(x.reference_name)
                inv_date = getattr(inv, "posting_date", None) if inv else None
                is_current = bool(inv_date) and getdate(from_date) <= getdate(inv_date) <= getdate(to_date)
                out.append(dict(base, bucket="sales_cash" if is_current else "debt_cash",
                                 amount=amt, against_invoice=x.reference_name))
                used += amt

            remainder = cash - used
            if remainder > 0.005:
                out.append(dict(base, bucket="unallocated", amount=remainder, against_invoice=None))
            continue

        # -------------------------------------------------- Journal Entries
        linked = None
        if r.voucher_type == "Journal Entry":
            linked = frappe.db.get_value("Journal Entry", r.voucher_no, "sales_invoice")
        out.append(dict(base, bucket="sales_cash" if linked else "unallocated",
                         amount=cash, against_invoice=linked))

    return out


@frappe.whitelist()
def get_cash_collection_split(from_date, to_date, company=None):
    """
    Daily: sales-cash (this range's own invoices) vs debt-cash (collecting
    on older invoices) vs unallocated (received but not yet matched to any
    invoice — kept as its own visible bucket, never folded into either
    side, precisely because of January's unmatched-receipts history).

    Same rule the CEO dashboard's Money-In bridge uses — see the D1 note
    at the top of this file. "Total collected" here will now match the
    CEO dashboard's "Money received" for the same company and a full
    calendar-month range, to the cent.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    rows = _cash_collection_rows(from_date, to_date, company)

    by_day = {}

    def bucket(date):
        return by_day.setdefault(date, {
            "sales_cash": 0.0, "debt_cash": 0.0, "unallocated": 0.0})

    for r in rows:
        bucket(r["date"])[r["bucket"]] += r["amount"]

    days = []
    cursor = from_date
    while cursor <= to_date:
        key = str(cursor)
        d = by_day.get(key, {"sales_cash": 0.0, "debt_cash": 0.0, "unallocated": 0.0})
        d = dict(d, date=key, total=d["sales_cash"] + d["debt_cash"] + d["unallocated"])
        days.append(d)
        cursor = add_days(cursor, 1)

    totals = {
        "sales_cash": sum(d["sales_cash"] for d in days),
        "debt_cash": sum(d["debt_cash"] for d in days),
        "unallocated": sum(d["unallocated"] for d in days),
    }
    totals["total"] = totals["sales_cash"] + totals["debt_cash"] + totals["unallocated"]

    return {
        "days": days, "totals": totals,
        "message": ("Sales-cash = collected against an invoice dated inside this range "
                    "(every POS payment line qualifies automatically — it's posted in "
                    "the same voucher as the sale). Debt-cash = collected, via Payment "
                    "Entry, against an invoice from before this range. Unallocated = "
                    "received but not yet matched to any invoice — watch this bucket; "
                    "it was 97% of January's receipts before the allocation backlog "
                    "was worked through. This is the same rule — including Journal "
                    "Entry cash and Payment-Entry overallocation scaling — behind the "
                    "CEO dashboard's own Money Received figure, so the total below "
                    "reconciles with it for a full-month range."),
    }


@frappe.whitelist()
def get_cash_collection_drilldown(from_date, to_date, date, company=None):
    """
    The individual vouchers behind one day's sales-cash/debt-cash/
    unallocated split, using the SAME [from_date, to_date] range the
    summary table was built from (so a click on one day inside a 3-month
    range shows that day's vouchers bucketed by "is the invoice inside
    the 3 months", not by "is the invoice from that exact day") — kept in
    sync with get_cash_collection_split by both calling the same
    _cash_collection_rows() helper.
    """
    date = getdate(date)
    rows = _cash_collection_rows(getdate(from_date), getdate(to_date), company)
    return [r for r in rows if r["date"] == str(date)]


# ============================================================= D2 — by mode

@frappe.whitelist()
def get_collections_by_mode(from_date, to_date, company=None):
    """
    Collections for the range, by cashier/merchant account (POS payments,
    the overwhelming majority) and by Payment Entry mode where set.
    Cashier name resolved via Sales Invoice.pos_profile -> POS Profile
    User, not by reverse-matching the account name.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    company_cond = "AND si.company = %(company)s" if company else ""
    values = {"from_date": from_date, "to_date": to_date, "company": company}

    pos_rows = frappe.db.sql("""
        SELECT si.pos_profile AS pos_profile, sip.mode_of_payment AS mode_of_payment,
               COUNT(*) AS n, SUM(sip.base_amount) AS amount
        FROM `tabSales Invoice Payment` sip
        INNER JOIN `tabSales Invoice` si ON si.name = sip.parent AND sip.parenttype = 'Sales Invoice'
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
          {company_cond}
        GROUP BY si.pos_profile, sip.mode_of_payment
        ORDER BY SUM(sip.base_amount) DESC
    """.format(company_cond=company_cond), values, as_dict=True)

    profile_user = {r.parent: r.user for r in frappe.get_all(
        "POS Profile User", fields=["parent", "user"])}

    data = [{
        "pos_profile": r.pos_profile or "(not set)",
        "cashier": profile_user.get(r.pos_profile, ""),
        "mode_of_payment": r.mode_of_payment,
        "transactions": r.n, "amount": flt(r.amount),
    } for r in pos_rows]

    total = sum(d["amount"] for d in data)
    for d in data:
        d["share"] = (d["amount"] / total) if total else 0.0

    return {
        "rows": data, "total": total,
        "message": ("POS payment lines only ({0} to {1}) — Payment Entry's own "
                    "mode_of_payment is blank on almost every row, so it isn't a "
                    "useful second source here.").format(from_date, to_date),
    }


@frappe.whitelist()
def get_collections_by_mode_drilldown(from_date, to_date, pos_profile, mode_of_payment, company=None):
    """The individual POS payment lines behind one (pos_profile, mode_of_payment) row."""
    from_date, to_date = getdate(from_date), getdate(to_date)
    company_cond = "AND si.company = %(company)s" if company else ""
    pos_profile_cond = "AND si.pos_profile = %(pos_profile)s" if pos_profile and pos_profile != "(not set)" else "AND (si.pos_profile IS NULL OR si.pos_profile = '')"
    values = {"from_date": from_date, "to_date": to_date, "company": company,
               "pos_profile": pos_profile, "mode_of_payment": mode_of_payment}

    rows = frappe.db.sql("""
        SELECT si.name AS invoice, si.posting_date AS posting_date, si.customer_name AS customer,
               sip.account AS account, sip.base_amount AS amount
        FROM `tabSales Invoice Payment` sip
        INNER JOIN `tabSales Invoice` si ON si.name = sip.parent AND sip.parenttype = 'Sales Invoice'
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
          AND sip.mode_of_payment = %(mode_of_payment)s
          {pos_profile_cond} {company_cond}
        ORDER BY si.posting_date DESC, sip.base_amount DESC
    """.format(pos_profile_cond=pos_profile_cond, company_cond=company_cond), values, as_dict=True)

    return [{"invoice": r.invoice, "posting_date": str(r.posting_date), "customer": r.customer,
              "account": r.account, "amount": flt(r.amount)} for r in rows]


# ==================================================== D3 — cashier reconciliation

@frappe.whitelist()
def get_cashier_reconciliation(from_date, to_date, company=None):
    """
    till_reconciliation.py's own rollforward, enriched with cashier
    names and a same-day-swept flag — NOT a second computation of the
    rollforward itself.
    """
    columns, data, message, chart, summary = till_execute(
        {"from_date": from_date, "to_date": to_date, "company": company})

    account_to_cashier = _account_to_cashier_map()
    main_merchant_accounts = {a for a in cash_bank_accounts(company) if "main merchant" in a.lower()}
    bank_accounts = set(frappe.get_all(
        "Account", filters={"account_type": "Bank", **({"company": company} if company else {})},
        pluck="name"))

    out = []
    for row in data:
        acct = row["account"]
        role = ("bank" if acct in bank_accounts
                else "main_cashier" if acct in main_merchant_accounts
                else "cashier")
        cashier = account_to_cashier.get(acct, "")
        swept_clean = None
        if role == "cashier":
            swept_clean = abs(row["closing"]) < 1.0
        out.append(dict(row, role=role, cashier=cashier, swept_clean=swept_clean))

    unswept_today = [r for r in out
                      if r["role"] == "cashier" and r["posting_date"] == getdate(to_date)
                      and r["swept_clean"] is False]

    return {
        "rows": out,
        "unswept_today": unswept_today,
        "message": (message + " 'Swept clean' only applies to individual cashier "
                    "tills — Main Merchant and bank accounts are expected to carry "
                    "a real running balance, not zero out daily."),
    }


@frappe.whitelist()
def get_till_drilldown(account, date, company=None):
    """The GL vouchers behind one till's one-day movement — same conditions
    till_reconciliation.py itself uses to classify collections_in/swept_out/other_out."""
    date = getdate(date)
    conditions = ["gle.account = %(account)s", "gle.is_cancelled = 0", "gle.posting_date = %(date)s"]
    values = {"account": account, "date": date}
    if company:
        conditions.append("gle.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT gle.voucher_type, gle.voucher_no, gle.party, gle.party_type,
               gle.debit, gle.credit, gle.remarks,
               EXISTS (
                   SELECT 1 FROM `tabGL Entry` g2
                   INNER JOIN `tabAccount` a2 ON a2.name = g2.account
                   WHERE g2.voucher_type = gle.voucher_type
                     AND g2.voucher_no = gle.voucher_no
                     AND g2.is_cancelled = 0 AND g2.credit > 0
                     AND a2.account_type IN ('Cash', 'Bank')
               ) AS is_internal
        FROM `tabGL Entry` gle
        WHERE {0}
        ORDER BY ABS(gle.debit - gle.credit) DESC
    """.format(" AND ".join(conditions)), values, as_dict=True)

    # FIXED 2026-08-22: gle.party as-is showed raw IDs for most Customer
    # rows — same fix as _cash_collection_rows and A1's rollforward
    # drilldown, resolved in bulk instead.
    names = _resolve_party_names(rows)

    out = []
    for r in rows:
        if flt(r.debit):
            kind = "collection_in"
        elif r.is_internal:
            kind = "swept_out"
        else:
            kind = "other_out"
        out.append({"voucher_type": r.voucher_type, "voucher_no": r.voucher_no,
                    "party": names.get((r.party_type, r.party)) or r.party,
                    "amount": flt(r.debit) - flt(r.credit), "kind": kind,
                    "remarks": (r.remarks or "")[:140]})
    return out


def _account_to_cashier_map():
    """
    account name -> cashier user, via POS Profile -> POS Payment Method ->
    Mode of Payment Account.

    BUG FIXED HERE (2026-08-22): 'Mode of Payment Account' is a child table
    whose own row does NOT carry a 'mode_of_payment' field — the mode of
    payment it belongs to is its PARENT document's name (parentfield
    'accounts' on the 'Mode of Payment' doctype). The original code asked
    frappe.get_all for a 'mode_of_payment' field that doesn't exist on this
    child doctype; a later edit swapped it for 'company' to stop the crash,
    which silently collapsed the whole map to a single {None: <last row's
    account>} entry — every cashier name on the live Till Reconciliation
    table came back blank as a result. Keying by 'parent' (the Mode of
    Payment name, matching POS Payment Method.mode_of_payment) is correct —
    verified against the real POS Payment Method / Mode of Payment Account
    export: 21/21 accounts resolved to a cashier in both January and July.
    """
    profile_user = {r.parent: r.user for r in frappe.get_all(
        "POS Profile User", fields=["parent", "user"])}
    payment_methods = frappe.get_all(
        "POS Payment Method", fields=["parent", "mode_of_payment"])
    mop_accounts = {r.parent: r.default_account for r in frappe.get_all(
        "Mode of Payment Account", fields=["parent", "default_account"])}

    out = {}
    for pm in payment_methods:
        account = mop_accounts.get(pm.mode_of_payment)
        user = profile_user.get(pm.parent)
        if account and user:
            out[account] = user
    return out


# ========================================================= E1 — cash & bank

@frappe.whitelist()
def get_cash_bank_position(from_date, to_date, company=None):
    """
    Daily cash + bank position: one aggregate rollforward line across
    every Cash/Bank account (reusing till_execute's per-account rows,
    same guarantee as Page 1 — this is a different GROUP BY over the
    same numbers, not a re-derivation), plus the Cash-only and Bank-only
    split, plus the per-account table for drill-down.
    """
    columns, data, message, chart, summary = till_execute(
        {"from_date": from_date, "to_date": to_date, "company": company})

    bank_accounts = set(frappe.get_all(
        "Account", filters={"account_type": "Bank", **({"company": company} if company else {})},
        pluck="name"))

    by_day = {}
    for row in data:
        key = str(row["posting_date"])
        d = by_day.setdefault(key, {"cash_closing": 0.0, "bank_closing": 0.0,
                                     "in": 0.0, "out": 0.0})
        is_bank = row["account"] in bank_accounts
        d["bank_closing" if is_bank else "cash_closing"] += flt(row["closing"])
        d["in"] += flt(row["collections_in"])
        d["out"] += flt(row["swept_out"]) + flt(row["other_out"])

    days = sorted(
        [{"date": k, "cash_closing": v["cash_closing"], "bank_closing": v["bank_closing"],
          "total_closing": v["cash_closing"] + v["bank_closing"],
          "in": v["in"], "out": v["out"]} for k, v in by_day.items()],
        key=lambda d: d["date"])

    return {
        "days": days, "by_account": data,
        "message": ("Aggregated from the same per-account rollforward Till "
                    "Reconciliation uses — cash_closing + bank_closing on any "
                    "day is the true total cash+bank position, GL-tied the same "
                    "way every other rollforward in this app is."),
    }