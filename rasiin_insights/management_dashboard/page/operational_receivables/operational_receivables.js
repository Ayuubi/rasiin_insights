/*
 * Operational Reports — Page 1: Receivables & Revenue.
 *
 * Path: rasiin_insights/management_dashboard/page/operational_receivables/
 *         operational_receivables.js
 *
 * Daily, transaction-level, for finance/accountants closing the day — the
 * companion to the CEO Management Dashboard, not a replacement for it.
 * Reads live from operational_api_receivable.py, which reads live from
 * GL Entry / Sales Invoice — never the monthly snapshot.
 *
 * COMPANY DEFAULT — fixed 2026-08-22
 *   This page used to default the Company filter to blank ("all
 *   companies"). Shaafi Diagnostic Center, a sister company, carries a
 *   static $5,645.92 receivable balance from before any of these exports
 *   (zero activity since — it just sits there), so "all companies" quietly
 *   inflated every receivable figure by that fixed amount versus the CEO
 *   dashboard, which has always defaulted to a single company. This page
 *   now calls the same get_filters() the CEO dashboard uses and defaults
 *   Company to the exact same value. "All companies" is still selectable.
 *
 * FILTERS — rebuilt again 2026-08-22
 *   These used to be rendered by hand (frappe.ui.form.make_control into a
 *   boxed row inside the page body) specifically so they wouldn't end up
 *   in the page's title-bar toolbar — the worry, from very early on, was
 *   that toolbar fields go invisible on a narrow screen. That never
 *   actually happened here, and the boxed custom row it produced instead
 *   looked and behaved like a different product from the CEO dashboard's
 *   own filter row, which really does just use page.add_field(). So this
 *   page now uses page.add_field() too, the exact same call the CEO
 *   dashboard makes, which is the only way for this to actually look and
 *   behave like the CEO dashboard rather than resembling it.
 *
 * NAV — rebuilt again 2026-08-22
 *   The page-switcher tab bar is a real <a href="/app/..."> now, not a
 *   <button> + frappe.set_route() from a click handler. The buttons
 *   worked for the URL, but left the destination blank until a manual
 *   refresh — Frappe hadn't necessarily loaded that page's own JS bundle
 *   yet, and a handmade click handler has no way to wait for that. A
 *   real link goes through Frappe's own in-app link routing, the same
 *   path every other link in the desk uses, which handles that loading
 *   correctly.
 *
 * DIALOGS — fixed 2026-08-22
 *   Two separate problems: a fast double-click on a row could fire two
 *   click events and open two copies of the same dialog (open_dialog_once
 *   below guards against that); and a long Remarks value with nowrap
 *   styling could push a dialog's table wider than the dialog itself,
 *   spilling text out past its right edge. Every dialog table is now
 *   wrapped in its own scrollable box, and free-text columns wrap
 *   normally instead of forcing the table wider.
 *
 * RECONCILIATION BRIDGES — added 2026-08-22
 *   A2 (Receivables by Patient Type) and B1 (Revenue by Item Group) each
 *   show a live bridge to the CEO dashboard's own figures instead of a
 *   plain footnote — see operational_api_receivable.py's docstrings on
 *   get_ar_by_patient_type and get_revenue_by_item_group for the exact
 *   mechanism and the reconciliation math behind each one.
 *
 * EXPORT
 *   Same pattern as the CEO dashboard: every table has its own "Export
 *   CSV" button, built client-side from whatever was last fetched
 *   (to_csv/download at the bottom of this file) — no server export
 *   endpoint, nothing to keep in sync with the query.
 */

frappe.pages['operational-receivables'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper, title: 'Receivables & Revenue', single_column: true
	});
	new OperationalReceivables(page).init();
};

// Add a route here the moment each future page exists — every page in the
// set carries the same list, so the tab bar reads identically everywhere.
const ROUTES = [
	{ label: 'CEO Dashboard', route: 'management-dashboard' },
	{ label: 'Receivables & Revenue', route: 'operational-receivables', current: true },
	{ label: 'Cash & Collections', route: 'operational-cash' },
	{ label: 'Expenses & Payables', route: 'operational-payables' },
	{ label: 'Stock', route: 'operational-stock' },
];

