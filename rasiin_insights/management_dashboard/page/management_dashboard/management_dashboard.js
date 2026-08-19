/*
 * Management Dashboard — page controller (v3).
 *
 * Path: rasiin_insights/management_dashboard/page/management_dashboard/
 *         management_dashboard.js
 *
 * FILTERS
 *   View by   Monthly / Quarterly / Half-yearly / Yearly. Everything else
 *             follows from it — the period lists rebuild themselves.
 *   Focus     one Period selector. Quarterly gives 2026-Q1 (Jan-Mar),
 *             2026-Q2 (Apr-Jun) and so on, spelled out so nobody guesses.
 *   Compare   exactly two selectors, Period A and Period B. Choosing the same
 *             period twice is refused — there is nothing to compare.
 *
 * PRINT AND EXPORT
 *   Print hides the chrome and lays the page out for A4 landscape.
 *   Every table has its own CSV button, so the comparison or the breakdown can
 *   go into a mail or a board pack without retyping.
 */

frappe.pages['management-dashboard'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper, title: 'Management Dashboard', single_column: true
	});
	new ManagementDashboard(page).init();
};

const GROUPS = [
	{
		title: 'Revenue', tone: 'rev', lines: [
			{ key: 'gross_sales', label: 'Gross sales', metric: 'gross_sales',
			  note: 'Before any discount' },
			{ key: 'discount', label: 'Less: discount given', metric: 'discount',
			  sign: -1, note: 'Given on the invoice' },
			{ key: 'return', label: 'Less: returns', metric: 'return', sign: -1,
			  note: 'Credit notes' },
			{ key: 'return_discount', label: 'Add back: discount on returns',
			  metric: 'return_discount', muted: true,
			  note: 'Discount reversed when the return was booked' },
			{ key: 'revenue_reclass', label: 'Less: reclassified to another company',
			  metric: 'revenue_reclass', sign: -1, muted: true,
			  note: 'Booked by Journal Entry, moved to a different company — see Control Panel' },
			{ key: 'net_sales', label: 'Net sales', strong: true,
			  note: 'What the ledger calls income' }
		]
	},
	{
		title: 'Money in', tone: 'cash', lines: [
			{ key: 'collection_current', label: "This period's invoices",
			  metric: 'collection_current' },
			{ key: 'collection_prior', label: 'Older debt', metric: 'collection_prior' },
			{ key: 'collection_unallocated', label: 'Not matched to an invoice',
			  metric: 'collection_unallocated', warn: true },
			{ key: 'total_collections', label: 'Money received', strong: true },
			{ key: 'payment_discount', label: 'Memo: written off at payment',
			  metric: 'payment_discount', muted: true }
		]
	},
	{
		title: 'Money out', tone: 'out', lines: [
			{ key: 'commission', label: 'Doctor commission', metric: 'commission' },
			{ key: 'payroll', label: 'Staff payroll', metric: 'payroll' },
			{ key: 'expense', label: 'Other expense', metric: 'expense' },
			{ key: 'refund', label: 'Refunds', metric: 'refund' },
			{ key: 'money_out', label: 'Money out', strong: true },
			{ key: 'net_cash', label: 'Net cash movement', strong: true,
			  note: 'Received less everything paid out' }
		]
	},
	{
		title: 'What we owe, what we are owed', tone: 'owe', lines: [
			{ key: 'ar_closing', label: 'Owed to us', metric: 'ar_transfer_in',
			  note: 'Closing receivable' },
			{ key: 'ap_closing', label: 'Owed by us', metric: 'payable_charged',
			  note: 'Closing payable' }
		]
	},
	{
		title: 'Ratios', tone: 'ratio', pct: true, lines: [
			{ key: 'discount_pct', label: 'Discount as % of gross', warn: true },
			{ key: 'return_pct', label: 'Returns as % of gross' },
			{ key: 'collection_efficiency', label: 'Collection efficiency',
			  note: 'Received divided by net sales' },
			{ key: 'quality_score', label: 'Collections traced', raw_pct: true,
			  note: 'Share that could be tied to a service' }
		]
	}
];

const HERO = [
	{ key: 'net_sales', label: 'Net sales', tone: 'rev' },
	{ key: 'total_collections', label: 'Money received', tone: 'cash' },
	{ key: 'money_out', label: 'Money out', tone: 'out', bad: true },
	{ key: 'net_cash', label: 'Net cash movement', tone: 'owe' }
];

const DRILL_METRICS = [
	['gross_sales', 'Gross sales'],
	['discount', 'Discount given'],
	['return', 'Returns'],
	['collection_current,collection_prior,collection_unallocated', 'Money received'],
	['collection_current', 'Collected — this period only'],
	['collection_prior', 'Collected — older debt only'],
	['collection_unallocated', 'Collected — not matched'],
	['return_discount', 'Discount on returns'],
	['revenue_reclass', 'Reclassified to another company'],
	['payment_discount', 'Discount at payment'],
	['commission', 'Doctor commission'],
	['payroll', 'Staff payroll'],
	['expense', 'Other expense'],
	['commission,payroll,expense', 'Total expense'],
	['refund', 'Refunds'],
	['ar_transfer_in', 'Debt created'],
	['ar_transfer_out', 'Debt cleared'],
	['payable_charged', 'Owed by us — charged'],
	['supplier_payment', 'Owed by us — paid']
];

const MONTH_NAME = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
	'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// Daily trend metric choices. Flow metrics only — see DAILY_METRICS in
