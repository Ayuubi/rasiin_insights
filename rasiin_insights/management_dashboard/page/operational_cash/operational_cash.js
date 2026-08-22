/*
 * Operational Reports — Page 2: Cash & Collections.
 *
 * Path: rasiin_insights/management_dashboard/page/operational_cash/
 *         operational_cash.js
 *
 * Daily, transaction-level, for finance/accountants closing the day.
 * Reads live from operational_api_cash.py, which reads live from Sales
 * Invoice Payment / Payment Entry / GL Entry (via till_reconciliation.py,
 * reused directly, not re-derived) — never the monthly snapshot.
 *
 * COMPANY DEFAULT — fixed 2026-08-22, see operational_receivables.js for
 * the full story: this page defaults Company to the same value the CEO
 * dashboard defaults to (via get_filters()), not blank/"all companies".
 *
 * D1 — REWRITTEN 2026-08-22. "Total collected" used to run about $29,600
 * short of the CEO dashboard's "Money received" for the same range,
 * because the old server-side logic used a different, self-invented rule
 * (same-calendar-day matching, no Journal Entry cash, no overallocation
 * scaling). operational_api_cash.py now replicates the CEO dashboard's
 * own collection-facts logic exactly — see that file's D1 docstring for
 * the full explanation and the validated numbers.
 *
 * FILTERS — rebuilt again 2026-08-22, same reasoning as
 * operational_receivables.js: page.add_field() now, the exact call the
 * CEO dashboard makes, instead of a hand-rendered boxed row.
 *
 * NAV — rebuilt again 2026-08-22, same reasoning as
 * operational_receivables.js: real <a href="/app/..."> tags, not
 * <button> + frappe.set_route(), which left the destination blank until
 * a manual refresh.
 *
 * DIALOGS — fixed 2026-08-22, same reasoning as
 * operational_receivables.js: a shared open_dialog()/guard_dblclick()
 * pair so every drilldown gets the same scroll wrapper (Remarks no
 * longer spills out past the dialog edge) and a fast double-click can't
 * open the same dialog twice.
 *
 * D1 / E1 — day tables now paginate client-side for the same reason A1's
 * does on Page 1: a multi-month range used to render one row per day
 * straight onto the page.
 *
 * D3 — one row per account (not one row per account per day, which is
 * what made the page run to 500+ rows). Click a row for the day-by-day
 * detail, click a day inside that for the GL vouchers behind it.
 *
 * EXPORT
 *   Same client-side CSV pattern as Page 1 / the CEO dashboard.
 */

frappe.pages['operational-cash'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper, title: 'Cash & Collections', single_column: true
	});
	new OperationalCash(page).init();
};

const ROUTES = [
	{ label: 'CEO Dashboard', route: 'management-dashboard' },
	{ label: 'Receivables & Revenue', route: 'operational-receivables' },
	{ label: 'Cash & Collections', route: 'operational-cash', current: true },
	// { label: 'Expenses & Payables', route: 'operational-expenses' },
];

const DAY_PAGE_SIZE = 31;

class OperationalCash {
	constructor(page) {
		this.page = page;
		this.$c = $(page.body);
	}

	async init() {
		this.build_menu();
		this.build_layout();
		this.render_nav();
		this.filter_meta = await this.call_filters();
		this.build_filters();
		this.bind_events();
		this.refresh_all();
	}

