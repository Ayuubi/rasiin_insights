"""
Controls, drift detection and the metric registry.

Path: rasiin_insights/management_dashboard/utils/controls.py

WHY THIS EXISTS
    The REFERENCE dictionaries in extract.py prove the RULES are right, once,
    against two months reconciled by hand. They cannot prove that March or next
    November is right — nobody reconciled those by hand and nobody ever will.

    This file closes that gap. Every control below compares the fact table
    against an independent query of the General Ledger. No hard-coded figures,
    no assumptions: if the facts drift from the ledger, the build says so and
    the report shows a warning instead of a wrong number.

    That is what makes the build portable. Point it at another month, another
    company or another client site and it still checks itself.

THE METRIC REGISTRY
    METRICS below is the data dictionary. Each entry records what a metric
    means, where it comes from, the formula, and the caveat that bites if you
    forget it. It lives here rather than in a document because a document goes
    stale the first time a rule changes and nobody notices.

        bench --site shaafi execute \
          rasiin_insights.management_dashboard.utils.controls.data_dictionary
"""

import time

import frappe
from frappe.utils import flt, getdate

from rasiin_insights.management_dashboard.utils import extract, snapshot


# --------------------------------------------------------------- registry

METRICS = {
    "gross_sales": {
        "label": "Gross sales",
        "means": "Full list value of everything billed, before any discount.",
        "source": "Sales Invoice Item.base_amount, plus credits to income "
                  "accounts from any other voucher type",
        "formula": "SUM(base_amount) on non-return lines",
        "caveat": "Includes revenue booked by Journal Entry, which the standard "
                  "Sales Register cannot see. That is 2.6% of January's income "
                  "and zero in July.",
    },
    "discount": {
        "label": "Invoice discount",
        "means": "Value given away on the invoice itself.",
        "source": "Sales Invoice Item",
        "formula": "SUM(base_amount - base_net_amount) on non-return lines",
        "caveat": "This is the discount given at the point of sale. It reduces "
                  "revenue. Do not confuse it with payment_discount.",
    },
    "return": {
        "label": "Returns",
        "means": "Value of credit notes — billed, then given back.",
        "source": "Sales Invoice with is_return = 1",
        "formula": "ABS(SUM(base_amount)) on return lines",
        "caveat": "Stored positive and subtracted by the reader. ERPNext holds "
                  "return lines negative.",
    },
    "return_discount": {
        "label": "Discount on returns",
        "means": "Discount that sat on a credit note.",
        "source": "Sales Invoice with is_return = 1",
        "formula": "ABS(SUM(base_amount - base_net_amount)) on return lines",
        "caveat": "Added back, not subtracted. Without it net sales lands "
                  "$5,869 low in July — the discount would be counted twice.",
    },
    "revenue_reclass": {
        "label": "Revenue moved out",
        "means": "Income debited out of an account, usually moved to another "
                 "company.",
        "source": "GL debits to income accounts, voucher_type != Sales Invoice",
        "formula": "SUM(debit) on income accounts",
        "caveat": "January's $72,904.88 is mostly MRI and Mammography revenue "
                  "moved from the hospital to the Diagnostic Center. Both sides "
                  "are real and both must stay visible.",
    },
    "collection_current": {
        "label": "Collected — this period's invoices",
        "means": "Money received against an invoice raised inside the period.",
        "source": "POS payments on the invoice, plus Payment Entry references "
                  "to in-period invoices",
        "formula": "Non-internal cash/bank debits, split by reference date",
        "caveat": "POS payments have no Payment Entry. Counting both sources "
                  "for the same invoice doubles every counter payment.",
    },
    "collection_prior": {
        "label": "Collected — older debt",
        "means": "Money received this period against an earlier invoice.",
        "source": "Payment Entry references to out-of-period invoices",
        "formula": "Same pass as collection_current, split on invoice date",
        "caveat": "A payment against a future-dated invoice counts here too — "
                  "it is not current-period revenue being collected.",
    },
    "collection_unallocated": {
        "label": "Collected — not matched to an invoice",
        "means": "Real money in the bank with no invoice attached.",
        "source": "Payment Entry with no reference rows, Journal Entry receipts",
        "formula": "Cash received minus the part allocated to invoices",
        "caveat": "$462,828.59 in January against $8,112.64 in July. It is not "
                  "an error in the data — it is a data-entry practice that was "
                  "fixed. Never spread it across services.",
    },
    "payment_discount": {
        "label": "Discount at payment",
        "means": "Written off when the patient paid, not when billed.",
        "source": "GL debits to the accounts in md_payment_discount_accounts",
        "formula": "SUM(debit) on those accounts",
        "caveat": "Reduces cash collected, never sales. The invoice was already "
                  "raised at full value. $35,253.17 in July on top of the "
                  "$293,960.96 given away on invoices.",
    },
    "ar_transfer_in": {
        "label": "Debt created",
        "means": "Money someone started owing us.",
        "source": "GL debits to receivable accounts",
        "formula": "SUM(debit) on account_type = Receivable",
        "caveat": "18% of January's was created by Journal Entry, not invoice — "
                  "patients billed, then the debt moved to an insurer.",
    },
    "ar_transfer_out": {
        "label": "Debt cleared",
        "means": "Money someone stopped owing us — paid, or moved elsewhere.",
        "source": "GL credits to receivable accounts",
        "formula": "SUM(credit) on account_type = Receivable",
        "caveat": "Includes the patient side of an insurance transfer, which is "
                  "not a payment at all.",
    },
    "payable_charged": {
        "label": "Billed to us",
        "means": "New liability — suppliers, staff, commission accrued.",
        "source": "GL credits to payable accounts",
        "formula": "SUM(credit) on account_type = Payable",
        "caveat": "Not the same as expense. This is the liability view; expense "
                  "is the P&L view. In January payables saw $467,435 while the "
                  "P&L carried $949,954.",
    },
    "supplier_payment": {
        "label": "Paid out",
        "means": "Liability actually settled.",
        "source": "GL debits to payable accounts",
        "formula": "SUM(debit) on account_type = Payable",
        "caveat": "Covers suppliers and employees both — read party_type to "
                  "separate them.",
    },
    "commission": {
        "label": "Doctors' commission and salaries",
        "means": "The hospital's single largest cost.",
        "source": "GL on accounts in md_commission_accounts",
        "formula": "SUM(debit - credit) on those accounts",
        "caveat": "46% of all spending and about 39% of net sales. Carved out "
                  "of expense so it can never hide inside a total.",
    },
    "payroll": {
        "label": "Staff salary",
        "means": "Salary other than doctors' commission.",
        "source": "GL on accounts in md_payroll_accounts",
        "formula": "SUM(debit - credit) on those accounts",
        "caveat": "About a third of the commission line. If the two look "
                  "similar, an account is in the wrong setting.",
    },
    "expense": {
        "label": "Other expense",
        "means": "Everything else it cost to run the place.",
        "source": "GL on accounts with root_type = Expense",
        "formula": "SUM(debit - credit), excluding commission, payroll and the "
                   "payment-discount accounts",
        "caveat": "Read from the P&L, not the payable ledger — half of spending "
                  "never touches a payable. Includes stock adjustment, which "
                  "netted $54,365.59 in July.",
    },
    "refund": {
        "label": "Refunds",
        "means": "Cash handed back to a patient.",
        "source": "GL credits to cash/bank accounts on Sales Invoice vouchers",
        "formula": "SUM(credit) where voucher_type = Sales Invoice",
        "caveat": "Money out, not negative revenue. The credit note itself is "
                  "already counted under return.",
    },
}

