# Copyright (c) 2026, Rasiin Technology and contributors
# For license information, please see license.txt

"""
Operational Reports — Page 1: Receivables & Revenue.

Path: rasiin_insights/management_dashboard/utils/operational_api_receivable.py

RENAMED 2026-08-22 (was operational_api.py)
    Reasonable question raised: "we have operational_api_cash.py and
    operational_api.py — why not the same file, or why isn't this one
    named for what it actually is?" It was never meant to be "the only
    other one" — Page 1 (Receivables & Revenue) and Page 2 (Cash &
    Collections, operational_api_cash.py) read different accounts for
    different questions (what's billed/owed vs what physically came in
    as cash — see operational_api_cash.py's own docstring for the full
    reasoning) and were always going to stay two files on the same
    pattern ar_aging.py / till_reconciliation.py already use. The generic
    name was just a leftover from before the second file existed to
    contrast it with. Renamed to say what it is, not what it isn't.

WHO THIS IS FOR
    Finance and accountants closing the day, not the CEO looking at a
    trend. Every function here answers "as of right now / today / this
    range" and reads live from GL Entry, Sales Invoice and Sales Invoice
    Item — never Management Snapshot (monthly-grain only, by design) and
    never Management Fact (a hourly-refreshed COPY built for the CEO
    dashboard's own dimensions, not a live source). See
    daily-filtering-and-next-steps.md for why that boundary exists.

WHY THIS DOES NOT DUPLICATE ar_aging.py / till_reconciliation.py
    Where an existing Script Report already computes something correctly
    (invoice-level outstanding, cash-account rollforwards), this module
    imports and calls that report's own execute()/helper functions
    directly rather than re-deriving the same SQL a second time. A
    Script Report's execute(filters) is a plain importable Python
    function — nothing about the "Script Report" doctype stops it being
    called from elsewhere. Two independent queries computing "the same"
    number is exactly the kind of drift this whole build has been trying
    to design out from the start.

RECONCILIATION GUARANTEE
    get_ar_rollforward's daily closing figure is opening + the SIGNED net
    GL movement on the receivable accounts for that day, full stop — the
    same identity a real ledger account always satisfies. The "Billed /
    Collected / Adjustments" breakdown is a read of that same movement,
    categorised by voucher_type for readability. Relabelling it never
    changes the total, so it cannot silently drift from get_revenue_by_item_group
    or from the standalone AR Aging report — they all read the same GL
    Entry rows for the same accounts.
"""

import frappe
from frappe.utils import flt, getdate, add_days, cint

from rasiin_insights.management_dashboard.report.ar_aging.ar_aging import (
    receivable_accounts,
    execute as ar_aging_execute,
)
from rasiin_insights.management_dashboard.utils.resolve import DimensionResolver


# ============================================================= A1 — rollforward

