// Copyright (c) 2026, Rasiin Technology and contributors
// For license information, please see license.txt
/* eslint-disable */

// Path: rasiin_insights/rasiin_insights/management_dashboard/report/top_items/top_items.js
frappe.query_reports["Top Items"] = {
	filters: [
		{
			fieldname: "from_date",
			label: "From date",
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
			reqd: 1
		},
		{
			fieldname: "to_date",
			label: "To date",
			fieldtype: "Date",
			default: frappe.datetime.month_end(),
			reqd: 1
		},
		{
			fieldname: "company",
			label: "Company",
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_default("company")
		},
		{
			fieldname: "top_n",
			label: "Top N",
			fieldtype: "Int",
			default: 20
		},
		{
			fieldname: "rank_by",
			label: "Rank by",
			fieldtype: "Select",
			options: "Net Sales\nGross Sales",
			default: "Net Sales"
		}
	]
};
