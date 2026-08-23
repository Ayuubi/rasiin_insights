/*
 * Operational Reports — Page 4: Stock.
 *
 * Path: rasiin_insights/management_dashboard/page/operational_stock/
 *         operational_stock.js
 *
 * Pharmacy/store + finance view of inventory — the companion to Pages
 * 1-3, same "live, transaction-level" philosophy applied to stock instead
 * of money. Reads live from operational_api_stock.py, which reads live
 * from Bin / Stock Ledger Entry / Stock Entry / Stock Reconciliation /
 * Purchase Receipt / GL Entry — never Management Snapshot or Fact.
 *
 * Built 2026-08-22, mirroring operational_payables.js's already-settled
 * patterns from day one (see claude/next-steps-stock-page4.md and
 * claude/page4-stock-scope-v1.md / page4-stock-data-validation.md):
 *   - Company default = api.get_filters()'s default_company (Shaafi
 *     Hospital). Shaafi Diagnostic Center has warehouses in the master
 *     but ZERO Bin/Stock Ledger Entry rows in the validated exports —
 *     same "static, no real activity" shape as the AR/AP side.
 *   - Filters are plain HTML controls, NOT page.add_field().
 *   - guarded_call()/_req_gen race-condition fix built in from the start
 *     (not retrofitted like it was on Pages 1-3).
 *   - Suite nav is a real <a href="/app/..."> with an explicit click
 *     handler that forces a full page load.
 *   - Every drilldown goes through open_dialog()/guard_dblclick(), with
 *     min-width (not just max-width) on free-text columns.
 *   - Long tables paginate client-side, 31 rows/page, full data kept in
 *     memory for CSV export.
 *   - S1/S2/S3 (current stock value, stock by item, dead stock) are all
 *     read from Bin, which has no historical grain — they ignore the
 *     From/To date filter by design (always "as of right now") while
 *     S4/S5/S6/S7/S8 use it normally. See operational_api_stock.py's
 *     docstring for why.
 */

frappe.pages['operational-stock'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper, title: 'Stock', single_column: true
	});
	new OperationalStock(page).init();
};

// Same list as every other page in the set — keep this in sync with
// management_dashboard.js / operational_receivables.js /
// operational_cash.js / operational_payables.js's own ROUTES arrays.
const ROUTES = [
	{ label: 'CEO Dashboard', route: 'management-dashboard' },
	{ label: 'Receivables & Revenue', route: 'operational-receivables' },
	{ label: 'Cash & Collections', route: 'operational-cash' },
	{ label: 'Expenses & Payables', route: 'operational-payables' },
	{ label: 'Stock', route: 'operational-stock', current: true },
];

const PAGE_SIZE = 31;

class OperationalStock {
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
		this.$c.find('#os-suite-nav').html(html);