// api.py for why balances (ar_closing/ap_closing) are not offered here.
const DAILY_METRICS = [
	['gross_sales', 'Gross sales'],
	['discount', 'Discount given'],
	['return', 'Returns'],
	['collection_current,collection_prior,collection_unallocated', 'Money received'],
	['commission', 'Doctor commission'],
	['payroll', 'Staff payroll'],
	['expense', 'Other expense'],
	['refund', 'Refunds'],
	['ar_transfer_in', 'Debt created'],
	['payable_charged', 'Payable charged']
];

class ManagementDashboard {
	constructor(page) {
		this.page = page;
		this.state = {
			mode: 'compare', granularity: 'Monthly', company: null,
			period_label: null, a_label: null, b_label: null,
			drill_metric: 'gross_sales', drill_dimension: 'Item Group',
			daily_metric: 'gross_sales'
		};
	}

	async init() {
		this.filters = await this.call('get_filters', {});
		this.definitions = await this.call('get_definitions', {});
		this.state.company = this.filters.default_company ||
			(this.filters.companies || [])[0] || null;
		this.build_layout();
		this.build_filter_bar();
		this.rebuild_period_options();
		this.render_daily_controls();
		this.refresh();
		this.refresh_daily();
	}

	call(method, args) {
		return frappe.call({
			method: 'rasiin_insights.management_dashboard.utils.api.' + method,
			args: args
		}).then(r => r.message);
	}

	// ------------------------------------------------------- period buckets

	buckets() {
		/* Months grouped into the chosen granularity, with a label that spells
		   out which months it covers — "2026-Q1 (Jan–Mar)" leaves nothing to
		   guess about. Same rule as the server, so a label always maps back. */
		const months = this.filters.periods || [];
		const g = this.state.granularity;
		const out = [];
		months.forEach(m => {
			const [y, mm] = m.split('-');
			const n = parseInt(mm, 10);
			let key = m;
			if (g === 'Quarterly') key = `${y}-Q${Math.floor((n - 1) / 3) + 1}`;
			else if (g === 'Half-yearly') key = `${y}-H${n <= 6 ? 1 : 2}`;
			else if (g === 'Yearly') key = y;
			let b = out.find(x => x.key === key);
			if (!b) { b = { key: key, months: [] }; out.push(b); }
			b.months.push(m);
		});
		return out.map(b => {
			const first = parseInt(b.months[0].split('-')[1], 10);
			const last = parseInt(b.months[b.months.length - 1].split('-')[1], 10);
			const span = b.months.length > 1
				? ` (${MONTH_NAME[first - 1]}–${MONTH_NAME[last - 1]})`
				: ` (${MONTH_NAME[first - 1]})`;
			return {
				key: b.key,
				label: g === 'Monthly' ? `${b.key}${span}` : `${b.key}${span}`,
				from: b.months[0], to: b.months[b.months.length - 1]
			};
		});
	}

	bucket_by_label(label) {
		return this.buckets().find(b => b.label === label);
	}

	// -------------------------------------------------------------- filters

	build_filter_bar() {
		this.f_gran = this.page.add_field({
			fieldname: 'granularity', label: 'View by', fieldtype: 'Select',
			options: this.filters.granularities, default: 'Monthly',
			change: () => {
				this.state.granularity = this.f_gran.get_value();
				this.rebuild_period_options();
				this.refresh();
			}
		});

		this.f_period = this.page.add_field({
			fieldname: 'period', label: 'Period', fieldtype: 'Select', options: [],
			change: () => { this.state.period_label = this.f_period.get_value(); this.refresh(); }
		});

		this.f_a = this.page.add_field({
			fieldname: 'period_a', label: 'Compare', fieldtype: 'Select', options: [],
			change: () => {
				this.state.a_label = this.f_a.get_value();
				if (this.state.a_label === this.state.b_label) {
					frappe.show_alert({ message: 'Pick two different periods',
						indicator: 'orange' });
					return;
				}
				this.refresh();
			}
		});

		this.f_b = this.page.add_field({
			fieldname: 'period_b', label: 'With', fieldtype: 'Select', options: [],
			change: () => {
				this.state.b_label = this.f_b.get_value();
				if (this.state.a_label === this.state.b_label) {
					frappe.show_alert({ message: 'Pick two different periods',
						indicator: 'orange' });
					return;
				}
				this.refresh();
			}
		});

		this.f_company = this.page.add_field({
			fieldname: 'company', label: 'Company', fieldtype: 'Select',
			options: ['All companies'].concat(this.filters.companies || []),
			default: this.state.company,
			change: () => {
				const v = this.f_company.get_value();
				this.state.company = (v === 'All companies') ? null : v;
				this.refresh();
				this.refresh_daily();
			}
		});

		this.page.set_secondary_action('Refresh', () => this.refresh());
		this.page.add_menu_item('Print', () => window.print());
		this.sync_filter_visibility();
	}

	rebuild_period_options() {
		const labels = this.buckets().map(b => b.label);
		[this.f_period, this.f_a, this.f_b].forEach(f => {
			f.df.options = labels; f.refresh();
		});
		const n = labels.length;
		this.state.period_label = labels[n - 1];
		this.state.b_label = labels[n - 1];
		this.state.a_label = labels[Math.max(0, n - 2)];
		this.f_period.set_value(this.state.period_label);
		this.f_a.set_value(this.state.a_label);
		this.f_b.set_value(this.state.b_label);
		// f.refresh() above re-renders each control's wrapper and silently
		// undoes whatever display:none sync_filter_visibility() had set —
		// that was the whole bug: every field showing in both modes. Re-hide
		// after every rebuild, not just once at page load.
		this.sync_filter_visibility();
	}

