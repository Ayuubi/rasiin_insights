// Copyright (c) 2026, Rasiin Technology and contributors
// For license information, please see license.txt

// 'Rebuild a period' button — added 2026-08-22, alongside
// snapshot.enqueue_manual_rebuild(). Runs the same work the nightly job
// does (utils/snapshot.py's build_period()), for one period, on demand,
// without a terminal. Always available regardless of MD Enabled / MD Run
// Hour / MD Rebuild Months — those gate the scheduler only, never a
// person asking for one period by name.

frappe.ui.form.on('Rasiin Insights Settings', {
	refresh: function (frm) {
		frm.add_custom_button('Rebuild a Period', () => {
			show_rebuild_dialog(frm);
		});
	},
});

function show_rebuild_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: 'Rebuild a Period',
		fields: [
			{
				fieldtype: 'Data', fieldname: 'period', label: 'Period',
				description: 'YYYY-MM, e.g. 2026-01. Runs the full rebuild for this ' +
					'month only — Sales, non-invoice revenue, collections, balances, ' +
					'money out, then the snapshot.',
				reqd: 1,
			},
			{
				fieldtype: 'Link', fieldname: 'company', label: 'Company',
				options: 'Company',
				description: 'Leave blank to rebuild every company that had a ' +
					'balance or activity in this period.',
			},
			{
				fieldtype: 'HTML', fieldname: 'note',
				options: '<div class="text-muted" style="font-size:12px;">' +
					'Runs in the background — a full period can take a few minutes. ' +
					'A row appears in <b>Management Build Log</b> when it finishes, ' +
					'with the same drift check the nightly job runs.</div>',
			},
		],
		primary_action_label: 'Queue Rebuild',
		primary_action: (values) => {
			const period = (values.period || '').trim();
			if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(period)) {
				frappe.msgprint({
					title: 'Check the period',
					message: 'Period must be in YYYY-MM form, e.g. 2026-01.',
					indicator: 'red',
				});
				return;
			}

			frappe.call({
				method: 'rasiin_insights.management_dashboard.utils.snapshot.enqueue_manual_rebuild',
				args: { period, company: values.company || null },
				freeze: true,
				freeze_message: 'Queueing rebuild for ' + period + '...',
				callback: () => {
					d.hide();
					frappe.show_alert({
						message: 'Queued: ' + period + '. Check Management Build Log for the result.',
						indicator: 'blue',
					}, 8);
					frappe.msgprint({
						title: 'Rebuild queued',
						indicator: 'blue',
						message: 'Rebuilding ' + period +
							(values.company ? ' for ' + values.company : ' for every company with activity that period') +
							' in the background. Open the <a href="/app/management-build-log?periods_rebuilt=' +
							encodeURIComponent(period) + '">Management Build Log</a> list in a minute or two ' +
							'to see whether it finished clean or drifted.',
					});
				},
			});
		},
	});
	d.show();
}