@frappe.whitelist()
def get_ar_rollforward(from_date, to_date, company=None):
    """
    Daily opening -> billed -> collected -> adjustments -> closing, summed
    across every Receivable-type account, one row per calendar day.

    Categorisation (by voucher_type, on the receivable-account leg only):
        Billed       Sales Invoice postings (a return invoice's negative
                     amount already nets in here — it is still a Sales
                     Invoice voucher, ERPNext stores return amounts
                     negative, nothing here special-cases it)
        Collected    Payment Entry postings, shown positive when they
                     reduce the balance (the normal case)
        Adjustments  Journal Entry postings — write-offs, patient/insurer
                     balance transfers, opening entries
        Other        anything else touching a receivable account

    closing = opening + billed_net + collected_net + adjustment_net + other_net
    using the RAW signed net movement for each bucket — the identity
    holds by construction, not by the labels chosen for display.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    accounts = receivable_accounts(company)
    if not accounts:
        return {"days": [], "message": "No Receivable-type accounts found for this company."}

    values = {"accounts": list(accounts), "from_date": from_date}
    company_cond = ""
    if company:
        company_cond = "AND gle.company = %(company)s"
        values["company"] = company

    opening_total = flt(frappe.db.sql("""
        SELECT SUM(gle.debit - gle.credit) AS bal
        FROM `tabGL Entry` gle
        WHERE gle.account IN %(accounts)s AND gle.is_cancelled = 0
          AND gle.posting_date < %(from_date)s {company_cond}
    """.format(company_cond=company_cond), values, as_dict=True)[0].bal or 0)

    values2 = dict(values, to_date=to_date)
    movement_rows = frappe.db.sql("""
        SELECT gle.posting_date AS posting_date, gle.voucher_type AS voucher_type,
               SUM(gle.debit - gle.credit) AS net
        FROM `tabGL Entry` gle
        WHERE gle.account IN %(accounts)s AND gle.is_cancelled = 0
          AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s {company_cond}
        GROUP BY gle.posting_date, gle.voucher_type
    """.format(company_cond=company_cond), values2, as_dict=True)

    BUCKET_OF = {"Sales Invoice": "billed", "Payment Entry": "collected", "Journal Entry": "adjustments"}
    by_day = {}
    for r in movement_rows:
        d = by_day.setdefault(str(r.posting_date),
                               {"billed": 0.0, "collected": 0.0, "adjustments": 0.0, "other": 0.0})
        d[BUCKET_OF.get(r.voucher_type, "other")] += flt(r.net)

    days = []
    running = opening_total
    cursor = from_date
    while cursor <= to_date:
        key = str(cursor)
        m = by_day.get(key, {"billed": 0.0, "collected": 0.0, "adjustments": 0.0, "other": 0.0})
        net = m["billed"] + m["collected"] + m["adjustments"] + m["other"]
        closing = running + net
        days.append({
            "date": key,
            "opening": running,
            "billed": m["billed"],
            "collected": -m["collected"],   # display positive = money that reduced AR
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
                    "Closing = Opening + Billed - Collected + Adjustments, exactly, "
                    "for every day — it is not possible for this to fail to add up, "
                    "since Closing is computed as that sum, not checked against it."),
    }


def _resolve_party_names(rows):
    """
    Bulk name lookup for a set of GL Entry rows carrying party_type/party
    — the exact batched-by-5000 pattern extract.py's build_balance_facts()
    already uses for this, not a per-row frappe.db.get_value() and not
    GL Entry's own party_name-style field (unreliably populated in this
    data — the reason A3's Top Receivable Movers had this same fix
    2026-08-22). Returns {(party_type, party): name}.
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