		this.$c.find('#os-suite-nav a').on('click', (e) => {
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

		this.$c.find('#os-filterbar').html(`
			<div class="os-filter-field">
				<label>From</label>
				<input type="date" class="os-from" value="${week_ago}">
			</div>
			<div class="os-filter-field">
				<label>To</label>
				<input type="date" class="os-to" value="${today}">
			</div>
			<div class="os-filter-field">
				<label>Company</label>
				<select class="os-company">
					${['All companies'].concat(companies).map(c =>
						`<option value="${c}" ${c === default_company ? 'selected' : ''}>${c}</option>`
					).join('')}
				</select>
			</div>
			<button class="os-btn os-refresh">Refresh</button>
		`);
		this.$c.find('.os-from, .os-to, .os-company').on('change', () => this.refresh_all());
		this.$c.find('.os-refresh').on('click', () => this.refresh_all());
	}

	get filters() {
		const company = this.$c.find('.os-company').val();
		return {
			from_date: this.$c.find('.os-from').val(),
			to_date: this.$c.find('.os-to').val(),
			company: (company && company !== 'All companies') ? company : null,
		};
	}

	// ---------------------------------------------------------------- layout

	build_layout() {
		this.$c.html(`
			<div class="rd os-page">
				<style>
					/* ---- design tokens, copied from operational_payables.js's .op-page ---- */
					.os-page { --ink:#0f172a; --dim:#64748b; --line:#e2e8f0; --navy:#1e3a5f;
						--good:#15803d; --bad:#b91c1c; --amber:#a16207;
						padding:15px 15px 50px; color:var(--ink); }

					.os-topbar { margin:16px 0 18px; }

					.os-filterbar { display:flex; align-items:center; gap:20px; flex-wrap:wrap;
						padding:4px 2px 16px; margin:0; border-bottom:1px solid var(--line); }
					.os-filter-field { display:flex; align-items:center; gap:8px; }
					.os-filter-field label { font-size:12px; color:var(--dim); font-weight:600; }
					.os-filter-field input[type=date], .os-filter-field select, .os-filter-field input[type=text] {
						padding:5px 9px; border-radius:6px; border:1px solid var(--line);
						font-size:12px; background:#fff; color:var(--ink); line-height:1.4; }
					.os-filter-field input:focus, .os-filter-field select:focus {
						outline:none; border-color:var(--navy); }
					.os-filterbar .os-refresh { margin-left:auto; }
					.rd-modes { display:inline-flex; background:#f1f5f9; border-radius:10px; padding:3px; }
					.rd-mode { border:0; background:transparent; padding:7px 18px; border-radius:8px;
						font-size:13px; cursor:pointer; color:var(--dim); display:inline-block;
						text-decoration:none; line-height:1.4; }
					.rd-mode.active { background:#fff; color:var(--navy); font-weight:600;
						box-shadow:0 1px 3px rgba(15,23,42,.12); cursor:default; }
					.rd-mode:not(.active):hover { color:var(--navy); text-decoration:none; }

					/* ---- panels ---- */
					.os-section { background:#fff; border:1px solid var(--line); border-radius:12px;
						padding:16px 18px 20px; margin-bottom:18px; }
					.os-section h4 { display:flex; justify-content:space-between; align-items:center;
						flex-wrap:wrap; gap:10px; font-size:14px; font-weight:700; letter-spacing:-.01em;
						margin:0 0 4px; }
					.os-section-sub { font-size:12px; color:var(--dim); margin:0 0 14px; line-height:1.5; }
					.os-btn { border:1px solid var(--line); background:#fff; border-radius:8px;
						padding:5px 12px; font-size:12px; cursor:pointer; color:var(--dim); }
					.os-btn:hover { border-color:var(--navy); color:var(--navy); }
					.os-btn:disabled { opacity:.4; cursor:default; }
					.os-btn:disabled:hover { border-color:var(--line); color:var(--dim); }

					/* ---- cards ---- */
					.os-cards { display:grid; gap:11px; grid-template-columns:repeat(auto-fit,minmax(185px,1fr));
						margin-bottom:14px; }
					.os-card { border:1px solid var(--line); border-left:3px solid var(--line); border-radius:10px;
						padding:13px 15px; background:#fff; }
					.os-card .label { font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:.05em; }
					.os-card .value { font-size:20px; font-weight:700; margin-top:4px; font-variant-numeric:tabular-nums; }
					.os-card.warn { border-left-color:var(--amber); }
					.os-card.good { border-left-color:var(--good); }
					.os-card.rev { border-left-color:var(--navy); }

					/* ---- tables ---- */
					.os-table-wrap { overflow-x:auto; }
					.os-table { width:100%; border-collapse:collapse; font-size:13px; }
					.os-table th { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim);
						text-align:right; border-bottom:2px solid var(--line); padding:8px 10px; white-space:nowrap; }
					.os-table th:first-child, .os-table td:first-child { text-align:left; }
					.os-table td { text-align:right; padding:7px 10px; font-variant-numeric:tabular-nums;
						border-bottom:1px solid #f1f5f9; white-space:nowrap; }
					.os-table tbody tr:hover td { background:#f8fafc; }
					.os-table tr.neg td { color:var(--bad); }
					.os-row-link { cursor:pointer; }
					.os-msg { font-size:12px; color:var(--dim); margin-top:8px; line-height:1.5; }

					.os-wrap { white-space:normal !important; min-width:180px; max-width:340px;
						word-break:break-word; text-align:left !important; }
					.os-dialog-scroll { max-height:60vh; overflow:auto; }
					.os-dialog-scroll table.os-table { width:auto; min-width:100%; }

					.os-pager { display:flex; align-items:center; gap:10px; margin-top:10px;
						font-size:12px; color:var(--dim); }

					.os-subhead { font-size:12px; font-weight:700; color:var(--navy); margin:16px 0 8px; }
					.os-subhead:first-of-type { margin-top:0; }

					@media (max-width:640px) {
						.os-section { padding:13px 12px 16px; }
						.os-table th, .os-table td { padding:7px 8px; }
					}
				</style>

				<div class="os-filterbar" id="os-filterbar"></div>
				<div class="os-topbar">
					<div class="rd-modes" id="os-suite-nav"></div>
				</div>

				<div class="os-section" id="os-s1">
					<h4>Current Stock Value <button class="os-btn os-export" data-target="s1">Export CSV</button></h4>
					<div class="os-section-sub">Live, as of right now — not affected by the From/To filter above (Bin only ever holds today's balance).</div>
					<div class="os-cards" id="os-s1-cards"></div>
					<div class="os-table-wrap"><table class="os-table" id="os-s1-table"></table></div>
					<div class="os-msg" id="os-s1-msg"></div>
				</div>

				<div class="os-section" id="os-s2">
					<h4>Stock by Item
						<span>
							<input type="text" class="os-item-search" placeholder="Search item..." style="padding:5px 9px;border-radius:6px;border:1px solid var(--line);font-size:12px;margin-right:8px;">
							<button class="os-btn os-export" data-target="s2">Export CSV</button>
						</span>
					</h4>
					<div class="os-section-sub">Live, as of right now. Zero-quantity items are hidden.</div>
					<div class="os-table-wrap"><table class="os-table" id="os-s2-table"></table></div>
					<div class="os-pager" id="os-s2-pager"></div>
					<div class="os-msg" id="os-s2-msg"></div>
				</div>

				<div class="os-section" id="os-s3">
					<h4>Dead / Slow-Moving Stock <button class="os-btn os-export" data-target="s3">Export CSV</button></h4>
					<div class="os-section-sub">Live, as of right now — items still holding stock with no movement in 90 days.</div>
					<div class="os-cards" id="os-s3-cards"></div>
					<div class="os-table-wrap"><table class="os-table" id="os-s3-table"></table></div>
					<div class="os-pager" id="os-s3-pager"></div>
					<div class="os-msg" id="os-s3-msg"></div>
				</div>

				<div class="os-section" id="os-s4">
					<h4>Daily Stock Movement <button class="os-btn os-export" data-target="s4">Export CSV</button></h4>
					<div class="os-cards" id="os-s4-cards"></div>
					<div class="os-table-wrap"><table class="os-table" id="os-s4-table"></table></div>
					<div class="os-pager" id="os-s4-pager"></div>
					<div class="os-msg" id="os-s4-msg"></div>
				</div>

				<div class="os-section" id="os-s5">
					<h4>Movement by Voucher Type <button class="os-btn os-export" data-target="s5">Export CSV</button></h4>
					<div class="os-table-wrap"><table class="os-table" id="os-s5-table"></table></div>
					<div class="os-msg" id="os-s5-msg"></div>
				</div>

				<div class="os-section" id="os-s6">
					<h4>Stock Transfers &amp; Adjustments <button class="os-btn os-export" data-target="s6-transfers">Export CSV</button></h4>
					<div class="os-cards" id="os-s6-cards"></div>
					<div class="os-subhead">Stock Entries (transfers &amp; issues)</div>
					<div class="os-table-wrap"><table class="os-table" id="os-s6-transfers-table"></table></div>
					<div class="os-pager" id="os-s6-transfers-pager"></div>
					<div class="os-subhead">Stock Reconciliations (physical-count adjustments)
						<button class="os-btn os-export" data-target="s6-adjustments" style="margin-left:8px">Export CSV</button>
					</div>
					<div class="os-table-wrap"><table class="os-table" id="os-s6-adjustments-table"></table></div>
					<div class="os-pager" id="os-s6-adjustments-pager"></div>
					<div class="os-msg" id="os-s6-msg"></div>
				</div>

				<div class="os-section" id="os-s7">
					<h4>COGS &amp; Gross Profit <button class="os-btn os-export" data-target="s7">Export CSV</button></h4>
					<div class="os-cards" id="os-s7-cards"></div>
					<div class="os-table-wrap"><table class="os-table" id="os-s7-table"></table></div>
					<div class="os-msg" id="os-s7-msg"></div>
				</div>

				<div class="os-section" id="os-s8">
					<h4>Goods Received vs Invoiced (item level) <button class="os-btn os-export" data-target="s8">Export CSV</button></h4>
					<div class="os-cards" id="os-s8-cards"></div>
					<div class="os-table-wrap"><table class="os-table" id="os-s8-table"></table></div>
					<div class="os-pager" id="os-s8-pager"></div>
					<div class="os-msg" id="os-s8-msg"></div>
				</div>
			</div>
		`);
	}

	bind_events() {
		this.$c.on('click', '.os-export', (e) => {
			const t = $(e.currentTarget).data('target');
			this.export_table(t);
		});
		this.$c.on('input', '.os-item-search', () => {
			this._s2_page = 0;
			this.render_s2_page();
		});
	}

	// --------------------------------------------------------------- refresh

	refresh_all() {
		const f = this.filters;
		if (!f.from_date || !f.to_date) return;
		// Every panel's fetch is tagged with the generation of this
		// refresh_all() call — a stale response is dropped in
		// guarded_call() rather than overwriting a newer render.
		this._req_gen = (this._req_gen || 0) + 1;
		this.load_s1(f);
		this.load_s2(f);
		this.load_s3(f);
		this.load_s4(f);
		this.load_s5(f);
		this.load_s6(f);
		this.load_s7(f);
		this.load_s8(f);
	}

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

	// ------------------------------------------------------------------- S1

	load_s1(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_stock.get_stock_value_summary',
			{ company: f.company },
			(d) => this.render_s1(d || {})
		);
	}

