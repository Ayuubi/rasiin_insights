# Copyright (c) 2026, Rasiin Technology and contributors
# For license information, please see license.txt

"""
Operational Reports — Page 4: Stock.

Path: rasiin_insights/management_dashboard/utils/operational_api_stock.py

WHO THIS IS FOR
    Pharmacy/store staff and finance, same audience as Pages 1-3 but for
    inventory instead of money. Every function here reads live from Bin /
    Stock Ledger Entry / Stock Entry / Stock Reconciliation / Purchase
    Receipt / GL Entry — never Management Snapshot or Management Fact.

SCOPE — confirmed with the user 2026-08-22, in three rounds
(see claude/page4-stock-scope-v1.md and claude/page4-stock-data-validation.md
in the project docs for the full back-and-forth):
    S1  Current stock value, by warehouse.
    S2  Stock by item (item-level snapshot).
    S3  Dead/slow-moving stock — 90-day no-movement cutoff, always "as of
        right now" (Bin has no historical grain, so this is not
        date-range filterable the way S4/S5/S6/S7/S8 are).
    S4  Daily stock movement, transaction-level, date-range.
    S5  Movement by voucher type, date-range.
    S6  Stock transfers & adjustments (Stock Entry by purpose, Stock
        Reconciliation).
    S7  COGS & gross profit.
    S8  Goods received vs invoiced, item level.

VALIDATED 2026-08-22 against the user's real Jan/Jul 2026 stock exports
(export_stock_range.py v2) before any of this was written:

    S7 — COGS account confirmed: `account_type = 'Cost of Goods Sold'`
    (5111 - Cost of Goods Sold - SH is the only account with that type in
    this chart). Sales-side stock deduction confirmed as Sales Invoice
    directly (update_stock=1) — Delivery Note has zero rows in both
    months, so it is not used at all. January's GL net on that account,
    Sales-Invoice-voucher-only, ties to Stock Ledger Entry's
    stock_value_difference on Sales Invoice rows EXACTLY (131,413.94 both
    ways). July ties to the cent too (127,719.18 vs 127,719.10, 8 cents
    of rounding across 16,900 postings) — the account's *full* balance is
    ~$5,719 higher in July because a handful of non-Sales-Invoice
    postings (a couple Stock Entry lines, a Purchase Receipt correction,
    one direct Purchase Invoice posting) also hit the same COGS account.
    This module reports the Sales-Invoice-attributable figure as the
    headline (the one that actually reconciles) and surfaces the
    residual separately rather than either dropping it silently or
    blending it in unexplained.

    S6 — January's Stock Reconciliation shows a ~$520k net value change
    that is almost the entire current stock value. Traced to source: it
    is 5-6 documents with `purpose = 'Opening Stock'` — a one-time
    system-go-live balance load, not physical-count corrections. This
    module excludes `purpose = 'Opening Stock'` from the adjustments
    table so a one-time setup event doesn't read as ongoing inventory
    noise, but reports its excluded total so it isn't just hidden either.

    S8 — Purchase Receipt Item's `purchase_invoice` back-link field is
    empty on every row in this instance (not used/synced), but
    `billed_amt` (ERPNext's own independently-maintained running total)
    is populated and reliable — this module reads billed_amt, not the
    broken link field. January has only 3 Purchase Receipt documents all
    month (the real GRN step only became the standard process around
    July — confirmed via the voucher-type split in
    get_movement_by_voucher_type: January's stock-in came through
    Purchase Invoice directly, July's through Purchase Receipt) — S8 will
    correctly look nearly empty for January by design, not by bug.

    S1/S2 — Shaafi Diagnostic Center has 5 of its own warehouses in the
    Warehouse master, but ZERO Bin rows and ZERO Stock Ledger Entry rows
    in either month — the same "static balance, no real activity" shape
    already found on the payables/receivables side. The company filter
    is kept for architectural consistency with Pages 1-3, but will render
    empty for SDC today.
"""

import frappe
from frappe.utils import flt, cint, getdate, add_days, nowdate


