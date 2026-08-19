"""
Fact extraction — sales.

Path: rasiin_insights/management_dashboard/utils/extract.py

This file currently implements Step 2.2 of the runbook: gross sales, discounts and
returns from Sales Invoice. Journal Entry revenue, transfers, collections and money
out are added in later steps, each behind its own function.

SAFETY
------
Nothing writes unless you pass dry_run=False. The default is a read-only run that
prints its totals against the reconciled reference figures for January and July.
Writes only ever touch `tabManagement Fact` — no ERPNext table is modified.

Usage:

    # read-only, prints and compares
    bench --site shaafi execute \
      rasiin_insights.management_dashboard.utils.extract.dry_run_sales \
      --kwargs "{'period': '2026-07'}"

    # both reference months at once
    bench --site shaafi execute \
      rasiin_insights.management_dashboard.utils.extract.verify_reference_months

    # only after the numbers match
    bench --site shaafi execute \
      rasiin_insights.management_dashboard.utils.extract.build_sales \
      --kwargs "{'period': '2026-07', 'dry_run': False}"

SIGN CONVENTION
---------------
Amounts are stored positive; `metric` carries the meaning. Return invoices hold
negative line values in ERPNext, so their absolute value is stored under `return`
and `return_discount`, and the reader subtracts.

    net_sales = gross_sales - discount - return + return_discount

Verified against production:

                     January        July
    gross_sales    1,328,743.51   1,433,735.33
    discount         235,689.50     293,960.96
    return            12,295.33      19,928.97
    return_discount      509.00       5,869.34
    net sales      1,081,267.68   1,125,714.74
"""

import time

import frappe
from frappe.utils import getdate, get_first_day, get_last_day, flt

from rasiin_insights.management_dashboard.utils.resolve import (
    DimensionResolver,
    build_insurance_flag,
)

CHUNK = 5000

# Reconciled by hand from the raw exports. Do not edit without redoing that work.
REFERENCE = {
    "2026-01": {
        "gross_sales": 1328743.51,
        "discount": 235689.50,
        "return": 12295.33,
        "return_discount": 509.00,
        "net_sales": 1081267.68,
        "invoices": 32619,
    },
    "2026-07": {
        "gross_sales": 1433735.33,
        "discount": 293960.96,
        "return": 19928.97,
        "return_discount": 5869.34,
        "net_sales": 1125714.74,
        "invoices": 34929,
    },
}


# ----------------------------------------------------------------- helpers

def get_settings():
    return frappe.get_single("Rasiin Insights Settings")


def period_bounds(period):
    """'2026-07' -> (date(2026,7,1), date(2026,7,31))"""
    start = getdate(period + "-01")
    return get_first_day(start), get_last_day(start)


def _docstatus_filter():
    """Drafts are excluded unless the setting says otherwise (runbook Step 0.1)."""
    return "(0, 1)" if get_settings().md_include_drafts else "(1)"


# ------------------------------------------------------------- extraction

def fetch_sales_rows(period, company=None):
    """
    One query, one row per invoice line. No per-row database access anywhere
    downstream — that is what keeps this fast at 100,000 lines a month.
    """
    start, end = period_bounds(period)
    conditions = ["si.posting_date BETWEEN %(start)s AND %(end)s"]
    if company:
        conditions.append("si.company = %(company)s")

    return frappe.db.sql("""
        SELECT
            si.name              AS invoice,
            si.company           AS company,
            si.posting_date      AS posting_date,
            si.is_return         AS is_return,
            si.customer          AS customer,
            si.customer_name     AS customer_name,
            si.is_opening        AS is_opening,
            si.customer_group    AS customer_group,
            si.so_type           AS sales_type,
            si.is_insurance      AS is_insurance,
            si.insurance         AS insurance,
            si.ref_practitioner  AS practitioner,
            si.owner             AS cashier,
            sii.idx              AS idx,
            sii.item_code        AS item_code,
            sii.item_name        AS item_name,
            sii.item_group       AS item_group,
            sii.income_account   AS income_account,
            sii.cost_center      AS cost_center,
            sii.warehouse        AS warehouse,
            sii.base_amount      AS base_amount,
            sii.base_net_amount  AS base_net_amount
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE {conditions}
          AND si.docstatus IN {docstatus}
        ORDER BY si.name, sii.idx
    """.format(
        conditions=" AND ".join(conditions),
        docstatus=_docstatus_filter(),
    ), {"start": start, "end": end, "company": company}, as_dict=True)


def build_sales_facts(period, company=None, resolver=None):
    """
    Turn invoice lines into fact rows. Returns (facts, totals, warnings).

    Emits up to two rows per line:
      normal line  -> gross_sales, and discount when the line was discounted
      return line  -> return,      and return_discount when the return carried one

    A line with no item group becomes 'Unclassified' and is flagged, never dropped.
    There is exactly one such line in January: the 334.20 opening invoice.
    """
    resolver = resolver or DimensionResolver()
    rows = fetch_sales_rows(period, company)

    facts = []
    totals = {"gross_sales": 0.0, "discount": 0.0, "return": 0.0,
              "return_discount": 0.0}
    warnings = {"unclassified_lines": 0, "unclassified_value": 0.0,
            "insurer_without_flag": set(), "opening_lines": 0,
            "zero_net_total": 0}
    invoices = set()

    for r in rows:
        invoices.add(r.invoice)

        item_group = r.item_group or "Unclassified"
        flags = []
        if not r.item_group:
            flags.append("unclassified")
            warnings["unclassified_lines"] += 1
            warnings["unclassified_value"] += flt(r.base_net_amount)

        if r.is_opening == "Yes":
            flags.append("opening")
            warnings["opening_lines"] += 1

        insurance_flag = build_insurance_flag(r.is_insurance, r.insurance)
        if insurance_flag == "0" and r.insurance:
            flags.append("insurer_no_flag")
            warnings["insurer_without_flag"].add(r.invoice)

        quality_flag = ",".join(flags)

        dims = resolver.resolve_all(r.posting_date, {
            "item_group": r.item_group,
            "sales_type": r.sales_type,
            "cost_center": r.cost_center,
            "income_account": r.income_account,
            "warehouse": r.warehouse,
            "company": r.company,
            "insurance_flag": insurance_flag,
            "customer_group": r.customer_group,
        })

        base = {
            "company": r.company,
            "posting_date": r.posting_date,
            "period": period,
            "voucher_type": "Sales Invoice",
            "voucher_no": r.invoice,
            "source_invoice": r.invoice,
            "item_code": r.item_code,
            "item_name": r.item_name,
            "item_group": item_group,
            "channel": dims["channel"],
            "channel_source": dims["channel_source"],
            "entity": dims["entity"],
            "service_line": dims.get("service_line"),
            "service_line_source": dims.get("service_line_source"),
            "cost_center": r.cost_center,
            "sales_type": r.sales_type,
            "payer_type": dims["payer_type"],
            "customer": r.customer,
            "customer_name": r.customer_name,
            "practitioner": r.practitioner,
            "cashier": r.cashier,
            "party_type": "Customer",
            "party": r.customer,
            "quality_flag": quality_flag,
        }

        gross = flt(r.base_amount)
        net = flt(r.base_net_amount)
        discount = gross - net

        if r.is_return:
            # ERPNext holds return lines negative. Store the absolute value.
            if gross:
                facts.append(dict(base, metric="return", amount=abs(gross)))
                totals["return"] += abs(gross)
            if discount:
                facts.append(dict(base, metric="return_discount",
                                  amount=abs(discount)))
                totals["return_discount"] += abs(discount)
        else:
            if gross:
                facts.append(dict(base, metric="gross_sales", amount=gross))
                totals["gross_sales"] += gross
            if discount:
                facts.append(dict(base, metric="discount", amount=discount))
                totals["discount"] += discount

    totals["net_sales"] = (totals["gross_sales"] - totals["discount"]
                           - totals["return"] + totals["return_discount"])
    totals["invoices"] = len(invoices)
    totals["lines"] = len(rows)
    totals["fact_rows"] = len(facts)

    return facts, totals, warnings


# ---------------------------------------------------------------- writing

def delete_sales_facts(period, company=None):
    """Idempotent rebuild: clear this period's sales facts before reinserting."""
    conditions = ["period = %(period)s",
                  "metric IN ('gross_sales', 'discount', 'return', 'return_discount')",
                  "voucher_type = 'Sales Invoice'"]
    if company:
        conditions.append("company = %(company)s")
    frappe.db.sql("""
        DELETE FROM `tabManagement Fact` WHERE {0}
    """.format(" AND ".join(conditions)), {"period": period, "company": company})


def insert_facts(facts):
    """Bulk insert fact rows. See insert_facts_into for the mechanics."""
    return insert_facts_into(facts, "Management Fact")


def build_sales(period, company=None, dry_run=True):
    """
    Extract this period's sales into Management Fact.

    dry_run=True (default) computes everything and writes nothing.
    """
    started = time.time()
    facts, totals, warnings = build_sales_facts(period, company)

    if dry_run:
        _report(period, totals, warnings, time.time() - started, wrote=0)
        return totals

    delete_sales_facts(period, company)
    written = insert_facts(facts)
    _report(period, totals, warnings, time.time() - started, wrote=written)
    return totals


# -------------------------------------------------------------- reporting

def _money(v):
    return "{:>16,.2f}".format(flt(v))