const A1_PAGE_SIZE = 31;

class OperationalReceivables {
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
		// Fallback path — same links, in case someone still reaches for it.
		ROUTES.filter(r => !r.current).forEach(r => {
			this.page.add_menu_item(r.label, () => frappe.set_route(r.route));
		});
	}

	render_nav() {
		const html = ROUTES.map(r =>
			`<a class="rd-mode${r.current ? ' active' : ''}" data-route="${r.route}" href="/app/${r.route}">${r.label}</a>`
		).join('');
		this.$c.find('#or-suite-nav').html(html);

		// FIXED 2026-08-22: the <a href> alone still went blank until a
		// manual refresh — Frappe's own document-level link handler was
		// intercepting the click and doing its own client-side routing
		// before the destination page's JS was necessarily loaded, exactly
		// like the <button> it replaced. This handler runs first (it's
		// bound on the link itself, not on document), stops that handler
		// from ever seeing the click, and forces a real full-page load —
		// the same thing a manual refresh does, which is why that always
		// "fixed" it.
		this.$c.find('#or-suite-nav a').on('click', (e) => {
			if ($(e.currentTarget).hasClass('active')) { e.preventDefault(); return; }
			e.preventDefault();
			e.stopPropagation();
			window.location.href = e.currentTarget.href;
		});
	}

	// --------------------------------------------------------------- filters
	//
	// FIXED 2026-08-22, take 4: page.add_field() does not reliably render
	// into this page's toolbar at all, for ANY field type — the previous
	// fix moved From/To out of it on the (correct) theory that Date wasn't
	// supported, but left Company on page.add_field() because it's a
	// Select, the one type proven to work on the CEO dashboard. It doesn't
	// work here — the deployed screenshot shows no Company control
	// anywhere. Whatever's different about this page's toolbar, three
	// rounds of chasing it (class toggle, !important CSS, field-type
	// swap) haven't found it, so this stops trying to use the toolbar at
	// all. All three filters are now plain HTML controls in their own
	// strip in the page body, styled to look like a toolbar (light
	// background, sits right under the title, tabs below it) — same
	// visual order as the CEO dashboard's real toolbar-then-tabs layout,
	// built with a mechanism proven to actually render on this page.

	build_filters() {
		const companies = this.filter_meta.companies || [];
		const today = frappe.datetime.get_today();
		const week_ago = frappe.datetime.add_days(today, -6);
		const default_company = this.filter_meta.default_company || 'All companies';

		this.$c.find('#or-filterbar').html(`
			<div class="or-filter-field">
				<label>From</label>
				<input type="date" class="or-from" value="${week_ago}">
			</div>
			<div class="or-filter-field">
				<label>To</label>
				<input type="date" class="or-to" value="${today}">
			</div>
			<div class="or-filter-field">
				<label>Company</label>
				<select class="or-company">
					${['All companies'].concat(companies).map(c =>
						`<option value="${c}" ${c === default_company ? 'selected' : ''}>${c}</option>`
					).join('')}
				</select>
			</div>
			<button class="or-btn or-refresh">Refresh</button>
		`);
		this.$c.find('.or-from, .or-to, .or-company').on('change', () => this.refresh_all());
		this.$c.find('.or-refresh').on('click', () => this.refresh_all());
	}

	get filters() {
		const company = this.$c.find('.or-company').val();
		return {
			from_date: this.$c.find('.or-from').val(),
			to_date: this.$c.find('.or-to').val(),
			company: (company && company !== 'All companies') ? company : null,
		};
	}

	// ---------------------------------------------------------------- layout

	build_layout() {
		this.$c.html(`
			<div class="rd or-page">
				<style>
					/* ---- design tokens, copied from management_dashboard.js's .rd ---- */
					.or-page { --ink:#0f172a; --dim:#64748b; --line:#e2e8f0; --navy:#1e3a5f;
						--good:#15803d; --bad:#b91c1c; --amber:#a16207;
						padding:15px 15px 50px; color:var(--ink); }

					.or-topbar { margin:16px 0 18px; }

					/* POLISHED 2026-08-22 — dropped the bordered/shaded card
					   look; a flush row (no box) sits closer to how the CEO
					   dashboard's own native toolbar reads — controls right
					   under the title, not visually boxed off as their own
					   section. */
					.or-filterbar { display:flex; align-items:center; gap:20px; flex-wrap:wrap;
						padding:4px 2px 16px; margin:0; border-bottom:1px solid var(--line); }
					.or-filter-field { display:flex; align-items:center; gap:8px; }
					.or-filter-field label { font-size:12px; color:var(--dim); font-weight:600; }
					.or-filter-field input[type=date], .or-filter-field select {
						padding:5px 9px; border-radius:6px; border:1px solid var(--line);
						font-size:12px; background:#fff; color:var(--ink); line-height:1.4; }
					.or-filter-field input[type=date]:focus, .or-filter-field select:focus {
						outline:none; border-color:var(--navy); }
					.or-filterbar .or-refresh { margin-left:auto; }
					.rd-modes { display:inline-flex; background:#f1f5f9; border-radius:10px; padding:3px; }
					.rd-mode { border:0; background:transparent; padding:7px 18px; border-radius:8px;
						font-size:13px; cursor:pointer; color:var(--dim); display:inline-block;
						text-decoration:none; line-height:1.4; }
					.rd-mode.active { background:#fff; color:var(--navy); font-weight:600;
						box-shadow:0 1px 3px rgba(15,23,42,.12); cursor:default; }
					.rd-mode:not(.active):hover { color:var(--navy); text-decoration:none; }

					/* ---- panels ---- */
					.or-section { background:#fff; border:1px solid var(--line); border-radius:12px;
						padding:16px 18px 20px; margin-bottom:18px; }
					.or-section h4 { display:flex; justify-content:space-between; align-items:center;
						flex-wrap:wrap; gap:10px; font-size:14px; font-weight:700; letter-spacing:-.01em;
						margin:0 0 14px; }
					.or-btn { border:1px solid var(--line); background:#fff; border-radius:8px;
						padding:5px 12px; font-size:12px; cursor:pointer; color:var(--dim); }
					.or-btn:hover { border-color:var(--navy); color:var(--navy); }
					.or-btn:disabled { opacity:.4; cursor:default; }
					.or-btn:disabled:hover { border-color:var(--line); color:var(--dim); }

					/* ---- cards ---- */
					.or-cards { display:grid; gap:11px; grid-template-columns:repeat(auto-fit,minmax(185px,1fr));
						margin-bottom:14px; }
					.or-card { border:1px solid var(--line); border-left:3px solid var(--line); border-radius:10px;
						padding:13px 15px; background:#fff; }
					.or-card.clickable { cursor:pointer; }
					.or-card.clickable:hover { box-shadow:0 2px 8px rgba(15,23,42,.08); border-color:var(--navy); }
					.or-card .label { font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:.05em; }
					.or-card .value { font-size:20px; font-weight:700; margin-top:4px; font-variant-numeric:tabular-nums; }
					.or-card.warn { border-left-color:var(--amber); }
					.or-card.good { border-left-color:var(--good); }
					.or-card.rev { border-left-color:var(--navy); }

					/* ---- bridge panels ---- */
					.or-bridge { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
						background:#f8fafc; border:1px solid var(--line); border-radius:10px;
						padding:12px 14px; margin:10px 0 12px; font-size:13px; }
					.or-bridge-term { display:flex; flex-direction:column; align-items:flex-start; }
					.or-bridge-term .t-lab { font-size:10px; color:var(--dim); text-transform:uppercase; letter-spacing:.05em; }
					.or-bridge-term .t-val { font-size:15px; font-weight:700; font-variant-numeric:tabular-nums; }
					.or-bridge-op { font-size:16px; color:var(--dim); font-weight:600; }
					.or-bridge-term.result { margin-left:auto; }
					.or-bridge-term.result .t-val { color:var(--navy); }
					.or-bridge-note { font-size:12px; color:var(--dim); margin-top:2px; line-height:1.5; }

					/* ---- tables ---- */
					.or-table-wrap { overflow-x:auto; }
					.or-table { width:100%; border-collapse:collapse; font-size:13px; }
					.or-table th { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim);
						text-align:right; border-bottom:2px solid var(--line); padding:8px 10px; white-space:nowrap; }
					.or-table th:first-child, .or-table td:first-child { text-align:left; }
					.or-table td { text-align:right; padding:7px 10px; font-variant-numeric:tabular-nums;
						border-bottom:1px solid #f1f5f9; white-space:nowrap; }
					.or-table tbody tr:hover td { background:#f8fafc; }
					.or-table tr.neg td { color:var(--bad); }
					.or-row-link { cursor:pointer; }
					.or-msg { font-size:12px; color:var(--dim); margin-top:8px; line-height:1.5; }

					/* a dialog table's free-text columns (Party/Remarks) need to
					   wrap, or a long value pushes the whole table wider than the
					   dialog and spills out past its edge. FIXED 2026-08-22:
					   this used to be max-width only, with the table itself
					   at width:100% — with no floor on this column's width,
					   the browser squeezed it down to almost nothing to make
					   room for the other (nowrap) columns, wrapping every
					   single word onto its own line and blowing the row up
					   to 15+ lines tall (that's also why "Type" looked like
					   it was showing garbage — the row was so tall the
					   dialog was vertically scrolled into its middle).
					   min-width is the actual fix; the table below no
					   longer forces width:100% inside a dialog, so it can
					   overflow into its own horizontal scrollbar instead of
					   crushing this column. */
					.or-wrap { white-space:normal !important; min-width:200px; max-width:380px;
						word-break:break-word; text-align:left !important; }
					.or-dialog-scroll { max-height:60vh; overflow:auto; }
					.or-dialog-scroll table.or-table { width:auto; min-width:100%; }

					/* ---- pagination (A1) ---- */
					.or-pager { display:flex; align-items:center; gap:10px; margin-top:10px;
						font-size:12px; color:var(--dim); }

					@media (max-width:640px) {
						.or-section { padding:13px 12px 16px; }
						.or-table th, .or-table td { padding:7px 8px; }
					}
				</style>

				<div class="or-filterbar" id="or-filterbar"></div>
				<div class="or-topbar">
					<div class="rd-modes" id="or-suite-nav"></div>
				</div>

				<div class="or-section" id="or-a1">
					<h4>Receivables Rollforward <button class="or-btn or-export" data-target="a1">Export CSV</button></h4>
					<div class="or-cards" id="or-a1-cards"></div>
					<div class="or-table-wrap"><table class="or-table" id="or-a1-table"></table></div>
					<div class="or-pager" id="or-a1-pager"></div>
					<div class="or-msg" id="or-a1-msg"></div>
				</div>

				<div class="or-section" id="or-a2">
					<h4>Receivables by Patient Type (as of "To" date) <button class="or-btn or-export" data-target="a2">Export CSV</button></h4>
					<div class="or-cards" id="or-a2-cards"></div>
					<div class="or-bridge" id="or-a2-bridge"></div>
					<div class="or-msg" id="or-a2-msg"></div>
				</div>

				<div class="or-section" id="or-a3">
					<h4>Top Receivable Movers <button class="or-btn or-export" data-target="a3">Export CSV</button></h4>
					<div class="or-table-wrap"><table class="or-table" id="or-a3-table"></table></div>
				</div>

				<div class="or-section" id="or-b1">
					<h4>Revenue by Item Group <button class="or-btn or-export" data-target="b1">Export CSV</button></h4>
					<div class="or-cards" id="or-b1-cards"></div>
					<div class="or-bridge" id="or-b1-bridge"></div>
					<div class="or-table-wrap"><table class="or-table" id="or-b1-table"></table></div>
					<div class="or-msg" id="or-b1-msg"></div>
				</div>
			</div>
		`);
	}

	bind_events() {
		this.$c.on('click', '.or-export', (e) => {
			const t = $(e.currentTarget).data('target');
			this.export_table(t);
		});
	}

	// --------------------------------------------------------------- refresh

	refresh_all() {
		const f = this.filters;
		if (!f.from_date || !f.to_date) return;
		// 2026-08-22 — tag every panel's fetch with this refresh's
		// generation. Requests are not guaranteed to resolve in the order
		// they were fired (a slower-but-earlier request can land after a
		// faster-but-later one), so a stale response is dropped in
		// guarded_call() instead of overwriting a newer, correctly-filtered
		// render — this is what caused A2 to briefly show a previous
		// filter selection's data on 2026-08-22, confirmed against real
		// screenshots.
		this._req_gen = (this._req_gen || 0) + 1;
		this.load_a1(f);
		this.load_a2(f);
		this.load_a3(f);
		this.load_b1(f);
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

	// ------------------------------------------------------------------- A1

	load_a1(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_receivable.get_ar_rollforward',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company },
			(d) => this.render_a1(d || {})
		);
	}

	render_a1(d) {
		$('#or-a1-cards').html(`
			${this.card('Opening (start of range)', d.opening_total, false, 'Real GL balance the day before the range starts', 'rev')}
			${this.card('Closing (end of range)', d.closing_total, false, 'Real GL balance as of the "To" date — matches CEO Dashboard "Owed to us" once the month is snapshotted', 'rev')}
			${this.card('Net movement', (d.closing_total || 0) - (d.opening_total || 0), false, 'Closing minus opening — how much the receivable grew or shrank over the range')}
		`);

		this._a1_days = d.days || [];
		this._a1_page = 0;

		const rows = [['Date', 'Opening', 'Billed', 'Collected', 'Adjustments', 'Closing']];
		this._a1_days.forEach(day => rows.push(
			[day.date, day.opening, day.billed, day.collected, day.adjustments, day.closing]));
		this.export_rows.a1 = rows;

		$('#or-a1-msg').text(d.message || '');
		this.render_a1_page();
	}

	// FIXED 2026-08-22 — a multi-month range used to render one row per
	// calendar day straight onto the page (90+ rows for a 3-month range),
	// which is exactly the kind of unbounded table that made the page run
	// long before. Paginated client-side now, A1_PAGE_SIZE days at a
	// time — the full range is still fetched once and kept in memory
	// (this._a1_days), so paging and CSV export are both instant, no
	// extra server round-trip.
	render_a1_page() {
		const days = this._a1_days || [];
		const pages = Math.max(1, Math.ceil(days.length / A1_PAGE_SIZE));
		this._a1_page = Math.min(Math.max(0, this._a1_page), pages - 1);
		const start = this._a1_page * A1_PAGE_SIZE;
		const slice = days.slice(start, start + A1_PAGE_SIZE);

		let html = '<tr><th>Date</th><th>Opening</th><th>Billed</th><th>Collected</th><th>Adjustments</th><th>Closing</th></tr>';
		slice.forEach(day => {
			html += `<tr class="or-row-link" data-date="${day.date}">
				<td>${day.date}</td><td>${this.n(day.opening)}</td><td>${this.n(day.billed)}</td>
				<td>${this.n(day.collected)}</td><td>${this.n(day.adjustments)}</td>
				<td><b>${this.n(day.closing)}</b></td></tr>`;
		});
		$('#or-a1-table').html(html);

		if (days.length > A1_PAGE_SIZE) {
			$('#or-a1-pager').html(`
				<button class="or-btn" id="or-a1-prev" ${this._a1_page === 0 ? 'disabled' : ''}>&larr; Newer</button>
				<span>Days ${start + 1}&ndash;${start + slice.length} of ${days.length}</span>
				<button class="or-btn" id="or-a1-next" ${this._a1_page >= pages - 1 ? 'disabled' : ''}>Older &rarr;</button>
			`);
			$('#or-a1-prev').on('click', () => { this._a1_page--; this.render_a1_page(); });
			$('#or-a1-next').on('click', () => { this._a1_page++; this.render_a1_page(); });
		} else {
			$('#or-a1-pager').empty();
		}

		$('#or-a1-table').off('click', 'tr.or-row-link').on('click', 'tr.or-row-link', (e) => {
			const date = $(e.currentTarget).data('date');
			if (this.guard_dblclick('a1-' + date)) return;
			this.show_day_drilldown(date);
		});
	}

	show_day_drilldown(date) {
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_receivable.get_ar_rollforward_drilldown',
			args: { date, company: this.filters.company },
			callback: (r) => {
				const rows = r.message || [];
				let html = '<div class="or-dialog-scroll"><table class="or-table" style="width:100%">' +
					'<tr><th>Type</th><th>Voucher</th><th>Party</th><th>Account</th><th>Amount</th><th>Remarks</th></tr>';
				rows.forEach(x => {
					html += `<tr><td>${x.voucher_type}</td><td>${x.voucher_no}</td>
						<td>${x.party || ''}</td><td>${x.account}</td>
						<td style="text-align:right">${this.n(x.amount)}</td><td class="or-wrap">${x.remarks || ''}</td></tr>`;
				});
				html += '</table></div>';
				this.open_dialog(`Receivable vouchers — ${date}`, html);
			},
		});
	}

	// ------------------------------------------------------------------- A2
	//
	// Summary cards + click-to-dialog for the invoice list (fixed
	// 2026-08-22 — this used to dump the entire invoice-level list onto
	// the page unconditionally). The bridge below the cards shows the
	// true ledger balance next to the invoice-level sum, live, so the
	// size of the "unmatched receipts" gap is never a mystery.

	load_a2(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_receivable.get_ar_by_patient_type',
			{ as_of_date: f.to_date, company: f.company },
			(d) => this.render_a2(d || {})
		);
	}

	render_a2(d) {
		this._a2_drilldown = d.drilldown || [];
		const summary = d.summary || [];
		$('#or-a2-cards').html(summary.map(s => `
			<div class="or-card clickable${s.patient_type === 'Unclassified' ? ' warn' : ''}" data-patient-type="${s.patient_type}">
				<div class="label">${s.patient_type} outstanding (invoice-level)</div>
				<div class="value">${this.n(s.outstanding)}</div>
				<div class="or-msg" style="margin-top:2px">${s.invoice_count} invoices · ${s.patient_count} patients · click to view</div>
			</div>
		`).join(''));

		const b = d.bridge || {};
		$('#or-a2-bridge').html(`
			<div class="or-bridge-term">
				<div class="t-lab">Sum of invoice-level outstanding</div>
				<div class="t-val">${this.n(b.invoice_level_total)}</div>
			</div>
			<div class="or-bridge-op">&minus;</div>
			<div class="or-bridge-term">
				<div class="t-lab">Unmatched receipts (real cash, not tied to one invoice)</div>
				<div class="t-val">${this.n(b.unmatched_receipts)}</div>
			</div>
			<div class="or-bridge-op">=</div>
			<div class="or-bridge-term result">
				<div class="t-lab">True ledger balance (matches CEO Dashboard's "Owed to us")</div>
				<div class="t-val">${this.n(b.true_closing_balance)}</div>
			</div>
		`);
		$('#or-a2-msg').html(
			`<div class="or-bridge-note">${(b.message || '')}</div><div style="margin-top:4px">${d.message || ''}</div>`
		);

		const rows = [['Invoice', 'Date', 'Age', 'Bucket', 'Customer', 'Patient Type', 'Payer Type', 'Outstanding']];
		this._a2_drilldown.forEach(x => rows.push([x.invoice, x.posting_date, x.age, x.bucket,
			x.customer_name, x.patient_type, x.payer_type, x.outstanding]));
		this.export_rows.a2 = rows;

		$('#or-a2-cards').off('click', '.or-card').on('click', '.or-card', (e) => {
			const pt = $(e.currentTarget).data('patient-type');
			if (this.guard_dblclick('a2-' + pt)) return;
			this.show_a2_drilldown(pt);
		});
	}

	show_a2_drilldown(patient_type) {
		const rows = this._a2_drilldown.filter(x => x.patient_type === patient_type);
		let html = '<div class="or-dialog-scroll"><table class="or-table" style="width:100%">' +
			'<tr><th>Invoice</th><th>Date</th><th>Age</th><th>Bucket</th><th>Customer</th><th>Payer Type</th><th>Outstanding</th></tr>';
		rows.forEach(x => {
			html += `<tr><td>${x.invoice}</td><td style="text-align:right">${x.posting_date}</td><td style="text-align:right">${x.age}</td>
				<td style="text-align:right">${x.bucket}</td><td class="or-wrap">${x.customer_name}</td>
				<td style="text-align:right">${x.payer_type}</td><td style="text-align:right">${this.n(x.outstanding)}</td></tr>`;
		});
		html += '</table></div>';
		this.open_dialog(`${patient_type} outstanding — ${rows.length} invoice(s)`, html, 'extra-large');
	}

	// ------------------------------------------------------------------- A3

	load_a3(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_receivable.get_ar_top_movers',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company, limit: 15 },
			(list) => this.render_a3(list || [])
		);
	}

	render_a3(list) {
		// Customer name resolved server-side now (see get_ar_top_movers) —
		// this used to show the raw ID (CUST-2026-204512); the ID is kept
		// in the CSV export as its own column, since finance may still
		// want it for lookups, but the on-page table shows the name only.
		const rows = [['Customer', 'Customer ID', 'Billed', 'Collected', 'Net Movement', 'Transactions']];
		let html = '<tr><th>Customer</th><th>Billed</th><th>Collected</th><th>Net Movement</th><th>Transactions</th></tr>';
		list.forEach(x => {
			const name = x.customer_name || x.customer;
			rows.push([name, x.customer, x.billed, x.collected, x.net_movement, x.transactions]);
			const cls = x.net_movement > 0 ? 'neg' : '';
			html += `<tr class="${cls}"><td>${name}</td><td>${this.n(x.billed)}</td>
				<td>${this.n(x.collected)}</td><td><b>${this.n(x.net_movement)}</b></td><td>${x.transactions}</td></tr>`;
		});
		$('#or-a3-table').html(html);
		this.export_rows.a3 = rows;
	}

	// ------------------------------------------------------------------- B1
	//
	// Bridge added 2026-08-22 — see operational_api_receivable.py's
	// get_revenue_by_item_group docstring for the exact reconciliation.

	load_b1(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_receivable.get_revenue_by_item_group',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company },
			(d) => this.render_b1(d || {})
		);
	}

	render_b1(d) {
		const byPT = d.by_patient_type || [];
		$('#or-b1-cards').html(
			this.card('Invoice revenue (this range)', d.total_net, false, 'Net invoiced amount across all Sales Invoices posted in the range', 'rev') +
			byPT.map(p => this.card(p.patient_type, p.net_amount, p.patient_type === 'Unclassified')).join('')
		);

		const b = d.bridge || {};
		$('#or-b1-bridge').html(`
			<div class="or-bridge-term">
				<div class="t-lab">Invoice revenue</div>
				<div class="t-val">${this.n(b.invoice_net)}</div>
			</div>
			<div class="or-bridge-op">+</div>
			<div class="or-bridge-term">
				<div class="t-lab">Journal-booked revenue</div>
				<div class="t-val">${this.n(b.gross_sales)}</div>
			</div>
			<div class="or-bridge-op">&minus;</div>
			<div class="or-bridge-term">
				<div class="t-lab">Reclassified elsewhere</div>
				<div class="t-val">${this.n(b.revenue_reclass)}</div>
			</div>
			<div class="or-bridge-op">=</div>
			<div class="or-bridge-term result">
				<div class="t-lab">Combined net (matches CEO Dashboard's Net Sales)</div>
				<div class="t-val">${this.n(b.combined_net)}</div>
			</div>
		`);

		const groups = d.by_item_group || [];
		const rows = [['Item Group', 'Gross', 'Net', 'OPD', 'IPD', 'Unclassified', 'Share', 'Invoices']];
		let html = '<tr>' + rows[0].map(h => `<th>${h}</th>`).join('') + '</tr>';
		groups.forEach(g => {
			rows.push([g.item_group, g.gross_amount, g.net_amount, g.opd, g.ipd, g.unclassified,
				(g.share * 100).toFixed(1) + '%', g.invoice_count]);
			html += `<tr><td>${g.item_group}</td><td>${this.n(g.gross_amount)}</td>
				<td><b>${this.n(g.net_amount)}</b></td><td>${this.n(g.opd)}</td><td>${this.n(g.ipd)}</td>
				<td>${this.n(g.unclassified)}</td><td>${(g.share * 100).toFixed(1)}%</td><td>${g.invoice_count}</td></tr>`;
		});
		$('#or-b1-table').html(html);
		$('#or-b1-msg').html(
			`<div>${d.message || ''}</div><div class="or-bridge-note" style="margin-top:4px">${(b.message || '')}</div>` +
			// 2026-08-22 — this range may not fully tie to CEO Dashboard if the
			// month is already snapshotted: this bridge reads live GL Entry,
			// so a Journal Entry posted after CEO Dashboard's snapshot for
			// that month was last rebuilt (backdated into an already-closed
			// range) shows up here immediately but not there yet. Confirmed
			// for Jan/Jul 2026: the live gap tied to the cent to that
			// month's payment write-off figure once new journal entries
			// covering it were posted after the snapshot.
			`<div class="or-bridge-note" style="margin-top:4px">If this doesn't match CEO Dashboard's Net Sales for an ` +
			`already-closed month, it's most likely a voucher posted after that month's snapshot was last rebuilt — ` +
			`this panel is always live, CEO Dashboard updates on its own schedule.</div>`
		);
		this.export_rows.b1 = rows;
	}

	// ---------------------------------------------------------------- helpers

	card(label, value, warn, sub, tone) {
		return `<div class="or-card${warn ? ' warn' : ''}${tone ? ' ' + tone : ''}">
			<div class="label">${label}</div>
			<div class="value">${this.n(value)}</div>
			${sub ? `<div class="or-msg" style="margin-top:2px">${sub}</div>` : ''}
		</div>`;
	}

	n(v) {
		v = flt(v);
		return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
	}

	// A fast double-click on a row fires two separate 'click' events —
	// without this, that opened two copies of the same dialog. `key`
	// identifies what's being opened (e.g. "a1-2026-01-05"); a second
	// call for the same key inside the guard window is dropped silently.
	guard_dblclick(key) {
		this._click_guard = this._click_guard || {};
		const now = new Date().getTime();
		const last = this._click_guard[key] || 0;
		this._click_guard[key] = now;
		return (now - last) < 500;
	}

	// Every drilldown dialog goes through this one place now, so the
	// scroll wrapper and sizing are consistent instead of hand-repeated
	// per dialog (that inconsistency is exactly how the Remarks-overflow
	// bug happened — one dialog had the scroll wrapper, most didn't).
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
	// Same to_csv/download pattern as management_dashboard.js — kept
	// self-contained here rather than shared, since this app has no JS
	// module bundling between pages yet.

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