DEAD_STOCK_CUTOFF_DAYS = 90
DRILLDOWN_ROW_CAP = 500


# ============================================================= account/warehouse helpers

def cogs_accounts(company=None):
    filters = {"account_type": "Cost of Goods Sold"}
    if company:
        filters["company"] = company
    return set(frappe.get_all("Account", filters=filters, pluck="name"))


def warehouses_for_company(company=None):
    """{warehouse_name: company} for every warehouse, or scoped to one company."""
    filters = {}
    if company:
        filters["company"] = company
    return {r.name: r.company for r in
            frappe.get_all("Warehouse", filters=filters, fields=["name", "company"])}


def _item_group_map(item_codes=None):
    if not item_codes:
        return {}
    return {r.name: {"item_name": r.item_name, "item_group": r.item_group or "Unclassified"}
            for r in frappe.get_all("Item", filters={"name": ["in", list(item_codes)]},
                                     fields=["name", "item_name", "item_group"])}


# ============================================================= S1 — current stock value

@frappe.whitelist()
def get_stock_value_summary(company=None):
    """
    Current stock value by warehouse, straight off Bin — always "as of
    right now", the same as the Bin table itself (no posting_date grain
    to filter by a range). See this module's docstring for why SDC shows
    empty here.
    """
    wh_company = warehouses_for_company(company)
    if not wh_company:
        return {"warehouses": [], "message": "No warehouses found for this company."}

    rows = frappe.db.sql("""
        SELECT b.warehouse AS warehouse, wh.company AS company,
               SUM(b.actual_qty) AS qty, SUM(b.stock_value) AS value
        FROM `tabBin` b
        INNER JOIN `tabWarehouse` wh ON wh.name = b.warehouse
        WHERE wh.name IN %(warehouses)s
        GROUP BY b.warehouse, wh.company
        ORDER BY value DESC
    """, {"warehouses": list(wh_company.keys())}, as_dict=True)

    total_qty = sum(flt(r.qty) for r in rows)
    total_value = sum(flt(r.value) for r in rows)

    return {
        "warehouses": [{"warehouse": r.warehouse, "company": r.company,
                         "qty": flt(r.qty), "value": flt(r.value)} for r in rows],
        "total_qty": total_qty, "total_value": total_value,
        "warehouse_count": len([r for r in rows if flt(r.value) or flt(r.qty)]),
        "message": ("Live snapshot from Bin, as of right now — not a historical "
                    "as-of-date figure, since Bin only ever holds the current balance."),
    }


# ============================================================= S2 — stock by item

@frappe.whitelist()
def get_stock_by_item(company=None, only_nonzero=1):
    """
    Item-level current stock snapshot (Bin joined to Item + Warehouse).
    Full list returned in one call and paginated/filtered client-side —
    same pattern as C1/aging's day-range tables, just item-grain instead
    of day-grain (~3,300 stock items at most, well within what the
    existing pages already keep in memory for CSV export).
    """
    wh_company = warehouses_for_company(company)
    if not wh_company:
        return {"rows": [], "message": "No warehouses found for this company."}

    conditions = ["b.warehouse IN %(warehouses)s"]
    if cint(only_nonzero):
        conditions.append("(b.actual_qty != 0 OR b.stock_value != 0)")

    rows = frappe.db.sql("""
        SELECT b.item_code AS item_code, b.warehouse AS warehouse,
               b.actual_qty AS qty, b.valuation_rate AS valuation_rate,
               b.stock_value AS stock_value
        FROM `tabBin` b
        WHERE {0}
    """.format(" AND ".join(conditions)), {"warehouses": list(wh_company.keys())}, as_dict=True)

    item_info = _item_group_map({r.item_code for r in rows})
    data = []
    for r in rows:
        info = item_info.get(r.item_code, {})
        data.append({
            "item_code": r.item_code, "item_name": info.get("item_name") or r.item_code,
            "item_group": info.get("item_group") or "Unclassified", "warehouse": r.warehouse,
            "qty": flt(r.qty), "valuation_rate": flt(r.valuation_rate), "stock_value": flt(r.stock_value),
        })
    data.sort(key=lambda d: -d["stock_value"])

    return {
        "rows": data,
        "message": ("Live snapshot from Bin, as of right now. {0} item x warehouse rows."
                    ).format(len(data)),
    }


