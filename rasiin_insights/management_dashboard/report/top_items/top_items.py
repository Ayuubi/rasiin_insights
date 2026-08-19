# Copyright (c) 2026, Rasiin Technology and contributors
# For license information, please see license.txt

"""
Top Items — which services/products earn the most, by net sales.

Path: rasiin_insights/rasiin_insights/management_dashboard/report/top_items/top_items.py

Answers Question 9 in the catalog: "Which 20 services earn us the most?"

Item-level detail is deliberately not a Management Snapshot dimension —
same reasoning as leaving customer out of it (snapshot.py: thousands of
distinct values x every metric, for a question nobody asks of a summary).
This reads Management Fact directly, live, for whatever date range is
asked — cheap, since it's one indexed GROUP BY over a bounded range, not a
scan of the whole table.
"""

import frappe
from frappe.utils import flt, cint, getdate, get_first_day, get_last_day


def execute(filters=None):
    filters = frappe._dict(filters or {})
    from_date = getdate(filters.from_date) if filters.from_date else get_first_day(getdate())
    to_date = getdate(filters.to_date) if filters.to_date else get_last_day(getdate())
    company = filters.company
    top_n = cint(filters.top_n) or 20
    rank_by = filters.rank_by or "Net Sales"

    columns = [
        {"label": "Rank", "fieldname": "rank", "fieldtype": "Int", "width": 60},
        {"label": "Item Code", "fieldname": "item_code", "fieldtype": "Data", "width": 130},
        {"label": "Item Name", "fieldname": "item_name", "fieldtype": "Data", "width": 220},
        {"label": "Item Group", "fieldname": "item_group", "fieldtype": "Data", "width": 150},
        {"label": "Gross Sales", "fieldname": "gross_sales", "fieldtype": "Currency", "width": 120},
        {"label": "Discount", "fieldname": "discount", "fieldtype": "Currency", "width": 110},
        {"label": "Returns", "fieldname": "return_amt", "fieldtype": "Currency", "width": 110},
        {"label": "Net Sales", "fieldname": "net_sales", "fieldtype": "Currency", "width": 120},
        {"label": "Share of Net Sales", "fieldname": "share", "fieldtype": "Percent", "width": 120},
    ]

    conditions = [
        "posting_date BETWEEN %(from)s AND %(to)s",
        "metric IN ('gross_sales', 'discount', 'return', 'return_discount')",
        "item_code IS NOT NULL AND item_code != ''",
    ]
    values = {"from": from_date, "to": to_date}
    if company:
        conditions.append("company = %(company)s")
        values["company"] = company

    rows = frappe.db.sql("""
        SELECT item_code,
               MAX(item_name) AS item_name, MAX(item_group) AS item_group,
               metric, SUM(amount) AS amount
        FROM `tabManagement Fact`
        WHERE {0}
        GROUP BY item_code, metric
    """.format(" AND ".join(conditions)), values, as_dict=True)

    if not rows:
        return columns, [], "No item-level sales in this range.", None, []

    items = {}
    for r in rows:
        d = items.setdefault(r.item_code, {
            "item_code": r.item_code, "item_name": r.item_name,
            "item_group": r.item_group, "gross_sales": 0.0,
            "discount": 0.0, "return_amt": 0.0, "return_discount": 0.0,
        })
        if r.metric == "return":
            d["return_amt"] += flt(r.amount)
        elif r.metric == "return_discount":
            d["return_discount"] += flt(r.amount)
        else:
            d[r.metric] += flt(r.amount)

    for d in items.values():
        d["net_sales"] = (d["gross_sales"] - d["discount"]
                           - d["return_amt"] + d["return_discount"])

    sort_key = "gross_sales" if rank_by == "Gross Sales" else "net_sales"
    ranked = sorted(items.values(), key=lambda d: -d[sort_key])[:top_n]

    total_net = sum(d["net_sales"] for d in items.values())
    data = []
    for i, d in enumerate(ranked, start=1):
        d["rank"] = i
        d["share"] = (d["net_sales"] / total_net) if total_net else 0.0
        data.append(d)

    message = ("Ranked by {0}, {1} to {2}. Read live from Management Fact — "
               "item is not a snapshot dimension, the same reason customer "
               "isn't either.").format(rank_by, from_date, to_date)

    report_summary = [
        {"label": "Items with sales in range", "value": len(items), "datatype": "Int"},
        {"label": "Top {0} share of net sales".format(len(data)),
         "value": round(sum(d["share"] for d in data) * 100, 1), "datatype": "Percent"},
    ]

    return columns, data, message, None, report_summary