@frappe.whitelist()
def get_ar_rollforward_drilldown(date, company=None):
    """The vouchers behind one day's movement, for the 'billed/collected/adjustments' click-through."""
    date = getdate(date)
    accounts = receivable_accounts(company)
    if not accounts:
        return []

    conditions = ["gle.account IN %(accounts)s", "gle.is_cancelled = 0",
                  "gle.posting_date = %(date)s"]
    values = {"accounts": list(accounts), "date": date}
    if company:
        conditions.append("gle.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT gle.voucher_type, gle.voucher_no, gle.party, gle.party_type,
               gle.debit, gle.credit, gle.remarks, gle.account
        FROM `tabGL Entry` gle
        WHERE {0}
        ORDER BY gle.voucher_type, ABS(gle.debit - gle.credit) DESC
    """.format(" AND ".join(conditions)), values, as_dict=True)

    # FIXED 2026-08-22: this used to show gle.party as-is — a raw ID like
    # CUST-2026-212009 for the majority of rows where the party isn't
    # already a human-readable name (some Supplier/Insurance records are
    # named that way, most Customer records aren't). Resolved in bulk now,
    # falling back to the raw ID only when no master record matches it.
    names = _resolve_party_names(rows)

    return [{
        "voucher_type": r.voucher_type, "voucher_no": r.voucher_no,
        "party": names.get((r.party_type, r.party)) or r.party,
        "account": r.account,
        "amount": flt(r.debit) - flt(r.credit),
        "remarks": (r.remarks or "")[:140],
    } for r in rows]


def _ar_true_closing_balance(as_of_date, company=None):
    """
    The true net GL balance across every Receivable account as of a date
    — SUM(debit - credit) over every posting up to AND INCLUDING that
    date. The identical identity get_ar_rollforward's opening_total
    already uses for "balance going into the range" (there it's
    posting_date < from_date; here it's posting_date <= as_of_date,
    since this wants the balance AT that date, not the day before it).
    This is the number that always ties to the CEO dashboard's "Owed to
    us" / ar_closing figure for the same date and company — it is the
    ledger, full stop, not an invoice-level reconstruction of it.
    """
    accounts = receivable_accounts(company)
    if not accounts:
        return 0.0
    values = {"accounts": list(accounts), "as_of": as_of_date}
    company_cond = ""
    if company:
        company_cond = "AND gle.company = %(company)s"
        values["company"] = company
    return flt(frappe.db.sql("""
        SELECT SUM(gle.debit - gle.credit) AS bal
        FROM `tabGL Entry` gle
        WHERE gle.account IN %(accounts)s AND gle.is_cancelled = 0
          AND gle.posting_date <= %(as_of)s {company_cond}
    """.format(company_cond=company_cond), values, as_dict=True)[0].bal or 0)


# ============================================================ A2 — patient type

@frappe.whitelist()
def get_ar_by_patient_type(as_of_date, company=None):
    """
    Today's (or any date's) outstanding, split OPD vs IPD vs Unclassified,
    with the full patient-level invoice list for drill-down/CSV.

    Calls ar_aging.execute() directly rather than re-querying GL Entry a
    second time — ar_aging already rebuilds outstanding correctly (from
    the ledger, not the stale Sales Invoice.outstanding_amount field) and
    already resolves Patient Type per invoice.

    BRIDGE — added 2026-08-22
    ar_aging (and so this summary) can only ever show outstanding at
    INVOICE grain — it groups GL Entry rows by which Sales Invoice they
    are against. A receipt that came in but was never allocated to any
    specific invoice (a Payment Entry with an unallocated remainder, most
    of them — see the module note in ar_aging.py itself) still reduces
    the customer's real ledger balance, but not any single invoice's, so
    it never appears as a reduction anywhere in this invoice-level sum.
    The sum of "still owed per invoice" can therefore run well ahead of
    what the ledger actually says is outstanding, and the gap gets larger
    every month unmatched receipts pile up, not smaller — which is
    exactly what produced the ~$824k this page showed "as of today"
    against the true ~$96k balance. `bridge` below computes the TRUE net
    ledger balance the same live way get_ar_rollforward does, so the two
    can be shown side by side with a real number for the difference
    instead of a mismatch nobody can explain.
    """
    columns, data, message, chart, summary = ar_aging_execute(
        {"as_of_date": as_of_date, "company": company})

    by_type = {}
    for row in data:
        t = row.get("patient_type") or "Unclassified"
        b = by_type.setdefault(t, {"patient_type": t, "outstanding": 0.0,
                                    "invoice_count": 0, "patient_count": set()})
        b["outstanding"] += flt(row["outstanding"])
        b["invoice_count"] += 1
        b["patient_count"].add(row["customer_name"])

    summary_rows = sorted(
        [{"patient_type": v["patient_type"], "outstanding": v["outstanding"],
          "invoice_count": v["invoice_count"], "patient_count": len(v["patient_count"])}
         for v in by_type.values()],
        key=lambda x: -x["outstanding"])

    invoice_level_total = sum(v["outstanding"] for v in by_type.values())
    true_closing = _ar_true_closing_balance(getdate(as_of_date), company)
    unmatched = invoice_level_total - true_closing

    return {
        "as_of_date": as_of_date,
        "summary": summary_rows,
        "drilldown": data,   # already invoice/patient level — feed straight to the table + CSV
        "message": message,
        "bridge": {
            "invoice_level_total": invoice_level_total,
            "true_closing_balance": true_closing,
            "unmatched_receipts": unmatched,
            "message": (
                "The cards above are the sum of what's still owed against each "
                "specific invoice we can trace — that's what invoice-level aging "
                "can ever show. The true net receivable balance on the ledger "
                "right now, the same number the CEO dashboard's \"Owed to us\" "
                "reads, is the figure below it. The difference is real cash "
                "already received and already reducing the customer's balance — "
                "it just was never allocated against one specific invoice, so no "
                "single invoice's outstanding ever dropped to reflect it. Nothing "
                "here is double-counted or lost; it just isn't visible at invoice "
                "grain, which is exactly why this bridge exists."
            ),
        },
    }


# ================================================================ A3 — movers

@frappe.whitelist()
def get_ar_top_movers(from_date, to_date, company=None, limit=15):
    """
    Which customers moved the outstanding balance the most in this range,
    billed vs collected, ranked by the size of the swing either direction.

    NAME RESOLUTION — added 2026-08-22
    GL Entry only carries the customer's ID (party), which is why this
    table was showing raw values like "CUST-2026-204512" instead of a
    name. Resolved in bulk against Customer.customer_name afterward —
    the same pattern build_balance_facts() in extract.py already uses,
    rather than GL Entry's own party_name field, which isn't reliably
    populated across this data. Bulk, not per-row: at most `limit`
    customers here, so this is a single extra query either way.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    accounts = receivable_accounts(company)
    if not accounts:
        return []

    conditions = ["gle.account IN %(accounts)s", "gle.is_cancelled = 0",
                  "gle.posting_date BETWEEN %(from_date)s AND %(to_date)s",
                  "gle.party_type = 'Customer'", "gle.party IS NOT NULL", "gle.party != ''"]
    values = {"accounts": list(accounts), "from_date": from_date, "to_date": to_date}
    if company:
        conditions.append("gle.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT gle.party AS customer,
               SUM(CASE WHEN gle.voucher_type = 'Sales Invoice' THEN gle.debit - gle.credit ELSE 0 END) AS billed,
               SUM(CASE WHEN gle.voucher_type = 'Payment Entry' THEN gle.credit - gle.debit ELSE 0 END) AS collected,
               SUM(gle.debit - gle.credit) AS net_movement,
               COUNT(*) AS n
        FROM `tabGL Entry` gle
        WHERE {0}
        GROUP BY gle.party
        ORDER BY ABS(SUM(gle.debit - gle.credit)) DESC
        LIMIT %(limit)s
    """.format(" AND ".join(conditions)), dict(values, limit=cint(limit)), as_dict=True)

    names = {}
    customer_ids = [r.customer for r in rows]
    if customer_ids:
        for rec in frappe.get_all("Customer", filters={"name": ["in", customer_ids]},
                                   fields=["name", "customer_name"]):
            names[rec.name] = rec.customer_name

    return [{"customer": r.customer, "customer_name": names.get(r.customer) or r.customer,
              "billed": flt(r.billed), "collected": flt(r.collected),
              "net_movement": flt(r.net_movement), "transactions": r.n} for r in rows]


def _fetch_noninvoice_revenue_range(from_date, to_date, company=None):
    """
    Same rule as extract.py's fetch_noninvoice_revenue_rows() — every
    posting to an Income-root-type account that did NOT come from a
    Sales Invoice (almost entirely Journal Entry: restaurant/shop revenue
    with no invoice at all, and cross-company reclassifications) — bound
    to an arbitrary date range instead of a fixed YYYY-MM period, on the
    same reasoning _fetch_cash_movements_range in operational_api_cash.py
    documents for D1.
    """
    conditions = [
        "gle.posting_date BETWEEN %(from_date)s AND %(to_date)s",
        "gle.is_cancelled = 0",
        "acc.root_type = 'Income'",
        "gle.voucher_type != 'Sales Invoice'",
    ]
    values = {"from_date": from_date, "to_date": to_date, "company": company}
    if company:
        conditions.append("gle.company = %(company)s")

    return frappe.db.sql("""
        SELECT gle.debit AS debit, gle.credit AS credit
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE {conditions}
    """.format(conditions=" AND ".join(conditions)), values, as_dict=True)


# ========================================================== B1 — revenue by item group

@frappe.whitelist()
def get_revenue_by_item_group(from_date, to_date, company=None):
    """
    Net revenue for the range, by item group, split OPD/IPD/Unclassified.

    Reads Sales Invoice Item live (base_net_amount — already net of any
    line-level discount, and ERPNext stores a return invoice's amounts
    negative, so summing across return and non-return invoices together
    already nets correctly; no separate return subtraction needed here).

    NOTE ON METHODOLOGY — verified against real data before shipping this:
    summing base_net_amount across every submitted Sales Invoice Item for
    a full month reproduces the CEO dashboard's reconciled Net Sales
    figure to the cent, independently, for both January (1,081,267.68)
    and July (1,125,714.74) — checked against the raw exports, not
    assumed. That will not hold for every possible range: this is
    invoice-line revenue only, so a period where meaningful revenue was
    booked by Journal Entry instead of an invoice (see the gross_sales_je
    addendum) would show lower here than the CEO dashboard's Gross Sales
    line for that same period. It needs item-group and OPD/IPD grain,
    which the journal-booked slice does not carry, so that is read as a
    real trade-off, not an oversight.

    BRIDGE — added 2026-08-22
    The invoice-line total above ($1,081,267.68 for January, Shaafi
    Hospital only) is not the same figure as the CEO dashboard's Net
    Sales for January ($1,065,533) — both are correct, they answer
    slightly different questions. January's actual difference: Shaafi
    Hospital had -$15,734.44 of net journal-booked revenue that range
    (mostly MRI/CT/Mammography revenue reclassified OUT to Shaafi
    Diagnostic Center that same month), and 1,081,267.68 - 15,734.44 =
    1,065,533.24, which is the CEO dashboard's figure to the cent. The
    response's "bridge" key computes that live for whatever range and
    company are selected, in place of the plain footnote this used to
    carry, so the two numbers are never left to just look like a
    mismatch.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    company_cond = "AND si.company = %(company)s" if company else ""
    values = {"from_date": from_date, "to_date": to_date, "company": company}

    rows = frappe.db.sql("""
        SELECT sii.item_group AS item_group,
               si.source_order AS source_order,
               si.posting_date AS posting_date,
               SUM(sii.base_net_amount) AS net_amount,
               SUM(sii.base_amount) AS gross_amount,
               COUNT(DISTINCT si.name) AS invoice_count
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
          {company_cond}
        GROUP BY sii.item_group, si.source_order, si.posting_date
    """.format(company_cond=company_cond), values, as_dict=True)

    noninvoice_rows = _fetch_noninvoice_revenue_range(from_date, to_date, company)
    gross_sales = sum(flt(r.credit) for r in noninvoice_rows)
    revenue_reclass = sum(flt(r.debit) for r in noninvoice_rows)
    net_noninvoice = gross_sales - revenue_reclass

    if not rows:
        return {
            "by_item_group": [], "by_patient_type": [],
            "message": "No invoiced revenue in this range.",
            "bridge": {
                "invoice_net": 0.0, "gross_sales": gross_sales,
                "revenue_reclass": revenue_reclass, "net_noninvoice": net_noninvoice,
                "combined_net": net_noninvoice, "gl_rows": len(noninvoice_rows),
            },
        }

    resolver = DimensionResolver()
    by_item_group = {}
    by_patient_type = {}
    for r in rows:
        patient_type, _ = resolver.resolve(
            "Patient Type", r.posting_date, {"source_order": r.source_order})

        ig = by_item_group.setdefault(r.item_group or "(no item group)", {
            "item_group": r.item_group or "(no item group)",
            "gross_amount": 0.0, "net_amount": 0.0, "invoice_count": 0,
            "opd": 0.0, "ipd": 0.0, "unclassified": 0.0,
        })
        ig["gross_amount"] += flt(r.gross_amount)
        ig["net_amount"] += flt(r.net_amount)
        ig["invoice_count"] += r.invoice_count
        ig[{"OPD": "opd", "IPD": "ipd"}.get(patient_type, "unclassified")] += flt(r.net_amount)

        pt = by_patient_type.setdefault(patient_type, {"patient_type": patient_type, "net_amount": 0.0})
        pt["net_amount"] += flt(r.net_amount)

    by_item_group_list = sorted(by_item_group.values(), key=lambda d: -d["net_amount"])
    by_patient_type_list = sorted(by_patient_type.values(), key=lambda d: -d["net_amount"])
    total_net = sum(d["net_amount"] for d in by_item_group_list)
    for d in by_item_group_list:
        d["share"] = (d["net_amount"] / total_net) if total_net else 0.0

    return {
        "by_item_group": by_item_group_list,
        "by_patient_type": by_patient_type_list,
        "total_net": total_net,
        "message": ("Invoice-item revenue, {0} to {1}. Does not include revenue booked by "
                    "Journal Entry — see the bridge below for that slice, live for this "
                    "same range.").format(from_date, to_date),
        "bridge": {
            "invoice_net": total_net,
            "gross_sales": gross_sales,
            "revenue_reclass": revenue_reclass,
            "net_noninvoice": net_noninvoice,
            "combined_net": total_net + net_noninvoice,
            "gl_rows": len(noninvoice_rows),
            "message": (
                "Journal-booked revenue (gross_sales) for this range, minus that "
                "same revenue reclassified or moved elsewhere (revenue_reclass) — "
                "add this to the invoice-line Net Revenue above to reach the CEO "
                "dashboard's Net Sales for the same company and range. For a "
                "full calendar month this reconciles to the cent; for a partial "
                "range it's still the correct live number, just not one the CEO "
                "dashboard itself shows at anything other than month grain."
            ),
        },
    }