	sync_filter_visibility() {
		const focus = this.state.mode === 'focus';
		this.f_period.$wrapper.css('display', focus ? '' : 'none');
		this.f_a.$wrapper.css('display', focus ? 'none' : '');
		this.f_b.$wrapper.css('display', focus ? 'none' : '');
	}

	// --------------------------------------------------------------- layout

	build_layout() {
		this.$c = $(`
			<div class="rd">
				<div class="rd-bar">
					<div class="rd-modes">
						<button class="rd-mode active" data-mode="compare">Compare two periods</button>
						<button class="rd-mode" data-mode="focus">Focus on one</button>
					</div>
					<div class="rd-scope"></div>
				</div>

				<div class="rd-health"></div>
				<div class="rd-hero"></div>

				<div class="rd-panel">
					<div class="rd-panel-head">
						<div class="rd-panel-title">The full picture</div>
						<button class="rd-btn rd-export-main">Export CSV</button>
					</div>
					<div class="rd-main"></div>
				</div>

				<div class="rd-panel">
					<div class="rd-panel-head">
						<div class="rd-panel-title">How the money moved</div>
						<div class="rd-panel-note">Every period available, so the
							selection sits in context</div>
					</div>
					<div class="rd-trend"></div>
				</div>

				<div class="rd-panel">
					<div class="rd-panel-head">
						<div class="rd-panel-title rd-drill-title">Where it came from</div>
						<div class="rd-drill-controls"></div>
						<button class="rd-btn rd-export-drill">Export CSV</button>
					</div>
					<div class="rd-drill-row">
						<div class="rd-drill-chart"></div>
						<div class="rd-drill-table"></div>
					</div>
				</div>

				<div class="rd-panel">
					<div class="rd-panel-head">
						<div class="rd-panel-title">Is it changing?</div>
						<div class="rd-panel-note">Top six, everything else grouped</div>
					</div>
					<div class="rd-drill-trend"></div>
				</div>

				<div class="rd-panel">
					<div class="rd-panel-head">
						<div class="rd-panel-title">Daily trend</div>
						<div class="rd-daily-controls"></div>
						<div class="rd-panel-note rd-daily-total"></div>
					</div>
					<div class="rd-daily-chart"></div>

					<div class="rd-panel-head" style="margin-top:20px">
						<div class="rd-panel-title">Day by day</div>
						<div class="rd-panel-note">Same story as the table above, one column
							per day. Receivables are month-end only — see Compare for those.</div>
						<button class="rd-btn rd-export-daily">Export CSV</button>
					</div>
					<div class="rd-daily-table"></div>
				</div>
			</div>
			<style>
			.rd { --ink:#0f172a; --dim:#64748b; --line:#e2e8f0; --navy:#1e3a5f;
				--good:#15803d; --bad:#b91c1c; --amber:#a16207;
				padding-bottom:60px; color:var(--ink); }

			.rd-bar { display:flex; align-items:center; justify-content:space-between;
				gap:12px; flex-wrap:wrap; margin:2px 0 16px; }
			.rd-modes { display:inline-flex; background:#f1f5f9; border-radius:10px;
				padding:3px; }
			.rd-mode { border:0; background:transparent; padding:7px 18px;
				border-radius:8px; font-size:13px; cursor:pointer; color:var(--dim); }
			.rd-mode.active { background:#fff; color:var(--navy); font-weight:600;
				box-shadow:0 1px 3px rgba(15,23,42,.12); }
			.rd-scope { font-size:12px; color:var(--dim); }

			.rd-health { padding:11px 15px; border-radius:10px; font-size:13px;
				margin-bottom:18px; border:1px solid transparent; }
			.rd-health.good { background:#f0fdf4; border-color:#bbf7d0; color:#14532d; }
			.rd-health.warn { background:#fffbeb; border-color:#fde68a; color:#78350f; }

			/* hero */
			.rd-hero { display:grid; gap:14px; margin-bottom:22px;
				grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); }
			.rd-hcard { border-radius:12px; padding:16px 18px; background:#fff;
				border:1px solid var(--line); position:relative; overflow:hidden; }
			.rd-hcard::before { content:''; position:absolute; left:0; top:0; bottom:0;
				width:4px; }
			.rd-hcard.rev::before { background:#1e3a5f; }
			.rd-hcard.cash::before { background:#15803d; }
			.rd-hcard.out::before { background:#b91c1c; }
			.rd-hcard.owe::before { background:#a16207; }
			.rd-hlab { font-size:11px; letter-spacing:.06em; text-transform:uppercase;
				color:var(--dim); }
			.rd-hval { font-size:27px; font-weight:700; margin-top:5px;
				font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
			.rd-hsub { font-size:12px; margin-top:5px; color:var(--dim); }
			.rd-up { color:var(--good); font-weight:600; }
			.rd-down { color:var(--bad); font-weight:600; }

			/* panels */
			.rd-panel { background:#fff; border:1px solid var(--line);
				border-radius:12px; padding:16px 18px 20px; margin-bottom:18px; }
			.rd-panel-head { display:flex; align-items:center; gap:12px;
				flex-wrap:wrap; margin-bottom:14px; }
			.rd-panel-title { font-size:14px; font-weight:700; letter-spacing:-.01em;
				margin-right:auto; }
			.rd-panel-note { font-size:11px; color:var(--dim); }
			.rd-btn { border:1px solid var(--line); background:#fff; border-radius:8px;
				padding:5px 12px; font-size:12px; cursor:pointer; color:var(--dim); }
			.rd-btn:hover { border-color:var(--navy); color:var(--navy); }

			/* tables */
			.rd-scroll { overflow:auto; }
			.rd-tbl { width:100%; border-collapse:collapse; font-size:13px;
				min-width:520px; }
			.rd-tbl th, .rd-tbl td { padding:8px 12px; }
			.rd-tbl thead th { font-size:11px; text-transform:uppercase;
				letter-spacing:.05em; color:var(--dim); text-align:right;
				border-bottom:2px solid var(--line); white-space:nowrap; }
			.rd-tbl thead th:first-child { text-align:left; }
			.rd-tbl td { text-align:right; font-variant-numeric:tabular-nums;
				border-bottom:1px solid #f1f5f9; }
			.rd-tbl td:first-child { text-align:left; }
			.rd-tbl tbody tr:hover td { background:#f8fafc; }
			.rd-lbl { cursor:pointer; }
			.rd-lbl:hover { color:var(--navy); text-decoration:underline; }
			.rd-grp td { font-size:11px; font-weight:700; text-transform:uppercase;
				letter-spacing:.06em; }
			.rd-grp.rev td { background:#eef2f8; color:#1e3a5f; }
			.rd-grp.cash td { background:#eefaf1; color:#15803d; }
			.rd-grp.out td { background:#fdeeee; color:#b91c1c; }
			.rd-grp.owe td { background:#fdf6e8; color:#8a5a00; }
			.rd-grp.ratio td { background:#f1f5f9; color:var(--dim); }
			.rd-strong td { font-weight:700; background:#fcfdfe;
				border-top:1px solid var(--line); border-bottom:1px solid var(--line); }
			.rd-muted td { color:var(--dim); font-style:italic; }
			.rd-neg { color:var(--bad); }
			.rd-chip { display:inline-block; padding:1px 7px; border-radius:20px;
				font-size:11px; font-weight:600; }
			.rd-chip.up { background:#dcfce7; color:#15803d; }
			.rd-chip.down { background:#fee2e2; color:#b91c1c; }
			.rd-flag { color:var(--amber); font-weight:700; margin-left:4px; }

			/* focus cards */
			.rd-grpblock { margin-bottom:20px; }
			.rd-grptitle { font-size:11px; font-weight:700; letter-spacing:.06em;
				text-transform:uppercase; color:var(--dim); margin-bottom:9px; }
			.rd-cards { display:grid; gap:11px;
				grid-template-columns:repeat(auto-fit,minmax(185px,1fr)); }
			.rd-card { border:1px solid var(--line); border-left:3px solid var(--line);
				border-radius:10px; padding:13px 15px; cursor:pointer; background:#fff; }
			.rd-card:hover { box-shadow:0 2px 8px rgba(15,23,42,.08); }
			.rd-card.rev { border-left-color:#1e3a5f; }
			.rd-card.cash { border-left-color:#15803d; }
			.rd-card.out { border-left-color:#b91c1c; }
			.rd-card.owe { border-left-color:#a16207; }
			.rd-card.ratio { border-left-color:#94a3b8; }
			.rd-card.strong { background:#fbfdff; }
			.rd-card.strong .rd-val { font-size:24px; }
			.rd-lab { font-size:11px; color:var(--dim); text-transform:uppercase;
				letter-spacing:.05em; }
			.rd-val { font-size:20px; font-weight:700; margin-top:4px;
				font-variant-numeric:tabular-nums; }
			.rd-sub { font-size:11px; color:var(--dim); margin-top:3px; }

			.rd-drill-row { display:grid; gap:20px; grid-template-columns:1.15fr 1fr; }
			.rd-drill-controls { display:flex; gap:8px; flex-wrap:wrap; }
			.rd-drill-controls select { padding:6px 10px; border-radius:8px;
				border:1px solid var(--line); font-size:12px; background:#fff; }
			.rd-bar-mini { height:3px; background:var(--navy); border-radius:2px;
				margin-top:4px; opacity:.75; }

			.rd-daily-controls { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
			.rd-daily-controls input[type=date] { padding:5px 8px; border-radius:8px;
				border:1px solid var(--line); font-size:12px; background:#fff; }
			.rd-daily-controls select { padding:6px 10px; border-radius:8px;
				border:1px solid var(--line); font-size:12px; background:#fff; }
			.rd-daily-total { font-weight:600; }

			@media (max-width:1000px) {
				.rd-drill-row { grid-template-columns:1fr; }
				.rd-hval { font-size:23px; }
			}
			@media (max-width:640px) {
				.rd-panel { padding:13px 12px 16px; }
				.rd-tbl th, .rd-tbl td { padding:7px 8px; }
			}

			@media print {
				.navbar, .page-head, .page-form, .rd-modes, .rd-btn,
				.rd-drill-controls, .layout-side-section, .page-actions { display:none !important; }
				.rd-panel { break-inside:avoid; border:1px solid #ccc; }
				.rd-hero { grid-template-columns:repeat(4,1fr); }
				.rd { padding:0; }
				body { background:#fff; }
				@page { size:A4 landscape; margin:12mm; }
			}
			</style>
		`).appendTo(this.page.body);

		$(this.page.wrapper).find('.page-form').removeClass('hide').show();

		this.$c.find('.rd-mode').on('click', (e) => {
			this.state.mode = e.currentTarget.dataset.mode;
			this.$c.find('.rd-mode').removeClass('active');
			$(e.currentTarget).addClass('active');
			this.sync_filter_visibility();
			this.refresh();
		});

		this.$c.find('.rd-export-main').on('click', () => this.export_main());
		this.$c.find('.rd-export-drill').on('click', () => this.export_drill());
		this.$c.find('.rd-export-daily').on('click', () => this.export_daily());
	}