def _report(period, totals, warnings, seconds, wrote):
    ref = REFERENCE.get(period)
    print("\n" + "=" * 68)
    print("SALES EXTRACTION — {0}   ({1})".format(
        period, "DRY RUN, nothing written" if not wrote else
        "{0:,} fact rows written".format(wrote)))
    print("=" * 68)

    keys = ["gross_sales", "discount", "return", "return_discount", "net_sales"]
    if ref:
        print("{0:<18}{1:>16}{2:>16}{3:>12}".format(
            "", "computed", "reference", "diff"))
        ok = True
        for k in keys:
            diff = flt(totals[k]) - flt(ref[k])
            if abs(diff) >= 0.01:
                ok = False
            print("{0:<18}{1}{2}{3:>12}".format(
                k, _money(totals[k]), _money(ref[k]),
                "OK" if abs(diff) < 0.01 else "{:,.2f}".format(diff)))
        print("{0:<18}{1:>16,}{2:>16,}{3:>12}".format(
            "invoices", totals["invoices"], ref["invoices"],
            "OK" if totals["invoices"] == ref["invoices"] else
            totals["invoices"] - ref["invoices"]))
        print("-" * 68)
        print("RESULT: {0}".format(
            "matches the reconciled figures"
            if ok else "DOES NOT MATCH — do not write this period"))
    else:
        for k in keys:
            print("{0:<18}{1}".format(k, _money(totals[k])))
        print("{0:<18}{1:>16,}".format("invoices", totals["invoices"]))
        print("(no reference figures for this period — sanity check against the GL)")

    print("-" * 68)
    print("invoice lines read     {0:>10,}".format(totals["lines"]))
    print("fact rows produced     {0:>10,}".format(totals["fact_rows"]))
    print("seconds                {0:>10.1f}".format(seconds))

    if warnings["unclassified_lines"]:
        print("\nlines with no item group: {0} worth {1:,.2f} — kept as 'Unclassified'"
              .format(warnings["unclassified_lines"], warnings["unclassified_value"]))
    if warnings["opening_lines"]:
        print("opening-entry lines: {0} — in sales but not in GL income, flagged 'opening'"
              .format(warnings["opening_lines"]))
    if warnings["insurer_without_flag"]:
        print("invoices naming an insurer without the flag: {0} — not counted as insurance"
              .format(len(warnings["insurer_without_flag"])))
    print("")


def dry_run_sales(period, company=None):
    """Read-only. Safe on production."""
    return build_sales(period, company=company, dry_run=True)


def verify_reference_months(company=None):
    """
    Run both reconciled months and report. This is the Step 2.2 acceptance test.
    Read-only.
    """
    results = {}
    for period in ("2026-01", "2026-07"):
        results[period] = build_sales(period, company=company, dry_run=True)

    print("=" * 68)
    all_ok = True
    for period, totals in results.items():
        ref = REFERENCE[period]
        ok = all(abs(flt(totals[k]) - flt(ref[k])) < 0.01
                 for k in ("gross_sales", "discount", "return",
                           "return_discount", "net_sales"))
        all_ok = all_ok and ok
        print("{0}: {1}".format(period, "PASS" if ok else "FAIL"))
    print("=" * 68)
    print("Step 2.2 {0}\n".format(
        "complete — safe to write" if all_ok else "not complete"))
    return results


def sample_facts(period, limit=15, metric=None):
    """
    Eyeball what would be written. Read-only.
    Useful for checking the resolved dimensions look sane before any write.
    """
    facts, _, _ = build_sales_facts(period)
    if metric:
        facts = [f for f in facts if f["metric"] == metric]
    print("\n{0:<24}{1:<16}{2:>12}  {3:<12}{4:<14}{5}".format(
        "item_group", "metric", "amount", "channel", "payer_type", "practitioner"))
    print("-" * 100)
    for f in facts[:limit]:
        print("{0:<24}{1:<16}{2:>12,.2f}  {3:<12}{4:<14}{5}".format(
            (f["item_group"] or "")[:23], f["metric"], f["amount"],
            (f["channel"] or "")[:11], (f["payer_type"] or "")[:13],
            (f["practitioner"] or "")[:30]))
    print("")
    return len(facts)


# =====================================================================
# STEP 2.3 — Revenue that does not come from a Sales Invoice
# =====================================================================
# Append this to extract.py, below the sales section.
#
# Restaurant and Shop revenue have no invoice, no item and no item group.
# They exist only as Journal Entry credits to an income account, so the sales
# extractor structurally cannot see them. This is where they appear.
#
# Two metrics:
#   gross_sales      credits to an income account   (revenue earned)
#   revenue_reclass  debits  to an income account   (revenue moved or reversed)
#
# Net contribution = gross_sales - revenue_reclass.
#
# January's debits are almost entirely one reclassification: MRI, CT-Scan and
# Mammography revenue moved out of Shaafi Hospital and into Shaafi Diagnostic
# Center. Both sides are real and both must be visible, which is why the debit
# side is its own metric rather than a negative amount.
#
# ADD TO Management Fact `metric` OPTIONS:  revenue_reclass
# ADD TO Management Fact FIELDS:            service_line, service_line_source (Data)

REFERENCE_NONINVOICE = {
    "2026-01": {
        "gross_sales": 102170.03,
        "revenue_reclass": 72904.88,
        "net": 29265.15,
        "gl_rows": 36,
        "by_company": {
            "Shaafi Hospital": -15734.44,
            "Shaafi Diagnostic Center": 44999.59,
        },
    },
    "2026-07": {
        "gross_sales": 0.0,
        "revenue_reclass": 0.0,
        "net": 0.0,
        "gl_rows": 0,
        "by_company": {},
    },
}


def fetch_noninvoice_revenue_rows(period, company=None):
    """
    Every income posting that did not come from a Sales Invoice.

    Includes Journal Entry (the big one) and any other voucher type that touches
    an income account — a Purchase Invoice credited $7.00 to income in January,
    and leaving it out would mean the fact table never equals the P&L.

    Journal Entry custom fields are joined in because they carry the only link
    back to a patient or invoice: `sales_invoice` is filled on 97.7% of the
    journals that touch a customer receivable.
    """
    start, end = period_bounds(period)
    conditions = [
        "gle.posting_date BETWEEN %(start)s AND %(end)s",
        "gle.is_cancelled = 0",
        "acc.root_type = 'Income'",
        "gle.voucher_type != 'Sales Invoice'",
    ]
    if company:
        conditions.append("gle.company = %(company)s")

    return frappe.db.sql("""
        SELECT
            gle.name            AS gl_name,
            gle.company         AS company,
            gle.posting_date    AS posting_date,
            gle.voucher_type    AS voucher_type,
            gle.voucher_no      AS voucher_no,
            gle.account         AS income_account,
            gle.cost_center     AS cost_center,
            gle.debit           AS debit,
            gle.credit          AS credit,
            gle.party_type      AS party_type,
            gle.party           AS party,
            gle.remarks         AS remarks,
            je.sales_invoice    AS je_sales_invoice,
            je.patient          AS je_patient,
            je.practitioner     AS je_practitioner,
            je.insurance_company AS je_insurance_company,
            je.commission_entry_type AS je_commission_type
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        LEFT JOIN `tabJournal Entry` je
               ON je.name = gle.voucher_no AND gle.voucher_type = 'Journal Entry'
        WHERE {conditions}
        ORDER BY gle.posting_date, gle.voucher_no
    """.format(conditions=" AND ".join(conditions)),
        {"start": start, "end": end, "company": company}, as_dict=True)


def build_noninvoice_revenue_facts(period, company=None, resolver=None):
    """
    Returns (facts, totals, warnings).

    These rows have no item group by construction, so `item_group` is always
    'Unclassified' and every row is flagged. That is not a data problem — a
    restaurant meal has no clinical item group. The Service Line dimension is
    what makes them meaningful, and it resolves from the income account.
    """
    resolver = resolver or DimensionResolver()
    rows = fetch_noninvoice_revenue_rows(period, company)

    facts = []
    totals = {"gross_sales": 0.0, "revenue_reclass": 0.0}
    warnings = {"reclass_rows": 0, "linked_to_invoice": 0, "by_company": {}}

    for r in rows:
        dims = resolver.resolve_all(r.posting_date, {
            "item_group": None,
            "sales_type": None,
            "cost_center": r.cost_center,
            "income_account": r.income_account,
            "warehouse": None,
            "company": r.company,
            "insurance_flag": "1" if r.je_insurance_company else "0",
            "customer_group": None,
        })

        flags = ["no_item_group"]
        if flt(r.debit) > 0:
            flags.append("reclass")
            warnings["reclass_rows"] += 1
        if r.je_sales_invoice:
            warnings["linked_to_invoice"] += 1

        base = {
            "company": r.company,
            "posting_date": r.posting_date,
            "period": period,
            "voucher_type": r.voucher_type,
            "voucher_no": r.voucher_no,
            "source_invoice": r.je_sales_invoice,
            "item_code": None,
            "item_name": None,
            "item_group": "Unclassified",
            "channel": dims["channel"],
            "channel_source": dims["channel_source"],
            "entity": dims["entity"],
            "service_line": dims.get("service_line"),
            "service_line_source": dims.get("service_line_source"),
            "cost_center": r.cost_center,
            "sales_type": None,
            "payer_type": dims["payer_type"],
            "customer": r.party if r.party_type == "Customer" else None,
            "customer_name": None,
            "practitioner": r.je_practitioner,
            "cashier": None,
            "party_type": r.party_type,
            "party": r.party,
            "quality_flag": ",".join(flags),
        }

        net = flt(r.credit) - flt(r.debit)
        warnings["by_company"][r.company] = \
            warnings["by_company"].get(r.company, 0.0) + net

        if flt(r.credit):
            facts.append(dict(base, metric="gross_sales", amount=flt(r.credit)))
            totals["gross_sales"] += flt(r.credit)
        if flt(r.debit):
            facts.append(dict(base, metric="revenue_reclass", amount=flt(r.debit)))
            totals["revenue_reclass"] += flt(r.debit)

    totals["net"] = totals["gross_sales"] - totals["revenue_reclass"]
    totals["gl_rows"] = len(rows)
    totals["fact_rows"] = len(facts)
    return facts, totals, warnings


def delete_noninvoice_revenue_facts(period, company=None):
    conditions = [
        "period = %(period)s",
        "voucher_type != 'Sales Invoice'",
        "metric IN ('gross_sales', 'revenue_reclass')",
    ]
    if company:
        conditions.append("company = %(company)s")
    frappe.db.sql("""
        DELETE FROM `tabManagement Fact` WHERE {0}
    """.format(" AND ".join(conditions)), {"period": period, "company": company})


