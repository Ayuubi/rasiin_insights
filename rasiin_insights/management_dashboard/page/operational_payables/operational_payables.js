/*
 * Operational Reports — Page 3: Expenses & Payables.
 *
 * Path: rasiin_insights/management_dashboard/page/operational_payables/
 *         operational_payables.js
 *
 * Daily, transaction-level, for finance/accountants closing the day — the
 * companion to the CEO Management Dashboard, not a replacement for it.
 * Reads live from operational_api_payables.py, which reads live from
 * GL Entry / Purchase Invoice / Payment Entry — never the monthly snapshot.
 *
 * Built 2026-08-22, mirroring operational_receivables.js's/
 * operational_cash.js's already-settled patterns from the start rather
 * than re-deriving them (see next-steps-payables-and-stock.md):
 *   - Company default = api.get_filters()'s default_company (Shaafi
 *     Hospital), not "all companies" — Shaafi Diagnostic Center carries a
 *     static $14,451.50 payable balance with zero movement in Jan/Jul
 *     2026, the exact AP mirror of the AR-side SDC balance found
 *     2026-08-22.
 *   - Filters are plain HTML controls in their own flush strip, NOT
 *     page.add_field() — confirmed unreliable on these operational pages
 *     across three rounds on Page 1, never re-attempted here.
 *   - Suite nav is a real <a href="/app/..."> with an explicit click
 *     handler that forces a full page load, so the destination page's JS
 *     bundle is never raced.
 *   - Every drilldown goes through the same open_dialog()/guard_dblclick()
 *     pair, with min-width (not just max-width) on free-text columns and
 *     no width:100% lock on a dialog's table.
 *   - Long day-range tables paginate client-side, 31 rows/page, full data
 *     kept in memory (C1, C3 here).
 */

frappe.pages['operational-payables'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper, title: 'Expenses & Payables', single_column: true
	});
	new OperationalPayables(page).init();
};

// Same list as every other page in the set, so the tab bar reads
// identically everywhere — keep this in sync with operational_receivables.js
// / operational_cash.js / management_dashboard.js's own ROUTES arrays.
const ROUTES = [
	{ label: 'CEO Dashboard', route: 'management-dashboard' },
	{ label: 'Receivables & Revenue', route: 'operational-receivables' },
	{ label: 'Cash & Collections', route: 'operational-cash' },
	{ label: 'Expenses & Payables', route: 'operational-payables', current: true },
];

const PAGE_SIZE = 31;

class OperationalPayables {
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
		this.$c.find('#op-suite-nav').html(html);