	// --------------------------------------------------------------- render

	async refresh() {
		const buckets = this.buckets();
		if (!buckets.length) return;
		frappe.dom.freeze();
		try {
			if (this.state.mode === 'compare') await this.refresh_compare(buckets);
			else await this.refresh_focus(buckets);
			await this.render_trend(buckets);
			this.render_drill_controls();
			await this.refresh_drill();
		} finally {
			frappe.dom.unfreeze();
		}
	}

	async load_bucket(b) {
		/* One bucket's figures. Asking the server for exactly the months in the
		   bucket keeps the roll-up rules in one place — flows summed, balances
		   taken from the last month. */
		const [summary, health] = await Promise.all([
			this.call('get_summary', {
				from_period: b.from, to_period: b.to,
				granularity: 'Yearly', company: this.state.company
			}),
			this.call('get_health', {
				from_period: b.from, to_period: b.to, company: this.state.company
			})
		]);
		const p = (summary.periods || [])[0] || {};
		p.quality_score = health.worst;
		p._label = b.label;
		p._health = health;
		return p;
	}

	async refresh_compare(buckets) {
		const A = this.bucket_by_label(this.state.a_label) || buckets[buckets.length - 2] || buckets[0];
		const B = this.bucket_by_label(this.state.b_label) || buckets[buckets.length - 1];
		const [a, b] = await Promise.all([this.load_bucket(A), this.load_bucket(B)]);
		this.pair = { a: a, b: b, A: A, B: B };

		this.$c.find('.rd-scope').text(
			`${A.label} vs ${B.label} · ${this.state.company || 'All companies'}`);
		this.render_health(b._health.worst < a._health.worst ? b._health : a._health);
		this.render_hero(b, a, B.label, A.label);
		this.render_compare_table(a, b, A, B);
	}