def build_noninvoice_revenue(period, company=None, dry_run=True):
    started = time.time()
    facts, totals, warnings = build_noninvoice_revenue_facts(period, company)

    if not dry_run:
        delete_noninvoice_revenue_facts(period, company)
        insert_facts(facts)

    ref = REFERENCE_NONINVOICE.get(period)
    print("\n" + "=" * 68)
    print("NON-INVOICE REVENUE — {0}   ({1})".format(
        period, "DRY RUN, nothing written" if dry_run
        else "{0:,} fact rows written".format(len(facts))))
    print("=" * 68)

    if ref:
        print("{0:<20}{1:>16}{2:>16}{3:>12}".format("", "computed", "reference", "diff"))
        ok = True
        for k in ("gross_sales", "revenue_reclass", "net"):
            diff = flt(totals[k]) - flt(ref[k])
            if abs(diff) >= 0.01:
                ok = False
            print("{0:<20}{1}{2}{3:>12}".format(
                k, _money(totals[k]), _money(ref[k]),
                "OK" if abs(diff) < 0.01 else "{:,.2f}".format(diff)))
        print("{0:<20}{1:>16,}{2:>16,}{3:>12}".format(
            "gl rows", totals["gl_rows"], ref["gl_rows"],
            "OK" if totals["gl_rows"] == ref["gl_rows"] else
            totals["gl_rows"] - ref["gl_rows"]))
        print("-" * 68)
        for comp, expected in sorted(ref["by_company"].items()):
            got = warnings["by_company"].get(comp, 0.0)
            match = abs(got - expected) < 0.01
            if not match:
                ok = False
            print("net for {0:<28}{1}   {2}".format(
                comp, _money(got), "OK" if match else
                "expected {0}".format(_money(expected))))
        print("-" * 68)
        print("RESULT: {0}".format(
            "matches the reconciled figures" if ok
            else "DOES NOT MATCH — do not write this period"))
    else:
        for k in ("gross_sales", "revenue_reclass", "net"):
            print("{0:<20}{1}".format(k, _money(totals[k])))

    print("-" * 68)
    print("GL rows read           {0:>10,}".format(totals["gl_rows"]))
    print("fact rows produced     {0:>10,}".format(totals["fact_rows"]))
    print("seconds                {0:>10.1f}".format(time.time() - started))
    if warnings["reclass_rows"]:
        print("\nreclassification rows: {0} — revenue debited out of one account"
              .format(warnings["reclass_rows"]))
    if warnings["linked_to_invoice"]:
        print("rows linked to an invoice via je.sales_invoice: {0}"
              .format(warnings["linked_to_invoice"]))
    print("")
    return totals


def service_line_breakdown(period, company=None):
    """
    Read-only. Answers the CEO's question 3: restaurant, shop and hajj tracked
    separately from core hospital revenue. Combines invoice and non-invoice
    revenue, so Restaurant and Shop finally appear.
    """
    resolver = DimensionResolver()
    sales_facts, _, _ = build_sales_facts(period, company, resolver)
    other_facts, _, _ = build_noninvoice_revenue_facts(period, company, resolver)

    signs = {"gross_sales": 1, "discount": -1, "return": -1,
             "return_discount": 1, "revenue_reclass": -1}
    lines = {}
    for f in sales_facts + other_facts:
        key = f.get("service_line") or "Unclassified"
        lines[key] = lines.get(key, 0.0) + signs[f["metric"]] * f["amount"]

    print("\nREVENUE BY SERVICE LINE — {0}".format(period))
    print("-" * 46)
    for name, amount in sorted(lines.items(), key=lambda x: -x[1]):
        print("{0:<28}{1:>16,.2f}".format(name, amount))
    print("-" * 46)
    print("{0:<28}{1:>16,.2f}\n".format("TOTAL", sum(lines.values())))
    return lines


def verify_noninvoice_revenue(company=None):
    """Step 2.3 acceptance test. Read-only."""
    results = {}
    for period in ("2026-01", "2026-07"):
        results[period] = build_noninvoice_revenue(period, company, dry_run=True)
    return results


# =====================================================================
# STEP 2.4 — Internal transfers
# =====================================================================
# Append to extract.py, below the non-invoice revenue section.
#
# This is the single most important rule in the build. Without it, collections
# overstate by 167-202%: cash swept from a cashier till to the main merchant
# account and on to the bank is counted as money received, two or three times.
#
# THE RULE
#   A debit to a cash or bank account is an internal transfer when the SAME
#   voucher also credits a cash or bank account. The money never left the
#   business, so it is not a collection.
#
# Applied to every voucher type. January routes transfers through Journal Entry
# ($1,890,565), Payment Entry ($517,791) and payment_type 'Internal Transfer'
# ($83,425). July uses Journal Entry only. Hard-coding this to Journal Entry
# would silently miss half a million dollars.
#
# This function EXCLUDES rows. It writes no facts of its own — that is the point.
# It is used by the collections step (2.5) to decide what counts as money in.
#
# The `md_transfer_detection` setting exists for the day the daily_cash_transfer
# link starts being filled. It is 0% filled in both January and July, so the
# default and only working option today is 'GL contra account'.

REFERENCE_TRANSFERS = {
    "2026-01": {
        "naive_cash_in": 3874342.68,
        "internal_transfers": 2408356.56,
        "true_money_in": 1465986.12,
        "by_voucher_type": {
            "Journal Entry": 1890565.20,
            "Payment Entry": 517791.36,
        },
    },
    "2026-07": {
        "naive_cash_in": 3236902.84,
        "internal_transfers": 2153071.06,
        "true_money_in": 1083831.78,
        "by_voucher_type": {
            "Journal Entry": 2153071.06,
        },
    },
}


def cash_bank_accounts(company=None):
    """
    Accounts that hold actual money.

    Read from the chart of accounts, never hard-coded — 'Discount - SH' was
    wrongly typed as Cash until August and pulled $37,302 of discounts into the
    January cash figure. Fixing the account type fixed every report at once.
    """
    filters = {"account_type": ["in", ["Cash", "Bank"]]}
    if company:
        filters["company"] = company
    return set(frappe.get_all("Account", filters=filters, pluck="name"))


