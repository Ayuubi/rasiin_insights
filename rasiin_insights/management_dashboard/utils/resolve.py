"""
Dimension resolver.

Resolves Channel, Entity, Payer Type and Service Line from raw source values,
using effective-dated rules stored in `Management Dimension Rule`.

Path: rasiin_insights/management_dashboard/utils/resolve.py

Design rules (do not change without reading BUILD_PLAN.md section 5b):

1. Rules are tried in priority order. First match wins.
2. A rule with no `source_field` returns its `fallback_value` and ends the chain.
   `fallback_value` on a sourced rule is ignored.
3. A source value that is empty, or present but not in the mapping, falls through
   to the next rule. It is not an error.
4. Rules are effective-dated. A row is resolved with the rule that was valid on its
   own posting_date, so changing a rule never rewrites history.
5. The resolver never reads the database per row. Rules are loaded once per
   instance and results are memoised.
"""

import frappe
from frappe.utils import getdate

# Maps the `source_field` Select option to the key the extractor supplies
# in its source_values dict.
SOURCE_FIELD_KEYS = {
    "Item Group": "item_group",
    "Sales Type": "sales_type",
    "Cost Center": "cost_center",
    "Income Account": "income_account",
    "Warehouse": "warehouse",
    "Company": "company",
    "Insurance Flag": "insurance_flag",
    "Customer Group": "customer_group",
}

UNRESOLVED = "Unclassified"


def build_insurance_flag(is_insurance, insurance):
    """
    Insurance is only real when BOTH the checkbox is ticked AND the insurer is named.
    Confirmed against production data: the flag is never set without the field
    (January 50 invoices, July 374, zero flag-only cases), while a handful of
    invoices name an insurer without the flag (January 1 = $9.00, July 4 = $57.50).
    Those are incomplete records, not insurance business, so they fall through to
    the customer group rule and are tagged by the extractor.

    Returns "1" or "0" — always a string, so the mapping table stays readable.
    """
    has_flag = str(is_insurance or "0") in ("1", "True", "true", "Yes")
    has_insurer = bool(insurance) and str(insurance).strip() != ""
    return "1" if (has_flag and has_insurer) else "0"