	async refresh_focus(buckets) {
		const idx = buckets.findIndex(x => x.label === this.state.period_label);
		const cur = buckets[idx >= 0 ? idx : buckets.length - 1];
		const prev = buckets[(idx >= 0 ? idx : buckets.length - 1) - 1];
		const [c, p] = await Promise.all([
			this.load_bucket(cur), prev ? this.load_bucket(prev) : Promise.resolve(null)
		]);
		this.single = { cur: c, prev: p };

		this.$c.find('.rd-scope').text(
			`${cur.label} · ${this.state.company || 'All companies'}`);
		this.render_health(c._health);
		this.render_hero(c, p, cur.label, prev ? prev.label : null);
		this.render_focus_cards(c, p);
	}

	render_health(h) {
		const good = h.worst >= 95;
		this.$c.find('.rd-health').removeClass('good warn')
			.addClass(good ? 'good' : 'warn')
			.html(`<b>${good ? 'Data quality: good' : 'Data quality: read this'}</b> — ${h.message}
				Lowest traced share here: <b>${h.worst}%</b>.`);
	}

	value_of(p, line) {
		if (!p) return 0;
		if (line.key === 'quality_score') return flt(p.quality_score) / 100;
		return flt(p[line.key]);
	}

	render_hero(cur, prev, cur_label, prev_label) {
		const html = HERO.map(h => {
			const v = flt(cur[h.key]);
			let sub = cur_label;
			if (prev && flt(prev[h.key])) {
				const pv = flt(prev[h.key]);
				const ch = (v - pv) / Math.abs(pv);
				const up = ch >= 0;
				const good = h.bad ? !up : up;
				sub = `<span class="${good ? 'rd-up' : 'rd-down'}">${up ? '▲' : '▼'}
					${this.pct(Math.abs(ch))}</span> vs ${prev_label}`;
			}
			return `<div class="rd-hcard ${h.tone}">
				<div class="rd-hlab">${h.label}</div>
				<div class="rd-hval">${this.money(v)}</div>
				<div class="rd-hsub">${sub}</div>
			</div>`;
		}).join('');
		this.$c.find('.rd-hero').html(html);
	}

	render_compare_table(a, b, A, B) {
		this.export_rows = [['Figure', A.label, B.label, 'Change', '%']];
		let html = `<div class="rd-scroll"><table class="rd-tbl">
			<thead><tr><th>Figure</th><th>${A.key}</th><th>${B.key}</th>
			<th>Change</th><th>%</th></tr></thead><tbody>`;

		GROUPS.forEach(g => {
			html += `<tr class="rd-grp ${g.tone}"><td colspan="5">${g.title}</td></tr>`;
			this.export_rows.push([g.title, '', '', '', '']);
			g.lines.forEach(line => {
				const av = this.value_of(a, line);
				const bv = this.value_of(b, line);
				const is_pct = g.pct || line.raw_pct;
				const fmt = v => is_pct ? this.pct(v) : this.money(line.sign === -1 ? -v : v);
				const diff = bv - av;
				const pctch = av ? diff / Math.abs(av) : 0;
				const up = diff >= 0;
				const good = (line.warn || line.sign === -1) ? !up : up;
				const chip = diff === 0 ? '' :
					`<span class="rd-chip ${good ? 'up' : 'down'}">${up ? '▲' : '▼'} ${this.pct(Math.abs(pctch))}</span>`;
				const cls = [line.strong ? 'rd-strong' : '', line.muted ? 'rd-muted' : ''
					].filter(Boolean).join(' ');

				html += `<tr class="${cls}">
					<td><span class="rd-lbl" data-metric="${line.metric || ''}"
						data-key="${line.key}" data-label="${line.label}">${line.label}</span>
						${line.warn ? '<span class="rd-flag">!</span>' : ''}</td>
					<td class="${line.sign === -1 ? 'rd-neg' : ''}">${fmt(av)}</td>
					<td class="${line.sign === -1 ? 'rd-neg' : ''}">${fmt(bv)}</td>
					<td>${is_pct ? this.pct(diff) : this.money(diff)}</td>
					<td>${chip}</td></tr>`;

				this.export_rows.push([line.label, is_pct ? (av * 100).toFixed(2) : av.toFixed(2),
					is_pct ? (bv * 100).toFixed(2) : bv.toFixed(2),
					is_pct ? (diff * 100).toFixed(2) : diff.toFixed(2),
					(pctch * 100).toFixed(2)]);
			});
		});

		html += '</tbody></table></div>';
		this.$c.find('.rd-main').html(html);
		this.$c.find('.rd-panel-title').eq(0).text(`${A.key} compared with ${B.key}`);
		this.bind_explain();
	}