	render_s1(d) {
		$('#os-s1-cards').html(`
			${this.card('Total stock value', d.total_value, false, 'Live, from Bin, every warehouse in scope', 'rev')}
			${this.card('Total quantity', d.total_qty, false, 'Sum of actual_qty across all items/warehouses')}
			${this.card('Warehouses holding stock', d.warehouse_count, false, 'Of the warehouses in scope for this company')}
		`);

		const rows = [['Warehouse', 'Company', 'Qty', 'Value']];
		let html = '<tr><th>Warehouse</th><th>Company</th><th>Qty</th><th>Value</th></tr>';
		(d.warehouses || []).forEach(w => {
			rows.push([w.warehouse, w.company, w.qty, w.value]);
			html += `<tr><td>${w.warehouse}</td><td>${w.company || ''}</td>
				<td>${this.n(w.qty)}</td><td><b>${this.n(w.value)}</b></td></tr>`;
		});
		$('#os-s1-table').html(html);
		this.export_rows.s1 = rows;
		$('#os-s1-msg').text(d.message || '');
	}

	// ------------------------------------------------------------------- S2

	load_s2(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_stock.get_stock_by_item',
			{ company: f.company, only_nonzero: 1 },
			(d) => {
				this._s2_rows = d.rows || [];
				this._s2_page = 0;
				$('#os-s2-msg').text(d.message || '');
				this.render_s2_page();
			}
		);
	}

	render_s2_page() {
		const search = ($('.os-item-search').val() || '').toLowerCase().trim();
		let list = this._s2_rows || [];
		if (search) {
			list = list.filter(x =>
				(x.item_code || '').toLowerCase().includes(search) ||
				(x.item_name || '').toLowerCase().includes(search));
		}

		const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
		this._s2_page = Math.min(Math.max(0, this._s2_page), pages - 1);
		const start = this._s2_page * PAGE_SIZE;
		const slice = list.slice(start, start + PAGE_SIZE);

		let html = '<tr><th>Item</th><th>Item Group</th><th>Warehouse</th><th>Qty</th><th>Valuation Rate</th><th>Stock Value</th></tr>';
		slice.forEach(x => {
			html += `<tr><td class="os-wrap">${x.item_name} <span style="color:var(--dim)">(${x.item_code})</span></td>
				<td>${x.item_group}</td><td>${x.warehouse}</td><td>${this.n(x.qty)}</td>
				<td>${this.n(x.valuation_rate)}</td><td><b>${this.n(x.stock_value)}</b></td></tr>`;
		});
		$('#os-s2-table').html(html);

		if (list.length > PAGE_SIZE) {
			$('#os-s2-pager').html(`
				<button class="os-btn" id="os-s2-prev" ${this._s2_page === 0 ? 'disabled' : ''}>&larr; Prev</button>
				<span>Rows ${start + 1}&ndash;${start + slice.length} of ${list.length}</span>
				<button class="os-btn" id="os-s2-next" ${this._s2_page >= pages - 1 ? 'disabled' : ''}>Next &rarr;</button>
			`);
			$('#os-s2-prev').on('click', () => { this._s2_page--; this.render_s2_page(); });
			$('#os-s2-next').on('click', () => { this._s2_page++; this.render_s2_page(); });
		} else {
			$('#os-s2-pager').empty();
		}

		const rows = [['Item Code', 'Item Name', 'Item Group', 'Warehouse', 'Qty', 'Valuation Rate', 'Stock Value']];
		(this._s2_rows || []).forEach(x => rows.push(
			[x.item_code, x.item_name, x.item_group, x.warehouse, x.qty, x.valuation_rate, x.stock_value]));
		this.export_rows.s2 = rows;
	}

	// ------------------------------------------------------------------- S3

	load_s3(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_stock.get_dead_stock',
			{ company: f.company },
			(d) => this.render_s3(d || {})
		);
	}

	render_s3(d) {
		$('#os-s3-cards').html(`
			${this.card('Dead/slow-moving items', (d.rows || []).length, (d.rows || []).length > 0, `No movement in ${d.cutoff_days || 90} days, still holding stock`)}
			${this.card('Value tied up', d.total_value, (d.total_value || 0) > 0, 'Total stock value of the items above', 'warn')}
		`);

		this._s3_rows = d.rows || [];
		this._s3_page = 0;

		const rows = [['Item', 'Item Group', 'Warehouse', 'Qty', 'Stock Value', 'Last Movement']];
		this._s3_rows.forEach(x => rows.push(
			[x.item_code, x.item_group, x.warehouse, x.qty, x.stock_value, x.last_movement_date || 'never']));
		this.export_rows.s3 = rows;

		$('#os-s3-msg').text(d.message || '');
		this.render_s3_page();
	}

	render_s3_page() {
		const list = this._s3_rows || [];
		const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
		this._s3_page = Math.min(Math.max(0, this._s3_page), pages - 1);
		const start = this._s3_page * PAGE_SIZE;
		const slice = list.slice(start, start + PAGE_SIZE);

		let html = '<tr><th>Item</th><th>Item Group</th><th>Warehouse</th><th>Qty</th><th>Stock Value</th><th>Last Movement</th></tr>';
		slice.forEach(x => {
			html += `<tr class="neg"><td class="os-wrap">${x.item_name} <span style="color:var(--dim)">(${x.item_code})</span></td>
				<td>${x.item_group}</td><td>${x.warehouse}</td><td>${this.n(x.qty)}</td>
				<td><b>${this.n(x.stock_value)}</b></td><td>${x.last_movement_date || 'never'}</td></tr>`;
		});
		$('#os-s3-table').html(html);

		if (list.length > PAGE_SIZE) {
			$('#os-s3-pager').html(`
				<button class="os-btn" id="os-s3-prev" ${this._s3_page === 0 ? 'disabled' : ''}>&larr; Prev</button>
				<span>Rows ${start + 1}&ndash;${start + slice.length} of ${list.length}</span>
				<button class="os-btn" id="os-s3-next" ${this._s3_page >= pages - 1 ? 'disabled' : ''}>Next &rarr;</button>
			`);
			$('#os-s3-prev').on('click', () => { this._s3_page--; this.render_s3_page(); });
			$('#os-s3-next').on('click', () => { this._s3_page++; this.render_s3_page(); });
		} else {
			$('#os-s3-pager').empty();
		}
	}

	// ------------------------------------------------------------------- S4

	load_s4(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_stock.get_daily_stock_movement',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company },
			(d) => this.render_s4(d || {})
		);
	}

	render_s4(d) {
		$('#os-s4-cards').html(`
			${this.card('Qty in (this range)', d.total_qty_in, false, 'Every positive stock movement — receiving, transfers in, adjustments up')}
			${this.card('Qty out (this range)', d.total_qty_out, false, 'Every negative stock movement — dispensing, transfers out, adjustments down')}
			${this.card('Net value change', d.total_value_change, false, 'Sum of stock_value_difference — how much total stock value moved', 'rev')}
		`);

		this._s4_days = d.days || [];
		this._s4_page = 0;

		const rows = [['Date', 'Qty In', 'Qty Out', 'Value Change', 'Vouchers']];
		this._s4_days.forEach(day => rows.push([day.date, day.qty_in, day.qty_out, day.value_change, day.vouchers]));
		this.export_rows.s4 = rows;

		$('#os-s4-msg').text(d.message || '');
		this.render_s4_page();
	}

	render_s4_page() {
		const days = this._s4_days || [];
		const pages = Math.max(1, Math.ceil(days.length / PAGE_SIZE));
		this._s4_page = Math.min(Math.max(0, this._s4_page), pages - 1);
		const start = this._s4_page * PAGE_SIZE;
		const slice = days.slice(start, start + PAGE_SIZE);

		let html = '<tr><th>Date</th><th>Qty In</th><th>Qty Out</th><th>Value Change</th><th>Vouchers</th></tr>';
		slice.forEach(day => {
			html += `<tr class="os-row-link" data-date="${day.date}">
				<td>${day.date}</td><td>${this.n(day.qty_in)}</td><td>${this.n(day.qty_out)}</td>
				<td><b>${this.n(day.value_change)}</b></td><td>${day.vouchers}</td></tr>`;
		});
		$('#os-s4-table').html(html);

		if (days.length > PAGE_SIZE) {
			$('#os-s4-pager').html(`
				<button class="os-btn" id="os-s4-prev" ${this._s4_page === 0 ? 'disabled' : ''}>&larr; Newer</button>
				<span>Days ${start + 1}&ndash;${start + slice.length} of ${days.length}</span>
				<button class="os-btn" id="os-s4-next" ${this._s4_page >= pages - 1 ? 'disabled' : ''}>Older &rarr;</button>
			`);
			$('#os-s4-prev').on('click', () => { this._s4_page--; this.render_s4_page(); });
			$('#os-s4-next').on('click', () => { this._s4_page++; this.render_s4_page(); });
		} else {
			$('#os-s4-pager').empty();
		}

		$('#os-s4-table').off('click', 'tr.os-row-link').on('click', 'tr.os-row-link', (e) => {
			const date = $(e.currentTarget).data('date');
			if (this.guard_dblclick('s4-' + date)) return;
			this.show_s4_drilldown(date);
		});
	}

	show_s4_drilldown(date) {
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_stock.get_stock_movement_day_drilldown',
			args: { date, company: this.filters.company },
			callback: (r) => {
				const d = r.message || {};
				const rows = d.rows || [];
				let html = '<div class="os-dialog-scroll"><table class="os-table" style="width:100%">' +
					'<tr><th>Item</th><th>Warehouse</th><th>Type</th><th>Voucher</th><th>Qty</th><th>Rate</th><th>Value Change</th></tr>';
				rows.forEach(x => {
					html += `<tr><td class="os-wrap">${x.item_name} (${x.item_code})</td><td>${x.warehouse}</td>
						<td>${x.voucher_type}</td><td>${x.voucher_no}</td><td>${this.n(x.actual_qty)}</td>
						<td>${this.n(x.valuation_rate)}</td><td>${this.n(x.stock_value_difference)}</td></tr>`;
				});
				html += '</table>';
				if (d.message) html += `<div class="os-msg">${d.message}</div>`;
				html += '</div>';
				this.open_dialog(`Stock movement — ${date}`, html);
			},
		});
	}

	// ------------------------------------------------------------------- S5

	load_s5(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_stock.get_movement_by_voucher_type',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company },
			(d) => this.render_s5(d || {})
		);
	}

	render_s5(d) {
		const rows = [['Voucher Type', 'Qty In', 'Qty Out', 'Value Change', 'Vouchers']];
		let html = '<tr><th>Voucher Type</th><th>Qty In</th><th>Qty Out</th><th>Value Change</th><th>Vouchers</th></tr>';
		(d.rows || []).forEach(x => {
			rows.push([x.voucher_type, x.qty_in, x.qty_out, x.value_change, x.vouchers]);
			html += `<tr><td>${x.voucher_type}</td><td>${this.n(x.qty_in)}</td><td>${this.n(x.qty_out)}</td>
				<td><b>${this.n(x.value_change)}</b></td><td>${x.vouchers}</td></tr>`;
		});
		$('#os-s5-table').html(html);
		this.export_rows.s5 = rows;
		$('#os-s5-msg').text(d.message || '');
	}

	// ------------------------------------------------------------------- S6

	load_s6(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_stock.get_stock_transfers',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company },
			(d) => this.render_s6_transfers(d || {})
		);
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_stock.get_stock_adjustments',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company },
			(d) => this.render_s6_adjustments(d || {})
		);
	}

	render_s6_transfers(d) {
		this._s6t_rows = d.rows || [];
		this._s6t_page = 0;

		const by_purpose = d.by_purpose || [];
		$('#os-s6-cards').html(
			by_purpose.map(p => this.card(p.purpose, p.amount, false, `${p.entries} entries, ${this.n(p.qty)} qty`)).join('') +
			(by_purpose.length ? '' : this.card('Stock Entries', 0, false, 'None in this range'))
		);

		const rows = [['Date', 'Name', 'Purpose', 'From', 'To', 'Items', 'Qty', 'Amount']];
		this._s6t_rows.forEach(x => rows.push(
			[x.posting_date, x.name, x.purpose, x.from_warehouse, x.to_warehouse, x.item_count, x.qty, x.amount]));
		this.export_rows['s6-transfers'] = rows;

		this.render_s6t_page();
	}

	render_s6t_page() {
		const list = this._s6t_rows || [];
		const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
		this._s6t_page = Math.min(Math.max(0, this._s6t_page), pages - 1);
		const start = this._s6t_page * PAGE_SIZE;
		const slice = list.slice(start, start + PAGE_SIZE);

		let html = '<tr><th>Date</th><th>Name</th><th>Purpose</th><th>From</th><th>To</th><th>Items</th><th>Qty</th><th>Amount</th></tr>';
		slice.forEach(x => {
			html += `<tr class="os-row-link" data-se="${x.name}"><td>${x.posting_date}</td><td>${x.name}</td>
				<td>${x.purpose || ''}</td><td>${x.from_warehouse || ''}</td><td>${x.to_warehouse || ''}</td>
				<td>${x.item_count}</td><td>${this.n(x.qty)}</td><td><b>${this.n(x.amount)}</b></td></tr>`;
		});
		$('#os-s6-transfers-table').html(html);

		if (list.length > PAGE_SIZE) {
			$('#os-s6-transfers-pager').html(`
				<button class="os-btn" id="os-s6t-prev" ${this._s6t_page === 0 ? 'disabled' : ''}>&larr; Prev</button>
				<span>Rows ${start + 1}&ndash;${start + slice.length} of ${list.length}</span>
				<button class="os-btn" id="os-s6t-next" ${this._s6t_page >= pages - 1 ? 'disabled' : ''}>Next &rarr;</button>
			`);
			$('#os-s6t-prev').on('click', () => { this._s6t_page--; this.render_s6t_page(); });
			$('#os-s6t-next').on('click', () => { this._s6t_page++; this.render_s6t_page(); });
		} else {
			$('#os-s6-transfers-pager').empty();
		}

		$('#os-s6-transfers-table').off('click', 'tr.os-row-link').on('click', 'tr.os-row-link', (e) => {
			const name = $(e.currentTarget).data('se');
			if (this.guard_dblclick('se-' + name)) return;
			this.show_stock_entry_drilldown(name);
		});
	}

	show_stock_entry_drilldown(name) {
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_stock.get_stock_entry_drilldown',
			args: { name },
			callback: (r) => {
				const rows = r.message || [];
				let html = '<div class="os-dialog-scroll"><table class="os-table" style="width:100%">' +
					'<tr><th>Item</th><th>From</th><th>To</th><th>Qty</th><th>UOM</th><th>Rate</th><th>Amount</th></tr>';
				rows.forEach(x => {
					html += `<tr><td class="os-wrap">${x.item_name} (${x.item_code})</td>
						<td>${x.s_warehouse || ''}</td><td>${x.t_warehouse || ''}</td>
						<td>${this.n(x.qty)}</td><td>${x.uom || ''}</td>
						<td>${this.n(x.valuation_rate)}</td><td>${this.n(x.amount)}</td></tr>`;
				});
				html += '</table></div>';
				this.open_dialog(`Stock Entry — ${name}`, html);
			},
		});
	}

	render_s6_adjustments(d) {
		this._s6a_rows = d.rows || [];
		this._s6a_page = 0;

		const rows = [['Date', 'Name', 'Purpose', 'Items', 'Value Change']];
		this._s6a_rows.forEach(x => rows.push([x.posting_date, x.name, x.purpose, x.item_count, x.value_change]));
		this.export_rows['s6-adjustments'] = rows;

		$('#os-s6-msg').text(d.message || '');
		this.render_s6a_page();
	}

	render_s6a_page() {
		const list = this._s6a_rows || [];
		const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
		this._s6a_page = Math.min(Math.max(0, this._s6a_page), pages - 1);
		const start = this._s6a_page * PAGE_SIZE;
		const slice = list.slice(start, start + PAGE_SIZE);

		let html = '<tr><th>Date</th><th>Name</th><th>Purpose</th><th>Items</th><th>Value Change</th></tr>';
		slice.forEach(x => {
			const cls = x.value_change < 0 ? 'neg' : '';
			html += `<tr class="os-row-link ${cls}" data-sr="${x.name}"><td>${x.posting_date}</td><td>${x.name}</td>
				<td>${x.purpose}</td><td>${x.item_count}</td><td><b>${this.n(x.value_change)}</b></td></tr>`;
		});
		$('#os-s6-adjustments-table').html(html);

		if (list.length > PAGE_SIZE) {
			$('#os-s6-adjustments-pager').html(`
				<button class="os-btn" id="os-s6a-prev" ${this._s6a_page === 0 ? 'disabled' : ''}>&larr; Prev</button>
				<span>Rows ${start + 1}&ndash;${start + slice.length} of ${list.length}</span>
				<button class="os-btn" id="os-s6a-next" ${this._s6a_page >= pages - 1 ? 'disabled' : ''}>Next &rarr;</button>
			`);
			$('#os-s6a-prev').on('click', () => { this._s6a_page--; this.render_s6a_page(); });
			$('#os-s6a-next').on('click', () => { this._s6a_page++; this.render_s6a_page(); });
		} else {
			$('#os-s6-adjustments-pager').empty();
		}

		$('#os-s6-adjustments-table').off('click', 'tr.os-row-link').on('click', 'tr.os-row-link', (e) => {
			const name = $(e.currentTarget).data('sr');
			if (this.guard_dblclick('sr-' + name)) return;
			this.show_stock_reconciliation_drilldown(name);
		});
	}

	show_stock_reconciliation_drilldown(name) {
		frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.operational_api_stock.get_stock_reconciliation_drilldown',
			args: { name },
			callback: (r) => {
				const rows = r.message || [];
				let html = '<div class="os-dialog-scroll"><table class="os-table" style="width:100%">' +
					'<tr><th>Item</th><th>Warehouse</th><th>System Qty</th><th>Counted Qty</th><th>Diff</th><th>Amount Diff</th></tr>';
				rows.forEach(x => {
					const cls = x.amount_difference < 0 ? 'neg' : '';
					html += `<tr class="${cls}"><td class="os-wrap">${x.item_name} (${x.item_code})</td>
						<td>${x.warehouse}</td><td>${this.n(x.current_qty)}</td><td>${this.n(x.qty)}</td>
						<td>${this.n(x.quantity_difference)}</td><td>${this.n(x.amount_difference)}</td></tr>`;
				});
				html += '</table></div>';
				this.open_dialog(`Stock Reconciliation — ${name}`, html);
			},
		});
	}

	// ------------------------------------------------------------------- S7

	load_s7(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_stock.get_cogs_profit',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company },
			(d) => this.render_s7(d || {})
		);
	}

	render_s7(d) {
		$('#os-s7-cards').html(`
			${this.card('Net Sales', d.net_sales, false, 'Invoice-line revenue, same figure the Receivables page ties to the CEO dashboard', 'rev')}
			${this.card('COGS', d.cogs, false, 'Sales-Invoice-attributable Cost of Goods Sold — ties exactly to Stock Ledger Entry')}
			${this.card('Gross Profit', d.gross_profit, false, 'Net Sales minus COGS', 'good')}
			${this.card('Gross Margin %', (d.margin_pct || 0) * 100, false, 'Gross Profit / Net Sales')}
		`);

		const rows = [['Item Group', 'Revenue', 'COGS', 'Gross Profit', 'Margin %']];
		let html = '<tr><th>Item Group</th><th>Revenue</th><th>COGS</th><th>Gross Profit</th><th>Margin %</th></tr>';
		(d.by_item_group || []).forEach(x => {
			rows.push([x.item_group, x.revenue, x.cogs, x.gross_profit, (x.margin_pct * 100).toFixed(1) + '%']);
			html += `<tr><td>${x.item_group}</td><td>${this.n(x.revenue)}</td><td>${this.n(x.cogs)}</td>
				<td><b>${this.n(x.gross_profit)}</b></td><td>${(x.margin_pct * 100).toFixed(1)}%</td></tr>`;
		});
		$('#os-s7-table').html(html);
		this.export_rows.s7 = rows;
		$('#os-s7-msg').text(d.message || '');
	}

	// ------------------------------------------------------------------- S8

	load_s8(f) {
		this.guarded_call(
			'rasiin_insights.management_dashboard.utils.operational_api_stock.get_grn_vs_invoiced',
			{ from_date: f.from_date, to_date: f.to_date, company: f.company },
			(d) => this.render_s8(d || {})
		);
	}

	render_s8(d) {
		$('#os-s8-cards').html(`
			${this.card('Received (this range)', d.total_received, false, 'Sum of Purchase Receipt Item amount', 'rev')}
			${this.card('Billed (this range)', d.total_billed, false, "Sum of Purchase Receipt Item's billed_amt")}
			${this.card('Unbilled gap', d.total_gap, (d.total_gap || 0) > 0, 'Received minus billed — the item-level companion to Payables\' C2', 'warn')}
		`);

		this._s8_rows = d.rows || [];
		this._s8_page = 0;

		const rows = [['Item', 'Received Qty', 'Received Amount', 'Billed Amount', 'Gap']];
		this._s8_rows.forEach(x => rows.push(
			[x.item_code, x.received_qty, x.received_amount, x.billed_amount, x.gap]));
		this.export_rows.s8 = rows;

		$('#os-s8-msg').text(d.message || '');
		this.render_s8_page();
	}

	render_s8_page() {
		const list = this._s8_rows || [];
		const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
		this._s8_page = Math.min(Math.max(0, this._s8_page), pages - 1);
		const start = this._s8_page * PAGE_SIZE;
		const slice = list.slice(start, start + PAGE_SIZE);

		let html = '<tr><th>Item</th><th>Received Qty</th><th>Received Amount</th><th>Billed Amount</th><th>Gap</th></tr>';
		slice.forEach(x => {
			const cls = Math.abs(x.gap) >= 0.01 ? 'neg' : '';
			html += `<tr class="${cls}"><td class="os-wrap">${x.item_name} <span style="color:var(--dim)">(${x.item_code})</span></td>
				<td>${this.n(x.received_qty)}</td><td>${this.n(x.received_amount)}</td>
				<td>${this.n(x.billed_amount)}</td><td><b>${this.n(x.gap)}</b></td></tr>`;
		});
		$('#os-s8-table').html(html);

		if (list.length > PAGE_SIZE) {
			$('#os-s8-pager').html(`
				<button class="os-btn" id="os-s8-prev" ${this._s8_page === 0 ? 'disabled' : ''}>&larr; Prev</button>
				<span>Rows ${start + 1}&ndash;${start + slice.length} of ${list.length}</span>
				<button class="os-btn" id="os-s8-next" ${this._s8_page >= pages - 1 ? 'disabled' : ''}>Next &rarr;</button>
			`);
			$('#os-s8-prev').on('click', () => { this._s8_page--; this.render_s8_page(); });
			$('#os-s8-next').on('click', () => { this._s8_page++; this.render_s8_page(); });
		} else {
			$('#os-s8-pager').empty();
		}
	}

	// ---------------------------------------------------------------- helpers

	card(label, value, warn, sub, tone) {
		return `<div class="os-card${warn ? ' warn' : ''}${tone ? ' ' + tone : ''}">
			<div class="label">${label}</div>
			<div class="value">${this.n(value)}</div>
			${sub ? `<div class="os-msg" style="margin-top:2px">${sub}</div>` : ''}
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