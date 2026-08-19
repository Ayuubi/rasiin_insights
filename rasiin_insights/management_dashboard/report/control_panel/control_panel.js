// Copyright (c) 2026, Rasiin Technology and contributors
// For license information, please see license.txt
/* eslint-disable */

// Path: rasiin_insights/rasiin_insights/management_dashboard/report/control_panel/control_panel.js
frappe.query_reports["Control Panel"] = {
	filters: [
		{
			fieldname: "period",
			label: "Period (YYYY-MM)",
			fieldtype: "Data",
			default: frappe.datetime.get_today().slice(0, 7),
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