# ============================================================= S3 — dead/slow-moving

@frappe.whitelist()
def get_dead_stock(company=None, cutoff_days=None):
    """
    Items still holding stock (qty > 0, from Bin) with no Stock Ledger
    Entry movement in `cutoff_days` (default 90). Both Bin and the
    last-movement lookup are inherently "as of right now" — this is a
    live panel, not something a date-range filter elsewhere on the page
    changes. See this module's docstring for why.
    """
    cutoff_days = cint(cutoff_days) or DEAD_STOCK_CUTOFF_DAYS
    wh_company = warehouses_for_company(company)
    if not wh_company:
        return {"rows": [], "message": "No warehouses found for this company."}

    cutoff_date = getdate(add_days(nowdate(), -cutoff_days))

    bin_rows = frappe.db.sql("""
        SELECT b.item_code AS item_code, b.warehouse AS warehouse,
               b.actual_qty AS qty, b.stock_value AS stock_value
        FROM `tabBin` b
        WHERE b.warehouse IN %(warehouses)s AND b.actual_qty > 0
    """, {"warehouses": list(wh_company.keys())}, as_dict=True)

    if not bin_rows:
        return {"rows": [], "total_value": 0.0,
                "message": "No warehouses in this company currently hold stock."}

    last_move = {(r.item_code, r.warehouse): r.last_movement_date for r in frappe.db.sql("""
        SELECT sle.item_code AS item_code, sle.warehouse AS warehouse,
               MAX(sle.posting_date) AS last_movement_date
        FROM `tabStock Ledger Entry` sle
        WHERE sle.warehouse IN %(warehouses)s AND sle.is_cancelled = 0
        GROUP BY sle.item_code, sle.warehouse
    """, {"warehouses": list(wh_company.keys())}, as_dict=True)}

    item_info = _item_group_map({r.item_code for r in bin_rows})
    candidates = []
    for r in bin_rows:
        key = (r.item_code, r.warehouse)
        lm = last_move.get(key)
        if lm and getdate(lm) >= cutoff_date:
            continue  # moved recently enough, not dead
        info = item_info.get(r.item_code, {})
        candidates.append({
            "item_code": r.item_code, "item_name": info.get("item_name") or r.item_code,
            "item_group": info.get("item_group") or "Unclassified", "warehouse": r.warehouse,
            "qty": flt(r.qty), "stock_value": flt(r.stock_value),
            "last_movement_date": str(lm) if lm else None,
        })

    candidates.sort(key=lambda d: -d["stock_value"])
    total_value = sum(d["stock_value"] for d in candidates)

    return {
        "rows": candidates, "total_value": total_value, "cutoff_days": cutoff_days,
        "message": ("As of today ({0}). Items holding stock (qty > 0) with no Stock Ledger "
                    "Entry movement in the last {1} days, or none on record at all. This is "
                    "always live — not affected by the date filters on other panels."
                    ).format(nowdate(), cutoff_days),
    }


# ============================================================= S4 — daily stock movement