DERIVED = {
    "net_sales": "gross_sales - discount - return + return_discount "
                 "- revenue_reclass",
    "total_collections": "collection_current + collection_prior "
                         "+ collection_unallocated",
    "total_expense": "commission + payroll + expense",
    "ar_closing": "ar_opening + ar_transfer_in - ar_transfer_out",
    "ap_closing": "ap_opening + payable_charged - supplier_payment",
    "net_cash_movement": "total_collections - refund - supplier_payment "
                         "- payroll",
}


def data_dictionary():
    """
    Print every metric, what it means, and how it is calculated.

    This is the answer to "where did this number come from?" — print it, hand
    it over, and it is always current because it is read from the code that
    does the work.
    """
    print("\n" + "=" * 78)
    print("METRIC DEFINITIONS — rasiin_insights management dashboard")
    print("=" * 78)
    for name in sorted(METRICS):
        m = METRICS[name]
        print("\n{0}   ({1})".format(name, m["label"]))
        print("  means    {0}".format(m["means"]))
        print("  source   {0}".format(m["source"]))
        print("  formula  {0}".format(m["formula"]))
        print("  caveat   {0}".format(m["caveat"]))

    print("\n" + "=" * 78)
    print("DERIVED FIGURES")
    print("=" * 78)
    for name, formula in DERIVED.items():
        print("  {0:<20} = {1}".format(name, formula))
    print("")
    return {"metrics": len(METRICS), "derived": len(DERIVED)}