	call_filters() {
		return frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.api.get_filters',
			args: {},
		}).then(r => r.message || {});
	}

	// ------------------------------------------------------------------- nav

	build_menu() {
		ROUTES.filter(r => !r.current).forEach(r => {
			this.page.add_menu_item(r.label, () => frappe.set_route(r.route));
		});
	}

	render_nav() {
		const html = ROUTES.map(r =>
			`<a class="rd-mode${r.current ? ' active' : ''}" data-route="${r.route}" href="/app/${r.route}">${r.label}</a>`
		).join('');
		this.$c.find('#oc-suite-nav').html(html);

		// FIXED 2026-08-22: the <a href> alone still went blank until a
		// manual refresh — Frappe's own document-level link handler was
		// intercepting the click and doing its own client-side routing
		// before the destination page's JS was necessarily loaded, exactly
		// like the <button> it replaced. This handler runs first (it's
		// bound on the link itself, not on document), stops that handler
		// from ever seeing the click, and forces a real full-page load —
		// the same thing a manual refresh does, which is why that always
		// "fixed" it.
		this.$c.find('#oc-suite-nav a').on('click', (e) => {
			if ($(e.currentTarget).hasClass('active')) { e.preventDefault(); return; }
			e.preventDefault();
			e.stopPropagation();
			window.location.href = e.currentTarget.href;
		});
	}

	// --------------------------------------------------------------- filters

	// FIXED 2026-08-22, take 4: page.add_field() does not reliably render
	// into this page's toolbar at all, for ANY field type — the previous
	// fix moved From/To out of it on the (correct) theory that Date wasn't
	// supported, but left Company on page.add_field() because it's a
	// Select, the one type proven to work on the CEO dashboard. It doesn't
	// work here — the deployed screenshot shows no Company control
	// anywhere. Three rounds of chasing the toolbar (class toggle,
	// !important CSS, field-type swap) haven't found what's different
	// about it, so this stops trying to use it at all. All three filters
	// are now plain HTML controls in their own strip in the page body,
	// styled to look like a toolbar (light background, sits right under
	// the title, tabs below it) — same visual order as the CEO
	// dashboard's real toolbar-then-tabs layout, built with a mechanism
	// proven to actually render on this page.

	build_filters() {
		const companies = this.filter_meta.companies || [];
		const today = frappe.datetime.get_today();
		const week_ago = frappe.datetime.add_days(today, -6);
		const default_company = this.filter_meta.default_company || 'All companies';

		this.$c.find('#oc-filterbar').html(`
			<div class="oc-filter-field">
				<label>From</label>
				<input type="date" class="oc-from" value="${week_ago}">
			</div>
			<div class="oc-filter-field">
				<label>To</label>
				<input type="date" class="oc-to" value="${today}">
			</div>
			<div class="oc-filter-field">
				<label>Company</label>
				<select class="oc-company">
					${['All companies'].concat(companies).map(c =>
						`<option value="${c}" ${c === default_company ? 'selected' : ''}>${c}</option>`
					).join('')}
				</select>
			</div>
			<button class="oc-btn oc-refresh">Refresh</button>
		`);
		this.$c.find('.oc-from, .oc-to, .oc-company').on('change', () => this.refresh_all());
		this.$c.find('.oc-refresh').on('click', () => this.refresh_all());
	}

	get filters() {
		const company = this.$c.find('.oc-company').val();
		return {
			from_date: this.$c.find('.oc-from').val(),
			to_date: this.$c.find('.oc-to').val(),
			company: (company && company !== 'All companies') ? company : null,
		};
	}

	// ---------------------------------------------------------------- layout

	build_layout() {
		this.$c.html(`
			<div class="rd oc-page">
				<style>
					.oc-page { --ink:#0f172a; --dim:#64748b; --line:#e2e8f0; --navy:#1e3a5f;
						--good:#15803d; --bad:#b91c1c; --amber:#a16207;
						padding:15px 15px 50px; color:var(--ink); }

					.oc-topbar { margin:16px 0 18px; }

					/* POLISHED 2026-08-22 — dropped the bordered/shaded card
					   look; a flush row (no box) sits closer to how the CEO
					   dashboard's own native toolbar reads — controls right
					   under the title, not visually boxed off as their own
					   section. */
					.oc-filterbar { display:flex; align-items:center; gap:20px; flex-wrap:wrap;
						padding:4px 2px 16px; margin:0; border-bottom:1px solid var(--line); }
					.oc-filter-field { display:flex; align-items:center; gap:8px; }
					.oc-filter-field label { font-size:12px; color:var(--dim); font-weight:600; }
					.oc-filter-field input[type=date], .oc-filter-field select {
						padding:5px 9px; border-radius:6px; border:1px solid var(--line);
						font-size:12px; background:#fff; color:var(--ink); line-height:1.4; }
					.oc-filter-field input[type=date]:focus, .oc-filter-field select:focus {
						outline:none; border-color:var(--navy); }
					.oc-filterbar .oc-refresh { margin-left:auto; }
					.rd-modes { display:inline-flex; background:#f1f5f9; border-radius:10px; padding:3px; }
					.rd-mode { border:0; background:transparent; padding:7px 18px; border-radius:8px;
						font-size:13px; cursor:pointer; color:var(--dim); display:inline-block;
						text-decoration:none; line-height:1.4; }
					.rd-mode.active { background:#fff; color:var(--navy); font-weight:600;
						box-shadow:0 1px 3px rgba(15,23,42,.12); cursor:default; }
					.rd-mode:not(.active):hover { color:var(--navy); text-decoration:none; }

					.oc-section { background:#fff; border:1px solid var(--line); border-radius:12px;
						padding:16px 18px 20px; margin-bottom:18px; }
					.oc-section h4 { display:flex; justify-content:space-between; align-items:center;
						flex-wrap:wrap; gap:10px; font-size:14px; font-weight:700; letter-spacing:-.01em;
						margin:0 0 14px; }
					.oc-btn { border:1px solid var(--line); background:#fff; border-radius:8px;
						padding:5px 12px; font-size:12px; cursor:pointer; color:var(--dim); }
					.oc-btn:hover { border-color:var(--navy); color:var(--navy); }
					.oc-btn:disabled { opacity:.4; cursor:default; }
					.oc-btn:disabled:hover { border-color:var(--line); color:var(--dim); }

					.oc-cards { display:grid; gap:11px; grid-template-columns:repeat(auto-fit,minmax(185px,1fr));
						margin-bottom:14px; }
					.oc-card { border:1px solid var(--line); border-left:3px solid var(--line); border-radius:10px;
						padding:13px 15px; background:#fff; }
					.oc-card .label { font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:.05em; }
					.oc-card .value { font-size:20px; font-weight:700; margin-top:4px; font-variant-numeric:tabular-nums; }
					.oc-card.warn { border-left-color:var(--amber); }
					.oc-card.good { border-left-color:var(--good); }
					.oc-card.cash { border-left-color:var(--good); }

					.oc-msg { font-size:12px; color:var(--dim); margin-top:8px; line-height:1.5; }

					.oc-table-wrap { overflow-x:auto; }
					.oc-table { width:100%; border-collapse:collapse; font-size:13px; }
					.oc-table th { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim);
						text-align:right; border-bottom:2px solid var(--line); padding:8px 10px; white-space:nowrap; }
					.oc-table th:first-child, .oc-table td:first-child { text-align:left; }
					.oc-table td { text-align:right; padding:7px 10px; font-variant-numeric:tabular-nums;
						border-bottom:1px solid #f1f5f9; white-space:nowrap; }
					.oc-table tbody tr:hover td { background:#f8fafc; }
					.oc-table tr.neg td { color:var(--bad); }
					.oc-table tr.warn td { background:#fffbeb; }
					.oc-row-link { cursor:pointer; }
					.oc-pill { display:inline-block; padding:2px 9px; border-radius:20px; font-size:11px; font-weight:600; }
					.oc-pill.ok { background:#dcfce7; color:var(--good); }
					.oc-pill.bad { background:#fee2e2; color:var(--bad); }

					/* a dialog table's free-text columns need to wrap, or a long
					   value pushes the whole table wider than the dialog and
					   spills text out past its edge. FIXED 2026-08-22: this
					   used to be max-width only, with the table itself at
					   width:100% — with no floor on this column's width, the
					   browser squeezed it down to almost nothing to make room
					   for the other (nowrap) columns, wrapping every single
					   word onto its own line and blowing the row up to 15+
					   lines tall. min-width is the actual fix; the table
					   below no longer forces width:100% inside a dialog, so
					   it can overflow into its own horizontal scrollbar
					   instead of crushing this column. */
					.oc-wrap { white-space:normal !important; min-width:200px; max-width:380px;
						word-break:break-word; text-align:left !important; }
					.oc-dialog-scroll { max-height:60vh; overflow:auto; }
					.oc-dialog-scroll table.oc-table { width:auto; min-width:100%; }

					.oc-pager { display:flex; align-items:center; gap:10px; margin-top:10px;
						font-size:12px; color:var(--dim); }

					@media (max-width:640px) {
						.oc-section { padding:13px 12px 16px; }
						.oc-table th, .oc-table td { padding:7px 8px; }
					}
				</style>

				<div class="oc-filterbar" id="oc-filterbar"></div>
				<div class="oc-topbar">
					<div class="rd-modes" id="oc-suite-nav"></div>
				</div>

				<div class="oc-section" id="oc-d1">
					<h4>Cash Collection Split — Sales-Cash vs Debt-Cash <button class="oc-btn oc-export" data-target="d1">Export CSV</button></h4>
					<div class="oc-cards" id="oc-d1-cards"></div>
					<div class="oc-table-wrap"><table class="oc-table" id="oc-d1-table"></table></div>
					<div class="oc-pager" id="oc-d1-pager"></div>
					<div class="oc-msg" id="oc-d1-msg"></div>
				</div>

				<div class="oc-section" id="oc-d2">
					<h4>Collections by Mode of Payment / Cashier <button class="oc-btn oc-export" data-target="d2">Export CSV</button></h4>
					<div class="oc-cards" id="oc-d2-cards"></div>
					<div class="oc-table-wrap"><table class="oc-table" id="oc-d2-table"></table></div>
					<div class="oc-msg" id="oc-d2-msg"></div>
				</div>

				<div class="oc-section" id="oc-d3">
					<h4>Cashier / Till Reconciliation <button class="oc-btn oc-export" data-target="d3">Export CSV</button></h4>
					<div class="oc-cards" id="oc-d3-cards"></div>
					<div class="oc-msg" id="oc-d3-msg-top">One row per till — click a row for the day-by-day detail in range.</div>
					<div class="oc-table-wrap"><table class="oc-table" id="oc-d3-table"></table></div>
					<div class="oc-msg" id="oc-d3-msg"></div>
				</div>

				<div class="oc-section" id="oc-e1">
					<h4>Cash &amp; Bank Position <button class="oc-btn oc-export" data-target="e1">Export CSV</button></h4>
					<div class="oc-cards" id="oc-e1-cards"></div>
					<div class="oc-table-wrap"><table class="oc-table" id="oc-e1-table"></table></div>
					<div class="oc-pager" id="oc-e1-pager"></div>
					<div class="oc-msg" id="oc-e1-msg"></div>
				</div>
			</div>
		`);
	}

	bind_events() {
		this.$c.on('click', '.oc-export', (e) => {
			const t = $(e.currentTarget).data('target');
			this.export_table(t);
		});
	}

	// --------------------------------------------------------------- refresh

	refresh_all() {
		const f = this.filters;
		if (!f.from_date || !f.to_date) return;
		this.load_d1(f);
		this.load_d2(f);
		this.load_d3(f);
		this.load_e1(f);
	}

	// ------------------------------------------------------------------- D1

	load_d1(f) {
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_cash.get_cash_collection_split',
			args: { from_date: f.from_date, to_date: f.to_date, company: f.company },
			callback: (r) => this.render_d1(r.message || {}),
		});
	}

	render_d1(d) {
		const totals = d.totals || {};
		$('#oc-d1-cards').html(
			this.card('Sales-cash', totals.sales_cash, false, null, 'cash') +
			this.card('Debt-cash', totals.debt_cash, false) +
			this.card('Unallocated', totals.unallocated, flt(totals.unallocated) > 0) +
			this.card('Total collected', totals.total, false, null, 'cash')
		);

		this._d1_days = d.days || [];
		this._d1_page = 0;

		const rows = [['Date', 'Sales-Cash', 'Debt-Cash', 'Unallocated', 'Total']];
		this._d1_days.forEach(day => rows.push([day.date, day.sales_cash, day.debt_cash, day.unallocated, day.total]));
		this.export_rows.d1 = rows;

		$('#oc-d1-msg').text(d.message || '');
		this.render_day_page('d1', this._d1_days, ['sales_cash', 'debt_cash', 'unallocated'],
			(date) => this.show_d1_drilldown(date));
	}

	// Shared by D1 and E1 — both are one-row-per-day tables that can run
	// long for a multi-month range. `fields` are the day object's numeric
	// keys (besides date/total) to render as plain columns; `onRowClick`
	// gets the clicked day's date string.
	render_day_page(key, days, fields, onRowClick) {
		const pageKey = '_' + key + '_page';
		this[pageKey] = this[pageKey] || 0;
		const pages = Math.max(1, Math.ceil(days.length / DAY_PAGE_SIZE));
		this[pageKey] = Math.min(Math.max(0, this[pageKey]), pages - 1);
		const start = this[pageKey] * DAY_PAGE_SIZE;
		const slice = days.slice(start, start + DAY_PAGE_SIZE);

		const labels = { sales_cash: 'Sales-Cash', debt_cash: 'Debt-Cash', unallocated: 'Unallocated',
			cash_closing: 'Cash Closing', bank_closing: 'Bank Closing', total_closing: 'Total Closing',
			'in': 'In', out: 'Out' };
		let html = '<tr><th>Date</th>' + fields.map(f => `<th>${labels[f] || f}</th>`).join('') + '<th>Total</th></tr>';
		slice.forEach(day => {
			html += `<tr class="oc-row-link" data-date="${day.date}"><td>${day.date}</td>` +
				fields.map(f => `<td>${this.n(day[f])}</td>`).join('') +
				`<td><b>${this.n(day.total !== undefined ? day.total : day.total_closing)}</b></td></tr>`;
		});
		$(`#oc-${key}-table`).html(html);

		if (days.length > DAY_PAGE_SIZE) {
			$(`#oc-${key}-pager`).html(`
				<button class="oc-btn oc-${key}-prev" ${this[pageKey] === 0 ? 'disabled' : ''}>&larr; Newer</button>
				<span>Days ${start + 1}&ndash;${start + slice.length} of ${days.length}</span>
				<button class="oc-btn oc-${key}-next" ${this[pageKey] >= pages - 1 ? 'disabled' : ''}>Older &rarr;</button>
			`);
			$(`.oc-${key}-prev`).on('click', () => { this[pageKey]--; this.render_day_page(key, days, fields, onRowClick); });
			$(`.oc-${key}-next`).on('click', () => { this[pageKey]++; this.render_day_page(key, days, fields, onRowClick); });
		} else {
			$(`#oc-${key}-pager`).empty();
		}

		$(`#oc-${key}-table`).off('click', 'tr.oc-row-link').on('click', 'tr.oc-row-link', (e) => {
			const date = $(e.currentTarget).data('date');
			if (this.guard_dblclick(key + '-' + date)) return;
			onRowClick(date);
		});
	}

	show_d1_drilldown(date) {
		const f = this.filters;
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_cash.get_cash_collection_drilldown',
			args: { from_date: f.from_date, to_date: f.to_date, date, company: f.company },
			callback: (r) => {
				const rows = r.message || [];
				let html = '<div class="oc-dialog-scroll"><table class="oc-table" style="width:100%">' +
					'<tr><th>Bucket</th><th>Type</th><th>Voucher</th><th>Party</th><th>Against Invoice</th><th>Amount</th></tr>';
				rows.forEach(x => {
					html += `<tr><td>${x.bucket}</td><td>${x.voucher_type}</td>
						<td>${x.voucher}</td><td class="oc-wrap">${x.party || ''}</td>
						<td>${x.against_invoice || ''}</td><td style="text-align:right">${this.n(x.amount)}</td></tr>`;
				});
				html += '</table></div>';
				this.open_dialog(`Cash collection vouchers — ${date}`, html);
			},
		});
	}

	// ------------------------------------------------------------------- D2

	load_d2(f) {
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_cash.get_collections_by_mode',
			args: { from_date: f.from_date, to_date: f.to_date, company: f.company },
			callback: (r) => this.render_d2(r.message || {}),
		});
	}

	render_d2(d) {
		$('#oc-d2-cards').html(this.card('Total collected (POS)', d.total, false, null, 'cash'));
		const rows = [['Cashier', 'POS Profile', 'Mode of Payment', 'Transactions', 'Amount', 'Share']];
		let html = '<tr>' + rows[0].map(h => `<th>${h}</th>`).join('') + '</tr>';
		(d.rows || []).forEach(x => {
			rows.push([x.cashier, x.pos_profile, x.mode_of_payment, x.transactions, x.amount, (x.share * 100).toFixed(1) + '%']);
			html += `<tr class="oc-row-link" data-pos-profile="${x.pos_profile}" data-mode="${x.mode_of_payment || ''}">
				<td>${x.cashier || '(unresolved)'}</td>
				<td>${x.pos_profile}</td>
				<td>${x.mode_of_payment || ''}</td>
				<td style="text-align:right">${x.transactions}</td><td><b>${this.n(x.amount)}</b></td><td style="text-align:right">${(x.share * 100).toFixed(1)}%</td></tr>`;
		});
		$('#oc-d2-table').html(html);
		$('#oc-d2-msg').text(d.message || '');
		this.export_rows.d2 = rows;

		$('#oc-d2-table').off('click', 'tr.oc-row-link').on('click', 'tr.oc-row-link', (e) => {
			const el = $(e.currentTarget);
			const posProfile = el.data('pos-profile'), mode = el.data('mode');
			if (this.guard_dblclick('d2-' + posProfile + '-' + mode)) return;
			this.show_d2_drilldown(posProfile, mode);
		});
	}

	show_d2_drilldown(pos_profile, mode_of_payment) {
		const f = this.filters;
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_cash.get_collections_by_mode_drilldown',
			args: { from_date: f.from_date, to_date: f.to_date, pos_profile, mode_of_payment, company: f.company },
			callback: (r) => {
				const rows = r.message || [];
				let html = '<div class="oc-dialog-scroll"><table class="oc-table" style="width:100%">' +
					'<tr><th>Invoice</th><th>Date</th><th>Customer</th><th>Account</th><th>Amount</th></tr>';
				rows.forEach(x => {
					html += `<tr><td>${x.invoice}</td><td>${x.posting_date}</td>
						<td class="oc-wrap">${x.customer || ''}</td><td>${x.account}</td>
						<td style="text-align:right">${this.n(x.amount)}</td></tr>`;
				});
				html += '</table></div>';
				this.open_dialog(`Transactions — ${pos_profile} / ${mode_of_payment}`, html);
			},
		});
	}

	// ------------------------------------------------------------------- D3
	//
	// FIXED 2026-08-22: one row per account now, not one row per account
	// per day (that was 500+ rows and the main reason the page was so
	// long). The day-by-day rows are still fetched in full and kept in
	// memory (this._d3_rows) — nothing lost, just aggregated for display
	// and expanded again, client-side, when a row is clicked.

	load_d3(f) {
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_cash.get_cashier_reconciliation',
			args: { from_date: f.from_date, to_date: f.to_date, company: f.company },
			callback: (r) => this.render_d3(r.message || {}),
		});
	}

	render_d3(d) {
		this._d3_rows = d.rows || [];
		const byAccount = {};
		this._d3_rows.forEach(x => {
			const acc = byAccount[x.account] || (byAccount[x.account] = {
				account: x.account, role: x.role, cashier: x.cashier,
				latest_date: null, latest_closing: 0, latest_swept_clean: null,
				negative_days: 0, total_in: 0,
			});
			if (!acc.latest_date || x.posting_date > acc.latest_date) {
				acc.latest_date = x.posting_date;
				acc.latest_closing = x.closing;
				acc.latest_swept_clean = x.swept_clean;
			}
			if (x.flag === 'NEGATIVE') acc.negative_days += 1;
			acc.total_in += flt(x.collections_in);
		});
		const accounts = Object.values(byAccount).sort((a, b) => b.total_in - a.total_in);

		const unswept = accounts.filter(a => a.role === 'cashier' && a.latest_swept_clean === false);
		$('#oc-d3-cards').html(this.card_int(
			'Cashier tills not swept clean (as of their latest activity in range)', unswept.length, unswept.length > 0));

		const rows = [['Account (Till)', 'Role', 'Cashier', 'Latest Date', 'Latest Closing',
			'Days Flagged Negative', 'Total Collected In Range', 'Status']];
		let html = '<tr>' + rows[0].map(h => `<th>${h}</th>`).join('') + '</tr>';
		accounts.forEach(a => {
			const status = a.role === 'cashier'
				? (a.latest_swept_clean ? 'swept' : 'not swept')
				: '';
			rows.push([a.account, a.role, a.cashier, a.latest_date, a.latest_closing,
				a.negative_days, a.total_in, status]);
			const rowCls = a.negative_days > 0 ? 'neg' : (a.latest_swept_clean === false ? 'warn' : '');
			const sweptPill = a.role === 'cashier'
				? (a.latest_swept_clean ? '<span class="oc-pill ok">swept</span>' : '<span class="oc-pill bad">not swept</span>')
				: '';
			html += `<tr class="${rowCls} oc-row-link" data-account="${a.account}">
				<td>${a.account}</td>
				<td>${a.role}</td>
				<td>${a.cashier || ''}</td>
				<td style="text-align:right">${a.latest_date || ''}</td>
				<td><b>${this.n(a.latest_closing)}</b></td>
				<td style="text-align:right">${a.negative_days || ''}</td>
				<td>${this.n(a.total_in)}</td>
				<td>${sweptPill}</td></tr>`;
		});
		$('#oc-d3-table').html(html);
		$('#oc-d3-msg').text(d.message || '');
		this.export_rows.d3 = rows;

		$('#oc-d3-table').off('click', 'tr.oc-row-link').on('click', 'tr.oc-row-link', (e) => {
			const account = $(e.currentTarget).data('account');
			if (this.guard_dblclick('d3-' + account)) return;
			this.show_d3_account_modal(account);
		});
	}

	show_d3_account_modal(account) {
		const rows = this._d3_rows.filter(r => r.account === account)
			.sort((a, b) => a.posting_date < b.posting_date ? -1 : 1);
		let html = '<div class="oc-dialog-scroll"><table class="oc-table" style="width:100%">' +
			'<tr><th>Date</th><th>Opening</th><th>Collections In</th><th>Swept Out</th><th>Other Out</th><th>Closing</th><th>Flag</th></tr>';
		rows.forEach(x => {
			html += `<tr class="oc-row-link ${x.flag === 'NEGATIVE' ? 'neg' : ''}" data-date="${x.posting_date}">
				<td>${x.posting_date}</td><td>${this.n(x.opening)}</td><td>${this.n(x.collections_in)}</td>
				<td>${this.n(x.swept_out)}</td><td>${this.n(x.other_out)}</td>
				<td><b>${this.n(x.closing)}</b></td><td>${x.flag || ''}</td></tr>`;
		});
		html += '</table></div><div class="oc-msg" style="margin-top:8px">Click a day to see the GL vouchers behind it.</div>';
		const d = this.open_dialog(`${account} — day by day`, html, 'extra-large');
		d.fields_dict.body.$wrapper.off('click', 'tr.oc-row-link').on('click', 'tr.oc-row-link', (e) => {
			const date = $(e.currentTarget).data('date');
			if (this.guard_dblclick('d3drill-' + account + '-' + date)) return;
			this.show_d3_drilldown(account, date);
		});
	}

	show_d3_drilldown(account, date) {
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_cash.get_till_drilldown',
			args: { account, date, company: this.filters.company },
			callback: (r) => {
				const rows = r.message || [];
				let html = '<div class="oc-dialog-scroll"><table class="oc-table" style="width:100%">' +
					'<tr><th>Kind</th><th>Type</th><th>Voucher</th><th>Party</th><th>Amount</th><th>Remarks</th></tr>';
				rows.forEach(x => {
					html += `<tr><td>${x.kind}</td><td>${x.voucher_type}</td>
						<td>${x.voucher_no}</td><td class="oc-wrap">${x.party || ''}</td>
						<td style="text-align:right">${this.n(x.amount)}</td><td class="oc-wrap">${x.remarks || ''}</td></tr>`;
				});
				html += '</table></div>';
				this.open_dialog(`${account} — ${date}`, html);
			},
		});
	}

	// ------------------------------------------------------------------- E1

	load_e1(f) {
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_cash.get_cash_bank_position',
			args: { from_date: f.from_date, to_date: f.to_date, company: f.company },
			callback: (r) => this.render_e1(r.message || {}),
		});
	}

	render_e1(d) {
		this._e1_by_account = d.by_account || [];
		this._e1_days = d.days || [];
		this._e1_page = 0;
		const last = this._e1_days[this._e1_days.length - 1] || {};
		$('#oc-e1-cards').html(
			this.card('Cash on hand (latest)', last.cash_closing, false) +
			this.card('Bank balance (latest)', last.bank_closing, false) +
			this.card('Total cash + bank (latest)', last.total_closing, false, null, 'cash')
		);

		const rows = [['Date', 'Cash Closing', 'Bank Closing', 'Total Closing', 'In', 'Out']];
		this._e1_days.forEach(day => rows.push(
			[day.date, day.cash_closing, day.bank_closing, day.total_closing, day.in, day.out]));
		this.export_rows.e1 = rows;

		$('#oc-e1-msg').text(d.message || '');
		this.render_day_page('e1', this._e1_days, ['cash_closing', 'bank_closing', 'in', 'out'],
			(date) => this.show_e1_drilldown(date));
	}

	show_e1_drilldown(date) {
		const rows = (this._e1_by_account || []).filter(r => String(r.posting_date) === String(date));
		let html = '<div class="oc-dialog-scroll"><table class="oc-table" style="width:100%">' +
			'<tr><th>Account</th><th>Opening</th><th>Collections In</th><th>Swept Out</th><th>Other Out</th><th>Closing</th><th>Flag</th></tr>';
		rows.forEach(x => {
			html += `<tr><td>${x.account}</td><td style="text-align:right">${this.n(x.opening)}</td>
				<td style="text-align:right">${this.n(x.collections_in)}</td><td style="text-align:right">${this.n(x.swept_out)}</td><td style="text-align:right">${this.n(x.other_out)}</td>
				<td style="text-align:right"><b>${this.n(x.closing)}</b></td><td style="text-align:right">${x.flag || ''}</td></tr>`;
		});
		html += '</table></div>';
		this.open_dialog(`Cash & Bank accounts — ${date}`, html);
	}

	// ---------------------------------------------------------------- helpers

	card(label, value, warn, sub, tone) {
		return `<div class="oc-card${warn ? ' warn' : ''}${tone ? ' ' + tone : ''}">
			<div class="label">${label}</div>
			<div class="value">${this.n(value)}</div>
			${sub ? `<div class="oc-msg" style="margin-top:2px">${sub}</div>` : ''}
		</div>`;
	}

	// Plain integer count — no currency-style decimals. Used for "how many
	// tills/rows" cards, which read badly as "8.00".
	card_int(label, value, warn, sub) {
		return `<div class="oc-card${warn ? ' warn' : ''}">
			<div class="label">${label}</div>
			<div class="value">${cint(value)}</div>
			${sub ? `<div class="oc-msg" style="margin-top:2px">${sub}</div>` : ''}
		</div>`;
	}

	n(v) {
		v = flt(v);
		return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
	}

	// A fast double-click on a row fires two separate 'click' events —
	// without this, that opened two copies of the same dialog.
	guard_dblclick(key) {
		this._click_guard = this._click_guard || {};
		const now = new Date().getTime();
		const last = this._click_guard[key] || 0;
		this._click_guard[key] = now;
		return (now - last) < 500;
	}

	// Every drilldown dialog goes through this one place, so the scroll
	// wrapper and sizing are consistent instead of hand-repeated per
	// dialog (that inconsistency is exactly how the Remarks-overflow bug
	// happened — one dialog had the scroll wrapper, most didn't).
	open_dialog(title, bodyHtml, size) {
		const d = new frappe.ui.Dialog({
			title, size: size || 'large',
			fields: [{ fieldtype: 'HTML', fieldname: 'body' }],
		});
		d.fields_dict.body.$wrapper.html(bodyHtml);
		d.show();
		return d;
	}

	get export_rows() {
		this._export_rows = this._export_rows || {};
		return this._export_rows;
	}

	// --------------------------------------------------------------- export

	to_csv(rows) {
		return rows.map(row => row.map(cell => {
			const s = (cell === null || cell === undefined) ? '' : String(cell);
			return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
		}).join(',')).join('\r\n');
	}

	download(rows, name) {
		const blob = new Blob(['﻿' + this.to_csv(rows)], { type: 'text/csv;charset=utf-8;' });
		const a = document.createElement('a');
		a.href = URL.createObjectURL(blob);
		a.download = name;
		document.body.appendChild(a);
		a.click();
		document.body.removeChild(a);
	}

	export_table(key) {
		const rows = this.export_rows[key];
		if (!rows) return;
		const f = this.filters;
		this.download(rows, `${key}_${f.from_date}_to_${f.to_date}.csv`.replace(/[^\w.\-]+/g, '_'));
	}
}

function flt(v) {
	const n = parseFloat(v);
	return isNaN(n) ? 0 : n;
}

function cint(v) {
	const n = parseInt(v, 10);
	return isNaN(n) ? 0 : n;
}