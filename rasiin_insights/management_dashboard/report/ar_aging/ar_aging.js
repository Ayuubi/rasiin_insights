// Copyright (c) 2026, Rasiin Technology and contributors
// For license information, please see license.txt
/* eslint-disable */

// Path: rasiin_insights/rasiin_insights/management_dashboard/report/ar_aging/ar_aging.js
frappe.query_reports["AR Aging"] = {
	filters: [
		{
			fieldname: "as_of_date",
			label: "As of date",
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