@frappe.whitelist()
def get_daily_stock_movement(from_date, to_date, company=None):
    """
    Day-by-day qty in/out and value change across every Stock Ledger
    Entry in the range — the stock-side mirror of C1/A1's day rows.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    conditions = ["sle.posting_date BETWEEN %(from_date)s AND %(to_date)s", "sle.is_cancelled = 0"]
    values = {"from_date": from_date, "to_date": to_date}
    if company:
        conditions.append("sle.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT sle.posting_date AS posting_date,
               SUM(CASE WHEN sle.actual_qty > 0 THEN sle.actual_qty ELSE 0 END) AS qty_in,
               SUM(CASE WHEN sle.actual_qty < 0 THEN -sle.actual_qty ELSE 0 END) AS qty_out,
               SUM(sle.stock_value_difference) AS value_change,
               COUNT(DISTINCT sle.voucher_no) AS vouchers
        FROM `tabStock Ledger Entry` sle
        WHERE {0}
        GROUP BY sle.posting_date
    """.format(" AND ".join(conditions)), values, as_dict=True)

    by_day = {str(r.posting_date): r for r in rows}
    days = []
    total_in = total_out = total_value = 0.0
    cursor = from_date
    while cursor <= to_date:
        key = str(cursor)
        r = by_day.get(key)
        d = {
            "date": key,
            "qty_in": flt(r.qty_in) if r else 0.0,
            "qty_out": flt(r.qty_out) if r else 0.0,
            "value_change": flt(r.value_change) if r else 0.0,
            "vouchers": cint(r.vouchers) if r else 0,
        }
        days.append(d)
        total_in += d["qty_in"]; total_out += d["qty_out"]; total_value += d["value_change"]
        cursor = add_days(cursor, 1)

    return {
        "days": days, "total_qty_in": total_in, "total_qty_out": total_out,
        "total_value_change": total_value,
        "message": ("Every Stock Ledger Entry, {0} to {1} — receiving, dispensing, transfers, "
                    "POS sale, and manual adjustments all included. Click a day for the raw "
                    "movement rows.").format(from_date, to_date),
    }


@frappe.whitelist()
def get_stock_movement_day_drilldown(date, company=None):
    """Raw Stock Ledger Entry rows for one day, for the S4 click-through."""
    date = getdate(date)
    conditions = ["sle.posting_date = %(date)s", "sle.is_cancelled = 0"]
    values = {"date": date}
    if company:
        conditions.append("sle.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT sle.item_code AS item_code, sle.warehouse AS warehouse,
               sle.voucher_type AS voucher_type, sle.voucher_no AS voucher_no,
               sle.actual_qty AS actual_qty, sle.valuation_rate AS valuation_rate,
               sle.stock_value_difference AS stock_value_difference
        FROM `tabStock Ledger Entry` sle
        WHERE {0}
        ORDER BY ABS(sle.stock_value_difference) DESC
        LIMIT {1}
    """.format(" AND ".join(conditions), DRILLDOWN_ROW_CAP), values, as_dict=True)

    item_info = _item_group_map({r.item_code for r in rows})
    for r in rows:
        r["item_name"] = item_info.get(r.item_code, {}).get("item_name") or r.item_code

    return {"rows": rows, "capped": len(rows) >= DRILLDOWN_ROW_CAP,
            "message": ("Largest-value {0} movements for this day, by absolute value change."
                        ).format(DRILLDOWN_ROW_CAP) if len(rows) >= DRILLDOWN_ROW_CAP else ""}


# ============================================================= S5 — movement by voucher type

@frappe.whitelist()
def get_movement_by_voucher_type(from_date, to_date, company=None):
    """Qty in/out and value change grouped by voucher type, for the range."""
    from_date, to_date = getdate(from_date), getdate(to_date)
    conditions = ["sle.posting_date BETWEEN %(from_date)s AND %(to_date)s", "sle.is_cancelled = 0"]
    values = {"from_date": from_date, "to_date": to_date}
    if company:
        conditions.append("sle.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT sle.voucher_type AS voucher_type,
               SUM(CASE WHEN sle.actual_qty > 0 THEN sle.actual_qty ELSE 0 END) AS qty_in,
               SUM(CASE WHEN sle.actual_qty < 0 THEN -sle.actual_qty ELSE 0 END) AS qty_out,
               SUM(sle.stock_value_difference) AS value_change,
               COUNT(DISTINCT sle.voucher_no) AS vouchers
        FROM `tabStock Ledger Entry` sle
        WHERE {0}
        GROUP BY sle.voucher_type
        ORDER BY vouchers DESC
    """.format(" AND ".join(conditions)), values, as_dict=True)

    return {
        "rows": [{"voucher_type": r.voucher_type, "qty_in": flt(r.qty_in), "qty_out": flt(r.qty_out),
                   "value_change": flt(r.value_change), "vouchers": cint(r.vouchers)} for r in rows],
        "message": "Where stock movement came from this range — receiving, dispensing, POS sale, adjustment.",
    }