# --------------------------------------------------------------- controls

def _fact_sum(period, company, metrics):
    if isinstance(metrics, str):
        metrics = [metrics]
    row = frappe.db.sql("""
        SELECT COALESCE(SUM(amount), 0) AS a FROM `tabManagement Fact`
        WHERE company = %(c)s AND period = %(p)s AND metric IN %(m)s
    """, {"c": company, "p": period, "m": metrics}, as_dict=True)[0]
    return flt(row.a)


def _fact_sum_flagged(period, company, metrics, flag):
    if isinstance(metrics, str):
        metrics = [metrics]
    row = frappe.db.sql("""
        SELECT COALESCE(SUM(amount), 0) AS a FROM `tabManagement Fact`
        WHERE company = %(c)s AND period = %(p)s AND metric IN %(m)s
          AND quality_flag LIKE %(f)s
    """, {"c": company, "p": period, "m": metrics,
          "f": "%" + flag + "%"}, as_dict=True)[0]
    return flt(row.a)


def gl_income(period, company):
    start, end = extract.period_bounds(period)
    row = frappe.db.sql("""
        SELECT COALESCE(SUM(gle.credit - gle.debit), 0) AS a
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE acc.root_type = 'Income' AND gle.is_cancelled = 0
          AND gle.company = %(c)s
          AND gle.posting_date BETWEEN %(s)s AND %(e)s
    """, {"c": company, "s": start, "e": end}, as_dict=True)[0]
    return flt(row.a)


def gl_cash_in(period, company):
    """Non-internal debits to cash and bank — the Step 2.4 rule, in one query."""
    start, end = extract.period_bounds(period)
    row = frappe.db.sql("""
        SELECT COALESCE(SUM(gle.debit), 0) AS a
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE acc.account_type IN ('Cash', 'Bank') AND gle.is_cancelled = 0
          AND gle.debit > 0 AND gle.company = %(c)s
          AND gle.posting_date BETWEEN %(s)s AND %(e)s
          AND NOT EXISTS (
              SELECT 1 FROM `tabGL Entry` g2
              INNER JOIN `tabAccount` a2 ON a2.name = g2.account
              WHERE g2.voucher_type = gle.voucher_type
                AND g2.voucher_no = gle.voucher_no
                AND g2.is_cancelled = 0 AND g2.credit > 0
                AND a2.account_type IN ('Cash', 'Bank'))
    """, {"c": company, "s": start, "e": end}, as_dict=True)[0]
    return flt(row.a)


def gl_balance(account_type, period, company):
    """Closing balance on the ledger as at the end of the period."""
    _, end = extract.period_bounds(period)
    row = frappe.db.sql("""
        SELECT COALESCE(SUM(gle.debit), 0) AS d,
               COALESCE(SUM(gle.credit), 0) AS c
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE acc.account_type = %(t)s AND gle.is_cancelled = 0
          AND gle.company = %(co)s AND gle.posting_date <= %(e)s
    """, {"t": account_type, "co": company, "e": end}, as_dict=True)[0]
    if account_type == "Receivable":
        return flt(row.d) - flt(row.c)
    return flt(row.c) - flt(row.d)


def gl_expense(period, company):
    """P&L expense, excluding the payment-discount accounts."""
    start, end = extract.period_bounds(period)
    excluded = extract._account_list("md_payment_discount_accounts") or [""]
    row = frappe.db.sql("""
        SELECT COALESCE(SUM(gle.debit - gle.credit), 0) AS a
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE acc.root_type = 'Expense' AND gle.is_cancelled = 0
          AND gle.company = %(c)s
          AND gle.posting_date BETWEEN %(s)s AND %(e)s
          AND gle.account NOT IN %(x)s
    """, {"c": company, "s": start, "e": end, "x": excluded}, as_dict=True)[0]
    return flt(row.a)