	render_focus_cards(cur, prev) {
		this.export_rows = [['Figure', 'Value']];
		const html = GROUPS.map(g => `
			<div class="rd-grpblock">
				<div class="rd-grptitle">${g.title}</div>
				<div class="rd-cards">${g.lines.map(line => {
					const v = this.value_of(cur, line);
					const is_pct = g.pct || line.raw_pct;
					const shown = is_pct ? this.pct(v) : this.money(v);
					this.export_rows.push([line.label, is_pct ? (v * 100).toFixed(2) : v.toFixed(2)]);
					let delta = '';
					if (prev) {
						const pv = this.value_of(prev, line);
						if (pv) {
							const ch = (v - pv) / Math.abs(pv);
							const up = ch >= 0;
							const good = (line.warn || line.sign === -1) ? !up : up;
							delta = `<div class="rd-sub"><span class="${good ? 'rd-up' : 'rd-down'}">
								${up ? '▲' : '▼'} ${this.pct(Math.abs(ch))}</span> vs previous</div>`;
						}
					}
					return `<div class="rd-card ${g.tone} ${line.strong ? 'strong' : ''}"
						data-metric="${line.metric || ''}" data-key="${line.key}"
						data-label="${line.label}">
						<div class="rd-lab">${line.label}</div>
						<div class="rd-val">${shown}</div>${delta}
						${line.note ? `<div class="rd-sub">${line.note}</div>` : ''}
					</div>`;
				}).join('')}</div>
			</div>`).join('');

		this.$c.find('.rd-main').html(html);
		this.$c.find('.rd-panel-title').eq(0).text('The full picture');
		this.bind_explain();
	}

	bind_explain() {
		this.$c.find('.rd-lbl, .rd-card').on('click', (e) => {
			const d = e.currentTarget.dataset;
			this.explain(d.metric, d.label, d.key);
		});
	}

	explain(metric, label, key) {
		const m = metric && this.definitions.metrics[metric];
		const derived = this.definitions.derived || {};
		let body = '';
		if (m) {
			body = `<p><b>What it means</b><br>${m.means}</p>
				<p><b>Where it comes from</b><br>${m.source}</p>
				<p><b>How it is calculated</b><br><code>${m.formula}</code></p>
				<p><b>Worth knowing</b><br>${m.caveat}</p>`;
		}
		if (derived[key]) {
			body += `<p><b>Formula</b><br><code>${key} = ${derived[key]}</code></p>`;
		}
		if (!body) body = '<p>Derived from the figures above it.</p>';

		// The story continues here: "what does this mean" and "show me the
		// report behind it" are two different questions. This dialog answers
		// the first; its primary action jumps straight into the drill-down
		// panel already filtered on this exact metric, answering the second.
		const dialog = new frappe.ui.Dialog({ title: label, fields: [
			{ fieldtype: 'HTML', options: body }
		] });
		if (metric) {
			dialog.set_primary_action('See the breakdown', () => {
				dialog.hide();
				this.jump_to_drill(metric, label);
			});
		}
		dialog.show();
	}

	jump_to_drill(metric, label) {
		this.state.drill_metric = metric;
		this.render_drill_controls();
		this.refresh_drill();
		const el = this.$c.find('.rd-drill-row')[0];
		if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
	}

	async render_trend(buckets) {
		/* Always the whole history, whatever is selected — a two-period
		   comparison with no context is how people mistake a seasonal dip for
		   a collapse. */
		const first = buckets[0], last = buckets[buckets.length - 1];
		const summary = await this.call('get_summary', {
			from_period: first.from, to_period: last.to,
			granularity: this.state.granularity, company: this.state.company
		});
		const p = summary.periods || [];
		new frappe.Chart(this.$c.find('.rd-trend')[0], {
			data: {
				labels: p.map(x => x.period),
				datasets: [
					{ name: 'Net sales', values: p.map(x => x.net_sales) },
					{ name: 'Money received', values: p.map(x => x.total_collections) },
					{ name: 'Money out', values: p.map(x => x.money_out) }
				]
			},
			type: 'line', height: 250, colors: ['#1e3a5f', '#15803d', '#b91c1c'],
			lineOptions: { hideDots: 0, regionFill: 0 },
			axisOptions: { xIsSeries: 1 },
			tooltipOptions: { formatTooltipY: d => this.money(d) }
		});
	}

	render_drill_controls() {
		const dims = this.filters.dimensions || [];
		this.$c.find('.rd-drill-controls').html(`
			<select class="rd-metric">${DRILL_METRICS.map(([v, l]) =>
				`<option value="${v}" ${v === this.state.drill_metric ? 'selected' : ''}>${l}</option>`
			).join('')}</select>
			<select class="rd-dimension">${dims.map(d =>
				`<option value="${d}" ${d === this.state.drill_dimension ? 'selected' : ''}>by ${d}</option>`
			).join('')}</select>`);
		this.$c.find('.rd-metric').on('change', e => {
			this.state.drill_metric = e.target.value; this.refresh_drill();
		});
		this.$c.find('.rd-dimension').on('change', e => {
			this.state.drill_dimension = e.target.value; this.refresh_drill();
		});
	}