# ============================================================= S6 — transfers & adjustments

@frappe.whitelist()
def get_stock_transfers(from_date, to_date, company=None):
    """
    Stock Entry documents in the range — transfers between warehouses and
    material issues (consumption), one row per document.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    conditions = ["se.posting_date BETWEEN %(from_date)s AND %(to_date)s", "se.docstatus = 1"]
    values = {"from_date": from_date, "to_date": to_date}
    if company:
        conditions.append("se.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT se.name AS name, se.posting_date AS posting_date, se.purpose AS purpose,
               se.from_warehouse AS from_warehouse, se.to_warehouse AS to_warehouse,
               SUM(sed.amount) AS amount, COUNT(sed.name) AS item_count, SUM(sed.qty) AS qty
        FROM `tabStock Entry` se
        INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
        WHERE {0}
        GROUP BY se.name
        ORDER BY se.posting_date DESC, se.name DESC
    """.format(" AND ".join(conditions)), values, as_dict=True)

    by_purpose = {}
    total_qty = total_amount = 0.0
    for r in rows:
        p = by_purpose.setdefault(r.purpose or "(none)", {"purpose": r.purpose or "(none)",
                                                            "entries": 0, "qty": 0.0, "amount": 0.0})
        p["entries"] += 1; p["qty"] += flt(r.qty); p["amount"] += flt(r.amount)
        total_qty += flt(r.qty); total_amount += flt(r.amount)

    return {
        "rows": [{"name": r.name, "posting_date": r.posting_date, "purpose": r.purpose,
                   "from_warehouse": r.from_warehouse, "to_warehouse": r.to_warehouse,
                   "item_count": r.item_count, "qty": flt(r.qty), "amount": flt(r.amount)} for r in rows],
        "by_purpose": sorted(by_purpose.values(), key=lambda d: -d["entries"]),
        "total_qty": total_qty, "total_amount": total_amount,
        "message": "Every submitted Stock Entry, by purpose — Material Issue, Material Transfer, etc.",
    }


@frappe.whitelist()
def get_stock_entry_drilldown(name):
    """Line items for one Stock Entry document, for the S6 click-through."""
    rows = frappe.db.sql("""
        SELECT sed.item_code AS item_code, sed.item_name AS item_name,
               sed.s_warehouse AS s_warehouse, sed.t_warehouse AS t_warehouse,
               sed.qty AS qty, sed.uom AS uom, sed.valuation_rate AS valuation_rate,
               sed.amount AS amount
        FROM `tabStock Entry Detail` sed
        WHERE sed.parent = %(name)s
        ORDER BY sed.idx
    """, {"name": name}, as_dict=True)
    return rows