		this.$c.find('#op-suite-nav a').on('click', (e) => {
			if ($(e.currentTarget).hasClass('active')) { e.preventDefault(); return; }
			e.preventDefault();
			e.stopPropagation();
			window.location.href = e.currentTarget.href;
		});
	}

	// --------------------------------------------------------------- filters

	build_filters() {
		const companies = this.filter_meta.companies || [];
		const today = frappe.datetime.get_today();
		const week_ago = frappe.datetime.add_days(today, -6);
		const default_company = this.filter_meta.default_company || 'All companies';

		this.$c.find('#op-filterbar').html(`
			<div class="op-filter-field">
				<label>From</label>
				<input type="date" class="op-from" value="${week_ago}">
			</div>
			<div class="op-filter-field">
				<label>To</label>
				<input type="date" class="op-to" value="${today}">
			</div>
			<div class="op-filter-field">
				<label>Company</label>
				<select class="op-company">
					${['All companies'].concat(companies).map(c =>
						`<option value="${c}" ${c === default_company ? 'selected' : ''}>${c}</option>`
					).join('')}
				</select>
			</div>
			<button class="op-btn op-refresh">Refresh</button>
		`);
		this.$c.find('.op-from, .op-to, .op-company').on('change', () => this.refresh_all());
		this.$c.find('.op-refresh').on('click', () => this.refresh_all());
	}

	get filters() {
		const company = this.$c.find('.op-company').val();
		return {
			from_date: this.$c.find('.op-from').val(),
			to_date: this.$c.find('.op-to').val(),
			company: (company && company !== 'All companies') ? company : null,
		};
	}

	// ---------------------------------------------------------------- layout

	build_layout() {
		this.$c.html(`
			<div class="rd op-page">
				<style>
					/* ---- design tokens, copied from operational_receivables.js's .or-page ---- */
					.op-page { --ink:#0f172a; --dim:#64748b; --line:#e2e8f0; --navy:#1e3a5f;
						--good:#15803d; --bad:#b91c1c; --amber:#a16207;
						padding:15px 15px 50px; color:var(--ink); }

					.op-topbar { margin:16px 0 18px; }

					.op-filterbar { display:flex; align-items:center; gap:20px; flex-wrap:wrap;
						padding:4px 2px 16px; margin:0; border-bottom:1px solid var(--line); }
					.op-filter-field { display:flex; align-items:center; gap:8px; }
					.op-filter-field label { font-size:12px; color:var(--dim); font-weight:600; }
					.op-filter-field input[type=date], .op-filter-field select {
						padding:5px 9px; border-radius:6px; border:1px solid var(--line);
						font-size:12px; background:#fff; color:var(--ink); line-height:1.4; }
					.op-filter-field input[type=date]:focus, .op-filter-field select:focus {
						outline:none; border-color:var(--navy); }
					.op-filterbar .op-refresh { margin-left:auto; }
					.rd-modes { display:inline-flex; background:#f1f5f9; border-radius:10px; padding:3px; }
					.rd-mode { border:0; background:transparent; padding:7px 18px; border-radius:8px;
						font-size:13px; cursor:pointer; color:var(--dim); display:inline-block;
						text-decoration:none; line-height:1.4; }
					.rd-mode.active { background:#fff; color:var(--navy); font-weight:600;
						box-shadow:0 1px 3px rgba(15,23,42,.12); cursor:default; }
					.rd-mode:not(.active):hover { color:var(--navy); text-decoration:none; }

					/* ---- panels ---- */
					.op-section { background:#fff; border:1px solid var(--line); border-radius:12px;
						padding:16px 18px 20px; margin-bottom:18px; }
					.op-section h4 { display:flex; justify-content:space-between; align-items:center;
						flex-wrap:wrap; gap:10px; font-size:14px; font-weight:700; letter-spacing:-.01em;
						margin:0 0 14px; }
					.op-btn { border:1px solid var(--line); background:#fff; border-radius:8px;
						padding:5px 12px; font-size:12px; cursor:pointer; color:var(--dim); }
					.op-btn:hover { border-color:var(--navy); color:var(--navy); }
					.op-btn:disabled { opacity:.4; cursor:default; }
					.op-btn:disabled:hover { border-color:var(--line); color:var(--dim); }

					/* ---- cards ---- */
					.op-cards { display:grid; gap:11px; grid-template-columns:repeat(auto-fit,minmax(185px,1fr));
						margin-bottom:14px; }
					.op-card { border:1px solid var(--line); border-left:3px solid var(--line); border-radius:10px;
						padding:13px 15px; background:#fff; }
					.op-card.clickable { cursor:pointer; }
					.op-card.clickable:hover { box-shadow:0 2px 8px rgba(15,23,42,.08); border-color:var(--navy); }
					.op-card .label { font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:.05em; }
					.op-card .value { font-size:20px; font-weight:700; margin-top:4px; font-variant-numeric:tabular-nums; }
					.op-card.warn { border-left-color:var(--amber); }
					.op-card.good { border-left-color:var(--good); }
					.op-card.rev { border-left-color:var(--navy); }

					/* ---- tables ---- */
					.op-table-wrap { overflow-x:auto; }
					.op-table { width:100%; border-collapse:collapse; font-size:13px; }
					.op-table th { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim);
						text-align:right; border-bottom:2px solid var(--line); padding:8px 10px; white-space:nowrap; }
					.op-table th:first-child, .op-table td:first-child { text-align:left; }
					.op-table td { text-align:right; padding:7px 10px; font-variant-numeric:tabular-nums;
						border-bottom:1px solid #f1f5f9; white-space:nowrap; }
					.op-table tbody tr:hover td { background:#f8fafc; }
					.op-table tr.neg td { color:var(--bad); }
					.op-row-link { cursor:pointer; }
					.op-msg { font-size:12px; color:var(--dim); margin-top:8px; line-height:1.5; }

					/* min-width (not just max-width) on free-text columns — the
					   or-wrap fix from Page 1, applied here from the start. */
					.op-wrap { white-space:normal !important; min-width:200px; max-width:380px;
						word-break:break-word; text-align:left !important; }
					.op-dialog-scroll { max-height:60vh; overflow:auto; }
					.op-dialog-scroll table.op-table { width:auto; min-width:100%; }

					/* ---- pagination (C1/C3) ---- */
					.op-pager { display:flex; align-items:center; gap:10px; margin-top:10px;
						font-size:12px; color:var(--dim); }

					@media (max-width:640px) {
						.op-section { padding:13px 12px 16px; }
						.op-table th, .op-table td { padding:7px 8px; }
					}
				</style>

				<div class="op-filterbar" id="op-filterbar"></div>
				<div class="op-topbar">
					<div class="rd-modes" id="op-suite-nav"></div>
				</div>

				<div class="op-section" id="op-c3">
					<h4>Payables Rollforward <button class="op-btn op-export" data-target="c3">Export CSV</button></h4>
					<div class="op-cards" id="op-c3-cards"></div>
					<div class="op-table-wrap"><table class="op-table" id="op-c3-table"></table></div>
					<div class="op-pager" id="op-c3-pager"></div>
					<div class="op-msg" id="op-c3-msg"></div>
				</div>

				<div class="op-section" id="op-aging">
					<h4>Payables Aging by Supplier (as of "To" date) <button class="op-btn op-export" data-target="aging">Export CSV</button></h4>
					<div class="op-cards" id="op-aging-cards"></div>
					<div class="op-table-wrap"><table class="op-table" id="op-aging-table"></table></div>
					<div class="op-pager" id="op-aging-pager"></div>
					<div class="op-msg" id="op-aging-msg"></div>
				</div>

				<div class="op-section" id="op-top">
					<h4>Top Payable Movers <button class="op-btn op-export" data-target="top">Export CSV</button></h4>
					<div class="op-table-wrap"><table class="op-table" id="op-top-table"></table></div>
				</div>

				<div class="op-section" id="op-c1">
					<h4>Daily Expenses <button class="op-btn op-export" data-target="c1">Export CSV</button></h4>
					<div class="op-cards" id="op-c1-cards"></div>
					<div class="op-table-wrap"><table class="op-table" id="op-c1-table"></table></div>
					<div class="op-pager" id="op-c1-pager"></div>
					<div class="op-msg" id="op-c1-msg"></div>
				</div>

				<div class="op-section" id="op-c2">
					<h4>Goods/Assets Received, Not Yet Invoiced (as of "To" date) <button class="op-btn op-export" data-target="c2">Export CSV</button></h4>
					<div class="op-cards" id="op-c2-cards"></div>
					<div class="op-table-wrap"><table class="op-table" id="op-c2-table"></table></div>
					<div class="op-pager" id="op-c2-pager"></div>
					<div class="op-msg" id="op-c2-msg"></div>
				</div>

				<div class="op-section" id="op-c4">
					<h4>Supplier Payments <button class="op-btn op-export" data-target="c4">Export CSV</button></h4>
					<div class="op-cards" id="op-c4-cards"></div>
					<div class="op-table-wrap"><table class="op-table" id="op-c4-table"></table></div>
					<div class="op-pager" id="op-c4-pager"></div>
					<div class="op-msg" id="op-c4-msg"></div>
				</div>
			</div>
		`);
	}

	bind_events() {
		this.$c.on('click', '.op-export', (e) => {
			const t = $(e.currentTarget).data('target');
			this.export_table(t);
		});
	}

	// --------------------------------------------------------------- refresh

	refresh_all() {
		const f = this.filters;
		if (!f.from_date || !f.to_date) return;
		// 2026-08-22 — every panel's fetch is tagged with the generation of
		// this refresh_all() call. Nothing here guarantees requests land in
		// the order they were fired (a slower-but-earlier request can
		// resolve after a faster-but-later one), so a stale response is
		// dropped in guarded_call() rather than being allowed to overwrite
		// a newer, correctly-filtered render.
		this._req_gen = (this._req_gen || 0) + 1;
		this.load_c3(f);
		this.load_aging(f);
		this.load_top(f);
		this.load_c1(f);
		this.load_c2(f);
		this.load_c4(f);
	}

	// A frappe.call() wrapper that only renders if no newer refresh_all()
	// has started since this call was fired — see the note in refresh_all().
	guarded_call(method, args, render) {
		const gen = this._req_gen;
		frappe.call({
			method, args,
			callback: (r) => {
				if (gen !== this._req_gen) return;
				render(r.message);
			},
		});
	}

	// ------------------------------------------------------------------- C3

	load_c3(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_payables.get_ap_rollforward',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company },
			(d) => this.render_c3(d || {})
		);
	}

	render_c3(d) {
		$('#op-c3-cards').html(`
			${this.card('Opening (start of range)', d.opening_total, false, 'Real GL balance the day before the range starts', 'rev')}
			${this.card('Closing (end of range)', d.closing_total, false, 'Real GL balance as of the "To" date — what we owe suppliers right now', 'rev')}
			${this.card('Net movement', (d.closing_total || 0) - (d.opening_total || 0), false, 'Closing minus opening — how much the payable grew or shrank over the range')}
		`);

		this._c3_days = d.days || [];
		this._c3_page = 0;

		const rows = [['Date', 'Opening', 'Charged', 'Paid', 'Adjustments', 'Closing']];
		this._c3_days.forEach(day => rows.push(
			[day.date, day.opening, day.charged, day.paid, day.adjustments, day.closing]));
		this.export_rows.c3 = rows;

		$('#op-c3-msg').text(d.message || '');
		this.render_c3_page();
	}

	render_c3_page() {
		const days = this._c3_days || [];
		const pages = Math.max(1, Math.ceil(days.length / PAGE_SIZE));
		this._c3_page = Math.min(Math.max(0, this._c3_page), pages - 1);
		const start = this._c3_page * PAGE_SIZE;
		const slice = days.slice(start, start + PAGE_SIZE);

		let html = '<tr><th>Date</th><th>Opening</th><th>Charged</th><th>Paid</th><th>Adjustments</th><th>Closing</th></tr>';
		slice.forEach(day => {
			html += `<tr class="op-row-link" data-date="${day.date}">
				<td>${day.date}</td><td>${this.n(day.opening)}</td><td>${this.n(day.charged)}</td>
				<td>${this.n(day.paid)}</td><td>${this.n(day.adjustments)}</td>
				<td><b>${this.n(day.closing)}</b></td></tr>`;
		});
		$('#op-c3-table').html(html);

		if (days.length > PAGE_SIZE) {
			$('#op-c3-pager').html(`
				<button class="op-btn" id="op-c3-prev" ${this._c3_page === 0 ? 'disabled' : ''}>&larr; Newer</button>
				<span>Days ${start + 1}&ndash;${start + slice.length} of ${days.length}</span>
				<button class="op-btn" id="op-c3-next" ${this._c3_page >= pages - 1 ? 'disabled' : ''}>Older &rarr;</button>
			`);
			$('#op-c3-prev').on('click', () => { this._c3_page--; this.render_c3_page(); });
			$('#op-c3-next').on('click', () => { this._c3_page++; this.render_c3_page(); });
		} else {
			$('#op-c3-pager').empty();
		}

		$('#op-c3-table').off('click', 'tr.op-row-link').on('click', 'tr.op-row-link', (e) => {
			const date = $(e.currentTarget).data('date');
			if (this.guard_dblclick('c3-' + date)) return;
			this.show_c3_drilldown(date);
		});
	}

	show_c3_drilldown(date) {
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_payables.get_ap_rollforward_drilldown',
			args: { date, company: this.filters.company },
			callback: (r) => {
				const rows = r.message || [];
				let html = '<div class="op-dialog-scroll"><table class="op-table" style="width:100%">' +
					'<tr><th>Type</th><th>Voucher</th><th>Party</th><th>Account</th><th>Amount</th><th>Remarks</th></tr>';
				rows.forEach(x => {
					html += `<tr><td>${x.voucher_type}</td><td>${x.voucher_no}</td>
						<td>${x.party || ''}</td><td>${x.account}</td>
						<td style="text-align:right">${this.n(x.amount)}</td><td class="op-wrap">${x.remarks || ''}</td></tr>`;
				});
				html += '</table></div>';
				this.open_dialog(`Payable vouchers — ${date}`, html);
			},
		});
	}

	// ---------------------------------------------------------------- aging

	load_aging(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_payables.get_ap_aging',
			{ as_of_date: f.to_date, company: f.company },
			(d) => this.render_aging(d || {})
		);
	}

	render_aging(d) {
		const summary = d.summary || [];
		const AGING_SUB = {
			'Total outstanding': 'Sum of every open Purchase Invoice, rebuilt from GL Entry as of the "To" date',
			'Weighted average age (days)': 'Outstanding-weighted average of days since each invoice was posted',
			'120+ days': 'Outstanding balance on invoices posted more than 120 days ago — the oldest, highest-risk slice',
			'Invoices in this book': 'Count of open Purchase Invoices making up the total above',
		};
		$('#op-aging-cards').html(summary.map(s => `
			<div class="op-card${s.indicator === 'Red' ? ' warn' : ''}">
				<div class="label">${s.label}</div>
				<div class="value">${s.datatype === 'Currency' ? this.n(s.value) : s.value}</div>
				${AGING_SUB[s.label] ? `<div class="op-msg" style="margin-top:2px">${AGING_SUB[s.label]}</div>` : ''}
			</div>
		`).join(''));

		// FIXED 2026-08-22 — this used to dump every outstanding invoice
		// straight onto the page unconditionally (234 rows for a single
		// month in real data), exactly the A2/D3 bug already fixed on
		// Pages 1/2. Full list is still fetched once and kept in memory
		// (this._aging_rows), so paging and CSV export are both instant.
		this._aging_rows = d.rows || [];
		this._aging_page = 0;

		const rows = [['Invoice', 'Date', 'Age', 'Bucket', 'Supplier', 'Company', 'Outstanding']];
		this._aging_rows.forEach(x => rows.push(
			[x.invoice, x.posting_date, x.age, x.bucket, x.supplier_name, x.company, x.outstanding]));
		this.export_rows.aging = rows;

		$('#op-aging-msg').text(d.message || '');
		this.render_aging_page();
	}

	render_aging_page() {
		const list = this._aging_rows || [];
		const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
		this._aging_page = Math.min(Math.max(0, this._aging_page), pages - 1);
		const start = this._aging_page * PAGE_SIZE;
		const slice = list.slice(start, start + PAGE_SIZE);

		let html = '<tr><th>Invoice</th><th>Date</th><th>Age</th><th>Bucket</th><th>Supplier</th><th>Company</th><th>Outstanding</th></tr>';
		slice.forEach(x => {
			const cls = x.bucket === '120+' ? 'neg' : '';
			html += `<tr class="${cls}"><td>${x.invoice}</td><td>${x.posting_date}</td><td>${x.age}</td>
				<td>${x.bucket}</td><td class="op-wrap">${x.supplier_name || ''}</td><td>${x.company}</td>
				<td><b>${this.n(x.outstanding)}</b></td></tr>`;
		});
		$('#op-aging-table').html(html);

		if (list.length > PAGE_SIZE) {
			$('#op-aging-pager').html(`
				<button class="op-btn" id="op-aging-prev" ${this._aging_page === 0 ? 'disabled' : ''}>&larr; Prev</button>
				<span>Rows ${start + 1}&ndash;${start + slice.length} of ${list.length}</span>
				<button class="op-btn" id="op-aging-next" ${this._aging_page >= pages - 1 ? 'disabled' : ''}>Next &rarr;</button>
			`);
			$('#op-aging-prev').on('click', () => { this._aging_page--; this.render_aging_page(); });
			$('#op-aging-next').on('click', () => { this._aging_page++; this.render_aging_page(); });
		} else {
			$('#op-aging-pager').empty();
		}
	}

	// -------------------------------------------------------------- top movers

	load_top(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_payables.get_ap_top_movers',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company, limit: 15 },
			(list) => this.render_top(list || [])
		);
	}

	render_top(list) {
		const rows = [['Supplier', 'Supplier ID', 'Charged', 'Paid', 'Net Movement', 'Transactions']];
		let html = '<tr><th>Supplier</th><th>Charged</th><th>Paid</th><th>Net Movement</th><th>Transactions</th></tr>';
		list.forEach(x => {
			const name = x.supplier_name || x.supplier;
			rows.push([name, x.supplier, x.charged, x.paid, x.net_movement, x.transactions]);
			const cls = x.net_movement > 0 ? 'neg' : '';
			html += `<tr class="${cls}"><td class="op-wrap">${name}</td><td>${this.n(x.charged)}</td>
				<td>${this.n(x.paid)}</td><td><b>${this.n(x.net_movement)}</b></td><td>${x.transactions}</td></tr>`;
		});
		$('#op-top-table').html(html);
		this.export_rows.top = rows;
	}

	// ------------------------------------------------------------------- C1

	load_c1(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_payables.get_daily_expenses',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company },
			(d) => this.render_c1(d || {})
		);
	}

	render_c1(d) {
		$('#op-c1-cards').html(`
			${this.card('Total expenses (this range)', d.total, false, 'Every GL posting to an Expense-type account over the range — Refunds and other non-Expense accounts are not included here', 'rev')}
			${this.card('Of which, petty cash', d.total_petty_cash, false, 'Share of the total posted to the petty cash accounts (50301/50302)')}
		`);

		this._c1_days = d.days || [];
		this._c1_page = 0;

		const rows = [['Date', 'Petty Cash', 'Other Expenses', 'Total']];
		this._c1_days.forEach(day => rows.push([day.date, day.petty_cash, day.other, day.total]));
		this.export_rows.c1 = rows;

		$('#op-c1-msg').text(d.message || '');
		this.render_c1_page();
	}

	render_c1_page() {
		const days = this._c1_days || [];
		const pages = Math.max(1, Math.ceil(days.length / PAGE_SIZE));
		this._c1_page = Math.min(Math.max(0, this._c1_page), pages - 1);
		const start = this._c1_page * PAGE_SIZE;
		const slice = days.slice(start, start + PAGE_SIZE);

		let html = '<tr><th>Date</th><th>Petty Cash</th><th>Other Expenses</th><th>Total</th></tr>';
		slice.forEach(day => {
			html += `<tr class="op-row-link" data-date="${day.date}">
				<td>${day.date}</td><td>${this.n(day.petty_cash)}</td><td>${this.n(day.other)}</td>
				<td><b>${this.n(day.total)}</b></td></tr>`;
		});
		$('#op-c1-table').html(html);

		if (days.length > PAGE_SIZE) {
			$('#op-c1-pager').html(`
				<button class="op-btn" id="op-c1-prev" ${this._c1_page === 0 ? 'disabled' : ''}>&larr; Newer</button>
				<span>Days ${start + 1}&ndash;${start + slice.length} of ${days.length}</span>
				<button class="op-btn" id="op-c1-next" ${this._c1_page >= pages - 1 ? 'disabled' : ''}>Older &rarr;</button>
			`);
			$('#op-c1-prev').on('click', () => { this._c1_page--; this.render_c1_page(); });
			$('#op-c1-next').on('click', () => { this._c1_page++; this.render_c1_page(); });
		} else {
			$('#op-c1-pager').empty();
		}

		$('#op-c1-table').off('click', 'tr.op-row-link').on('click', 'tr.op-row-link', (e) => {
			const date = $(e.currentTarget).data('date');
			if (this.guard_dblclick('c1-' + date)) return;
			this.show_c1_drilldown(date);
		});
	}

	show_c1_drilldown(date) {
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_payables.get_expense_day_drilldown',
			args: { date, company: this.filters.company },
			callback: (r) => {
				const rows = r.message || [];
				let html = '<div class="op-dialog-scroll"><table class="op-table" style="width:100%">' +
					'<tr><th>Type</th><th>Voucher</th><th>Party</th><th>Account</th><th>Amount</th><th>Remarks</th></tr>';
				rows.forEach(x => {
					html += `<tr><td>${x.voucher_type}</td><td>${x.voucher_no}</td>
						<td>${x.party || ''}</td><td>${x.account}</td>
						<td style="text-align:right">${this.n(x.amount)}</td><td class="op-wrap">${x.remarks || ''}</td></tr>`;
				});
				html += '</table></div>';
				this.open_dialog(`Expense vouchers — ${date}`, html);
			},
		});
	}

	// ------------------------------------------------------------------- C2

	load_c2(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_payables.get_grn_not_invoiced',
			{ as_of_date: f.to_date, company: f.company },
			(d) => this.render_c2(d || {})
		);
	}

	render_c2(d) {
		$('#op-c2-cards').html(
			this.card('Received, not yet invoiced (as of "To" date)', d.balance, false,
				'Live balance of the GRN accrual accounts — goods/assets already received but the supplier bill has not landed yet', 'rev')
		);

		// Paginated for the same reason as aging/C4 (see 2026-08-22 notes
		// there) — the server caps this at 200 postings, which is still
		// more than a page's worth on a busy month; full list stays in
		// memory for export/paging.
		this._c2_rows = d.rows || [];
		this._c2_page = 0;

		const rows = [['Date', 'Type', 'Voucher', 'Account', 'Debit', 'Credit', 'Remarks']];
		this._c2_rows.forEach(x => rows.push(
			[x.posting_date, x.voucher_type, x.voucher_no, x.account, x.debit, x.credit, x.remarks]));
		this.export_rows.c2 = rows;

		$('#op-c2-msg').text(d.message || '');
		this.render_c2_page();
	}

	render_c2_page() {
		const list = this._c2_rows || [];
		const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
		this._c2_page = Math.min(Math.max(0, this._c2_page), pages - 1);
		const start = this._c2_page * PAGE_SIZE;
		const slice = list.slice(start, start + PAGE_SIZE);

		let html = '<tr><th>Date</th><th>Type</th><th>Voucher</th><th>Account</th><th>Debit</th><th>Credit</th><th>Remarks</th></tr>';
		slice.forEach(x => {
			html += `<tr><td>${x.posting_date}</td><td>${x.voucher_type}</td><td>${x.voucher_no}</td>
				<td class="op-wrap">${x.account}</td><td>${this.n(x.debit)}</td><td>${this.n(x.credit)}</td>
				<td class="op-wrap">${x.remarks || ''}</td></tr>`;
		});
		$('#op-c2-table').html(html);

		if (list.length > PAGE_SIZE) {
			$('#op-c2-pager').html(`
				<button class="op-btn" id="op-c2-prev" ${this._c2_page === 0 ? 'disabled' : ''}>&larr; Prev</button>
				<span>Rows ${start + 1}&ndash;${start + slice.length} of ${list.length}</span>
				<button class="op-btn" id="op-c2-next" ${this._c2_page >= pages - 1 ? 'disabled' : ''}>Next &rarr;</button>
			`);
			$('#op-c2-prev').on('click', () => { this._c2_page--; this.render_c2_page(); });
			$('#op-c2-next').on('click', () => { this._c2_page++; this.render_c2_page(); });
		} else {
			$('#op-c2-pager').empty();
		}
	}

	// ------------------------------------------------------------------- C4

	load_c4(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_payables.get_supplier_payments',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company },
			(d) => this.render_c4(d || {})
		);
	}

	render_c4(d) {
		$('#op-c4-cards').html(this.card('Total paid to suppliers (this range)', d.total, false,
			'Sum of every Payment Entry posted against a supplier over the range', 'rev'));

		// Paginated for the same reason as aging (see 2026-08-22 note there)
		// — a wide date range can return far more than a page's worth of
		// Payment Entries; full list stays in memory for export/paging.
		this._c4_rows = d.rows || [];
		this._c4_page = 0;

		const rows = [['Date', 'Payment Entry', 'Supplier', 'Amount', 'Mode of Payment', 'Reference', 'Company']];
		this._c4_rows.forEach(x => rows.push(
			[x.posting_date, x.payment_entry, x.supplier_name, x.paid_amount, x.mode_of_payment, x.reference_no, x.company]));
		this.export_rows.c4 = rows;

		$('#op-c4-msg').text(d.message || '');
		this.render_c4_page();
	}

	render_c4_page() {
		const list = this._c4_rows || [];
		const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
		this._c4_page = Math.min(Math.max(0, this._c4_page), pages - 1);
		const start = this._c4_page * PAGE_SIZE;
		const slice = list.slice(start, start + PAGE_SIZE);

		let html = '<tr><th>Date</th><th>Payment Entry</th><th>Supplier</th><th>Amount</th><th>Mode of Payment</th><th>Reference</th><th>Company</th></tr>';
		slice.forEach(x => {
			html += `<tr><td>${x.posting_date}</td><td>${x.payment_entry}</td><td class="op-wrap">${x.supplier_name}</td>
				<td><b>${this.n(x.paid_amount)}</b></td><td>${x.mode_of_payment || ''}</td>
				<td class="op-wrap">${x.reference_no || ''}</td><td>${x.company}</td></tr>`;
		});
		$('#op-c4-table').html(html);

		if (list.length > PAGE_SIZE) {
			$('#op-c4-pager').html(`
				<button class="op-btn" id="op-c4-prev" ${this._c4_page === 0 ? 'disabled' : ''}>&larr; Prev</button>
				<span>Rows ${start + 1}&ndash;${start + slice.length} of ${list.length}</span>
				<button class="op-btn" id="op-c4-next" ${this._c4_page >= pages - 1 ? 'disabled' : ''}>Next &rarr;</button>
			`);
			$('#op-c4-prev').on('click', () => { this._c4_page--; this.render_c4_page(); });
			$('#op-c4-next').on('click', () => { this._c4_page++; this.render_c4_page(); });
		} else {
			$('#op-c4-pager').empty();
		}
	}

	// ---------------------------------------------------------------- helpers

	card(label, value, warn, sub, tone) {
		return `<div class="op-card${warn ? ' warn' : ''}${tone ? ' ' + tone : ''}">
			<div class="label">${label}</div>
			<div class="value">${this.n(value)}</div>
			${sub ? `<div class="op-msg" style="margin-top:2px">${sub}</div>` : ''}
		</div>`;
	}

	n(v) {
		v = flt(v);
		return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
	}

	guard_dblclick(key) {
		this._click_guard = this._click_guard || {};
		const now = new Date().getTime();
		const last = this._click_guard[key] || 0;
		this._click_guard[key] = now;
		return (now - last) < 500;
	}

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