def normalise(value):
    """Everything compared against the mapping table is a trimmed string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


class DimensionResolver:
    """
    Load once per build run, then call resolve() per row.

        r = DimensionResolver()
        channel, src = r.resolve("Channel", posting_date, source_values)
    """

    def __init__(self):
        self._rules = {}       # dimension -> [rule dicts, priority order]
        self._memo = {}        # (dimension, date_key, memo_key) -> (value, source)
        self._load()

    # ------------------------------------------------------------------ load

    def _load(self):
        rules = frappe.get_all(
            "Management Dimension Rule",
            filters={"is_active": 1},
            fields=[
                "name", "dimension", "priority", "source_field",
                "valid_from", "valid_to", "fallback_value",
            ],
            order_by="dimension asc, priority asc",
        )
        if not rules:
            frappe.log_error(
                "No active Management Dimension Rule rows found. "
                "Every dimension will resolve to Unclassified.",
                "Rasiin Insights",
            )

        mappings = frappe.get_all(
            "Management Dimension Mapping",
            filters={"parent": ["in", [r.name for r in rules]]} if rules else {"parent": ["in", [""]]},
            fields=["parent", "source_value", "dimension_value"],
        )
        by_parent = {}
        for m in mappings:
            by_parent.setdefault(m.parent, {})[normalise(m.source_value)] = m.dimension_value

        for r in rules:
            self._rules.setdefault(r.dimension, []).append({
                "name": r.name,
                "priority": r.priority,
                "source_field": r.source_field or None,
                "source_key": SOURCE_FIELD_KEYS.get(r.source_field) if r.source_field else None,
                "valid_from": getdate(r.valid_from) if r.valid_from else None,
                "valid_to": getdate(r.valid_to) if r.valid_to else None,
                "fallback_value": r.fallback_value,
                "mapping": by_parent.get(r.name, {}),
            })

        # An unknown source_field would silently resolve nothing. Fail loudly instead.
        for dimension, rules_for_dim in self._rules.items():
            for rule in rules_for_dim:
                if rule["source_field"] and not rule["source_key"]:
                    frappe.throw(
                        "Rule {0} uses source field '{1}', which the resolver does not "
                        "know how to read. Add it to SOURCE_FIELD_KEYS in resolve.py."
                        .format(rule["name"], rule["source_field"])
                    )

    # --------------------------------------------------------------- resolve

    def _active_rules(self, dimension, on_date):
        out = []
        for rule in self._rules.get(dimension, []):
            if rule["valid_from"] and on_date < rule["valid_from"]:
                continue
            if rule["valid_to"] and on_date > rule["valid_to"]:
                continue
            out.append(rule)
        return out

    def resolve(self, dimension, posting_date, source_values):
        """
        Returns (dimension_value, source_label).

        source_label records which rule produced the answer, so a report can
        explain itself: "Pharmacy (Item Group)" vs "Pharmacy (Sales Type)".
        """
        on_date = getdate(posting_date)
        rules = self._active_rules(dimension, on_date)
        if not rules:
            return UNRESOLVED, "no active rule"

        # Memo key uses only the source values this dimension actually consumes,
        # so repeated item groups and customer groups hit the cache.
        keys = tuple(r["source_key"] for r in rules if r["source_key"])
        memo_key = (
            dimension,
            on_date.strftime("%Y-%m"),
            tuple(normalise(source_values.get(k)) for k in keys),
        )
        cached = self._memo.get(memo_key)
        if cached:
            return cached

        result = (UNRESOLVED, "unresolved")
        for rule in rules:
            if not rule["source_key"]:
                # Fallback-only rule: ends the chain.
                result = (rule["fallback_value"] or UNRESOLVED, "fallback")
                break
            value = normalise(source_values.get(rule["source_key"]))
            if not value:
                continue
            mapped = rule["mapping"].get(value)
            if mapped:
                result = (mapped, rule["source_field"])
                break

        self._memo[memo_key] = result
        return result

    def resolve_all(self, posting_date, source_values, dimensions=None):
        """
        Resolve every dimension in one call. This is what the extractor uses.

            {"channel": "Pharmacy", "channel_source": "Item Group",
             "entity": "Main Hospital", "entity_source": "Cost Center", ...}
        """
        dimensions = dimensions or ["Channel", "Entity", "Payer Type", "Service Line"]
        out = {}
        for dim in dimensions:
            value, source = self.resolve(dim, posting_date, source_values)
            slug = dim.lower().replace(" ", "_")
            out[slug] = value
            out[slug + "_source"] = source
        return out

    # ------------------------------------------------------------ diagnostics

    def describe(self):
        """Print the loaded rule chain. Use when a figure resolves unexpectedly."""
        lines = []
        for dimension in sorted(self._rules):
            lines.append(dimension)
            for rule in self._rules[dimension]:
                window = "{0} to {1}".format(
                    rule["valid_from"] or "always", rule["valid_to"] or "current")
            
                if rule["source_key"]:
                    lines.append("  {0}. {1:<16} {2:<24} {3} mappings".format(
                        rule["priority"], rule["source_field"], window, len(rule["mapping"])))
                else:
                    lines.append("  {0}. {1:<16} {2:<24} -> {3}".format(
                        rule["priority"], "(fallback)", window, rule["fallback_value"]))
        return "\n".join(lines)


# --------------------------------------------------------------------- tests

def self_test():
    """
    Run against the real rules in this site:

        bench --site shaafi execute \
          rasiin_insights.management_dashboard.utils.resolve.self_test

    Every case below is drawn from real Shaafi data. If one fails, the rules in
    `Management Dimension Rule` do not match what the dashboard was reconciled on.
    """
    r = DimensionResolver()
    print("\nLoaded rules\n" + "-" * 60)
    print(r.describe())

    cases = [
        # (label, dimension, date, source_values, expected_value, expected_source)
        ("Drug line resolves by item group",
         "Channel", "2026-07-15",
         {"item_group": "Drug", "sales_type": "Cashiers"},
         "Pharmacy", "Item Group"),

        ("Lab line rung up at the pharmacy counter stays Hospital",
         "Channel", "2026-07-15",
         {"item_group": "Laboratory", "sales_type": "Pharmacy"},
         "Hospital", "fallback"),

        ("Unmapped item group with no sales type falls back",
         "Channel", "2026-07-15",
         {"item_group": "Ambulance", "sales_type": None},
         "Hospital", "fallback"),

        ("Consumable is pharmacy too",
         "Channel", "2026-01-15",
         {"item_group": "Consumable"},
         "Pharmacy", "Item Group"),

        ("Diagnostic Center splits out by cost centre",
         "Entity", "2026-07-15",
         {"cost_center": "Shaafi Diagnostic Center - SH"},
         "Diagnostic Center", "Cost Center"),

        ("Pharmacy cost centre is still the main hospital entity",
         "Entity", "2026-07-15",
         {"cost_center": "Pharmacy - SH"},
         "Main Hospital", "Cost Center"),

        ("Blank cost centre falls back to Main Hospital",
         "Entity", "2026-07-15",
         {"cost_center": None},
         "Main Hospital", "fallback"),

        ("Insurance needs BOTH the flag and the insurer",
         "Payer Type", "2026-07-15",
         {"insurance_flag": build_insurance_flag(1, "AMANAH"),
          "customer_group": "All Customer Groups"},
         "Insurance", "Insurance Flag"),

        ("Insurer named but flag not ticked is not insurance",
         "Payer Type", "2026-07-15",
         {"insurance_flag": build_insurance_flag(0, "AMANAH"),
          "customer_group": "All Customer Groups"},
         "Cash patient", "fallback"),

        ("Flag ticked with no insurer is not insurance",
         "Payer Type", "2026-07-15",
         {"insurance_flag": build_insurance_flag(1, None),
          "customer_group": "All Customer Groups"},
         "Cash patient", "fallback"),

        ("Corporate resolves by customer group",
         "Payer Type", "2026-07-15",
         {"insurance_flag": "0", "customer_group": "Corporate"},
         "Corporate", "Customer Group"),

        ("Membership resolves by customer group",
         "Payer Type", "2026-07-15",
         {"insurance_flag": "0", "customer_group": "Membership"},
         "Membership", "Customer Group"),

        ("Default customer group is a cash patient",
         "Payer Type", "2026-01-15",
         {"insurance_flag": "0", "customer_group": "All Customer Groups"},
         "Cash patient", "fallback"),
    ]

    print("\nResults\n" + "-" * 60)
    passed = failed = 0
    for label, dimension, date, values, want_value, want_source in cases:
        got_value, got_source = r.resolve(dimension, date, values)
        ok = (got_value == want_value and got_source == want_source)
        if ok:
            passed += 1
            print("  PASS  {0}".format(label))
        else:
            failed += 1
            print("  FAIL  {0}".format(label))
            print("        expected {0!r} via {1!r}".format(want_value, want_source))
            print("        got      {0!r} via {1!r}".format(got_value, got_source))

    print("-" * 60)
    print("{0} passed, {1} failed\n".format(passed, failed))

    # resolve_all shape check
    combined = r.resolve_all("2026-07-15", {
        "item_group": "Drug",
        "cost_center": "Main - SH",
        "insurance_flag": "0",
        "customer_group": "Membership",
    })
    print("resolve_all sample:")
    for k in sorted(combined):
        print("   {0:<20} {1}".format(k, combined[k]))

    return {"passed": passed, "failed": failed}