def run_controls(period, company):
    """
    Compare the fact table against the ledger. Returns a list of checks.

    Each check is (name, facts, ledger, variance, explained, note).
    'explained' is variance we can account for — an opening-entry invoice sits
    in sales but never reaches income, so it is a known difference rather than
    a fault.
    """
    checks = []

    net_facts = (_fact_sum(period, company, "gross_sales")
                 - _fact_sum(period, company, "discount")
                 - _fact_sum(period, company, "return")
                 + _fact_sum(period, company, "return_discount")
                 - _fact_sum(period, company, "revenue_reclass"))
    opening = _fact_sum_flagged(period, company, "gross_sales", "opening")
    checks.append(("net sales vs GL income", net_facts, gl_income(period, company),
                   opening,
                   "opening-flagged invoices are in sales but not in income"))

    coll_facts = _fact_sum(period, company,
                           ["collection_current", "collection_prior",
                            "collection_unallocated"])
    checks.append(("collections vs GL cash in", coll_facts,
                   gl_cash_in(period, company), 0.0, ""))

    ar_snap = frappe.db.get_value("Management Snapshot", {
        "company": company, "period": period, "metric": "ar_closing",
        "dimension_type": "Total"}, "amount")
    checks.append(("closing receivable vs GL", flt(ar_snap),
                   gl_balance("Receivable", period, company), 0.0, ""))

    ap_snap = frappe.db.get_value("Management Snapshot", {
        "company": company, "period": period, "metric": "ap_closing",
        "dimension_type": "Total"}, "amount")
    checks.append(("closing payable vs GL", flt(ap_snap),
                   gl_balance("Payable", period, company), 0.0, ""))

    exp_facts = _fact_sum(period, company, ["commission", "payroll", "expense"])
    checks.append(("expense vs GL", exp_facts, gl_expense(period, company),
                   0.0, ""))

    out = []
    for name, facts, ledger, explained, note in checks:
        variance = round(flt(facts) - flt(ledger), 2)
        unexplained = round(variance - flt(explained), 2)
        out.append({
            "check": name, "facts": flt(facts), "ledger": flt(ledger),
            "variance": variance, "explained": flt(explained),
            "unexplained": unexplained, "note": note,
        })
    return out


def check_period(period, company=None, verbose=True):
    """
    Run the controls for a period and report. Read-only.

        bench --site shaafi execute \
          rasiin_insights.management_dashboard.utils.controls.check_period \
          --kwargs "{'period': '2026-07'}"
    """
    threshold = flt(extract.get_settings().md_drift_threshold or 1.0)
    companies = [company] if company else snapshot.companies_in_period(period)
    results = {}

    for comp in companies:
        checks = run_controls(period, comp)
        drifted = [c for c in checks if abs(c["unexplained"]) > threshold]
        results[comp] = {"checks": checks, "drift": len(drifted)}

        if not verbose:
            continue

        print("\n" + "=" * 78)
        print("CONTROLS — {0} / {1}".format(period, comp))
        print("=" * 78)
        print("{0:<30}{1:>15}{2:>15}{3:>15}".format(
            "", "facts", "ledger", "unexplained"))
        for c in checks:
            print("{0:<30}{1:>15,.2f}{2:>15,.2f}{3:>15}".format(
                c["check"], c["facts"], c["ledger"],
                "OK" if abs(c["unexplained"]) <= threshold
                else "{:,.2f}".format(c["unexplained"])))
            if c["explained"] and c["note"]:
                print("{0:<30}{1:>15,.2f}  {2}".format(
                    "  explained", c["explained"], c["note"]))
        print("-" * 78)
        print("RESULT: {0}".format(
            "the fact table agrees with the ledger" if not drifted
            else "{0} check(s) drifted beyond {1:,.2f}".format(
                len(drifted), threshold)))

    return results


def check_all_periods(company=None):
    """Every period that has facts, oldest first. Read-only."""
    periods = [r.period for r in frappe.db.sql("""
        SELECT DISTINCT period FROM `tabManagement Fact` ORDER BY period
    """, as_dict=True)]
    summary = []
    for period in periods:
        results = check_period(period, company, verbose=False)
        for comp, r in results.items():
            summary.append((period, comp, r["drift"], r["checks"]))

    print("\n" + "=" * 78)
    print("CONTROL SUMMARY — every built period")
    print("=" * 78)
    print("{0:<12}{1:<32}{2:>10}".format("period", "company", "drifted"))
    for period, comp, drift, _ in summary:
        print("{0:<12}{1:<32}{2:>10}".format(
            period, comp[:31], drift if drift else "clean"))
    print("")
    return summary


def stamp_controls(period, company=None):
    """
    Write the control results onto the snapshot rows so a report can show a
    warning without recomputing. Writes to Management Snapshot only.
    """
    companies = [company] if company else snapshot.companies_in_period(period)
    for comp in companies:
        checks = run_controls(period, comp)
        worst = max((abs(c["unexplained"]) for c in checks), default=0.0)
        frappe.db.sql("""
            UPDATE `tabManagement Snapshot` SET variance = %(v)s
            WHERE company = %(c)s AND period = %(p)s
              AND dimension_type = 'Total' AND metric = 'control_check'
        """, {"v": worst, "c": comp, "p": period})
    frappe.db.commit()
    return True