def fetch_cash_movements(period, company=None):
    """
    Every posting to a cash or bank account in the period, with a flag saying
    whether the same voucher also moved money out of another cash or bank
    account.

    The EXISTS subquery is the transfer rule. It is done in SQL rather than in
    Python because at 191,000 GL rows a month, pulling everything into memory to
    group it costs more than letting the database do the work.
    """
    start, end = period_bounds(period)
    conditions = [
        "gle.posting_date BETWEEN %(start)s AND %(end)s",
        "gle.is_cancelled = 0",
        "acc.account_type IN ('Cash', 'Bank')",
    ]
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
            gle.against       AS against,
            gle.cost_center   AS cost_center,
            EXISTS (
                SELECT 1
                FROM `tabGL Entry` g2
                INNER JOIN `tabAccount` a2 ON a2.name = g2.account
                WHERE g2.voucher_type = gle.voucher_type
                  AND g2.voucher_no   = gle.voucher_no
                  AND g2.is_cancelled = 0
                  AND g2.credit > 0
                  AND a2.account_type IN ('Cash', 'Bank')
            ) AS is_internal,
            EXISTS (
                SELECT 1
                FROM `tabGL Entry` g3
                INNER JOIN `tabAccount` a3 ON a3.name = g3.account
                WHERE g3.voucher_type = gle.voucher_type
                  AND g3.voucher_no   = gle.voucher_no
                  AND g3.is_cancelled = 0
                  AND g3.debit > 0
                  AND a3.account_type IN ('Cash', 'Bank')
            ) AS is_internal_out
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE {conditions}
        ORDER BY gle.posting_date, gle.voucher_no
    """.format(conditions=" AND ".join(conditions)),
        {"start": start, "end": end, "company": company}, as_dict=True)


def summarise_transfers(period, company=None):
    """
    Returns the collections bridge for the period. Read-only, writes nothing.

        naive_cash_in       every debit to a cash or bank account
        internal_transfers  the portion that is just money moving between them
        true_money_in       what patients and payers actually handed over
    """
    rows = fetch_cash_movements(period, company)

    totals = {
        "naive_cash_in": 0.0,
        "internal_transfers": 0.0,
        "true_money_in": 0.0,
        "money_out": 0.0,
        "gl_rows": len(rows),
    }
    by_voucher_type = {}
    by_account = {}
    real_by_voucher = {}
    money_out_by_voucher = {}

    for r in rows:
        debit = flt(r.debit)
        credit = flt(r.credit)

        if debit > 0:
            totals["naive_cash_in"] += debit
            if r.is_internal:
                totals["internal_transfers"] += debit
                by_voucher_type[r.voucher_type] = \
                    by_voucher_type.get(r.voucher_type, 0.0) + debit
                by_account[r.account] = by_account.get(r.account, 0.0) + debit
            else:
                totals["true_money_in"] += debit
                real_by_voucher[r.voucher_type] = \
                    real_by_voucher.get(r.voucher_type, 0.0) + debit

        if credit > 0 and not r.is_internal_out:
            totals["money_out"] += credit
            money_out_by_voucher[r.voucher_type] = \
                money_out_by_voucher.get(r.voucher_type, 0.0) + credit

    totals["money_out_by_voucher"] = money_out_by_voucher
    return totals, by_voucher_type, by_account, real_by_voucher


def verify_transfers(company=None):
    """
    Step 2.4 acceptance test. Read-only, safe on production.

        bench --site shaafi execute \
          rasiin_insights.management_dashboard.utils.extract.verify_transfers
    """
    all_ok = True

    for period in ("2026-01", "2026-07"):
        started = time.time()
        totals, by_vt, by_acct, real_by_vt = summarise_transfers(period, company)
        ref = REFERENCE_TRANSFERS[period]

        print("\n" + "=" * 68)
        print("INTERNAL TRANSFERS — {0}".format(period))
        print("=" * 68)
        print("{0:<24}{1:>16}{2:>16}{3:>12}".format(
            "", "computed", "reference", "diff"))

        ok = True
        for key in ("naive_cash_in", "internal_transfers", "true_money_in"):
            diff = flt(totals[key]) - flt(ref[key])
            if abs(diff) >= 0.01:
                ok = False
            print("{0:<24}{1}{2}{3:>12}".format(
                key, _money(totals[key]), _money(ref[key]),
                "OK" if abs(diff) < 0.01 else "{:,.2f}".format(diff)))

        print("-" * 68)
        print("transfers by voucher type")
        for vt, expected in sorted(ref["by_voucher_type"].items()):
            got = by_vt.get(vt, 0.0)
            match = abs(got - expected) < 0.01
            if not match:
                ok = False
            print("  {0:<22}{1}   {2}".format(
                vt, _money(got),
                "OK" if match else "expected {0}".format(_money(expected))))
        for vt, got in sorted(by_vt.items()):
            if vt not in ref["by_voucher_type"]:
                ok = False
                print("  {0:<22}{1}   UNEXPECTED".format(vt, _money(got)))

        print("-" * 68)
        print("real money in, by voucher type")
        for vt, amount in sorted(real_by_vt.items(), key=lambda x: -x[1]):
            print("  {0:<22}{1}".format(vt, _money(amount)))

        print("-" * 68)
        print("top accounts receiving transfers")
        for acct, amount in sorted(by_acct.items(), key=lambda x: -x[1])[:6]:
            print("  {0:<40}{1}".format(acct[:39], _money(amount)))

        print("-" * 68)
        print("overstatement if transfers were counted: {0:.0%}".format(
            (totals["naive_cash_in"] / totals["true_money_in"] - 1)
            if totals["true_money_in"] else 0))
        print("cash/bank GL rows read {0:>10,}".format(totals["gl_rows"]))
        print("seconds                {0:>10.1f}".format(time.time() - started))
        print("RESULT: {0}".format(
            "matches the reconciled figures" if ok else "DOES NOT MATCH"))
        all_ok = all_ok and ok

    print("\n" + "=" * 68)
    print("Step 2.4 {0}\n".format(
        "complete" if all_ok else "not complete — do not build collections yet"))
    return all_ok


def show_transfer_effect(period="2026-07", company=None):
    """
    The two-line story for the CEO. Read-only.

    This is what to put on a slide: the old number, the rule, the real number.
    """
    totals, _, _, _ = summarise_transfers(period, company)
    print("\nCOLLECTIONS — {0}".format(period))
    print("-" * 52)
    print("{0:<34}{1}".format("What the old reports showed",
                              _money(totals["naive_cash_in"])))
    print("{0:<34}{1}".format("Less: money moved between our own accounts",
                              _money(-totals["internal_transfers"])))
    print("-" * 52)
    print("{0:<34}{1}".format("Money actually received",
                              _money(totals["true_money_in"])))
    print("{0:<34}{1}".format("Money paid out",
                              _money(-totals["money_out"])))
    print("-" * 52)
    print("{0:<34}{1}".format("Net movement",
                              _money(totals["true_money_in"] - totals["money_out"])))
    print("")


# =====================================================================
# STEP 2.5 — Collections
# =====================================================================
# Append to extract.py, below the transfers section.
#
# Three sources of money in, no overlap:
#
#   POS payments      taken on the invoice itself. These have NO Payment Entry —
#                     counting both would double every counter payment.
#   Payment Entry     received separately, usually settling a debt.
#   Journal Entry     money in booked by journal rather than a receipt.
#
# The GL is the arbiter. Every collection fact traces to one non-internal debit
# to a cash or bank account, so the total always equals true_money_in from
# Step 2.4 — 1,465,986.12 in January, 1,083,831.78 in July.
#
# SCALING
#   A Payment Entry can allocate more than it actually received: settle a $105
#   invoice with $100 cash and a $5 write-off and the references say $105.
#   Allocations are therefore scaled to the cash that really moved. Three
#   payments need it in January, nineteen in July.
#
# PAYMENT DISCOUNT
#   The write-off itself is its own metric. It reduces cash collected, never
#   sales — a different leak from the invoice discount, and a different
#   conversation. $37,302.21 in January, $35,253.17 in July.
#
# ITEM GROUP
#   Collections carry no item group yet. Step 2.6 allocates them across each
#   invoice's lines pro rata. Until then they are flagged 'pending_allocation'.

REFERENCE_COLLECTIONS = {
    "2026-01": {
        "pos": 893032.27,
        "pe_current": 12314.65,
        "pe_prior": 65.79,
        "pe_unallocated": 462828.59,
        "je": 97744.82,
        "total": 1465986.12,
        "payment_discount": 37302.21,
    },
    "2026-07": {
        "pos": 910071.33,
        "pe_current": 128605.96,
        "pe_prior": 26100.83,
        "pe_unallocated": 8112.64,
        "je": 10941.02,
        "total": 1083831.78,
        "payment_discount": 35253.17,
    },
}


def fetch_invoice_context(voucher_nos):
    """Dimensions for POS collections, keyed by invoice name."""
    if not voucher_nos:
        return {}
    rows = frappe.get_all(
        "Sales Invoice",
        filters={"name": ["in", list(voucher_nos)]},
        fields=["name", "customer", "customer_name", "customer_group",
                "so_type", "is_insurance", "insurance", "ref_practitioner",
                "owner", "cost_center", "posting_date"],
    )
    return {r.name: r for r in rows}


def fetch_payment_entry_context(voucher_nos):
    """Dimensions and allocations for Payment Entry collections."""
    if not voucher_nos:
        return {}, {}
    pes = frappe.get_all(
        "Payment Entry",
        filters={"name": ["in", list(voucher_nos)]},
        fields=["name", "party_type", "party", "mode_of_payment",
                "paid_to", "owner", "cost_center", "posting_date"],
    )
    refs = frappe.get_all(
        "Payment Entry Reference",
        filters={"parent": ["in", list(voucher_nos)]},
        fields=["parent", "reference_doctype", "reference_name",
                "allocated_amount"],
    )
    by_pe = {}
    for r in refs:
        by_pe.setdefault(r.parent, []).append(r)
    return {p.name: p for p in pes}, by_pe


def fetch_invoice_dates(invoice_names):
    if not invoice_names:
        return {}
    rows = frappe.get_all(
        "Sales Invoice",
        filters={"name": ["in", list(invoice_names)]},
        fields=["name", "posting_date", "customer", "customer_name"],
    )
    return {r.name: r for r in rows}


def build_collection_facts(period, company=None, resolver=None):
    """Returns (facts, totals, warnings). Reads only."""
    resolver = resolver or DimensionResolver()
    start, end = period_bounds(period)
    rows = fetch_cash_movements(period, company)
    money_in = [r for r in rows if flt(r.debit) > 0 and not r.is_internal]

    inv_names = {r.voucher_no for r in money_in
                 if r.voucher_type == "Sales Invoice"}
    pe_names = {r.voucher_no for r in money_in
                if r.voucher_type == "Payment Entry"}

    invoices = fetch_invoice_context(inv_names)
    pes, pe_refs = fetch_payment_entry_context(pe_names)

    referenced = {ref.reference_name
                  for refs in pe_refs.values() for ref in refs
                  if ref.reference_doctype == "Sales Invoice"}
    ref_invoices = fetch_invoice_dates(referenced - inv_names)
    ref_invoices.update({k: v for k, v in invoices.items()})

    facts = []
    totals = {"pos": 0.0, "pe_current": 0.0, "pe_prior": 0.0,
              "pe_unallocated": 0.0, "je": 0.0}
    warnings = {"scaled_payments": 0, "je_linked_to_invoice": 0,
                "pe_without_context": 0}

    def dims_for(posting_date, ctx):
        return resolver.resolve_all(posting_date, {
            "item_group": None,
            "sales_type": getattr(ctx, "so_type", None) if ctx else None,
            "cost_center": getattr(ctx, "cost_center", None) if ctx else None,
            "income_account": None,
            "warehouse": None,
            "company": None,
            "insurance_flag": build_insurance_flag(
                getattr(ctx, "is_insurance", None) if ctx else None,
                getattr(ctx, "insurance", None) if ctx else None),
            "customer_group": getattr(ctx, "customer_group", None) if ctx else None,
        })

    for r in money_in:
        cash = flt(r.debit)

        base = {
            "company": r.company,
            "posting_date": r.posting_date,
            "period": period,
            "voucher_type": r.voucher_type,
            "voucher_no": r.voucher_no,
            "item_code": None,
            "item_name": None,
            "item_group": None,
            "cost_center": r.cost_center,
            "merchant_account": r.account,
            "party_type": r.party_type,
            "party": r.party,
        }

        # ---------------------------------------------------- POS payments
        if r.voucher_type == "Sales Invoice":
            ctx = invoices.get(r.voucher_no)
            d = dims_for(r.posting_date, ctx)
            facts.append(dict(
                base,
                metric="collection_current",
                amount=cash,
                source_invoice=r.voucher_no,
                channel=d["channel"], channel_source=d["channel_source"],
                entity=d["entity"],
                service_line=d.get("service_line"),
                service_line_source=d.get("service_line_source"),
                payer_type=d["payer_type"],
                sales_type=getattr(ctx, "so_type", None) if ctx else None,
                customer=getattr(ctx, "customer", None) if ctx else None,
                customer_name=getattr(ctx, "customer_name", None) if ctx else None,
                practitioner=getattr(ctx, "ref_practitioner", None) if ctx else None,
                cashier=getattr(ctx, "owner", None) if ctx else None,
                mode_of_payment=None,
                quality_flag="pending_allocation",
            ))
            totals["pos"] += cash
            continue

        # ------------------------------------------------- Payment Entries
        if r.voucher_type == "Payment Entry":
            pe = pes.get(r.voucher_no)
            if not pe:
                warnings["pe_without_context"] += 1
            refs = pe_refs.get(r.voucher_no, [])
            allocated = sum(flt(x.allocated_amount) for x in refs)

            factor = 1.0
            if allocated > cash and allocated > 0:
                factor = cash / allocated
                warnings["scaled_payments"] += 1

            used = 0.0
            for x in refs:
                amount = flt(x.allocated_amount) * factor
                if not amount:
                    continue
                inv = ref_invoices.get(x.reference_name)
                inv_date = getattr(inv, "posting_date", None) if inv else None
                is_current = bool(inv_date) and \
                    getdate(start) <= getdate(inv_date) <= getdate(end)
                metric = "collection_current" if is_current else "collection_prior"
                d = dims_for(r.posting_date, inv)
                facts.append(dict(
                    base,
                    metric=metric,
                    amount=amount,
                    source_invoice=x.reference_name,
                    channel=d["channel"], channel_source=d["channel_source"],
                    entity=d["entity"],
                    service_line=d.get("service_line"),
                    service_line_source=d.get("service_line_source"),
                    payer_type=d["payer_type"],
                    sales_type=None,
                    customer=getattr(pe, "party", None) if pe else r.party,
                    customer_name=getattr(inv, "customer_name", None) if inv else None,
                    practitioner=None,
                    cashier=getattr(pe, "owner", None) if pe else None,
                    mode_of_payment=getattr(pe, "mode_of_payment", None) if pe else None,
                    quality_flag="pending_allocation",
                ))
                totals["pe_current" if is_current else "pe_prior"] += amount
                used += amount

            remainder = cash - used
            if remainder > 0.005:
                d = dims_for(r.posting_date, None)
                facts.append(dict(
                    base,
                    metric="collection_unallocated",
                    amount=remainder,
                    source_invoice=None,
                    channel=d["channel"], channel_source=d["channel_source"],
                    entity=d["entity"],
                    service_line=d.get("service_line"),
                    service_line_source=d.get("service_line_source"),
                    payer_type=d["payer_type"],
                    sales_type=None,
                    customer=getattr(pe, "party", None) if pe else r.party,
                    customer_name=None,
                    practitioner=None,
                    cashier=getattr(pe, "owner", None) if pe else None,
                    mode_of_payment=getattr(pe, "mode_of_payment", None) if pe else None,
                    quality_flag="unallocated",
                ))
                totals["pe_unallocated"] += remainder
            continue

        # -------------------------------------------------- Journal Entries
        d = dims_for(r.posting_date, None)
        linked = None
        if r.voucher_type == "Journal Entry":
            linked = frappe.db.get_value("Journal Entry", r.voucher_no,
                                         "sales_invoice")
            if linked:
                warnings["je_linked_to_invoice"] += 1
        facts.append(dict(
            base,
            metric="collection_current" if linked else "collection_unallocated",
            amount=cash,
            source_invoice=linked,
            channel=d["channel"], channel_source=d["channel_source"],
            entity=d["entity"],
            service_line=d.get("service_line"),
            service_line_source=d.get("service_line_source"),
            payer_type=d["payer_type"],
            sales_type=None,
            customer=r.party if r.party_type == "Customer" else None,
            customer_name=None,
            practitioner=None,
            cashier=None,
            mode_of_payment=None,
            quality_flag="pending_allocation" if linked else "unallocated",
        ))
        totals["je"] += cash

    totals["total"] = (totals["pos"] + totals["pe_current"] + totals["pe_prior"]
                       + totals["pe_unallocated"] + totals["je"])
    totals["fact_rows"] = len(facts)
    return facts, totals, warnings


# ------------------------------------------------------ payment discount

def build_payment_discount_facts(period, company=None):
    """
    Write-offs granted at payment time, from the Payment Entry deduction table.
    Reduces cash collected, never sales.
    """
    start, end = period_bounds(period)

    accounts = [a.strip() for a in
                (get_settings().md_payment_discount_accounts or "").splitlines()
                if a.strip()]
    if not accounts:
        return [], 0.0

    conditions = ["gle.posting_date BETWEEN %(start)s AND %(end)s",
                  "gle.is_cancelled = 0", "gle.debit > 0",
                  "gle.account IN %(accounts)s"]
    if company:
        conditions.append("gle.company = %(company)s")

    rows = frappe.db.sql("""
        SELECT gle.voucher_no AS voucher_no, gle.voucher_type AS voucher_type,
               gle.debit AS amount, gle.posting_date AS posting_date,
               gle.company AS company, gle.party AS party,
               gle.party_type AS party_type, gle.cost_center AS cost_center
        FROM `tabGL Entry` gle
        WHERE {0}
    """.format(" AND ".join(conditions)),
        {"start": start, "end": end, "company": company,
         "accounts": accounts}, as_dict=True)

    facts = []
    total = 0.0
    for r in rows:
        amount = flt(r.amount)
        if amount <= 0:
            continue
        facts.append({
            "company": r.company, "posting_date": r.posting_date,
            "period": period, "metric": "payment_discount", "amount": amount,
            "voucher_type": r.voucher_type, "voucher_no": r.voucher_no,
            "source_invoice": None, "item_code": None, "item_name": None,
            "item_group": None, "channel": None, "channel_source": None,
            "entity": None, "service_line": None, "service_line_source": None,
            "cost_center": r.cost_center, "sales_type": None, "payer_type": None,
            "customer": r.party if r.party_type == "Customer" else None,
            "customer_name": None, "practitioner": None,
            "mode_of_payment": None, "cashier": None,
            "merchant_account": None,
            "party_type": r.party_type, "party": r.party,
            "quality_flag": "pending_allocation",
        })
        total += amount
    return facts, total


# ------------------------------------------------------------- write/verify

def delete_collection_facts(period, company=None):
    conditions = ["period = %(period)s",
                  "metric IN ('collection_current', 'collection_prior', "
                  "'collection_unallocated', 'payment_discount')"]
    if company:
        conditions.append("company = %(company)s")
    frappe.db.sql("DELETE FROM `tabManagement Fact` WHERE {0}".format(
        " AND ".join(conditions)), {"period": period, "company": company})


def build_collections(period, company=None, dry_run=True):
    started = time.time()
    facts, totals, warnings = build_collection_facts(period, company)
    disc_facts, disc_total = build_payment_discount_facts(period, company)
    totals["payment_discount"] = disc_total

    if not dry_run:
        delete_collection_facts(period, company)
        insert_facts(facts + disc_facts)

    ref = REFERENCE_COLLECTIONS.get(period)
    print("\n" + "=" * 68)
    print("COLLECTIONS — {0}   ({1})".format(
        period, "DRY RUN, nothing written" if dry_run
        else "{0:,} fact rows written".format(len(facts) + len(disc_facts))))
    print("=" * 68)

    keys = ["pos", "pe_current", "pe_prior", "pe_unallocated", "je",
            "total", "payment_discount"]
    ok = True
    if ref:
        print("{0:<20}{1:>16}{2:>16}{3:>12}".format(
            "", "computed", "reference", "diff"))
        for k in keys:
            diff = flt(totals[k]) - flt(ref[k])
            if abs(diff) >= 0.01:
                ok = False
            print("{0:<20}{1}{2}{3:>12}".format(
                k, _money(totals[k]), _money(ref[k]),
                "OK" if abs(diff) < 0.01 else "{:,.2f}".format(diff)))
        print("-" * 68)
        print("RESULT: {0}".format(
            "matches the reconciled figures" if ok else "DOES NOT MATCH"))
    else:
        for k in keys:
            print("{0:<20}{1}".format(k, _money(totals[k])))

    print("-" * 68)
    print("fact rows produced     {0:>10,}".format(
        totals["fact_rows"] + len(disc_facts)))
    print("seconds                {0:>10.1f}".format(time.time() - started))
    if warnings["scaled_payments"]:
        print("payments scaled to actual cash: {0}".format(
            warnings["scaled_payments"]))
    if warnings["je_linked_to_invoice"]:
        print("journal receipts linked to an invoice: {0}".format(
            warnings["je_linked_to_invoice"]))
    if warnings["pe_without_context"]:
        print("payment entries with no header found: {0}".format(
            warnings["pe_without_context"]))
    print("")
    return totals


def verify_collections(company=None):
    """Step 2.5 acceptance test. Read-only."""
    for period in ("2026-01", "2026-07"):
        build_collections(period, company, dry_run=True)


# =====================================================================
# STEP 2.6 — Pro-rata allocation of collections to service lines
# =====================================================================
# Append to extract.py, below the collections section.
#
# A collection knows which invoice it paid, but not which services on that
# invoice. This splits every traceable collection across the invoice's own
# item lines, in proportion to line value:
#
#     alloc(line) = amount * line.base_net_amount / invoice.base_net_total
#
# THE RESIDUAL RULE
#   Percentages do not round cleanly. After rounding every line to cents, any
#   remainder goes to the largest line. Without this the drill-down ends up a
#   few cents off its own header, and a CEO who finds one cent missing stops
#   trusting the whole dashboard.
#
# WHAT CANNOT BE ALLOCATED
#   Receipts with no invoice reference have no service line and never will.
#   They stay as 'Unallocated' with the flag intact. In January that is
#   $462,828.59 — the unmatched-receipts problem, shown rather than smeared
#   across services.
#
# PRIOR-PERIOD INVOICES
#   A July payment settling a March invoice needs March's invoice lines, so the
#   line lookup is NOT restricted to the period being built. That is why this
#   step queries Sales Invoice Item by invoice name rather than by date.
#
# ACCEPTANCE
#   Conservation. Every dollar of collection must land somewhere: the sum of
#   allocated rows plus unallocated rows must equal the Step 2.5 totals exactly
#   — 1,465,986.12 in January, 1,083,831.78 in July. Nothing is created, nothing
#   is lost.

# POS-only allocation, computed from the raw exports. Used as a partial check:
# these are the four largest service lines paid at the counter.
REFERENCE_ALLOCATION = {
    "2026-01": {
        "pos_cash": 893032.27,
        "top": {"OT": 283913.25, "Drug": 186003.32,
                "Laboratory": 161356.03, "Ultrasound": 61286.19},
    },
    "2026-07": {
        "pos_cash": 910071.33,
        "top": {"OT": 300230.49, "Laboratory": 193830.62,
                "Drug": 183450.68, "CT-Scan": 44557.46},
    },
}

UNALLOCATED = "Unallocated"


def fetch_invoice_lines(invoice_names):
    """
    Item lines for a set of invoices, whatever period they belong to.

    Returns {invoice: [{item_code, item_name, item_group, net}, ...]}.
    Chunked because a month can reference tens of thousands of invoices and
    MariaDB has a limit on IN-list size.
    """
    lines = {}
    names = [n for n in invoice_names if n]
    if not names:
        return lines

    for i in range(0, len(names), 5000):
        batch = names[i:i + 5000]
        rows = frappe.db.sql("""
            SELECT parent, item_code, item_name, item_group, base_net_amount
            FROM `tabSales Invoice Item`
            WHERE parent IN %(names)s
            ORDER BY parent, idx
        """, {"names": batch}, as_dict=True)
        for r in rows:
            lines.setdefault(r.parent, []).append({
                "item_code": r.item_code,
                "item_name": r.item_name,
                "item_group": r.item_group or "Unclassified",
                "net": flt(r.base_net_amount),
            })
    return lines


def split_pro_rata(amount, lines):
    """
    Split `amount` across `lines` in proportion to line value, then push the
    rounding remainder onto the largest line so the parts sum exactly.

    Returns [(line, allocated_amount), ...] with zero-value parts dropped.

    A credit note has negative lines, so proportions are taken on absolute
    values — otherwise a mixed invoice would produce shares above 100%.
    """
    amount = flt(amount)
    if not lines or not amount:
        return []

    weights = [abs(flt(l["net"])) for l in lines]
    total = sum(weights)

    if total <= 0:
        # No line value to weight on — put it all on the first line rather
        # than dropping the money.
        return [(lines[0], round(amount, 2))]

    parts = []
    running = 0.0
    for line, weight in zip(lines, weights):
        share = round(amount * weight / total, 2)
        parts.append([line, share])
        running += share

    residual = amount - running
    if abs(residual) > 1e-9:
        largest = max(range(len(parts)), key=lambda i: weights[i])
        parts[largest][1] = parts[largest][1] + residual

    return [(line, share) for line, share in parts if share]


def allocate_collection_facts(facts):
    """
    Expand voucher-level collection facts into service-line facts.

    Input: the facts from build_collection_facts (one row per cash movement).
    Output: the same money, split by item group where an invoice is known.

    Rows with no source_invoice, and payment_discount rows, pass through with
    item_group = 'Unallocated'.
    """
    collect_metrics = {"collection_current", "collection_prior",
                       "collection_unallocated", "payment_discount"}

    invoice_names = {f.get("source_invoice") for f in facts
                     if f.get("metric") in collect_metrics
                     and f.get("source_invoice")}
    lines_by_invoice = fetch_invoice_lines(invoice_names)

    out = []
    stats = {"allocated": 0.0, "unallocated": 0.0,
             "invoices_missing_lines": set(), "rows_in": 0, "rows_out": 0}

    for f in facts:
        if f.get("metric") not in collect_metrics:
            out.append(f)
            continue

        stats["rows_in"] += 1
        invoice = f.get("source_invoice")
        lines = lines_by_invoice.get(invoice) if invoice else None

        if not lines:
            if invoice:
                stats["invoices_missing_lines"].add(invoice)
            row = dict(f)
            row["item_group"] = UNALLOCATED
            flags = [x for x in (row.get("quality_flag") or "").split(",") if x]
            if "unallocated" not in flags:
                flags.append("unallocated")
            flags = [x for x in flags if x != "pending_allocation"]
            row["quality_flag"] = ",".join(flags)
            out.append(row)
            stats["unallocated"] += flt(f["amount"])
            stats["rows_out"] += 1
            continue

        for line, share in split_pro_rata(f["amount"], lines):
            row = dict(f)
            row["item_group"] = line["item_group"]
            row["item_code"] = line["item_code"]
            row["item_name"] = line["item_name"]
            row["amount"] = share
            flags = [x for x in (row.get("quality_flag") or "").split(",")
                     if x and x != "pending_allocation"]
            row["quality_flag"] = ",".join(flags)
            out.append(row)
            stats["allocated"] += share
            stats["rows_out"] += 1

    stats["invoices_missing_lines"] = len(stats["invoices_missing_lines"])
    return out, stats


def build_collections_allocated(period, company=None, dry_run=True):
    """
    Step 2.5 plus Step 2.6: collections, split by service line.

    This replaces build_collections once allocation is accepted — same delete
    scope, so re-running is safe either way.
    """
    started = time.time()
    facts, totals, warnings = build_collection_facts(period, company)
    disc_facts, disc_total = build_payment_discount_facts(period, company)

    allocated, stats = allocate_collection_facts(facts + disc_facts)

    if not dry_run:
        delete_collection_facts(period, company)
        insert_facts(allocated)

    collected = totals["total"]
    landed = stats["allocated"] + stats["unallocated"]

    print("\n" + "=" * 68)
    print("COLLECTIONS BY SERVICE LINE — {0}   ({1})".format(
        period, "DRY RUN, nothing written" if dry_run
        else "{0:,} fact rows written".format(len(allocated))))
    print("=" * 68)
    print("{0:<34}{1}".format("collections from step 2.5", _money(collected)))
    print("{0:<34}{1}".format("payment discount", _money(disc_total)))
    print("{0:<34}{1}".format("expected total to place",
                              _money(collected + disc_total)))
    print("-" * 68)
    print("{0:<34}{1}".format("allocated to a service line",
                              _money(stats["allocated"])))
    print("{0:<34}{1}".format("could not be traced", _money(stats["unallocated"])))
    print("{0:<34}{1}".format("total placed", _money(landed)))

    conserved = abs(landed - (collected + disc_total)) < 0.02
    print("-" * 68)
    print("CONSERVATION: {0}".format(
        "every dollar accounted for" if conserved
        else "MISMATCH of {0:,.2f} — do not write".format(
            landed - collected - disc_total)))

    ref = REFERENCE_ALLOCATION.get(period)
    if ref:
        by_group = {}
        for f in allocated:
            if f["metric"] == "collection_current" and \
                    f["voucher_type"] == "Sales Invoice":
                by_group[f["item_group"]] = \
                    by_group.get(f["item_group"], 0.0) + flt(f["amount"])
        print("-" * 68)
        print("counter payments by service line (top lines checked)")
        ok = True
        for group, expected in sorted(ref["top"].items(),
                                      key=lambda x: -x[1]):
            got = by_group.get(group, 0.0)
            match = abs(got - expected) < 1.00
            if not match:
                ok = False
            print("  {0:<22}{1}{2:>14}".format(
                group, _money(got),
                "OK" if match else "want {0:,.2f}".format(expected)))
        pos_total = sum(by_group.values())
        pos_match = abs(pos_total - ref["pos_cash"]) < 0.05
        print("  {0:<22}{1}{2:>14}".format(
            "TOTAL counter", _money(pos_total),
            "OK" if pos_match else "want {0:,.2f}".format(ref["pos_cash"])))
        ok = ok and pos_match
        print("RESULT: {0}".format(
            "matches the reconciled figures" if ok else "DOES NOT MATCH"))

    print("-" * 68)
    print("voucher rows in         {0:>10,}".format(stats["rows_in"]))
    print("service-line rows out   {0:>10,}".format(stats["rows_out"]))
    print("seconds                 {0:>10.1f}".format(time.time() - started))
    if stats["invoices_missing_lines"]:
        print("\ninvoices referenced but with no item lines found: {0}"
              .format(stats["invoices_missing_lines"]))
    print("")

    return {"collected": collected, "payment_discount": disc_total,
            "allocated": stats["allocated"], "unallocated": stats["unallocated"]}


def collections_by_service_line(period, company=None, limit=20):
    """Read-only. What the money received was actually paying for."""
    facts, _, _ = build_collection_facts(period, company)
    disc, _ = build_payment_discount_facts(period, company)
    allocated, _ = allocate_collection_facts(facts + disc)

    by_group = {}
    for f in allocated:
        if f["metric"] == "payment_discount":
            continue
        by_group[f["item_group"]] = by_group.get(f["item_group"], 0.0) + flt(f["amount"])

    print("\nCOLLECTIONS BY SERVICE LINE — {0}".format(period))
    print("-" * 50)
    for group, amount in sorted(by_group.items(), key=lambda x: -x[1])[:limit]:
        print("{0:<30}{1:>18,.2f}".format(group, amount))
    print("-" * 50)
    print("{0:<30}{1:>18,.2f}\n".format("TOTAL", sum(by_group.values())))
    return by_group


def verify_allocation(company=None):
    """Step 2.6 acceptance test. Read-only."""
    for period in ("2026-01", "2026-07"):
        build_collections_allocated(period, company, dry_run=True)


# =====================================================================
# STEP 2.7 — Receivables and payables
# =====================================================================
# Append to extract.py, below the allocation section.
#
# Movement rows, not balance rows. Every posting to a receivable or payable
# account becomes a fact, so the CEO can drill into what created the debt and
# who settled it. The closing balance is then arithmetic:
#
#     opening + charged - settled = closing
#
# The snapshot (Step 2.9) carries closing forward as the next period's opening,
# which is what stops the report rescanning history every time it runs.
#
# METRICS
#   ar_transfer_in    debit  to a receivable account  — debt created
#   ar_transfer_out   credit to a receivable account  — debt settled or moved
#   expense           credit to a payable account     — billed to us
#   supplier_payment  debit  to a payable account     — paid by us
#
# WHY 'ar_transfer' RATHER THAN 'ar_charged'
#   Because a large share of the movement is not a sale at all. In January
#   $237,795.37 was debited to receivable by Journal Entry — patients billed,
#   then the debt moved to an insurer. Ticking 'bill to insurance' on an invoice
#   creates exactly that journal. The invoice looks settled while the insurer
#   owes the money, which is why receivables must be read from the GL party and
#   never from Sales Invoice.outstanding_amount.
#
# OPENING BALANCES
#   Read from every posting before the period start, so the first month of the
#   dashboard is correct even though the hospital went live mid-ledger.
#   January opens at 159,340.77 receivable and 157,125.40 payable.
#
# ACCEPTANCE
#   opening + charged - settled = closing, for both ledgers, both months:
#
#                      January          July
#     AR closing       43,733.79      85,305.50
#     AP closing      426,475.67     416,686.03

REFERENCE_BALANCES = {
    "2026-01": {
        "ar": {"opening": 159340.77, "charged": 1337053.33,
               "settled": 1452660.31, "closing": 43733.79},
        "ap": {"opening": 157125.40, "charged": 467435.55,
               "settled": 198085.28, "closing": 426475.67},
    },
    "2026-07": {
        "ar": {"opening": 76466.37, "charged": 1164260.86,
               "settled": 1155421.73, "closing": 85305.50},
        "ap": {"opening": 350941.70, "charged": 477984.39,
               "settled": 412240.06, "closing": 416686.03},
    },
}

LEDGERS = {
    "Receivable": {
        "charged_metric": "ar_transfer_in",    # debit  = debt created
        "settled_metric": "ar_transfer_out",   # credit = debt cleared
        "charged_side": "debit",
    },
    "Payable": {
        "charged_metric": "payable_charged",           # credit = billed to us
        "settled_metric": "supplier_payment",  # debit  = paid by us
        "charged_side": "credit",
    },
}


def opening_balance(account_type, period, company=None):
    """
    Balance carried into the period, from every posting before it.

    Signed so that a positive number means what you would expect: money owed to
    us for receivables, money we owe for payables.
    """
    start, _ = period_bounds(period)
    conditions = ["gle.posting_date < %(start)s", "gle.is_cancelled = 0",
                  "acc.account_type = %(account_type)s"]
    if company:
        conditions.append("gle.company = %(company)s")

    row = frappe.db.sql("""
        SELECT COALESCE(SUM(gle.debit), 0) AS debit,
               COALESCE(SUM(gle.credit), 0) AS credit
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE {0}
    """.format(" AND ".join(conditions)),
        {"start": start, "account_type": account_type, "company": company},
        as_dict=True)[0]

    if account_type == "Receivable":
        return flt(row.debit) - flt(row.credit)
    return flt(row.credit) - flt(row.debit)


def fetch_ledger_movements(account_type, period, company=None):
    start, end = period_bounds(period)
    conditions = ["gle.posting_date BETWEEN %(start)s AND %(end)s",
                  "gle.is_cancelled = 0",
                  "acc.account_type = %(account_type)s"]
    if company:
        conditions.append("gle.company = %(company)s")

    return frappe.db.sql("""
        SELECT gle.name AS gl_name, gle.company AS company,
               gle.posting_date AS posting_date,
               gle.voucher_type AS voucher_type, gle.voucher_no AS voucher_no,
               gle.account AS account, gle.debit AS debit, gle.credit AS credit,
               gle.party_type AS party_type, gle.party AS party,
               gle.cost_center AS cost_center,
               gle.against_voucher AS against_voucher
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE {0}
        ORDER BY gle.posting_date, gle.voucher_no
    """.format(" AND ".join(conditions)),
        {"start": start, "end": end, "account_type": account_type,
         "company": company}, as_dict=True)


def build_balance_facts(period, company=None, resolver=None):
    """
    Returns (facts, totals). Reads only.

    Party names are looked up in bulk — 69,541 receivable rows a month means a
    per-row get_value would take longer than the rest of the build combined.
    """
    resolver = resolver or DimensionResolver()
    facts = []
    totals = {}

    party_names = {}
    for account_type in LEDGERS:
        rows = fetch_ledger_movements(account_type, period, company)
        parties = {(r.party_type, r.party) for r in rows if r.party}
        for doctype in {p[0] for p in parties if p[0]}:
            names = [p[1] for p in parties if p[0] == doctype]
            field = {"Customer": "customer_name", "Supplier": "supplier_name",
                     "Employee": "employee_name"}.get(doctype)
            if not field:
                continue
            for i in range(0, len(names), 5000):
                for rec in frappe.get_all(doctype,
                                          filters={"name": ["in", names[i:i + 5000]]},
                                          fields=["name", field]):
                    party_names[(doctype, rec["name"])] = rec.get(field)

        cfg = LEDGERS[account_type]
        key = "ar" if account_type == "Receivable" else "ap"
        opening = opening_balance(account_type, period, company)
        charged = settled = 0.0

        for r in rows:
            debit, credit = flt(r.debit), flt(r.credit)
            if cfg["charged_side"] == "debit":
                charged_amt, settled_amt = debit, credit
            else:
                charged_amt, settled_amt = credit, debit

            d = resolver.resolve_all(r.posting_date, {
                "item_group": None, "sales_type": None,
                "cost_center": r.cost_center, "income_account": None,
                "warehouse": None, "company": r.company,
                "insurance_flag": "0", "customer_group": None,
            })

            base = {
                "company": r.company, "posting_date": r.posting_date,
                "period": period, "voucher_type": r.voucher_type,
                "voucher_no": r.voucher_no,
                "source_invoice": r.against_voucher,
                "item_code": None, "item_name": None, "item_group": None,
                "channel": d["channel"], "channel_source": d["channel_source"],
                "entity": d["entity"],
                "service_line": d.get("service_line"),
                "service_line_source": d.get("service_line_source"),
                "cost_center": r.cost_center, "sales_type": None,
                "payer_type": d["payer_type"],
                "customer": r.party if r.party_type == "Customer" else None,
                "customer_name": party_names.get((r.party_type, r.party)),
                "practitioner": None, "mode_of_payment": None,
                "merchant_account": r.account, "cashier": None,
                "party_type": r.party_type, "party": r.party,
                "quality_flag": "" if r.party else "no_party",
            }

            if charged_amt:
                facts.append(dict(base, metric=cfg["charged_metric"],
                                  amount=charged_amt))
                charged += charged_amt
            if settled_amt:
                facts.append(dict(base, metric=cfg["settled_metric"],
                                  amount=settled_amt))
                settled += settled_amt

        totals[key] = {
            "opening": opening, "charged": charged, "settled": settled,
            "closing": opening + charged - settled, "gl_rows": len(rows),
        }

    totals["fact_rows"] = len(facts)
    return facts, totals


def delete_balance_facts(period, company=None):
    conditions = ["period = %(period)s",
                  "metric IN ('ar_transfer_in', 'ar_transfer_out', "
                  "'payable_charged', 'supplier_payment')"]
    if company:
        conditions.append("company = %(company)s")
    frappe.db.sql("DELETE FROM `tabManagement Fact` WHERE {0}".format(
        " AND ".join(conditions)), {"period": period, "company": company})


def build_balances(period, company=None, dry_run=True):
    started = time.time()
    facts, totals = build_balance_facts(period, company)

    if not dry_run:
        delete_balance_facts(period, company)
        insert_facts(facts)

    ref = REFERENCE_BALANCES.get(period)
    print("\n" + "=" * 68)
    print("RECEIVABLES AND PAYABLES — {0}   ({1})".format(
        period, "DRY RUN, nothing written" if dry_run
        else "{0:,} fact rows written".format(len(facts))))
    print("=" * 68)

    ok = True
    for key, label in (("ar", "RECEIVABLE — owed to us"),
                       ("ap", "PAYABLE — owed by us")):
        t = totals[key]
        print("\n{0}".format(label))
        if ref:
            print("{0:<20}{1:>16}{2:>16}{3:>12}".format(
                "", "computed", "reference", "diff"))
            for k in ("opening", "charged", "settled", "closing"):
                diff = flt(t[k]) - flt(ref[key][k])
                if abs(diff) >= 0.01:
                    ok = False
                print("{0:<20}{1}{2}{3:>12}".format(
                    k, _money(t[k]), _money(ref[key][k]),
                    "OK" if abs(diff) < 0.01 else "{:,.2f}".format(diff)))
        else:
            for k in ("opening", "charged", "settled", "closing"):
                print("{0:<20}{1}".format(k, _money(t[k])))

        identity = abs((t["opening"] + t["charged"] - t["settled"])
                       - t["closing"]) < 0.01
        if not identity:
            ok = False
        print("{0:<20}{1}".format(
            "identity check",
            "opening + charged - settled = closing" if identity
            else "IDENTITY BROKEN"))
        print("{0:<20}{1:>16,}".format("GL rows", t["gl_rows"]))

    print("-" * 68)
    print("fact rows produced     {0:>10,}".format(totals["fact_rows"]))
    print("seconds                {0:>10.1f}".format(time.time() - started))
    print("RESULT: {0}".format(
        "matches the reconciled figures" if ok else "DOES NOT MATCH"))
    print("")
    return totals


def receivable_by_voucher_type(period, company=None):
    """
    Read-only. Shows how much debt is created outside the invoice path —
    the insurance and employee transfers.
    """
    facts, _ = build_balance_facts(period, company)
    out = {}
    for f in facts:
        if f["metric"] not in ("ar_transfer_in", "ar_transfer_out"):
            continue
        out.setdefault(f["voucher_type"], {}).setdefault(f["metric"], 0.0)
        out[f["voucher_type"]][f["metric"]] += flt(f["amount"])

    print("\nRECEIVABLE MOVEMENT BY VOUCHER TYPE — {0}".format(period))
    print("-" * 60)
    print("{0:<24}{1:>17}{2:>17}".format("", "debt created", "debt cleared"))
    for vt in sorted(out):
        print("{0:<24}{1:>17,.2f}{2:>17,.2f}".format(
            vt, out[vt].get("ar_transfer_in", 0.0),
            out[vt].get("ar_transfer_out", 0.0)))
    print("")
    return out


def verify_balances(company=None):
    """Step 2.7 acceptance test. Read-only."""
    for period in ("2026-01", "2026-07"):
        build_balances(period, company, dry_run=True)


# =====================================================================
# STEP 2.8 — Money out: expenses, commission, payroll, refunds
# =====================================================================
# Append to extract.py, below the balances section.
#
# READ THIS FIRST — a naming clash to fix in Step 2.7
#
#   Step 2.7 emits `expense` for CREDITS to a payable account, meaning "billed
#   to us but not yet paid". This step emits `expense` for DEBITS to an expense
#   account, meaning "what it actually cost us". Those are different numbers
#   measuring different things, and leaving both under one metric would give the
#   CEO two answers to one question — precisely the disease we are curing.
#
#   FIX BEFORE RUNNING:
#     1. Add metric option `payable_charged` to Management Fact
#     2. In Step 2.7, change LEDGERS["Payable"]["charged_metric"]
#        from "expense" to "payable_charged"
#     3. In delete_balance_facts, change 'expense' to 'payable_charged'
#
#   After that: `payable_charged` is the liability view, `expense` is the P&L
#   view, and neither pretends to be the other.
#
# WHAT THIS STEP READS
#   Every posting to an account with root_type = 'Expense', net of credits.
#   That catches costs however they arrive — purchase invoice, journal, direct
#   cash payment, stock movement — which the payable ledger alone does not.
#   In January the payable ledger saw $467,435 of charges while the P&L carried
#   $949,954 of expense. Reading only one of them halves the picture.
#
# THREE CARVE-OUTS FROM THE EXPENSE TOTAL
#   commission        Doctors' salaries and commissions. The largest single
#                     cost in the hospital: $441,824 in January, $444,365 in
#                     July — more than three times general payroll.
#   payroll           Staff salary accounts.
#   payment_discount  Already captured in Step 2.5. Excluded here, or the
#                     write-offs would be counted twice.
#
# SETTINGS TO ADD (Rasiin Insights Settings, Small Text, one account per line)
#   md_commission_accounts   -> 50010 - Doctors' Salaries & Commissions - SH
#   md_payroll_accounts      -> 5213 - Salary - SH
#   md_payment_discount_accounts already exists from Step 2.5.
#
# ACCEPTANCE
#                        January          July
#     commission        441,824.09      444,364.51
#     payroll           124,866.08      130,901.41
#     expense (other)   346,879.97      383,443.47
#     total expense     913,570.14      958,709.39
#     refund              5,633.00        7,710.00
#
#   Total expense excludes payment discount by design. Add it back and you get
#   the raw P&L expense figure of 949,954.41 and 993,962.06.

REFERENCE_MONEY_OUT = {
    "2026-01": {"commission": 441824.09, "payroll": 124866.08,
                "expense": 346879.97, "total_expense": 913570.14,
                "refund": 5633.00},
    "2026-07": {"commission": 444364.51, "payroll": 130901.41,
                "expense": 383443.47, "total_expense": 958709.39,
                "refund": 7710.00},
}


def _account_list(fieldname):
    return [a.strip() for a in
            (get_settings().get(fieldname) or "").splitlines() if a.strip()]


def fetch_expense_rows(period, company=None):
    start, end = period_bounds(period)
    conditions = ["gle.posting_date BETWEEN %(start)s AND %(end)s",
                  "gle.is_cancelled = 0", "acc.root_type = 'Expense'"]
    if company:
        conditions.append("gle.company = %(company)s")

    return frappe.db.sql("""
        SELECT gle.company AS company, gle.posting_date AS posting_date,
               gle.voucher_type AS voucher_type, gle.voucher_no AS voucher_no,
               gle.account AS account, gle.debit AS debit, gle.credit AS credit,
               gle.party_type AS party_type, gle.party AS party,
               gle.cost_center AS cost_center
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE {0}
        ORDER BY gle.posting_date, gle.voucher_no
    """.format(" AND ".join(conditions)),
        {"start": start, "end": end, "company": company}, as_dict=True)


def fetch_refund_rows(period, company=None):
    """
    Cash paid back to patients inside an invoice — the credit side of a POS
    payment on a credit note. Not an expense, so it is kept separate.
    """
    start, end = period_bounds(period)
    conditions = ["gle.posting_date BETWEEN %(start)s AND %(end)s",
                  "gle.is_cancelled = 0", "gle.credit > 0",
                  "gle.voucher_type = 'Sales Invoice'",
                  "acc.account_type IN ('Cash', 'Bank')"]
    if company:
        conditions.append("gle.company = %(company)s")

    return frappe.db.sql("""
        SELECT gle.company AS company, gle.posting_date AS posting_date,
               gle.voucher_no AS voucher_no, gle.account AS account,
               gle.credit AS amount, gle.party AS party,
               gle.cost_center AS cost_center
        FROM `tabGL Entry` gle
        INNER JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE {0}
    """.format(" AND ".join(conditions)),
        {"start": start, "end": end, "company": company}, as_dict=True)


def build_money_out_facts(period, company=None, resolver=None):
    resolver = resolver or DimensionResolver()
    commission_accounts = set(_account_list("md_commission_accounts"))
    payroll_accounts = set(_account_list("md_payroll_accounts"))
    discount_accounts = set(_account_list("md_payment_discount_accounts"))

    facts = []
    totals = {"commission": 0.0, "payroll": 0.0, "expense": 0.0,
              "refund": 0.0, "excluded_discount": 0.0}

    for r in fetch_expense_rows(period, company):
        net = flt(r.debit) - flt(r.credit)
        if not net:
            continue

        if r.account in discount_accounts:
            totals["excluded_discount"] += net
            continue

        if r.account in commission_accounts:
            metric = "commission"
        elif r.account in payroll_accounts:
            metric = "payroll"
        else:
            metric = "expense"

        d = resolver.resolve_all(r.posting_date, {
            "item_group": None, "sales_type": None,
            "cost_center": r.cost_center, "income_account": None,
            "warehouse": None, "company": r.company,
            "insurance_flag": "0", "customer_group": None,
        })

        facts.append({
            "company": r.company, "posting_date": r.posting_date,
            "period": period, "metric": metric, "amount": net,
            "voucher_type": r.voucher_type, "voucher_no": r.voucher_no,
            "source_invoice": None, "item_code": None, "item_name": None,
            "item_group": None,
            "channel": d["channel"], "channel_source": d["channel_source"],
            "entity": d["entity"],
            "service_line": d.get("service_line"),
            "service_line_source": d.get("service_line_source"),
            "cost_center": r.cost_center, "sales_type": None,
            "payer_type": None, "customer": None, "customer_name": None,
            "practitioner": None, "mode_of_payment": None,
            "merchant_account": r.account, "cashier": None,
            "party_type": r.party_type, "party": r.party,
            "quality_flag": "" if r.party or metric == "expense" else "no_party",
        })
        totals[metric] += net

    for r in fetch_refund_rows(period, company):
        amount = flt(r.amount)
        if not amount:
            continue
        d = resolver.resolve_all(r.posting_date, {
            "item_group": None, "sales_type": None,
            "cost_center": r.cost_center, "income_account": None,
            "warehouse": None, "company": r.company,
            "insurance_flag": "0", "customer_group": None,
        })
        facts.append({
            "company": r.company, "posting_date": r.posting_date,
            "period": period, "metric": "refund", "amount": amount,
            "voucher_type": "Sales Invoice", "voucher_no": r.voucher_no,
            "source_invoice": r.voucher_no, "item_code": None,
            "item_name": None, "item_group": None,
            "channel": d["channel"], "channel_source": d["channel_source"],
            "entity": d["entity"],
            "service_line": d.get("service_line"),
            "service_line_source": d.get("service_line_source"),
            "cost_center": r.cost_center, "sales_type": None,
            "payer_type": None, "customer": r.party, "customer_name": None,
            "practitioner": None, "mode_of_payment": None,
            "merchant_account": r.account, "cashier": None,
            "party_type": "Customer", "party": r.party, "quality_flag": "",
        })
        totals["refund"] += amount

    totals["total_expense"] = (totals["commission"] + totals["payroll"]
                               + totals["expense"])
    totals["fact_rows"] = len(facts)
    return facts, totals


def delete_money_out_facts(period, company=None):
    conditions = ["period = %(period)s",
                  "metric IN ('commission', 'payroll', 'expense', 'refund')"]
    if company:
        conditions.append("company = %(company)s")
    frappe.db.sql("DELETE FROM `tabManagement Fact` WHERE {0}".format(
        " AND ".join(conditions)), {"period": period, "company": company})


def build_money_out(period, company=None, dry_run=True):
    started = time.time()
    facts, totals = build_money_out_facts(period, company)

    if not dry_run:
        delete_money_out_facts(period, company)
        insert_facts(facts)

    ref = REFERENCE_MONEY_OUT.get(period)
    print("\n" + "=" * 68)
    print("MONEY OUT — {0}   ({1})".format(
        period, "DRY RUN, nothing written" if dry_run
        else "{0:,} fact rows written".format(len(facts))))
    print("=" * 68)

    keys = ["commission", "payroll", "expense", "total_expense", "refund"]
    ok = True
    if ref:
        print("{0:<20}{1:>16}{2:>16}{3:>12}".format(
            "", "computed", "reference", "diff"))
        for k in keys:
            diff = flt(totals[k]) - flt(ref[k])
            if abs(diff) >= 0.01:
                ok = False
            print("{0:<20}{1}{2}{3:>12}".format(
                k, _money(totals[k]), _money(ref[k]),
                "OK" if abs(diff) < 0.01 else "{:,.2f}".format(diff)))
        print("-" * 68)
        print("RESULT: {0}".format(
            "matches the reconciled figures" if ok else "DOES NOT MATCH"))
    else:
        for k in keys:
            print("{0:<20}{1}".format(k, _money(totals[k])))

    print("-" * 68)
    print("{0:<34}{1}".format("payment discount excluded here",
                              _money(totals["excluded_discount"])))
    print("{0:<34}{1}".format("raw P&L expense (for cross-check)",
                              _money(totals["total_expense"]
                                     + totals["excluded_discount"])))
    print("fact rows produced     {0:>10,}".format(totals["fact_rows"]))
    print("seconds                {0:>10.1f}".format(time.time() - started))
    print("")
    return totals


def expense_breakdown(period, company=None, limit=15):
    """Read-only. Where the money goes, largest first."""
    facts, _ = build_money_out_facts(period, company)
    by_account = {}
    for f in facts:
        if f["metric"] == "refund":
            continue
        key = f["merchant_account"]
        by_account[key] = by_account.get(key, 0.0) + flt(f["amount"])

    print("\nEXPENSE BY ACCOUNT — {0}".format(period))
    print("-" * 66)
    for account, amount in sorted(by_account.items(),
                                  key=lambda x: -x[1])[:limit]:
        print("{0:<48}{1:>16,.2f}".format(str(account)[:47], amount))
    print("-" * 66)
    print("{0:<48}{1:>16,.2f}\n".format("TOTAL", sum(by_account.values())))
    return by_account


def verify_money_out(company=None):
    """Step 2.8 acceptance test. Read-only."""
    for period in ("2026-01", "2026-07"):
        build_money_out(period, company, dry_run=True)


def insert_facts_into(rows, doctype):
    """
    Bulk insert a list of plain dicts into any doctype table.

    Frappe's normal document API validates, runs hooks and writes one row at a
    time. That is right for a user saving an invoice and wrong here: 137,000
    fact rows would take hours. bulk_insert writes straight to the table.

    The trade-off is that nothing is validated — no mandatory checks, no Select
    option checks, no hooks. A metric value missing from the Select options will
    be written happily and simply not render in the UI. That is why every metric
    is added to the doctype by hand before its build step is run.

    Rows are written in chunks of CHUNK with a commit between them, so the
    database never holds one enormous transaction and other users are not
    blocked while a month is being built.

    Every dict must have identical keys — the column list is taken from the
    first row and reused for all of them.
    """
    if not rows:
        return 0

    # name/creation/modified/owner/modified_by are required on every Frappe
    # table and are not part of the caller's data, so they are added here.
    
    # Take the union of every row's keys, not just the first row's. Different
    # build functions produce slightly different key sets, and taking row[0]
    # as gospel fails with "Column count doesn't match value count" hundreds of
    # rows later — a long way from the row that actually caused it.
    keys = sorted({k for row in rows for k in row.keys()})
    columns = ["name", "creation", "modified", "owner", "modified_by"] + keys
    now = frappe.utils.now()
    user = frappe.session.user or "Administrator"
    written = 0

    for i in range(0, len(rows), CHUNK):
        batch = rows[i:i + CHUNK]
        values = []
        for f in batch:
            # 18 hex chars, not 10. At 10 the birthday paradox makes a
            # duplicate primary key near-certain across a full backfill:
            # ~8% at 400k rows, ~37% at 1M, unavoidable at 2.6M. 18 chars
            # puts the collision probability around 1 in a billion.
            row = [frappe.generate_hash(length=18), now, now, user, user]
            row += [f.get(k) for k in keys]      # .get() fills gaps with None
            values.append(row)
        frappe.db.bulk_insert(doctype, fields=columns, values=values)
        frappe.db.commit()
        written += len(batch)

    return written