	// ---------------------------------------------------------- daily trend

	render_daily_controls() {
		/* Independent of the Period/Compare filters above — its own date
		   range, defaulting to the last 30 days. That decouples "what month
		   am I looking at" from "show me day by day", which is what was
		   actually asked for (Question 31 in the catalog), not a full daily
		   version of the whole page. */
		const today = frappe.datetime.get_today();
		const from = frappe.datetime.add_days(today, -29);
		this.$c.find('.rd-daily-controls').html(`
			<input type="date" class="rd-daily-from" value="${from}">
			<span style="font-size:12px;color:var(--dim)">to</span>
			<input type="date" class="rd-daily-to" value="${today}">
			<select class="rd-daily-metric">${DAILY_METRICS.map(([v, l]) =>
				`<option value="${v}" ${v === this.state.daily_metric ? 'selected' : ''}>${l}</option>`
			).join('')}</select>`);
		this.$c.find('.rd-daily-from, .rd-daily-to').on('change', () => this.refresh_daily());
		this.$c.find('.rd-daily-metric').on('change', (e) => {
			this.state.daily_metric = e.target.value;
			this.refresh_daily();
		});
	}

	async refresh_daily() {
		const from = this.$c.find('.rd-daily-from').val();
		const to = this.$c.find('.rd-daily-to').val();
		if (!from || !to) return;
		const [trend, summary] = await Promise.all([
			this.call('get_daily_trend', { from_date: from, to_date: to,
				metric: this.state.daily_metric, company: this.state.company }),
			this.call('get_daily_summary', { from_date: from, to_date: to,
				company: this.state.company })
		]);
		this.render_daily_chart(trend);
		this.render_daily_table(summary.days, summary.total);
	}

	render_daily_chart(res) {
		this.$c.find('.rd-daily-total').text(`Total: ${this.money(res.total)}`);
		new frappe.Chart(this.$c.find('.rd-daily-chart')[0], {
			data: { labels: res.dates, datasets: [{ name: 'Amount', values: res.values }] },
			type: 'line', height: 220, colors: ['#1e3a5f'],
			lineOptions: { hideDots: 0, regionFill: 1 },
			tooltipOptions: { formatTooltipY: d => this.money(d) }
		});
	}

	day_label(date_str) {
		const [y, m, d] = date_str.split('-');
		return `${MONTH_NAME[parseInt(m, 10) - 1]} ${parseInt(d, 10)}`;
	}

	render_daily_table(days, total) {
		/* Exactly the Compare table's shape — same GROUPS, same signs, same
		   formulas — just one column per day instead of two columns for two
		   periods. Receivables are dropped: there is no daily balance. */
		const groups = GROUPS.filter(g => g.title !== 'What we owe, what we are owed');

		this.daily_table_rows = [['Figure'].concat(days.map(d => this.day_label(d.date)), 'Total')];

		let html = `<div class="rd-scroll"><table class="rd-tbl">
			<thead><tr><th>Figure</th>${days.map(d =>
				`<th>${this.day_label(d.date)}</th>`).join('')}<th>Total</th></tr></thead>
			<tbody>`;

		groups.forEach(g => {
			html += `<tr class="rd-grp ${g.tone}"><td colspan="${days.length + 2}">${g.title}</td></tr>`;
			this.daily_table_rows.push([g.title]);

			g.lines.forEach(line => {
				const is_pct = g.pct || line.raw_pct;
				const fmt = v => is_pct ? this.pct(v) : this.money(line.sign === -1 ? -v : v);
				const vals = days.map(d => this.value_of(d, line));
				const tot = this.value_of(total, line);
				const cls = [line.strong ? 'rd-strong' : '', line.muted ? 'rd-muted' : ''
					].filter(Boolean).join(' ');

				html += `<tr class="${cls}">
					<td>${line.label}${line.warn ? '<span class="rd-flag">!</span>' : ''}</td>
					${vals.map(v => `<td class="${line.sign === -1 ? 'rd-neg' : ''}">${fmt(v)}</td>`).join('')}
					<td class="${line.sign === -1 ? 'rd-neg' : ''}">${fmt(tot)}</td></tr>`;

				this.daily_table_rows.push([line.label].concat(
					vals.map(v => is_pct ? (v * 100).toFixed(2) : v.toFixed(2)),
					is_pct ? (tot * 100).toFixed(2) : tot.toFixed(2)));
			});
		});

		html += '</tbody></table></div>';
		this.$c.find('.rd-daily-table').html(html);
	}

	export_daily() {
		if (!this.daily_table_rows) return;
		const from = this.$c.find('.rd-daily-from').val();
		const to = this.$c.find('.rd-daily-to').val();
		this.download(this.daily_table_rows,
			`daily_${from}_to_${to}.csv`.replace(/[^\w.\-]+/g, '_'));
	}

	blank_label(dimension) {
		/* "Not recorded" implies someone forgot. Usually the transaction has no
		   such attribute at all — a journal entry has no cashier and no item
		   line, and never will. */
		return {
			'Cashier': 'No cashier (journal entry)',
			'Item Group': 'No item line (journal entry)',
			'Merchant Account': 'No merchant account',
			'Mode of Payment': 'No payment mode',
			'Practitioner': 'No practitioner',
			'Sales Type': 'Sales type not selected',
			'Payer Type': 'Payer not classified',
			'Service Line': 'Service line unresolved',
			'Quality Flag': 'Clean'
		}[dimension] || 'Not applicable';
	}

