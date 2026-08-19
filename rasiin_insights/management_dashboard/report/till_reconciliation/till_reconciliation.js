// Copyright (c) 2026, Rasiin Technology and contributors
// For license information, please see license.txt
/* eslint-disable */

// Path: rasiin_insights/rasiin_insights/management_dashboard/report/till_reconciliation/till_reconciliation.js
frappe.query_reports["Till Reconciliation"] = {
	filters: [
		{
			fieldname: "from_date",
			label: "From date",
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -6),
			reqd: 1
		},
		{
			fieldname: "to_date",
			label: "To date",
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1
		},
		{
			fieldname: "company",
			label: "Company",
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_default("company")
		}
	]
};