@frappe.whitelist()
def get_stock_adjustments(from_date, to_date, company=None):
    """
    Stock Reconciliation documents in the range, one row per document,
    EXCLUDING purpose = 'Opening Stock' (a one-time system-setup load,
    not a physical-count correction — see this module's docstring for
    how that was found and confirmed against the real January export).
    The excluded total is still reported, not just silently dropped.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    conditions = ["sr.posting_date BETWEEN %(from_date)s AND %(to_date)s", "sr.docstatus = 1"]
    values = {"from_date": from_date, "to_date": to_date}
    if company:
        conditions.append("sr.company = %(company)s")
        values["company"] = company

    all_rows = frappe.db.sql("""
        SELECT sr.name AS name, sr.posting_date AS posting_date, sr.purpose AS purpose,
               COUNT(sri.name) AS item_count, SUM(sri.amount_difference) AS value_change
        FROM `tabStock Reconciliation` sr
        INNER JOIN `tabStock Reconciliation Item` sri ON sri.parent = sr.name
        WHERE {0}
        GROUP BY sr.name
        ORDER BY sr.posting_date DESC, sr.name DESC
    """.format(" AND ".join(conditions)), values, as_dict=True)

    included = [r for r in all_rows if r.purpose != "Opening Stock"]
    excluded = [r for r in all_rows if r.purpose == "Opening Stock"]
    total_value = sum(flt(r.value_change) for r in included)
    excluded_value = sum(flt(r.value_change) for r in excluded)

    return {
        "rows": [{"name": r.name, "posting_date": r.posting_date, "purpose": r.purpose,
                   "item_count": r.item_count, "value_change": flt(r.value_change)} for r in included],
        "total_value": total_value,
        "excluded_opening_stock": {"count": len(excluded), "value": excluded_value},
        "message": ("Physical-count adjustments, purpose = 'Opening Stock' excluded (system "
                    "go-live balance loads, not ongoing corrections)." +
                    (" {0} Opening Stock document(s) worth {1:,.2f} excluded from this range."
                     .format(len(excluded), excluded_value) if excluded else "")),
    }


@frappe.whitelist()
def get_stock_reconciliation_drilldown(name):
    """Line items for one Stock Reconciliation document, for the S6 click-through."""
    rows = frappe.db.sql("""
        SELECT sri.item_code AS item_code, sri.item_name AS item_name, sri.warehouse AS warehouse,
               sri.current_qty AS current_qty, sri.qty AS qty,
               sri.quantity_difference AS quantity_difference,
               sri.current_valuation_rate AS current_valuation_rate,
               sri.valuation_rate AS valuation_rate, sri.amount_difference AS amount_difference
        FROM `tabStock Reconciliation Item` sri
        WHERE sri.parent = %(name)s
        ORDER BY ABS(sri.amount_difference) DESC
    """, {"name": name}, as_dict=True)
    return rows


# ============================================================= S7 — COGS & gross profit

@frappe.whitelist()
def get_cogs_profit(from_date, to_date, company=None):
    """
    Net Sales (invoice-line revenue, same identity Page 1's B1 already
    validated to tie to the CEO dashboard's Net Sales to the cent) minus
    COGS (GL postings to the Cost-of-Goods-Sold account, Sales-Invoice-
    voucher-only — the slice that ties exactly to Stock Ledger Entry, see
    this module's docstring) = Gross Profit. A small non-Sales-Invoice
    residual on the same COGS account is reported separately, not folded
    into the headline.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    accounts = cogs_accounts(company)
    if not accounts:
        return {"message": "No Cost of Goods Sold-type account found for this company."}

    si_cond = "AND si.company = %(company)s" if company else ""
    gl_cond = "AND gle.company = %(company)s" if company else ""
    values = {"from_date": from_date, "to_date": to_date, "company": company, "accounts": list(accounts)}

    sales_rows = frappe.db.sql("""
        SELECT sii.item_group AS item_group, SUM(sii.base_net_amount) AS net_amount
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE si.docstatus = 1 AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s {si_cond}
        GROUP BY sii.item_group
    """.format(si_cond=si_cond), values, as_dict=True)
    net_sales = sum(flt(r.net_amount) for r in sales_rows)
    revenue_by_group = {r.item_group or "Unclassified": flt(r.net_amount) for r in sales_rows}

    cogs_split = frappe.db.sql("""
        SELECT gle.voucher_type AS voucher_type, SUM(gle.debit - gle.credit) AS net
        FROM `tabGL Entry` gle
        WHERE gle.account IN %(accounts)s AND gle.is_cancelled = 0
          AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s {gl_cond}
        GROUP BY gle.voucher_type
    """.format(gl_cond=gl_cond), values, as_dict=True)
    cogs_sales = sum(flt(r.net) for r in cogs_split if r.voucher_type == "Sales Invoice")
    cogs_other = sum(flt(r.net) for r in cogs_split if r.voucher_type != "Sales Invoice")

    cogs_by_group_rows = frappe.db.sql("""
        SELECT sle.item_code AS item_code, SUM(sle.stock_value_difference) AS value_change
        FROM `tabStock Ledger Entry` sle
        WHERE sle.voucher_type = 'Sales Invoice' AND sle.is_cancelled = 0
          AND sle.posting_date BETWEEN %(from_date)s AND %(to_date)s
          {0}
        GROUP BY sle.item_code
    """.format("AND sle.company = %(company)s" if company else ""), values, as_dict=True)
    item_info = _item_group_map({r.item_code for r in cogs_by_group_rows})
    cogs_by_group = {}
    for r in cogs_by_group_rows:
        ig = item_info.get(r.item_code, {}).get("item_group") or "Unclassified"
        cogs_by_group[ig] = cogs_by_group.get(ig, 0.0) - flt(r.value_change)  # flip sign: cost, not stock delta

    groups = sorted(set(revenue_by_group) | set(cogs_by_group))
    by_item_group = []
    for ig in groups:
        rev = revenue_by_group.get(ig, 0.0)
        cogs = cogs_by_group.get(ig, 0.0)
        gp = rev - cogs
        by_item_group.append({
            "item_group": ig, "revenue": rev, "cogs": cogs, "gross_profit": gp,
            "margin_pct": (gp / rev) if rev else 0.0,
        })
    by_item_group.sort(key=lambda d: -d["revenue"])

    gross_profit = net_sales - cogs_sales
    margin_pct = (gross_profit / net_sales) if net_sales else 0.0

    return {
        "net_sales": net_sales, "cogs": cogs_sales, "gross_profit": gross_profit,
        "margin_pct": margin_pct, "cogs_other_residual": cogs_other,
        "by_item_group": by_item_group,
        "message": ("Net Sales is invoice-line revenue (Sales Invoice Item, same figure Page "
                    "1's revenue-by-item-group uses). COGS is the Sales-Invoice-attributable "
                    "share of the Cost of Goods Sold account's GL postings — the slice that "
                    "reconciles exactly to Stock Ledger Entry's stock value change on those "
                    "same vouchers." +
                    (" {0:,.2f} of additional COGS-account postings this range came from "
                     "non-Sales-Invoice vouchers (stock corrections, direct purchase postings) "
                     "and is not included in the margin above."
                     .format(cogs_other) if abs(cogs_other) >= 0.01 else "")),
    }


# ============================================================= S8 — received vs invoiced

@frappe.whitelist()
def get_grn_vs_invoiced(from_date, to_date, company=None):
    """
    Item-level received (Purchase Receipt Item.amount) vs invoiced
    (Purchase Receipt Item.billed_amt — ERPNext's own running total,
    reliable in this instance even though the purchase_invoice back-link
    column is not populated, see this module's docstring).
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    conditions = ["pr.posting_date BETWEEN %(from_date)s AND %(to_date)s", "pr.docstatus = 1"]
    values = {"from_date": from_date, "to_date": to_date}
    if company:
        conditions.append("pr.company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT pri.item_code AS item_code, SUM(pri.qty) AS received_qty,
               SUM(pri.amount) AS received_amount, SUM(pri.billed_amt) AS billed_amount
        FROM `tabPurchase Receipt Item` pri
        INNER JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
        WHERE {0}
        GROUP BY pri.item_code
    """.format(" AND ".join(conditions)), values, as_dict=True)

    item_info = _item_group_map({r.item_code for r in rows})
    data = []
    total_received = total_billed = 0.0
    for r in rows:
        gap = flt(r.received_amount) - flt(r.billed_amount)
        info = item_info.get(r.item_code, {})
        data.append({
            "item_code": r.item_code, "item_name": info.get("item_name") or r.item_code,
            "received_qty": flt(r.received_qty), "received_amount": flt(r.received_amount),
            "billed_amount": flt(r.billed_amount), "gap": gap,
        })
        total_received += flt(r.received_amount); total_billed += flt(r.billed_amount)
    data.sort(key=lambda d: -abs(d["gap"]))

    return {
        "rows": data, "total_received": total_received, "total_billed": total_billed,
        "total_gap": total_received - total_billed,
        "message": ("Purchase Receipt items in range vs their billed_amt (ERPNext's own "
                     "running total). A positive gap means received but not yet fully "
                     "invoiced — the item-level companion to Payables' C2 GL balance."),
    }