	async refresh_drill() {
		const s = this.state;
		const buckets = this.buckets();
		const compare = s.mode === 'compare';
		const A = compare ? (this.bucket_by_label(s.a_label) || buckets[0]) : null;
		const B = compare ? (this.bucket_by_label(s.b_label) || buckets[buckets.length - 1])
			: (this.bucket_by_label(s.period_label) || buckets[buckets.length - 1]);

		const base = { metric: s.drill_metric, dimension_type: s.drill_dimension,
			company: s.company };
		const calls = [this.call('get_dimension',
			Object.assign({}, base, { from_period: B.from, to_period: B.to }))];
		if (compare) {
			calls.push(this.call('get_dimension',
				Object.assign({}, base, { from_period: A.from, to_period: A.to })));
		}
		calls.push(this.call('get_dimension_trend', Object.assign({}, base, {
			from_period: buckets[0].from, to_period: buckets[buckets.length - 1].to,
			granularity: s.granularity, top: 6 })));

		const res = await Promise.all(calls);
		const bDim = res[0];
		const aDim = compare ? res[1] : null;
		const trend = res[res.length - 1];

		const label = x => x.label === 'Not recorded'
			? this.blank_label(s.drill_dimension) : (x.label || '');

		this.$c.find('.rd-drill-title').text(
			compare ? `Where it came from — ${A.key} vs ${B.key}`
			: `Where it came from — ${B.label}`);

		const top = bDim.rows.slice(0, 12);
		new frappe.Chart(this.$c.find('.rd-drill-chart')[0], {
			data: {
				labels: top.map(label),
				datasets: compare
					? [{ name: A.key, values: top.map(x => {
							const m = (aDim.rows || []).find(y => y.label === x.label);
							return m ? m.amount : 0; }) },
					   { name: B.key, values: top.map(x => x.amount) }]
					: [{ name: 'Amount', values: top.map(x => x.amount) }]
			},
			type: 'bar', height: 340,
			colors: compare ? ['#94a3b8', '#1e3a5f'] : ['#1e3a5f'],
			tooltipOptions: { formatTooltipY: d => this.money(d) }
		});

		this.drill_rows = [compare ? ['Dimension', A.key, B.key, 'Change']
			: ['Dimension', 'Amount', 'Share']];

		const body = bDim.rows.map(x => {
			const name = label(x);
			if (!compare) {
				this.drill_rows.push([name, flt(x.amount).toFixed(2),
					(flt(x.share) * 100).toFixed(2)]);
				return `<tr><td>${frappe.utils.escape_html(name)}</td>
					<td>${this.money(x.amount)}</td>
					<td>${this.pct(x.share)}<div class="rd-bar-mini"
						style="width:${Math.round(x.share * 100)}%"></div></td></tr>`;
			}
			const m = (aDim.rows || []).find(y => y.label === x.label);
			const av = m ? flt(m.amount) : 0;
			const diff = flt(x.amount) - av;
			this.drill_rows.push([name, av.toFixed(2), flt(x.amount).toFixed(2),
				diff.toFixed(2)]);
			return `<tr><td>${frappe.utils.escape_html(name)}</td>
				<td>${this.money(av)}</td><td>${this.money(x.amount)}</td>
				<td class="${diff < 0 ? 'rd-neg' : ''}">${this.money(diff)}</td></tr>`;
		}).join('');

		this.$c.find('.rd-drill-table').html(`
			<div class="rd-scroll" style="max-height:340px"><table class="rd-tbl">
				<thead><tr><th>${s.drill_dimension}</th>
				${compare ? `<th>${A.key}</th><th>${B.key}</th><th>Change</th>`
					: '<th>Amount</th><th>Share</th>'}</tr></thead>
				<tbody>${body}</tbody>
				<tfoot><tr class="rd-strong"><td>Total</td>
					${compare ? `<td>${this.money(aDim.total)}</td>
						<td>${this.money(bDim.total)}</td>
						<td>${this.money(flt(bDim.total) - flt(aDim.total))}</td>`
					: `<td>${this.money(bDim.total)}</td><td></td>`}
				</tr></tfoot>
			</table></div>`);

		new frappe.Chart(this.$c.find('.rd-drill-trend')[0], {
			data: { labels: trend.periods, datasets: trend.series },
			type: 'bar', height: 300, barOptions: { stacked: 1 },
			tooltipOptions: { formatTooltipY: d => this.money(d) }
		});
	}

	// --------------------------------------------------------------- export

	to_csv(rows) {
		return rows.map(r => r.map(c => {
			const s = String(c === null || c === undefined ? '' : c);
			return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
		}).join(',')).join('\n');
	}

	download(rows, name) {
		const blob = new Blob(['\ufeff' + this.to_csv(rows)],
			{ type: 'text/csv;charset=utf-8;' });
		const a = document.createElement('a');
		a.href = URL.createObjectURL(blob);
		a.download = name;
		a.click();
		URL.revokeObjectURL(a.href);
	}

	export_main() {
		if (!this.export_rows) return;
		const scope = this.state.mode === 'compare'
			? `${this.state.a_label}_vs_${this.state.b_label}` : this.state.period_label;
		this.download(this.export_rows,
			`management_summary_${scope}.csv`.replace(/[^\w.\-]+/g, '_'));
	}

	export_drill() {
		if (!this.drill_rows) return;
		this.download(this.drill_rows,
			`${this.state.drill_dimension}_${this.state.drill_metric}.csv`
				.replace(/[^\w.\-]+/g, '_'));
	}

	money(v) {
		return format_currency(flt(v), frappe.defaults.get_default('currency'), 0);
	}

	pct(v) { return (flt(v) * 100).toFixed(1) + '%'; }
}